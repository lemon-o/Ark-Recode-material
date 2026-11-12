"""从官方 CDN 提取全角色 Spine 骨骼立绘资产（.skel + .atlas + .png）。

机制：
1. 解析 Addressables Catalog，定位角色的主立绘目录：
   Assets/Game/Hero/{HeroID}/{HeroID}/{BaseName}.skel.bytes
   Assets/Game/Hero/{HeroID}/{HeroID}/{BaseName}.atlas.txt
   Assets/Game/Hero/{HeroID}/{HeroID}/{BaseName}.png
   （早期角色 BaseName 为 CG_H001_a，后期角色为 H195）
2. 下载角色对应的全部必要 bundle，精准导出完整的 Spine 三件套：
   - {BaseName}.skel
   - {BaseName}.atlas
   - {BaseName}.png
3. 保存至 spine/{HeroID}/ 目录下。保持原名可保证 atlas 内部纹理引用无需修改即可被 spine-player 直接加载。

用法：
    python tools/fetch_spine.py --ids H195,H001        # 指定角色提取
    python tools/fetch_spine.py --limit 5             # 抽样提取 5 个
    python tools/fetch_spine.py --all                 # 全量提取所有角色 (自动增量跳过)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
import UnityPy

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from fetch_assets import ContentCatalog, PatchServer

CONFIG_PATH = ROOT / "backend" / "config.py"
DEFAULT_TARGET = ROOT / "spine"
DEFAULT_CACHE = ROOT / "Asset Cache"
BOOTSTRAP_ROUTE = "GameServerDBSettingHandler.QueryBulletinInfoResult"

PORTRAIT_SKEL_PATTERN = re.compile(r"^Assets/Game/Hero/(H\d+)/\1/([^/]+)\.skel\.bytes$")


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


def find_all_spine_portraits(catalog: ContentCatalog) -> dict[str, tuple[str, list[str]]]:
    """返回 {HeroID: (BaseName, [所有涉及的 bundle 内部地址])}"""
    heroes: dict[str, tuple[str, list[str]]] = {}
    for addr in catalog._ids:
        m = PORTRAIT_SKEL_PATTERN.match(addr)
        if m:
            hid, base_name = m.group(1), m.group(2)
            bundles = list(catalog.bundles_for(addr))
            # 同时也检查 png 地址所在的 bundle（防止部分角色如 H060 的 png 放在 duplicate bundle 里）
            png_addr = f"Assets/Game/Hero/{hid}/{hid}/{base_name}.png"
            png_bundles = catalog.bundles_for(png_addr)
            for pb in png_bundles:
                if pb not in bundles:
                    bundles.append(pb)
            if bundles:
                heroes[hid] = (base_name, bundles)
    return dict(sorted(heroes.items()))


def extract_hero_spine(bundle_paths: list[Path], hero_id: str, base_name: str, out_dir: Path) -> dict[str, int]:
    """从 bundle 列表中提取大立绘三件套，按 hero_id 统一命名并修正 atlas 引用"""
    env = UnityPy.load(*[str(p) for p in bundle_paths])
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = {}

    target_atlas = f"{base_name}.atlas"
    target_skel = f"{base_name}.skel"

    for obj in env.objects:
        if obj.type.name == "TextAsset":
            d = obj.read()
            m_name = getattr(d, "m_Name", "")
            if m_name == target_atlas:
                raw = getattr(d, "m_Script", b"")
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                # 统一为 <hero_id>.atlas，若内部贴图引用包含 base_name.png，则同步替换为 hero_id.png
                if base_name != hero_id:
                    text = text.replace(f"{base_name}.png", f"{hero_id}.png")
                p = out_dir / f"{hero_id}.atlas"
                p.write_text(text, encoding="utf-8")
                saved["atlas"] = len(text)
            elif m_name == target_skel:
                raw = getattr(d, "m_Script", b"")
                b = raw.encode("utf-8", "surrogateescape") if isinstance(raw, str) else bytes(raw)
                p = out_dir / f"{hero_id}.skel"
                p.write_bytes(b)
                saved["skel"] = len(b)
        elif obj.type.name == "Texture2D":
            d = obj.read()
            m_name = getattr(d, "m_Name", "")
            if m_name == base_name:
                img = d.image
                p = out_dir / f"{hero_id}.png"
                img.save(p, format="PNG")
                saved["png"] = p.stat().st_size

    # 清理同一目录下可能残留的历史旧命名（如 CG_H001_a.*）
    if base_name != hero_id:
        for ext in [".skel", ".atlas", ".png"]:
            old_p = out_dir / f"{base_name}{ext}"
            if old_p.is_file():
                try:
                    old_p.unlink()
                except OSError:
                    pass

    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提取游戏全角色 Spine 骨骼立绘")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="输出目录 (默认: spine/)")
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="缓存目录")
    parser.add_argument("--ids", default="", help="指定提取的角色 ID (逗号分隔，如 H195,H001)")
    parser.add_argument("--limit", type=int, default=0, help="最多提取角色数量 (测试用)")
    parser.add_argument("--all", action="store_true", help="全量提取所有角色")
    parser.add_argument("--force", action="store_true", help="强制覆盖已存在的文件")
    parser.add_argument("--jobs", type=int, default=4, help="并发下载数 (默认 4)")
    args = parser.parse_args(argv)

    cfg = _read_config()
    client = httpx.Client(timeout=float(cfg.get("HTTP_TIMEOUT", 30.0)))
    base, cat_name = _resolve_patch_info(client, cfg["GAME_ROUTER"])
    print(f"CDN Base: {base} | Catalog: {cat_name}", flush=True)

    server = PatchServer(client, cfg, args.cache, False)
    catalog = server.load_catalog(base, cat_name)
    all_heroes = find_all_spine_portraits(catalog)
    print(f"Catalog 中共发现 {len(all_heroes)} 个拥有大立绘 Spine 的角色", flush=True)

    if args.ids:
        wanted = {x.strip().upper() for x in args.ids.split(",") if x.strip()}
        targets = {hid: info for hid, info in all_heroes.items() if hid in wanted}
    elif args.limit > 0:
        targets = dict(list(all_heroes.items())[:args.limit])
    elif args.all:
        targets = all_heroes
    else:
        print("提示：未指定 --all、--ids 或 --limit，默认以 --limit 3 抽样测试运行。", flush=True)
        print("如需全量提取，请加上 --all 参数。", flush=True)
        targets = dict(list(all_heroes.items())[:3])

    print(f"本次计划处理角色数: {len(targets)}", flush=True)
    args.target.mkdir(parents=True, exist_ok=True)

    success_count = 0
    skip_count = 0
    fail_count = 0

    lock = threading.Lock()

    def process_hero(hid: str, info: tuple[str, list[str]]) -> tuple[str, bool, str]:
        base_name, bundles_internal = info
        hero_dir = args.target / hid
        skel_file = hero_dir / f"{hid}.skel"
        atlas_file = hero_dir / f"{hid}.atlas"
        png_file = hero_dir / f"{hid}.png"

        if not args.force and skel_file.is_file() and atlas_file.is_file() and png_file.is_file():
            if skel_file.stat().st_size > 0 and atlas_file.stat().st_size > 0 and png_file.stat().st_size > 0:
                return hid, True, f"已存在(跳过: {hid})"

        try:
            bundle_paths = [server.fetch_bundle(base, b) for b in bundles_internal]
            stats = extract_hero_spine(bundle_paths, hid, base_name, hero_dir)
            if "skel" in stats and "atlas" in stats and "png" in stats:
                msg = f"提取成功 [{hid}] (PNG: {stats['png']/1024/1024:.1f}MB, SKEL: {stats['skel']/1024:.0f}KB)"
                return hid, True, msg
            else:
                missing = [k for k in ["skel", "atlas", "png"] if k not in stats]
                return hid, False, f"部分缺失 [{hid}]: {missing}"
        except Exception as exc:
            return hid, False, f"错误: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(process_hero, hid, info): hid for hid, info in targets.items()}
        for fut in as_completed(futures):
            hid, ok, msg = fut.result()
            with lock:
                if ok:
                    if "跳过" in msg:
                        skip_count += 1
                        print(f"[-] {hid:6}: {msg}", flush=True)
                    else:
                        success_count += 1
                        print(f"[+] {hid:6}: {msg}", flush=True)
                else:
                    fail_count += 1
                    print(f"[x] {hid:6}: {msg}", flush=True)

    print("\n" + "=" * 50, flush=True)
    print(f"处理完成！成功提取: {success_count} 个, 跳过已有: {skip_count} 个, 失败: {fail_count} 个", flush=True)
    print(f"Spine 输出目录: {args.target}", flush=True)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
