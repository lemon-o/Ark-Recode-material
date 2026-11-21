"""从官方 StaticData 与多语言文本表构建全量角色档案 (data/heroes.json)。

区分官方「已正式上线角色」与「拆包未上线卫星角色」：
1. 核心判定：正式服实装角色必须在好感度签名表 (RoleSignature.txt) 中登记台词并开放主页互动；
   未上线的拆包预埋角色（如未来的节日限定、拆包卫星）在签名表中为空。
2. 聚合元数据：
   - 角色 ID、星级、属性、职业
   - 多语言名字（简中、繁中、英文、日文）
   - 是否已上线 (is_released)
   - 资产收录标志：has_spine, has_avatar, has_gacha, has_collection
3. 产物输出至 data/heroes.json，供下游应用一键过滤已上线/未上线角色。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import UnityPy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from fetch_assets import ContentCatalog, PatchServer

CONFIG_PATH = ROOT / "backend" / "config.py"
DEFAULT_CACHE = ROOT / "Asset Cache"
BOOTSTRAP_ROUTE = "GameServerDBSettingHandler.QueryBulletinInfoResult"
OUTPUT_FILE = ROOT / "data" / "heroes.json"

_TZ8 = timezone(timedelta(hours=8))

DISCORD_NOTICE_PATH = ROOT / "data" / "discord-notice.json"
CATALOG_INFO_PATH = ROOT / "data" / "catalog-info.json"

# 手工校准的未上线/拆包预埋角色 ID 集合：
# 官方数据表中可能已提前写入好感度签名、活动配置或 Spine 资源，但正式服尚未实装上线。
UNRELEASED_HERO_OVERRIDES: set[str] = {
    "H154",  # 迦布莉
    "H155",  # 蕾蜜莉
    "H157",  # 曼德菈
    "H158",  # 露菲娜
    "H159",  # 约兰塔
    "H161",  # 新生伊娥丝
    "H164",  # 塔丝梅图姆
    "H198",  # 执法者维尔莉特
    "H201",  # 藿普
    "H607",  # 玖可
}


def _get_announced_hero_ids() -> set[str]:
    """从 Discord 官方公告或版本基线中提取已确认为正式上线的角色 ID 集合（优先放行白名单）。"""
    announced: set[str] = set()

    # 1. 优先读取 Discord 专用公告快照
    if DISCORD_NOTICE_PATH.is_file():
        try:
            d = json.loads(DISCORD_NOTICE_PATH.read_text(encoding="utf-8"))
            for h in (d.get("pickup_heroes") or []) + (d.get("mystic_heroes") or []):
                if isinstance(h, dict) and h.get("id"):
                    announced.add(str(h["id"]).upper())
        except (OSError, ValueError):
            pass

    # 2. 读取 catalog-info.json 中的公告解析记录
    if CATALOG_INFO_PATH.is_file():
        try:
            d = json.loads(CATALOG_INFO_PATH.read_text(encoding="utf-8"))
            for h in (d.get("pickup_heroes") or []) + (d.get("mystic_heroes") or []):
                if isinstance(h, dict) and h.get("id"):
                    announced.add(str(h["id"]).upper())
        except (OSError, ValueError):
            pass

    return announced


def _read_config() -> dict[str, str]:
    wanted = {"GAME_ROUTER", "GAME_ORIGIN", "GAME_REFERER", "HTTP_TIMEOUT"}
    try:
        tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        sys.exit(f"读取 backend/config.py 失败: {exc}")
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in wanted:
                    if isinstance(node.value, ast.Constant):
                        found[target.id] = str(node.value.value)
    return found


def _resolve_patch_info(client: httpx.Client, router: str) -> tuple[str, str]:
    body = json.dumps({"data": {}, "route": BOOTSTRAP_ROUTE}).encode("utf-8")
    resp = client.request("PUT", router, content=body)
    resp.raise_for_status()
    info = (resp.json() or {}).get("Info", {})
    domain = str(info.get("PathDomain") or info.get("PathDomains") or "").rstrip("/")
    base = domain + str(info.get("PatchPosFix") or "")
    catalog_name = str(info.get("NewCatalogName") or "catalog")
    return base, catalog_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从官方 StaticData 与多语言文本表构建全量角色档案 (data/heroes.json)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="缓存目录")
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE, help="输出文件路径")
    args = parser.parse_args(argv)

    cfg = _read_config()
    client = httpx.Client(timeout=float(cfg.get("HTTP_TIMEOUT", 30.0)))
    base, cat_name = _resolve_patch_info(client, cfg["GAME_ROUTER"])
    server = PatchServer(client, cfg, args.cache, False)
    catalog = server.load_catalog(base, cat_name)

    # 1. 定位 staticdata 与 text 两个 bundle
    staticdata_bundles = list(catalog.bundles_for("Assets/Game/StaticData/Role.txt"))
    text_bundles = list(catalog.bundles_for("Assets/Game/StaticData/Text/CHS.txt"))

    if not staticdata_bundles or not text_bundles:
        print("未找到 StaticData 或 Text bundle 地址", file=sys.stderr)
        return 1

    p_static = [server.fetch_bundle(base, b) for b in staticdata_bundles]
    p_text = [server.fetch_bundle(base, b) for b in text_bundles]

    # 2. 读取多语言文本表 (CHS, CHT, ENG, JPN)
    env_text = UnityPy.load(*[str(p) for p in p_text])
    loc_maps: dict[str, dict[str, str]] = {
        "CHS": {},
        "CHT": {},
        "ENG": {},
        "JPN": {},
    }
    for obj in env_text.objects:
        if obj.type.name == "TextAsset":
            d = obj.read()
            lang = getattr(d, "m_Name", "")
            if lang in loc_maps:
                raw = getattr(d, "m_Script", b"")
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                for line in text.strip().splitlines():
                    if "@" in line:
                        k, v = line.split("@", 1)
                        loc_maps[lang][k] = v.strip()

    # 3. 读取静态数据表
    env_static = UnityPy.load(*[str(p) for p in p_static])
    role_rows = []
    role_signatures = set()
    role_collections = set()
    gacha_roles = set()
    hero_limits = {}

    for obj in env_static.objects:
        if obj.type.name == "TextAsset":
            d = obj.read()
            name = getattr(d, "m_Name", "")
            raw = getattr(d, "m_Script", b"")
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
            lines = text.strip().splitlines()

            if name == "Role":
                for l in lines[1:]:
                    parts = l.split("@")
                    # IsHero (col 12) == '1'
                    if len(parts) > 12 and parts[12] == "1":
                        role_rows.append(parts)
            elif name == "Item":
                item_headers = lines[0].split("@")
                tag_col = item_headers.index("Tags") if "Tags" in item_headers else 19
                for l in lines[1:]:
                    parts = l.split("@")
                    if parts and parts[0].startswith("H") and len(parts) > tag_col:
                        tags = parts[tag_col]
                        is_limit = ("LimitStar" in tags) and ("Normal" not in tags)
                        hero_limits[parts[0]] = is_limit
            elif name == "RoleSignature":
                for l in lines[1:]:
                    parts = l.split("@")
                    # 必须登记有实际签名台词（排除仅有角色 ID 但签名内容为空的占位行）
                    if parts and parts[0] and len(parts) > 2 and parts[2].strip():
                        role_signatures.add(parts[0])
            elif name == "RoleCollection":
                for l in lines[1:]:
                    parts = l.split("@")
                    if len(parts) > 1 and parts[1]:
                        role_collections.add(parts[1])
            elif name == "Commodity":
                for l in lines[1:]:
                    parts = l.split("@")
                    # 检查 GuaranteedRoleIDs (49), Banner (25), Drops (45)
                    for idx in [25, 45, 46, 49]:
                        if len(parts) > idx and parts[idx]:
                            for m in re.findall(r"H\d+", parts[idx]):
                                gacha_roles.add(m)

    # 4. 本地资产检查（Spine / 头像）
    spine_dir = ROOT / "spine"
    avatars_dir = ROOT / "avatars"

    existing_spines = {
        p.name for p in spine_dir.iterdir() if p.is_dir()
    } if spine_dir.is_dir() else set()

    existing_avatars = {
        p.stem for p in avatars_dir.glob("*.png")
    } if avatars_dir.is_dir() else set()

    # 5. 组合角色档案字典
    heroes_dict = {}
    released_ids = []
    unreleased_ids = []

    # 优先读取 Discord 官方公告/版本基线确认上线的白名单角色
    announced_ids = _get_announced_hero_ids()
    if announced_ids:
        print(f"公告确认上线放行名单 (Discord/Official): {sorted(list(announced_ids))}")

    for parts in role_rows:
        hid = parts[0]
        title_key = parts[1] if len(parts) > 1 else ""
        name_key = parts[2] if len(parts) > 2 else ""
        star = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
        element = parts[4] if len(parts) > 4 else ""
        job = parts[5] if len(parts) > 5 else ""

        # 多语言名称
        name_chs = loc_maps["CHS"].get(name_key) or name_key
        name_cht = loc_maps["CHT"].get(name_key) or name_chs
        name_eng = loc_maps["ENG"].get(name_key) or name_chs
        name_jpn = loc_maps["JPN"].get(name_key) or name_chs

        # 核心判定：是否正式上线
        # 满足以下两条之一即可判定为正式上线：
        # 1. 【Discord 官方频道/公告优先】：被官方公告明确确认为本期限定招募或神秘招募（自动解除黑名单限制放行）
        # 2. 或满足常规实装条件：
        #    a. 不在手工校准的未上线/拆包预埋黑名单中（hid not in UNRELEASED_HERO_OVERRIDES）
        #    b. 在好感度签名表中登记有非空内容（hid in role_signatures）
        #    c. 官方 CDN 中已实装 Spine 骨骼立绘（hid in existing_spines）
        #    d. 具有正式官方角色名称（排除 H169 这种纯 ID 占位测试空壳）
        is_officially_announced = hid in announced_ids
        is_released = bool(
            is_officially_announced
            or (
                hid not in UNRELEASED_HERO_OVERRIDES
                and hid in role_signatures
                and hid in existing_spines
                and name_chs != hid
            )
        )

        if is_released:
            released_ids.append(hid)
        else:
            unreleased_ids.append(hid)

        heroes_dict[hid] = {
            "id": hid,
            "name": name_chs,
            "name_chs": name_chs,
            "name_cht": name_cht,
            "name_eng": name_eng,
            "name_jpn": name_jpn,
            "star": star,
            "element": element,
            "job": job,
            "is_released": is_released,
            "is_limited": hero_limits.get(hid, False),
            "has_spine": hid in existing_spines,
            "has_avatar": hid in existing_avatars,
            "has_gacha": hid in gacha_roles,
            "has_collection": hid in role_collections,
        }

    released_ids.sort()
    unreleased_ids.sort()
    heroes_dict = dict(sorted(heroes_dict.items()))

    payload = {
        "updated": datetime.now(_TZ8).replace(microsecond=0).isoformat(),
        "total": len(heroes_dict),
        "released_count": len(released_ids),
        "unreleased_count": len(unreleased_ids),
        "released_ids": released_ids,
        "unreleased_ids": unreleased_ids,
        "heroes": heroes_dict,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    rel_path = (args.output.relative_to(ROOT) if args.output.is_relative_to(ROOT) else args.output).as_posix()
    print(
        f"{rel_path} 已更新: 共收录 {len(heroes_dict)} 个角色 "
        f"(已上线: {len(released_ids)}, 未上线/拆包: {len(unreleased_ids)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
