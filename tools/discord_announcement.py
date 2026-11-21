"""Discord 官方公告抓取与解析模块 (tools/discord_announcement.py)。

优先于 EROLABS 网页新闻：
1. 抓取渠道（优先梯队）：
   - 渠道 A：环境变量 DISCORD_ANNOUNCEMENT_TEXT（支持 workflow_dispatch / 手动复制直接传入）
   - 渠道 B：Discord REST API（若配置了 DISCORD_BOT_TOKEN 或 DISCORD_USER_TOKEN 与 DISCORD_CHANNEL_ID）
   - 渠道 C：本地最新基线 data/discord-notice.json（若近期有效）
2. 结构化解析：
   - 维护时间窗口（起始、结束、时间戳）
   - 限定活动招募团员（PickUp Heroes）
   - 神秘招募团员（Mystic Heroes，解决网页新闻不发神秘池的问题）
   - 复刻招募团员（Rerun Heroes）
3. 联动支持：
   - 供 tools/check_version.py 优先判定维护与版本更新
   - 供 tools/build_heroes.py 自动将本期确认为新上线的团员从未发布名单中放行
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
HEROES_JSON_PATH = ROOT / "data" / "heroes.json"
NOTICE_CACHE_PATH = ROOT / "data" / "discord-notice.json"

GAME_TZ = timezone(timedelta(hours=8))

# 默认官方中文公告频道 ID（可通过环境变量覆盖）
# 官方服务器 Ark Re:Code (Guild: 1119147450981158955)
DEFAULT_DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "1119147451576754232")


def parse_maintenance_time(text: str, default_year: int | None = None) -> dict[str, Any] | None:
    """从公告文本提取维护时间窗口。

    兼容两种常见格式：
    1. 时间范围：11/18 08:00 - 14:00 (UTC+8)
    2. 单点更新/不停机更新：預計將於11/18 08:00 (UTC+8)進行活動更新
    """
    if not text:
        return None
    if default_year is None:
        default_year = datetime.now(GAME_TZ).year

    # 格式 1：时间范围
    match_range = re.search(
        r"(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?(?:\s*\([^\)]*\))?\s*"
        r"(\d{1,2}):(\d{2})\s*[-~至到]\s*(\d{1,2}):(\d{2})(?:\s*\(?UTC\+?8\)?)?",
        text,
        re.IGNORECASE,
    )
    if match_range:
        year = int(match_range.group(1)) if match_range.group(1) else default_year
        month = int(match_range.group(2))
        day = int(match_range.group(3))
        start_h, start_m = int(match_range.group(4)), int(match_range.group(5))
        end_h, end_m = int(match_range.group(6)), int(match_range.group(7))

        try:
            start_dt = datetime(year, month, day, start_h, start_m, tzinfo=GAME_TZ)
            end_dt = datetime(year, month, day, end_h, end_m, tzinfo=GAME_TZ)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            return {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "startTimestamp": start_dt.timestamp(),
                "endTimestamp": end_dt.timestamp(),
                "raw": match_range.group(0).strip(),
            }
        except Exception:
            pass

    # 格式 2：单点更新（如不停机更新 11/18 08:00 (UTC+8)）
    match_single = re.search(
        r"(?:將?於|时间|時間)?\s*(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?(?:\s*\([^\)]*\))?\s*"
        r"(\d{1,2}):(\d{2})(?:\s*\(?UTC\+?8\)?)?",
        text,
        re.IGNORECASE,
    )
    if match_single:
        year = int(match_single.group(1)) if match_single.group(1) else default_year
        month = int(match_single.group(2))
        day = int(match_single.group(3))
        start_h, start_m = int(match_single.group(4)), int(match_single.group(5))
        try:
            start_dt = datetime(year, month, day, start_h, start_m, tzinfo=GAME_TZ)
            end_dt = start_dt + timedelta(hours=1)
            return {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "startTimestamp": start_dt.timestamp(),
                "endTimestamp": end_dt.timestamp(),
                "raw": match_single.group(0).strip(),
            }
        except Exception:
            pass

    return None


def _load_hero_name_map() -> dict[str, str]:
    """从 data/heroes.json 构建「全名/别名 -> 角色 ID」映射。"""
    if not HEROES_JSON_PATH.is_file():
        return {}
    try:
        data = json.loads(HEROES_JSON_PATH.read_text(encoding="utf-8"))
        heroes = data.get("heroes") or {}
    except (OSError, ValueError):
        return {}

    mapping: dict[str, str] = {}
    # 先填简短名，再填带称号全称，确保全称覆盖简短名
    for hid, h in heroes.items():
        for k in ["name", "name_chs", "name_cht", "name_eng"]:
            val = h.get(k)
            if val and isinstance(val, str):
                mapping[val.strip()] = hid

    return mapping


def parse_announcement_text(content: str) -> dict[str, Any]:
    """深度解析 Discord 官方维护公告纯文本。"""
    name_map = _load_hero_name_map()
    window = parse_maintenance_time(content)

    pickup_heroes: list[dict[str, str]] = []
    mystic_heroes: list[dict[str, str]] = []
    rerun_heroes: list[dict[str, str]] = []

    # 1. 拆解单行或段落
    lines = content.splitlines()

    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue

        # 匹配书名号「...」中的角色名或羁绊名
        bracket_names = re.findall(r"「([^」]+)」", line_clean)
        if not bracket_names:
            continue

        is_mystic = bool(re.search(r"神秘(?:招募|契約|池)|銀河(?:招募|契約)", line_clean))
        is_rerun = bool(re.search(r"復刻|复刻", line_clean))
        is_pickup = bool(re.search(r"活動招募|限定招募|PickUp|全新團員|登場|加入", line_clean))

        for name in bracket_names:
            name_strip = name.strip()
            hid = name_map.get(name_strip)
            if not hid:
                # 模糊匹配：若包含特定全名
                for known_name, target_id in name_map.items():
                    if len(known_name) >= 3 and known_name in name_strip:
                        hid = target_id
                        break

            if not hid:
                continue

            hero_entry = {"id": hid, "name": name_strip}

            if is_mystic:
                if hero_entry not in mystic_heroes:
                    mystic_heroes.append(hero_entry)
            elif is_rerun:
                if hero_entry not in rerun_heroes:
                    rerun_heroes.append(hero_entry)
            elif is_pickup or not (is_mystic or is_rerun):
                if hero_entry not in pickup_heroes:
                    pickup_heroes.append(hero_entry)

    # 2. 提取整篇简短活动标题
    title_match = re.search(r"限定活動[「『]([^」』]+)[」』]|主題活動[「『]([^」』]+)[」』]", content)
    activity_name = title_match.group(1) or title_match.group(2) if title_match else ""

    summary_parts = []
    if activity_name:
        summary_parts.append(f"限定活動「{activity_name}」")
    if pickup_heroes:
        summary_parts.append("限定: " + "/".join(h["name"] for h in pickup_heroes))
    if mystic_heroes:
        summary_parts.append("神秘: " + "/".join(h["name"] for h in mystic_heroes))

    summary_title = " · ".join(summary_parts) if summary_parts else (content[:50].strip() + "...")

    return {
        "title": summary_title,
        "activity_name": activity_name,
        "window": window.get("raw") if window else "",
        "startTime": window.get("start") if window else "",
        "endTime": window.get("end") if window else "",
        "startTimestamp": window.get("startTimestamp") if window else 0.0,
        "endTimestamp": window.get("endTimestamp") if window else 0.0,
        "pickup_heroes": pickup_heroes,
        "mystic_heroes": mystic_heroes,
        "rerun_heroes": rerun_heroes,
        "content": content,
    }


def extract_message_text(msg: dict[str, Any]) -> str:
    """提取消息文本，兼容原生文本、转发快照 (message_snapshots) 及富文本 Embeds。"""
    parts: list[str] = []
    content = (msg.get("content") or "").strip()
    if content:
        parts.append(content)

    # 1. 兼容 Discord 官方消息转发 / 快照 (Forwarded Message / message_snapshots)
    for snap in msg.get("message_snapshots") or []:
        s_msg = snap.get("message") or {}
        s_content = (s_msg.get("content") or "").strip()
        if s_content:
            parts.append(s_content)
        for emb in s_msg.get("embeds") or []:
            title = (emb.get("title") or "").strip()
            desc = (emb.get("description") or "").strip()
            if title:
                parts.append(title)
            if desc:
                parts.append(desc)

    # 2. 兼容原生 Embeds (如 Webhook 机器人推送的富文本卡片)
    for emb in msg.get("embeds") or []:
        title = (emb.get("title") or "").strip()
        desc = (emb.get("description") or "").strip()
        if title:
            parts.append(title)
        if desc:
            parts.append(desc)

    return "\n\n".join(parts)


def fetch_from_discord_api(
    token: str,
    channel_id: str,
    timeout: float = 10.0,
) -> str | None:
    """通过 Discord API 获取官方频道最新消息文本。"""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=10"

    auth_header = token.strip()
    if not (auth_header.startswith("Bot ") or auth_header.startswith("Bearer ")):
        # 若为常规 Bot Token，补全 Bot 前缀
        auth_header = f"Bot {auth_header}"

    headers = {
        "Authorization": auth_header,
        "User-Agent": "DiscordBot (https://github.com/lemon-o/Ark-Recode-material, 1.0.0)",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                messages = resp.json()
                if messages and isinstance(messages, list):
                    # 遍历最新消息，优先寻找包含维护或招募信息的公告
                    for msg in messages:
                        text = extract_message_text(msg)
                        if any(k in text for k in ("維護", "维护", "招募", "更新", "機率UP", "概率UP")):
                            return text
                    # 若未命中特定关键词，取第一条最新非空文本
                    for msg in messages:
                        text = extract_message_text(msg)
                        if text:
                            return text
            else:
                print(f"[Discord API] 读取失败: HTTP {resp.status_code} ({resp.text[:100]})", file=sys.stderr)
    except Exception as exc:
        print(f"[Discord API] 请求异常: {exc}", file=sys.stderr)

    return None


def fetch_discord_notice(timeout: float = 10.0) -> dict[str, Any] | None:
    """获取并解析 Discord 官方频道的最新公告。

    优先级：
    1. 环境变量 DISCORD_ANNOUNCEMENT_TEXT（手动注入或工作流入参）
    2. DISCORD_BOT_TOKEN / DISCORD_USER_TOKEN 远程抓取
    3. 本地缓存 data/discord-notice.json
    """
    raw_text = os.environ.get("DISCORD_ANNOUNCEMENT_TEXT", "").strip()

    # 1. 环境变量直接注入
    if raw_text:
        parsed = parse_announcement_text(raw_text)
        parsed["source"] = "discord_env"
        return parsed

    # 2. Discord API 读取
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_USER_TOKEN") or os.environ.get("DISCORD_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID", DEFAULT_DISCORD_CHANNEL_ID)

    if token and channel_id:
        text = fetch_from_discord_api(token, channel_id, timeout=timeout)
        if text:
            parsed = parse_announcement_text(text)
            parsed["source"] = "discord_api"
            return parsed

    # 3. 本地磁盘基线缓存
    if NOTICE_CACHE_PATH.is_file():
        try:
            cached = json.loads(NOTICE_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("content"):
                cached["source"] = "discord_cache"
                return cached
        except (OSError, ValueError):
            pass

    return None


def save_discord_notice(notice_data: dict[str, Any]) -> None:
    """持久化 Discord 公告状态至 data/discord-notice.json。"""
    NOTICE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    NOTICE_CACHE_PATH.write_text(
        json.dumps(notice_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已持久化 Discord 公告基线 -> {NOTICE_CACHE_PATH}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="解析并测试 Discord 官方频道公告")
    parser.add_argument("--text", type=str, help="直接指定公告文本进行测试解析")
    parser.add_argument("--save", action="store_true", help="保存解析结果到 data/discord-notice.json")
    args = parser.parse_args(argv)

    if args.text:
        res = parse_announcement_text(args.text)
        res["source"] = "cli_text"
    else:
        res = fetch_discord_notice()

    if not res:
        print("未获取到有效的 Discord 公告信息（未配置 Token 或无文本输入）")
        return 1

    print("=== Discord 公告解析结果 ===")
    print(f"标题/概要: {res.get('title')}")
    print(f"维护窗口:   {res.get('window')}")
    print(f"限定招募:   {res.get('pickup_heroes')}")
    print(f"神秘招募:   {res.get('mystic_heroes')}")
    print(f"复刻招募:   {res.get('rerun_heroes')}")
    print(f"情报来源:   {res.get('source')}")

    if args.save:
        save_discord_notice(res)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
