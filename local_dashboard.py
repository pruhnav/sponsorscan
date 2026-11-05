"""Local, single-user dashboard for SponsorScan.

SPONSORSCAN_DB selects the existing job database unless --db is supplied.
Private profile and application data live in --workspace, never in static assets.
This server intentionally binds only to 127.0.0.1. It is not a public hosting API.
"""

from __future__ import annotations

import copy
import hmac
import json
import math
import mimetypes
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from onboarding import (SKILL_WEIGHTS, build_profile, known_skill_names,
                        setup_guide, slugify, unknown_skills)
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
        if profile_path:
            load_profile(self.profile_path)

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
        return {"text": setup_guide(profile, self.profile_path, windows=os.name == "nt",
                                   db_path=self.db_path)}

    def data_status(self):
        result = {"jobs": 0, "employers": 0, "last_fetched": None,
                  "failed_boards": 0, "error": None}
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
                    result["last_fetched"] = con.execute(
                        "SELECT MAX(fetched_at) FROM fetched_companies").fetchone()[0]
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
                "task": dict(self.task), "statuses": list(STATUSES)}

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
            signature = (self.db_path.stat().st_mtime_ns, self.db_path.stat().st_size,
                         self.profile_path.stat().st_mtime_ns, int(time.time() // 60))
            if self.cache_signature != signature:
                self.cache = generate_report(profile=profile, db_path=self.db_path)
                self.cache_signature = signature
            return self.cache

    def save_application(self, data):
        if not self.profile():
            raise ValueError("Create your profile first.")
        key = data.get("job_key")
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
        return (f"Update complete. {failed} boards failed; results are incomplete. Retry to refresh them."
                if failed else "Update complete. Your matches are ready to refresh.")

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
        if self.path.startswith("/api/") and self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return True

    def send(self, code, content, content_type="application/json; charset=utf-8"):
        if not isinstance(content, bytes):
            content = json.dumps(content, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; font-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
            else:
                # A fixed allowlist avoids ever serving profiles, databases or credentials.
                assets = {"/": "index.html", "/index.html": "index.html",
                          "/app.js": "app.js", "/style.css": "style.css",
                          "/instrument-sans-400.ttf": "instrument-sans-400.ttf",
                          "/instrument-sans-500.ttf": "instrument-sans-500.ttf",
                          "/instrument-sans-600.ttf": "instrument-sans-600.ttf"}
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
