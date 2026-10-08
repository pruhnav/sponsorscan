# Guide for AI assistants

This file is for AI coding assistants (Claude Code, Codex, Cursor, Copilot,
Gemini and others) working in this repository. Most people who open SponsorScan
with an assistant want help **setting it up**, often without much command-line
experience. The playbook below is how to take them through it.

## What SponsorScan is

A job-search tool. It joins the U.S. Department of Labor's H-1B filing data
(LCA disclosures) with live postings from public job boards (Greenhouse, Lever,
Ashby, Workday), then filters and ranks entry-level roles. Everything past the
core report is optional: a personalized profile, email alerts, a Google Sheet,
and a GitHub Actions schedule.

| File | Role |
|---|---|
| `sponsorscan.py` | CLI: `load-lca`, `fetch-jobs`, `report`, `discover`, `setup`, `doctor` |
| `sponsor_daily_report.py` | Personalized report from a profile; writes the all-matches and new-jobs CSVs |
| `onboarding.py` | The `setup` wizard and `doctor` checks |
| `profile_loader.py` | Loads and validates profile JSON |
| `scripts/send_job_email.py` | Emails new jobs through Gmail. `--check` tests the login |
| `scripts/update_google_sheet.py` | Copies both CSVs into a Google Sheet. `--check` tests access |
| `workflows/sponsorscan.example.yml` | GitHub Actions template; copy into `.github/workflows/` |
| `docs/` | Human guides. `REFERENCE.md` documents every flag |

Tests: `pip install -r requirements-dev.txt` then `python -m pytest tests -q`.
`python selftest.py` runs the pipeline offline. Neither touches the network.

## How to help someone set it up

### Ground rules

1. **Ask first what they want.** Work authorization (OPT, STEM OPT, citizen,
   permanent resident, other), and which extras: email alerts, Google Sheets,
   running on a schedule. Set up only what they ask for. Each extra adds
   accounts and secrets.
2. **One step at a time.** Give one action, wait for the result, verify, then
   move on. Several browser steps in one message get skipped.
3. **Run what you can; hand over what you cannot.** You can run commands, edit
   files and read output. The user must do anything in a browser: Google Cloud
   Console, Google account security settings, sharing a spreadsheet, GitHub
   settings. For those, give the exact page, the button names in order, and what
   they should see when it worked.
4. **Never ask for a secret in chat.** That means the Gmail App Password and
   the service-account JSON key. Anything pasted into the conversation is stored
   in its history. Use the patterns in "Handling secrets" below. If a user
   pastes one anyway, tell them to revoke it and create a new one, then carry on.
5. **Never print a secret.** Do not `cat`, `type`, `Get-Content` or `Read` the
   service-account key file, and never echo `GMAIL_APP_PASSWORD`. If you need
   the service account's email address, run `update_google_sheet.py --check`,
   which prints it, or extract only that field:
   `python -c "import json;print(json.load(open('service-account.json'))['client_email'])"`.
6. **Verify every stage with its check command** before calling it done:
   `python sponsorscan.py doctor --profile profiles/<id>.json`,
   `python scripts/send_job_email.py --check`, and
   `python scripts/update_google_sheet.py --check`. Each prints what to fix.
   Relay that fix in plain words instead of guessing.
7. **Mind the shell.** The user is often on Windows PowerShell, where
   `export`, `cp` with globs, and `<` redirection do not work as they do in
   bash. Give commands for their shell. The docs show both forms.

### Handling secrets

Environment variables set in the user's own terminal are not visible to the
commands you run, and yours are not visible to theirs. Choose one of these:

- **Service-account key (Google Sheets).** Ask the user to move the downloaded
  key into the repository root as `service-account.json`. `.gitignore` already
  excludes `service-account*.json`, and you can confirm with
  `git check-ignore -v service-account.json`. `GOOGLE_SERVICE_ACCOUNT_JSON`
  accepts a file path, so you can then run the check yourself without reading
  the key:

  ```powershell
  $env:GOOGLE_SERVICE_ACCOUNT_JSON = "service-account.json"
  $env:GOOGLE_SPREADSHEET_ID = "<id or full URL>"
  python scripts/update_google_sheet.py --check
  ```

- **Gmail App Password.** You never handle it. Give the user this block to run
  in **their own** terminal, then ask them to paste back the output (it never
  contains the password):

  ```powershell
  $env:GMAIL_ADDRESS = "sender@gmail.com"
  $env:GMAIL_APP_PASSWORD = Read-Host "App Password"
  $env:NOTIFICATION_EMAIL = "recipient@example.com"
  python scripts/send_job_email.py --check
  ```

  On macOS or Linux, use `read -s GMAIL_APP_PASSWORD; export GMAIL_APP_PASSWORD`
  in place of the `Read-Host` line, and `export` for the others.

- **GitHub Actions secrets.** If `gh auth status` succeeds, you can set the
  non-password secrets and the key file directly, without the values entering
  the chat:

  ```powershell
  Get-Content service-account.json -Raw | gh secret set GOOGLE_SERVICE_ACCOUNT_JSON
  gh secret set GOOGLE_SPREADSHEET_ID --body "<id>"
  gh secret set GMAIL_ADDRESS --body "sender@gmail.com"
  gh secret set NOTIFICATION_EMAIL --body "recipient@example.com"
  ```

  The App Password is set by the user, since `gh` prompts for it:
  `gh secret set GMAIL_APP_PASSWORD`. Without `gh`, send them to
  Settings > Secrets and variables > Actions > New repository secret, one
  secret at a time.

### Stage 1: install and first report

Follow `README.md`: create a virtual environment, `pip install -r
requirements.txt`, then `load-lca --latest --replace`, `fetch-jobs --replace`,
`report`. Citizens and permanent residents skip `load-lca`. The LCA download
is 100-400 MB and loading takes minutes; say so before starting. If the DOL
site blocks the download, `load-lca` prints the manual route.

### Stage 2: profile

Run `python sponsorscan.py setup`, which is interactive. It is better for the
user to run it in their own terminal. Alternatively, ask the questions yourself
and write the profile with `onboarding.build_profile`/`save_profile`. Then run
`python sponsor_daily_report.py --profile profiles/<id>.json` and `doctor`.
The profile sets the output filenames (`output_files` in the JSON). The email
and Sheets scripts must be pointed at the same names through `NEW_JOBS_CSV` and
`ALL_MATCHES_CSV`, or they will look for the default `matches_48h.csv`.

The wizard asks whether the user wants email alerts and a Google Sheet, records
the answers in `notifications.email_enabled` and
`notifications.google_sheets_enabled` so `doctor` checks them, and ends by
printing the browser steps for each extra they chose. If you write the profile
yourself, pass `email_enabled` and `google_sheets_enabled` to `build_profile`.

### Stage 3: email alerts (optional)

Full guide: `docs/EMAIL_SETUP.md`. The user's steps, in order:

1. Turn on 2-Step Verification for the Google account that will send the mail:
   https://myaccount.google.com/signinoptions/twosv
2. Create an App Password at https://myaccount.google.com/apppasswords (name it
   "SponsorScan"). Google shows 16 letters in four groups; the spaces do not
   matter.
3. Run the `--check` block from "Handling secrets" and paste back the output.

Then run the report and `send_job_email.py` without `--check` for a real
message. It sends nothing when the new-jobs CSV has no rows, so a first test
after a fresh `--reset-state` report is the reliable one. Tell them to check
spam the first time.

### Stage 4: Google Sheets (optional)

Full guide: `docs/GOOGLE_SHEETS_SETUP.md`. This is the step people get stuck
on. Take it slowly and confirm each step before giving the next.

1. `pip install -r requirements-sheets.txt` (you run this).
2. User: create a Google Cloud project at
   https://console.cloud.google.com/projectcreate. No billing is needed.
3. User: enable the Sheets API for that project at
   https://console.cloud.google.com/apis/library/sheets.googleapis.com. Check
   that the project picker at the top shows the new project.
4. User: IAM & Admin > Service Accounts > Create service account. A name is
   enough; skip the optional role and access steps.
5. User: open the service account > Keys > Add key > Create new key > JSON.
   A file downloads. Have them move it to the repository root as
   `service-account.json`. If their organization blocks key creation, the
   policy `iam.disableServiceAccountKeyCreation` is the cause, and a personal
   Google account avoids it.
6. User: create a spreadsheet at https://sheets.new and copy its URL.
7. User: in the spreadsheet, Share > add the service account's email (the
   `client_email`, ending in `.iam.gserviceaccount.com`) as **Editor**. Turn
   off "Notify people"; the address has no inbox. Get the address for them
   with the one-liner in ground rule 5.
8. You: run `update_google_sheet.py --check` as shown in "Handling secrets".
9. You: run it without `--check` after a report exists, and ask them to look
   at the two tabs.

Tell the user that the two tabs are **replaced on every run**. Anything typed
into them, like an "applied" column, is lost. Notes belong in a separate tab.

What `--check` failures mean (the script prints the fix; this is for context):

| Message says | Cause |
|---|---|
| "not a service-account key" | They downloaded an OAuth client ID, not a service-account key (step 5) |
| "not valid JSON" | Partial paste, or the variable holds something else |
| "Sheets API is not enabled" | Step 3 was done in a different project, or not at all |
| "cannot edit this spreadsheet" | Not shared with the service account, or shared as Viewer (step 7) |
| "No spreadsheet has this ID" | Wrong ID. A full URL is accepted, so have them paste the URL |
| "rejected the service-account key" | Key was deleted or disabled; create a new one |
| "client libraries are not installed" | Step 1 |

The service account is not a Gmail account and cannot send email. Users mix
the two up. The sending address for alerts is always their own Gmail.

### Stage 5: run on a schedule (optional)

Guide: `docs/REFERENCE.md#github-actions-automation`.

1. Copy `workflows/sponsorscan.example.yml` to `.github/workflows/sponsorscan.yml`.
2. Edit its `env:` block: `PROFILE_PATH`, `ALL_MATCHES_CSV`, `NEW_JOBS_CSV`
   and `STATE_FILE` must match the profile's `output_files`.
3. **The profile must reach the runner.** `profiles/*.json` is gitignored, so it
   is not pushed by default. Discuss this with the user before choosing: in a
   **private** repository, `git add -f profiles/<id>.json` is fine. In a public
   one, the profile (their name, skills, locations) would be published, so
   they should make a private copy of the repository first. A fork of a public
   repository cannot be made private; create a new private repository and push
   to it instead.
4. Set the secrets for the extras they chose (see "Handling secrets"). The
   Sheets and email steps skip themselves until their secrets exist, so a
   missing secret shows as a skipped step rather than a failure.
5. The runner usually cannot reach the DOL site. For OPT and STEM OPT
   profiles, set the `LCA_FILE_URL` repository variable as REFERENCE.md
   explains, or the first run fails with no LCA data.
6. Commit, push, then trigger it: `gh workflow run sponsorscan.yml` or
   Actions > SponsorScan > Run workflow. Watch it with `gh run watch` and read
   any failing step's log with `gh run view --log-failed`.

The workflow commits the state file back to the repository after each run, so
the user should `git pull` before making local changes.

## Conventions for code changes

- Match the surrounding style: module docstrings list environment variables,
  comments explain why rather than what, and errors say what to do next.
- Scripts under `scripts/` exit 0 on "nothing to do" (no new jobs) and 1 on a
  real failure, with the reason on stderr. Workflows depend on that.
- Tests must not touch the network. The Sheets tests use a fake client; follow
  that pattern.
