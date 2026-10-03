"""Drive sponsor_daily_report.py as a subprocess against a synthetic database.

The unit tests cover the rules; this covers the wiring, which is where the
profile, the CSV writer and the state file actually meet.
"""

import csv
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

JOBS = [
    # company, title, location, description
    ("Databricks", "Software Engineer, New Grad", "San Francisco, CA",
     "Join us. Our 401(k) vests after 3 years of service."),
    ("Databricks", "Senior Staff Software Engineer", "San Francisco, CA",
     "Lead the team."),
    ("Benchling", "Machine Learning Engineer", "Remote - US",
     "Visa sponsorship is available. Python and PyTorch."),
    ("Acme Defense", "Software Engineer", "Santa Clara, CA",
     "Applicants must be a US citizen."),
    ("Globex", "Software Engineer", "Remote in Europe",
     "Great team, Python."),
    ("Initech", "Research Scientist", "Austin, TX",
     "A PhD in machine learning is required."),
    ("Umbrella", "Software Engineer", "Seattle, WA",
     "We need 8+ years of experience building distributed systems."),
]

EMPLOYERS = [
    # employer_norm, display, certified, denied, withdrawn, titles, lvl1..4
    ("databricks", "DATABRICKS, INC.", 120, 2, 1,
     ["software engineer", "data scientist"], 0, 0, 60, 60),
    ("benchling", "Benchling, Inc.", 8, 0, 0,
     ["research engineer"], 8, 0, 0, 0),
]


@pytest.fixture
def workspace(tmp_path):
    db = tmp_path / "sponsorscan.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE employers (
            employer_norm TEXT PRIMARY KEY, employer_display TEXT,
            certified INTEGER, denied INTEGER, withdrawn INTEGER,
            titles TEXT, states TEXT,
            lvl1 INTEGER, lvl2 INTEGER, lvl3 INTEGER, lvl4 INTEGER);
        CREATE TABLE jobs (
            job_key TEXT PRIMARY KEY, source TEXT, company TEXT,
            company_norm TEXT, title TEXT, location TEXT, url TEXT,
            posted TEXT, description TEXT, fetched_at TEXT);
    """)
    con.executemany(
        "INSERT INTO employers VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(norm, display, cert, den, wd, json.dumps(titles), json.dumps(["CA"]),
          l1, l2, l3, l4)
         for norm, display, cert, den, wd, titles, l1, l2, l3, l4 in EMPLOYERS])

    posted = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sys.path.insert(0, str(REPO))
    from sponsorscan import norm_employer

    con.executemany(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(f"test:{i}", "test", company, norm_employer(company), title, location,
          f"https://example.com/jobs/{i}", posted, description, "2026-09-15")
         for i, (company, title, location, description) in enumerate(JOBS)])
    con.commit()
    con.close()
    return tmp_path


def run_report(workspace, *args):
    result = subprocess.run(
        [sys.executable, str(REPO / "sponsor_daily_report.py"),
         "--db", str(workspace / "sponsorscan.db"),
         "--out", str(workspace / "all.csv"),
         "--new-out", str(workspace / "new.csv"),
         "--state", str(workspace / "state.json"),
         "--hours", "48", "--min-score", "0", *args],
        capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_report_runs_and_filters(workspace):
    run_report(workspace)
    rows = read_csv(workspace / "all.csv")
    titles = {r["title"] for r in rows}

    assert "Software Engineer, New Grad" in titles, "benefit boilerplate dropped a new grad"
    assert "Machine Learning Engineer" in titles

    assert "Senior Staff Software Engineer" not in titles, "senior title kept"
    companies = {r["company"] for r in rows}
    assert "Acme Defense" not in companies, "citizen-only posting kept"
    assert "Globex" not in companies, "'Remote in Europe' treated as US"
    assert "Initech" not in companies, "PhD-required posting kept"
    assert "Umbrella" not in companies, "8-years posting kept"


def test_lca_history_reaches_the_output(workspace):
    run_report(workspace)
    rows = {r["company"]: r for r in read_csv(workspace / "all.csv")}
    assert int(rows["Databricks"]["lca_certified"]) == 120
    assert "certified LCAs" in rows["Databricks"]["why_ranked"]


def test_everything_is_new_on_the_first_run(workspace):
    run_report(workspace)
    assert read_csv(workspace / "all.csv") == read_csv(workspace / "new.csv")
    assert (workspace / "state.json").exists()


def test_second_run_reports_nothing_new(workspace):
    run_report(workspace)
    run_report(workspace)
    assert read_csv(workspace / "new.csv") == []
    assert read_csv(workspace / "all.csv"), "all-matches must stay populated"


def test_reset_state_makes_everything_new_again(workspace):
    run_report(workspace)
    run_report(workspace)
    run_report(workspace, "--reset-state")
    assert read_csv(workspace / "new.csv")


# Tracking across runs. A job leaves the database when its board fails to
# fetch, and comes back on the next good run; that must not read as new.

def add_job(workspace, job_id, company, title="Software Engineer, New Grad",
            location="San Francisco, CA", description="Python.", source="test"):
    from sponsorscan import norm_employer
    posted = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = sqlite3.connect(workspace / "sponsorscan.db")
    con.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"test:{job_id}", source, company, norm_employer(company), title, location,
         f"https://example.com/jobs/{job_id}", posted, description, "2026-09-15"))
    con.commit()
    con.close()


def remove_company_jobs(workspace, company):
    con = sqlite3.connect(workspace / "sponsorscan.db")
    rows = con.execute("SELECT * FROM jobs WHERE company = ?", (company,)).fetchall()
    con.execute("DELETE FROM jobs WHERE company = ?", (company,))
    con.commit()
    con.close()
    return rows


def restore_jobs(workspace, rows):
    con = sqlite3.connect(workspace / "sponsorscan.db")
    con.executemany("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


def new_titles(workspace):
    return {(r["company"], r["title"]) for r in read_csv(workspace / "new.csv")}


def test_failed_board_does_not_resend_its_jobs(workspace):
    run_report(workspace)
    rows = remove_company_jobs(workspace, "Databricks")
    run_report(workspace)
    restore_jobs(workspace, rows)
    run_report(workspace)
    assert new_titles(workspace) == set()


def test_new_posting_at_a_tracked_company_is_new(workspace):
    run_report(workspace)
    add_job(workspace, 100, "Databricks", title="Data Engineer, New Grad")
    run_report(workspace)
    assert new_titles(workspace) == {("Databricks", "Data Engineer, New Grad")}


def test_first_sighting_of_a_company_is_a_silent_baseline(workspace):
    run_report(workspace)
    add_job(workspace, 100, "Stripe")
    output = run_report(workspace)

    assert new_titles(workspace) == set()
    assert ("Stripe", "Software Engineer, New Grad") in {
        (r["company"], r["title"]) for r in read_csv(workspace / "all.csv")}
    assert "1 match at 1 newly tracked company" in output

    add_job(workspace, 101, "Stripe", title="Data Engineer, New Grad")
    run_report(workspace)
    assert new_titles(workspace) == {("Stripe", "Data Engineer, New Grad")}


def test_first_sighting_of_a_company_on_a_feed_is_new(workspace):
    run_report(workspace)
    add_job(workspace, 100, "Stripe", description="", source="speedyapply")
    run_report(workspace)
    assert new_titles(workspace) == {("Stripe", "Software Engineer, New Grad")}


def test_company_fetched_with_no_postings_is_already_tracked(workspace):
    run_report(workspace)
    con = sqlite3.connect(workspace / "sponsorscan.db")
    con.execute("CREATE TABLE fetched_companies (company_norm TEXT PRIMARY KEY, "
                "fetched_at TEXT)")
    con.execute("INSERT INTO fetched_companies VALUES ('stripe', '2026-09-15')")
    con.commit()
    con.close()
    run_report(workspace)

    add_job(workspace, 100, "Stripe")
    run_report(workspace)
    assert new_titles(workspace) == {("Stripe", "Software Engineer, New Grad")}


def test_old_state_format_is_upgraded_without_silencing(workspace):
    run_report(workspace)
    keys = [r["job_key"] for r in read_csv(workspace / "all.csv")]
    (workspace / "state.json").write_text(
        json.dumps({"active_job_keys": keys}), encoding="utf-8")

    add_job(workspace, 100, "Stripe")
    run_report(workspace)
    assert new_titles(workspace) == {("Stripe", "Software Engineer, New Grad")}


def test_long_unseen_keys_are_forgotten(workspace):
    today = datetime.now(timezone.utc).date()
    (workspace / "state.json").write_text(json.dumps({
        "version": 2,
        "seen": {"stale": (today - timedelta(days=400)).isoformat(),
                 "recent": (today - timedelta(days=5)).isoformat()},
        "companies": [],
    }), encoding="utf-8")
    run_report(workspace)

    seen = json.loads((workspace / "state.json").read_text(encoding="utf-8"))["seen"]
    assert "stale" not in seen
    assert "recent" in seen


def citizen_profile(tmp_path):
    profile = tmp_path / "citizen.json"
    profile.write_text(json.dumps({
        "profile_id": "citizen", "work_authorization": "us_citizen",
        "reject_citizenship_required": False}), encoding="utf-8")
    return str(profile)


def test_citizen_ranking_ignores_filing_history(workspace, tmp_path):
    add_job(workspace, 100, "Stripe")  # no LCA history in the fixture
    run_report(workspace, "--profile", citizen_profile(tmp_path))
    rows = {r["company"]: r for r in read_csv(workspace / "all.csv")}
    assert rows["Databricks"]["sponsorship_score"] == rows["Stripe"]["sponsorship_score"]
    assert int(rows["Databricks"]["lca_certified"]) == 120, "history is still shown"


def test_citizen_report_runs_without_lca_data(workspace, tmp_path):
    con = sqlite3.connect(workspace / "sponsorscan.db")
    con.execute("DELETE FROM employers")
    con.commit()
    con.close()
    output = run_report(workspace, "--profile", citizen_profile(tmp_path))
    assert read_csv(workspace / "all.csv")
    assert "No LCA data" not in output


def test_missing_lca_data_warns_a_candidate_who_needs_sponsorship(workspace):
    con = sqlite3.connect(workspace / "sponsorscan.db")
    con.execute("DELETE FROM employers")
    con.commit()
    con.close()
    assert "No LCA data" in run_report(workspace)


def test_profile_drives_roles_and_tiers(workspace, tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "profile_id": "test",
        "work_authorization": "opt",
        "target_roles": ["Machine Learning Engineer"],
        "skills": {"Python": 7, "PyTorch": 6},
        "company_tiers": {"5": ["Benchling"]},
        "preferred_companies": ["Benchling"],
        "output_files": {"all_matches": "a.csv", "new_matches": "n.csv",
                         "state": "s.json"},
    }), encoding="utf-8")

    run_report(workspace, "--profile", str(profile))
    rows = read_csv(workspace / "all.csv")

    assert {r["role_family"] for r in rows} == {"Machine Learning Engineer"}
    row = rows[0]
    assert row["company"] == "Benchling"
    assert row["company_tier"] == "5"
    assert "preferred company" in row["why_ranked"]
    assert "Python" in row["matched_resume_skills"]


def test_contradictory_profile_warns(workspace, tmp_path):
    profile = tmp_path / "citizen.json"
    profile.write_text(json.dumps({
        "profile_id": "citizen",
        "work_authorization": "us_citizen",
        "reject_citizenship_required": True,
    }), encoding="utf-8")

    output = run_report(workspace, "--profile", str(profile))
    assert "warning" in output
    assert "reject_citizenship_required" in output


def test_missing_database_fails_clearly(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPO / "sponsor_daily_report.py"),
         "--db", str(tmp_path / "absent.db")],
        capture_output=True, text=True, cwd=str(REPO))
    assert result.returncode != 0
    assert "absent.db" in result.stderr
