"""从官方维护公告与游戏卡池元数据自动生成当前正在进行中的招募卡池情报 (data/current-pools.json)。

同时提供看板生成能力，可将卡池看板表格自动嵌入 README.md，供用户与下游应用（抽卡模拟器、Wiki、前端看板、机器人）即时获取：
1. 正在开放中的所有未结束卡池（包含多期重叠卡池、限定招募、神秘招募与限定组合招募）；
2. 招募类型精准标注：首发团员标注为「限定招募 (首发)」，复刻角色标注为「限定招募」；
3. 特殊限时卡池：支持限定组合招募等自选池；
4. 对应的卡池横幅 (Banner)、专属羁绊小图 CDN 直链；
5. 具体开放时间段（依据开服时间 + 默认/公告持续周期精准计算）。
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

ANNOUNCEMENTS_DIR = ROOT / "data" / "announcements"
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

# 已知首发上线团员名单（用于首发标注兜底）
KNOWN_DEBUT_HERO_IDS = {"H195", "H199"}


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
    target_name: str,
    content: str,
    start_time_iso: str,
) -> dict[str, Any]:
    """计算卡池开放时间范围与持续时长。

    业务规则：
    1. 优先从该卡池/团员所在公告行提取显式指定的开放周期（如「持續1周」、「持續3周」）；
    2. 若未明确说明：
       - 神秘招募卡池 (category == 'mystic') 默认持续 3 周 (21 天)；
       - 其他限时卡池（限定招募、限定组合招募、复刻招募）默认持续 2 周 (14 天)；
    3. 自动由卡池开放开始时间 + 持续时长计算出具体的截止时间段。
    """
    days = 21 if category == "mystic" else 14
    dur_str = "3周" if category == "mystic" else "2周"

    # 1. 尝试从包含该目标名字的特定行提取
    for line in content.splitlines():
        if target_name and target_name in line:
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
                break

    start_iso = ""
    end_iso = ""
    display_time = ""

    if start_time_iso:
        try:
            s_dt = datetime.fromisoformat(start_time_iso)
            # 计算到期日期
            end_date = (s_dt + timedelta(days=days)).date()

            # 卡池结束时刻判定准则：
            # 1. 若卡池本身为 08:00 开启（早间/不停机更新），到期日维护通常在 08:00 结束
            # 2. 其余常规维护卡池（如 14:00 关机），到期日随官方维护开启，严格在 14:00 关闭（绝非 19:00 或晚上）
            close_hour = 8 if s_dt.hour == 8 else 14
            e_dt = datetime(end_date.year, end_date.month, end_date.day, close_hour, 0, tzinfo=s_dt.tzinfo)

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


def is_debut_hero(
    hero_id: str,
    hero_name: str,
    content: str,
    explicit_flag: bool | None = None,
) -> bool:
    """判定团员是否为本期首发上线团员。"""
    if explicit_flag is not None:
        return bool(explicit_flag)
    if hero_id in KNOWN_DEBUT_HERO_IDS:
        return True
    for line in content.splitlines():
        if hero_name and hero_name in line:
            if re.search(r"全新|首發|首发|首度|初登場|初次登場", line):
                return True
    return False


def is_hero_limited(
    hero_id: str,
    hero_name: str,
    content: str,
    hero_detail: dict[str, Any],
) -> bool:
    """判定团员是否为限定角色（非限定即为常驻/普池团员）。

    三重判定：
    1. heroes.json 元数据中的 is_limited 标记（取自官方 Item.txt Tags 列，最底层最权威依据）；
    2. 公告正文中该角色所在行是否明确冠以「限定團員」/「限定角色」；
    3. 公告行未加限定者（如「團員「埃帕蒂」」）判定为常规常驻角色。
    """
    # 1. 优先查验 heroes.json 中的底层官方标记
    if "is_limited" in hero_detail:
        if not hero_detail["is_limited"]:
            # 数据表标记为常驻时，除非公告单行明确强调「限定團員」，否则为常驻
            for line in content.splitlines():
                if hero_name and hero_name in line and re.search(r"限定(?:團員|角色|英雄)", line):
                    return True
            return False
        return True

    # 2. 公告文本行检查
    for line in content.splitlines():
        if hero_name and hero_name in line:
            if re.search(r"限定(?:團員|角色|英雄)", line):
                return True

    return False


def resolve_pool_type(
    cat_key: str,
    cat_label: str,
    is_limited: bool,
    is_debut: bool,
) -> str:
    """依据招募类型、限定状态与首发状态生成规范的招募类型标签。"""
    if cat_key == "mystic":
        return "神秘招募 (首发)" if is_debut else "神秘招募"

    if is_limited:
        return "限定招募 (首发)" if is_debut else "限定招募"
    else:
        # 常驻团员（如埃帕蒂）机率UP招募
        return "常规招募 (首发)" if is_debut else "常规招募"


def parse_combo_pools(
    content: str,
    base_start_time: str,
) -> list[dict[str, Any]]:
    """从公告正文中解析限定组合招募与自选神秘招募等特殊活动卡池。"""
    special_pools: list[dict[str, Any]] = []
    lines = content.splitlines()

    for line in lines:
        # 1. 限定组合招募
        if re.search(r"組合招募|组合招募", line):
            m_name = re.search(r"【(?:招募機率UP|招募概率UP)】\s*([^\n，,]*?(?:組合招募|组合招募)[^\n，,]*)", line)
            raw_name = m_name.group(1).strip() if m_name else "限定组合招募"
            clean_name = re.sub(r"(?:開放|开启|開啟)$", "", raw_name).strip()

            timeline = calculate_pool_timeline(
                category="combo",
                target_name=clean_name,
                content=line,
                start_time_iso=base_start_time,
            )

            banner_file = "BN_Summon_MultiSummonLimit.png"
            banner_path = ROOT / "banners" / banner_file
            banner_url = f"{CDN_BASE}/banners/{banner_file}" if banner_path.is_file() else ""

            entry = {
                "pool_id": "ComboSummon_MultiSummonLimit",
                "name": clean_name,
                "type": "限定组合招募",
                "category": "combo",
                "is_debut": False,
                "pity_cap": 120,
                "hero": None,
                "exclusive_bond": {
                    "id": "",
                    "name_chs": "自选3名团员",
                    "name_cht": "自選3名團員",
                    "icon_url": "",
                },
                "banner": {
                    "file": banner_file if banner_path.is_file() else "",
                    "url": banner_url,
                },
                "start_time": timeline["start_time"],
                "end_time": timeline["end_time"],
                "duration": timeline["duration"],
                "duration_days": timeline["duration_days"],
                "display_time": timeline["display_time"],
            }
            special_pools.append(entry)

        # 2. 自选神秘招募（如 3周年紀念神秘招募）
        elif re.search(r"神秘招募.*?(?:選擇|选择).*?團員|神秘招募.*?(?:自選|自选)", line):
            m_name = re.search(r"【(?:招募機率UP|招募概率UP)】\s*([^\n，,]*?神秘招募[^\n，,]*)", line)
            raw_name = m_name.group(1).strip() if m_name else "自选神秘招募"
            clean_name = re.sub(r"(?:開放|开启|開啟)$", "", raw_name).strip()

            timeline = calculate_pool_timeline(
                category="mystic",
                target_name=clean_name,
                content=line,
                start_time_iso=base_start_time,
            )

            banner_file = "BN_Summon_MultiSummonMystic.png"
            banner_path = ROOT / "banners" / banner_file
            banner_url = f"{CDN_BASE}/banners/{banner_file}" if banner_path.is_file() else ""

            entry = {
                "pool_id": "MysticSummon_MultiSummonMystic",
                "name": clean_name,
                "type": "神秘招募 (自选)",
                "category": "mystic",
                "is_debut": False,
                "pity_cap": 200,
                "hero": None,
                "exclusive_bond": {
                    "id": "",
                    "name_chs": "自选1名团员",
                    "name_cht": "自選1名團員",
                    "icon_url": "",
                },
                "banner": {
                    "file": banner_file if banner_path.is_file() else "",
                    "url": banner_url,
                },
                "start_time": timeline["start_time"],
                "end_time": timeline["end_time"],
                "duration": timeline["duration"],
                "duration_days": timeline["duration_days"],
                "display_time": timeline["display_time"],
            }
            special_pools.append(entry)

    return special_pools


def extract_pools_from_announcement(
    announcement: dict[str, Any],
    heroes_meta: dict[str, Any],
    pools_meta: dict[str, Any],
    bonds_meta: dict[str, Any],
) -> list[dict[str, Any]]:
    """从单个公告对象中提取该公告所涉及的全部招募卡池。"""
    content = announcement.get("content", "")
    maintenance = announcement.get("maintenance", {})
    heroes_section = announcement.get("heroes", {})

    hero_bond_map = parse_hero_bond_pairs(content)
    pools_dict = pools_meta.get("pools", {})
    all_heroes = heroes_meta.get("heroes", {})

    pools: list[dict[str, Any]] = []

    base_start_time = maintenance.get("end_time") or maintenance.get("start_time") or ""

    # 1. 角色限定 / 神秘 / 复刻卡池
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

            raw_bond_name = (
                hero_bond_map.get(name_cht)
                or hero_bond_map.get(name_chs)
                or hero_bond_map.get(h.get("name", ""))
                or ""
            )
            bond_info = resolve_bond_info(raw_bond_name, bonds_meta) if raw_bond_name else None

            banner_file = f"BN_Summon_{hid}.png"
            banner_path = ROOT / "banners" / banner_file
            has_banner = banner_path.is_file()
            banner_url = f"{CDN_BASE}/banners/{banner_file}" if has_banner else ""

            pool_id = f"ActivitySummon{hid}" if cat_key != "mystic" else f"MysticSummon{hid}"
            pool_cfg = pools_dict.get(pool_id) or pools_dict.get(f"AcyivitySummon{hid}") or {}
            cap = pool_cfg.get("cap") or default_cap

            timeline = calculate_pool_timeline(
                category=cat_key,
                target_name=name_cht or name_chs,
                content=content,
                start_time_iso=base_start_time,
            )

            debut = is_debut_hero(
                hero_id=hid,
                hero_name=name_cht or name_chs,
                content=content,
                explicit_flag=h.get("is_debut"),
            )

            limited = is_hero_limited(
                hero_id=hid,
                hero_name=name_cht or name_chs,
                content=content,
                hero_detail=hero_detail,
            )

            type_label = resolve_pool_type(
                cat_key=cat_key,
                cat_label=cat_label,
                is_limited=limited,
                is_debut=debut,
            )

            pool_entry = {
                "pool_id": pool_id,
                "type": type_label,
                "category": cat_key,
                "is_debut": debut,
                "is_limited": limited,
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
            pools.append(pool_entry)

    # 2. 特殊限时组合招募池
    combo_pools = parse_combo_pools(content=content, base_start_time=base_start_time)
    pools.extend(combo_pools)

    return pools


def load_all_announcements(primary_announcement: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """汇总已归档的历史公告与当前公告。"""
    announcements: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if ANNOUNCEMENTS_DIR.is_dir():
        for f in sorted(ANNOUNCEMENTS_DIR.glob("*.json")):
            data = _load_json(f)
            aid = str(data.get("id") or f.stem)
            if aid and aid not in seen_ids:
                seen_ids.add(aid)
                announcements.append(data)

    if primary_announcement:
        aid = str(primary_announcement.get("id") or "")
        if aid and aid not in seen_ids:
            seen_ids.add(aid)
            announcements.append(primary_announcement)
        elif not aid:
            announcements.append(primary_announcement)

    return announcements


def build_current_pools_data(
    primary_announcement: dict[str, Any],
    heroes_meta: dict[str, Any],
    pools_meta: dict[str, Any],
    bonds_meta: dict[str, Any],
    now_dt: datetime | None = None,
) -> dict[str, Any]:
    """汇总并构建当前所有未结束的招募活动卡池情报。"""
    if now_dt is None:
        now_dt = datetime.now(_TZ8)

    announcements = load_all_announcements(primary_announcement)
    if not announcements and primary_announcement:
        announcements = [primary_announcement]

    existing_current = _load_json(OUTPUT_PATH)
    existing_pools = existing_current.get("active_pools") or []

    pools_registry: dict[str, dict[str, Any]] = {}

    # 1. 载入先前已存在的活跃池基线
    for p in existing_pools:
        pid = p.get("pool_id")
        if pid:
            pools_registry[pid] = p

    # 2. 从所有收集到的公告中提取/覆盖卡池
    order_counter = 0
    for ann in announcements:
        extracted = extract_pools_from_announcement(ann, heroes_meta, pools_meta, bonds_meta)
        for ep in extracted:
            order_counter += 1
            ep["_order"] = order_counter
            pid = ep["pool_id"]
            pools_registry[pid] = ep

    latest_ann = primary_announcement or (announcements[-1] if announcements else {})
    maintenance = latest_ann.get("maintenance", {})
    maint_start_dt = None
    if maintenance and maintenance.get("start_time"):
        try:
            maint_start_dt = datetime.fromisoformat(maintenance["start_time"])
        except Exception:
            pass

    # 3. 过滤并自动清理已截止的卡池
    # 核心铁律：每一次官方维护都会关闭当天（或之前）过期的卡池
    active_pools: list[dict[str, Any]] = []
    for pid, pool in pools_registry.items():
        end_time_str = pool.get("end_time")
        if not end_time_str:
            active_pools.append(pool)
            continue

        try:
            end_dt = datetime.fromisoformat(end_time_str)
        except Exception:
            active_pools.append(pool)
            continue

        # 规则 1：客观时间已经超过截止时间 -> 清理剔除
        if now_dt >= end_dt:
            continue

        # 规则 2：官方维护自动清理机制——当期官方维护一旦开始，当天（或更早）到期的往期卡池随维护关闭，一律清理剔除
        if maint_start_dt and now_dt >= maint_start_dt:
            if end_dt.date() <= maint_start_dt.date():
                continue

        active_pools.append(pool)

    # 4. 排序：按卡池开放起始时间升序排列，相同起始时间保持公告声明顺序
    active_pools.sort(key=lambda x: (x.get("start_time") or "", x.get("_order", 0)))
    for p in active_pools:
        p.pop("_order", None)

    return {
        "updated_at": now_dt.isoformat(timespec="seconds"),
        "announcement_id": latest_ann.get("id", ""),
        "activity_name": latest_ann.get("activity_name", ""),
        "update_type": latest_ann.get("update_type", "活动更新"),
        "is_hot_reload": latest_ann.get("is_hot_reload", False),
        "maintenance": maintenance,
        "active_pools": active_pools,
    }


def render_readme_dashboard(pools_data: dict[str, Any]) -> str:
    """生成嵌入 README.md 的卡池看板 Markdown 表格（精简 4 列规范）。"""
    active_pools = pools_data.get("active_pools", [])
    maint = pools_data.get("maintenance", {})
    window = maint.get("window", "")

    lines = [
        START_TAG,
        "<!-- 本看板联动官方维护公告与卡池配置自动生成，请勿手动修改 -->",
        f"> **当前招募活动窗口**：`{window}` | **更新类型**：{pools_data.get('update_type', '活动更新')}",
        "> **实时接口**：[`data/current-pools.json`](https://cdn.jsdelivr.net/gh/lemon-o/Ark-Recode-material@main/data/current-pools.json)",
        "",
        "| 卡池横幅 | 招募类型 | 专属羁绊 | 开放时间 |",
        "| :---: | :---: | :---: | :---: |",
    ]

    if not active_pools:
        lines.append("| *(暂无进行中的招募卡池)* | - | - | - |")
    else:
        for p in active_pools:
            bond = p.get("exclusive_bond") or {}
            banner = p.get("banner", {})

            # 横幅列
            if banner.get("url"):
                banner_md = f"[![Banner]({banner['url']})]({banner['url']})"
            else:
                banner_md = "*(暂无横幅)*"

            # 专属羁绊列
            if p.get("category") == "combo" or bond.get("name_chs") == "自选3名团员":
                bond_md = "— (自选3名团员)"
            elif bond and bond.get("name_chs") == "自选1名团员":
                bond_md = "— (自选1名团员)"
            elif bond and bond.get("name_chs"):
                bond_name = bond.get("name_chs")
                bond_icon = bond.get("icon_url")
                if bond_icon:
                    bond_md = f'<img src="{bond_icon}" width="28" height="28" align="center" /> 「{bond_name}」'
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
