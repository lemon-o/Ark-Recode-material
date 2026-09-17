"""把 index.json 的关键数字回写进 README 的「当前状态」块。

只改 ``<!-- SYNC-STATS:START -->`` 与 ``<!-- SYNC-STATS:END -->`` 之间的内容；
标记不存在就把整段追加到文件末尾。内容没变就不写盘 —— workflow 的
``git status --porcelain`` 检查才不会因此多出空提交。

**为什么有一行「最近活动」**：GitHub 会在仓库 60 天没有任何提交后自动禁用
scheduled workflow，之后必须人工去 Actions 页面重新启用——游戏几个月不出新
角色是完全正常的，那样 index.json 和状态块都不变，仓库就会真的静默 60 天。
所以 HEAD 提交超过 :data:`STALE_DAYS` 天时，把「最近活动」刷成今天，强制产生
一次保活提交；之后这一行取 HEAD 的日期，保持稳定、不再产生 diff。

用法：
    python tools/update_readme.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "index.json"

START = "<!-- SYNC-STATS:START -->"
END = "<!-- SYNC-STATS:END -->"
NOTE = "<!-- 本块由 sync-avatars 工作流自动更新，请勿手改 -->"
REPO = "lemon-o/Ark-Recode-material"

# GitHub 的禁用线是 60 天无提交；留一倍余量，空闲仓库每 30 天保活一次。
STALE_DAYS = 30.0

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _block(count: int, updated: str, activity: str) -> str:
    lines = [
        START,
        NOTE,
        f"- 头像总数：{count}",
        f"- 最近同步：{updated or '（未知）'}（UTC）",
        f"- 最近活动：{activity or '（未知）'}",
        f"- 清单：`https://cdn.jsdelivr.net/gh/{REPO}@main/index.json`",
        END,
    ]
    return "\n".join(lines)


def _head_date_utc() -> str:
    """HEAD 提交时间的 UTC 日期（``YYYY-MM-DD``）；git 不可用时返回空串。

    刻意只取日期不取时刻：值必须在「写进 README 之后」保持稳定，否则每次运行
    都会比上次提交晚几分钟，永远差一拍、每 12 小时空提交一次。日期粒度下，
    提交当天与随后 30 天内的取值都一样，天然幂等。
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    stamp = result.stdout.strip()
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def main() -> int:
    payload: dict = {}
    if INDEX.is_file():
        try:
            loaded = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError):
            payload = {}
    entries = payload.get("avatars")
    count = len(entries) if isinstance(entries, dict) else 0
    updated = str(payload.get("updated") or "")

    head = _head_date_utc()
    stale = False
    if head:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(head + "T00:00:00+00:00")
            stale = age.total_seconds() > STALE_DAYS * 86400
        except ValueError:
            stale = False
    # 保活：HEAD 太旧就把「最近活动」刷成今天，逼出一次提交；平时取 HEAD 日期，
    # 与已提交的值相同 ⇒ 无 diff ⇒ 无空提交。
    activity = (
        datetime.now(timezone.utc).strftime("%Y-%m-%d") if (stale or not head) else head
    )

    text = README.read_text(encoding="utf-8") if README.is_file() else ""
    fresh = _block(count, updated, activity)
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if pattern.search(text):
        # 用 lambda 交回替换串：块里有反引号/反斜杠也不会被当成反向引用展开。
        new_text = pattern.sub(lambda _: fresh, text, count=1)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        new_text = text + "\n## 当前状态\n\n" + fresh + "\n"

    if new_text == text:
        print("README 无变化")
        return 0
    README.write_text(new_text, encoding="utf-8")
    print(f"README 已更新：{count} 张，updated={updated or '未知'}，activity={activity or '未知'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
