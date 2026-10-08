"""Onboarding checks and the setup wizard's profile assembly."""

import sqlite3
from datetime import date

import pytest

from profile_loader import VALID_WORK_AUTHORIZATION as WORK_AUTH_SET

from onboarding import (
    check_companies_file,
    known_skill_names,
    suggest_skill,
    unknown_skills,
    warn_unknown_skills,
    check_database,
    check_dependencies,
    format_results,
    build_profile,
    collect_answers,
    find_lca_links,
    format_prompt,
    latest_lca_link,
    run_setup,
    save_profile,
    setup_guide,
    run_checks,
    skill_weight,
    slugify,
    check_jobs_fetched,
    check_lca_loaded,
    check_profile,
    exit_code,
    warn_empty_targeting,
    warn_authorization,
    warn_optional_dependencies,
    warn_notification_env,
    warn_score_threshold,
    warn_stale_jobs,
)


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE employers (
            employer_norm    TEXT PRIMARY KEY,
            employer_display TEXT,
            certified        INTEGER DEFAULT 0
        );
        CREATE TABLE jobs (
            job_key TEXT PRIMARY KEY,
            company TEXT,
            title   TEXT,
            posted  TEXT
        );
    """)
    yield connection
    connection.close()


def test_lca_check_fails_when_no_employers_loaded(con):
    result = check_lca_loaded(con)
    assert result.status == "FAIL"
    assert "load-lca" in result.remedy


def test_lca_check_is_not_needed_for_a_citizen(con):
    result = check_lca_loaded(con, {"work_authorization": "us_citizen"})
    assert result.status == "OK"
    assert "not needed" in result.message.lower()


def test_lca_check_passes_and_reports_the_employer_count(con):
    con.executemany(
        "INSERT INTO employers (employer_norm, certified) VALUES (?, ?)",
        [("acme", 5), ("globex", 12)])
    result = check_lca_loaded(con)
    assert result.status == "OK"
    assert "2" in result.message


def add_job(con, key, posted):
    con.execute(
        "INSERT INTO jobs (job_key, company, title, posted) VALUES (?, ?, ?, ?)",
        (key, "Acme", "Software Engineer", posted))


def test_jobs_check_fails_when_nothing_has_been_fetched(con):
    result = check_jobs_fetched(con)
    assert result.status == "FAIL"
    assert "fetch-jobs" in result.remedy


def test_jobs_check_passes_and_reports_the_posting_count(con):
    add_job(con, "a", "2026-09-20")
    add_job(con, "b", "2026-09-21")
    result = check_jobs_fetched(con)
    assert result.status == "OK"
    assert "2" in result.message


def test_stale_jobs_warn_when_the_newest_posting_is_over_a_week_old(con):
    add_job(con, "a", "2026-09-01")
    result = warn_stale_jobs(con, today=date(2026, 9, 23))
    assert result.status == "WARN"
    assert "fetch-jobs" in result.remedy


def test_recent_jobs_do_not_warn(con):
    add_job(con, "a", "2026-09-20")
    result = warn_stale_jobs(con, today=date(2026, 9, 23))
    assert result.status == "OK"


# --------------------------------------------------------------- profile checks

def write_profile(tmp_path, data):
    import json
    path = tmp_path / "p.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_profile_check_fails_and_surfaces_the_validation_error(tmp_path):
    path = write_profile(tmp_path, {"profile_id": "x", "minimum_score": -5})
    result = check_profile(path)
    assert result.status == "FAIL"
    assert "minimum_score" in result.message


def test_profile_check_fails_when_the_file_is_absent(tmp_path):
    result = check_profile(tmp_path / "nope.json")
    assert result.status == "FAIL"


def test_profile_check_passes_on_a_valid_profile(tmp_path):
    path = write_profile(tmp_path, {"profile_id": "casey", "work_authorization": "opt"})
    result = check_profile(path)
    assert result.status == "OK"
    assert "casey" in result.message


def test_high_minimum_score_warns(tmp_path):
    assert warn_score_threshold({"minimum_score": 140}).status == "WARN"


def test_default_minimum_score_does_not_warn(tmp_path):
    assert warn_score_threshold({"minimum_score": 95}).status == "OK"


def test_empty_roles_and_skills_warn():
    result = warn_empty_targeting({"target_roles": [], "skills": {}})
    assert result.status == "WARN"


def test_populated_targeting_does_not_warn():
    result = warn_empty_targeting(
        {"target_roles": ["Software Engineer"], "skills": {"Python": 7}})
    assert result.status == "OK"


def test_enabled_email_without_credentials_warns():
    profile = {"notifications": {"email_enabled": True, "google_sheets_enabled": False}}
    result = warn_notification_env(profile, env={})
    assert result.status == "WARN"
    assert "GMAIL_ADDRESS" in result.message


def test_enabled_email_with_credentials_does_not_warn():
    profile = {"notifications": {"email_enabled": True, "google_sheets_enabled": False}}
    env = {"GMAIL_ADDRESS": "a@b.com", "GMAIL_APP_PASSWORD": "x",
           "NOTIFICATION_EMAIL": "c@d.com"}
    assert warn_notification_env(profile, env=env).status == "OK"


SHEETS_CONFIGURED = {"GOOGLE_SERVICE_ACCOUNT_JSON": "{}", "GOOGLE_SPREADSHEET_ID": "x"}


def test_enabled_sheets_without_its_libraries_warns():
    profile = {"notifications": {"email_enabled": False, "google_sheets_enabled": True}}
    result = warn_notification_env(profile, env=SHEETS_CONFIGURED,
                                   modules=("no_such_module_xyz",))
    assert result.status == "WARN"
    assert result.remedy == "pip install -r requirements-sheets.txt"


def test_enabled_sheets_with_its_libraries_does_not_warn():
    profile = {"notifications": {"email_enabled": False, "google_sheets_enabled": True}}
    assert warn_notification_env(profile, env=SHEETS_CONFIGURED,
                                 modules=("json",)).status == "OK"


def test_disabled_notifications_do_not_warn():
    profile = {"notifications": {"email_enabled": False, "google_sheets_enabled": False}}
    assert warn_notification_env(profile, env={}).status == "OK"


# ------------------------------------------------------------- companies.yaml

def test_companies_check_fails_when_the_file_is_missing(tmp_path):
    result = check_companies_file(tmp_path / "absent.yaml")
    assert result.status == "FAIL"
    assert "discover" in result.remedy


def test_companies_check_counts_boards_across_providers(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n"
        "  greenhouse:\n"
        "    - {slug: stripe, name: Stripe}\n"
        "    - {slug: airbnb, name: Airbnb}\n"
        "  lever:\n"
        "    - {slug: netflix, name: Netflix}\n",
        encoding="utf-8")
    result = check_companies_file(path)
    assert result.status == "OK"
    assert "3" in result.message


def test_companies_check_fails_when_no_boards_are_listed(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies: {}\n", encoding="utf-8")
    assert check_companies_file(path).status == "FAIL"


# ------------------------------------------------------------------ exit code

def test_exit_code_is_one_when_any_check_fails():
    from onboarding import CheckResult
    results = [CheckResult("a", "OK", ""), CheckResult("b", "FAIL", "")]
    assert exit_code(results) == 1


def test_warnings_alone_do_not_fail_the_run():
    from onboarding import CheckResult
    results = [CheckResult("a", "OK", ""), CheckResult("b", "WARN", "")]
    assert exit_code(results) == 0


# --------------------------------------------------------- environment checks

def test_dependency_check_fails_and_names_the_missing_module():
    result = check_dependencies(["requests", "a_module_that_does_not_exist"])
    assert result.status == "FAIL"
    assert "a_module_that_does_not_exist" in result.message
    assert "pip install" in result.remedy


def test_dependency_check_passes_when_all_present():
    assert check_dependencies(["json", "sqlite3"]).status == "OK"


def test_database_check_fails_when_the_file_is_absent(tmp_path):
    result = check_database(tmp_path / "missing.db")
    assert result.status == "FAIL"
    assert "load-lca" in result.remedy


def test_database_check_passes_and_reports_the_path(tmp_path):
    db = tmp_path / "sponsorscan.db"
    db.write_bytes(b"x" * 2048)
    result = check_database(db)
    assert result.status == "OK"
    assert "sponsorscan.db" in result.message


def test_contradictory_authorization_warns():
    # An OPT profile that also rejects OPT-excluded postings is consistent;
    # a citizen profile claiming OPT rules is not.
    profile = {
        "work_authorization": "us_citizen",
        "reject_citizenship_required": True,
    }
    assert warn_authorization(profile).status == "WARN"


def test_consistent_authorization_does_not_warn():
    profile = {
        "work_authorization": "opt",
        "reject_citizenship_required": True,
        "reject_permanent_authorization_required": True,
        "reject_opt_excluded": True,
    }
    assert warn_authorization(profile).status == "OK"


# --------------------------------------------------------------------- output

def test_format_shows_status_message_and_remedy():
    from onboarding import CheckResult
    text = format_results([
        CheckResult("LCA data", "OK", "48,201 employers loaded"),
        CheckResult("Live postings", "FAIL", "No postings fetched",
                    "python sponsorscan.py fetch-jobs --replace"),
    ])
    assert "OK" in text
    assert "48,201 employers loaded" in text
    assert "fetch-jobs" in text


def test_format_summarises_counts():
    from onboarding import CheckResult
    text = format_results([
        CheckResult("a", "FAIL", ""),
        CheckResult("b", "WARN", ""),
        CheckResult("c", "OK", ""),
    ])
    assert "1 failed" in text
    assert "1 warning" in text


# --------------------------------------------------------------- the full run

def build_db(tmp_path, employers=0, jobs=0):
    db = tmp_path / "sponsorscan.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE employers (employer_norm TEXT PRIMARY KEY, certified INTEGER);
        CREATE TABLE jobs (job_key TEXT PRIMARY KEY, company TEXT, title TEXT,
                           posted TEXT);
    """)
    for i in range(employers):
        connection.execute("INSERT INTO employers VALUES (?, ?)", (f"e{i}", 5))
    for i in range(jobs):
        connection.execute("INSERT INTO jobs VALUES (?, ?, ?, ?)",
                           (f"j{i}", "Acme", "Engineer", "2026-09-22"))
    connection.commit()
    connection.close()
    return db


def test_run_checks_reports_the_earliest_broken_stage(tmp_path):
    db = build_db(tmp_path, employers=10, jobs=0)
    results = run_checks(db_path=db, companies_path=tmp_path / "none.yaml",
                         profile_path=None, env={}, today=date(2026, 9, 23))
    failed = [r for r in results if r.status == "FAIL"]
    assert any("postings" in r.message.lower() for r in failed)
    assert exit_code(results) == 1


def test_run_checks_passes_on_a_complete_setup(tmp_path):
    db = build_db(tmp_path, employers=10, jobs=5)
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n",
        encoding="utf-8")
    profile = write_profile(tmp_path, {"profile_id": "casey",
                                       "work_authorization": "opt",
                                       "target_roles": ["Software Engineer"]})
    results = run_checks(db_path=db, companies_path=companies,
                         profile_path=profile, env={}, today=date(2026, 9, 23))
    assert exit_code(results) == 0


def test_run_checks_passes_for_a_citizen_with_no_lca_data(tmp_path):
    db = build_db(tmp_path, employers=0, jobs=5)
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n",
        encoding="utf-8")
    profile = write_profile(tmp_path, {"profile_id": "casey",
                                       "work_authorization": "us_citizen",
                                       "reject_citizenship_required": False,
                                       "target_roles": ["Software Engineer"]})
    results = run_checks(db_path=db, companies_path=companies,
                         profile_path=profile, env={}, today=date(2026, 9, 23))
    assert exit_code(results) == 0, format_results(results)


def test_run_checks_without_a_profile_skips_profile_checks(tmp_path):
    db = build_db(tmp_path, employers=10, jobs=5)
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n",
        encoding="utf-8")
    results = run_checks(db_path=db, companies_path=companies,
                         profile_path=None, env={}, today=date(2026, 9, 23))
    assert exit_code(results) == 0
    assert not any(r.name == "Profile" and r.status == "FAIL" for r in results)


def test_missing_optional_module_warns_rather_than_failing():
    # rapidfuzz is guarded by HAVE_RAPIDFUZZ and openpyxl is imported lazily,
    # so their absence degrades behaviour instead of breaking the pipeline.
    result = warn_optional_dependencies({"absent_module": "fuzzy matching"})
    assert result.status == "WARN"
    assert "absent_module" in result.message
    assert "fuzzy matching" in result.message


def test_present_optional_modules_do_not_warn():
    assert warn_optional_dependencies({"json": "whatever"}).status == "OK"


# ------------------------------------------------------------------- CLI wiring

def test_doctor_subcommand_reports_a_missing_database(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(repo / "sponsorscan.py"), "doctor",
         "--db", str(tmp_path / "absent.db")],
        capture_output=True, text=True, cwd=str(repo))
    assert result.returncode == 1
    assert "load-lca" in result.stdout


def test_doctor_subcommand_succeeds_on_a_healthy_setup(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    db = build_db(tmp_path, employers=10, jobs=5)
    result = subprocess.run(
        [sys.executable, str(repo / "sponsorscan.py"), "doctor", "--db", str(db)],
        capture_output=True, text=True, cwd=str(repo))
    assert result.returncode == 0, result.stdout + result.stderr


def test_format_names_the_check_alongside_its_message():
    from onboarding import CheckResult
    text = format_results([CheckResult("Dependencies", "OK", "All installed")])
    assert "Dependencies" in text
    assert "All installed" in text


def test_freshness_message_uses_singular_for_one_day(con):
    add_job(con, "a", "2026-09-22")
    result = warn_stale_jobs(con, today=date(2026, 9, 23))
    assert "1 day old" in result.message
    assert "1 days" not in result.message


# ============================================================ the setup wizard

def test_slugify_lowercases_and_underscores_a_name():
    assert slugify("Pranav Balachander") == "pranav_balachander"


def test_slugify_strips_punctuation_and_collapses_gaps():
    assert slugify("  Mary-Jane  O'Brien! ") == "mary_jane_obrien"


def test_slugify_falls_back_when_nothing_usable_remains():
    assert slugify("!!!") == "candidate"


def test_skill_labels_map_to_the_weights_the_examples_already_use():
    assert skill_weight("1") == 7   # Strong
    assert skill_weight("2") == 5   # Comfortable
    assert skill_weight("3") == 3   # Familiar


def test_unrecognised_skill_rating_defaults_to_the_middle():
    assert skill_weight("") == 5
    assert skill_weight("banana") == 5


def test_built_profile_passes_the_real_validator():
    from profile_loader import validate_profile
    profile = build_profile(
        name="Casey Jones",
        profile_id="casey",
        work_authorization="opt",
        target_roles=["Software Engineer"],
        skills={"Python": 7},
        preferred_locations=["Remote"],
        max_required_experience=1,
        report_hours=48)
    validate_profile(profile)          # raises if invalid
    assert profile["name"] == "Casey Jones"
    assert profile["skills"] == {"Python": 7}


def test_output_filenames_derive_from_the_profile_id():
    profile = build_profile(
        name="Casey", profile_id="casey", work_authorization="opt",
        target_roles=[], skills={}, preferred_locations=[],
        max_required_experience=1, report_hours=48)
    files = profile["output_files"]
    assert files["all_matches"] == "casey_matches_48h.csv"
    assert files["new_matches"] == "casey_new_jobs_48h.csv"
    assert files["state"] == ".sponsorscan_casey_state.json"


def test_built_profile_keeps_notifications_off_by_default():
    profile = build_profile(
        name="Casey", profile_id="casey", work_authorization="opt",
        target_roles=[], skills={}, preferred_locations=[],
        max_required_experience=1, report_hours=48)
    assert profile["notifications"] == {"email_enabled": False,
                                        "google_sheets_enabled": False}


def scripted(answers):
    """An `ask` callable that replays answers, then falls back to defaults."""
    remaining = iter(answers)

    def ask(prompt, default=""):
        try:
            return next(remaining)
        except StopIteration:
            return default

    return ask


def test_work_authorization_menu_covers_every_valid_value():
    from onboarding import WORK_AUTHORIZATION_CHOICES
    from profile_loader import VALID_WORK_AUTHORIZATION
    assert set(WORK_AUTHORIZATION_CHOICES) == VALID_WORK_AUTHORIZATION


def test_collect_answers_reads_a_full_session():
    answers = collect_answers(scripted([
        "Casey Jones",                      # name
        "casey",                            # profile_id
        "1",                                # work authorization -> first choice
        "Software Engineer, Data Engineer",  # target roles
        "Python, SQL",                      # skills
        "1",                                # Python -> Strong
        "3",                                # SQL    -> Familiar
        "Remote, Texas",                    # locations
        "2",                                # max experience
        "72",                               # report hours
    ]))
    from onboarding import WORK_AUTHORIZATION_CHOICES
    assert answers["name"] == "Casey Jones"
    assert answers["profile_id"] == "casey"
    assert answers["work_authorization"] == WORK_AUTHORIZATION_CHOICES[0]
    assert answers["target_roles"] == ["Software Engineer", "Data Engineer"]
    assert answers["skills"] == {"Python": 7, "SQL": 3}
    assert answers["preferred_locations"] == ["Remote", "Texas"]
    assert answers["max_required_experience"] == 2
    assert answers["report_hours"] == 72


def test_blank_answers_fall_back_to_defaults():
    answers = collect_answers(scripted([""] * 12))
    assert answers["profile_id"]                      # never empty
    assert answers["work_authorization"] in WORK_AUTH_SET
    assert answers["max_required_experience"] == 1
    assert answers["report_hours"] == 48


def test_profile_id_defaults_to_a_slug_of_the_name():
    answers = collect_answers(scripted(["Mary-Jane O'Brien", ""]))
    assert answers["profile_id"] == "mary_jane_obrien"


def test_non_numeric_experience_falls_back_rather_than_crashing():
    answers = collect_answers(scripted([
        "Casey", "casey", "1", "", "", "", "banana", "not a number"]))
    assert answers["max_required_experience"] == 1
    assert answers["report_hours"] == 48


def test_collected_answers_build_a_valid_profile():
    from profile_loader import validate_profile
    answers = collect_answers(scripted([
        "Casey", "casey", "1", "Software Engineer", "Python", "1",
        "Remote", "1", "48"]))
    validate_profile(build_profile(**answers))


def test_saved_profile_can_be_loaded_back(tmp_path):
    from profile_loader import load_profile
    profile = build_profile(
        name="Casey", profile_id="casey", work_authorization="opt",
        target_roles=["Software Engineer"], skills={"Python": 7},
        preferred_locations=[], max_required_experience=1, report_hours=48)
    path = save_profile(profile, tmp_path / "casey.json")
    reloaded = load_profile(path)
    assert reloaded["profile_id"] == "casey"
    assert reloaded["skills"] == {"Python": 7}


def test_saving_an_invalid_profile_raises_rather_than_writing(tmp_path):
    from profile_loader import ProfileError
    path = tmp_path / "bad.json"
    with pytest.raises(ProfileError):
        save_profile({"profile_id": ""}, path)
    assert not path.exists()


def test_run_setup_writes_a_loadable_profile(tmp_path):
    from profile_loader import load_profile
    ask = scripted(["Casey Jones", "casey", "1", "Software Engineer",
                    "Python", "1", "Remote", "1", "48"])
    path = run_setup(ask, profiles_dir=tmp_path)
    assert path == tmp_path / "casey.json"
    assert load_profile(path)["name"] == "Casey Jones"


def test_run_setup_asks_before_overwriting_and_honours_a_new_name(tmp_path):
    from profile_loader import load_profile
    (tmp_path / "casey.json").write_text("{}", encoding="utf-8")
    ask = scripted([
        "Casey Jones", "casey", "1", "Software Engineer", "Python", "1",
        "Remote", "1", "48",
        "n", "n",     # no email alerts, no Google Sheet
        "n",          # do not overwrite
        "casey_two",  # use this id instead
    ])
    path = run_setup(ask, profiles_dir=tmp_path)
    assert path == tmp_path / "casey_two.json"
    assert load_profile(path)["profile_id"] == "casey_two"
    # the original file is untouched
    assert (tmp_path / "casey.json").read_text(encoding="utf-8") == "{}"


def test_run_setup_overwrites_when_told_to(tmp_path):
    from profile_loader import load_profile
    (tmp_path / "casey.json").write_text("{}", encoding="utf-8")
    ask = scripted([
        "Casey Jones", "casey", "1", "Software Engineer", "Python", "1",
        "Remote", "1", "48",
        "n", "n",
        "y",
    ])
    path = run_setup(ask, profiles_dir=tmp_path)
    assert path == tmp_path / "casey.json"
    assert load_profile(path)["name"] == "Casey Jones"


def test_setup_subcommand_writes_a_profile_from_stdin(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from profile_loader import load_profile
    repo = Path(__file__).resolve().parent.parent
    answers = "\n".join([
        "Casey Jones", "casey", "1", "Software Engineer", "Python", "1",
        "Remote", "1", "48", "n", "n",
    ]) + "\n"
    result = subprocess.run(
        [sys.executable, str(repo / "sponsorscan.py"), "setup",
         "--profiles-dir", str(tmp_path)],
        input=answers, capture_output=True, text=True, cwd=str(repo))
    assert result.returncode == 0, result.stdout + result.stderr
    written = tmp_path / "casey.json"
    assert written.exists()
    profile = load_profile(written)
    assert profile["name"] == "Casey Jones"
    assert profile["skills"] == {"Python": 7}
    # the wizard tells the user what to run next
    assert "doctor" in result.stdout or "sponsor_daily_report" in result.stdout


def test_blank_answers_leave_notifications_off():
    answers = collect_answers(scripted([""] * 12))
    profile = build_profile(**answers)
    assert profile["notifications"] == {
        "email_enabled": False, "google_sheets_enabled": False}


def test_notification_answers_reach_the_profile(tmp_path):
    from profile_loader import load_profile
    ask = scripted(["Casey", "casey", "1", "Software Engineer", "Python", "1",
                    "Remote", "1", "48", "y", "yes"])
    path = run_setup(ask, profiles_dir=tmp_path)
    assert load_profile(path)["notifications"] == {
        "email_enabled": True, "google_sheets_enabled": True}


def _guide_profile(email=False, sheets=False):
    return build_profile(
        name="Casey", profile_id="casey", work_authorization="opt",
        target_roles=["Software Engineer"], skills={"Python": 7},
        preferred_locations=[], max_required_experience=1, report_hours=48,
        email_enabled=email, google_sheets_enabled=sheets)


def test_guide_without_extras_points_at_the_optional_docs():
    guide = setup_guide(_guide_profile(), "profiles/casey.json")
    assert "doctor --profile profiles/casey.json" in guide
    assert "apppasswords" not in guide
    assert "service account" not in guide
    assert "docs/EMAIL_SETUP.md" in guide


def test_email_guide_explains_the_app_password_and_hides_it():
    guide = setup_guide(_guide_profile(email=True), "p.json", windows=True)
    assert "https://myaccount.google.com/apppasswords" in guide
    assert "https://myaccount.google.com/signinoptions/twosv" in guide
    assert '$env:GMAIL_APP_PASSWORD = Read-Host' in guide
    # the profile's own CSV, not the default matches_48h.csv
    assert '$env:NEW_JOBS_CSV = "casey_new_jobs_48h.csv"' in guide
    assert "send_job_email.py --check" in guide


def test_sheets_guide_walks_through_the_service_account():
    guide = setup_guide(_guide_profile(sheets=True), "p.json", windows=False)
    assert "pip install -r requirements-sheets.txt" in guide
    assert "sheets.googleapis.com" in guide
    assert "service-account.json" in guide
    assert "Editor" in guide
    assert 'export ALL_MATCHES_CSV="casey_matches_48h.csv"' in guide
    assert "update_google_sheet.py --check" in guide
    assert "$env:" not in guide


def test_sheets_guide_moves_the_key_with_the_users_shell():
    windows = setup_guide(_guide_profile(sheets=True), "p.json", windows=True)
    posix = setup_guide(_guide_profile(sheets=True), "p.json", windows=False)
    assert "Move-Item -Destination service-account.json" in windows
    assert "mv " not in windows
    assert 'mv "$(ls -t ~/Downloads/*.json | head -1)"' in posix
    # the address is read from one field, never by printing the whole key
    for guide in (windows, posix):
        assert "['client_email']" in guide


def test_posix_guide_reads_the_password_without_echo():
    guide = setup_guide(_guide_profile(email=True), "p.json", windows=False)
    assert "read -s" in guide
    assert "GMAIL_APP_PASSWORD=" not in guide


def test_missing_notification_env_names_the_guide():
    profile = {"notifications": {"email_enabled": True, "google_sheets_enabled": True}}
    message = warn_notification_env(profile, env={}).message
    assert "docs/EMAIL_SETUP.md" in message
    assert "docs/GOOGLE_SHEETS_SETUP.md" in message


def test_short_defaults_are_shown_inline():
    assert format_prompt("Your name", "Candidate") == "Your name [Candidate]: "


def test_a_prompt_without_a_default_shows_no_brackets():
    assert format_prompt("Which skills?", "") == "Which skills?: "


def test_long_defaults_are_not_repeated_in_brackets():
    # The roles question already lists the default in its body; repeating a
    # six-item list in brackets makes the prompt unreadable.
    long_default = ", ".join(["Software Engineer", "Backend Engineer",
                              "Full Stack Engineer", "Data Engineer"])
    rendered = format_prompt("Roles", long_default)
    assert long_default not in rendered
    assert rendered.endswith(": ")


# =================================================== resolving the DOL download

DOL_PAGE = """
<html><body>
  <h2>Disclosure Data</h2>
  <a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx">FY2025 Q4</a>
  <a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2026_Q1.xlsx">FY2026 Q1</a>
  <a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2026_Q2.xlsx">FY2026 Q2</a>
  <a href="/sites/dolgov/files/ETA/oflc/pdfs/PERM_Disclosure_Data_FY2026_Q2.xlsx">PERM, not LCA</a>
  <a href="/some/other/page.html">Unrelated</a>
</body></html>
"""

BASE = "https://www.dol.gov/agencies/eta/foreign-labor/performance"


def test_finds_only_lca_disclosure_links():
    links = find_lca_links(DOL_PAGE, BASE)
    assert len(links) == 3
    assert all("LCA_Disclosure_Data" in link["url"] for link in links)
    assert not any("PERM" in link["url"] for link in links)


def test_relative_hrefs_become_absolute_urls():
    links = find_lca_links(DOL_PAGE, BASE)
    assert all(link["url"].startswith("https://www.dol.gov/") for link in links)


def test_picks_the_newest_fiscal_year_and_quarter():
    assert latest_lca_link(DOL_PAGE, BASE).endswith("LCA_Disclosure_Data_FY2026_Q2.xlsx")


def test_a_later_fiscal_year_beats_a_higher_quarter():
    page = """
      <a href="/x/LCA_Disclosure_Data_FY2025_Q4.xlsx">old year, late quarter</a>
      <a href="/x/LCA_Disclosure_Data_FY2026_Q1.xlsx">new year, early quarter</a>
    """
    assert latest_lca_link(page, BASE).endswith("FY2026_Q1.xlsx")


def test_no_matching_link_returns_none_rather_than_raising():
    assert latest_lca_link("<html><body>nothing here</body></html>", BASE) is None


def test_case_and_separator_variations_still_match():
    page = '<a href="/x/lca_disclosure_data_fy2026_q3.xlsx">lowercase</a>'
    assert latest_lca_link(page, BASE).endswith("fy2026_q3.xlsx")


def run_cli(*argv):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    return subprocess.run([sys.executable, str(repo / "sponsorscan.py"), *argv],
                          capture_output=True, text=True, cwd=str(repo))


def test_load_lca_advertises_the_latest_flag():
    result = run_cli("load-lca", "--help")
    assert result.returncode == 0
    assert "--latest" in result.stdout


def test_latest_and_an_explicit_path_are_mutually_exclusive():
    result = run_cli("load-lca", "--latest", "somefile.xlsx")
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    # argparse rejects the combination, not the flag itself
    assert "not allowed with" in combined
    assert "unrecognized" not in combined


def test_load_lca_without_a_path_or_latest_explains_itself():
    result = run_cli("load-lca")
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "--latest" in combined


def test_absolute_hrefs_are_preserved():
    # The live DOL page mixes relative paths with fully-qualified URLs, and the
    # newest file is currently one of the absolute ones.
    page = '<a href="https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx">FY2026 Q3</a>'
    assert latest_lca_link(page, BASE) == (
        "https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx")


def test_absolute_and_relative_links_are_ranked_together():
    page = """
      <a href="https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx">newest, absolute</a>
      <a href="/sites/dolgov/files/ETA/oflc/pdfs/LCA_Disclosure_Data_FY2025_Q4.xlsx">older, relative</a>
    """
    assert latest_lca_link(page, BASE).endswith("FY2026_Q3.xlsx")


def test_download_filename_survives_a_doubled_slash():
    # os.path.basename returns "" for this on Windows, treating the doubled
    # slash as a UNC path, so the download would be misnamed.
    from onboarding import local_filename_for
    assert local_filename_for(
        "https://www.dol.gov//media/LCA_Disclosure_Data_FY2026_Q3.xlsx"
    ) == "LCA_Disclosure_Data_FY2026_Q3.xlsx"


def test_download_filename_handles_an_ordinary_path():
    from onboarding import local_filename_for
    assert local_filename_for(
        "https://www.dol.gov/sites/x/LCA_Disclosure_Data_FY2025_Q4.xlsx"
    ) == "LCA_Disclosure_Data_FY2025_Q4.xlsx"


def test_download_filename_falls_back_when_the_url_has_no_file():
    from onboarding import local_filename_for
    assert local_filename_for("https://www.dol.gov/") == "lca_download.xlsx"


# ------------------------------------------------------------ message grammar

def test_single_employer_reads_naturally(con):
    con.execute("INSERT INTO employers (employer_norm, certified) VALUES ('a', 1)")
    assert "1 employer loaded" in check_lca_loaded(con).message


def test_single_posting_reads_naturally(con):
    add_job(con, "a", "2026-09-22")
    assert "1 posting stored" in check_jobs_fetched(con).message


def test_single_board_and_provider_read_naturally(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n",
        encoding="utf-8")
    message = check_companies_file(path).message
    assert "1 board across 1 provider" in message
    assert "providers" not in message


def test_plural_forms_are_unchanged(con, tmp_path):
    con.executemany("INSERT INTO employers (employer_norm, certified) VALUES (?, 1)",
                    [("a",), ("b",)])
    assert "2 employers loaded" in check_lca_loaded(con).message


# ================================================== skill vocabulary guidance

def test_known_skill_names_come_from_the_scorer():
    import sponsor_daily_report
    names = known_skill_names()
    assert set(names) == set(sponsor_daily_report.SKILL_PATTERNS)
    assert "Python" in names


def test_unknown_skills_are_identified():
    assert unknown_skills({"Python": 7, "Postgres": 5, "SQL": 6}) == ["Postgres"]


def test_known_skills_report_nothing_unknown():
    assert unknown_skills({"Python": 7, "Machine Learning": 6}) == []


def test_case_differences_are_not_treated_as_unknown():
    # The scorer's lookup is exact, so a case slip really does fall back to a
    # literal match; it should still be flagged, with the correct spelling.
    assert unknown_skills({"tensorflow": 5}) == ["tensorflow"]
    assert suggest_skill("tensorflow") == "TensorFlow"


def test_a_near_miss_suggests_the_real_name():
    assert suggest_skill("Pythn") == "Python"
    assert suggest_skill("Scikit Learn") == "Scikit-learn"


def test_a_skill_with_no_close_match_suggests_nothing():
    # Nothing resembling Fortran is in the vocabulary.
    assert suggest_skill("Fortran") is None
    assert suggest_skill("COBOL") is None


def test_an_alias_suggests_the_skill_that_already_covers_it():
    # These are not close as strings, but each is already a pattern under the
    # suggested skill, so typing them separately is redundant.
    assert suggest_skill("K8s") == "Kubernetes"
    assert suggest_skill("Golang") == "Go"
    assert suggest_skill("Postgres") == "PostgreSQL"


def test_doctor_warns_about_unknown_skills_in_a_profile():
    result = warn_unknown_skills({"skills": {"Python": 7, "Postgres": 5}})
    assert result.status == "WARN"
    assert "Postgres" in result.message
    assert "literal" in result.message.lower()


def test_doctor_is_quiet_when_every_skill_is_known():
    assert warn_unknown_skills({"skills": {"Python": 7}}).status == "OK"


def test_doctor_is_quiet_when_there_are_no_skills():
    assert warn_unknown_skills({"skills": {}}).status == "OK"


def test_the_warning_includes_a_suggestion_when_one_exists():
    result = warn_unknown_skills({"skills": {"tensorflow": 5}})
    assert "TensorFlow" in result.message


def recording(answers):
    """An `ask` that replays answers and records the prompts it was given."""
    remaining = iter(answers)
    prompts = []

    def ask(prompt, default=""):
        prompts.append(prompt)
        try:
            return next(remaining)
        except StopIteration:
            return default

    return ask, prompts


def test_the_skills_question_shows_the_known_vocabulary():
    ask, prompts = recording(["Casey", "casey", "1", "", "Python", "1", "", "1", "48"])
    collect_answers(ask)
    skills_prompt = next(p for p in prompts if "resume" in p.lower())
    assert "Python" in skills_prompt


def test_the_wizard_flags_a_skill_the_scorer_will_only_match_literally():
    notices = []
    ask = scripted(["Casey", "casey", "1", "", "Postgres", "2", "", "1", "48"])
    collect_answers(ask, notify=notices.append)
    assert any("Postgres" in n for n in notices)
    assert any("literal" in n.lower() for n in notices)


def test_the_wizard_stays_quiet_for_recognised_skills():
    notices = []
    ask = scripted(["Casey", "casey", "1", "", "Python, SQL", "1", "1", "", "1", "48"])
    collect_answers(ask, notify=notices.append)
    assert notices == []


def test_run_checks_reports_unknown_skills(tmp_path):
    db = build_db(tmp_path, employers=10, jobs=5)
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n",
        encoding="utf-8")
    profile = write_profile(tmp_path, {
        "profile_id": "casey", "work_authorization": "opt",
        "skills": {"Postgres": 5}})
    results = run_checks(db_path=db, companies_path=companies,
                         profile_path=profile, env={}, today=date(2026, 9, 23))
    skill_results = [r for r in results if r.name == "Skill names"]
    assert len(skill_results) == 1
    assert skill_results[0].status == "WARN"


def test_run_setup_surfaces_skill_notices(tmp_path):
    notices = []
    ask = scripted(["Casey", "casey", "1", "Software Engineer", "Postgres", "1",
                    "Remote", "1", "48"])
    run_setup(ask, profiles_dir=tmp_path, notify=notices.append)
    assert any("Postgres" in n for n in notices)


def test_setup_subcommand_prints_the_skill_notice(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    answers = "\n".join([
        "Casey", "casey", "1", "Software Engineer", "Postgres", "1",
        "Remote", "1", "48", "n", "n",
    ]) + "\n"
    result = subprocess.run(
        [sys.executable, str(repo / "sponsorscan.py"), "setup",
         "--profiles-dir", str(tmp_path)],
        input=answers, capture_output=True, text=True, cwd=str(repo))
    assert result.returncode == 0, result.stdout + result.stderr
    # the notice itself, not the prompt hint that also contains "literally"
    assert "has no built-in pattern" in result.stdout


def test_missing_database_points_a_citizen_at_fetch_jobs(tmp_path):
    result = check_database(tmp_path / "absent.db", {"work_authorization": "us_citizen"})
    assert result.status == "FAIL"
    assert "fetch-jobs" in result.remedy
