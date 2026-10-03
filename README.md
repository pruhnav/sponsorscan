<p align="center">
  <img src="assets/banner.svg" alt="sponsorscan" width="100%">
</p>

<p align="center">
  <img src="assets/job-boards.gif"
       alt="Live postings from Greenhouse, Lever, Ashby and Workday" width="100%"><br>
  <picture>
    <source media="(max-width: 700px)" srcset="assets/citizens-marquee-mobile.svg">
    <img src="assets/citizens-marquee.svg"
         alt="Also for U.S. citizens and green card holders" width="100%">
  </picture>
</p>

Finds entry-level postings at employers with real H-1B filing history, and drops
the ones that disqualify you outright.

It joins two sources that cannot go stale on someone else's schedule:

- **DOL OFLC LCA disclosure data.** Quarterly, official, a legal filing
  requirement. Free bulk download, no API key.
- **Public ATS job board APIs** (Greenhouse, Lever, Ashby, Workday). Served straight from
  the employer, no aggregator in between.

Curated college job lists such as
[SpeedyApply's](https://github.com/speedyapply/2027-SWE-College-Jobs) can be
followed too, to reach employers with no board listed. Their postings carry no
description, so they are ranked on the title alone.

The core workflow is:

1. Load the latest LCA disclosure data.
2. Pull current postings from the job boards in `companies.yaml`.
3. Filter and rank the results.

Then, optionally:

4. Generate a personalized report ranked against your own skills and eligibility.
5. Sync it to Google Sheets and send email notifications.
6. Run the whole thing on a schedule through GitHub Actions.

`discover` also widens the company list automatically when you want broader
coverage than the boards that ship in `companies.yaml`.

## Install

Clone the repository and create a virtual environment.

### Windows PowerShell

```powershell
git clone https://github.com/pruhnav/sponsorscan.git
cd sponsorscan

python -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -r requirements.txt
```

The execution-policy change applies only to the current PowerShell window. When
you open a new terminal later, return to the repository and activate the
environment again:

```powershell
cd "C:\path\to\sponsorscan"
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

### macOS and Linux

```bash
git clone https://github.com/pruhnav/sponsorscan.git
cd sponsorscan

python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Quickstart

Three commands from a clean clone to a CSV of matches. Every flag for every
command is documented in [docs/REFERENCE.md](docs/REFERENCE.md).

### 1. Load the sponsorship data

SponsorScan reads the quarterly **LCA Programs (H-1B, H-1B1, E-3)** disclosure
file published by the DOL. It is an `.xlsx` file, typically 100-400 MB, and the
filename changes each quarter.

U.S. citizens and permanent residents can skip this step. Their reports ignore
sponsorship history; see [docs/CITIZEN_SETUP.md](docs/CITIZEN_SETUP.md).

```powershell
python sponsorscan.py load-lca --latest --replace
```

That resolves the newest file from the DOL site and downloads it. If the DOL
changes their page and the lookup fails, it tells you exactly what to download
by hand, then:

```powershell
python sponsorscan.py load-lca "$HOME\Downloads\LCA_Disclosure_Data_FY2026_Q2.xlsx" --replace
```

On macOS or Linux:

```bash
python sponsorscan.py load-lca ~/Downloads/LCA_Disclosure_Data_FY2026_Q2.xlsx --replace
```

This takes a few minutes. Re-run it when the DOL publishes a new quarterly file.

### 2. Pull live postings

```powershell
python sponsorscan.py fetch-jobs --replace
```

`companies.yaml` already ships with 32 confirmed job boards, so this
works immediately. There is no list to build first.

### 3. Generate the report

```powershell
python sponsorscan.py report --out matches.csv
```

The CSV lands in the current folder. It opens directly in Excel, or imports into
Google Sheets.

### Optional: widen the company list

The shipped list is deliberately small. `discover` reads the employers loaded in
step 1, guesses job-board slugs from their legal names, probes all three
supported providers, and adds every confirmed board to `companies.yaml`:

```powershell
python sponsorscan.py discover
```

The first run can take a long time, because it may perform thousands of HTTP
probes. Results are cached in the database as they land, so `Ctrl+C` is safe and
a later run resumes from the cache. Skip this until you want broader coverage.

### Optional: personalize the report

```powershell
python sponsorscan.py setup
```

Asks a handful of questions - work authorization, target roles, skills,
locations - and writes a profile for you, instead of leaving you to hand-edit
25 JSON fields. You type your skills in; there is no resume file to upload. The
wizard shows which skill names the scorer recognises and flags any it will only
match literally. Then run the personalized report, which adds eligibility
filtering and ranking weighted by the skills you listed:

```powershell
python sponsor_daily_report.py --profile profiles/yours.json
```

### Something not working?

```powershell
python sponsorscan.py doctor
```

Checks every stage and names the one that needs attention, instead of leaving
you with an empty CSV and no explanation. Add `--profile profiles/yours.json`
to validate a profile at the same time.

## Where to go next

| You want | Read |
|---|---|
| Every command flag, the scoring table, and the caveats | [docs/REFERENCE.md](docs/REFERENCE.md) |
| A report ranked against the skills you list and your eligibility | [docs/REFERENCE.md](docs/REFERENCE.md#personalized-reporting) |
| Setup for OPT or STEM OPT | [docs/OPT_SETUP.md](docs/OPT_SETUP.md) |
| Setup for U.S. citizens | [docs/CITIZEN_SETUP.md](docs/CITIZEN_SETUP.md) |
| Google Sheets sync | [docs/GOOGLE_SHEETS_SETUP.md](docs/GOOGLE_SHEETS_SETUP.md) |
| Email notifications | [docs/EMAIL_SETUP.md](docs/EMAIL_SETUP.md) |
| Running it on a schedule, unattended | [docs/REFERENCE.md](docs/REFERENCE.md#github-actions-automation) |
| Help from an AI coding assistant | Open the repository in Claude Code, Codex, Cursor or Copilot and ask it to set things up. [AGENTS.md](AGENTS.md) tells it how. |

Before trusting a run, read the
[caveats](docs/REFERENCE.md#caveats-read-these). An LCA filing is evidence of
prior willingness to sponsor, not a job offer, and employer name matching is
imperfect.
