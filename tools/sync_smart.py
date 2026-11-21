"""Ark-Recode 素材抓取智能调度与定向精准提取工具。

设计理念与业务分工：
1. 维护结束准时触发 (slot == "end_time")：
   - 极速响应：直接从官方维护公告 (data/announcement.json) 提取 UP 角色名单；
   - 复刻跳过：检查角色头像、头像框与 Spine 骨骼立绘三件套是否已存在；
     若已全部存在，判定为「复刻团员」，直接跳过全量下载，仅刷新卡池动态看板与公告文件，5 秒内极速完工；
   - 定向精准抓取：若发现全新实装团员，仅针对新角色 ID 执行定向素材拉取 (--ids <HID>)，20-30 秒内迅速落库。
2. 夜间 12 点兜底扫描 (slot == "night_fallback")：
   - 全面兜底：对全量素材分类（头像、技能、道具、装备、图集、数据表、Spine）执行增量扫描，
     捕获可能在日间通过热更新实装的散落素材、活动剧情 CG 与未公告预埋资源。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

ANNOUNCEMENT_PATH = ROOT / "data" / "announcement.json"
HEROES_PATH = ROOT / "data" / "heroes.json"

_TZ8 = timezone(timedelta(hours=8))


def _run_cmd(cmd: list[str]) -> int:
    print(f"\n[+] 执行指令: {' '.join(cmd)}", flush=True)
    res = subprocess.run(cmd, cwd=str(ROOT))
    if res.returncode != 0:
        print(f"[-] 指令执行失败 (返回码: {res.returncode}): {' '.join(cmd)}", file=sys.stderr, flush=True)
    return res.returncode


def _load_announcement_heroes() -> list[str]:
    """从 announcement.json 汇总提取本期所有公告涉及的团员 ID。"""
    if not ANNOUNCEMENT_PATH.is_file():
        return []
    try:
        data = json.loads(ANNOUNCEMENT_PATH.read_text(encoding="utf-8"))
        heroes_sec = data.get("heroes", {})
        all_announced: list[str] = []
        for cat in ("pickup", "mystic", "rerun"):
            for h in heroes_sec.get(cat, []):
                hid = str(h.get("id") or "").strip().upper()
                if hid and hid.startswith("H") and hid not in all_announced:
                    all_announced.append(hid)
        return all_announced
    except Exception as exc:
        print(f"[-] 读取公告团员列表失败: {exc}", file=sys.stderr)
        return []


def is_hero_already_in_repo(hid: str) -> bool:
    """检查角色基础素材三件套是否已在仓库中完整收录。"""
    avatar_file = ROOT / "avatars" / f"{hid}.png"
    spine_skel = ROOT / "spine" / hid / f"{hid}.skel"
    spine_atlas = ROOT / "spine" / hid / f"{hid}.atlas"
    spine_png = ROOT / "spine" / hid / f"{hid}.png"

    has_avatar = avatar_file.is_file() and avatar_file.stat().st_size > 0
    has_spine = (
        spine_skel.is_file()
        and spine_skel.stat().st_size > 0
        and spine_atlas.is_file()
        and spine_atlas.stat().st_size > 0
        and spine_png.is_file()
        and spine_png.stat().st_size > 0
    )
    return has_avatar and has_spine


def run_targeted_sync(hero_ids: list[str], dry_run: bool = False) -> int:
    """针对公告内团员执行精准定向抓取与跳过判定。"""
    print(f"\n[*] 启动【维护结束准时抓取 (定向模式)】...")
    print(f"[*] 本期公告涉及团员: {hero_ids or '无'}")

    new_heroes: list[str] = []
    rerun_heroes: list[str] = []

    for hid in hero_ids:
        if is_hero_already_in_repo(hid):
            rerun_heroes.append(hid)
        else:
            new_heroes.append(hid)

    if rerun_heroes:
        print(f"[OK] 检测到已存在复刻团员: {rerun_heroes}（素材已完整收录，跳过下载）")

    if not new_heroes:
        print("[OK] 本期所有公告团员素材均已就绪（均为复刻团员或无新角色），无需下载游戏资产包！")
        print("[*] 正在刷新当前招募动态看板与角色档案...")
        _run_cmd([sys.executable, "tools/build_current_pools.py", "--update-readme"])
        _run_cmd([sys.executable, "tools/update_readme.py"])
        print("[OK] 定向同步完成，耗时极短，无大文件变动。")
        return 0

    print(f"[+] 发现全新实装团员: {new_heroes}，启动定向极速拉取...")
    if dry_run:
        print(f"[*] [Dry Run] 计划提取角色 ID: {new_heroes}")
        return 0

    ids_str = ",".join(new_heroes)

    # 1. 定向抓取新角色的头像、头像框、技能图标、卡池横幅
    code = _run_cmd([
        sys.executable,
        "tools/fetch_assets.py",
        "--target", ".",
        "--ids", ids_str,
        "--only", "avatars,heads,skills,bonds,banner",
    ])
    if code != 0:
        return code

    # 2. 定向抓取新角色的 Spine 骨骼立绘
    code = _run_cmd([
        sys.executable,
        "tools/fetch_spine.py",
        "--ids", ids_str,
        "--target", "spine",
        "--jobs", "4",
    ])
    if code != 0:
        return code

    # 3. 刷新全量元数据与看板
    _run_cmd([sys.executable, "tools/build_heroes.py"])
    _run_cmd([sys.executable, "tools/build_current_pools.py", "--update-readme"])
    _run_cmd([sys.executable, "tools/build_index.py"])
    _run_cmd([sys.executable, "tools/update_readme.py"])

    print(f"[OK] 新团员 {new_heroes} 定向抓取全部完成！")
    return 0


def run_night_fallback_sync(dry_run: bool = False) -> int:
    """夜间 12 点兜底增量扫描，覆盖全量资产。"""
    print(f"\n[*] 启动【夜间 12 点兜底全量增量扫描】...")
    if dry_run:
        print("[*] [Dry Run] 模拟执行全量增量扫描...")
        return 0

    # 1. 全量增量小图标与元数据抓取
    code = _run_cmd([
        sys.executable,
        "tools/fetch_assets.py",
        "--target", ".",
        "--only", "avatars,heads,skills,icons,atlas,banner,data,bonds",
    ])
    if code != 0:
        return code

    # 2. 全量 Spine 增量补齐
    code = _run_cmd([
        sys.executable,
        "tools/fetch_spine.py",
        "--all",
        "--target", "spine",
        "--jobs", "4",
    ])
    if code != 0:
        return code

    # 3. 刷新元数据、看板与清单
    _run_cmd([sys.executable, "tools/build_heroes.py"])
    _run_cmd([sys.executable, "tools/build_current_pools.py", "--update-readme"])
    _run_cmd([sys.executable, "tools/build_index.py"])
    _run_cmd([sys.executable, "tools/update_readme.py"])

    print("[OK] 夜间兜底增量扫描完成！")
    return 0


def resolve_slot(slot_arg: str) -> str:
    if slot_arg and slot_arg != "auto":
        return slot_arg

    # 从环境变量读取
    env_slot = os.environ.get("DISPATCH_SLOT", "").strip().lower()
    if env_slot in ("end_time", "night_fallback"):
        return env_slot

    # 自动根据当前时刻推断：23:00 - 01:00 判定为夜间兜底，其余为维护结束定向
    now_hour = datetime.now(_TZ8).hour
    if now_hour in (23, 0):
        return "night_fallback"
    return "end_time"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ark-Recode 素材抓取智能调度与定向精准提取")
    parser.add_argument(
        "--slot",
        choices=["auto", "end_time", "night_fallback"],
        default="auto",
        help="触发时段 (end_time: 维护结束定向抓取; night_fallback: 夜间12点增量兜底; auto: 自动识别)",
    )
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写盘")
    args = parser.parse_args(argv)

    slot = resolve_slot(args.slot)
    print(f"[*] 智能调度生效时段: [{slot}] (请求槽位: {args.slot})")

    if slot == "end_time":
        announced_heroes = _load_announcement_heroes()
        return run_targeted_sync(announced_heroes, dry_run=args.dry_run)
    else:
        return run_night_fallback_sync(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
