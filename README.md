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

### Before you start

You need three things:

- **Python 3.10 or newer.** Download it from <https://www.python.org/downloads/>.
  On Windows, tick **Add python.exe to PATH** on the installer's first screen.
  To check, run `python --version` (on macOS, `python3 --version`).
- **Git.** Download it from <https://git-scm.com/downloads>. If you'd rather not
  install it, open this repository on GitHub, click the green **Code** button,
  choose **Download ZIP**, unzip it, and skip the `git clone` line below.
- **A terminal**, where every command below is typed. On Windows, open the Start
  menu and type **PowerShell**. On macOS, open **Terminal** from Applications >
  Utilities.

`git clone` creates a `sponsorscan` folder inside whichever folder the terminal
is in. A new terminal starts in your user folder, so it ends up at
`C:\Users\<you>\sponsorscan` on Windows or `~/sponsorscan` on macOS. Every
command runs from that folder, and that is where the CSVs land.

Then clone the repository and create a virtual environment.

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
cd "$HOME\sponsorscan"
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

If you cloned somewhere else, `cd` to that folder instead. When the environment
is active, the prompt starts with `(.venv)`.

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
blocks the download or changes its page, download the file in a browser instead:

1. Open <https://www.dol.gov/agencies/eta/foreign-labor/performance>.
2. The page is long. Press `Ctrl+F` (`Cmd+F` on macOS) and search for
   **LCA Programs**.
3. Under that heading, click the newest disclosure file: the highest fiscal
   year, then the highest quarter, named like `LCA_Disclosure_Data_FY2026_Q4.xlsx`.
   Skip the record-layout PDF beside it.
4. It saves to your Downloads folder. Load it with the file's real name:

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

The CSV lands in the `sponsorscan` folder your terminal is in. Open it with
`start matches.csv` on Windows or `open matches.csv` on macOS, or double-click
it in File Explorer or Finder. It opens in Excel, Numbers or Google Sheets.

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
match literally. It saves your profile in the `profiles` folder inside
`sponsorscan`, named after the profile ID you chose, and ends by printing the
exact command for the personalized report. That report adds eligibility
filtering and ranking weighted by the skills you listed:

```powershell
python sponsor_daily_report.py --profile profiles/yours.json
```

Replace `yours` with your profile ID. The report writes two CSVs to the
`sponsorscan` folder, `<profile id>_matches_48h.csv` and
`<profile id>_new_jobs_48h.csv`.

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
