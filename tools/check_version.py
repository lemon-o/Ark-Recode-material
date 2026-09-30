"""检查游戏服务器 Addressables Catalog 是否发生版本更新。

无须依赖 UnityPy，纯轻量 HTTP 请求（耗时 < 2 秒）：
1. 请求游戏路由端点 GameServerDBSettingHandler.QueryBulletinInfoResult 获取 NewCatalogName 与 PathDomain；
2. HEAD 请求该 Catalog JSON 文件获取 ETag 与 Last-Modified；
3. 比对本地 data/catalog-info.json：
   - 若 catalog 名称、ETag 或大小变动，判定为有版本更新，向 GITHUB_OUTPUT 输出 changed=true；
   - 若无变动，输出 changed=false；
4. 携带 --save 参数时更新本地 data/catalog-info.json 基线。

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
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "backend" / "config.py"
STATE_PATH = ROOT / "data" / "catalog-info.json"
BOOTSTRAP_ROUTE = "GameServerDBSettingHandler.QueryBulletinInfoResult"


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


def save_local_state(info: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "catalog": info.get("catalog"),
        "url": info.get("url"),
        "etag": info.get("etag"),
        "last_modified": info.get("last_modified"),
        "size": info.get("size"),
        "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已更新 Catalog 基线状态 -> {STATE_PATH}")


def _set_github_output(name: str, value: str) -> None:
    out_file = os.environ.get("GITHUB_OUTPUT")
    if out_file:
        try:
            with open(out_file, "a", encoding="utf-8") as fh:
                fh.write(f"{name}={value}\n")
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查游戏 Addressables Catalog 是否有更新")
    parser.add_argument("--save", action="store_true", help="保存远端最新 Catalog 状态为本地基线")
    parser.add_argument("--force", action="store_true", help="强制标记为有更新 (changed=true)")
    args = parser.parse_args(argv)

    print("正在向游戏服务器查询最新 Catalog 信息...")
    remote = fetch_remote_catalog_info()
    local = load_local_state()

    remote_catalog = remote.get("catalog")
    remote_etag = remote.get("etag")
    remote_size = remote.get("size")

    local_catalog = local.get("catalog")
    local_etag = local.get("etag")
    local_size = local.get("size")

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
    elif remote_catalog != local_catalog:
        changed = True
        reasons.append(f"Catalog 名称变更: {local_catalog} -> {remote_catalog}")
    elif remote_etag and local_etag and remote_etag != local_etag:
        changed = True
        reasons.append(f"ETag 变更: {local_etag} -> {remote_etag}")
    elif remote_size and local_size and remote_size != local_size:
        changed = True
        reasons.append(f"文件大小变更: {local_size} -> {remote_size}")

    if changed:
        print(f"[判定结果] 发现版本更新：{'; '.join(reasons)}")
    else:
        print("[判定结果] Catalog 无变化，当前已是最新状态。")

    _set_github_output("changed", "true" if changed else "false")
    _set_github_output("catalog", str(remote_catalog))
    _set_github_output("catalog_url", str(remote.get("url")))

    if args.save:
        save_local_state(remote)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
