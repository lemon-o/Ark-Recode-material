"""从官方维护公告 (data/announcement.json) 与游戏卡池元数据自动生成当前正在进行中的招募卡池情报 (data/current-pools.json)。

同时提供看板生成能力，可将卡池看板表格自动嵌入 README.md，供用户与下游应用（抽卡模拟器、Wiki、前端看板、机器人）即时获取：
1. 本期限定招募 (PickUp)、神秘招募与复刻招募的核心团员与专属羁绊；
2. 对应的卡池横幅 (Banner)、角色头像、羁绊小图 CDN 直链；
3. 保底抽数 (Pity Cap：限定 120 抽，神秘 200 抽)；
4. 开放时间与持续周期。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

ANNOUNCEMENT_PATH = ROOT / "data" / "announcement.json"
HEROES_PATH = ROOT / "data" / "heroes.json"
SUMMON_POOLS_PATH = ROOT / "data" / "summon-pools.json"
BONDS_PATH = ROOT / "data" / "bonds.json"
OUTPUT_PATH = ROOT / "data" / "current-pools.json"
README_PATH = ROOT / "README.md"

CDN_BASE = "https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main"

_TZ8 = timezone(timedelta(hours=8))

START_TAG = "<!-- CURRENT-POOLS:START -->"
END_TAG = "<!-- CURRENT-POOLS:END -->"


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def parse_hero_bond_pairs(text: str) -> dict[str, str]:
    """从公告正文中提取「团员」与「专属羁绊」对应关系。"""
    pairs: dict[str, str] = {}
    pattern = re.compile(
        r"(?:團員|限定團員|角色|英雄)?[「『]([^」』]+)[」』].*?與(?:專屬)?羈絆[「『]([^」』]+)[」』]"
    )
    for hero_name, bond_name in pattern.findall(text):
        pairs[hero_name.strip()] = bond_name.strip()
    return pairs


def parse_pool_duration(line_or_text: str) -> str:
    """提取卡池开放持续时长（如持续2周、持续1周）。"""
    m = re.search(r"持續\s*(\d+)\s*(周|週|天|日)", line_or_text)
    if m:
        unit = "周" if m.group(2) in ("周", "週") else "天"
        return f"{m.group(1)}{unit}"
    return "2周"


def resolve_bond_info(bond_name: str, bonds_meta: dict[str, dict[str, str]]) -> dict[str, str] | None:
    """依据羁绊名称反查羁绊 ID 与 CDN 地址。"""
    if not bond_name:
        return None
    name_clean = bond_name.strip()
    for aid, meta in bonds_meta.items():
        if meta.get("name_cht") == name_clean or meta.get("name_chs") == name_clean:
            return {
                "id": aid,
                "name_chs": meta.get("name_chs", name_clean),
                "name_cht": meta.get("name_cht", name_clean),
                "icon_url": f"{CDN_BASE}/bonds/{aid}.png",
            }
    # 模糊包含匹配
    for aid, meta in bonds_meta.items():
        chs = meta.get("name_chs", "")
        cht = meta.get("name_cht", "")
        if (chs and (name_clean in chs or chs in name_clean)) or (cht and (name_clean in cht or cht in name_clean)):
            return {
                "id": aid,
                "name_chs": chs or name_clean,
                "name_cht": cht or name_clean,
                "icon_url": f"{CDN_BASE}/bonds/{aid}.png",
            }
    return {
        "id": "",
        "name_chs": name_clean,
        "name_cht": name_clean,
        "icon_url": "",
    }


def build_current_pools_data(
    announcement: dict[str, Any],
    heroes_meta: dict[str, Any],
    pools_meta: dict[str, Any],
    bonds_meta: dict[str, Any],
) -> dict[str, Any]:
    content = announcement.get("content", "")
    maintenance = announcement.get("maintenance", {})
    heroes_section = announcement.get("heroes", {})

    hero_bond_map = parse_hero_bond_pairs(content)
    pools_dict = pools_meta.get("pools", {})
    all_heroes = heroes_meta.get("heroes", {})

    active_pools: list[dict[str, Any]] = []

    # 分类处理：pickup, mystic, rerun
    categories = [
        ("pickup", "限定招募", 120),
        ("mystic", "神秘招募", 200),
        ("rerun", "复刻招募", 120),
    ]

    for cat_key, cat_label, default_cap in categories:
        hero_list = heroes_section.get(cat_key, [])
        for h in hero_list:
            hid = str(h.get("id") or "").strip()
            if not hid:
                continue

            hero_detail = all_heroes.get(hid, {})
            name_chs = hero_detail.get("name_chs") or hero_detail.get("name") or h.get("name_chs") or h.get("name") or ""
            name_cht = hero_detail.get("name_cht") or h.get("name_cht") or h.get("name") or ""
            name_eng = hero_detail.get("name_eng") or h.get("name_eng") or ""

            # 寻找专属羁绊
            raw_bond_name = (
                hero_bond_map.get(name_cht)
                or hero_bond_map.get(name_chs)
                or hero_bond_map.get(h.get("name", ""))
                or ""
            )
            bond_info = resolve_bond_info(raw_bond_name, bonds_meta) if raw_bond_name else None

            # 寻找 Banner
            banner_file = f"BN_Summon_{hid}.png"
            banner_path = ROOT / "banners" / banner_file
            has_banner = banner_path.is_file()
            banner_url = f"{CDN_BASE}/banners/{banner_file}" if has_banner else ""

            # 匹配池信息与保底
            pool_id = f"ActivitySummon{hid}" if cat_key != "mystic" else f"MysticSummon{hid}"
            pool_cfg = pools_dict.get(pool_id) or pools_dict.get(f"AcyivitySummon{hid}") or {}
            cap = pool_cfg.get("cap") or default_cap

            # 持续时间
            duration = parse_pool_duration(content)

            pool_entry = {
                "pool_id": pool_id,
                "type": cat_label,
                "category": cat_key,
                "pity_cap": cap,
                "hero": {
                    "id": hid,
                    "name_chs": name_chs,
                    "name_cht": name_cht,
                    "name_eng": name_eng,
                    "star": hero_detail.get("star", h.get("star", 5)),
                    "element": hero_detail.get("element", h.get("element", "")),
                    "job": hero_detail.get("job", h.get("job", "")),
                    "avatar_url": f"{CDN_BASE}/avatars/{hid}.png",
                },
                "exclusive_bond": bond_info,
                "banner": {
                    "file": banner_file if has_banner else "",
                    "url": banner_url,
                },
                "start_time": maintenance.get("end_time") or maintenance.get("start_time") or "",
                "duration": duration,
            }
            active_pools.append(pool_entry)

    return {
        "updated_at": datetime.now(_TZ8).isoformat(timespec="seconds"),
        "announcement_id": announcement.get("id", ""),
        "activity_name": announcement.get("activity_name", ""),
        "update_type": announcement.get("update_type", "常规停机维护"),
        "is_hot_reload": announcement.get("is_hot_reload", False),
        "maintenance": maintenance,
        "active_pools": active_pools,
    }


def render_readme_dashboard(pools_data: dict[str, Any]) -> str:
    """生成嵌入 README.md 的卡池看板 Markdown 表格。"""
    active_pools = pools_data.get("active_pools", [])
    maint = pools_data.get("maintenance", {})
    window = maint.get("window", "")

    lines = [
        START_TAG,
        "<!-- 本看板联动官方维护公告与卡池配置自动生成，请勿手动修改 -->",
        f"> **当前招募活动窗口**：`{window}` | **更新类型**：{pools_data.get('update_type', '常规维护')}",
        "> **实时接口**：[`data/current-pools.json`](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/current-pools.json)",
        "",
        "| 卡池横幅 | 招募类型 | 核心角色 | 专属羁绊 | 保底上限 | 开放周期 |",
        "| :---: | :---: | :---: | :---: | :---: | :---: |",
    ]

    if not active_pools:
        lines.append("| *暂无正在进行中的招募卡池* | - | - | - | - | - |")
    else:
        for p in active_pools:
            hero = p.get("hero", {})
            bond = p.get("exclusive_bond") or {}
            banner = p.get("banner", {})

            # 横幅
            if banner.get("url"):
                banner_md = f"[![Banner]({banner['url']})]({banner['url']})"
            else:
                banner_md = "*(暂无横幅)*"

            # 角色
            star = f"{hero.get('star', 5)}★"
            elem = hero.get("element", "")
            job = hero.get("job", "")
            meta_desc = f"{star} {elem}/{job}".strip()
            avatar_url = hero.get("avatar_url", "")
            name = hero.get("name_chs") or hero.get("name_cht") or hero.get("id")
            hero_md = f"<img src=\"{avatar_url}\" width=\"36\" height=\"36\" align=\"center\" /> **[{name}]({avatar_url})**<br><sub>{meta_desc}</sub>"

            # 羁绊
            if bond and bond.get("name_chs"):
                bond_name = bond.get("name_chs")
                bond_icon = bond.get("icon_url")
                if bond_icon:
                    bond_md = f"<img src=\"{bond_icon}\" width=\"28\" height=\"28\" align=\"center\" /> 「{bond_name}」"
                else:
                    bond_md = f"「{bond_name}」"
            else:
                bond_md = "—"

            pool_type = p.get("type", "限定招募")
            cap = f"{p.get('pity_cap', 120)} 抽"
            duration = f"{p.get('duration', '2周')}"

            lines.append(f"| {banner_md} | **{pool_type}** | {hero_md} | {bond_md} | {cap} | {duration} |")

    lines.append(END_TAG)
    return "\n".join(lines)


def update_readme_with_dashboard(dashboard_text: str) -> bool:
    if not README_PATH.is_file():
        return False

    content = README_PATH.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START_TAG) + r".*?" + re.escape(END_TAG), re.S)

    if pattern.search(content):
        new_content = pattern.sub(lambda _: dashboard_text, content, count=1)
    else:
        # 插入到「当前状态」块之后
        status_end_tag = "<!-- SYNC-STATS:END -->"
        if status_end_tag in content:
            new_content = content.replace(
                status_end_tag,
                status_end_tag + "\n\n### 当前招募活动动态看板 (Active Summon Pools)\n\n" + dashboard_text,
            )
        else:
            new_content = content + "\n\n### 当前招募活动动态看板 (Active Summon Pools)\n\n" + dashboard_text + "\n"

    if new_content != content:
        README_PATH.write_text(new_content, encoding="utf-8")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成当前正在进行中的招募卡池情报 (data/current-pools.json)")
    parser.add_argument("--announcement", type=Path, default=ANNOUNCEMENT_PATH, help="维护公告路径")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH, help="输出文件路径")
    parser.add_argument("--update-readme", action="store_true", help="是否同步更新 README.md 看板")
    args = parser.parse_args(argv)

    announcement = _load_json(args.announcement)
    if not announcement:
        print("[!] 维护公告文件为空，跳过卡池情报生成。")
        return 0

    heroes_meta = _load_json(HEROES_PATH)
    pools_meta = _load_json(SUMMON_POOLS_PATH)
    bonds_meta = _load_json(BONDS_PATH)

    pools_data = build_current_pools_data(announcement, heroes_meta, pools_meta, bonds_meta)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(pools_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[OK] 已导出当前卡池情报 -> {args.output} (收录 {len(pools_data.get('active_pools', []))} 个活跃卡池)")

    if args.update_readme:
        dashboard = render_readme_dashboard(pools_data)
        updated = update_readme_with_dashboard(dashboard)
        if updated:
            print("[OK] 已同步刷新 README.md 卡池看板")
        else:
            print("[*] README.md 看板内容无变化")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
