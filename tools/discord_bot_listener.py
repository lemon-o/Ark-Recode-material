"""本地/自建守护进程：Discord 官方公告监听与 GitHub Actions 触发器 (tools/discord_bot_listener.py)。

适用于希望在本地、NAS 或自建 Linux 主机上运行常驻守护进程的用户：
1. 定时（默认每 60 秒）轮询 Discord 目标频道（#综合）；
2. 提取并分析最新公告；
3. 一旦发现新发布的维护或招募公告，自动调用 GitHub API (repository_dispatch) 触发云端 Actions 运行。

运行方式：
    python tools/discord_bot_listener.py --interval 60
    python tools/discord_bot_listener.py --once
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
from tools.discord_announcement import extract_message_text, parse_announcement_text

DEFAULT_CHANNEL_ID = "1573590290424401932"
DEFAULT_REPO = "lemon-o/Ark-Recode-material"
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / ".last_dispatched_msg.txt"


def get_latest_announcement(
    bot_token: str,
    channel_id: str,
    timeout: float = 10.0,
) -> dict[str, Any] | None:
    """从 Discord 抓取最新消息并提取公告。"""
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages?limit=5"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "User-Agent": "DiscordBot (https://github.com/lemon-o/Ark-Recode-material, 1.0.0)",
    }

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code != 200:
                print(f"[!] Discord API 报错: HTTP {resp.status_code} ({resp.text[:100]})", file=sys.stderr)
                return None

            messages = resp.json()
            if not isinstance(messages, list) or not messages:
                return None

            keywords = ("維護", "维护", "更新", "招募", "機率UP", "概率UP")
            for msg in messages:
                text = extract_message_text(msg)
                if any(k in text for k in keywords):
                    return {
                        "id": msg.get("id"),
                        "timestamp": msg.get("timestamp"),
                        "content": text,
                    }

    except Exception as exc:
        print(f"[!] 请求 Discord 失败: {exc}", file=sys.stderr)

    return None


def dispatch_github_workflow(
    github_token: str,
    repo: str,
    announcement: dict[str, Any],
) -> bool:
    """向 GitHub 发送 repository_dispatch 请求，唤醒云端 Runner。"""
    url = f"https://api.github.com/repos/{repo}/dispatches"
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ArkRecodeSync/1.0",
        "Content-Type": "application/json",
    }
    payload = {
        "event_type": "discord_announcement",
        "client_payload": {
            "message_id": announcement.get("id"),
            "timestamp": announcement.get("timestamp"),
            "content": announcement.get("content"),
        },
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 204:
                print(f"[OK] 成功唤醒 GitHub Actions! (Event: discord_announcement, MsgID: {announcement.get('id')})")
                return True
            else:
                print(f"[FAIL] 触发失败: HTTP {resp.status_code} - {resp.text}", file=sys.stderr)
    except Exception as exc:
        print(f"[FAIL] 请求 GitHub 异常: {exc}", file=sys.stderr)

    return False


def run_check(
    bot_token: str,
    channel_id: str,
    github_token: str,
    repo: str,
) -> bool:
    """单次检查与调度。"""
    announcement = get_latest_announcement(bot_token, channel_id)
    if not announcement:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 暂未检索到维护/招募公告")
        return False

    msg_id = announcement["id"]
    last_id = ""
    if STATE_FILE.is_file():
        last_id = STATE_FILE.read_text(encoding="utf-8").strip()

    if last_id == msg_id:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 公告已处理过 (ID: {msg_id})，跳过")
        return False

    parsed = parse_announcement_text(announcement["content"])
    print(f"[*] 发现新公告 (ID: {msg_id}): {parsed.get('title')}")

    success = dispatch_github_workflow(github_token, repo, announcement)
    if success:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(msg_id, encoding="utf-8")
        return True

    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord 公告常驻监听与 GitHub Actions 联动程序")
    parser.add_argument("--interval", type=int, default=60, help="轮询间隔秒数（默认 60s）")
    parser.add_argument("--once", action="store_true", help="仅执行一次检查后退出")
    args = parser.parse_args()

    bot_token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_CHANNEL_ID") or DEFAULT_CHANNEL_ID
    github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT")
    repo = os.environ.get("GITHUB_REPO") or DEFAULT_REPO

    if not bot_token:
        sys.exit("[错误] 缺少 DISCORD_BOT_TOKEN 环境变量")

    if not github_token:
        # 尝试通过 gh CLI 自动读取 token
        import subprocess
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
            github_token = res.stdout.strip()
        except Exception:
            pass

    if not github_token:
        sys.exit("[错误] 缺少 GITHUB_TOKEN 或 GITHUB_PAT 环境变量（需要 repo 调度权限）")

    print(f"[*] 启动 Discord 公告监听 (频道: #{channel_id}, 仓库: {repo})...")

    if args.once:
        run_check(bot_token, channel_id, github_token, repo)
        return

    while True:
        try:
            run_check(bot_token, channel_id, github_token, repo)
        except Exception as exc:
            print(f"[!] 轮询出错: {exc}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
