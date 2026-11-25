"""Ark-Recode 素材抓取智能调度与定向精准提取工具。

设计理念与业务分工：
1. 维护结束准时触发 (slot == "end_time")：
   - 极速响应：直接从官方维护公告 (data/announcement.json) 提取 UP 角色名单与限时活动；
   - 复刻跳过：检查角色头像、头像框与 Spine 骨骼立绘三件套是否已存在；
     若已全部存在且无新活动，判定为「纯复刻维护」，直接跳过全量下载，仅刷新卡池动态看板与公告文件，极速完工；
   - 活动商店联动：若公告包含新限时活动/活动商店（即使团员为复刻），定向提取活动图集 (atlas)、数据表 (data) 与活动横幅；
   - 定向精准抓取：若发现全新实装团员，仅针对新角色 ID 执行定向素材拉取 (--ids <HID>)，附带活动商店素材，20-30 秒内迅速落库。
2. 夜间 12 点兜底扫描 (slot == "night_fallback")：
   - 全面兜底：对全量素材分类（头像、技能、道具、装备、图集、数据表、Spine）执行增量扫描，
     捕获可能在日间通过热更新实装的散落素材、活动剧情 CG 与未公告预埋资源。
"""

from __future__ import annotations

import argparse
import json
import os
import re
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


def _detect_announcement_activity() -> dict[str, Any] | None:
    """检查维护公告中是否有新限时活动与活动商店。"""
    if not ANNOUNCEMENT_PATH.is_file():
        return None
    try:
        data = json.loads(ANNOUNCEMENT_PATH.read_text(encoding="utf-8"))
        act_name = str(data.get("activity_name") or "").strip()
        content = str(data.get("content") or "")

        if not act_name:
            m = re.search(r"(?:限定活動|主題活動|活动|活動)[】\s]*[「『]([^」』]+)[」』]", content)
            if m:
                act_name = m.group(1).strip()

        has_shop = bool(re.search(r"活動商店|活动商店|活動代幣|活动代币|商店兌換", content))
        has_theme_act = bool(act_name or re.search(r"【(?:主題活動|限定活動|活動|活动)】", content))

        if has_theme_act or has_shop:
            return {
                "name": act_name or "限时主题活动",
                "has_shop": has_shop,
            }
        return None
    except Exception:
        return None


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
    """针对公告内团员与活动商店素材执行精准定向抓取与跳过判定。"""
    print(f"\n[*] 启动【维护结束准时抓取 (定向模式)】...")
    print(f"[*] 本期公告涉及团员: {hero_ids or '无'}")

    activity_info = _detect_announcement_activity()
    if activity_info:
        print(f"[+] 检测到限时活动: 「{activity_info['name']}」" + (" (含活动商店/代币兑换)" if activity_info.get("has_shop") else ""))

    new_heroes: list[str] = []
    rerun_heroes: list[str] = []

    for hid in hero_ids:
        if is_hero_already_in_repo(hid):
            rerun_heroes.append(hid)
        else:
            new_heroes.append(hid)

    if rerun_heroes:
        print(f"[OK] 检测到已存在复刻团员: {rerun_heroes}（角色素材已完整收录，跳过立绘下载）")

    # 分支 1：公告内没有全新角色
    if not new_heroes:
        # 分支 1.1：没有新角色，但有新活动/活动商店 -> 定向抓取活动图集与数据表
        if activity_info:
            print("[+] 团员虽均为复刻，但本期存在新限时活动/活动商店，启动活动商店素材定向拉取 (atlas,data,banner)...")
            if dry_run:
                print("[*] [Dry Run] 计划提取活动图集与商店素材: atlas, data, banner")
                return 0
            code = _run_cmd([
                sys.executable,
                "tools/fetch_assets.py",
                "--target", ".",
                "--only", "atlas,data,banner",
            ])
            if code != 0:
                return code
            _run_cmd([sys.executable, "tools/build_heroes.py"])
            _run_cmd([sys.executable, "tools/build_current_pools.py", "--update-readme"])
            _run_cmd([sys.executable, "tools/build_index.py"])
            _run_cmd([sys.executable, "tools/update_readme.py"])
            print("[OK] 活动商店素材与动态看板定向同步完成！")
            return 0

        # 分支 1.2：没有新角色，也没有新活动 -> 完全无需抓取，仅刷新看板与心跳
        print("[OK] 本期所有公告团员素材均已就绪（均为复刻团员），且无新活动商店，无需下载游戏资产包！")
        print("[*] 正在刷新当前招募动态看板与角色档案...")
        _run_cmd([sys.executable, "tools/build_current_pools.py", "--update-readme"])
        _run_cmd([sys.executable, "tools/update_readme.py"])
        print("[OK] 定向同步完成，耗时极短，无大文件变动。")
        return 0

    # 分支 2：存在全新实装团员
    print(f"[+] 发现全新实装团员: {new_heroes}，启动定向极速拉取...")
    if dry_run:
        print(f"[*] [Dry Run] 计划提取角色 ID: {new_heroes}")
        if activity_info:
            print("[*] [Dry Run] 计划附带提取活动商店素材: atlas, data")
        return 0

    ids_str = ",".join(new_heroes)
    # 若有新活动，一并抓取 atlas 与 data（包含活动代币、活动商店兑换物图标）
    families = "avatars,heads,skills,bonds,banner,atlas,data" if activity_info else "avatars,heads,skills,bonds,banner"

    # 1. 定向抓取新角色的头像、头像框、技能图标、卡池横幅（及活动图集/商店代币）
    code = _run_cmd([
        sys.executable,
        "tools/fetch_assets.py",
        "--target", ".",
        "--ids", ids_str,
        "--only", families,
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

    print(f"[OK] 新团员 {new_heroes} 与活动商店素材定向抓取全部完成！")
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
    if env_slot in ("end_time", "maint_mid_2h", "night_fallback"):
        return env_slot

    # 自动根据当前时刻推断：23:00 - 01:00 判定为夜间兜底，其余为维护开始后/结束时定向
    now_hour = datetime.now(_TZ8).hour
    if now_hour in (23, 0):
        return "night_fallback"
    return "maint_mid_2h"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ark-Recode 素材抓取智能调度与定向精准提取")
    parser.add_argument(
        "--slot",
        choices=["auto", "maint_mid_2h", "end_time", "night_fallback"],
        default="auto",
        help="触发时段 (maint_mid_2h: 维护开始后2小时定向抓取; end_time: 维护结束开服核验; night_fallback: 夜间12点增量兜底; auto: 自动识别)",
    )
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不写盘")
    args = parser.parse_args(argv)

    slot = resolve_slot(args.slot)
    print(f"[*] 智能调度生效时段: [{slot}] (请求槽位: {args.slot})")

    if slot in ("end_time", "maint_mid_2h"):
        announced_heroes = _load_announcement_heroes()
        return run_targeted_sync(announced_heroes, dry_run=args.dry_run)
    else:
        return run_night_fallback_sync(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
