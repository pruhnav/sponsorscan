"""Local, single-user dashboard for SponsorScan.

SPONSORSCAN_DB selects the existing job database unless --db is supplied.
Private profile and application data live in --workspace, never in static assets.
Company logos are fetched from each company's own site (its `domain:` in
companies.yaml) on first view and cached in the workspace's logos/ folder.
This server intentionally binds only to 127.0.0.1. It is not a public hosting API.
"""

from __future__ import annotations

import copy
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from onboarding import (SKILL_WEIGHTS, build_profile, known_skill_names,
                        setup_guide, setup_sections, slugify, unknown_skills)
from profile_loader import (DEFAULT_PROFILE, VALID_WORK_AUTHORIZATION, ProfileError,
                            authorization_warnings, load_profile, validate_profile)

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "dashboard"
STATUSES = ("saved", "applied", "interview", "offer", "rejected", "archived")
MAX_BODY = 64 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def safe_url(value):
    """Posting URLs are untrusted data, even when they came from a job board."""
    if not isinstance(value, str) or len(value) > 4096:
        return ""
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in ("http", "https") and parsed.hostname else ""
    except ValueError:
        return ""


class ApplicationStore:
    """Snapshots outlive both the report window and replacement of the job feed."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS applications (
                owner TEXT NOT NULL, job_key TEXT NOT NULL, snapshot TEXT NOT NULL,
                status TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                applied_at TEXT, PRIMARY KEY(owner, job_key))""")

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def list(self, owner):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM applications WHERE owner=? "
                               "ORDER BY updated_at DESC", (owner,)).fetchall()
        return [dict(json.loads(row["snapshot"]), **{
            key: row[key] for key in ("status", "notes", "created_at", "updated_at",
                                      "applied_at")}) for row in rows]

    def save(self, owner, job, status, notes):
        if status not in STATUSES:
            raise ValueError("Choose a valid application status.")
        if not isinstance(notes, str) or len(notes) > 10000:
            raise ValueError("Notes must be text with at most 10,000 characters.")
        job = dict(job, url=safe_url(job.get("url", "")))
        stamp = now_iso()
        with self.connect() as con:
            old = con.execute("SELECT * FROM applications WHERE owner=? AND job_key=?",
                              (owner, job["job_key"])).fetchone()
            applied = old["applied_at"] if old else None
            if status in ("applied", "interview", "offer", "rejected") and not applied:
                applied = stamp
            # Do not overwrite the original evidence or submission snapshot on refresh.
            snapshot = old["snapshot"] if old else json.dumps(job)
            created = old["created_at"] if old else stamp
            con.execute("""INSERT INTO applications VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, job_key) DO UPDATE SET
                status=excluded.status, notes=excluded.notes,
                updated_at=excluded.updated_at, applied_at=excluded.applied_at""",
                        (owner, job["job_key"], snapshot, status, notes, created, stamp,
                         applied))

    def delete(self, owner, job_key):
        with self.connect() as con:
            return con.execute("DELETE FROM applications WHERE owner=? AND job_key=?",
                               (owner, job_key)).rowcount > 0


def bounded_int(data, key, default, low, high, existing=None):
    value = data.get(key, default)
    if type(value) is not int or value < 0 or (
            not low <= value <= high and value != existing):
        raise ValueError(f"{key} must be a whole number between {low} and {high}.")
    return value


def text_list(value, label):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"{label} must be a list of at most 100 items.")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 150
           for item in value):
        raise ValueError(f"Every {label} entry must be text, 1 to 150 characters.")
    return list(dict.fromkeys(item.strip() for item in value))


# ------------------------------------------------------------- company logos
LOGO_TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif",
              "webp": "image/webp", "ico": "image/x-icon", "svg": "image/svg+xml"}
LOGO_MAX_BYTES = 512 * 1024
LOGO_RETRY_SECONDS = 7 * 24 * 3600  # a site without a usable icon is asked again weekly
LOGO_AGENT = "Mozilla/5.0 (compatible; SponsorScan dashboard)"
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def image_kind(data):
    """The type of a usable logo, judged from its bytes and never its label.

    Rasters must be at least 32 pixels, so a 16-pixel favicon is not stretched
    into a blur; the tile keeps its letter instead.
    """
    if not data or len(data) < 24 or len(data) > LOGO_MAX_BYTES:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        width, height = struct.unpack(">II", data[16:24])
        return "png" if min(width, height) >= 32 else None
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"\x00\x00\x01\x00":
        count = struct.unpack("<H", data[4:6])[0]
        # Each 16-byte directory entry starts with the width; 0 means 256.
        widths = [data[6 + 16 * i] or 256 for i in range(min(count, 32)) if 6 + 16 * i < len(data)]
        return "ico" if widths and max(widths) >= 32 else None
    head = data[:2048].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head:
        return "svg"
    return None


class _IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attrs = {key.lower(): value or "" for key, value in attrs}
        rel = attrs.get("rel", "").lower().split()
        if attrs.get("href") and any(r == "icon" or r.startswith("apple-touch-icon") for r in rel):
            self.links.append((rel, attrs["href"].strip(), attrs.get("sizes", ""),
                               attrs.get("type", "").lower()))


def icon_candidates(html, base_url):
    """Icon URLs for a home page, best first.

    Touch icons are large, square and opaque, so they come first; then large
    declared icons, SVG, the conventional /apple-touch-icon.png, any other
    declared icon, and /favicon.ico last.
    """
    parser = _IconLinks()
    parser.feed(html or "")
    root = "{0.scheme}://{0.netloc}".format(urlsplit(base_url))
    ranked = [((3, 0), root + "/apple-touch-icon.png"), ((5, 0), root + "/favicon.ico")]
    for rel, href, sizes, kind in parser.links:
        url = urljoin(base_url, href)
        if urlsplit(url).scheme not in ("http", "https"):
            continue  # inline data: icons and other schemes
        size = max((int(n) for n in re.findall(r"(\d+)x\d+", sizes)), default=0)
        svg = kind == "image/svg+xml" or urlsplit(url).path.lower().endswith(".svg")
        if any(r.startswith("apple-touch-icon") for r in rel):
            rank = (0, -size)
        elif size >= 64 and not svg:
            rank = (1, -size)
        elif svg:
            rank = (2, 0)
        else:
            rank = (4, -size)
        ranked.append((rank, url))
    ranked.sort(key=lambda item: item[0])
    return list(dict.fromkeys(url for _, url in ranked))


def _public_host(host):
    """Whether every address a host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    try:
        return bool(infos) and all(
            ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)
    except ValueError:
        return False


def fetch_public(url, limit, deadline, truncate=False):
    """GET a public http(s) URL as (bytes, final URL), or (None, url).

    Redirects are followed by hand, at most four, so none can lead the request
    to this computer or the local network. A body over `limit` bytes is refused,
    or cut short when `truncate` is set (a home page's icons are near its top).
    """
    import requests

    for _ in range(5):
        parts = urlsplit(url)
        remaining = deadline - time.monotonic()
        if (remaining <= 0 or parts.scheme not in ("http", "https") or not parts.hostname
                or not _public_host(parts.hostname)):
            return None, url
        try:
            with requests.get(url, headers={"User-Agent": LOGO_AGENT}, stream=True,
                              timeout=min(6.0, max(1.0, remaining)),
                              allow_redirects=False) as response:
                if response.is_redirect and response.headers.get("Location"):
                    url = urljoin(url, response.headers["Location"])
                    continue
                if response.status_code != 200:
                    return None, url
                data = b""
                for chunk in response.iter_content(16384):
                    data += chunk
                    if len(data) > limit:
                        if not truncate:
                            return None, url
                        return data[:limit], url
                return data, url
        except requests.RequestException:
            return None, url
    return None, url


def fetch_logo_url(url, budget=15.0):
    """(kind, bytes) of the image at a configured logo address, or None."""
    data, _ = fetch_public(url, LOGO_MAX_BYTES, time.monotonic() + budget)
    kind = image_kind(data)
    return (kind, data) if kind else None


def fetch_logo(domain, budget=15.0):
    """(kind, bytes) of a company's icon from its own site, or None."""
    deadline = time.monotonic() + budget
    home, base = fetch_public(f"https://{domain}/", 1024 * 1024, deadline, truncate=True)
    html = home.decode("utf-8", "replace") if home else ""
    for url in icon_candidates(html, base if home else f"https://{domain}/")[:8]:
        if time.monotonic() >= deadline:
            break
        data, _ = fetch_public(url, LOGO_MAX_BYTES, deadline)
        kind = image_kind(data)
        if kind:
            return kind, data
    return None


_SKILL_VOCABULARY = None


def add_fit_breakdown(report, db_path, profile):
    """Explain each match's profile fit in the parts the report scored it with.

    Profile fit is the role's base score plus points for each profile skill
    the posting mentions, capped at 100. This reuses those same functions, so
    the parts add up to the score shown. `missing` lists vocabulary skills the
    posting mentions that the profile does not list.
    """
    global _SKILL_VOCABULARY
    import re as _re

    from skill_patterns import SKILL_PATTERNS
    from sponsor_daily_report import (MAX_SKILL_POINTS, build_resume_skills,
                                      build_target_roles, classify_role, score_skills)

    matches = report.get("matches", [])
    if not matches:
        return
    if _SKILL_VOCABULARY is None:
        _SKILL_VOCABULARY = [(name, [_re.compile(p, _re.I) for p in patterns])
                             for name, patterns in SKILL_PATTERNS.items()]
    roles, skills = build_target_roles(profile), build_resume_skills(profile)
    role_max = max((score for _, _, score in roles), default=0)
    weights = {name: value for name, _, value in skills}

    def parts(name):  # "NLP/LLMs" covers "NLP" and "LLMs"
        return {name.lower(), *(part.strip().lower() for part in name.split("/"))}

    own = set().union(*(parts(name) for name in weights)) if weights else set()
    texts = {}
    keys = [job["job_key"] for job in matches if job.get("job_key")]
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                                     uri=True)) as con:
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                for key, title, description in con.execute(
                        "SELECT job_key, title, description FROM jobs WHERE job_key IN ({})"
                        .format(",".join("?" * len(chunk))), chunk):
                    texts[key] = (title or "", description or "")
    except sqlite3.OperationalError:
        pass  # without descriptions, the parts still come from the title
    for job in matches:
        title, description = texts.get(job.get("job_key"), (job.get("title") or "", ""))
        family, base = classify_role(title, roles)
        matched, points = score_skills(title, description, skills)
        text = f"{title}\n{description}"
        asked = [name for name, patterns in _SKILL_VOCABULARY
                 if not parts(name) & own and any(p.search(text) for p in patterns)]
        # A combined name repeats a skill already listed on its own.
        named = {name.lower() for name in asked}
        asked = [name for name in asked if "/" not in name or not (parts(name) - {name.lower()}) & named]
        job["fit_breakdown"] = {
            "role": {"name": family or job.get("role_family", ""), "points": base, "max": role_max},
            "skills": {"points": points, "max": MAX_SKILL_POINTS, "listed": len(weights),
                       "matched": [[name, weights.get(name, 0)] for name in matched]},
            "missing": asked[:10], "missing_total": len(asked),
        }


FILING_QUARTERS = 8  # bars in each employer's chart: two years of quarters


def add_filing_history(report, db_path):
    """Add each matched employer's certified H-1B filings per fiscal quarter.

    Report rows name the matched employer (`lca_employer`) but not its key, and
    the CSV writer rejects extra fields, so the dashboard adds the series here.
    `metadata.lca_quarters` labels the quarters every series shares, oldest
    first. `metadata.perm_loaded` tells a real zero from green card data that
    was never loaded, which the employers table cannot.
    """
    from sponsorscan import norm_employer

    matches = report.get("matches", [])
    metadata = report.setdefault("metadata", {})
    metadata["lca_quarters"], metadata["perm_loaded"] = [], False
    keys = {}
    for job in matches:
        job["lca_by_quarter"] = []
        if job.get("lca_employer"):
            keys[id(job)] = norm_employer(job["lca_employer"])
    try:
        with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro",
                                     uri=True)) as con:
            try:
                metadata["perm_loaded"] = bool(
                    con.execute("SELECT COUNT(*) FROM perm_quarters").fetchone()[0])
            except sqlite3.OperationalError:
                pass  # a database written before green card data existed
            # Quarter 0 holds filings without a decision date; no bar shows them.
            quarters = [tuple(row) for row in con.execute(
                "SELECT fiscal_year, quarter FROM lca_quarters WHERE quarter > 0 "
                "ORDER BY fiscal_year DESC, quarter DESC LIMIT ?", (FILING_QUARTERS,))][::-1]
            if not quarters:
                return
            years = sorted({year for year, _ in quarters})
            numbers = sorted({quarter for _, quarter in quarters})
            names = sorted(set(keys.values()))
            counts = {}
            for start in range(0, len(names), 500):
                chunk = names[start:start + 500]
                # Leading with the primary key's year and quarter lets SQLite
                # look rows up instead of scanning every employer.
                query = ("SELECT employer_norm, fiscal_year, quarter, SUM(certified) "
                         "FROM lca_quarter_employers WHERE fiscal_year IN ({}) "
                         "AND quarter IN ({}) AND employer_norm IN ({}) GROUP BY 1, 2, 3").format(
                             ",".join("?" * len(years)), ",".join("?" * len(numbers)),
                             ",".join("?" * len(chunk)))
                for name, year, quarter, certified in con.execute(
                        query, [*years, *numbers, *chunk]):
                    counts[(name, year, quarter)] = certified or 0
    except sqlite3.OperationalError:
        return  # a database loaded before filings were stored per quarter
    metadata["lca_quarters"] = [f"FY{year} Q{quarter}" for year, quarter in quarters]
    for job in matches:
        if id(job) in keys:
            job["lca_by_quarter"] = [counts.get((keys[id(job)], year, quarter), 0)
                                     for year, quarter in quarters]


class Dashboard:
    def __init__(self, db_path, workspace, profile_path=None, companies=None):
        self.db_path = Path(db_path).resolve()
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.profile_path = Path(profile_path).resolve() if profile_path else (
            self.workspace / "profile.json")
        self.companies = Path(companies or "companies.yaml").resolve()
        self.owner = str(self.profile_path)
        self.store = ApplicationStore(self.workspace / "applications.sqlite3")
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.report_lock = threading.Lock()
        self.cache = None
        self.cache_signature = None
        self.task = {"running": False, "message": "", "error": False}
        self.process = None
        self.closing = False
        self.fetch_logo = fetch_logo  # replaced in tests, which never touch the network
        self.fetch_logo_url = fetch_logo_url
        self.logo_locks = {}
        self._domains, self._domains_signature = {}, None
        self._logo_sources = {}
        if profile_path:
            load_profile(self.profile_path)

    def company_domains(self):
        """Company name (as reports show it) to web domain, from companies.yaml."""
        try:
            stat = self.companies.stat()
        except OSError:
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._domains_signature != signature:
            import yaml

            domains, sources = {}, {}
            try:
                config = yaml.safe_load(self.companies.read_text(encoding="utf-8")) or {}
                for entries in (config.get("companies") or {}).values():
                    for entry in entries or []:
                        if not isinstance(entry, dict):
                            continue
                        domain = str(entry.get("domain") or "").strip().lower()
                        name = entry.get("name") or entry.get("slug")
                        if name and DOMAIN_RE.match(domain):
                            domains[str(name)] = domain
                            # `logo:` is for sites that turn away anything but a
                            # browser; an https image address replaces the lookup.
                            logo = str(entry.get("logo") or "").strip()
                            if logo.startswith("https://") and len(logo) <= 2048:
                                sources[domain] = logo
            except (OSError, AttributeError, yaml.YAMLError):
                domains, sources = {}, {}  # a broken file means letter tiles, not a broken dashboard
            self._domains, self._logo_sources = domains, sources
            self._domains_signature = signature
        return self._domains

    def _logo_state(self, domain):
        """(cached file or None, whether a recent miss means do not ask again).

        A miss records where it looked, so changing the `logo:` address tries
        again at once instead of waiting out the week.
        """
        folder = self.workspace / "logos"
        for kind in LOGO_TYPES:
            cached = folder / f"{domain}.{kind}"
            if cached.exists():
                return cached, False
        marker = folder / f"{domain}.none"
        source = self._logo_sources.get(domain, "site")
        try:
            recent = time.time() - marker.stat().st_mtime < LOGO_RETRY_SECONDS
            same = marker.read_text(encoding="utf-8").split("\n")[1:2] == [source]
        except OSError:
            return None, False
        return None, recent and same

    def logo(self, domain):
        """(content type, bytes) of a company's logo, fetched once, or None.

        Only domains listed in companies.yaml are fetched, so a crafted URL
        cannot make this server request an arbitrary host.
        """
        if domain not in set(self.company_domains().values()):
            return None
        folder = self.workspace / "logos"
        folder.mkdir(exist_ok=True)
        with self.lock:
            lock = self.logo_locks.setdefault(domain, threading.Lock())
        with lock:  # rows for the same company wait for one fetch
            # A file dropped into logos/ by hand counts like a fetched one.
            cached, skip = self._logo_state(domain)
            if cached:
                return LOGO_TYPES[cached.suffix[1:]], cached.read_bytes()
            if skip:
                return None
            source = self._logo_sources.get(domain)
            found = self.fetch_logo_url(source) if source else self.fetch_logo(domain)
            marker = folder / f"{domain}.none"
            if not found:
                marker.write_text(f"{now_iso()}\n{source or 'site'}\n", encoding="utf-8")
                return None
            kind, data = found
            (folder / f"{domain}.{kind}").write_bytes(data)
            marker.unlink(missing_ok=True)
            return LOGO_TYPES[kind], data

    def profile(self):
        return load_profile(self.profile_path) if self.profile_path.exists() else None

    def save_profile(self, data):
        if not isinstance(data, dict):
            raise ValueError("Supply a profile object.")
        name = data.get("name", "")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError("Enter a name between 1 and 100 characters.")
        authorization = data.get("work_authorization")
        if not isinstance(authorization, str) or authorization not in VALID_WORK_AUTHORIZATION:
            raise ValueError("Choose your work authorization.")
        roles = text_list(data.get("target_roles", []), "roles")
        skill_input = data.get("skills", [])
        if isinstance(skill_input, dict):
            skills = text_list(list(skill_input), "skills")
            if len(skills) != len(skill_input):
                raise ValueError("List each skill only once.")
            if any(type(weight) is not int or weight < 0
                   for weight in skill_input.values()):
                raise ValueError("Each skill weight must be a nonnegative whole number.")
            skill_weights = {name.strip(): weight for name, weight in skill_input.items()}
        else:
            skills = text_list(skill_input, "skills")
            skill_weights = None
        locations = text_list(data.get("preferred_locations", []), "locations")
        if not roles or not skills:
            raise ValueError("Add at least one target role and one skill.")
        notifications = data.get("notifications", {})
        if not isinstance(notifications, dict):
            raise ValueError("Notification preferences must be an object.")
        for key in ("email_enabled", "google_sheets_enabled"):
            if key in notifications and type(notifications[key]) is not bool:
                raise ValueError(f"{key} must be true or false.")
        with self.lock:
            old = self.profile()
            defaults = old or DEFAULT_PROFILE
            experience = bounded_int(
                data, "max_required_experience", defaults["max_required_experience"],
                0, 40, existing=(old or {}).get("max_required_experience"))
            hours = data.get("report_hours", defaults["report_hours"])
            if (type(hours) not in (int, float) or not math.isfinite(hours) or hours <= 0
                    or (not 1 <= hours <= 8760 and hours != (old or {}).get("report_hours"))):
                raise ValueError("report_hours must be between 1 and 8760.")
            minimum = bounded_int(data, "minimum_score", defaults["minimum_score"],
                                  0, 500, existing=(old or {}).get("minimum_score"))
            weights = (old or {}).get("skills", {})
            built = build_profile(
                name.strip(), (old or {}).get("profile_id", slugify(name)),
                authorization, roles,
                skill_weights if skill_weights is not None else
                {skill: weights.get(skill, SKILL_WEIGHTS["comfortable"]) for skill in skills}, locations,
                experience, hours)
            if old:
                # Preserve custom filters, tiers, exports and notification preferences.
                updated = copy.deepcopy(old)
                keys = ("name", "work_authorization", "target_roles", "skills",
                        "preferred_locations", "max_required_experience", "report_hours")
                updated.update({key: built[key] for key in keys})
                if old["work_authorization"] != built["work_authorization"]:
                    for key in ("reject_citizenship_required",
                                "reject_permanent_authorization_required",
                                "reject_opt_excluded", "reject_no_future_sponsorship"):
                        updated[key] = built[key]
            else:
                updated = built
            updated["minimum_score"] = minimum
            updated["notifications"].update(notifications)
            validate_profile(updated)
            self.profile_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.profile_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.profile_path)
            self.cache_signature = None
        return updated

    def setup_instructions(self):
        profile = self.profile()
        if profile is None:
            raise ValueError("Create and save your profile before opening setup instructions.")
        windows = os.name == "nt"
        # Both shells, so the page can switch without a round trip; the default
        # is this computer's own.
        return {"text": setup_guide(profile, self.profile_path, windows=windows,
                                   db_path=self.db_path),
                "shell": "windows" if windows else "posix",
                "sections": {shell: setup_sections(profile, self.profile_path,
                                                   windows=shell == "windows",
                                                   db_path=self.db_path)
                             for shell in ("windows", "posix")}}

    def data_status(self):
        result = {"jobs": 0, "employers": 0, "companies": 0, "last_fetched": None,
                  "failed_boards": 0, "failed_board_names": [], "error": None}
        if not self.db_path.exists():
            return result
        try:
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as con:
                for table, name in (("jobs", "jobs"), ("employers", "employers"),
                                    ("fetch_failures", "failed_boards")):
                    try:
                        result[name] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except sqlite3.OperationalError:
                        pass
                try:
                    result["last_fetched"], result["companies"] = con.execute(
                        "SELECT MAX(fetched_at), COUNT(DISTINCT company_norm) "
                        "FROM fetched_companies").fetchone()
                except sqlite3.OperationalError:
                    pass
                try:
                    # Boards are stored as "workday/cisco/wd5/site"; the second
                    # part names the company, which is what the user recognizes.
                    result["failed_board_names"] = sorted({
                        (board.split("/") + [board])[1] for (board,) in
                        con.execute("SELECT board FROM fetch_failures LIMIT 50")})
                except sqlite3.OperationalError:
                    pass
        except sqlite3.Error:
            result["error"] = "The job database could not be read. Check the database path."
        return result

    def bootstrap(self):
        from sponsor_daily_report import ROLE_DEFINITIONS

        profile = self.profile()
        warnings = authorization_warnings(profile) if profile else []
        if profile:
            unrecognized = unknown_skills(profile["skills"])
            if unrecognized:
                warnings.append("These skills use exact text matching: " + ", ".join(unrecognized))
        return {"token": self.token, "profile": profile, "warnings": warnings,
                "roles": [row[0] for row in ROLE_DEFINITIONS],
                "skill_levels": [{"label": name.title(), "weight": SKILL_WEIGHTS[name]}
                                 for name in ("strong", "comfortable", "familiar")],
                "profile_defaults": {key: value for key, value in DEFAULT_PROFILE.items()
                                     if key.startswith("reject_") or key in (
                                         "max_required_experience", "report_hours", "minimum_score")},
                "skills": list(known_skill_names()), "data": self.data_status(),
                "task": dict(self.task), "statuses": list(STATUSES),
                "logos": self.logo_urls()}

    def logo_urls(self):
        """Company name to logo URL, leaving out sites already known to have none,
        so the page does not ask for them on every load."""
        urls = {}
        for name, domain in self.company_domains().items():
            cached, skip = self._logo_state(domain)
            if cached or not skip:
                urls[name] = f"/logo/{domain}"
        return urls

    def report(self):
        from sponsor_daily_report import generate_report

        profile = self.profile()
        if profile is None:
            return {"matches": [], "dropped": [], "counts": {}, "metadata": {},
                    "message": "Create your profile to see matching jobs."}
        if not self.db_path.exists() or not self.data_status()["jobs"]:
            return {"matches": [], "dropped": [], "counts": {}, "metadata": {},
                    "message": "Fetch jobs to build your first shortlist."}
        with self.report_lock:
            # A new fetch or profile edit rebuilds at once. Otherwise the posting
            # window (days long) moves slowly, so a report is reused for 15
            # minutes; building one over thousands of jobs takes seconds.
            signature = (self.db_path.stat().st_mtime_ns, self.db_path.stat().st_size,
                         self.profile_path.stat().st_mtime_ns, int(time.time() // 900))
            if self.cache_signature != signature:
                report = generate_report(profile=profile, db_path=self.db_path)
                add_filing_history(report, self.db_path)
                add_fit_breakdown(report, self.db_path, profile)
                self.cache = report
                self.cache_signature = signature
            return self.cache

    def save_application(self, data):
        if not self.profile():
            raise ValueError("Create your profile first.")
        key = data.get("job_key")
        if data.get("remove") is True:
            # Unsaving removes only the tracking record; the job stays in the feed.
            if not isinstance(key, str) or not self.store.delete(self.owner, key):
                raise ValueError("This application was already removed.")
            return {"ok": True}
        old = next((row for row in self.store.list(self.owner)
                    if row["job_key"] == key), None)
        if old:
            job = old
        elif data.get("manual") is True:
            title, company = data.get("title"), data.get("company")
            if any(not isinstance(x, str) or not x.strip() or len(x) > 200
                   for x in (title, company)):
                raise ValueError("Enter a company and job title, at most 200 characters each.")
            url = safe_url(data.get("url", ""))
            if data.get("url") and not url:
                raise ValueError("Use an http:// or https:// posting URL.")
            job = {"job_key": "manual:" + uuid.uuid4().hex, "title": title.strip(),
                   "company": company.strip(), "url": url, "source": "Added by you",
                   "location": "", "posted": ""}
        else:
            job = next((row for row in self.report()["matches"] if row["job_key"] == key), None)
            if job is None:
                raise ValueError("This job is no longer in the feed. Refresh or add it manually.")
        self.store.save(self.owner, job, data.get("status", (old or {}).get("status", "saved")),
                        data.get("notes", (old or {}).get("notes", "")))
        return {"ok": True}

    def start_task(self, action):
        commands = {
            "fetch": ["fetch-jobs", "--replace", "--companies", str(self.companies)],
            "history": ["load-lca", "--prebuilt", "--replace"],
        }
        if not isinstance(action, str) or action not in commands:
            raise ValueError("Choose fetch or history.")
        with self.lock:
            if self.task["running"]:
                raise ValueError("An update is already running. Wait for it to finish.")
            self.task = {"running": True, "error": False, "action": action,
                         "message": "Fetching job boards..." if action == "fetch"
                         else "Loading the prebuilt employer history..."}
        worker = threading.Thread(target=self._run_task, args=(action, commands[action]), daemon=True)
        worker.start()
        return dict(self.task)

    def database_signature(self):
        if not self.db_path.exists():
            return None
        stat = self.db_path.stat()
        return stat.st_mtime_ns, stat.st_size

    def publish_update(self, staging, action, original_signature):
        """Only replace the usable feed after a successful staged collection."""
        with closing(sqlite3.connect(staging)) as source:
            if action == "fetch":
                completed = source.execute("SELECT COUNT(*) FROM fetched_companies").fetchone()[0]
                failed = source.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0]
                if not completed:
                    raise RuntimeError("No job boards completed successfully. Your previous feed was kept. Check your connection and companies.yaml, then retry.")
            else:
                failed = 0
                if not source.execute("SELECT COUNT(*) FROM employers").fetchone()[0]:
                    raise RuntimeError("No employer records were loaded. Your previous data was kept.")
            with self.report_lock:
                if self.database_signature() != original_signature:
                    raise RuntimeError("The job database changed during this update. Your data was kept; retry after the other update finishes.")
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.db_path)) as destination:
                    source.backup(destination)
                self.cache_signature = None
        if not failed:
            return "Update complete. Your matches are ready to refresh."
        boards = "1 job board" if failed == 1 else f"{failed} job boards"
        return f"Update complete, but {boards} did not respond. Fetch again to retry them."

    def _run_task(self, action, command):
        log = self.workspace / "last-update.log"
        staging = self.workspace / ("update-" + uuid.uuid4().hex + ".sqlite3")
        process = None
        result = {"running": False, "error": False, "message": "Update cancelled."}
        try:
            signature = self.database_signature()
            if signature is not None:
                with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(staging)) as destination:
                        source.backup(destination)
            env = dict(os.environ, SPONSORSCAN_DB=str(staging), PYTHONUTF8="1")
            with log.open("w", encoding="utf-8") as output:
                with self.lock:
                    if self.closing:
                        return
                    process = subprocess.Popen(
                        [sys.executable, str(ROOT / "sponsorscan.py"), *command],
                        cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                    self.process = process
                code = process.wait(timeout=1800)
            if code:
                raise RuntimeError("Update failed. Your previous data was kept. See last-update.log in your dashboard workspace.")
            result["message"] = self.publish_update(staging, action, signature)
        except (OSError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            if process and process.poll() is None:
                process.kill()
                process.wait()
            result = {"running": False, "error": True, "message": str(exc)}
        finally:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass  # A cleanup failure must not leave the update marked as running.
            with self.lock:
                self.process = None
                self.task = result

    def close(self):
        with self.lock:
            self.closing = True
            if self.process and self.process.poll() is None:
                self.process.terminate()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, DashboardHandler)

    def server_bind(self):
        if os.name == "nt":
            # Windows address reuse can let an old dashboard share this port.
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Request logs can include private job titles or notes. Keep the terminal quiet.
        pass

    def allowed_request(self):
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{host}" for host in allowed}:
            return False
        # Another site must not be able to call the API or start logo fetches.
        if (self.path.startswith(("/api/", "/logo/"))
                and self.headers.get("Sec-Fetch-Site") == "cross-site"):
            return False
        return True

    PAGE_POLICY = ("default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
                   "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                   "base-uri 'none'; form-action 'self'")
    # Logos come from other sites. Sandboxed, an SVG cannot run script even when
    # its URL is opened directly.
    LOGO_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

    def send(self, code, content, content_type="application/json; charset=utf-8",
             policy=PAGE_POLICY, cache="no-store"):
        if not isinstance(content, bytes):
            content = json.dumps(content, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", policy)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if not self.allowed_request():
            self.send(403, {"error": "Open the dashboard from its localhost URL."})
            return
        path = urlsplit(self.path).path
        app = self.server.app
        try:
            if path == "/api/bootstrap":
                self.send(200, app.bootstrap())
            elif path == "/api/jobs":
                self.send(200, app.report())
            elif path == "/api/applications":
                self.send(200, {"applications": app.store.list(app.owner)})
            elif path == "/api/task":
                self.send(200, dict(app.task))
            elif path == "/api/setup-guide":
                self.send(200, app.setup_instructions())
            elif path.startswith("/logo/"):
                found = app.logo(path[len("/logo/"):])
                if found:
                    self.send(200, found[1], found[0], policy=self.LOGO_POLICY,
                              cache="private, max-age=86400")
                else:
                    self.send(404, {"error": "No logo for this company."})
            else:
                # A fixed allowlist avoids ever serving profiles, databases or credentials.
                assets = {"/": "index.html", "/index.html": "index.html",
                          "/app.js": "app.js", "/theme.js": "theme.js",
                          "/style.css": "style.css",
                          "/hanken-grotesk.ttf": "hanken-grotesk.ttf"}
                filename = assets.get(path)
                if not filename:
                    self.send(404, {"error": "Not found."})
                    return
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                if filename.endswith(".js"):
                    content_type = "text/javascript"
                self.send(200, (ASSETS / filename).read_bytes(), content_type)
        except (ValueError, ProfileError) as exc:
            self.send(400, {"error": str(exc)})
        except (OSError, sqlite3.Error):
            self.send(503, {"error": "Local data could not be read. Check the profile and database paths, then refresh."})

    def do_POST(self):
        app = self.server.app
        if not self.allowed_request() or not hmac.compare_digest(
                self.headers.get("X-SponsorScan-Token", ""), app.token):
            self.send(403, {"error": "Reload the dashboard before saving changes."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY:
                raise ValueError("Request is empty or too large.")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Use application/json.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Supply a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/profile":
                self.send(200, {"profile": app.save_profile(data)})
            elif path == "/api/application":
                self.send(200, app.save_application(data))
            elif path == "/api/update":
                self.send(202, app.start_task(data.get("action")))
            else:
                self.send(404, {"error": "Not found."})
        except (ValueError, ProfileError, UnicodeDecodeError) as exc:
            self.send(400, {"error": str(exc)})
        except (OSError, sqlite3.Error):
            self.send(503, {"error": "Could not save local data. Check available disk space and folder permissions."})


def run_dashboard(args):
    profile_path = args.profile
    if not profile_path and not (Path(args.workspace) / "profile.json").exists():
        candidates = [p for p in Path("profiles").glob("*.json")
                      if not p.name.endswith(".example.json")]
        if len(candidates) == 1:
            profile_path = candidates[0]
    try:
        app = Dashboard(args.db or os.environ.get("SPONSORSCAN_DB", "sponsorscan.db"),
                        args.workspace, profile_path, args.companies)
        server = DashboardServer(("127.0.0.1", args.port), app)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not start the dashboard: {exc}. Check the profile, folder, or choose another --port.")
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"SponsorScan dashboard: {url}\nLocal data: {app.workspace}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped. Your applications are saved.")
    finally:
        app.close()
        server.server_close()
