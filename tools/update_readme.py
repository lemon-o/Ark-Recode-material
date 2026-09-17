"""把 index.json 的关键数字回写进 README 顶部的「当前状态」块。

只改 ``<!-- SYNC-STATS:START -->`` 与 ``<!-- SYNC-STATS:END -->`` 之间的内容；
标记不存在就插到首行标题之后。**「最近扫描」每次运行都取当前时刻**，所以每轮
工作流必然产生一次 README 提交 —— GitHub 会在仓库 60 天无提交后自动禁用
scheduled workflow（要人工去 Actions 页重新启用），每天两次运行保证了永不触发。

用法：
    python tools/update_readme.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "index.json"

# 统一用东八区展示（用户在东八区，cron 也按北京时间跑）。
_TZ8 = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
INDEX = ROOT / "index.json"

START = "<!-- SYNC-STATS:START -->"
END = "<!-- SYNC-STATS:END -->"
NOTE = "<!-- 本块由 sync-avatars 工作流自动更新，请勿手改 -->"
REPO = "lemon-o/Ark-Recode-material"

# 各分区的展示顺序与中文名（与 build_index.py 的 SECTIONS 对齐）。
_LABELS = {
    "avatars": "头像",
    "heads": "头像框",
    "skills": "技能图标",
    "icons": "功能图标",
    "items": "道具图标",
    "equip": "装备图标",
    "equipset": "套装图标",
}


def _block(sections: dict[str, int], scanned_at: str) -> str:
    breakdown = " / ".join(
        f"{_LABELS.get(key, key)} {count}" for key, count in sections.items()
    )
    total = sum(sections.values())
    lines = [
        START,
        NOTE,
        f"- 素材总数：{total}（{breakdown}）",
        f"- 最近扫描：{scanned_at}（北京时间）",
        f"- 清单：`https://cdn.jsdelivr.net/gh/{REPO}@main/index.json`",
        END,
    ]
    return "\n".join(lines)


def _scan_time() -> str:
    """本次运行的时刻（东八区，秒级）。每轮都变 ⇒ 每轮都有一次 README 提交。"""
    return datetime.now(_TZ8).replace(microsecond=0).isoformat()


def main() -> int:
    payload: dict = {}
    if INDEX.is_file():
        try:
            loaded = json.loads(INDEX.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
        except (OSError, ValueError):
            payload = {}

    sections = {
        key: len(entries)
        for key, entries in payload.items()
        if isinstance(entries, dict)
    }
    fresh = _block(sections, _scan_time())

    text = README.read_text(encoding="utf-8") if README.is_file() else ""
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.S)
    if pattern.search(text):
        # 用 lambda 交回替换串：块里有反引号/反斜杠也不会被当成反向引用展开。
        new_text = pattern.sub(lambda _: fresh, text, count=1)
    elif text.lstrip().startswith("# "):
        # 还没有块：插到首行标题之后，保持「当前状态」在最顶部。
        head, _, rest = text.partition("\n")
        new_text = head + "\n\n" + fresh + "\n" + rest
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        new_text = text + "\n## 当前状态\n\n" + fresh + "\n"

    if new_text == text:
        print("README 无变化")
        return 0
    README.write_text(new_text, encoding="utf-8")
    print("README 已更新：共 " + str(sum(sections.values())) + " 张，扫描时刻已刷新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
