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
