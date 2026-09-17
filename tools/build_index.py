"""扫描 avatars/ 生成 index.json —— 客户端靠它判断自己缺哪些头像。

不直接让客户端列 GitHub 目录：那要走 REST API，未认证时每小时只有 60 次配额。
index.json 是纯静态文件，走 CDN，随便拉。

内容只由图片本身决定，所以没有新增时重复运行不会产生 diff。

用法：
    python tools/build_index.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AVATARS = ROOT / "avatars"
INDEX = ROOT / "index.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entries() -> dict[str, dict[str, object]]:
    return {
        png.stem: {"file": png.name, "bytes": png.stat().st_size, "sha256": _sha256(png)}
        for png in sorted(AVATARS.glob("*.png"))
    }


def main() -> int:
    entries = _entries()

    previous: object = None
    if INDEX.exists():
        try:
            previous = (json.loads(INDEX.read_text(encoding="utf-8")) or {}).get("avatars")
        except (OSError, ValueError):
            previous = None
    if previous == entries:
        print(f"index.json 无变化（{len(entries)} 个头像）")
        return 0

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(entries),
        "avatars": entries,
    }
    INDEX.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"index.json 已更新：{len(entries)} 个头像")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
