#!/usr/bin/env python3
"""
sponsorscan - find entry-level jobs at employers with real H-1B filing history.

Two data sources, both of which are structurally durable:

  1. DOL OFLC LCA disclosure data. Published quarterly by the Office of Foreign
     Labor Certification. A legal filing requirement, so it keeps coming out
     regardless of anyone's business model. Free bulk download, no key.

  2. Public ATS job board APIs (Greenhouse, Lever, Ashby). Served straight from
     the employer with no aggregator in between to abandon it.

Usage:
    python sponsorscan.py load-lca --latest
    python sponsorscan.py fetch-jobs
    python sponsorscan.py report --out matches.csv

`companies.yaml` ships with confirmed boards, so a first run can skip
`discover` and go straight to fetch-jobs. Two commands exist to make setup
less fiddly:

    python sponsorscan.py setup     # answer a few questions, get a profile
    python sponsorscan.py doctor    # report which stage needs attention

Run `python sponsorscan.py <command> --help` for per-command options.
"""

import argparse
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
import yaml

try:
    from rapidfuzz import process as rf_process
    from rapidfuzz import fuzz as rf_fuzz
    HAVE_RAPIDFUZZ = True
except ImportError:
    HAVE_RAPIDFUZZ = False

DB_PATH = os.environ.get("SPONSORSCAN_DB", "sponsorscan.db")
UA = {"User-Agent": "sponsorscan/1.0 (personal job search tool)"}


# ---------------------------------------------------------------- normalization

# Suffixes and filler tokens stripped before matching employer names. "Google
# LLC", "GOOGLE INC." and "Google, Inc" all collapse to "google".
_SUFFIXES = {
    "inc", "incorporated", "llc", "l l c", "lp", "llp", "plc", "ltd", "limited",
    "co", "corp", "corporation", "company", "holdings", "holding", "group",
    "usa", "us", "america", "americas", "na", "the", "and", "of",
}

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def norm_employer(name):
    """Collapse an employer name to a comparable key."""
    if not name:
        return ""
    s = str(name).lower()
    s = s.replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    toks = [t for t in _WS.sub(" ", s).strip().split(" ") if t and t not in _SUFFIXES]
    return " ".join(toks)


# ------------------------------------------------------------------- db schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS employers (
    employer_norm    TEXT PRIMARY KEY,
    employer_display TEXT,
    certified        INTEGER DEFAULT 0,
    denied           INTEGER DEFAULT 0,
    withdrawn        INTEGER DEFAULT 0,
    titles           TEXT,
    states           TEXT,
    lvl1 INTEGER DEFAULT 0,
    lvl2 INTEGER DEFAULT 0,
    lvl3 INTEGER DEFAULT 0,
    lvl4 INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    job_key      TEXT PRIMARY KEY,
    source       TEXT,
    company      TEXT,
    company_norm TEXT,
    title        TEXT,
    location     TEXT,
    url          TEXT,
    posted       TEXT,
    description  TEXT,
    fetched_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_norm ON jobs(company_norm);
CREATE TABLE IF NOT EXISTS fetched_companies (
    company_norm TEXT PRIMARY KEY,
    fetched_at   TEXT
);
CREATE TABLE IF NOT EXISTS fetch_failures (
    board     TEXT PRIMARY KEY,
    error     TEXT,
    failed_at TEXT
);
CREATE TABLE IF NOT EXISTS probe_cache (
    provider   TEXT,
    slug       TEXT,
    ok         INTEGER,
    n_jobs     INTEGER,
    checked_at TEXT,
    PRIMARY KEY (provider, slug)
);
"""


def connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(employers)")}
    for col in ("lvl1", "lvl2", "lvl3", "lvl4"):
        if col not in have:
            con.execute(f"ALTER TABLE employers ADD COLUMN {col} INTEGER DEFAULT 0")
    # A Workday guess is a tenant name; the board it resolves to is stored here.
    if "resolved" not in {r[1] for r in con.execute("PRAGMA table_info(probe_cache)")}:
        con.execute("ALTER TABLE probe_cache ADD COLUMN resolved TEXT")
    con.commit()
    return con


# ------------------------------------------------------------------- load-lca

# The DOL file has ~100 columns and the exact header set drifts between fiscal
# years, so columns are resolved by name rather than position.
WANTED = {
    "employer": ["EMPLOYER_NAME"],
    "status": ["CASE_STATUS"],
    "title": ["JOB_TITLE", "SOC_TITLE"],
    "state": ["WORKSITE_STATE", "WORKSITE_STATE_1", "EMPLOYER_STATE"],
    # Prevailing wage level (I-IV). Under the FY2027 weighted selection rule,
    # petitions filed at Level III/IV get better lottery odds, so an employer's
    # typical level directly affects your chances, not just your pay.
    "wage_level": ["PW_WAGE_LEVEL", "PW_WAGE_LEVEL_1"],
}


def _resolve_columns(header):
    idx = {}
    upper = [str(h).strip().upper() if h is not None else "" for h in header]
    for key, candidates in WANTED.items():
        for cand in candidates:
            if cand in upper:
                idx[key] = upper.index(cand)
                break
    missing = [k for k in ("employer", "status") if k not in idx]
    if missing:
        raise SystemExit(
            f"Could not find required column(s) {missing} in the file header.\n"
            f"Saw: {upper[:25]}..."
        )
    return idx


def _iter_rows(path):
    """Stream rows from .xlsx or .csv without loading the whole file."""
    if path.lower().endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        for row in ws.iter_rows(values_only=True):
            yield list(row)
        wb.close()
    else:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.reader(fh):
                yield row


def _resolve_latest_lca():
    """Find the newest LCA disclosure file linked from the DOL page.

    Returns the URL, or exits with the manual instructions. A DOL redesign is
    the expected failure here, so it must not surface as a traceback.
    """
    import onboarding

    print(f"Looking for the newest disclosure file on {onboarding.DOL_PERFORMANCE_PAGE}")
    try:
        r = requests.get(onboarding.DOL_PERFORMANCE_PAGE, headers=UA, timeout=60)
        r.raise_for_status()
        url = onboarding.latest_lca_link(r.text, onboarding.DOL_PERFORMANCE_PAGE)
    except requests.RequestException as exc:
        url = None
        print(f"Could not reach the DOL site: {exc}")

    if not url:
        raise SystemExit(
            "Could not resolve a disclosure link automatically.\n"
            f"Open {onboarding.DOL_PERFORMANCE_PAGE}, download the most recent\n"
            "'LCA Programs (H-1B, H-1B1, E-3)' file, then run:\n"
            "  python sponsorscan.py load-lca <downloaded file> --replace")

    print(f"Found {url}")
    if sys.stdin.isatty():
        reply = input("Download this file? (Y/n): ").strip().lower()
        if reply.startswith("n"):
            raise SystemExit("Cancelled.")
    return url


def cmd_load_lca(args):
    if args.latest and args.path:
        raise SystemExit(
            "argument --latest: not allowed with an explicit path. "
            "Pass one or the other.")
    if not args.latest and not args.path:
        raise SystemExit(
            "Provide a path to a disclosure file, or pass --latest to resolve "
            "the newest one from the DOL site.")

    src = _resolve_latest_lca() if args.latest else args.path
    if src.startswith(("http://", "https://")):
        import onboarding
        local = onboarding.local_filename_for(src)
        print(f"Downloading {src} -> {local} (this file is typically 100-400 MB)")
        try:
            with requests.get(src, stream=True, headers=UA, timeout=120) as r:
                r.raise_for_status()
                with open(local, "wb") as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
        except requests.RequestException as exc:
            if os.path.exists(local):
                os.remove(local)  # a partial file would load as a short dataset
            # Nothing has been deleted yet, so an existing database is intact.
            raise SystemExit(
                f"Could not download {src}: {exc}\n"
                "The DOL site blocks some networks, including GitHub's runners. "
                "Download the file in a browser and pass its local path instead.")
        src = local

    if not os.path.exists(src):
        raise SystemExit(f"No such file: {src}")

    con = connect()
    if args.replace:
        con.execute("DELETE FROM employers")

    agg = {}
    rows = _iter_rows(src)
    try:
        header = next(rows)
    except StopIteration:
        raise SystemExit("File is empty.")
    idx = _resolve_columns(header)

    n = 0
    for row in rows:
        n += 1
        if n % 100_000 == 0:
            print(f"  ...{n:,} rows")

        def get(key):
            i = idx.get(key)
            return row[i] if i is not None and i < len(row) else None

        emp = get("employer")
        if not emp:
            continue
        key = norm_employer(emp)
        if not key:
            continue

        rec = agg.setdefault(key, {
            "display": str(emp).strip(), "certified": 0, "denied": 0,
            "withdrawn": 0, "titles": {}, "states": {},
            "lvl": {1: 0, 2: 0, 3: 0, 4: 0},
        })

        status = (str(get("status")) or "").strip().upper()
        if status.startswith("CERTIFIED") and "WITHDRAWN" in status:
            rec["withdrawn"] += 1
        elif status.startswith("CERTIFIED"):
            rec["certified"] += 1
        elif status.startswith("DENIED"):
            rec["denied"] += 1
        elif status.startswith("WITHDRAWN"):
            rec["withdrawn"] += 1

        t = get("title")
        if t:
            t = str(t).strip().lower()[:80]
            rec["titles"][t] = rec["titles"].get(t, 0) + 1
        st = get("state")
        if st:
            st = str(st).strip().upper()[:2]
            rec["states"][st] = rec["states"].get(st, 0) + 1
        lvl = str(get("wage_level") or "").strip().upper().replace("LEVEL", "").strip()
        lvl_n = {"I": 1, "II": 2, "III": 3, "IV": 4,
                 "1": 1, "2": 2, "3": 3, "4": 4}.get(lvl)
        if lvl_n:
            rec["lvl"][lvl_n] += 1

    print(f"Read {n:,} rows, {len(agg):,} distinct employers.")

    payload = []
    for key, rec in agg.items():
        top_titles = sorted(rec["titles"].items(), key=lambda kv: -kv[1])[:12]
        top_states = sorted(rec["states"].items(), key=lambda kv: -kv[1])[:8]
        payload.append((
            key, rec["display"], rec["certified"], rec["denied"], rec["withdrawn"],
            json.dumps([t for t, _ in top_titles]),
            json.dumps([s for s, _ in top_states]),
            rec["lvl"][1], rec["lvl"][2], rec["lvl"][3], rec["lvl"][4],
        ))

    con.executemany(
        "INSERT OR REPLACE INTO employers "
        "(employer_norm, employer_display, certified, denied, withdrawn, titles, states, "
        " lvl1, lvl2, lvl3, lvl4) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", payload)
    con.commit()
    con.close()
    print(f"Loaded {len(payload):,} employers into {DB_PATH}")


# ------------------------------------------------------------------ fetch-jobs
#
# NOTE: every function in this section makes a live HTTP call and could NOT be
# tested in the environment where this was written. If a provider changes its
# response shape, this is the first place to look.

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"(?i)<\s*(?:br|/p|/div|/li|/tr|/h[1-6])\s*/?>")
_SPACE_RUN = re.compile(r"[ \t\r\f\v]+")
_BLANK_RUN = re.compile(r"\n\s*\n+")


def html_to_text(raw):
    """Flatten ATS markup to prose.

    Greenhouse serves `content` as HTML inside an escaped JSON string, so a
    posting arrives looking like `&lt;p&gt;We don&#39;t sponsor&lt;/p&gt;`.
    The filters downstream are word-boundary regexes over prose and need real
    text. Unescape, turn block tags into newlines so that clause-bounded
    patterns cannot run across list items, then drop the remaining tags.
    """
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _SPACE_RUN.sub(" ", text)
    return _BLANK_RUN.sub("\n", text).strip()


def _get_json(url, timeout=25):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(slug):
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    data = _get_json(url)
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append({
            "job_key": f"greenhouse:{slug}:{j.get('id')}",
            "source": "greenhouse", "title": j.get("title", ""),
            "location": loc, "url": j.get("absolute_url", ""),
            # updated_at moves whenever the employer bulk-edits its board, so
            # a years-old posting would read as new. first_published does not.
            "posted": (j.get("first_published") or j.get("updated_at") or "")[:10],
            "description": html_to_text(j.get("content", "")),
        })
    return out


def fetch_lever(slug):
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    data = _get_json(url)
    out = []
    for j in data:
        cats = j.get("categories") or {}
        out.append({
            "job_key": f"lever:{slug}:{j.get('id')}",
            "source": "lever", "title": j.get("text", ""),
            "location": cats.get("location", "") or "",
            "url": j.get("hostedUrl", ""),
            "posted": time.strftime("%Y-%m-%d", time.gmtime((j.get("createdAt") or 0) / 1000))
                      if j.get("createdAt") else "",
            "description": html_to_text(
                (j.get("descriptionPlain") or "") + " " +
                " ".join(str(d.get("text", "")) + " " + str(d.get("content", ""))
                         for d in (j.get("lists") or []))),
        })
    return out


def fetch_ashby(slug):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
    data = _get_json(url)
    out = []
    for j in data.get("jobs", []):
        out.append({
            "job_key": f"ashby:{slug}:{j.get('id')}",
            "source": "ashby", "title": j.get("title", ""),
            "location": j.get("location", "") or "",
            "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
            "posted": (j.get("publishedAt") or "")[:10],
            "description": html_to_text(j.get("descriptionPlain", "")),
        })
    return out


# Workday hosts most large employers, which are most of the DOL filers. Its list
# endpoint gives titles and a relative "Posted 3 Days Ago", but no description,
# and the report needs the description for its disqualifier checks. So the list
# is paged in full (cheap) and details are fetched only for recent postings.
WORKDAY_MAX_AGE_DAYS = 2  # "Posted 2 Days Ago" can still be inside a 48-hour report
WORKDAY_PAGE = 20          # the API rejects anything larger
WORKDAY_DETAIL_DELAY = 0.1  # per request, per worker
WORKDAY_WORKERS = 4
_WORKDAY_AGE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.I)


def workday_age_days(posted_on):
    """Days since posting from Workday's "Posted N Days Ago" text, or None."""
    m = _WORKDAY_AGE.search(posted_on or "")
    if not m:
        return None
    word = m.group(1).lower()
    if word == "today":
        return 0
    if word == "yesterday":
        return 1
    return int(m.group(2))


def fetch_workday(slug, max_age_days=WORKDAY_MAX_AGE_DAYS):
    """slug is 'tenant/wdN/site', e.g. 'adobe/wd5/external_experienced'."""
    parts = slug.split("/")
    if len(parts) != 3:
        raise ValueError(f"workday slug must be tenant/wdN/site, got '{slug}'")
    tenant, wd, site = parts
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    headers = {**UA, "Accept": "application/json"}

    def list_page(offset):
        r = requests.post(f"{api}/jobs", headers=headers, timeout=25, json={
            "appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": offset,
            "searchText": ""})
        r.raise_for_status()
        time.sleep(WORKDAY_DETAIL_DELAY)
        return r.json()

    def detail(path):
        try:
            return _get_json(f"{api}{path}").get("jobPostingInfo") or {}
        except (requests.RequestException, ValueError) as exc:
            return exc
        finally:
            time.sleep(WORKDAY_DETAIL_DELAY)

    # The list has no usable sort order, so every page is read. The first page
    # gives the total; the rest go through a small pool, since a big employer
    # lists thousands of jobs at 20 a page.
    first = list_page(0)
    total = first.get("total") or 0
    with ThreadPoolExecutor(WORKDAY_WORKERS) as pool:
        pages = [first] + list(pool.map(list_page, range(WORKDAY_PAGE, total, WORKDAY_PAGE)))

        # Ordering is unstable between requests, so a job can appear twice.
        recent = {}
        for page in pages:
            for j in page.get("jobPostings") or []:
                age = workday_age_days(j.get("postedOn"))
                if age is not None and age > max_age_days:
                    continue
                recent.setdefault(j.get("externalPath", ""), j)
        details = list(pool.map(detail, recent))

    out, last_error = [], None
    for (path, j), info in zip(recent.items(), details):
        if isinstance(info, Exception):
            last_error = info
            continue
        places = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
        location = "; ".join(p for p in places if p)
        country = (info.get("country") or {}).get("descriptor")
        if country:
            location = f"{location}, {country}" if location else country
        out.append({
            "job_key": f"workday:{slug}:{info.get('jobReqId') or j.get('externalPath')}",
            "source": "workday", "title": info.get("title") or j.get("title", ""),
            "location": location, "url": info.get("externalUrl", ""),
            "posted": (info.get("startDate") or "")[:10],
            "description": html_to_text(info.get("jobDescription", "")),
        })

    # One bad detail is skipped; all of them failing means the board is down,
    # and fetch-jobs must record that rather than an empty board.
    if recent and not out:
        raise last_error
    return out


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday}


# SpeedyApply's college job lists are markdown tables regenerated daily from a
# private database, so the tables are the only public copy. A row has company,
# title, location, an apply link and an age in days, but no description, so the
# report's disqualifier and skill checks see the title alone.
FEED_SOURCES = {"speedyapply"}
_FEED_ROW = re.compile(r"^\|(.+)\|\s*(\d+)d\s*\|\s*$")
_HREF = re.compile(r'href="([^"]+)"')
_STRONG = re.compile(r"<strong>(.*?)</strong>", re.S)


def _feed_url(url):
    """Lowercased host and path without query or trailing slash, for dedupe."""
    parts = urllib.parse.urlsplit((url or "").strip())
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}"


def fetch_speedyapply(slug, today=None):
    """slug is 'owner/repo/path', e.g. 'speedyapply/2027-SWE-College-Jobs/README.md'."""
    parts = slug.split("/", 2)
    if len(parts) != 3:
        raise ValueError(f"speedyapply slug must be owner/repo/path, got '{slug}'")
    owner, repo, path = parts
    r = requests.get(f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/{path}",
                     headers=UA, timeout=25)
    r.raise_for_status()

    # Ages count from when the tables were generated, which is close enough
    # to the fetch time for a day-granular posted date.
    today = today or time.time()
    out = []
    for line in r.text.splitlines():
        m = _FEED_ROW.match(line.strip())
        if not m:
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        company = _STRONG.search(cells[0]) if cells else None
        link = _HREF.search(cells[-1]) if cells else None
        if len(cells) < 4 or not company or not link:
            continue
        url = html.unescape(link.group(1))
        out.append({
            "job_key": f"speedyapply:{_feed_url(url)}",
            "source": "speedyapply",
            "company": html.unescape(_STRONG.sub(r"\1", company.group(1))).strip(),
            "title": html.unescape(cells[1]),
            "location": html.unescape(re.sub(r"(?i)<br\s*/?>", "; ", cells[2])),
            "url": url,
            "posted": time.strftime("%Y-%m-%d",
                                    time.gmtime(today - int(m.group(2)) * 86400)),
            "description": "",
        })

    # A list that renders but yields no rows means the table format changed,
    # which must show up as a failed board rather than a quiet empty one.
    if not out:
        raise ValueError(f"no job rows found in {path}; the table format may have changed")
    return out


FEED_FETCHERS = {"speedyapply": fetch_speedyapply}


def cmd_fetch_jobs(args):
    with open(args.companies, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    con = connect()
    if args.replace:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM fetched_companies")
        con.execute("DELETE FROM fetch_failures")

    total, failed = 0, []
    for provider, entries in (cfg.get("companies") or {}).items():
        fetcher = FETCHERS.get(provider)
        if not fetcher:
            print(f"  ! unknown provider '{provider}', skipping")
            continue
        for entry in entries or []:
            if isinstance(entry, dict):
                slug = entry.get("slug")
                display = entry.get("name") or slug
            else:
                slug, display = entry, entry
            try:
                if provider == "workday":
                    jobs = fetcher(slug, max_age_days=args.workday_days)
                else:
                    jobs = fetcher(slug)
            except Exception as exc:
                failed.append(f"{provider}/{slug}: {exc}")
                # Read by the notification email, so a dead board is noticed.
                con.execute("INSERT OR REPLACE INTO fetch_failures VALUES (?, ?, ?)",
                            (f"{provider}/{slug}", str(exc)[:200],
                             time.strftime("%Y-%m-%d %H:%M")))
                con.commit()
                continue
            rows = [(
                j["job_key"], j["source"], display, norm_employer(display),
                j["title"], j["location"], j["url"], j["posted"],
                j["description"][:20000], time.strftime("%Y-%m-%d %H:%M"),
            ) for j in jobs]
            con.executemany(
                "INSERT OR REPLACE INTO jobs (job_key, source, company, company_norm, "
                "title, location, url, posted, description, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            # The report baselines employers it has not tracked before. A board
            # that answered with no postings is tracked all the same, so its
            # first real opening is reported rather than silenced.
            con.execute("DELETE FROM fetch_failures WHERE board = ?",
                        (f"{provider}/{slug}",))
            con.execute(
                "INSERT OR REPLACE INTO fetched_companies VALUES (?, ?)",
                (norm_employer(display), time.strftime("%Y-%m-%d %H:%M")))
            con.commit()
            total += len(rows)
            print(f"  {display:<28} {provider:<11} {len(rows):>4} postings")
            time.sleep(args.delay)

    # Feeds list many employers each, so they run after the boards: a company
    # whose own board was fetched keeps the board's copy, which has the
    # description, and a posting already stored under the same URL is skipped.
    boards = {r[0] for r in con.execute("SELECT company_norm FROM fetched_companies")}
    urls = {_feed_url(r[0]) for r in con.execute("SELECT url FROM jobs") if r[0]}
    for provider, entries in (cfg.get("feeds") or {}).items():
        fetcher = FEED_FETCHERS.get(provider)
        if not fetcher:
            print(f"  ! unknown feed '{provider}', skipping")
            continue
        for slug in entries or []:
            try:
                jobs = fetcher(slug)
            except Exception as exc:
                failed.append(f"{provider}/{slug}: {exc}")
                con.execute("INSERT OR REPLACE INTO fetch_failures VALUES (?, ?, ?)",
                            (f"{provider}/{slug}", str(exc)[:200],
                             time.strftime("%Y-%m-%d %H:%M")))
                con.commit()
                continue
            rows = []
            for j in jobs:
                company_norm = norm_employer(j["company"])
                if company_norm in boards or _feed_url(j["url"]) in urls:
                    continue
                urls.add(_feed_url(j["url"]))
                rows.append((
                    j["job_key"], j["source"], j["company"], company_norm,
                    j["title"], j["location"], j["url"], j["posted"],
                    j["description"], time.strftime("%Y-%m-%d %H:%M"),
                ))
            con.executemany(
                "INSERT OR REPLACE INTO jobs (job_key, source, company, company_norm, "
                "title, location, url, posted, description, fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            con.execute("DELETE FROM fetch_failures WHERE board = ?",
                        (f"{provider}/{slug}",))
            con.commit()
            total += len(rows)
            print(f"  {slug:<28} {provider:<11} {len(rows):>4} postings "
                  f"({len(jobs) - len(rows)} already on a fetched board)")
            time.sleep(args.delay)

    con.close()
    print(f"\n{total:,} postings stored.")
    if failed:
        print(f"{len(failed)} board(s) failed. Usually a wrong slug:")
        for f in failed:
            print("  -", f)


# ------------------------------------------------------------------- discover
#
# Turns the employer list you already loaded from DOL into a company list, by
# guessing job board slugs from employer names and probing which ones are real.

# Words dropped when building slug guesses, on top of the corporate suffixes in
# _SUFFIXES. These appear in legal names but rarely in board URLs.
_SLUG_DROP = {"technologies", "technology", "systems", "solutions", "services",
              "software", "labs", "laboratories", "international", "global",
              "worldwide", "enterprises", "industries", "partners", "ventures"}

PROBE_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json&limit=1",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def slug_candidates(name):
    """Plausible board slugs for an employer name, most likely first.

    Deliberately conservative. It does not try single-token guesses on
    multi-word names, because 'definitive' from 'Definitive Intelligence' would
    happily match some unrelated company's board and quietly poison the results.
    """
    base = norm_employer(name)
    if not base:
        return []
    toks = [t for t in base.split() if t not in _SLUG_DROP] or base.split()
    joined = "".join(toks)
    out = [joined]
    if len(toks) > 1:
        out.append("-".join(toks))
    # Variants that keep the filler words ("benchlingtechnologies"). Generated
    # whenever anything was dropped, not just for multi-token names, or a name
    # that reduces to a single token would never get this guess.
    full = "".join(base.split())
    if full != joined:
        out.append(full)
        out.append("-".join(base.split()))
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen and 2 <= len(s) <= 40:
            seen.add(s)
            uniq.append(s)
    return uniq


def probe(provider, slug, timeout=12):
    """Return (ok, n_jobs). A real board with zero openings still counts as ok.

    `ok` is True (the board exists), False (it definitively does not), or None
    (could not tell). The None case matters because a 429 or a read timeout
    says nothing about the slug: recording it as False would write off a real
    employer until the database is rebuilt. Only definitive answers are cached.
    """
    url = PROBE_URLS[provider].format(slug=slug)
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
    except requests.RequestException:
        return None, 0
    if r.status_code in (404, 410):
        return False, 0
    if r.status_code != 200:
        # 429, 5xx, or a redirect to a login page: unknown, not a miss.
        return None, 0
    try:
        data = r.json()
    except ValueError:
        return False, 0
    if provider == "greenhouse":
        return isinstance(data, dict) and "jobs" in data, len(data.get("jobs", []))
    if provider == "lever":
        return isinstance(data, list), len(data)
    return isinstance(data, dict) and "jobs" in data, len(data.get("jobs", []))


# Workday data centers, most common first. Every other wdN host is absent from
# DNS. A slug is tenant/wdN/site: the tenant is guessable from the employer
# name, the data center is found by trying each host, and the site is guessed
# from names employers commonly use. Site names are case-insensitive.
WORKDAY_HOSTS = ("wd1", "wd5", "wd12", "wd3", "wd10", "wd103", "wd108", "wd102",
                 "wd105", "wd107", "wd109", "wd501", "wd502", "wd503", "wd504")
WORKDAY_SITES = ("external", "careers", "externalcareers", "external_careers",
                 "externalcareersite", "external_career_site", "external_career",
                 "external_experienced", "corporatecareers", "ext", "jobs",
                 "{t}", "{t}careers", "{t}_careers", "{t}external", "{t}_external",
                 "{t}externalcareersite", "{t}_external_career_site", "{n}", "{n}_careers")


def _workday_status(tenant, wd, site, timeout):
    """(status code or None, total jobs) for one tenant/wd/site guess."""
    url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    try:
        r = requests.post(url, headers={**UA, "Accept": "application/json"},
                          timeout=timeout, json={"appliedFacets": {}, "limit": 1,
                                                 "offset": 0, "searchText": ""})
    except requests.RequestException:
        return None, 0
    total = 0
    if r.status_code == 200:
        try:
            total = r.json().get("total") or 0
        except ValueError:
            return None, 0
    return r.status_code, total


def find_workday_board(tenant, name, timeout=12):
    """Return (ok, slug, n_jobs) for a Workday tenant guess.

    ok is True with the full slug on a hit. It is False with slug None when no
    data center knows the tenant, and False with a partial "tenant/wdN" slug
    when the tenant exists but none of the common site names match, so the
    caller can report it for a manual lookup. It is None when an error
    (429, 5xx, timeout) made the answer uncertain, which must not be cached.
    """
    for wd in WORKDAY_HOSTS:
        status, total = _workday_status(tenant, wd, "external", timeout)
        if status == 422:
            continue            # unknown tenant on this data center
        if status == 200:
            return True, f"{tenant}/{wd}/external", total
        if status != 404:
            return None, None, 0

        words = [t for t in norm_employer(name).split() if t not in _SLUG_DROP]
        sites = dict.fromkeys(s.format(t=tenant, n="_".join(words))
                              for s in WORKDAY_SITES[1:])
        for site in sites:
            status, total = _workday_status(tenant, wd, site, timeout)
            if status == 200:
                return True, f"{tenant}/{wd}/{site}", total
            if status != 404:
                return None, None, 0
        return False, f"{tenant}/{wd}", 0
    return False, None, 0


DEFAULT_ROLES = ("software", "developer", "engineer", "data scien", "data analyst",
                 "machine learning", "computer", "research", "programmer",
                 "statistician", "analyst")


def _discover_workday(con, candidates, args):
    """Search Workday for the heavier filers. Returns {display: [(slug, n)]}.

    Each tenant guess can cost a request per data center, so this is opt-in
    and limited to employers with at least --workday-min-certified filings,
    which is where Workday users are. Every definite answer is cached.
    """
    from concurrent.futures import as_completed

    targets = [(d, c) for _, d, c in candidates if c >= args.workday_min_certified]
    cached = {slug: (ok, n, resolved) for slug, ok, n, resolved in con.execute(
        "SELECT slug, ok, n_jobs, resolved FROM probe_cache WHERE provider = 'workday'")}

    pending = {}
    for display, certified in targets:
        for tenant in slug_candidates(display):
            if "-" in tenant or tenant in cached:
                continue  # Workday tenants have no hyphens
            if tenant not in pending or certified > pending[tenant][1]:
                pending[tenant] = (display, certified)

    print(f"\nWorkday: {len(targets):,} employers with >= {args.workday_min_certified} "
          f"certified LCAs, {len(pending):,} tenant guesses to try "
          f"({len(cached):,} already cached).")
    transient = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(find_workday_board, t, d): (t, d) for t, (d, _) in pending.items()}
        try:
            for fut in as_completed(futs):
                tenant, display = futs[fut]
                try:
                    ok, board, n = fut.result()
                except Exception:
                    ok, board, n = None, None, 0
                if ok is None:
                    transient += 1
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO probe_cache VALUES (?,?,?,?,?,?)",
                    ("workday", tenant, int(ok), n, time.strftime("%Y-%m-%d"), board))
                con.commit()
                cached[tenant] = (ok, n, board)
                if ok:
                    print(f"  HIT workday     {board:<40} {n:>4} jobs   ({display})")
        except KeyboardInterrupt:
            print("\nInterrupted. Workday answers so far are cached.")
            pool.shutdown(wait=False, cancel_futures=True)
    if transient:
        print(f"  {transient:,} Workday search(es) failed transiently and were left "
              f"uncached. Re-run to retry them.")

    found, partial = {}, []
    for display, _ in targets:
        for tenant in slug_candidates(display):
            ok, n, board = cached.get(tenant, (False, 0, None))
            if ok:
                found.setdefault(display, []).append((board, n))
            elif board:
                partial.append((board, display))
    if partial:
        print("  Workday tenants found, but not their site name. Open the employer's "
              "careers page, copy the part after myworkdayjobs.com/, and add "
              "tenant/wdN/site to companies.yaml by hand:")
        for board, display in sorted(set(partial)):
            print(f"    {board:<24} {display}")
    return found


def cmd_discover(args):
    con = connect()
    roles = [r.strip().lower() for r in args.roles.split(",") if r.strip()] \
        if args.roles else list(DEFAULT_ROLES)
    states = [s.strip().upper() for s in args.states.split(",") if s.strip()] \
        if args.states else None

    rows = con.execute(
        "SELECT employer_norm, employer_display, certified, titles, states "
        "FROM employers WHERE certified >= ? ORDER BY certified DESC",
        (args.min_certified,)).fetchall()
    if not rows:
        raise SystemExit("No employers loaded. Run `load-lca` first.")

    candidates = []
    for norm, display, certified, titles_json, states_json in rows:
        titles = " ".join(json.loads(titles_json or "[]")).lower()
        if roles and not any(r in titles for r in roles):
            continue
        if states:
            emp_states = json.loads(states_json or "[]")
            if not any(s in emp_states for s in states):
                continue
        # No name-based exclusion. Filtering employers by whether their name
        # contains "consulting" drops Deloitte and Palantir while keeping TCS,
        # Infosys and Accenture, which is arbitrary and silently loses good
        # employers. Volume and wage-level data are in the report instead, so
        # you can judge rather than have the tool judge for you.
        if args.max_certified and certified > args.max_certified:
            continue
        candidates.append((norm, display, certified))
        if len(candidates) >= args.limit:
            break

    print(f"{len(candidates):,} candidate employers "
          f"(certified >= {args.min_certified}"
          f"{', states ' + ','.join(states) if states else ', nationwide'}).")

    cache = {(p, s): (ok, n) for p, s, ok, n in con.execute(
        "SELECT provider, slug, ok, n_jobs FROM probe_cache")}

    # Two employers can generate the same slug guess ("Acme Labs" and "Acme
    # Laboratories" both reduce to "acme"). One task per (provider, slug),
    # attributed to the heaviest filer, avoids probing the same URL twice.
    pending = {}
    for norm, display, certified in candidates:
        for slug in slug_candidates(display):
            for provider in PROBE_URLS:
                if (provider, slug) in cache:
                    continue
                prev = pending.get((provider, slug))
                if prev is None or certified > prev[1]:
                    pending[(provider, slug)] = (display, certified)
    tasks = [(p, s, d, c) for (p, s), (d, c) in pending.items()]

    print(f"{len(tasks):,} probes to run "
          f"({len(cache):,} already cached). Ctrl-C is safe, results are saved as they land.")

    found, done, transient = {}, 0, 0
    if tasks:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(probe, p, s): (p, s, d, c) for p, s, d, c in tasks}
            try:
                for fut in as_completed(futs):
                    provider, slug, display, certified = futs[fut]
                    try:
                        ok, n = fut.result()
                    except Exception:
                        ok, n = False, 0
                    done += 1
                    if ok is None:
                        # Not a definitive answer. Left uncached so that the
                        # next run retries instead of writing the employer off.
                        transient += 1
                        continue
                    con.execute(
                        "INSERT OR REPLACE INTO probe_cache "
                        "(provider, slug, ok, n_jobs, checked_at) VALUES (?,?,?,?,?)",
                        (provider, slug, int(ok), n, time.strftime("%Y-%m-%d")))
                    if ok:
                        cache[(provider, slug)] = (True, n)
                        print(f"  HIT {provider:<11} {slug:<28} {n:>4} jobs   ({display})")
                    if done % 300 == 0:
                        con.commit()
                        print(f"  ...{done:,}/{len(tasks):,}")
            except KeyboardInterrupt:
                print("\nInterrupted, saving what we have.")
            finally:
                con.commit()
    if transient:
        print(f"  {transient:,} probe(s) failed transiently (timeout, rate limit "
              f"or server error) and were left uncached. Re-run to retry them.")

    workday = _discover_workday(con, candidates, args) \
        if getattr(args, "workday", False) else {}

    # Rebuild the company list from every cached hit that maps to a candidate.
    by_display = {}
    for norm, display, certified in candidates:
        for slug in slug_candidates(display):
            for provider in PROBE_URLS:
                ok, n = cache.get((provider, slug), (False, 0))
                if ok:
                    prev = by_display.get(display)
                    if prev is None or n > prev[2]:
                        by_display[display] = (provider, slug, n, certified)
        for board, n in workday.get(display, []):
            prev = by_display.get(display)
            if prev is None or n > prev[2]:
                by_display[display] = ("workday", board, n, certified)

    existing, feeds = {}, {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        # Feeds are not boards discover can find, so they survive a rebuild.
        feeds = cfg.get("feeds") or {}
        if args.merge:
            existing = cfg.get("companies") or {}

    # Providers discover cannot probe, such as Workday, are carried over as-is.
    merged = {p: list(v or []) for p, v in existing.items()}
    for p in (*PROBE_URLS, "workday"):
        merged.setdefault(p, [])
    seen = {p: {(e.get("slug") if isinstance(e, dict) else e) for e in merged[p]}
            for p in merged}
    added = 0
    for display, (provider, slug, n, certified) in sorted(
            by_display.items(), key=lambda kv: -kv[1][3]):
        if slug in seen[provider]:
            continue
        merged[provider].append({"slug": slug, "name": display})
        seen[provider].add(slug)
        added += 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("# Generated by `sponsorscan.py discover`. Hand edits to `name:`\n"
                 "# are preserved on re-run with --merge (the default).\n"
                 "#\n"
                 "# Slugs are guessed from DOL employer names and confirmed by a live\n"
                 "# probe, so a listed board definitely exists. It is still possible for\n"
                 "# a guess to land on a DIFFERENT company with a similar name. If a\n"
                 "# company's postings look wrong, delete its line.\n\n")
        out = {"companies": {p: v for p, v in merged.items() if v}}
        if feeds:
            out["feeds"] = feeds
        yaml.safe_dump(out, fh, sort_keys=False, default_flow_style=False)

    con.close()
    total = sum(len(v) for v in merged.values())
    print(f"\n{len(by_display):,} employers matched to a live board. "
          f"Added {added:,} new; {total:,} companies now in {args.out}.")
    print("Next:  python sponsorscan.py fetch-jobs --replace")


# ---------------------------------------------------------------------- report

# Phrases that mean you are excluded regardless of anything else. Checked against
# the posting body. This is the filter that actually matters while on OPT.
DISQUALIFIERS = [
    # Any negation followed by "sponsor" within the same clause. Catches the
    # long tail of phrasings ("do not offer", "does not provide", "unable to",
    # "without", "no sponsorship available", "not now or in the future require")
    # without needing a pattern per variant. A sentence, a semicolon and a line
    # break all end the clause, so that an unrelated negation earlier in the
    # sentence cannot reach the word: "There is no cost to relocate; we sponsor
    # visas" is not a refusal.
    r"\b(?:not|no|non|unable|without|cannot|can't|won't|unwilling)\b[^.;\n]{0,60}?sponsor",
    r"\bmust be (?:a |an )?(?:u\.?\s?s\.?|united states)\s?(?:citizen|person|national)\b",
    r"\b(?:u\.?\s?s\.?|united states)\s?citizenship (?:is )?required\b",
    r"\bsecurity clearance\b",
    r"\bitar\b",
    r"\bexport control(?:led|s)?\b",
]

SPONSOR_POSITIVE = [
    r"\bvisa sponsorship (?:is )?available\b",
    r"\bwe (?:will )?sponsor\b",
    r"\bh-?1b sponsorship\b",
    r"\bsponsorship (?:is )?(?:available|offered|provided)\b",
]

SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|distinguished|lead|architect|manager|"
    r"director|head of|vp|vice president|chief|fellow)\b", re.I)

ENTRY_TITLE = re.compile(
    r"\b(intern|internship|new ?grad|new graduate|university grad|recent grad|"
    r"early career|entry.level|junior|jr\.?|associate|apprentice|i{1,2}\b)\b", re.I)

# Wording that marks a posting as early career even when the title also has a
# senior word, as in "Program Manager Intern". Stricter than ENTRY_TITLE, which
# includes "associate" and a bare "II" and would let "Senior Associate" through.
EARLY_CAREER_TITLE = re.compile(
    r"\b(intern|internship|new ?grad|new graduate|university grad|recent grad|"
    r"early career|entry.level|apprentice(?:ship)?)\b", re.I)


def is_senior_title(title):
    title = title or ""
    return bool(SENIOR_TITLE.search(title)) and not EARLY_CAREER_TITLE.search(title)


DISQ_RE = [re.compile(p, re.I) for p in DISQUALIFIERS]
POS_RE = [re.compile(p, re.I) for p in SPONSOR_POSITIVE]


def load_employer_index(con):
    rows = con.execute(
        "SELECT employer_norm, employer_display, certified, denied, withdrawn, titles, "
        "       states, lvl1, lvl2, lvl3, lvl4 FROM employers").fetchall()
    out = {}
    for r in rows:
        lvls = [r[7] or 0, r[8] or 0, r[9] or 0, r[10] or 0]
        tot = sum(lvls)
        filed = (r[2] or 0) + (r[3] or 0) + (r[4] or 0)
        out[r[0]] = {
            "display": r[1], "certified": r[2], "denied": r[3], "withdrawn": r[4],
            "titles": json.loads(r[5] or "[]"), "states": json.loads(r[6] or "[]"),
            "lvls": lvls,
            # Share filed at Level III/IV. Higher is better on two counts: the
            # pay is higher, and the FY2027 weighted lottery favours those levels.
            "senior_share": round((lvls[2] + lvls[3]) / tot, 2) if tot else None,
            "trouble_rate": round(((r[3] or 0) + (r[4] or 0)) / filed, 2) if filed else None,
        }
    return out


def match_employer(company_norm, index, keys, cutoff):
    """Exact key match, then fuzzy fallback."""
    if company_norm in index:
        return company_norm, 100
    if not HAVE_RAPIDFUZZ or not keys or cutoff <= 0:
        return None, 0
    hit = rf_process.extractOne(
        company_norm, keys, scorer=rf_fuzz.token_set_ratio, score_cutoff=cutoff)
    if hit:
        return hit[0], int(hit[1])
    return None, 0


def cmd_report(args):
    con = connect()
    index = load_employer_index(con)
    keys = list(index.keys())
    if not index:
        raise SystemExit("No LCA data loaded. Run `load-lca` first.")

    locs = [s.strip().lower() for s in (args.locations or "").split(",") if s.strip()]
    results, dropped = [], 0

    for row in con.execute(
            "SELECT company, company_norm, title, location, url, posted, description, source "
            "FROM jobs"):
        company, cnorm, title, location, url, posted, desc, source = row
        blob = f"{title}\n{desc}"

        hit = next((p.pattern for p in DISQ_RE if p.search(blob)), None)
        if hit:
            dropped += 1
            continue

        if not args.include_senior and is_senior_title(title):
            continue

        score, why = 0, []

        key, conf = match_employer(cnorm, index, keys, args.fuzzy_cutoff)
        emp = index.get(key) if key else None
        if emp and emp["certified"] > 0:
            score += 40
            why.append(f"{emp['certified']} certified LCAs")
            if conf < 100:
                why.append(f"fuzzy match {conf} to '{emp['display']}'")
            if emp["certified"] >= 25:
                score += 10
            if emp["senior_share"] is not None and emp["senior_share"] >= 0.5:
                score += 10
                why.append(f"{int(emp['senior_share']*100)}% filed at wage level III/IV")
            if emp["trouble_rate"] is not None and emp["trouble_rate"] >= 0.25:
                score -= 10
                why.append(f"{int(emp['trouble_rate']*100)}% denied or withdrawn")
            tl = (title or "").lower()
            if any(any(w in t for w in tl.split() if len(w) > 4) for t in emp["titles"]):
                score += 15
                why.append("filed for similar roles")
        elif args.sponsors_only:
            continue

        if ENTRY_TITLE.search(title or ""):
            score += 20
            why.append("entry level")

        if locs and any(l in (location or "").lower() for l in locs):
            score += 15
            why.append("location match")
        elif locs and not args.any_location:
            continue

        if any(p.search(blob) for p in POS_RE):
            score += 10
            why.append("sponsorship stated in posting")

        results.append({
            "score": score, "company": company, "title": title,
            "location": location, "posted": posted, "source": source,
            "signals": "; ".join(why), "url": url,
            "lca_certified": emp["certified"] if emp else 0,
            "lca_senior_wage_share": emp["senior_share"] if emp else None,
            "lca_denied_withdrawn_rate": emp["trouble_rate"] if emp else None,
        })

    con.close()
    results.sort(key=lambda r: (-r["score"], r["company"]))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(results[0].keys()) if results else
                               ["score", "company", "title", "location", "posted",
                                "source", "signals", "url", "lca_certified",
                                "lca_senior_wage_share", "lca_denied_withdrawn_rate"])
            w.writeheader()
            w.writerows(results)
        print(f"Wrote {len(results)} rows to {args.out}")

    print(f"\n{dropped} postings dropped on explicit disqualifiers "
          f"(citizenship, clearance, or no-sponsorship language).\n")
    for r in results[:args.top]:
        print(f"[{r['score']:>3}] {r['company']} - {r['title']}")
        print(f"      {r['location'] or '?'} | {r['posted'] or '?'} | {r['signals']}")
        print(f"      {r['url']}\n")


# ----------------------------------------------------------------------- setup

def cmd_setup(args):
    """Ask a few questions and write a valid profile."""
    import onboarding

    print("This writes a candidate profile. Blank answers take the default.\n")
    try:
        path = onboarding.run_setup(onboarding.console_ask,
                                    profiles_dir=args.profiles_dir,
                                    notify=print)
    except (KeyboardInterrupt, EOFError):
        raise SystemExit("\nCancelled. Nothing was written.")

    from profile_loader import load_profile

    print(f"\nWrote {path}\n")
    print(onboarding.setup_guide(load_profile(path), path, windows=os.name == "nt"))


# ---------------------------------------------------------------------- doctor

def cmd_doctor(args):
    """Report which pipeline stage needs attention.

    The checks live in onboarding.py as pure functions; this only supplies the
    paths, the environment and the date, then prints and sets the exit code.
    """
    import datetime

    import onboarding

    results = onboarding.run_checks(
        db_path=args.db or DB_PATH,
        companies_path=args.companies,
        profile_path=args.profile,
        env=os.environ,
        today=datetime.date.today())

    print(onboarding.format_results(results))
    raise SystemExit(onboarding.exit_code(results))


# ------------------------------------------------------------------------ main

def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("load-lca", help="Load DOL LCA disclosure file into SQLite")
    a.add_argument("path", nargs="?", default=None,
                   help="Local .xlsx/.csv path, or an https:// URL")
    a.add_argument("--latest", action="store_true",
                   help="Resolve the newest disclosure file from the DOL site "
                        "instead of passing a path")
    a.add_argument("--replace", action="store_true", help="Clear existing employer rows first")
    a.set_defaults(func=cmd_load_lca)

    b = sub.add_parser("fetch-jobs", help="Pull live postings from ATS job boards")
    b.add_argument("--companies", default="companies.yaml")
    b.add_argument("--replace", action="store_true")
    b.add_argument("--delay", type=float, default=0.4, help="Seconds between boards")
    b.add_argument("--workday-days", type=int, default=WORKDAY_MAX_AGE_DAYS,
                   help="Fetch Workday postings up to this many days old "
                        f"(default {WORKDAY_MAX_AGE_DAYS})")
    b.set_defaults(func=cmd_fetch_jobs)

    d = sub.add_parser("discover", help="Build companies.yaml from the DOL employer list")
    d.add_argument("--out", default="companies.yaml")
    d.add_argument("--min-certified", type=int, default=5,
                   help="Skip employers with fewer certified LCAs (default 5)")
    d.add_argument("--roles", default="",
                   help="Comma-separated title keywords; blank uses a tech/data default set")
    d.add_argument("--states", default="",
                   help="Comma-separated 2-letter states; blank means nationwide")
    d.add_argument("--limit", type=int, default=2000, help="Max employers to probe")
    d.add_argument("--workers", type=int, default=12)
    d.add_argument("--max-certified", type=int, default=0,
                   help="Skip employers above this many certified LCAs "
                        "(0 = no cap). Use to exclude the handful of mega-filers.")
    d.add_argument("--workday", action="store_true",
                   help="Also search Workday boards. Slower: up to one request per "
                        "Workday data center for each guess")
    d.add_argument("--workday-min-certified", type=int, default=100,
                   help="Only search Workday for employers with at least this many "
                        "certified LCAs (default 100)")
    d.add_argument("--no-merge", dest="merge", action="store_false",
                   help="Overwrite the company list instead of merging into it")
    d.set_defaults(func=cmd_discover, merge=True)

    c = sub.add_parser("report", help="Join, filter, rank, export")
    c.add_argument("--out", default="matches.csv")
    c.add_argument("--top", type=int, default=25)
    c.add_argument("--locations", default="",
                   help="Comma-separated location substrings; blank means anywhere in the US")
    c.add_argument("--any-location", action="store_true",
                   help="Keep postings that miss the location filter")
    c.add_argument("--include-senior", action="store_true")
    c.add_argument("--sponsors-only", action="store_true",
                   help="Drop employers with no certified LCAs on record")
    c.add_argument("--fuzzy-cutoff", type=int, default=90)
    c.set_defaults(func=cmd_report)

    st = sub.add_parser("setup", help="Answer a few questions to write a profile")
    st.add_argument("--profiles-dir", default="profiles",
                    help="Directory the profile is written to")
    st.set_defaults(func=cmd_setup)

    doc = sub.add_parser("doctor",
                         help="Check each pipeline stage and report what to fix")
    doc.add_argument("--profile", default=None,
                     help="Profile to validate; omitted skips the profile checks")
    doc.add_argument("--companies", default="companies.yaml")
    doc.add_argument("--db", default=None,
                     help="Database to inspect; defaults to SPONSORSCAN_DB or ./sponsorscan.db")
    doc.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
