"""从官方维护公告 (data/announcement.json) 与游戏卡池元数据自动生成当前正在进行中的招募卡池情报 (data/current-pools.json)。

同时提供看板生成能力，可将卡池看板表格自动嵌入 README.md，供用户与下游应用（抽卡模拟器、Wiki、前端看板、机器人）即时获取：
1. 本期限定招募 (PickUp)、神秘招募与复刻招募的核心团员与专属羁绊；
2. 对应的卡池横幅 (Banner)、角色头像、羁绊小图 CDN 直链；
3. 保底抽数 (Pity Cap：限定 120 抽，神秘 200 抽)；
4. 具体开放时间段（依据开服时间 + 默认/公告持续周期精准计算）。
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


def calculate_pool_timeline(
    category: str,
    hero_name: str,
    content: str,
    start_time_iso: str,
) -> dict[str, Any]:
    """计算卡池开放时间范围与持续时长。

    业务规则：
    1. 优先从该团员所在公告行提取显式指定的开放周期（如「持續1周」、「持續3周」）；
    2. 若未明确说明：
       - 神秘招募卡池 (category == 'mystic') 默认持续 3 周 (21 天)；
       - 其他限时卡池（限定招募、复刻招募）默认持续 2 周 (14 天)；
    3. 自动由卡池开放开始时间 + 持续时长计算出具体的截止时间段。
    """
    days = 21 if category == "mystic" else 14
    dur_str = "3周" if category == "mystic" else "2周"

    # 1. 尝试从包含该角色名字的特定行提取
    found_explicit = False
    for line in content.splitlines():
        if hero_name and hero_name in line:
            m = re.search(r"持續\s*(\d+)\s*(周|週|天|日)", line)
            if m:
                val = int(m.group(1))
                unit = m.group(2)
                if unit in ("周", "週"):
                    days = val * 7
                    dur_str = f"{val}周"
                else:
                    days = val
                    dur_str = f"{val}天"
                found_explicit = True
                break

    # 2. 若单行未指明，看整篇是否有一致指定的活动周期
    if not found_explicit:
        m = re.search(r"【招募機率UP】.*?持續\s*(\d+)\s*(周|週|天|日)", content, re.S)
        if m:
            val = int(m.group(1))
            unit = m.group(2)
            if unit in ("周", "週"):
                days = val * 7
                dur_str = f"{val}周"
            else:
                days = val
                dur_str = f"{val}天"

    start_iso = ""
    end_iso = ""
    display_time = ""

    if start_time_iso:
        try:
            s_dt = datetime.fromisoformat(start_time_iso)
            e_dt = s_dt + timedelta(days=days)
            start_iso = s_dt.isoformat()
            end_iso = e_dt.isoformat()
            display_time = f"{s_dt.strftime('%Y/%m/%d %H:%M')} - {e_dt.strftime('%Y/%m/%d %H:%M')}"
        except Exception:
            display_time = f"持续 {dur_str}"
    else:
        display_time = f"持续 {dur_str}"

    return {
        "start_time": start_iso,
        "end_time": end_iso,
        "duration": dur_str,
        "duration_days": days,
        "display_time": display_time,
    }


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

    base_start_time = maintenance.get("end_time") or maintenance.get("start_time") or ""

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

            # 精准计算开放时间段
            timeline = calculate_pool_timeline(
                category=cat_key,
                hero_name=name_cht or name_chs,
                content=content,
                start_time_iso=base_start_time,
            )

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
                "start_time": timeline["start_time"],
                "end_time": timeline["end_time"],
                "duration": timeline["duration"],
                "duration_days": timeline["duration_days"],
                "display_time": timeline["display_time"],
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
        "| 卡池横幅 | 招募类型 | 专属羁绊 | 开放时间 |",
        "| :---: | :---: | :---: | :---: |",
    ]

    if not active_pools:
        lines.append("| *暂无正在进行中的招募卡池* | - | - | - |")
    else:
        for p in active_pools:
            bond = p.get("exclusive_bond") or {}
            banner = p.get("banner", {})

            # 横幅
            if banner.get("url"):
                banner_md = f"[![Banner]({banner['url']})]({banner['url']})"
            else:
                banner_md = "*(暂无横幅)*"

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
            display_time = p.get("display_time", "")
            dur_str = p.get("duration", "2周")
            time_cell = f"{display_time}<br><sub>(持续 {dur_str})</sub>" if display_time else f"持续 {dur_str}"

            lines.append(f"| {banner_md} | **{pool_type}** | {bond_md} | {time_cell} |")

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
