import os, sys
# Ensure script runs from the root directory
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
GOOGLE_INDEX_DIR = ROOT_DIR
if os.getcwd() != ROOT_DIR:
    os.chdir(ROOT_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

#!/usr/bin/env python3
"""
smart_indexer.py — Google Indexing API · Smart State-Tracked Submitter
=======================================================================
Author  : Ramesh Das
Website : https://www.rameshdas.dev

RULES:
  • A URL is NEVER re-submitted once it has status="submitted",
    unless you explicitly pass --reset or --force.
  • Daily quota (200/day) resets automatically at midnight.
  • Every run appends a structured entry to indexing_runs.log.
  • All state lives in indexing_state_v3.json (human-readable).

USAGE:
  python smart_indexer.py               # run the indexer
  python smart_indexer.py --report      # show full report, no API calls
  python smart_indexer.py --dry-run     # preview what WOULD be submitted
  python smart_indexer.py --reset       # reset ALL urls back to "pending"
  python smart_indexer.py --reset-url <url>  # reset one specific url
  python smart_indexer.py --force       # re-submit even already-submitted urls
  python smart_indexer.py --limit 50    # override daily submit cap this run
"""

import os
import sys
import json
import time
import argparse
import glob as glob_mod
import xml.etree.ElementTree as ET
import re
from datetime import datetime, timezone
from collections import defaultdict
import urllib.request

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BASE_URL        = "https://www.rameshdas.dev"
_key_name       = "google-service-account.json"
JSON_KEY_FILE   = os.path.join(GOOGLE_INDEX_DIR, _key_name) if os.path.exists(os.path.join(GOOGLE_INDEX_DIR, _key_name)) else _key_name
SCOPES          = ["https://www.googleapis.com/auth/indexing"]
ENDPOINT        = "https://indexing.googleapis.com/v3/urlNotifications:publish"


def get_service_account_email() -> str:
    """Read the client_email from the service account key file (never hardcode)."""
    try:
        with open(JSON_KEY_FILE, "r") as f:
            return json.load(f).get("client_email", "<unknown — check key file>")
    except Exception:
        return "<unknown — key file not found>"


SERVICE_ACCOUNT_EMAIL = get_service_account_email()

STATE_FILE      = os.path.join(GOOGLE_INDEX_DIR, "indexing_state_v3.json")
LOG_FILE        = os.path.join(GOOGLE_INDEX_DIR, "indexing_runs.log")
HISTORY_FILE    = os.path.join(GOOGLE_INDEX_DIR, "indexing_runs_history.jsonl")

DAILY_LIMIT     = 200
SCHEMA_VERSION  = 3
DELAY_BETWEEN   = 0.5   # seconds between API calls

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORIZATION
# ─────────────────────────────────────────────────────────────────────────────
COUNTRY_CITY_MAP = {
    "usa":           ["san-francisco", "new-york", "seattle", "austin", "boston"],
    "uk":            ["london", "manchester", "cambridge"],
    "canada":        ["toronto", "vancouver", "montreal"],
    "australia":     ["sydney", "melbourne", "brisbane"],
    "germany":       ["berlin", "munich", "hamburg", "frankfurt", "stuttgart"],
    "japan":         ["tokyo", "osaka", "yokohama", "nagoya", "fukuoka",
                      "kyoto", "sapporo", "kobe", "sendai", "hiroshima"],
    "singapore":     ["singapore", "marina-bay", "one-north", "jurong-east", "tampines"],
    "saudi-arabia":  ["riyadh", "jeddah", "dammam", "khobar", "dhahran",
                      "mecca", "medina", "tabuk", "abha", "yanbu"],
    "uae":           ["dubai", "abu-dhabi", "sharjah", "ajman", "ras-al-khaimah",
                      "fujairah", "umm-al-quwain", "al-ain", "khor-fakkan",
                      "dibba-al-fujairah"],
    "qatar":         ["doha", "al-rayyan", "lusail", "al-wakrah", "umm-salal",
                      "al-khor", "al-daayen", "mesaieed", "dukhan",
                      "madinat-ash-shamal"],
    "middle-east":   [],
}

HUB_PAGES = {
    "hire-developers-usa.html",
    "hire-developers-uk.html",
    "hire-developers-canada.html",
    "hire-developers-australia.html",
    "hire-developers-germany.html",
    "hire-developers-japan.html",
    "hire-developers-singapore.html",
    "hire-developers-saudi-arabia.html",
    "hire-developers-middle-east.html",
}

# Priority: lower = submitted first
PRIORITY_MAP = {"core": 1, "hub": 2, "city_page": 3, "unknown": 9}


def categorize(filename: str) -> dict:
    """Return category, country, city, and priority for a file."""
    if filename in HUB_PAGES:
        country_slug = filename.replace("hire-developers-", "").replace(".html", "")
        return {"category": "hub", "country": country_slug, "city": None,
                "priority": PRIORITY_MAP["hub"]}
    for country, cities in COUNTRY_CITY_MAP.items():
        for city in cities:
            if filename.endswith(f"-{city}.html"):
                return {"category": "city_page", "country": country, "city": city,
                        "priority": PRIORITY_MAP["city_page"]}
    return {"category": "core", "country": None, "city": None,
            "priority": PRIORITY_MAP["core"]}


# ─────────────────────────────────────────────────────────────────────────────
# URL DISCOVERY  (HTML files + sitemaps)
# ─────────────────────────────────────────────────────────────────────────────
# Extensions that the Google Indexing API will NEVER accept (always 403)
SKIP_EXTENSIONS = {".xml", ".txt", ".pdf", ".json", ".csv", ".png", ".jpg",
                   ".jpeg", ".gif", ".svg", ".ico", ".webp", ".mp4", ".mp3",
                   ".zip", ".gz", ".woff", ".woff2", ".ttf", ".eot"}


def _is_indexable(url: str) -> bool:
    """Return True only for plain HTML URLs the Indexing API will accept."""
    path = url.replace(BASE_URL, "").split("?")[0].split("#")[0]
    if path == "" or path == "/":
        return True   # root domain — always valid
    ext = os.path.splitext(path)[1].lower()
    if ext in SKIP_EXTENSIONS:
        return False
    # Accept .html and no-extension paths
    return ext in {"", ".html", ".htm"}


def discover_urls_from_file(filepath: str) -> tuple:
    """Read URLs directly from a text file or URL, extracting URLs and tracking headers.
       Returns (found_urls, url_groups).
    """
    found = {}
    groups = {}
    current_header = "Uncategorized"
    try:
        if filepath.startswith("http://") or filepath.startswith("https://"):
            req = urllib.request.Request(filepath, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response:
                lines = response.read().decode('utf-8').splitlines()
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        
        for line in lines:
            line = line.strip()
            if line.startswith('#'):
                if '===' in line:
                    header = line.replace('#', '').replace('=', '').strip()
                    if header:
                        current_header = header
                continue
            if not line:
                continue
            match = re.search(r'(https?://[^\s<>"\'\]\[]+)', line)
            if match:
                url = match.group(1)
                if url.startswith(BASE_URL) and _is_indexable(url):
                    fname = url.replace(BASE_URL + "/", "") or "index.html"
                    found[url] = fname
                    groups[url] = current_header
    except Exception as e:
        print(f"  [error] Could not read URL source {filepath}: {e}")
    return found, groups


def discover_urls(directory: str = ".") -> dict:
    """
    Returns {url: filename} for every HTML URL that is safe to submit.
    Sources:
      1. All *.html files in the local directory
      2. <loc> tags inside all *.xml sitemaps (HTML-only — skips sitemaps themselves)
    """
    found = {}

    # 1 — HTML files on disk
    for fname in os.listdir(directory):
        if fname.endswith(".html") or fname.endswith(".htm"):
            url = f"{BASE_URL}/" if fname in ("index.html", "index.htm") else f"{BASE_URL}/{fname}"
            found[url] = fname

    # 2 — <loc> tags from sitemap XML files (extract URLs, don't submit the XMLs)
    xml_files = glob_mod.glob(os.path.join(directory, "*.xml"))
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    for xml_path in xml_files:
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            for loc in root.iter(f"{{{ns}}}loc"):
                url = loc.text.strip() if loc.text else ""
                if url and url.startswith(BASE_URL) and _is_indexable(url):
                    fname = url.replace(BASE_URL + "/", "") or "index.html"
                    if url not in found:
                        found[url] = fname
        except ET.ParseError:
            pass  # not a valid sitemap XML, skip silently

    return found


# ─────────────────────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state() -> dict:
    """Load state from disk, migrating from older formats if needed."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        # Reset daily quota counter if it's a new UTC day
        if state.get("quota", {}).get("date") != today_str():
            state["quota"]["date"]      = today_str()
            state["quota"]["used_today"] = 0
        return state

    # ── Bootstrap fresh state (possibly migrating from v2) ──────────────────
    state = {
        "schema_version": SCHEMA_VERSION,
        "created_at":     now_iso(),
        "last_updated":   now_iso(),
        "quota": {
            "date":       today_str(),
            "used_today": 0,
            "daily_limit": DAILY_LIMIT,
        },
        "urls": {}
    }

    # Migrate from indexing_progress_v2.json if it exists
    v2_path = "indexing_progress_v2.json"
    if os.path.exists(v2_path):
        try:
            with open(v2_path, "r") as f:
                v2 = json.load(f)
        except json.JSONDecodeError:
            v2 = {}
            print(f"  [warn] {v2_path} is empty or corrupt. Skipping migration.")

        # Carry over today's quota usage
        if v2.get("last_run_date") == today_str():
            state["quota"]["used_today"] = v2.get("daily_count", 0)

        for url, info in v2.get("pages", {}).items():
            fname = url.replace(BASE_URL + "/", "") or "index.html"
            cat_data = categorize(fname)
            state["urls"][url] = {
                "status":          info.get("status", "pending"),
                "category":        info.get("category") or cat_data["category"],
                "country":         info.get("country") or cat_data["country"],
                "city":            info.get("city")    or cat_data["city"],
                "priority":        PRIORITY_MAP.get(
                                       info.get("category", "unknown"), 9),
                "first_seen":      info.get("last_submitted") or now_iso(),
                "submitted_at":    info.get("last_submitted"),
                "submit_count":    1 if info.get("status") == "submitted" else 0,
                "attempts":        1 if info.get("status") == "submitted" else 0,
                "last_attempt_at": info.get("last_submitted"),
                "last_http_code":  200 if info.get("status") == "submitted" else None,
                "last_error":      None,
            }
        print(f"  [migrate] Imported {len(state['urls'])} URLs from indexing_progress_v2.json")

    return state


def save_state(state: dict):
    state["last_updated"] = now_iso()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def sync_discovered(state: dict, discovered: dict):
    """
    Add any newly discovered HTML URLs to the state (never remove existing ones).
    Also permanently marks any existing non-HTML URLs as 'skipped'.
    """
    added   = 0
    skipped = 0

    # Mark existing non-HTML entries as permanently skipped
    for url, info in state["urls"].items():
        if not _is_indexable(url) and info["status"] != "skipped":
            info["status"]     = "skipped"
            info["last_error"] = "Non-HTML URL — Indexing API only accepts HTML pages"
            skipped += 1

    # Add new HTML URLs
    for url, fname in discovered.items():
        if url not in state["urls"]:
            cat = categorize(fname)
            state["urls"][url] = {
                "status":          "pending",
                "category":        cat["category"],
                "country":         cat["country"],
                "city":            cat["city"],
                "priority":        cat["priority"],
                "first_seen":      now_iso(),
                "submitted_at":    None,
                "submit_count":    0,
                "attempts":        0,
                "last_attempt_at": None,
                "last_http_code":  None,
                "last_error":      None,
            }
            added += 1

    return added, skipped


# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────
def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def _label(info: dict) -> str:
    parts = [info["category"].upper()]
    if info.get("country"):
        parts.append(info["country"].upper())
    if info.get("city"):
        parts.append(info["city"].title())
    return "[" + "][".join(parts) + "]"

def log(msg: str):
    """Append a line to the log file AND print it."""
    line = f"[{_ts()}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def log_run_history(run_summary: dict):
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(run_summary) + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def bar(done: int, total: int, width: int = 28) -> str:
    if total == 0:
        return "─" * width
    filled = int(width * done / total)
    return "█" * filled + "░" * (width - filled)

def print_report(state: dict, target_urls: list = None, custom_groups: dict = None):
    urls   = state["urls"]
    
    if target_urls is not None:
        urls = {k: v for k, v in urls.items() if k in target_urls}
        
    quota  = state["quota"]
    total  = len(urls)

    by_status   = defaultdict(int)
    by_category = defaultdict(lambda: defaultdict(int))
    by_country  = defaultdict(lambda: defaultdict(int))
    never_tried = []
    failed_list = []

    for url, info in urls.items():
        st  = info["status"]
        if custom_groups and url in custom_groups:
            cat = custom_groups[url]
        else:
            cat = info.get("category", "unknown").upper()
        ctr = info.get("country") or "—"

        by_status[st] += 1
        # Exclude skipped from category/country breakdown (they're noise)
        if st != "skipped":
            by_category[cat][st] += 1
            by_category[cat]["total"] += 1
            if ctr != "—":
                by_country[ctr][st] += 1
                by_country[ctr]["total"] += 1

        if info.get("attempts", 0) == 0 and st not in ("skipped",):
            never_tried.append(url)
        if st in ("failed", "failed_403"):
            failed_list.append((url, info.get("last_error", ""), info.get("last_http_code")))

    submitted  = by_status.get("submitted", 0)
    pending    = by_status.get("pending", 0)
    failed     = by_status.get("failed", 0)
    failed_403 = by_status.get("failed_403", 0)
    skipped    = by_status.get("skipped", 0)
    indexable  = total - skipped   # URLs the API will actually accept

    W = 60
    print("\n" + "═" * W)
    print("  📊  GOOGLE INDEXING — STATUS REPORT")
    print("═" * W)
    print(f"  Generated : {_ts()}")
    print(f"  State file: {STATE_FILE}")
    print("─" * W)
    print(f"  {'Total URLs tracked':<28} {total:>6}")
    print(f"  {'⏭  Skipped (non-HTML)':<28} {skipped:>6}  (XML/TXT — never submitted)")
    print(f"  {'🔢 Indexable HTML pages':<28} {indexable:>6}")
    print(f"  {'✅ Submitted':<28} {submitted:>6}  {bar(submitted, indexable)}")
    print(f"  {'⏳ Pending (never sent)':<28} {pending:>6}  {bar(pending, indexable)}")
    print(f"  {'🔒 403 Forbidden (retry last)':<28} {failed_403:>6}  {bar(failed_403, indexable)}")
    print(f"  {'❌ Failed (other error)':<28} {failed:>6}  {bar(failed, indexable)}")
    print("─" * W)
    print(f"  {'📅 Quota date':<28} {quota['date']}")
    print(f"  {'📈 Used today':<28} {quota['used_today']:>3}/{quota['daily_limit']}  "
          f"{bar(quota['used_today'], quota['daily_limit'])}")
    print(f"  {'🟢 Remaining today':<28} {quota['daily_limit'] - quota['used_today']:>6}")

    print("\n" + "─" * W)
    print("  📁  BY CATEGORY (FROM FILE)" if custom_groups else "  📁  BY CATEGORY")
    print("─" * W)
    cats_to_print = sorted(by_category.keys()) if custom_groups else ("CORE", "HUB", "CITY_PAGE", "UNKNOWN")
    for cat in cats_to_print:
        data = by_category.get(cat)
        if not data:
            continue
        t   = data["total"]
        sub = data.get("submitted", 0)
        pct = sub / t * 100 if t else 0
        cat_str = str(cat)
        cat_str = (cat_str[:25] + "..") if len(cat_str) > 27 else cat_str
        width = 28 if custom_groups else 14
        print(f"  {cat_str:<{width}}  {sub:>4}/{t:<5}  {bar(sub, t, 20)}  {pct:5.1f}%")

    if by_country:
        print("\n" + "─" * W)
        print("  🌍  BY COUNTRY")
        print("─" * W)
        for ctr in sorted(by_country):
            data = by_country[ctr]
            t    = data["total"]
            sub  = data.get("submitted", 0)
            pct  = sub / t * 100 if t else 0
            name = ctr.replace("-", " ").title()
            print(f"  {name:<18}  {sub:>4}/{t:<5}  {bar(sub, t, 20)}  {pct:5.1f}%")

    if failed_list:
        print("\n" + "─" * W)
        print("  ❌  FAILED URLS")
        print("─" * W)
        for url, err, code in failed_list[:20]:
            short = url.replace(BASE_URL, "")
            print(f"  HTTP {code or '???'}  {short}")
            if err:
                print(f"           └─ {err[:80]}")

    print("\n" + "─" * W)
    print(f"  📝  Run history  : {HISTORY_FILE}")
    print(f"  📋  Full log     : {LOG_FILE}")
    print("═" * W + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# RESET HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def reset_all(state: dict):
    """Reset all non-skipped URLs back to pending."""
    count = 0
    for info in state["urls"].values():
        if info["status"] in ("skipped",):
            continue   # keep skipped as-is — they're permanently excluded
        if info["status"] != "pending":
            info["status"]          = "pending"
            info["submitted_at"]    = None
            info["last_attempt_at"] = None
            info["last_http_code"]  = None
            info["last_error"]      = None
            count += 1
    return count


def reset_url(state: dict, url: str) -> bool:
    if url not in state["urls"]:
        return False
    info = state["urls"][url]
    info["status"]          = "pending"
    info["submitted_at"]    = None
    info["last_attempt_at"] = None
    info["last_http_code"]  = None
    info["last_error"]      = None
    return True


def reset_failed(state: dict) -> int:
    """Reset all failed / failed_403 URLs back to pending so they can be retried."""
    count = 0
    for info in state["urls"].values():
        if info["status"] in ("failed", "failed_403"):
            info["status"]          = "pending"
            info["last_attempt_at"] = None
            info["last_http_code"]  = None
            info["last_error"]      = None
            count += 1
    return count


def preflight_check(url: str, timeout: int = 5) -> tuple:
    """
    HEAD-check the URL to confirm it is live before wasting an API call.
    Returns (is_live: bool, http_code: int).
    """
    import urllib.request
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": "SmartIndexer/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.status
    except Exception as e:
        code = getattr(e, "code", 0)
        return False, code


# ─────────────────────────────────────────────────────────────────────────────
# INDEXING RUN
# ─────────────────────────────────────────────────────────────────────────────
def _api_call(session, url: str) -> tuple:
    """
    POST a single URL to the Indexing API.
    Returns (http_code: int, error_message: str | None, raw_body: str).
    """
    try:
        response  = session.post(ENDPOINT, json={"url": url, "type": "URL_UPDATED"})
        code      = response.status_code
        raw       = response.text
        if code == 200:
            return code, None, raw
        # Extract human-readable error from Google JSON response
        try:
            body = response.json()
            msg  = body.get("error", {}).get("message", raw[:200])
        except Exception:
            msg = raw[:200]
        return code, msg, raw
    except Exception as e:
        return 0, str(e), ""


def _sanity_check(session, state: dict) -> tuple:
    """
    Verify the service account has Owner access by submitting the root URL.
    Always tests the root URL directly — does NOT depend on state contents.
    Returns (ok: bool, http_code: int, error_msg: str).
    """
    # Always test the root domain — simplest URL the API should accept
    test_url = f"{BASE_URL}/"
    code, err, _ = _api_call(session, test_url)
    log(f"SANITY   | Tested {test_url} → HTTP {code}")
    if code in (200, 429):
        return True, code, ""
    return False, code, err or ""


def run_indexing(state: dict, dry_run: bool = False, force: bool = False,
                 limit_override: int = None, preflight: bool = False, target_urls: list = None):
    quota = state["quota"]
    urls  = state["urls"]

    used_today   = quota["used_today"]
    daily_limit  = quota["daily_limit"]
    available    = (limit_override or daily_limit) - used_today

    if available <= 0:
        log(f"QUOTA    | 0 slots left today ({used_today}/{daily_limit}). Run again tomorrow.")
        return

    # ── Build submit queue ────────────────────────────────────────────────────
    # Statuses eligible for submission:
    #   pending     → never tried
    #   failed      → previous non-403 error, worth retrying
    #   failed_403  → previous 403, put at END of queue (lower priority)
    #   submitted   → skip unless --force
    #   skipped     → permanent skip (non-HTML)
    queue_main   = []   # pending + failed
    queue_403    = []   # failed_403 (tried after main)

    for url, info in urls.items():
        if target_urls is not None and url not in target_urls:
            continue
        st = info["status"]
        if st == "skipped":
            continue
        if not _is_indexable(url):
            info["status"]     = "skipped"
            info["last_error"] = "Non-HTML URL — auto-skipped"
            continue
        if not force and st == "submitted":
            continue
        if st == "failed_403":
            queue_403.append((info["priority"], url))
        else:
            queue_main.append((info["priority"], url))

    queue_main.sort(key=lambda x: x[0])
    queue_403.sort(key=lambda x: x[0])

    # Pending / failed first, then retry 403s
    full_queue   = [u for _, u in queue_main] + [u for _, u in queue_403]
    total_queued = len(full_queue)
    batch        = full_queue[:available]

    # ── Run header ────────────────────────────────────────────────────────────
    mode_tag = "[DRY-RUN] " if dry_run else ""
    log(f"{'─'*72}")
    log(f"RUN START  {mode_tag}| total_tracked={len(urls)} queued={total_queued} "
        f"(main={len(queue_main)} retry-403={len(queue_403)}) "
        f"batch={len(batch)} quota={used_today}/{daily_limit}")

    if not batch:
        log("DONE     | Nothing left to submit. All URLs are submitted or skipped.")
        log("─" * 72)
        return

    if dry_run:
        log(f"DRY-RUN  | Would submit {len(batch)} URLs:")
        for url in batch[:40]:
            info = urls[url]
            tag  = " [retry-403]" if info["status"] == "failed_403" else ""
            log(f"  WOULD   | {_label(info):<32} {url}{tag}")
        if len(batch) > 40:
            log(f"  ... and {len(batch) - 40} more")
        log("─" * 72)
        return

    # ── Authenticate ──────────────────────────────────────────────────────────
    if not os.path.exists(JSON_KEY_FILE):
        log(f"ERROR    | Service account key not found: {JSON_KEY_FILE}")
        return

    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        credentials = service_account.Credentials.from_service_account_file(
            JSON_KEY_FILE, scopes=SCOPES)
        session     = AuthorizedSession(credentials)
        log("AUTH     | Google Indexing API authenticated ✅")
    except Exception as e:
        log(f"ERROR    | Auth failed: {e}")
        return

    # ── Pre-flight sanity check ───────────────────────────────────────────────
    # Before burning ANY quota, confirm the service account has Owner access.
    log("SANITY   | Pre-run permission check …")
    ok, s_code, s_err = _sanity_check(session, state)
    if not ok:
        log(f"ABORT    | HTTP {s_code} — {s_err}")
        log("─" * 72)
        log("ACTION REQUIRED — Your service account is NOT an Owner in Search Console.")
        log("")
        log("  Follow these steps ONCE in your browser:")
        log("  1. Go to: https://search.google.com/search-console/")
        log("  2. Select property: https://www.rameshdas.dev/")
        log("  3. Click: Settings → Users and permissions → + Add user")
        log(f"  4. Email : {SERVICE_ACCOUNT_EMAIL}")
        log("  5. Role  : Owner   ← must be Owner, not just User")
        log("  6. Click Add → then re-run: python3 smart_indexer.py")
        log("─" * 72)
        return
    log("SANITY   | Permission confirmed ✅ — service account is authorized")

    # ── Submit loop ───────────────────────────────────────────────────────────
    run_submitted  = 0
    run_403        = 0
    run_failed     = 0
    run_not_live   = 0
    stop_reason    = "completed"

    # Threshold: if 10 consecutive 403s hit, re-run sanity check
    SANITY_RETRIGGER = 10
    consec_403       = 0

    for idx, url in enumerate(batch):
        info  = urls[url]
        label = _label(info)

        # ── Optional preflight live-check ─────────────────────────────────────
        if preflight:
            is_live, live_code = preflight_check(url)
            if not is_live:
                info["status"]         = "failed"
                info["last_error"]     = f"Not live — preflight HTTP {live_code}"
                info["last_http_code"] = live_code
                info["last_attempt_at"] = now_iso()
                info["attempts"]       = info.get("attempts", 0) + 1
                log(f"NOT-LIVE   | {label:<34} {url}  HTTP {live_code}")
                run_not_live += 1
                save_state(state)
                time.sleep(0.1)
                continue

        # ── API call ──────────────────────────────────────────────────────────
        info["attempts"]        = info.get("attempts", 0) + 1
        info["last_attempt_at"] = now_iso()

        http_code, err_msg, _ = _api_call(session, url)
        info["last_http_code"] = http_code

        if http_code == 200:
            info["status"]       = "submitted"
            info["submitted_at"] = now_iso()
            info["submit_count"] = info.get("submit_count", 0) + 1
            info["last_error"]   = None
            quota["used_today"] += 1
            consec_403           = 0
            log(f"SUBMITTED  | {label:<34} {url}")
            run_submitted += 1

        elif http_code == 429:
            # Quota exhausted — put back as pending and stop
            info["status"]      = "pending"
            info["last_error"]  = "HTTP 429 — daily quota exceeded"
            quota["used_today"] = daily_limit
            log(f"QUOTA-HIT  | {label:<34} {url}  HTTP 429 — quota full, stopping")
            stop_reason = "quota_exceeded"
            save_state(state)
            break

        elif http_code == 403:
            # Mark as failed_403 (separate status — will be retried last)
            info["status"]     = "failed_403"
            info["last_error"] = f"HTTP 403: {err_msg or 'Permission denied'}"
            log(f"FORBIDDEN  | {label:<34} {url}  HTTP 403 — {err_msg or 'Permission denied'}")
            run_403    += 1
            consec_403 += 1

            # After SANITY_RETRIGGER consecutive 403s, re-check global permission
            if consec_403 >= SANITY_RETRIGGER:
                log(f"SANITY   | {consec_403} consecutive 403s — re-checking permissions …")
                ok2, s2_code, s2_err = _sanity_check(session, state)
                if not ok2:
                    log(f"ABORT    | Global permission failure (HTTP {s2_code}). Stopping run.")
                    log("         | Fix: Search Console → Settings → Users & Permissions")
                    log(f"         |      Add  {SERVICE_ACCOUNT_EMAIL}  as Owner")
                    stop_reason = "global_auth_failure"
                    save_state(state)
                    break
                else:
                    log(f"SANITY   | Permission still valid — 403s are page-specific, continuing …")
                    consec_403 = 0

        else:
            info["status"]     = "failed"
            info["last_error"] = f"HTTP {http_code}: {err_msg or 'unknown error'}"
            log(f"FAILED     | {label:<34} {url}  HTTP {http_code} — {err_msg or ''}")
            consec_403 = 0
            run_failed += 1

        save_state(state)
        time.sleep(DELAY_BETWEEN)

    remaining = total_queued - (run_submitted + run_403 + run_failed + run_not_live)
    log(f"RUN END    | submitted={run_submitted} forbidden-403={run_403} "
        f"failed={run_failed} not-live={run_not_live} "
        f"remaining={max(remaining,0)} quota={quota['used_today']}/{daily_limit} "
        f"reason={stop_reason}")
    log("─" * 72)

    # ── Append run to history file ─────────────────────────────────────────────
    log_run_history({
        "run_at":        now_iso(),
        "dry_run":       dry_run,
        "force":         force,
        "stop_reason":   stop_reason,
        "submitted":     run_submitted,
        "failed_403":    run_403,
        "failed_other":  run_failed,
        "not_live":      run_not_live,
        "quota_used":    quota["used_today"],
        "quota_limit":   daily_limit,
        "remaining":     max(remaining, 0),
    })


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Smart Google Indexing API Submitter with Full State Tracking",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--report",       action="store_true", help="Print status report and exit (no API calls)")
    parser.add_argument("--dry-run",      action="store_true", help="Preview what would be submitted, no API calls")
    parser.add_argument("--force",        action="store_true", help="Re-submit even already-submitted URLs")
    parser.add_argument("--reset",        action="store_true", help="Reset ALL URLs back to pending, then exit")
    parser.add_argument("--reset-url",    metavar="URL",       help="Reset a single URL back to pending, then exit")
    parser.add_argument("--reset-failed", action="store_true", help="Reset only failed URLs back to pending, then exit")
    parser.add_argument("--preflight",    action="store_true", help="HEAD-check each URL is live before submitting (slower but safer)")
    parser.add_argument("--limit",        type=int,            help="Override max submissions this run")
    parser.add_argument("--dir",          default=".",         help="Directory to scan for HTML/XML files")
    parser.add_argument("--url-file",     help="Text file containing a list of URLs to index (one per line)")
    args = parser.parse_args()

    # ── Load state ────────────────────────────────────────────────────────────
    state = load_state()

    # ── Discover & sync ───────────────────────────────────────────────────────
    discovered = {}
    target_urls = None
    target_groups = None
    
    # Check explicitly passed url-file or default main-urls.txt
    default_url_file = os.path.join(GOOGLE_INDEX_DIR, "main-urls.txt")
    target_file = args.url_file if args.url_file else default_url_file
    
    if target_file.startswith("http://") or target_file.startswith("https://") or os.path.exists(target_file):
        file_urls, file_groups = discover_urls_from_file(target_file)
        
        # Check if any of these URLs already exist and are not pending
        already_processed = []
        for url in file_urls:
            if url in state["urls"]:
                info = state["urls"][url]
                if info["status"] not in ("pending", "skipped"):
                    already_processed.append(url)
                    
        if already_processed and sys.stdin.isatty():
            print(f"\n  [info] Found {len(already_processed)} URLs from {target_file} that are already in the system (status: submitted/failed).")
            try:
                ans = input(f"  Do you want to reset these {len(already_processed)} links to 'pending' to restart them? [y/N]: ")
                if ans.lower().strip() in ('y', 'yes'):
                    for url in already_processed:
                        reset_url(state, url)
                    print(f"  [reset] Reset {len(already_processed)} URL(s) to 'pending'.")
            except (KeyboardInterrupt, EOFError):
                print()
                
        discovered.update(file_urls)
        target_urls = list(file_urls.keys())
        target_groups = file_groups
        print(f"  [discover]     Loaded {len(file_urls)} HTML URL(s) from {target_file}")
    else:
        # Also discover from directory
        dir_urls = discover_urls(args.dir)
        discovered.update(dir_urls)

    added, auto_skipped = sync_discovered(state, discovered)
    if added:
        print(f"  [discover]     Added {added} new HTML URL(s) to state.")
    if auto_skipped:
        print(f"  [auto-skipped] Marked {auto_skipped} non-HTML URL(s) as skipped (XML/TXT — Indexing API rejects these).")
    save_state(state)

    # ── Handle sub-commands ───────────────────────────────────────────────────
    if args.reset:
        n = reset_all(state)
        save_state(state)
        print(f"  [reset] Reset {n} URL(s) to 'pending'.")
        return

    if args.reset_failed:
        n = reset_failed(state)
        save_state(state)
        print(f"  [reset-failed] Reset {n} failed URL(s) back to 'pending'.")
        return

    if args.reset_url:
        ok = reset_url(state, args.reset_url)
        save_state(state)
        if ok:
            print(f"  [reset-url] '{args.reset_url}' → pending.")
        else:
            print(f"  [reset-url] URL not found in state: {args.reset_url}")
        return

    if args.report:
        print_report(state, target_urls=target_urls, custom_groups=target_groups)
        return

    # ── Run indexing ──────────────────────────────────────────────────────────
    print_report(state, target_urls=target_urls, custom_groups=target_groups)
    run_indexing(
        state,
        dry_run=args.dry_run,
        force=args.force,
        limit_override=args.limit,
        preflight=args.preflight,
        target_urls=target_urls,
    )
    print_report(state, target_urls=target_urls, custom_groups=target_groups)


if __name__ == "__main__":
    main()
