"""把 index.json 的关键数字回写进 README 的「当前状态」块。

只改 ``<!-- SYNC-STATS:START -->`` 与 ``<!-- SYNC-STATS:END -->`` 之间的内容；
标记不存在就把整段追加到文件末尾。内容没变就不写盘 —— workflow 的
``git status --porcelain`` 检查才不会因此多出空提交。

用法：
    python tools/update_readme.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "index.json"

START = "<!-- SYNC-STATS:START -->"
END = "<!-- SYNC-STATS:END -->"
NOTE = "<!-- 本块由 sync-avatars 工作流自动更新，请勿手改 -->"
REPO = "lemon-o/Ark-Recode-material"


def _block(count: int, updated: str) -> str:
    lines = [
        START,
        NOTE,
        f"- 头像总数：{count}",
        f"- 最近同步：{updated or '（未知）'}（UTC）",
        f"- 清单：`https://cdn.jsdelivr.net/gh/{REPO}@main/index.json`",
        END,
    ]
    return "\n".join(lines)


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

    text = README.read_text(encoding="utf-8") if README.is_file() else ""
    fresh = _block(count, updated)
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
    print(f"README 已更新：{count} 张，updated={updated or '未知'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
