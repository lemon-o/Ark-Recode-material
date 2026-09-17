"""Watch the Ark Re:Code wiki for newly debuted units and pull their art.

The wiki's `Events/Banners` page lists every summon banner, and each row carries
two things that matter here:

* the banner image name embeds the hero id -- ``BN_Summon_H193_ENG.png`` -> H193
* the Notes column says ``Unit Debut`` for the banner that introduced the unit

So "a new unit exists" is exactly "a ``Unit Debut`` row names a hero id we have
not recorded yet".  When that happens this script calls ``tools/fetch_assets.py``
for just those ids, which is surgical: it never re-writes the avatars that are
already on disk.

Only rows that have actually started (a parseable Start date) are recorded as
known.  An unreleased entry -- Start/End are ``?`` -- stays out of the known set
on purpose, so the day it gets a real date it is reported again.  For the same
reason a unit whose art the CDN has not published yet is left unknown and retried
on the next run; the on-disk PNG is the source of truth for "we have this one".

Usage::

    python tools/watch_new_units.py                  # scan, then fetch new avatars
    python tools/watch_new_units.py --dry-run        # report only, touch nothing
    python tools/watch_new_units.py --no-fetch       # record state, skip the fetch
    python tools/watch_new_units.py --include-existing   # treat the baseline as new
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = ROOT / "build" / "unit-debut-state.json"
DEFAULT_TARGET = ROOT / "assets"
FETCH_TOOL = ROOT / "tools" / "fetch_assets.py"

WIKI_HOST = "https://arkrecodewiki.miraheze.org"
WIKI_PAGE = "Events/Banners"
# `w/index.php?action=raw` serves wikitext straight; api.php is the fallback.
RAW_URL = WIKI_HOST + "/w/index.php"
API_URL = WIKI_HOST + "/w/api.php"

# Miraheze fronts the HTML views with a "Checking your connection..." interstitial,
# and that page is served with HTTP 200.  A browser UA avoids it for raw/API calls;
# the body check below catches the case where it slips through anyway.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
INTERSTITIAL_MARKER = "Checking your connection"

# `{{:Events/Banners/3}}` -- transcluded subpages.  Reading them from the page
# means a new "Year 4" section needs no change here.
TRANSCLUSION = re.compile(r"\{\{:([^}|]+)\}\}")
ROW_SPLIT = re.compile(r"^\s*\|-.*$", re.M)
BANNER_ID = re.compile(r"BN[_ ]Summon_([A-Z]\d{3})[_ ]ENG")
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
INSIDE_ID = re.compile(r"[AT]\d{4}")


class WikiError(RuntimeError):
    pass


def fetch_wikitext(client: httpx.Client, title: str) -> str:
    """Return the raw wikitext of `title`, retrying and falling back to the API."""
    params = {"title": title, "action": "raw"}
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.get(RAW_URL, params=params)
            response.raise_for_status()
            text = response.text
            if INTERSTITIAL_MARKER not in text and ("{|" in text or "{{" in text or text.strip()):
                return text
            last = WikiError("server returned an interstitial instead of wikitext")
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(attempt)
    # API fallback: same content, different door.
    try:
        response = client.get(
            API_URL,
            params={
                "action": "parse",
                "page": title,
                "prop": "wikitext",
                "format": "json",
                "formatversion": "2",
            },
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["parse"]["wikitext"])
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise WikiError(f"cannot read {title!r}: {last or exc}") from exc


def discover_pages(main_text: str) -> list[str]:
    """The main page plus every subpage it transcludes, de-duplicated."""
    pages = [WIKI_PAGE]
    for title in TRANSCLUSION.findall(main_text):
        title = title.strip()
        if title and title not in pages:
            pages.append(title)
    return pages


def parse_debuts(text: str, page: str) -> list[dict[str, object]]:
    """Pull the `Unit Debut` rows out of one page's wikitext."""
    parts = ROW_SPLIT.split(text)
    found: list[dict[str, object]] = []
    for part in parts[1:]:  # parts[0] is the header block
        if "Unit Debut" not in part:
            continue
        banner = BANNER_ID.search(part)
        if not banner:
            continue
        dates = DATE.findall(part)
        unit = ""
        for target, label in WIKILINK.findall(part):
            if target.startswith(("File:", "Category:")):
                continue
            text_label = (label or target).strip()
            if INSIDE_ID.fullmatch(text_label):  # bond / internal id, not a name
                continue
            unit = text_label
            break
        found.append(
            {
                "id": banner.group(1),
                "unit": unit,
                "start": dates[0] if dates else None,
                "end": dates[1] if len(dates) > 1 else None,
                "page": page,
            }
        )
    return found


def dedupe(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """One record per hero id; the earliest start wins.

    The same unit is listed on both the global pages and the Nutaku pages, with
    per-server dates (Shien H166 is 2026-03-18 globally and 2025-05-26 on Nutaku).
    Earliest is deliberate: the watcher must not miss a debut because another
    server's table happened to be read first.  Nutaku is a strict subset of the
    global ids, so this cannot invent a unit the CDN has never heard of.
    """
    best: dict[str, dict[str, object]] = {}
    for record in records:
        hero_id = str(record["id"])
        current = best.get(hero_id)
        if current is None:
            best[hero_id] = record
            continue
        old, new = current["start"], record["start"]
        if (old is None and new is not None) or (old and new and str(new) < str(old)):
            best[hero_id] = record
    return best


def short_page(title: str) -> str:
    """`Events/Banners/3` -> `Year 3`, `Events/Banners` -> `unreleased`."""
    if title == WIKI_PAGE:
        return "unreleased"
    tail = title.rsplit("/", 1)[-1]
    return f"Year {tail}" if tail.isdigit() else tail


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(path: Path, known: set[str], scanned_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_scan": scanned_at,
        "known_ids": sorted(known),
    }
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def run_fetch(ids: list[str], target: Path, extra: list[str]) -> int:
    command = [
        sys.executable,
        str(FETCH_TOOL),
        "--only",
        "avatars",
        "--ids",
        ",".join(ids),
        "--target",
        str(target),
        *extra,
    ]
    print("  $ " + " ".join(command[1:]))
    return subprocess.call(command, cwd=str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="scan state file")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="asset output directory")
    parser.add_argument("--dry-run", action="store_true", help="report only; fetch nothing, write no state")
    parser.add_argument("--no-fetch", action="store_true", help="record state but do not fetch assets")
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="treat units already in the state as new (first-run backfill)",
    )
    parser.add_argument(
        "--since",
        default="",
        help="only report debuts with Start on/after this date, e.g. 2026-09-01",
    )
    parser.add_argument("--proxy", default="", help="proxy URL; default follows HTTP(S)_PROXY")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args()

    transport = {"proxy": args.proxy} if args.proxy else {}
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"}

    try:
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True, **transport) as client:
            main_text = fetch_wikitext(client, WIKI_PAGE)
            pages = discover_pages(main_text)
            texts = {pages[0]: main_text}
            for title in pages[1:]:
                texts[title] = fetch_wikitext(client, title)
    except WikiError as exc:
        parser.error(str(exc))

    records: list[dict[str, object]] = []
    for title, text in texts.items():
        records.extend(parse_debuts(text, title))
    debuts = dedupe(records)

    state = load_state(args.state)
    previous_scan = state.get("last_scan")
    known = set(str(item) for item in state.get("known_ids", []) or [])
    first_run = not state

    released = {k: v for k, v in debuts.items() if v["start"]}
    unreleased = {k: v for k, v in debuts.items() if not v["start"]}

    if args.include_existing:
        fresh = dict(released)
    else:
        fresh = {k: v for k, v in released.items() if k not in known}
    if args.since:
        fresh = {
            k: v for k, v in fresh.items()
            if v["start"] and str(v["start"]) >= args.since
        }

    # A debut whose avatar is already on disk still counts as "had it" -- this is
    # what keeps `--include-existing` from re-fetching 96 known-good files.
    on_disk = (
        {path.stem for path in (args.target / "avatars").glob("*.png")}
        if (args.target / "avatars").is_dir()
        else set()
    )
    have = {k for k in fresh if k in on_disk}

    report = {
        "pages": list(texts),
        "last_scan": previous_scan,
        "scanned_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "first_run": first_run,
        "debut_total": len(debuts),
        "released_total": len(released),
        "unreleased": {k: v["unit"] for k, v in sorted(unreleased.items())},
        "new": {k: v["unit"] for k, v in sorted(fresh.items())},
        "already_on_disk": sorted(have),
        "to_fetch": sorted(set(fresh) - have),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"wiki pages: {len(texts)} -> {', '.join(texts)}")
        print(f"Unit Debut rows: {len(debuts)} unique units ({len(released)} released, {len(unreleased)} not yet)")
        if previous_scan:
            print(f"last scan: {previous_scan}")
        else:
            print("last scan: (none -- this run establishes the baseline)")
        print()
        if fresh:
            print(f"new since last scan: {len(fresh)}")
            for hero_id, record in sorted(fresh.items()):
                mark = "have" if hero_id in have else "FETCH"
                print(
                    f"  {mark:5s} {hero_id}  {str(record['unit']):32s} "
                    f"{record['start']} -> {record['end']}  [{short_page(str(record['page']))}]"
                )
        else:
            print("new since last scan: 0")
        if unreleased:
            print()
            print(f"listed but not released yet ({len(unreleased)}): "
                  + ", ".join(f"{k} {v['unit']}" for k, v in sorted(unreleased.items())))
            print("  (kept out of the known set, so they are reported again once dated)")
        print()

    if args.dry_run:
        print("dry run: nothing fetched, state not written")
        return 0

    to_fetch = report["to_fetch"]
    fetch_failed = False
    if to_fetch and not args.no_fetch:
        print(f"fetching {len(to_fetch)} new avatar(s) via tools/fetch_assets.py")
        fetch_failed = run_fetch(to_fetch, args.target, ["--proxy", args.proxy] if args.proxy else []) != 0
        # Only a unit whose PNG now exists is safe to mark as known; an id the CDN
        # has not published yet must be retried next run.
        on_disk = {path.stem for path in (args.target / "avatars").glob("*.png")}
        still_missing = sorted(i for i in to_fetch if i not in on_disk)
        if still_missing:
            print(f"  not on the CDN yet, will retry next run: {', '.join(still_missing)}")
    elif to_fetch:
        print(f"--no-fetch: {len(to_fetch)} new avatar(s) left un-fetched")

    if args.include_existing:
        known = known | set(released)
    else:
        known = known | {i for i in to_fetch if i in on_disk} | have
    save_state(args.state, known, report["scanned_at"])
    print(f"state written: {args.state} ({len(known)} known units)")
    return 1 if fetch_failed else 0


if __name__ == "__main__":
    sys.exit(main())
