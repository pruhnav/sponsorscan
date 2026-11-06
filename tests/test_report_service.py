"""Exercise dashboard reporting against synthetic data without any network."""

import copy
import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

import sponsor_daily_report as report
from profile_loader import DEFAULT_PROFILE
from sponsorscan import SCHEMA


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def no_network(*args, **kwargs):
        pytest.fail("Report generation must not access the network")

    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    db = tmp_path / "jobs #1.db"
    now = datetime.now(timezone.utc)
    with sqlite3.connect(db) as con:
        con.executescript(SCHEMA)
        con.execute(
            "INSERT INTO employers (employer_norm, employer_display, certified, titles) "
            "VALUES (?, ?, ?, ?)",
            ("example", "Example Inc.", 10, '["software engineer"]'))
        for key, title, posted, description in [
            ("current", "Software Engineer", now.isoformat(), "Python. New graduates welcome."),
            ("old", "Software Engineer", (now - timedelta(hours=72)).isoformat(), "Python."),
            ("unknown", "Software Engineer", "", "Python."),
            ("senior", "Senior Software Engineer", now.isoformat(), "Lead the team."),
            ("blocked", "Software Engineer", now.isoformat(), "US citizenship required."),
        ]:
            con.execute(
                "INSERT INTO jobs (job_key, source, company, company_norm, title, "
                "location, url, posted, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, "test", "Example", "example", title, "Seattle, WA",
                 f"https://example.com/jobs/{key}", posted, description))
    profile = copy.deepcopy(DEFAULT_PROFILE)
    profile.update(work_authorization="opt", target_roles=["Software Engineer"],
                   skills={"python": 10}, minimum_score=0)
    profile["output_files"] = {
        "all_matches": str(tmp_path / "all.csv"),
        "new_matches": str(tmp_path / "new.csv"),
        "state": str(tmp_path / "state.json"),
    }
    return db, profile


def test_browsing_preserves_files_profile_and_notification_state(sample, tmp_path, capsys):
    db, profile = sample
    state = tmp_path / "state.json"
    state.write_text('{"version": 3, "seen": {"current": "2026-01-01"}}')
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    original_profile = copy.deepcopy(profile)

    first = report.generate_report(profile, db)
    second = report.generate_report(profile, db)

    assert first["matches"] == second["matches"]
    assert [row["job_key"] for row in first["matches"]] == ["current"]
    assert first["matches"][0]["is_new_since_last_run"] == ""
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before
    assert profile == original_profile
    assert capsys.readouterr().out == ""
    assert first["counts"]["posted_too_old"] == 1
    assert first["counts"]["posted_unknown"] == 1
    assert {row["job_key"] for row in first["dropped"]} == {"senior", "blocked"}
    assert first["metadata"]["total_jobs"] == 5
    json.dumps(first)


def test_service_and_cli_use_the_same_ranking(sample, monkeypatch, tmp_path):
    db, profile = sample
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    expected = report.generate_report(profile, db)
    monkeypatch.setattr(sys, "argv", ["sponsor_daily_report.py", "--db", str(db),
                                     "--profile", str(profile_path)])
    report.main()
    with open(tmp_path / "all.csv", encoding="utf-8-sig", newline="") as handle:
        actual = list(csv.DictReader(handle))
    for row in expected["matches"]:
        row["is_new_since_last_run"] = "YES"
    assert actual == [{key: str(value) for key, value in row.items()}
                      for row in expected["matches"]]
    assert (tmp_path / "state.json").is_file()


def test_explicit_options_override_profile_and_include_unknown_dates(sample):
    db, profile = sample
    profile.update(minimum_score=9999, report_hours=1)
    assert report.generate_report(profile, db)["matches"] == []
    result = report.generate_report(profile, db, hours=96, min_score=0,
                                    include_unknown_posted=True)
    assert {row["job_key"] for row in result["matches"]} == {"current", "old", "unknown"}
    assert result["metadata"]["hours"] == 96
    assert result["metadata"]["minimum_score"] == 0


def test_missing_database_is_actionable_and_never_created(tmp_path):
    db = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="fetch-jobs"):
        report.generate_report(db_path=db)
    assert not db.exists()


@pytest.mark.parametrize("hours", [0, -1, float("nan"), float("inf"), "48"])
def test_invalid_time_window(sample, hours):
    with pytest.raises(ValueError, match="greater than 0"):
        report.generate_report(sample[1], sample[0], hours=hours)


def test_database_is_opened_read_only(sample, monkeypatch):
    connect = sqlite3.connect
    calls = []

    def capture_connection(database, **kwargs):
        calls.append((database, kwargs))
        connection = connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM jobs")
        return connection

    monkeypatch.setattr(report.sqlite3, "connect", capture_connection)
    report.generate_report(sample[1], sample[0])
    assert len(calls) == 1
    assert calls[0][0].endswith("?mode=ro")
    assert calls[0][1] == {"uri": True}


def test_missing_lca_data_is_returned_as_warning(sample, capsys):
    db, profile = sample
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM employers")
    result = report.generate_report(profile, db)
    assert "load-lca" in result["metadata"]["warnings"][0]
    assert result["matches"][0]["sponsorship_score"] == 0
    assert capsys.readouterr().out == ""
