"""Purge jsDelivr CDN cache for changed assets in a git commit or given paths.

Usage:
    python tools/purge_cdn.py --commit HEAD
    python tools/purge_cdn.py skills/Icon_Skill_H186_001.png index.json
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = "lemon-o/Ark-Recode-material"
BRANCH = "main"
PURGE_BASE = f"https://purge.jsdelivr.net/gh/{REPO}@{BRANCH}"

WATCHED_PREFIXES = (
    "skills/",
    "avatars/",
    "heads/",
    "banners/",
    "bonds/",
    "icons/",
    "equip/",
    "equipset/",
    "uiconz/",
    "bg/",
    "spine/",
    "data/",
    "index.json",
)


def get_changed_files_from_commit(commit: str = "HEAD") -> list[str]:
    """Get list of modified/added files in the given git commit."""
    try:
        output = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
            text=True,
        )
        files = [line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()]
        return [f for f in files if any(f.startswith(prefix) or f == prefix for prefix in WATCHED_PREFIXES)]
    except Exception as exc:
        print(f"[-] Failed to get changed files from git commit {commit}: {exc}", file=sys.stderr)
        return []


def purge_single_path(path: str, retries: int = 2) -> tuple[str, bool, str]:
    """Purge a single file path via jsDelivr Purge API."""
    rel = path.lstrip("/").replace("\\", "/")
    url = f"{PURGE_BASE}/{rel}"
    req = urllib.request.Request(url, headers={"User-Agent": "Ark-Recode-PurgeBot/1.0"})
    
    last_err = ""
    for attempt in range(1 + retries):
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status == 200:
                    return rel, True, "OK"
                last_err = f"HTTP {resp.status}"
        except urllib.error.HTTPError as err:
            last_err = f"HTTP {err.code}"
        except Exception as exc:
            last_err = str(exc)
        if attempt < retries:
            time.sleep(1.0)
            
    return rel, False, last_err


def purge_paths(paths: list[str], max_workers: int = 4) -> dict[str, bool]:
    """Purge multiple paths concurrently."""
    clean_paths = sorted(set(p.strip().replace("\\", "/").lstrip("/") for p in paths if p.strip()))
    if not clean_paths:
        print("[*] No paths to purge.")
        return {}

    print(f"[*] Starting CDN purge for {len(clean_paths)} file(s)...")
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(purge_single_path, p): p for p in clean_paths}
        for future in as_completed(futures):
            path, ok, detail = future.result()
            results[path] = ok
            status_tag = "[OK]" if ok else "[-]"
            print(f"  {status_tag} {path} -> {detail}")

    success_count = sum(1 for v in results.values() if v)
    print(f"[*] CDN purge completed: {success_count}/{len(clean_paths)} succeeded.")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Purge jsDelivr cache for Ark-Recode-material assets")
    parser.add_argument("--commit", default="", help="Git commit reference to extract changed files from")
    parser.add_argument("paths", nargs="*", help="Explicit file paths to purge")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers")
    args = parser.parse_args()

    targets: list[str] = list(args.paths)
    if args.commit:
        targets.extend(get_changed_files_from_commit(args.commit))

    # Always ensure index.json is included if any file was changed
    if targets and "index.json" not in targets:
        targets.append("index.json")

    purge_paths(targets, max_workers=args.workers)


if __name__ == "__main__":
    main()
