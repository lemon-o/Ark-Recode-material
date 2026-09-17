"""Fetch runtime assets straight from the official Ark Re:Code patch CDN.

`sync_assets.py` maps a hand-maintained local archive (`assets_full/`) onto the
runtime `assets/` tree.  This script removes that manual step for the asset
families the game itself publishes, by walking the same HTTP path the WebGL
client walks:

1.  ``PUT {GAME_ROUTER}`` with ``route=GameServerDBSettingHandler.QueryBulletinInfoResult``
    -- the client's unauthenticated bootstrap call.  It answers with
    ``Info.PathDomain`` (the patch host), ``Info.PatchPosFix`` and
    ``Info.NewCatalogName``.
2.  ``GET {PathDomain}{PatchPosFix}/WebGL/{NewCatalogName}.json`` -- the
    Addressables content catalog (tens of MB, cached on disk).
3.  Decode the catalog's ``m_KeyDataString`` / ``m_BucketDataString`` /
    ``m_EntryDataString`` blobs into an ``address -> AssetBundle URL`` map.
4.  ``GET`` the bundles that carry each wanted asset and pull the named texture
    out of them with UnityPy.  An asset's dependency set can span several
    bundles (a Sprite in one, its texture in a sibling ``*.spriteatlas``), so
    every bundle in the set is loaded into a single environment.

Because an Addressables address *is* the game's asset path, and that path is
built from the same IDs the game's data tables use, new content is picked up
automatically -- there is no mapping table to maintain.

This script only ever writes files it can genuinely produce.  It never deletes
anything, so the families that are *not* on the WebGL CDN (equipment, set,
stat and slot icons, and the server-side data tables) can keep coming from
`sync_assets.py` / the local archive in parallel.

Requires UnityPy (``python -m pip install UnityPy``).  It is a tools-only
dependency and is deliberately absent from ``requirements.txt``.

Usage:

    python tools/fetch_assets.py
    python tools/fetch_assets.py --only avatars --dry-run
    python tools/fetch_assets.py --path-domain https://patch-arkre-labs.ecchi.xxx/GamePatch
"""

from __future__ import annotations

import argparse
import ast
import base64
import concurrent.futures
import hashlib
import json
import re
import struct
import sys
import threading
import time
import uuid
from io import BytesIO
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGET = ROOT / "assets"
DEFAULT_CACHE = ROOT / "build" / "asset-fetch-cache"

BOOTSTRAP_ROUTE = "GameServerDBSettingHandler.QueryBulletinInfoResult"
PATCH_DOMAIN_PLACEHOLDER = "PatchDomain"
WEBGL_SEGMENT = "/WebGL/"
CATALOG_FALLBACK_NAME = "catalog"

TEXTURE_TYPE = "UnityEngine.Texture2D"
SPRITE_TYPE = "UnityEngine.Sprite"
ASSET_BUNDLE_PROVIDER = "AssetBundleProvider"

# `sync_assets.py` reads avatars out of `<archive>/团员/头像/<ID>/Icon_Head_S_<ID>.png`
# and writes `avatars/<ID>.png`.  The game path below is the same asset.
#
# `AVATAR_ID_REMAP` in `sync_assets.py` translates an *asset* id into the *role*
# id the app looks up, so the catalog address keeps the asset id while the file
# name takes the role id.  `_avatar_family()` reads that table and gives it
# priority over a raw catalog entry sharing the same name.
AVATAR_PATTERN = re.compile(r"Assets/Game/Hero/(H\d+)/Img/Icon_Head_S_\1\.png")

# Only these two UI icons exist in the WebGL catalog; `sync_assets.py`'s
# `UI_MANUAL_FILES` and the remaining `UI_SOURCE_FILES` entries do not.
UI_ICONS = {
    "activity.png": "Lobby_SideStory",
    "store.png": "Lobby_Shop",
}

# ---------------------------------------------------------------- 图标白名单
#
# catalog 里 7000+ 张 PNG 大半是 CG / 立绘 / 羁绊图（`*_Sex_LoveTalk.png`、
# `Assets/Game/CG/A*/`、`CG_H*.png`），那些是 18+ 内容，绝不能进素材仓库。
# 这里用**白名单**而不是黑名单：只收文件名以 `Icon` 开头、且路径落在 Hero/Img
# 或 Icon/ 下的资产——CG 与羁绊图从来不叫这个名字，白名单天然碰不到它们。
#
#   heads   Icon_Head_{S,M,L,B}_<ID>   四种尺寸的头像框（S=已有 avatars 家族）
#   skills  Icon_Skill_<ID>_<nnn>      技能图标
#   icons   Assets/Game/Icon/{Function,Constellation}  功能/星座图标
#
# 两个捕获组：1=资产 ID（做 --ids 过滤与重映射），2=输出文件名（含 .png，
# 吃过一次亏：把 .png 放在组外，落盘的文件没有扩展名，glob("*.png") 一张都看不见）。
# 尺寸只收 L/M/S——B（最大号，~160KB/张）按用户要求排除，heads 体量砍掉约 1/4。
HERO_ICON_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "heads",
        re.compile(r"^Assets/Game/Hero/([A-Z]\d+)/Img/(Icon_Head_[LMS]_[A-Z]*\d[\w-]*?\.png)$"),
    ),
    (
        "skills",
        re.compile(r"^Assets/Game/Hero/([A-Z]\d+)/Img/(Icon_Skill_[A-Z]*\d[\w-]*?\.png)$"),
    ),
)
GAME_ICON_PATTERN = re.compile(
    r"^Assets/Game/Icon/(?:Function|Constellation)/([A-Za-z][\w]*)\.png$"
)

# 图集家族：道具/装备/套装图标不是独立 .png，而是 Icon/*.spriteatlas 的子资源
# （catalog 里只有图集本身的地址，单个图标按 Sprite 名索引）。所以这一族按
# "整包下载、枚举 Sprite 导出"的方式解包，输出名 = Sprite 名（游戏自己的
# 图标键，如 45StarHeroTicket / E001_1 / Attack；与 Item.txt 的 Icon 列
# `Icon/Item[<名字>]` 对应）。键=图集名，值=输出目录。
ATLAS_ADDRESS_PATTERN = re.compile(r"^Assets/Game/Icon/(Item|Equip|EquipSet)\.spriteatlas$")
ATLAS_CATEGORIES = {"Item": "items", "Equip": "equip", "EquipSet": "equipset"}


def _config_constants() -> dict[str, object]:
    """Read the game endpoints out of `backend/config.py` without importing it.

    Importing `backend.config` runs a settings migration against `settings.json`;
    a tools script has no business doing that.  Mirroring `sync_assets.py`'s
    `_set_icon_map()` approach keeps `config.py` the single source of truth.
    """
    wanted = {"GAME_ROUTER", "GAME_ORIGIN", "GAME_REFERER", "HTTP_TIMEOUT"}
    tree = ast.parse((ROOT / "backend" / "config.py").read_text(encoding="utf-8"))
    found: dict[str, object] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(
            "backend/config.py is missing a literal for: " + ", ".join(sorted(missing))
        )
    return found


def _avatar_id_remap() -> dict[str, str]:
    """Read `AVATAR_ID_REMAP` out of `tools/sync_assets.py` without importing it.

    `sync_assets.py` owns the mapping because it also has to rewrite the source
    file name (`Icon_Head_S_H801.png` lives in a folder that really holds H804).
    Both tools touching `assets/avatars/` must agree, so this reads the literal
    instead of keeping a second copy that can drift.

    A missing or non-literal table is not fatal -- it just means no remapping.
    """
    path = ROOT / "tools" / "sync_assets.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "AVATAR_ID_REMAP":
                try:
                    table = ast.literal_eval(node.value)
                except ValueError:
                    return {}
                if isinstance(table, dict):
                    return {
                        str(key): str(value)
                        for key, value in table.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
    return {}


def _read_key(buffer: bytes, offset: int) -> str | int:
    """Read one Addressables key.

    Types seen in the live catalog: 0 ascii, 1 utf-16, 2 packed uint32, and 4 --
    a composite of an int32 followed by a nested key (only two of those exist,
    both label-like, e.g. ``Sprite Assets/EmojiOne``).
    """
    kind = buffer[offset]
    offset += 1
    if kind == 0:
        length = struct.unpack_from("<i", buffer, offset)[0]
        return buffer[offset + 4: offset + 4 + length].decode("ascii", "replace")
    if kind == 1:
        length = struct.unpack_from("<i", buffer, offset)[0]
        return buffer[offset + 4: offset + 4 + length * 2].decode("utf-16-le", "replace")
    if kind == 2:
        return struct.unpack_from("<I", buffer, offset)[0]
    if kind == 4:
        return _read_key(buffer, offset + 4)
    raise RuntimeError(f"unknown Addressables key type {kind} at offset {offset - 1}")


def _read_keys(buffer: bytes, buckets: list[tuple[int, list[int]]]) -> list[str | int | None]:
    """Read every bucket's key, tolerating a key we cannot decode.

    Each key is read from its own offset rather than sequentially, so one bad
    key never shifts the rest.  Returning ``None`` for it keeps the list
    index-aligned with the bucket table, which matters because entry
    ``dependencyKey`` / ``primaryKey`` fields are bucket *indices*.
    """
    keys: list[str | int | None] = []
    for offset, _ in buckets:
        try:
            keys.append(_read_key(buffer, offset))
        except (IndexError, struct.error, RuntimeError):
            keys.append(None)
    return keys


class ContentCatalog:
    """Addressables content catalog (JSON format, built by Addressables 1.22).

    The three payload fields are opaque base64 blobs.  Field layout, verified
    against a live catalog:

    * ``m_KeyDataString``: keys addressed by *byte offset*; a leading int32 holds
      the key count, and the bucket table points into the rest.
    * ``m_BucketDataString``: ``int32 count`` then, per bucket,
      ``int32 keyOffset, int32 entryCount, int32 entryCount * entries``.
    * ``m_EntryDataString``: ``int32 count`` then, per entry, seven int32s:
      ``internalId, provider, dependencyKey, dependencyHash, dataIndex,
      primaryKey, resourceType``.  **``dependencyKey`` and ``primaryKey`` are
      bucket indices, not byte offsets** -- that is the part that is easy to get
      wrong, and getting it wrong yields plausible-looking but useless keys.
    """

    def __init__(self, payload: dict) -> None:
        self._ids: list[str] = payload["m_InternalIds"]
        self._providers: list[str] = payload["m_ProviderIds"]
        self._types: list[str] = [row["m_ClassName"] for row in payload["m_resourceTypes"]]
        key_data = base64.b64decode(payload["m_KeyDataString"])
        self._buckets = self._read_buckets(payload["m_BucketDataString"])
        self._keys = _read_keys(key_data, self._buckets)
        self._bucket_of: dict[str | int, int] = {}
        for index, key in enumerate(self._keys):
            if key is not None:
                self._bucket_of.setdefault(key, index)
        self._entries = self._read_entries(payload["m_EntryDataString"])

    @staticmethod
    def _read_buckets(raw: str) -> list[tuple[int, list[int]]]:
        buffer = base64.b64decode(raw)
        count = struct.unpack_from("<i", buffer, 0)[0]
        offset = 4
        buckets = []
        for _ in range(count):
            key_offset, entry_count = struct.unpack_from("<2i", buffer, offset)
            offset += 8
            entries = list(struct.unpack_from(f"<{entry_count}i", buffer, offset))
            offset += entry_count * 4
            buckets.append((key_offset, entries))
        return buckets

    @staticmethod
    def _read_entries(raw: str) -> list[tuple[int, ...]]:
        buffer = base64.b64decode(raw)
        count = struct.unpack_from("<i", buffer, 0)[0]
        if len(buffer) != 4 + count * 28:
            raise RuntimeError("unexpected Addressables entry table size")
        return [
            struct.unpack_from("<7i", buffer, 4 + index * 28) for index in range(count)
        ]

    def resource_type(self, index: int) -> str:
        slot = self._entries[index][6]
        return self._types[slot] if 0 <= slot < len(self._types) else ""

    def entries_for(self, address: str) -> list[int]:
        bucket = self._bucket_of.get(address)
        return list(self._buckets[bucket][1]) if bucket is not None else []

    def address(self, index: int) -> str:
        return self._ids[self._entries[index][0]]

    def _provider(self, index: int) -> str:
        slot = self._entries[index][1]
        return self._providers[slot] if 0 <= slot < len(self._providers) else ""

    def bundles_for(self, address: str) -> tuple[str, ...]:
        """Return every AssetBundle that carries ``address``.

        A dependency bucket may list more than one bundle: the owning bundle
        plus siblings it references.  Avatar icons hit exactly this case -- the
        small Sprite sits in one bundle while its texture lives in a separate
        ``*.spriteatlas`` bundle, so both have to be loaded together before the
        Sprite resolves to an image.
        """
        found: list[str] = []
        for index in self.entries_for(address):
            if not 0 <= self._entries[index][2] < len(self._keys):
                continue
            dependency = self._keys[self._entries[index][2]]
            bucket = self._bucket_of.get(dependency)
            if bucket is None:
                continue
            for candidate in self._buckets[bucket][1]:
                if ASSET_BUNDLE_PROVIDER not in self._provider(candidate):
                    continue
                internal_id = self._ids[self._entries[candidate][0]]
                if internal_id not in found:
                    found.append(internal_id)
        return tuple(found)

    def match(self, pattern: re.Pattern[str]) -> dict[str, str]:
        """Return ``{capture group ``1``: address}`` for every matching address."""
        found: dict[str, str] = {}
        for internal_id in self._ids:
            match = pattern.fullmatch(internal_id)
            if match:
                found.setdefault(match.group(1), internal_id)
        return found


def _bundle_url(base: str, internal_id: str) -> str:
    """Rewrite the catalog's ``PatchDomain`` placeholder onto the resolved host."""
    marker = f"://{PATCH_DOMAIN_PLACEHOLDER}"
    index = internal_id.find(marker)
    if index < 0:
        return internal_id
    return base.rstrip("/") + internal_id[index + len(marker):]


class PatchServer:
    """Resolves the patch host and serves catalog + bundles."""

    def __init__(self, client: httpx.Client, constants: dict, cache: Path, refresh: bool):
        self._client = client
        self._constants = constants
        self._cache = cache
        self._refresh = refresh
        self._bundle_dir = cache / "bundles"
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        """One lock per URL: workers may race for the same bundle."""
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _get(self, url: str, attempts: int = 4) -> bytes:
        """GET with retries: the patch CDN sometimes drops a body mid-transfer."""
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(url)
                response.raise_for_status()
                payload = response.content
                declared = response.headers.get("Content-Length")
                if declared and int(declared) != len(payload):
                    raise RuntimeError(
                        f"truncated body: {len(payload)} of {declared} bytes"
                    )
                return payload
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                last = exc
                if attempt < attempts:
                    time.sleep(1.0 * attempt)
        raise RuntimeError(f"GET {url} failed after {attempts} attempts: {last}")

    def resolve_base(self) -> tuple[str, str]:
        """Return ``(base url, catalog name)`` from the client's bootstrap call."""
        body = json.dumps({"data": {}, "route": BOOTSTRAP_ROUTE}).encode("utf-8")
        response = self._client.request("PUT", str(self._constants["GAME_ROUTER"]), content=body)
        response.raise_for_status()
        info = (response.json() or {}).get("Info") or {}
        domain = str(info.get("PathDomain") or info.get("PathDomains") or "").rstrip("/")
        if not domain:
            raise RuntimeError("bootstrap response carried no PathDomain")
        base = domain + str(info.get("PatchPosFix") or "")
        catalog = str(info.get("NewCatalogName") or CATALOG_FALLBACK_NAME)
        return base, catalog

    def load_catalog(self, base: str, name: str) -> ContentCatalog:
        path = self._cache / f"{name}.json"
        if self._refresh or not path.is_file():
            url = f"{base.rstrip('/')}{WEBGL_SEGMENT}{name}.json"
            print(f"Downloading catalog {url}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._get(url, attempts=3))
        try:
            return ContentCatalog(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as exc:
            raise RuntimeError(f"unusable catalog {path}: {exc}") from exc

    def fetch_bundle(self, base: str, internal_id: str) -> Path:
        url = _bundle_url(base, internal_id)
        path = self._bundle_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ".bundle")
        with self._lock_for(url):
            if not self._refresh and path.is_file():
                return path
            payload = self._get(url)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.part")
            temporary.write_bytes(payload)
            temporary.replace(path)
            return path


def _import_unitypy():
    try:
        import UnityPy
    except ImportError as exc:  # pragma: no cover - dependency hint
        raise RuntimeError(
            "UnityPy is required to unpack AssetBundles; run: python -m pip install UnityPy"
        ) from exc
    return UnityPy


def extract_texture(bundles: list[Path], asset_name: str):
    """Return a PIL image for ``asset_name``, preferring the Texture2D variant.

    Every bundle of the asset's dependency set is loaded into one environment so
    that a Sprite can resolve a texture stored in a sibling bundle.
    """
    environment = _import_unitypy().load(*[str(path) for path in bundles])
    fallback = None
    for obj in environment.objects:
        kind = obj.type.name
        if kind not in (TEXTURE_TYPE.rsplit(".", 1)[1], SPRITE_TYPE.rsplit(".", 1)[1]):
            continue
        data = obj.read()
        if getattr(data, "m_Name", "") != asset_name:
            continue
        if kind == "Texture2D":
            return data.image
        fallback = fallback or data.image
    return fallback


class Family:
    """One output directory plus the addresses that feed it."""

    def __init__(self, category: str, addresses: dict[str, str], note: str = "") -> None:
        self.category = category
        self.addresses = addresses
        self.note = note


def _avatar_family(catalog: ContentCatalog, wanted: set[str] | None) -> Family:
    """Map every avatar address onto the file name the app looks up.

    `AVATAR_ID_REMAP` translates an *asset* id into a *role* id.  The live
    catalog carries both ends of the pair under their own paths -- H801 holds
    蜜娜's art (the role table has no H801 at all) while H804 holds art that
    matches no role.  The hand-verified table therefore outranks a raw entry
    that happens to share the target name, or the remap would be silently
    undone by whichever id sorted last.
    """
    remap = _avatar_id_remap()
    discovered = catalog.match(AVATAR_PATTERN)

    addresses: dict[str, str] = {}
    applied: list[str] = []
    skipped: list[str] = []
    # Remapped ids first; `not in remap` is False for them, so they sort ahead.
    for hero_id, address in sorted(discovered.items(), key=lambda kv: (kv[0] not in remap, kv[0])):
        output_id = remap.get(hero_id, hero_id)
        if wanted is not None and not (hero_id in wanted or output_id in wanted):
            continue
        output_name = f"{output_id}.png"
        if output_name in addresses:
            skipped.append(f"{hero_id} -> {output_name}")
            continue
        addresses[output_name] = address
        if hero_id != output_id:
            applied.append(f"{hero_id} -> {output_name}")

    note = ""
    if applied:
        note = "remapped: " + ", ".join(applied)
    if skipped:
        note += ("" if not note else "; ") + "skipped, output already taken: " + ", ".join(skipped)
    return Family("avatars", addresses, note=note)


def _hero_icon_family(
    catalog: ContentCatalog,
    wanted: set[str] | None,
    category: str,
    pattern: re.Pattern[str],
) -> Family:
    """按白名单正则收集 ``Hero/*/Img`` 下的图标类资产（heads / skills）。

    结构与 :func:`_avatar_family` 同构：重映射条目优先（资产 ``H801`` 的头图其实
    是 ``H804`` 的，重映射改写输出文件名后，原始同名条目让位）；``--ids`` 过滤
    按**资产 ID** 判（该 ID 目录下的所有图标，含皮肤 SH*，一并命中）。
    """
    remap = _avatar_id_remap()
    # 重映射**目标**目录（如 H804）的内容与角色表对不上——那正是错位的另一半：
    # H804 目录里的文件名都带着 H801（Icon_Head_B_H801.png 之类），是另一个
    # 未知角色的图。整目录不收，只留重映射改写后的产物。
    untrusted = set(remap.values())
    matches: list[tuple[str, str, str]] = []
    for address in catalog._ids:  # noqa: SLF001 — match() 只吐一个捕获组，这里要两个
        found = pattern.fullmatch(address)
        if found:
            matches.append((found.group(1), found.group(2), address))

    addresses: dict[str, str] = {}
    applied: list[str] = []
    skipped: list[str] = []
    excluded = 0
    # 重映射优先：`not in remap` 为 False 的排前面（与 _avatar_family 同一招）。
    for asset_id, name, address in sorted(
        matches, key=lambda item: (item[0] not in remap, item[0], item[1])
    ):
        if asset_id in untrusted:
            excluded += 1
            continue
        if wanted is not None and not (
            asset_id in wanted or (asset_id in remap and remap[asset_id] in wanted)
        ):
            continue
        output_name = name
        if asset_id in remap:
            output_name = name.replace(asset_id, remap[asset_id], 1)
            if output_name != name:
                applied.append(f"{asset_id} -> {output_name}")
        if output_name in addresses:
            skipped.append(output_name)
            continue
        addresses[output_name] = address

    note = ""
    if excluded:
        note = f"excluded {excluded} files in untrusted dirs {sorted(untrusted)}"
    if applied:
        note += ("" if not note else "; ") + "remapped: " + ", ".join(applied)
    if skipped:
        note += ("" if not note else "; ") + "skipped, output already taken: " + ", ".join(skipped)
    return Family(category, addresses, note=note)


def _game_icon_family(catalog: ContentCatalog) -> Family:
    """收集 ``Assets/Game/Icon/{Function,Constellation}`` 的功能/星座图标。"""
    addresses = {}
    for address in catalog._ids:  # noqa: SLF001
        found = GAME_ICON_PATTERN.fullmatch(address)
        if found:
            addresses[found.group(1) + ".png"] = address
    return Family(
        "icons",
        addresses,
        note="whitelist: Function + Constellation only",
    )


def _atlas_family(catalog: ContentCatalog) -> Family:
    """道具/装备/套装图标：整包下载后按 Sprite 名逐个导出。"""
    discovered = catalog.match(ATLAS_ADDRESS_PATTERN)
    addresses = {
        f"{name}.spriteatlas": address for name, address in sorted(discovered.items())
    }
    missing = sorted(set(ATLAS_CATEGORIES) - set(discovered))
    note = f"missing: {', '.join(missing)}" if missing else ""
    return Family("atlas", addresses, note=note)


def extract_atlas_sprites(bundles: list[Path]) -> dict[str, bytes]:
    """枚举图集里的每个 Sprite，导出成 ``<Sprite名>.png``。"""
    environment = _import_unitypy().load(*[str(path) for path in bundles])
    exported: dict[str, bytes] = {}
    sprite_kind = SPRITE_TYPE.rsplit(".", 1)[1]
    for obj in environment.objects:
        if obj.type.name != sprite_kind:
            continue
        sprite = obj.read()
        name = getattr(sprite, "m_Name", "")
        if not name:
            continue
        stream = BytesIO()
        sprite.image.save(stream, format="PNG")
        exported[f"{name}.png"] = stream.getvalue()
    return exported


def _ui_family(catalog: ContentCatalog) -> Family:
    addresses = {}
    for output_name, asset_name in UI_ICONS.items():
        address = f"Assets/Game/Icon/Function/{asset_name}.png"
        if catalog.entries_for(address):
            addresses[output_name] = address
    return Family("ui", addresses, note="partial: only the icons the WebGL build ships")


def _read_target_id_list(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    return {item.strip() for item in raw.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="runtime asset output directory")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="catalog and bundle cache directory")
    parser.add_argument("--only", default="", help="comma-separated families (default: all)")
    parser.add_argument("--ids", default="", help="restrict avatars to these hero IDs, e.g. H193,H194")
    parser.add_argument("--path-domain", default="", help="skip the bootstrap call and use this patch base")
    parser.add_argument("--catalog-name", default="", help="override the catalog file name")
    parser.add_argument("--jobs", type=int, default=4, help="parallel bundle downloads (the game itself uses 5)")
    parser.add_argument("--proxy", default="", help="proxy URL; default follows HTTP(S)_PROXY")
    parser.add_argument("--refresh", action="store_true", help="ignore cached catalog and bundles")
    parser.add_argument("--dry-run", action="store_true", help="report what would change, download nothing")
    args = parser.parse_args()

    try:
        constants = _config_constants()
        transport = {"proxy": args.proxy} if args.proxy else {}
        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": str(constants["GAME_ORIGIN"]),
            "Referer": str(constants["GAME_REFERER"]),
            "Accept": "*/*",
            "Accept-Encoding": "gzip",
        }
        with httpx.Client(
            timeout=float(constants["HTTP_TIMEOUT"]), headers=headers, verify=False, **transport
        ) as client:
            server = PatchServer(client, constants, args.cache, args.refresh)
            if args.path_domain:
                base, catalog_name = args.path_domain, args.catalog_name or CATALOG_FALLBACK_NAME
            else:
                base, catalog_name = server.resolve_base()
                if args.catalog_name:
                    catalog_name = args.catalog_name
            print(f"Patch base {base}  catalog {catalog_name}")

            catalog = server.load_catalog(base, catalog_name)
            print(f"Catalog holds {len(catalog._ids)} internal ids")  # noqa: SLF001

            families = {
                "avatars": lambda: _avatar_family(catalog, _read_target_id_list(args.ids)),
                "heads": lambda: _hero_icon_family(
                    catalog, _read_target_id_list(args.ids), "heads", HERO_ICON_PATTERNS[0][1]
                ),
                "skills": lambda: _hero_icon_family(
                    catalog, _read_target_id_list(args.ids), "skills", HERO_ICON_PATTERNS[1][1]
                ),
                "icons": lambda: _game_icon_family(catalog),
                "atlas": lambda: _atlas_family(catalog),
                "ui": lambda: _ui_family(catalog),
            }
            selected = [name for name in args.only.split(",") if name] or list(families)
            unknown = [name for name in selected if name not in families]
            if unknown:
                raise RuntimeError("unknown family: " + ", ".join(unknown))

            jobs = []
            for name in selected:
                family = families[name]()
                if family.note:
                    print(f"  {family.category}: {family.note}")
                for output_name, address in sorted(family.addresses.items()):
                    jobs.append((family.category, output_name, address))
            print(f"Selected {len(jobs)} assets from {', '.join(selected)}")

            if args.dry_run:
                for category, output_name, address in jobs:
                    print(f"  would fetch {category}/{output_name} <- {address}")
                return 0

            # atlas 家族走独立的"整包枚举 Sprite"管线（_download_atlas），
            # 不能进常规管线——那里按地址名找资产，图集里没有叫这个名字的。
            regular_jobs = [job for job in jobs if job[0] != "atlas"]
            atlas_jobs = [job for job in jobs if job[0] == "atlas"]
            results = _download(catalog, server, base, regular_jobs, args.jobs)
            if atlas_jobs:
                results.update(
                    _download_atlas(catalog, server, base, atlas_jobs, args.jobs)
                )
        return _write(args.target, results)
    except (RuntimeError, httpx.HTTPError, OSError) as exc:
        parser.error(str(exc))
    return 1


def _download(
    catalog: ContentCatalog,
    server: PatchServer,
    base: str,
    jobs: list[tuple[str, str, str]],
    workers: int,
) -> dict[tuple[str, str], bytes | None]:
    """Fetch and decode every job.

    Jobs are grouped by their full bundle set, so each distinct set is
    downloaded once and decoded once even when it carries many assets.
    """
    grouped: dict[tuple[str, ...], list[tuple[str, str, str]]] = {}
    orphans: list[tuple[str, str, str]] = []
    for job in jobs:
        bundles = catalog.bundles_for(job[2])
        if bundles:
            grouped.setdefault(bundles, []).append(job)
        else:
            orphans.append(job)

    for _, output_name, address in orphans:
        print(f"  no bundle for {output_name} ({address})", file=sys.stderr)

    results: dict[tuple[str, str], bytes | None] = {}
    errors: list[str] = []

    def run(item: tuple[tuple[str, ...], list[tuple[str, str, str]]]):
        internal_ids, batch = item
        try:
            paths = [server.fetch_bundle(base, internal_id) for internal_id in internal_ids]
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return batch, None, str(exc)
        payload: dict[str, bytes | None] = {}
        for category, output_name, address in batch:
            asset_name = address.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            try:
                image = extract_texture(paths, asset_name)
            except Exception as exc:  # UnityPy raises a wide range of errors
                errors.append(f"{category}/{output_name}: {exc}")
                payload[output_name] = None
                continue
            if image is None:
                errors.append(f"{category}/{output_name}: {asset_name} missing from bundle")
                payload[output_name] = None
                continue
            stream = BytesIO()
            image.save(stream, format="PNG")
            payload[output_name] = stream.getvalue()
        return batch, payload, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for batch, payload, error in pool.map(run, grouped.items()):
            if error:
                for category, output_name, _ in batch:
                    results[(category, output_name)] = None
                errors.append(error)
                continue
            for category, output_name, _ in batch:
                results[(category, output_name)] = payload.get(output_name)

    for message in errors[:20]:
        print(f"  {message}", file=sys.stderr)
    if len(errors) > 20:
        print(f"  ... and {len(errors) - 20} more", file=sys.stderr)
    return results


def _download_atlas(
    catalog: ContentCatalog,
    server: PatchServer,
    base: str,
    jobs: list[tuple[str, str, str]],
    workers: int,
) -> dict[tuple[str, str], bytes | None]:
    """下载图集 bundle 并枚举其中的 Sprite（atlas 家族专用）。

    结果键是 ``(图集对应目录, "<Sprite名>.png")``，图集抓取失败时记
    ``(目录, "<Atlas名>.spriteatlas")`` = None，让 ``_write`` 计一次失败。
    """
    results: dict[tuple[str, str], bytes | None] = {}

    def run(item: tuple[str, str, str]):
        _category, output_name, address = item
        bundles = catalog.bundles_for(address)
        if not bundles:
            return output_name, None, "no bundle"
        try:
            paths = [server.fetch_bundle(base, internal) for internal in bundles]
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return output_name, None, str(exc)
        try:
            return output_name, extract_atlas_sprites(paths), None
        except Exception as exc:  # UnityPy raises a wide range of errors
            return output_name, None, str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for output_name, sprites, error in pool.map(run, jobs):
            category = ATLAS_CATEGORIES[output_name.rsplit(".", 1)[0]]
            if error:
                print(f"  atlas {output_name}: {error}", file=sys.stderr)
                results[(category, output_name)] = None
                continue
            if not sprites:
                print(f"  atlas {output_name}: no sprites", file=sys.stderr)
                results[(category, output_name)] = None
                continue
            for sprite_name, payload in sprites.items():
                if category == "equip":
                    # 应用侧装备图标约定不带下划线（E001_1 -> E0011.png）。
                    sprite_name = sprite_name.replace("_", "")
                results[(category, sprite_name)] = payload
    return results


def _write(target: Path, results: dict[tuple[str, str], bytes | None]) -> int:
    """Write changed files only.  Never removes anything."""
    changed = 0
    unchanged = 0
    failed = 0
    per_category: dict[str, list[int]] = {}
    for (category, output_name), payload in sorted(results.items()):
        stats = per_category.setdefault(category, [0, 0, 0])
        if payload is None:
            failed += 1
            stats[2] += 1
            continue
        destination = target / category / output_name
        if destination.is_file() and destination.read_bytes() == payload:
            unchanged += 1
            stats[1] += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        changed += 1
        stats[0] += 1

    summary = ", ".join(
        f"{category}={sum(counts)} (changed {counts[0]}, unchanged {counts[1]}, failed {counts[2]})"
        for category, counts in sorted(per_category.items())
    )
    print("Fetched " + (summary or "nothing"))
    if failed:
        print(f"{failed} assets could not be fetched", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
