"""检查游戏服务器 Addressables Catalog 与官方维护公告，判断是否发生版本更新。

检测机制（双重兜底）：
1. 官方公告时段抓取（同 lucima-tools）：
   请求 EROLABS 官方公告 API，提取星陨计划最新的停机维护时段（如 "09/30 14:00 - 17:00 (UTC+8)"）；
   若发现新的维护公告且当前时间已过维护起始点，判定为有版本更新；
2. 游戏 Catalog 物理变动探针：
   请求游戏网关 QueryBulletinInfoResult 获取激活的 NewCatalogName，
   HEAD 请求 CDN 比对 ETag、Last-Modified 与文件大小。即使官方临时热更未发公告，亦能精准捕获；
3. 输出结果到 GITHUB_OUTPUT，供 GitHub Actions 决策是否执行提取流水线。

用法：
    python tools/check_version.py
    python tools/check_version.py --save
    python tools/check_version.py --force
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "backend" / "config.py"
STATE_PATH = ROOT / "data" / "catalog-info.json"
BOOTSTRAP_ROUTE = "GameServerDBSettingHandler.QueryBulletinInfoResult"

GAME_TZ = timezone(timedelta(hours=8))

ANNOUNCEMENT_URLS = [
    "https://www.erolabs.com/api/v2/announcement/news",
    "https://www.ero-labs.love/api/v2/announcement/news",
]

ANNOUNCEMENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def _read_config() -> dict[str, str]:
    wanted = {"GAME_ROUTER", "GAME_ORIGIN", "GAME_REFERER"}
    try:
        tree = ast.parse(CONFIG_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        sys.exit(f"读取 backend/config.py 失败: {exc}")
    found: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                if isinstance(node.value, ast.Constant):
                    found[target.id] = str(node.value.value)
    return found


def parse_maintenance_time(text: str, default_year: int) -> dict[str, str | float] | None:
    match = re.search(
        r"(?:(\d{4})[年/.-])?(\d{1,2})[月/.-](\d{1,2})[日号]?(?:\s*\([^\)]*\))?\s*"
        r"(\d{1,2}):(\d{2})\s*[-~至到]\s*(\d{1,2}):(\d{2})(?:\s*\(?UTC\+?8\)?)?",
        text or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    year = int(match.group(1)) if match.group(1) else default_year
    month = int(match.group(2))
    day = int(match.group(3))
    start_h, start_m = int(match.group(4)), int(match.group(5))
    end_h, end_m = int(match.group(6)), int(match.group(7))

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
            "raw": match.group(0).strip(),
        }
    except Exception:
        return None


def fetch_maintenance_notice(timeout: float = 12.0) -> dict | None:
    """抓取 EROLABS 官方星陨计划最新的维护公告。"""
    params = {
        "lang": "zh",
        "noticeType": -1,
        "nowPage": 1,
        "count": 20,
        "gameIdList": "32",
    }
    for url in ANNOUNCEMENT_URLS:
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url, params=params, headers=ANNOUNCEMENT_HEADERS)
                if resp.status_code != 200:
                    continue
                news = (resp.json() or {}).get("data", {}).get("news") or []
                now_dt = datetime.now(GAME_TZ)
                for item in news:
                    hid = item.get("hgameId")
                    gname = item.get("gameName") or ""
                    if str(hid) != "32" and "星" not in gname and "Ark" not in gname:
                        continue
                    title = item.get("name") or ""
                    intro = item.get("intro") or ""
                    combined = f"{title} {intro}"
                    if not any(k in combined for k in ("維護", "维护", "停機", "停机", "關機", "关机")):
                        continue
                    parsed = parse_maintenance_time(combined, now_dt.year)
                    if parsed:
                        return {
                            "id": item.get("newsId"),
                            "title": title,
                            "window": parsed["raw"],
                            "startTime": parsed["start"],
                            "endTime": parsed["end"],
                            "startTimestamp": parsed["startTimestamp"],
                            "endTimestamp": parsed["endTimestamp"],
                            "intro": intro[:200],
                        }
        except Exception:
            continue
    return None


def fetch_remote_catalog_info(timeout: float = 15.0) -> dict[str, str | int]:
    cfg = _read_config()
    router = cfg.get("GAME_ROUTER", "")
    if not router:
        sys.exit("backend/config.py 中缺少 GAME_ROUTER")

    body = json.dumps({"data": {}, "route": BOOTSTRAP_ROUTE}).encode("utf-8")
    with httpx.Client(timeout=timeout) as client:
        resp = client.request("PUT", router, content=body)
        resp.raise_for_status()
        info = (resp.json() or {}).get("Info", {})
        domain = str(info.get("PathDomain") or info.get("PathDomains") or "").rstrip("/")
        if not domain:
            sys.exit("路由响应中未包含 PathDomain")
        base = domain + str(info.get("PatchPosFix") or "")
        catalog_name = str(info.get("NewCatalogName") or "catalog")

        catalog_url = f"{base}/WebGL/{catalog_name}.json"
        head_resp = client.head(catalog_url)
        head_resp.raise_for_status()
        etag = str(head_resp.headers.get("ETag") or "").strip()
        last_modified = str(head_resp.headers.get("Last-Modified") or "").strip()
        raw_len = head_resp.headers.get("Content-Length")
        size = int(raw_len) if raw_len and raw_len.isdigit() else 0

    return {
        "catalog": catalog_name,
        "url": catalog_url,
        "etag": etag,
        "last_modified": last_modified,
        "size": size,
    }


def load_local_state() -> dict:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def save_local_state(catalog_info: dict, notice: dict | None = None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "catalog": catalog_info.get("catalog"),
        "url": catalog_info.get("url"),
        "etag": catalog_info.get("etag"),
        "last_modified": catalog_info.get("last_modified"),
        "size": catalog_info.get("size"),
        "last_notice_id": (notice or {}).get("id"),
        "last_notice_title": (notice or {}).get("title"),
        "maintenance_window": (notice or {}).get("window"),
        "maintenance_end": (notice or {}).get("endTime"),
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新版本状态基线 -> {STATE_PATH}")


def _set_github_output(name: str, value: str) -> None:
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        try:
            with open(out_file, "a", encoding="utf-8") as fh:
                fh.write(f"{name}={value}\n")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查游戏 Catalog 与官方维护公告以判定版本更新")
    parser.add_argument("--save", action="store_true", help="保存远端最新 Catalog 与公告状态为本地基线")
    parser.add_argument("--force", action="store_true", help="强制标记为有更新 (changed=true)")
    args = parser.parse_args(argv)

    print("正在抓取官方维护公告与游戏 Catalog 状态...")
    notice = fetch_maintenance_notice()
    remote = fetch_remote_catalog_info()
    local = load_local_state()

    now_ts = datetime.now(timezone.utc).timestamp()

    if notice:
        print(f"官方维护公告: [{notice.get('window')}] {notice.get('title')}")
    else:
        print("未抓取到近期维护公告（常规状态）")

    remote_catalog = remote.get("catalog")
    remote_etag = remote.get("etag")
    remote_size = remote.get("size")

    local_catalog = local.get("catalog")
    local_etag = local.get("etag")
    local_size = local.get("size")
    last_notice_id = local.get("last_notice_id")

    print(f"远端 Catalog: {remote_catalog} (ETag: {remote_etag}, 大小: {remote_size})")
    print(f"本地 基线:    {local_catalog} (ETag: {local_etag}, 大小: {local_size})")

    changed = False
    reasons = []

    if args.force:
        changed = True
        reasons.append("强制触发 (--force)")
    elif not local:
        changed = True
        reasons.append("本地无基线状态记录")
    else:
        # 1. 公告时段检查：若有新维护公告，且当前时间已达到维护时段开始
        if notice and notice.get("id"):
            notice_id = notice["id"]
            start_ts = notice.get("startTimestamp", 0)
            if notice_id != last_notice_id and now_ts >= start_ts:
                changed = True
                reasons.append(f"命中官方新维护时段: {notice.get('window')} ({notice.get('title')})")

        # 2. Catalog 物理变动检查（热更/补丁上线）：名称、ETag 或大小变动
        if remote_catalog != local_catalog:
            changed = True
            reasons.append(f"Catalog 名称变更: {local_catalog} -> {remote_catalog}")
        elif remote_etag and local_etag and remote_etag != local_etag:
            changed = True
            reasons.append(f"Catalog ETag 变更: {local_etag} -> {remote_etag}")
        elif remote_size and local_size and remote_size != local_size:
            changed = True
            reasons.append(f"Catalog 大小变更: {local_size} -> {remote_size}")

    if changed:
        print(f"[判定结果] 发现版本更新：{'; '.join(reasons)}")
    else:
        print("[判定结果] Catalog 与维护状态无更新，当前已是最新。")

    notice_title = (notice or {}).get("title") or ""
    # 提取干净的活动简短标题（去下括号等）
    clean_title = re.sub(r"^[0-9/.\-\s]+(?:维护|維護|公告)?[，,\s]*", "", notice_title)
    if not clean_title:
        clean_title = notice_title or str(remote_catalog)

    _set_github_output("changed", "true" if changed else "false")
    _set_github_output("catalog", str(remote_catalog))
    _set_github_output("catalog_url", str(remote.get("url")))
    _set_github_output("notice_title", clean_title)
    _set_github_output("maintenance_window", str((notice or {}).get("window") or ""))

    if args.save:
        save_local_state(remote, notice)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
