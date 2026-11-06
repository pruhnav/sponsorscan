"""Offline dashboard tests: no job-board calls or listening network sockets."""

import copy
import io
import json
import os
import socket
import sqlite3
import struct
from email.message import Message
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from local_dashboard import (ApplicationStore, Dashboard, DashboardHandler, _public_host,
                             DashboardServer, icon_candidates, image_kind, safe_url)
from onboarding import build_profile, save_profile


@pytest.fixture
def app(tmp_path):
    return Dashboard(tmp_path / "jobs.db", tmp_path / "workspace")


@pytest.fixture
def profile_input():
    return {"name": "Test Candidate", "work_authorization": "opt",
            "target_roles": ["Software Engineer"], "skills": ["Python", "SQL"],
            "preferred_locations": ["Boston"], "max_required_experience": 1,
            "report_hours": 168, "minimum_score": 95}


def request(app, path, data=None, host="127.0.0.1:8765", origin=None, token=None,
            headers=None):
    """Exercise the real handlers with streams rather than a bound socket."""
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = SimpleNamespace(app=app, server_port=8765)
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = ""
    handler.command = "GET" if data is None else "POST"
    handler.headers = Message()
    handler.headers["Host"] = host
    for name, value in (headers or {}).items():
        handler.headers[name] = value
    if origin is not None:
        handler.headers["Origin"] = origin
    body = json.dumps(data).encode() if data is not None else b""
    handler.headers["Content-Length"] = str(len(body))
    handler.headers["Content-Type"] = "application/json"
    handler.headers["X-SponsorScan-Token"] = token if token is not None else app.token
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    if data is None:
        handler.do_GET()
    else:
        handler.do_POST()
    head, body = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(head.split(b" ")[1])
    return status, head, body


def test_first_run_is_empty_and_does_not_create_feed(app):
    assert app.profile() is None
    assert app.report()["matches"] == []
    assert app.data_status()["jobs"] == 0
    assert not app.db_path.exists()


def test_server_address_reuse_matches_platform():
    assert DashboardServer.allow_reuse_address is (os.name != "nt")


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_server_sets_windows_exclusive_option_before_binding(monkeypatch, platform):
    events = []
    server = DashboardServer.__new__(DashboardServer)
    server.socket = SimpleNamespace(setsockopt=lambda *args: events.append(("option", args)))
    monkeypatch.setattr("local_dashboard.os", SimpleNamespace(name=platform))
    # The constant is absent on other platforms, so emulate it without a socket.
    monkeypatch.setattr(socket, "SO_EXCLUSIVEADDRUSE", -5, raising=False)
    monkeypatch.setattr(ThreadingHTTPServer, "server_bind",
                        lambda self: events.append(("bind", self)))
    server.server_bind()
    expected = [("option", (socket.SOL_SOCKET, -5, 1))] if platform == "nt" else []
    assert events == expected + [("bind", server)]


def test_profile_onboarding_is_valid_and_reloadable(app, profile_input):
    profile = app.save_profile(profile_input)
    assert profile["skills"] == {"Python": 5, "SQL": 5}
    assert profile["notifications"] == {"email_enabled": False, "google_sheets_enabled": False}
    reopened = Dashboard(app.db_path, app.workspace)
    assert reopened.profile() == profile
    assert app.report()["message"].startswith("Fetch jobs")


def test_new_profile_uses_name_and_wizard_defaults(app, profile_input):
    data = {key: value for key, value in profile_input.items()
            if key not in ("report_hours", "minimum_score", "max_required_experience")}
    profile = app.save_profile(data)
    assert profile["profile_id"] == "test_candidate"
    assert profile["report_hours"] == 48
    assert profile["output_files"]["all_matches"] == "test_candidate_matches_48h.csv"


def test_weighted_skills_and_notification_preferences_round_trip(app, profile_input):
    data = dict(profile_input, skills={"Python": 7, "SQL": 0, "Custom Skill": 42},
                notifications={"email_enabled": True, "google_sheets_enabled": True,
                               "custom_setting": "keep"})
    created = app.save_profile(data)
    assert created["skills"] == data["skills"]
    updated = app.save_profile(dict(profile_input, name="New name",
                                   notifications={"email_enabled": False}))
    assert updated["skills"] == {"Python": 7, "SQL": 0}
    assert updated["notifications"] == {"email_enabled": False,
                                         "google_sheets_enabled": True,
                                         "custom_setting": "keep"}
    assert updated["profile_id"] == created["profile_id"]
    assert updated["output_files"] == created["output_files"]
    assert app.profile() == updated


@pytest.mark.parametrize("field,value,changed", [
    ("minimum_score", 999, 1000),
    ("report_hours", 0.5, 0.25),
    ("report_hours", 10000, 10001),
    ("max_required_experience", 60, 61),
])
def test_existing_out_of_range_settings_survive_unchanged(
        app, profile_input, field, value, changed):
    profile = app.save_profile(profile_input)
    profile[field] = value
    save_profile(profile, app.profile_path)
    assert app.save_profile(dict(profile_input, **{field: value}))[field] == value
    omitted = dict(profile_input)
    omitted.pop(field)
    assert app.save_profile(omitted)[field] == value
    before = app.profile_path.read_bytes()
    with pytest.raises(ValueError):
        app.save_profile(dict(profile_input, **{field: changed}))
    assert app.profile_path.read_bytes() == before


def test_setup_guide_requires_profile_and_never_runs_setup(app, profile_input, monkeypatch):
    status, _, body = request(app, "/api/setup-guide")
    assert status == 400
    assert "save your profile" in json.loads(body)["error"]
    app.save_profile(dict(profile_input, notifications={"email_enabled": True,
                                                       "google_sheets_enabled": True}))
    before = app.profile_path.read_bytes()

    def no_subprocess(*args, **kwargs):
        pytest.fail("Opening setup instructions must not run commands")

    monkeypatch.setattr("local_dashboard.subprocess.Popen", no_subprocess)
    status, _, body = request(app, "/api/setup-guide")
    text = json.loads(body)["text"]
    assert status == 200
    assert "send_job_email.py --check" in text
    assert "update_google_sheet.py --check" in text
    assert "not verified or started" in text
    assert "test_candidate_new_jobs_168h.csv" in text
    assert app.profile_path.read_bytes() == before
    assert not app.db_path.exists()
    assert request(app, "/api/setup-guide", origin="https://example.com")[0] == 403


def test_dashboard_setup_guide_uses_its_database(app, profile_input, monkeypatch):
    app.save_profile(profile_input)
    captured = {}

    def capture_guide(profile, path, **options):
        captured.update(profile=profile, path=path, **options)
        return "Instructions for this database"

    monkeypatch.setattr("local_dashboard.setup_guide", capture_guide)
    guide = app.setup_instructions()
    assert guide["text"] == "Instructions for this database"
    assert captured["db_path"] == app.db_path
    assert captured["path"] == app.profile_path
    # The Integrations page gets the same steps for both shells, using this database.
    assert guide["shell"] in ("windows", "posix")
    for shell in ("windows", "posix"):
        sections = {section["id"]: section for section in guide["sections"][shell]}
        assert {"telegram", "email", "sheets", "schedule", "check"} <= set(sections)
        assert " --db " in sections["check"]["steps"][0]["commands"][0]


def test_bootstrap_has_review_defaults_and_shared_proficiency_levels(app):
    data = app.bootstrap()
    assert data["skill_levels"] == [{"label": "Strong", "weight": 7},
                                     {"label": "Comfortable", "weight": 5},
                                     {"label": "Familiar", "weight": 3}]
    assert data["profile_defaults"]["report_hours"] == 48
    assert data["profile_defaults"]["reject_masters_required"] is True
    assert set(data["profile_defaults"]) == {
        key for key in data["profile_defaults"] if key.startswith("reject_")
    } | {"max_required_experience", "report_hours", "minimum_score"}


def test_edit_preserves_custom_settings_and_adjusts_authorization(tmp_path, profile_input):
    path = tmp_path / "profile.json"
    profile = build_profile("Old", "custom", "opt", ["Data Engineer"], {"Python": 9},
                            [], 1, 48, email_enabled=True)
    profile["reject_masters_required"] = False
    profile["company_tiers"] = {"1": ["Example"]}
    save_profile(profile, path)
    app = Dashboard(tmp_path / "jobs.db", tmp_path / "workspace", path)
    data = dict(profile_input, work_authorization="us_citizen", report_hours=2.5)
    updated = app.save_profile(data)
    assert updated["skills"]["Python"] == 9
    assert updated["notifications"] == profile["notifications"]
    assert updated["output_files"] == profile["output_files"]
    assert updated["company_tiers"] == profile["company_tiers"]
    assert not updated["reject_masters_required"]
    assert not updated["reject_citizenship_required"]
    assert not updated["reject_opt_excluded"]
    assert updated["report_hours"] == 2.5


@pytest.mark.parametrize("changes", [
    {"work_authorization": []}, {"work_authorization": "unknown"}, {"name": ""},
    {"skills": []}, {"skills": [5]}, {"target_roles": []},
    {"skills": {}}, {"skills": {"Python": True}}, {"skills": {"Python": -1}},
    {"skills": {"Python": 5.5}}, {"skills": {"Python": "7"}},
    {"skills": {"": 7}}, {"skills": {"Python": 7, " Python ": 5}},
    {"notifications": None}, {"notifications": []},
    {"notifications": {"email_enabled": 1}},
    {"notifications": {"google_sheets_enabled": "true"}},
    {"minimum_score": True}, {"report_hours": float("nan")},
    {"max_required_experience": -1}, {"report_hours": 0},
])
def test_invalid_profile_never_overwrites_existing(app, profile_input, changes):
    app.save_profile(profile_input)
    before = app.profile_path.read_bytes()
    with pytest.raises(ValueError):
        app.save_profile(dict(profile_input, **changes))
    assert app.profile_path.read_bytes() == before


def test_applications_survive_feed_deletion_and_restart(app, profile_input, monkeypatch):
    app.save_profile(profile_input)
    job = {"job_key": "greenhouse:example:123", "title": "Software Engineer",
           "company": "Example", "url": "https://example.com/jobs/123", "why_ranked": "Python"}
    monkeypatch.setattr(app, "report", lambda: {"matches": [job]})
    app.save_application({"job_key": job["job_key"], "status": "applied", "notes": "Referral sent"})
    first = app.store.list(app.owner)[0]
    app.save_application({"job_key": job["job_key"], "notes": "Interview next week"})
    record = app.store.list(app.owner)[0]
    assert record["status"] == "applied"
    assert record["applied_at"] == first["applied_at"]
    assert record["created_at"] == first["created_at"]
    reopened = Dashboard(app.db_path, app.workspace)
    assert not reopened.db_path.exists()
    assert reopened.store.list(reopened.owner)[0]["notes"] == "Interview next week"


def test_snapshots_are_not_overwritten_and_owners_are_isolated(tmp_path):
    store = ApplicationStore(tmp_path / "workspace.sqlite3")
    job = {"job_key": "1", "title": "Original title", "url": "https://example.com"}
    store.save("a", job, "saved", "")
    store.save("a", dict(job, title="Changed feed title"), "interview", "New notes")
    assert store.list("a")[0]["title"] == "Original title"
    assert store.list("a")[0]["status"] == "interview"
    assert store.list("b") == []


def test_manual_application_and_unsafe_url(app, profile_input):
    app.save_profile(profile_input)
    data = {"manual": True, "title": "Analyst", "company": "Example", "status": "applied"}
    app.save_application(data)
    record = app.store.list(app.owner)[0]
    assert record["job_key"].startswith("manual:")
    assert record["source"] == "Added by you"
    with pytest.raises(ValueError, match="http"):
        app.save_application(dict(data, url="javascript:alert(1)"))
    assert len(app.store.list(app.owner)) == 1


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///secret", "data:text/html,hi", "//evil.com", None])
def test_unsafe_links_are_removed(url):
    assert safe_url(url) == ""


def test_api_access_and_private_file_boundary(app):
    status, headers, body = request(app, "/api/bootstrap")
    assert status == 200
    assert json.loads(body)["token"] == app.token
    assert b"frame-ancestors 'none'" in headers
    for path in ("/../profiles/my.json", "/%2e%2e/sponsorscan.db", "/service-account.json", "/api/unknown"):
        assert request(app, path)[0] == 404
    assert request(app, "/api/bootstrap", host="evil.example:8765")[0] == 403
    assert request(app, "/api/bootstrap", origin="https://evil.example")[0] == 403


def test_profile_write_requires_token_and_same_origin(app, profile_input):
    assert request(app, "/api/profile", profile_input, token="wrong")[0] == 403
    assert request(app, "/api/profile", profile_input, origin="https://evil.example")[0] == 403
    assert not app.profile_path.exists()
    assert request(app, "/api/profile", profile_input, origin="http://localhost:8765")[0] == 200


def test_bad_update_action_is_a_client_error(app):
    assert request(app, "/api/update", {"action": []})[0] == 400


def make_feed(path, successful=1, failed=0, title="Old job"):
    with sqlite3.connect(path) as con:
        con.executescript("""CREATE TABLE jobs (title TEXT);
            CREATE TABLE fetched_companies (company_norm TEXT, fetched_at TEXT);
            CREATE TABLE fetch_failures (board TEXT);
            CREATE TABLE employers (name TEXT);""")
        if title:
            con.execute("INSERT INTO jobs VALUES (?)", (title,))
        for i in range(successful):
            con.execute("INSERT INTO fetched_companies VALUES (?, '2026-01-01')", (str(i),))
        for i in range(failed):
            con.execute("INSERT INTO fetch_failures VALUES (?)", (str(i),))


def test_failed_update_preserves_previous_feed(app, tmp_path):
    make_feed(app.db_path)
    staging = tmp_path / "stage.db"
    make_feed(staging, successful=0, failed=2, title=None)
    before = app.db_path.read_bytes()
    with pytest.raises(RuntimeError, match="previous feed"):
        app.publish_update(staging, "fetch", app.database_signature())
    assert app.db_path.read_bytes() == before


def test_partial_update_is_reported_and_success_is_published(app, tmp_path):
    make_feed(app.db_path)
    staging = tmp_path / "stage.db"
    make_feed(staging, successful=1, failed=1, title="New job")
    message = app.publish_update(staging, "fetch", app.database_signature())
    assert "1 job board did not respond" in message
    with sqlite3.connect(app.db_path) as con:
        assert con.execute("SELECT title FROM jobs").fetchone()[0] == "New job"


def test_failed_boards_are_named_by_company(app):
    make_feed(app.db_path)
    with sqlite3.connect(app.db_path) as con:
        con.executemany("INSERT INTO fetch_failures VALUES (?)",
                        [("workday/cisco/wd5/cisco_careers",), ("lever/acme",)])
    status = app.data_status()
    assert status["failed_boards"] == 2
    assert status["failed_board_names"] == ["acme", "cisco"]


def make_filings(path):
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE lca_quarters (fiscal_year INTEGER, quarter INTEGER);
            CREATE TABLE lca_quarter_employers (fiscal_year INTEGER, quarter INTEGER,
                employer_norm TEXT, certified INTEGER);
            CREATE TABLE perm_quarters (fiscal_year INTEGER, quarter INTEGER);""")
        con.executemany("INSERT INTO lca_quarters VALUES (?, ?)",
                        [(2025, 4), (2026, 1), (2026, 2), (2025, 0)])
        con.executemany("INSERT INTO lca_quarter_employers VALUES (?, ?, ?, ?)", [
            (2025, 4, "acme", 3), (2026, 2, "acme", 5), (2025, 0, "acme", 9),
            (2026, 1, "other", 7)])


def test_filing_history_is_aligned_to_loaded_quarters(tmp_path):
    from local_dashboard import add_filing_history
    from sponsorscan import norm_employer

    db = tmp_path / "filings.db"
    make_filings(db)
    name = next(display for display in ("Acme", "Acme Inc", "ACME")
                if norm_employer(display) == "acme")
    report = {"matches": [{"lca_employer": name}, {"lca_employer": ""}], "metadata": {}}
    add_filing_history(report, db)
    # Undated filings (quarter 0) appear in no bar; quarters without filings are 0.
    assert report["metadata"]["lca_quarters"] == ["FY2025 Q4", "FY2026 Q1", "FY2026 Q2"]
    assert report["matches"][0]["lca_by_quarter"] == [3, 0, 5]
    assert report["matches"][1]["lca_by_quarter"] == []
    assert report["metadata"]["perm_loaded"] is False


def test_filing_history_tolerates_an_older_database(app):
    make_feed(app.db_path)
    from local_dashboard import add_filing_history
    report = {"matches": [{"lca_employer": "Acme"}], "metadata": {}}
    add_filing_history(report, app.db_path)
    assert report["metadata"]["lca_quarters"] == []
    assert report["matches"][0]["lca_by_quarter"] == []


def test_concurrent_external_update_is_not_overwritten(app, tmp_path):
    make_feed(app.db_path)
    before = app.db_path.read_bytes()
    staging = tmp_path / "stage.db"
    make_feed(staging)
    with pytest.raises(RuntimeError, match="changed during"):
        app.publish_update(staging, "fetch", (-1, -1))
    assert app.db_path.read_bytes() == before


def test_task_already_running_does_not_start_another(app):
    app.task["running"] = True
    before = copy.deepcopy(app.task)
    with pytest.raises(ValueError, match="already running"):
        app.start_task("fetch")
    assert app.task == before


# ------------------------------------------------------------- company logos
PNG_64 = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 64, 64)
          + b"\x08\x06\x00\x00\x00" + b"\x00" * 24)


def test_image_kind_trusts_bytes_not_labels():
    assert image_kind(PNG_64) == "png"
    assert image_kind(PNG_64[:16] + struct.pack(">II", 16, 16) + PNG_64[24:]) is None
    assert image_kind(b"<!doctype html><html><head></head></html>") is None
    assert image_kind(b'{"token": "secret", "padding": "........"}') is None
    assert image_kind(b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>') == "svg"
    assert image_kind(b"\x00\x00\x01\x00\x01\x00" + bytes([48]) + b"\x00" * 30) == "ico"
    assert image_kind(b"\x00\x00\x01\x00\x01\x00" + bytes([16]) + b"\x00" * 30) is None


def test_icon_candidates_prefer_large_touch_icons():
    html = """<link rel="icon" href="/favicon-16.png" sizes="16x16">
        <link rel="icon" type="image/svg+xml" href="/logo.svg">
        <link rel="apple-touch-icon" href="https://cdn.example.com/touch.png" sizes="180x180">
        <link rel="icon" href="data:image/png;base64,AAAA">
        <link rel="icon" href="icons/192.png" sizes="192x192">"""
    urls = icon_candidates(html, "https://example.com/home/")
    assert urls[:3] == ["https://cdn.example.com/touch.png",
                        "https://example.com/home/icons/192.png", "https://example.com/logo.svg"]
    assert urls.index("https://example.com/apple-touch-icon.png") < urls.index(
        "https://example.com/favicon-16.png")
    assert urls[-1] == "https://example.com/favicon.ico"
    assert not any(url.startswith("data:") for url in urls)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.1.1"])
def test_logo_requests_never_reach_local_addresses(host):
    assert not _public_host(host)


@pytest.fixture
def logo_app(tmp_path):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  ashby:\n"
                         "    - {slug: acme, name: Acme Corp, domain: acme.com}\n"
                         "    - {slug: bad, name: Bad, domain: 'not a domain'}\n"
                         "    - {slug: plain}\n", encoding="utf-8")
    return Dashboard(tmp_path / "jobs.db", tmp_path / "workspace", companies=companies)


def test_logos_are_listed_only_for_valid_domains(logo_app):
    assert logo_app.bootstrap()["logos"] == {"Acme Corp": "/logo/acme.com"}


def test_unlisted_domain_is_never_fetched(logo_app):
    logo_app.fetch_logo = lambda domain: pytest.fail(f"fetched {domain}")
    assert request(logo_app, "/logo/evil.example")[0] == 404


def test_logo_is_fetched_once_and_served_sandboxed(logo_app):
    calls = []
    logo_app.fetch_logo = lambda domain: calls.append(domain) or ("png", PNG_64)
    for _ in range(2):
        status, head, body = request(logo_app, "/logo/acme.com")
        assert status == 200 and body == PNG_64
    assert calls == ["acme.com"]
    assert b"image/png" in head and b"sandbox" in head and b"nosniff" in head


def test_missing_logo_is_not_fetched_on_every_view(logo_app):
    calls = []
    logo_app.fetch_logo = lambda domain: calls.append(domain)
    assert request(logo_app, "/logo/acme.com")[0] == 404
    assert request(logo_app, "/logo/acme.com")[0] == 404
    assert calls == ["acme.com"]
    # The page stops asking for it at all.
    assert logo_app.bootstrap()["logos"] == {}


def test_other_sites_cannot_start_logo_fetches(logo_app):
    logo_app.fetch_logo = lambda domain: pytest.fail("fetched for another site")
    status, _, _ = request(logo_app, "/logo/acme.com", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_removing_an_application_deletes_only_that_record(app, profile_input):
    app.save_profile(profile_input)
    for title in ("Analyst", "Engineer"):
        app.save_application({"manual": True, "title": title, "company": "Example", "status": "applied"})
    first, second = app.store.list(app.owner)
    assert app.save_application({"job_key": first["job_key"], "remove": True}) == {"ok": True}
    assert [row["job_key"] for row in app.store.list(app.owner)] == [second["job_key"]]
    with pytest.raises(ValueError, match="already removed"):
        app.save_application({"job_key": first["job_key"], "remove": True})


def test_fit_breakdown_adds_up_and_names_missing_skills(tmp_path):
    from local_dashboard import add_fit_breakdown

    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE jobs (job_key TEXT, title TEXT, description TEXT)")
        con.execute("INSERT INTO jobs VALUES ('k1', 'Software Engineer', "
                    "'We use Python, SQL, Pandas, NLP and LLMs on Kubernetes.')")
    profile = {"target_roles": ["Software Engineer", "Machine Learning Engineer"],
               "skills": {"Python": 7, "Java": 5, "Pandas/NumPy": 3}}
    report = {"matches": [{"job_key": "k1", "title": "Software Engineer"}]}
    add_fit_breakdown(report, db, profile)
    parts = report["matches"][0]["fit_breakdown"]
    assert parts["role"]["name"] == "Software Engineer"
    assert parts["role"]["max"] >= parts["role"]["points"] > 0
    assert parts["skills"]["matched"] == [["Python", 7], ["Pandas/NumPy", 3]]
    assert parts["skills"]["points"] == 10 and parts["skills"]["listed"] == 3
    missing = parts["missing"]
    assert {"SQL", "Kubernetes", "NLP", "LLMs"} <= set(missing)
    # Neither a listed skill under another name nor a repeat of one already named.
    assert "Pandas" not in missing and "NLP/LLMs" not in missing


@pytest.fixture
def configured_logo_app(tmp_path):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  workday:\n"
                         "    - {slug: cisco/wd5/x, name: Cisco Systems, domain: cisco.com,\n"
                         "       logo: 'https://images.example.org/cisco.png'}\n", encoding="utf-8")
    return Dashboard(tmp_path / "jobs.db", tmp_path / "workspace", companies=companies)


def test_a_configured_logo_is_fetched_instead_of_the_site(configured_logo_app):
    # Sites behind bot protection answer 403 to anything but a browser.
    app = configured_logo_app
    app.fetch_logo = lambda domain: pytest.fail("asked the company's own site")
    fetched = []
    app.fetch_logo_url = lambda url: fetched.append(url) or ("png", PNG_64)
    assert request(app, "/logo/cisco.com")[2] == PNG_64
    assert fetched == ["https://images.example.org/cisco.png"]


def test_a_dropped_in_logo_file_is_used_even_after_a_miss(configured_logo_app):
    app = configured_logo_app
    app.fetch_logo_url = lambda url: None
    assert request(app, "/logo/cisco.com")[0] == 404
    assert app.bootstrap()["logos"] == {}
    (app.workspace / "logos" / "cisco.com.png").write_bytes(PNG_64)
    assert app.bootstrap()["logos"] == {"Cisco Systems": "/logo/cisco.com"}
    assert request(app, "/logo/cisco.com")[2] == PNG_64


def test_changing_the_configured_logo_tries_again(configured_logo_app):
    app = configured_logo_app
    app.fetch_logo_url = lambda url: None
    assert request(app, "/logo/cisco.com")[0] == 404
    app.companies.write_text(app.companies.read_text(encoding="utf-8").replace("cisco.png", "cisco-2.png")
                             + "\n", encoding="utf-8")
    tried = []
    app.fetch_logo_url = lambda url: tried.append(url) or ("png", PNG_64)
    assert app.bootstrap()["logos"] == {"Cisco Systems": "/logo/cisco.com"}
    assert request(app, "/logo/cisco.com")[0] == 200 and tried == ["https://images.example.org/cisco-2.png"]

