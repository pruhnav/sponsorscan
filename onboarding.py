#!/usr/bin/env python3
"""
Onboarding support for SponsorScan: the `doctor` checks and the `setup` wizard.

The pipeline's usual failure is silence. Every stage is long-running and cached,
so a misconfiguration surfaces as an empty CSV rather than an error, and a user
cannot tell which stage broke. `doctor` inspects each stage and names the one
that needs attention.

Checks are pure functions. They take an open connection, a profile dictionary,
or a path, and return a `CheckResult`. None of them opens a database, reads an
environment variable, or prints, so they can be tested against an in-memory
database with no network and no filesystem.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shlex
import sqlite3
import textwrap
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yaml

from profile_loader import (
    DEFAULT_PROFILE,
    NO_SPONSORSHIP_NEEDED,
    ProfileError,
    authorization_warnings,
    load_profile,
    validate_profile,
)
from skill_patterns import SKILL_PATTERNS

# Postings older than this suggest the jobs table predates the current run.
STALE_AFTER_DAYS = 7

# Above this, a profile filters out nearly everything. Most matches land
# between 95 and 120 against the default scoring.
HIGH_SCORE_THRESHOLD = 130

# Hard imports in sponsorscan.py: without these nothing runs.
REQUIRED_MODULES = ("requests", "yaml")

# Degraded-but-working without them. rapidfuzz is guarded by HAVE_RAPIDFUZZ,
# and openpyxl is imported lazily and only for .xlsx input.
OPTIONAL_MODULES = {
    "openpyxl": "reading .xlsx LCA files; .csv still works",
    "rapidfuzz": "fuzzy employer matching; falls back to exact matches",
}

# Asked as three labels rather than a 1-10 number. score_skills sums matched
# weights against a 42-point cap, so the gap between a 6 and a 7 is noise
# against a default minimum_score of 95. The real risk is self-assessment
# inflation: rate everything 8+ and the cap is reached after five matches, at
# which point every posting scores alike and ranking stops discriminating.
# Three labels force the relative spread the scoring depends on.
SKILL_WEIGHTS = {"1": 7, "2": 5, "3": 3,
                 "strong": 7, "comfortable": 5, "familiar": 3}

DEFAULT_SKILL_WEIGHT = 5

# Longer defaults are spelled out in the question body instead.
MAX_INLINE_DEFAULT = 30

# How many recognised skill names to show at the prompt. The full list is 38,
# too long to read inside a terminal question.
SKILL_HINT_COUNT = 14

# A fixed order, because VALID_WORK_AUTHORIZATION is a set and a numbered menu
# needs the same numbering every run. A test asserts the two stay in step.
WORK_AUTHORIZATION_CHOICES = (
    "opt", "stem_opt", "us_citizen", "permanent_resident", "other")

DEFAULT_TARGET_ROLES = (
    "Software Engineer", "Backend Engineer", "Full Stack Engineer",
    "Data Engineer", "Machine Learning Engineer", "AI Engineer")

DOL_PERFORMANCE_PAGE = (
    "https://www.dol.gov/agencies/eta/foreign-labor/performance")

# Matches the quarterly LCA disclosure filenames, e.g.
# LCA_Disclosure_Data_FY2026_Q2.xlsx. PERM and PW files share the page and are
# deliberately excluded.
LCA_LINK_RE = re.compile(
    r"""href=["']([^"']*LCA_Disclosure_Data_FY(\d{4})_Q(\d)[^"']*\.xlsx)["']""",
    re.I)

EMAIL_ENV = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NOTIFICATION_EMAIL")
SHEETS_ENV = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SPREADSHEET_ID")

# Installed from requirements-sheets.txt, only by those who use the uploader.
# google-api-python-client depends on google-auth, so one name covers both;
# find_spec on a dotted name would raise, not return None, without its parent.
SHEETS_MODULES = ("googleapiclient",)


@dataclass
class CheckResult:
    """One line of `doctor` output.

    `status` is "OK", "WARN", or "FAIL". Only "FAIL" affects the exit code.
    `remedy` is the command that fixes the problem, or None when there is
    nothing to run.
    """

    name: str
    status: str
    message: str
    remedy: str | None = None


def _count(n, singular, plural=None) -> str:
    """"1 employer" / "2 employers", so single-row setups read naturally."""
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def _table_count(con, table: str) -> int | None:
    """Row count for `table`, or None when the table does not exist."""
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return None


def check_lca_loaded(con, profile=None) -> CheckResult:
    """The employers table is populated by `load-lca`. A profile that needs no
    sponsorship does not need it at all."""
    from profile_loader import needs_sponsorship

    count = _table_count(con, "employers")
    if not needs_sponsorship(profile):
        auth = profile.get("work_authorization")
        return CheckResult("LCA data", "OK", f"Not needed for a {auth} profile")
    if not count:
        return CheckResult(
            "LCA data", "FAIL", "No employer records loaded",
            "python sponsorscan.py load-lca <file.xlsx> --replace")
    return CheckResult("LCA data", "OK", f"{_count(count, 'employer')} loaded")


def check_jobs_fetched(con) -> CheckResult:
    """The jobs table is populated by `fetch-jobs`."""
    count = _table_count(con, "jobs")
    if not count:
        return CheckResult(
            "Live postings", "FAIL", "No postings fetched",
            "python sponsorscan.py fetch-jobs --replace")
    return CheckResult("Live postings", "OK", f"{_count(count, 'posting')} stored")


def warn_stale_jobs(con, today: date) -> CheckResult:
    """Warn when the newest posting predates the staleness window.

    `today` is passed in rather than read from the clock so the result is
    reproducible.
    """
    try:
        newest = con.execute(
            "SELECT MAX(posted) FROM jobs WHERE posted IS NOT NULL "
            "AND posted <> ''").fetchone()[0]
    except Exception:
        newest = None

    if not newest:
        return CheckResult("Posting freshness", "OK", "No dated postings to judge")

    try:
        newest_date = datetime.strptime(newest[:10], "%Y-%m-%d").date()
    except ValueError:
        return CheckResult("Posting freshness", "OK", "No usable posting dates")

    age = (today - newest_date).days
    described = f"Newest posting is {age} day{'' if age == 1 else 's'} old"
    if age > STALE_AFTER_DAYS:
        return CheckResult(
            "Posting freshness", "WARN", described,
            "python sponsorscan.py fetch-jobs --replace")
    return CheckResult("Posting freshness", "OK", described)


def check_profile(path) -> CheckResult:
    """The profile parses, merges over the defaults, and validates."""
    try:
        profile = load_profile(path)
    except ProfileError as exc:
        return CheckResult("Profile", "FAIL", str(exc))
    return CheckResult(
        "Profile", "OK",
        f"{profile['profile_id']} ({profile['work_authorization']})")


def warn_score_threshold(profile) -> CheckResult:
    """A hand-raised score floor silently empties the report."""
    score = profile.get("minimum_score", 0)
    if score > HIGH_SCORE_THRESHOLD:
        return CheckResult(
            "Score threshold", "WARN",
            f"minimum_score is {score}; most matches score 95-120")
    return CheckResult("Score threshold", "OK", f"minimum_score is {score}")


def warn_empty_targeting(profile) -> CheckResult:
    """Without roles or skills every posting ranks the same."""
    if not profile.get("target_roles") and not profile.get("skills"):
        return CheckResult(
            "Targeting", "WARN",
            "No target_roles and no skills; every posting will rank alike")
    return CheckResult("Targeting", "OK", "Roles or skills configured")


def warn_notification_env(profile, env, modules=SHEETS_MODULES) -> CheckResult:
    """Notifications are enabled in the profile but unconfigured in the shell."""
    notifications = profile.get("notifications") or {}
    missing, guides = [], []
    if notifications.get("email_enabled"):
        unset = [k for k in EMAIL_ENV if not env.get(k)]
        missing += unset
        if unset:
            guides.append("docs/EMAIL_SETUP.md")
    if notifications.get("google_sheets_enabled"):
        unset = [k for k in SHEETS_ENV if not env.get(k)]
        missing += unset
        if unset:
            guides.append("docs/GOOGLE_SHEETS_SETUP.md")

    if missing:
        return CheckResult(
            "Notification config", "WARN",
            "Enabled in the profile but unset: " + ", ".join(missing)
            + ". Setup steps: " + " and ".join(guides))

    # Telegram has no profile switch: its two variables are the switch. One
    # without the other is a setup stopped halfway, usually before the chat ID.
    telegram = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if env.get(k)]
    if len(telegram) == 1:
        unset = "TELEGRAM_CHAT_ID" if telegram == ["TELEGRAM_BOT_TOKEN"] else "TELEGRAM_BOT_TOKEN"
        return CheckResult(
            "Notification config", "WARN",
            f"{telegram[0]} is set but {unset} is not. Run "
            "python scripts/send_telegram.py --find-chat to get the chat ID. "
            "Setup steps: docs/TELEGRAM_SETUP.md")

    if notifications.get("google_sheets_enabled") and any(
            importlib.util.find_spec(m) is None for m in modules):
        return CheckResult(
            "Notification config", "WARN",
            "Google Sheets is enabled but its client libraries are not installed",
            "pip install -r requirements-sheets.txt")
    return CheckResult("Notification config", "OK", "Nothing missing")


def check_companies_file(path) -> CheckResult:
    """`companies.yaml` parses and lists at least one board."""
    path = Path(path)
    if not path.exists():
        return CheckResult(
            "Company list", "FAIL", f"{path} not found",
            "python sponsorscan.py discover")

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return CheckResult("Company list", "FAIL", f"{path} is not valid YAML: {exc}")

    providers = data.get("companies") or {}
    total = sum(len(entries or []) for entries in providers.values())
    if not total:
        return CheckResult(
            "Company list", "FAIL", f"{path} lists no boards",
            "python sponsorscan.py discover")
    return CheckResult(
        "Company list", "OK",
        f"{_count(total, 'board')} across {_count(len(providers), 'provider')}")


def exit_code(results) -> int:
    """1 when any check failed, so `doctor` works as a CI preflight step."""
    return 1 if any(r.status == "FAIL" for r in results) else 0


def check_dependencies(modules=REQUIRED_MODULES) -> CheckResult:
    """Every module the pipeline imports is installed."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        return CheckResult(
            "Dependencies", "FAIL", "Not installed: " + ", ".join(missing),
            "pip install -r requirements.txt")
    return CheckResult("Dependencies", "OK", "All installed")


def warn_optional_dependencies(modules=OPTIONAL_MODULES) -> CheckResult:
    """Absent optional modules degrade behaviour rather than breaking it."""
    missing = [f"{name} ({why})" for name, why in modules.items()
               if importlib.util.find_spec(name) is None]
    if missing:
        return CheckResult(
            "Optional dependencies", "WARN", "Not installed: " + ", ".join(missing),
            "pip install -r requirements.txt")
    return CheckResult("Optional dependencies", "OK", "All installed")


def check_database(path, profile=None) -> CheckResult:
    """The SQLite file exists. Its contents are judged by the later checks."""
    from profile_loader import needs_sponsorship

    path = Path(path)
    if not path.exists():
        # Whichever command runs first creates the file. A profile that needs
        # no sponsorship never loads LCA data, so fetch-jobs is its first step.
        first = "python sponsorscan.py load-lca <file.xlsx> --replace"             if needs_sponsorship(profile) else "python sponsorscan.py fetch-jobs --replace"
        return CheckResult("Database", "FAIL", f"{path} not found", first)
    size_mb = path.stat().st_size / (1024 * 1024)
    return CheckResult("Database", "OK", f"{path.name} ({size_mb:.1f} MB)")


def warn_authorization(profile) -> CheckResult:
    """Surface contradictions between work_authorization and the reject_ flags."""
    warnings = authorization_warnings(profile)
    if warnings:
        return CheckResult("Authorization", "WARN", " ".join(warnings))
    return CheckResult("Authorization", "OK", "Consistent with the reject flags")


def format_results(results) -> str:
    """Render check results as the aligned block `doctor` prints."""
    lines = []
    for r in results:
        lines.append(f"  {r.status:<5} {r.name:<22} {r.message}".rstrip())
        if r.remedy:
            lines.append(f"        -> {r.remedy}")

    failed = sum(1 for r in results if r.status == "FAIL")
    warned = sum(1 for r in results if r.status == "WARN")

    if not failed and not warned:
        summary = "All checks passed"
    else:
        parts = []
        if failed:
            parts.append(f"{failed} failed")
        if warned:
            parts.append(f"{warned} warning" + ("s" if warned != 1 else ""))
        summary = ", ".join(parts)

    return "\n".join(lines) + "\n\n" + summary


def run_checks(db_path, companies_path, profile_path, env, today) -> list[CheckResult]:
    """Run every check in pipeline order and return the results.

    Checks are ordered so the first failure is the earliest broken stage, but
    every check still runs, so one invocation shows the whole picture. Stages
    that cannot be reached are skipped rather than reported as failures: with
    no database there is nothing to say about its contents.
    """
    results = [check_dependencies(), warn_optional_dependencies()]

    # Read early because whether LCA data is required depends on the profile.
    profile_result = check_profile(profile_path) if profile_path is not None else None
    profile = load_profile(profile_path) \
        if profile_result is not None and profile_result.status == "OK" else None

    db_result = check_database(db_path, profile)
    results.append(db_result)

    if db_result.status == "OK":
        con = sqlite3.connect(db_path)
        try:
            results.append(check_lca_loaded(con, profile))
            results.append(check_jobs_fetched(con))
            results.append(warn_stale_jobs(con, today=today))
        finally:
            con.close()

    results.append(check_companies_file(companies_path))

    if profile_result is not None:
        results.append(profile_result)
        if profile is not None:
            results.append(warn_score_threshold(profile))
            results.append(warn_empty_targeting(profile))
            results.append(warn_unknown_skills(profile))
            results.append(warn_authorization(profile))
            results.append(warn_notification_env(profile, env=env))

    return results


# ------------------------------------------------------------ the setup wizard

def slugify(name: str) -> str:
    """A short, filename-safe id derived from a display name."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").lower().replace("'", ""))
    return cleaned.strip("_") or "candidate"


def skill_weight(answer: str) -> int:
    """Map a rating answer to the weight the shipped examples already use."""
    return SKILL_WEIGHTS.get((answer or "").strip().lower(), DEFAULT_SKILL_WEIGHT)


def build_profile(name, profile_id, work_authorization, target_roles, skills,
                  preferred_locations, max_required_experience,
                  report_hours, email_enabled=False,
                  google_sheets_enabled=False) -> dict:
    """Assemble a complete profile from the wizard's answers.

    Pure: no prompting, no filesystem. Every field the wizard does not ask
    about keeps its DEFAULT_PROFILE value, so the written file stays a full
    profile and the format is unchanged.
    """
    profile = copy.deepcopy(DEFAULT_PROFILE)
    hours = f"{report_hours:g}"
    profile.update({
        "profile_id": profile_id,
        "name": name,
        "work_authorization": work_authorization,
        "target_roles": list(target_roles),
        "skills": dict(skills),
        "preferred_locations": list(preferred_locations),
        "max_required_experience": max_required_experience,
        "report_hours": report_hours,
        "output_files": {
            "all_matches": f"{profile_id}_matches_{hours}h.csv",
            "new_matches": f"{profile_id}_new_jobs_{hours}h.csv",
            "state": f".sponsorscan_{profile_id}_state.json",
        },
        "notifications": {
            "email_enabled": bool(email_enabled),
            "google_sheets_enabled": bool(google_sheets_enabled),
        },
    })
    # The defaults are written for someone who needs sponsorship. Left as they
    # are, a citizen's profile would drop citizen-only postings, and `doctor`
    # would flag the wizard's own output as contradictory.
    if work_authorization in NO_SPONSORSHIP_NEEDED:
        profile.update({
            "reject_citizenship_required": False,
            "reject_permanent_authorization_required": False,
            "reject_opt_excluded": False,
            "reject_no_future_sponsorship": False,
        })
    return profile


def _split_list(text) -> list[str]:
    """Comma-separated free text to a clean list."""
    return [part.strip() for part in (text or "").split(",") if part.strip()]


def _as_number(text, default, cast):
    """Parse a typed answer, falling back rather than raising on nonsense."""
    try:
        return cast(str(text).strip())
    except (TypeError, ValueError):
        return default


def collect_answers(ask, notify=None) -> dict:
    """Run the question sequence and return arguments for `build_profile`.

    `ask(prompt, default)` supplies each answer, so the sequence can be tested
    by replaying a script instead of simulating stdin. A blank answer takes the
    default at every step.
    """
    def asked(prompt, default=""):
        return (ask(prompt, default) or "").strip() or default

    name = asked("Your name", "Candidate")
    profile_id = slugify(asked("Id for your output files", slugify(name)))

    menu = "\n".join(f"  {i}) {choice}"
                     for i, choice in enumerate(WORK_AUTHORIZATION_CHOICES, 1))
    auth_answer = asked(f"Work authorization\n{menu}\nChoose", "1")
    index = _as_number(auth_answer, 1, int)
    if not 1 <= index <= len(WORK_AUTHORIZATION_CHOICES):
        index = 1
    work_authorization = WORK_AUTHORIZATION_CHOICES[index - 1]

    roles = _split_list(asked(
        "Target roles, comma-separated\n  default: "
        + ", ".join(DEFAULT_TARGET_ROLES) + "\nRoles",
        ", ".join(DEFAULT_TARGET_ROLES)))

    notify = notify or (lambda _message: None)

    recognised = ", ".join(known_skill_names()[:SKILL_HINT_COUNT])
    skills = {}
    for skill in _split_list(asked(
            "Which skills are on your resume? (comma-separated)\n"
            f"  recognised names include: {recognised}\n"
            "  anything else is matched literally, so spell it the way postings do"
            "\nSkills")):
        rating = asked(
            f"How would you rate {skill}?\n"
            "  1) Strong - a core skill\n  2) Comfortable\n  3) Familiar\nChoose",
            "2")
        skills[skill] = skill_weight(rating)

    for name in unknown_skills(skills):
        suggestion = suggest_skill(name)
        hint = f" Did you mean {suggestion}?" if suggestion else ""
        notify(f"  note: '{name}' has no built-in pattern, so it is matched "
               f"literally against posting text.{hint}")

    locations = _split_list(asked(
        "Preferred locations, comma-separated; blank means anywhere in the US"))

    max_experience = _as_number(
        asked("Maximum years of experience a posting may require", "1"), 1, int)
    report_hours = _as_number(asked("Report window in hours", "48"), 48, int)

    # Asked here so the profile records the choice and `doctor` checks it.
    # The browser steps themselves come from setup_guide once the file is saved.
    email_enabled = _is_yes(asked(
        "Email you when new jobs appear? Needs a Gmail account; "
        "the steps are shown at the end (y/N)", "n"))
    google_sheets_enabled = _is_yes(asked(
        "Copy results into a Google Sheet? Needs a Google account; "
        "the steps are shown at the end (y/N)", "n"))

    return {
        "name": name,
        "profile_id": profile_id,
        "work_authorization": work_authorization,
        "target_roles": roles,
        "skills": skills,
        "preferred_locations": locations,
        "max_required_experience": max_experience,
        "report_hours": report_hours,
        "email_enabled": email_enabled,
        "google_sheets_enabled": google_sheets_enabled,
    }


def _is_yes(text) -> bool:
    return (text or "").strip().lower().startswith("y")


def save_profile(profile, path) -> Path:
    """Validate, then write. An invalid profile never reaches disk."""
    validate_profile(profile)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return path


def run_setup(ask, profiles_dir="profiles", notify=None) -> Path:
    """Ask the questions, then write the profile, and return where it landed.

    An existing file is never replaced silently: the user is asked, and a
    refusal takes a different id rather than losing the answers just given.
    """
    answers = collect_answers(ask, notify=notify)
    profiles_dir = Path(profiles_dir)
    path = profiles_dir / f"{answers['profile_id']}.json"

    if path.exists():
        reply = (ask(f"{path} already exists. Overwrite? (y/N)", "n") or "n").strip().lower()
        if not reply.startswith("y"):
            replacement = slugify(
                (ask("Use a different id", f"{answers['profile_id']}_2") or "").strip()
                or f"{answers['profile_id']}_2")
            answers["profile_id"] = replacement
            path = profiles_dir / f"{replacement}.json"

    return save_profile(build_profile(**answers), path)


def _shell_quote(value, windows):
    """Quote a literal argument without evaluating profile text as shell code."""
    value = str(value)
    return "'" + value.replace("'", "''") + "'" if windows else shlex.quote(value)


def _shell_lines(env, windows) -> list[str]:
    """Set each variable in the user's shell. A None value is a hidden prompt,
    so a password never lands in the command history."""
    lines = []
    for key, value in env:
        if windows:
            lines.append(f'$env:{key} = Read-Host "{key}"' if value is None
                         else f'$env:{key} = {_shell_quote(value, windows)}')
        else:
            lines.append(f'read -s -p "{key}: " {key}; echo; export {key}'
                         if value is None else f'export {key}={_shell_quote(value, windows)}')
    return lines


def _block(lines, indent=7) -> list[str]:
    return [" " * indent + line for line in lines]


def _profile_file_command(profile, profile_path, windows) -> list[str]:
    """Copy a profile outside profiles/ (the dashboard keeps its own) into the
    folder the workflow reads, then add it past .gitignore."""
    target = f"profiles/{profile['profile_id']}.json"
    lines = []
    if Path(profile_path).as_posix() != target:
        source = _shell_quote(profile_path, windows)
        lines.append(f"Copy-Item {source} {target}" if windows else f"cp {source} {target}")
    return lines + [f"git add -f {target}"]


def setup_sections(profile, profile_path, windows=False, db_path=None) -> list[dict]:
    """Every optional extra as numbered steps, for the dashboard and the terminal.

    Pure, like build_profile: the caller decides the shell. A step has a title,
    notes, an optional link to open, and `commands` to run or `snippet` lines to
    paste into a file. Commands carry this profile's own file names, because the
    scripts otherwise look for the default matches_48h.csv. Passwords and tokens
    are always read by a hidden prompt, so they never land in command history.
    The steps are condensed from the guides each section names.
    """
    outputs = profile["output_files"]
    profile_argument = _shell_quote(profile_path, windows)
    db_argument = f" --db {_shell_quote(db_path, windows)}" if db_path is not None else ""
    new_jobs = _shell_lines([("NEW_JOBS_CSV", outputs["new_matches"])], windows)
    key_secret = ("Get-Content service-account.json -Raw | gh secret set GOOGLE_SERVICE_ACCOUNT_JSON"
                  if windows else "gh secret set GOOGLE_SERVICE_ACCOUNT_JSON < service-account.json")
    copy_workflow = (["New-Item -ItemType Directory -Force .github/workflows | Out-Null",
                      "Copy-Item workflows/sponsorscan.example.yml .github/workflows/sponsorscan.yml"]
                     if windows else
                     ["mkdir -p .github/workflows",
                      "cp workflows/sponsorscan.example.yml .github/workflows/sponsorscan.yml"])
    return [
        {"id": "check", "title": "Check your setup", "guide": "README.md",
         "summary": "Confirm your profile and data, then run a report in the terminal.",
         "intro": [], "steps": [
             {"title": "Check the setup",
              "notes": ["Each problem it finds comes with the fix."],
              "commands": [f"python sponsorscan.py doctor --profile {profile_argument}{db_argument}"]},
             {"title": "Run your first report",
              "notes": ["It writes this profile's CSV files, which the alert scripts read."],
              "commands": [f"python sponsor_daily_report.py --profile {profile_argument}{db_argument}"]},
         ]},
        {"id": "telegram", "title": "Telegram alerts", "guide": "docs/TELEGRAM_SETUP.md",
         "summary": "A message on your phone when new jobs appear. The quickest extra: no Google account.",
         "intro": ["Each run sends one message that lists the new matches, with an Open "
                   "button for each job, so your phone buzzes once per run."],
         "steps": [
             {"title": "Create your bot", "link": "https://t.me/BotFather", "notes": [
                 "In Telegram, open @BotFather (it has a blue verified mark) and send /newbot.",
                 "Choose a display name, then a username that ends in bot, such as "
                 "yourname_sponsorscan_bot.",
                 "BotFather replies with a token like 123456789:AAH... It is the bot's "
                 "password: never paste it into a chat, an issue or a commit."]},
             {"title": "Say hello to your bot", "notes": [
                 "Open the t.me link BotFather gave you and press Start. A bot cannot "
                 "message you first."]},
             {"title": "Find your chat ID", "notes": [
                 "Paste the token when it is asked for; it stays hidden. The script prints "
                 "a line like TELEGRAM_CHAT_ID=987654321."],
              "commands": [*_shell_lines([("TELEGRAM_BOT_TOKEN", None)], windows),
                           "python scripts/send_telegram.py --find-chat"]},
             {"title": "Check, then send a test message", "notes": [
                 "Use the number it printed. A \"SponsorScan is connected\" message should arrive."],
              "commands": [*_shell_lines([("TELEGRAM_CHAT_ID", "<the number it printed>")], windows),
                           "python scripts/send_telegram.py --check",
                           "python scripts/send_telegram.py --test"]},
             {"title": "Send the new jobs after a report", "notes": [
                 "Nothing is sent when there are none."],
              "commands": [*new_jobs, "python scripts/send_telegram.py"]},
         ]},
        {"id": "email", "title": "Email alerts", "guide": "docs/EMAIL_SETUP.md",
         "summary": "New matches in your inbox, sent from your own Gmail account.",
         "intro": [
             "Gmail does not accept your normal password from a script. It needs an App "
             "Password: a separate 16-letter password that only SponsorScan uses.",
             "Before you start: open each link in a browser signed in to the Gmail account "
             "that will SEND the alerts. Your profile picture at the top right of each page "
             "shows which account is active; click it to switch."],
         "steps": [
             {"title": "Turn on 2-Step Verification",
              "link": "https://myaccount.google.com/signinoptions/twosv", "notes": [
                  "Google may ask for your password first.",
                  'If the page says "2-Step Verification is on", go to the next step.',
                  'Otherwise click "Turn on 2-Step Verification" and follow the prompts. '
                  "Google asks for a phone number and texts it a code."]},
             {"title": "Create the App Password",
              "link": "https://myaccount.google.com/apppasswords", "notes": [
                  'In the "App name" box, type SponsorScan and click Create.',
                  'A box titled "Generated app password" shows 16 letters in four groups, '
                  "like: abcd efgh ijkl mnop",
                  "Copy them now. Google never shows them again. If you lose them, delete "
                  "this one on the same page and create another. Then click Done.",
                  'Seeing "The setting you are looking for is not available"? 2-Step '
                  "Verification is still off, or this is a work or school account. Use a "
                  "personal Gmail account."]},
             {"title": "Test the login", "notes": [
                 "Paste the App Password when it is asked for; the spaces do not matter. It "
                 "stays out of your command history, and nothing is sent yet.",
                 '"Check passed. Nothing was sent." means it worked. Use your real addresses '
                 "in place of the examples."],
              "commands": [*_shell_lines([
                  ("GMAIL_ADDRESS", "you@gmail.com"),
                  ("GMAIL_APP_PASSWORD", None),
                  ("NOTIFICATION_EMAIL", "where-alerts-go@example.com"),
              ], windows), "python scripts/send_job_email.py --check"]},
             {"title": "Email the new jobs after a report", "notes": [
                 "Nothing is sent when there are none. Check your spam folder the first time."],
              "commands": [*new_jobs, "python scripts/send_job_email.py"]},
         ]},
        {"id": "sheets", "title": "Google Sheet", "guide": "docs/GOOGLE_SHEETS_SETUP.md",
         "summary": "Every match copied into a spreadsheet you can sort and share.",
         "intro": [
             "SponsorScan writes to the sheet as a service account: a robot Google account "
             "you create, then share the sheet with. It is free; no billing account or "
             "credit card is needed.",
             "Before you start: use a personal Google account. Work and school accounts "
             "often block the key download. Your profile picture at the top right of each "
             "page shows which account is active; click it to switch."],
         "steps": [
             {"title": "Install the Google client libraries",
              "notes": ["Run this in your SponsorScan terminal."],
              "commands": ["pip install -r requirements-sheets.txt"]},
             {"title": "Create a Google Cloud project",
              "link": "https://console.cloud.google.com/projectcreate", "notes": [
                  'First visit only: tick the Terms of Service box and click "Agree and continue".',
                  '"Project name": type SponsorScan. Leave "Location" as it is.',
                  "Click Create and wait about 30 seconds. The bell icon at the top right "
                  "shows when it is ready."]},
             {"title": "Turn on the Google Sheets API",
              "link": "https://console.cloud.google.com/apis/library/sheets.googleapis.com",
              "notes": [
                  'Top left, next to "Google Cloud", the project picker must say SponsorScan. '
                  "If not, click it and choose SponsorScan.",
                  'Click the blue Enable button. It worked when the page shows "API Enabled" '
                  "or a Manage button instead."]},
             {"title": "Create the service account",
              "link": "https://console.cloud.google.com/iam-admin/serviceaccounts", "notes": [
                  "Check the project picker says SponsorScan again.",
                  'Click "+ Create service account" near the top.',
                  '"Service account name": type sponsorscan. The ID fills in by itself.',
                  'Click "Create and continue", then Continue on the "Permissions (optional)" '
                  "step, then Done. No role is needed.",
                  "Back at the list, the new row's Email column ends in .iam.gserviceaccount.com."]},
             {"title": "Download its key", "notes": [
                 "In that list, click the service account's email address, then the Keys tab.",
                 'Click "Add key", then "Create new key". Leave JSON selected and click '
                 "Create. A .json file lands in your Downloads folder.",
                 'Seeing "Service account key creation is disabled"? Your organization blocks '
                 "keys. Start again with a personal account.",
                 "Move the key file you just downloaded into the repository root as "
                 "service-account.json.",
                 "Git ignores that name. Never commit, share or paste its contents."]},
             {"title": "Create the spreadsheet", "link": "https://sheets.new", "notes": [
                 'A blank "Untitled spreadsheet" opens. Click that title at the top left to '
                 "rename it, for example SponsorScan Jobs.",
                 "Copy the whole URL from the address bar. It looks like "
                 "https://docs.google.com/spreadsheets/d/1AbC.../edit"]},
             {"title": "Share it with the service account", "notes": [
                 "Print the service account's address with the command below. It reads only "
                 "that one field of the key.",
                 'In the spreadsheet, click Share and paste the address into "Add people, groups".',
                 "The role beside it must say Editor, not Viewer.",
                 'Untick "Notify people"; the address has no inbox. Then click Share.'],
              "commands": ["python -c \"import json;print(json.load(open("
                           "'service-account.json'))['client_email'])\""]},
             {"title": "Test it", "notes": [
                 "Paste the spreadsheet URL in place of the placeholder. \"Check passed. "
                 "Nothing was written.\" means it worked; anything else names the step to redo."],
              "commands": [*_shell_lines([
                  ("GOOGLE_SERVICE_ACCOUNT_JSON", "service-account.json"),
                  ("GOOGLE_SPREADSHEET_ID", "<paste the spreadsheet URL>"),
              ], windows), "python scripts/update_google_sheet.py --check"]},
             {"title": "Copy the results in after a report", "notes": [
                 "Both tabs are replaced on every run, so keep your own notes, like an "
                 "applied column, in a separate tab."],
              "commands": [*_shell_lines([
                  ("ALL_MATCHES_CSV", outputs["all_matches"]),
                  ("NEW_JOBS_CSV", outputs["new_matches"]),
              ], windows), "python scripts/update_google_sheet.py"]},
         ]},
        {"id": "schedule", "title": "Run on a schedule", "guide": "docs/REFERENCE.md#github-actions-automation",
         "summary": "GitHub runs SponsorScan for you, so alerts arrive with your computer off.",
         "intro": ["The commands in the other sections last only for one terminal window. "
                   "GitHub Actions runs the whole pipeline on its own schedule and sends "
                   "whichever alerts you set up."],
         "steps": [
             {"title": "Use a private repository", "link": "https://github.com/new", "notes": [
                 "The schedule reads your profile from the repository, and it holds your "
                 "name, skills and locations. Run it from a private repository you own.",
                 "A fork of a public repository cannot be made private, so create a new "
                 "private repository and push your copy to it."]},
             {"title": "Add the workflow", "commands": copy_workflow},
             {"title": "Point it at your profile", "notes": [
                 "In .github/workflows/sponsorscan.yml, set these lines in the env: block."],
              "snippet": [f"PROFILE_PATH: profiles/{profile['profile_id']}.json",
                          f"ALL_MATCHES_CSV: {outputs['all_matches']}",
                          f"NEW_JOBS_CSV: {outputs['new_matches']}",
                          f"STATE_FILE: {outputs['state']}"]},
             {"title": "Add your profile to the repository", "notes": [
                 "Profiles are ignored by git, so this adds yours on purpose."],
              "commands": _profile_file_command(profile, profile_path, windows)},
             {"title": "Add the secrets for your extras", "notes": [
                 "Run only the lines for the extras you set up. gh prompts for each "
                 "password and token, so they never appear on screen. Each alert step "
                 "skips itself until its secrets exist."],
              "commands": ["gh auth login",
                           "gh secret set TELEGRAM_BOT_TOKEN",
                           'gh secret set TELEGRAM_CHAT_ID --body "<chat id>"',
                           "gh secret set GMAIL_APP_PASSWORD",
                           'gh secret set GMAIL_ADDRESS --body "you@gmail.com"',
                           'gh secret set NOTIFICATION_EMAIL --body "where-alerts-go@example.com"',
                           key_secret,
                           'gh secret set GOOGLE_SPREADSHEET_ID --body "<spreadsheet URL>"']},
             {"title": "Commit, push and start a run", "notes": [
                 "Each run commits its state file back, so run git pull before you change "
                 "anything locally."],
              "commands": ["git add .github/workflows/sponsorscan.yml",
                           'git commit -m "Run SponsorScan on a schedule"',
                           "git push",
                           "gh workflow run sponsorscan.yml",
                           "gh run watch"]},
         ]},
    ]


def _section_text(section) -> list[str]:
    """One section as plain text for the terminal."""
    out = ["", f"{section['title']} (full guide: {section['guide']})"]
    for paragraph in section["intro"]:
        out += ["", *textwrap.wrap(paragraph, 74)]
    out.append("")
    for number, step in enumerate(section["steps"], 1):
        out.append(f"  {number}. {step['title']}")
        if step.get("link"):
            out.append(" " * 7 + step["link"])
        for note in step.get("notes", []):
            out += textwrap.wrap(note, 76, initial_indent="     - ", subsequent_indent="       ")
        out += _block(step.get("commands", []) + step.get("snippet", []))
        out.append("")
    return out[:-1]


def setup_guide(profile, profile_path, windows=False, db_path=None) -> str:
    """What to do after `setup`, as text: the checks, then each extra chosen.

    The dashboard's Integrations tab shows the same steps from setup_sections,
    with copy buttons, for every extra.
    """
    notifications = profile.get("notifications") or {}
    sections = {section["id"]: section
                for section in setup_sections(profile, profile_path, windows, db_path)}
    out = [
        "Next steps",
        "",
        "Optional selections record your preferences. Connections and scheduled",
        "runs are not verified or started by saving the profile.",
        "",
        "Check the setup, then run your first report:",
        *_block([command for step in sections["check"]["steps"] for command in step["commands"]]),
    ]
    chosen = [key for key, flag in (("email", "email_enabled"), ("sheets", "google_sheets_enabled"))
              if notifications.get(flag)]
    for key in chosen:
        out += _section_text(sections[key])
    if chosen:
        out += [
            "",
            "These settings last only for this terminal window. To run every day",
            "on GitHub Actions, see docs/REFERENCE.md#github-actions-automation.",
        ]
    else:
        out += [
            "",
            "Telegram alerts, email alerts and a Google Sheet are optional. See",
            "docs/TELEGRAM_SETUP.md, docs/EMAIL_SETUP.md and docs/GOOGLE_SHEETS_SETUP.md,",
            "or run setup again to add email or a sheet.",
        ]
    out += [
        "",
        "The dashboard shows these steps with copy buttons: run",
        "python sponsorscan.py dashboard and open Integrations.",
    ]
    return "\n".join(out)


def format_prompt(prompt, default="") -> str:
    """Render one prompt, showing the default only when it stays readable.

    Long defaults are already spelled out in the question body; repeating a
    six-item list in brackets makes the line unreadable.
    """
    if default and len(str(default)) <= MAX_INLINE_DEFAULT:
        return f"{prompt} [{default}]: "
    return f"{prompt}: "


def console_ask(prompt, default=""):
    """Prompt on the terminal, showing the default that a blank answer takes."""
    return input(format_prompt(prompt, default))


# --------------------------------------------------- resolving the DOL download

def find_lca_links(html, base_url=DOL_PERFORMANCE_PAGE) -> list[dict]:
    """Every LCA disclosure link on the page, as absolute URLs."""
    links = []
    for href, year, quarter in LCA_LINK_RE.findall(html or ""):
        links.append({
            "url": urljoin(base_url, href),
            "fiscal_year": int(year),
            "quarter": int(quarter),
        })
    return links


def latest_lca_link(html, base_url=DOL_PERFORMANCE_PAGE) -> str | None:
    """The newest disclosure file on the page, or None when none is found.

    None is the expected answer when the DOL restructures the page, and the
    caller reports the manual path rather than failing with a traceback.
    """
    links = find_lca_links(html, base_url)
    if not links:
        return None
    newest = max(links, key=lambda link: (link["fiscal_year"], link["quarter"]))
    return newest["url"]


def local_filename_for(url) -> str:
    """The filename a download should be saved under.

    os.path.basename is wrong here: on Windows it reads the doubled slash in
    "https://www.dol.gov//media/FILE.xlsx" as a UNC path and returns "", so the
    file would land under a generic fallback name. URL paths are always
    POSIX-shaped, so split on "/" directly.
    """
    path = urlparse(str(url)).path
    return path.rstrip("/").rsplit("/", 1)[-1] or "lca_download.xlsx"


# --------------------------------------------------- skill vocabulary guidance

def known_skill_names() -> tuple[str, ...]:
    """The skill names the scorer has hand-written patterns for."""
    return tuple(SKILL_PATTERNS)


def unknown_skills(skills) -> list[str]:
    """Skill names the scorer has no pattern for.

    These are not worthless: `_compile_skills` falls back to a literal
    word-boundary match on the name, which works when a posting spells the
    skill exactly the way the profile does. It fails silently when it does
    not - "Postgres" never matches a posting that says "PostgreSQL".
    """
    known = set(known_skill_names())
    return [name for name in (skills or {}) if name not in known]


def suggest_skill(name) -> str | None:
    """The known skill that already covers `name`, or None.

    Two ways to be covered. The typed name may be an alias the scorer already
    recognises - "K8s" is a pattern under Kubernetes, "Golang" under Go - in
    which case listing it separately is redundant and the real key is better.
    Otherwise it may simply be misspelled, which difflib catches.

    difflib rather than rapidfuzz, because rapidfuzz is optional and this has
    to work without it.
    """
    import difflib

    text = str(name).strip()
    if not text:
        return None

    if text in SKILL_PATTERNS:
        return None

    # A near-miss on the name itself comes first, because it is the more
    # specific answer: "Postgres" should suggest PostgreSQL rather than SQL,
    # even though SQL's patterns also cover it.
    lowered = {n.lower(): n for n in known_skill_names()}
    matches = difflib.get_close_matches(text.lower(), list(lowered), n=1, cutoff=0.8)
    if matches:
        suggestion = lowered[matches[0]]
        return None if suggestion == text else suggestion

    # Otherwise, an alias the scorer already recognises under another name.
    for skill, patterns in SKILL_PATTERNS.items():
        if any(re.search(p, text, re.I) for p in patterns):
            return skill

    return None


def warn_unknown_skills(profile) -> CheckResult:
    """Flag skills that will only ever be matched literally."""
    unknown = unknown_skills((profile or {}).get("skills"))
    if not unknown:
        return CheckResult("Skill names", "OK", "All recognised by the scorer")

    described = []
    for name in unknown:
        suggestion = suggest_skill(name)
        described.append(f"{name} (did you mean {suggestion}?)" if suggestion else name)

    return CheckResult(
        "Skill names", "WARN",
        "Matched literally, so a posting spelling them differently will not "
        "count: " + ", ".join(described))
