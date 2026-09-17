"""扫描各素材目录生成 index.json —— 客户端靠它判断自己缺哪些图。

每个分区（avatars / heads / skills / icons）各出一个键，键是文件 stem：
    avatars 键即角色 ID（H193），其余家族键是完整图标名（Icon_Head_B_H193）。
不直接让客户端列 GitHub 目录：那要走 REST API，未认证时每小时只有 60 次配额。
index.json 是纯静态文件，走 CDN，随便拉。

内容只由图片本身决定，所以没有新增时重复运行不会产生 diff。

用法：
    python tools/build_index.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.json"

# index.json 里的键 -> 仓库目录。加新家族在这里登记一行即可。
SECTIONS: tuple[tuple[str, str], ...] = (
    ("avatars", "avatars"),
    ("heads", "heads"),
    ("skills", "skills"),
    ("icons", "icons"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries(folder: Path) -> dict[str, dict[str, object]]:
    return {
        png.stem: {"file": png.name, "bytes": png.stat().st_size, "sha256": _sha256(png)}
        for png in sorted(folder.glob("*.png"))
    }


def main() -> int:
    payload: dict[str, object] = {}
    changed = False
    previous_all: dict = {}
    if INDEX.exists():
        try:
            previous_all = json.loads(INDEX.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            previous_all = {}
    for key, folder in SECTIONS:
        entries = _entries(ROOT / folder)
        payload[key] = entries
        if previous_all.get(key) != entries:
            changed = True
        print(f"{key}: {len(entries)} 张")

    total = sum(len(v) for k, v in payload.items() if isinstance(v, dict))
    if not changed:
        print(f"index.json 无变化（共 {total} 张）")
        return 0

    document: dict[str, object] = {
        # 时间戳统一东八区（与 README「最近扫描」、cron 的北京时间口径一致）。
        "updated": datetime.now(timezone(timedelta(hours=8)))
        .replace(microsecond=0)
        .isoformat(),
        "count": total,
    }
    document.update(payload)
    INDEX.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"index.json 已更新：共 {total} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
