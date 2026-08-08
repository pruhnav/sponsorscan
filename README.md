# sponsorscan

Finds entry-level postings at employers with real H-1B filing history, and drops
the ones that disqualify you outright.

It joins two sources that cannot go stale on someone else's schedule:

- **DOL OFLC LCA disclosure data.** Quarterly, official, a legal filing
  requirement. Free bulk download, no API key.
- **Public ATS job board APIs** (Greenhouse, Lever, Ashby). Served straight from
  the employer, no aggregator in between.

The core workflow is:

1. Load the latest LCA disclosure data.
2. Discover employers with supported public job boards.
3. Pull their current postings.
4. Filter and rank the results.
5. Optionally generate a personalized report, sync it to Google Sheets, and send
   email notifications through GitHub Actions.

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

## 1. Get the LCA data

Go to <https://www.dol.gov/agencies/eta/foreign-labor/performance> and download
the most recent **LCA Programs (H-1B, H-1B1, E-3)** disclosure file. It is an
`.xlsx` file and is typically 100-400 MB.

The exact filename changes each quarter. Replace the example filename below with
the file you downloaded.

### Windows PowerShell

```powershell
python sponsorscan.py load-lca "$HOME\Downloads\LCA_Disclosure_Data_FY2026_Q2.xlsx" --replace
```

If the filename contains spaces, keep the path inside quotation marks.

### macOS and Linux

```bash
python sponsorscan.py load-lca ~/Downloads/LCA_Disclosure_Data_FY2026_Q2.xlsx --replace
```

This takes a few minutes. It builds a local SQLite database of employers with
certified, denied, and withdrawn counts, the roles they filed for, and the
states in which they filed.

Re-run this step when the DOL publishes a new quarterly disclosure file.

## 2. Build the company list

```powershell
python sponsorscan.py discover
```

This is what keeps the tool from being a hand-typed watchlist. It reads the
employers you just loaded, keeps the ones nationwide that filed for tech and
data roles, guesses job-board slugs from their legal names, probes all three
supported providers, and writes every confirmed board into `companies.yaml`.

The first run can take a while because it may perform thousands of HTTP probes.
Results are cached in the database as they land, so `Ctrl+C` is safe and a later
run resumes from the cache.

| Flag | Effect |
|---|---|
| `--states CA,NY,WA` | Restrict discovery to selected states; blank is nationwide |
| `--roles "machine learning,data scien"` | Use custom title keywords; blank uses the default tech/data set |
| `--min-certified 15` | Require heavier sponsorship history; default is 5 |
| `--max-certified 2000` | Skip employers above the specified number of certified LCAs; 0 removes the cap |
| `--no-merge` | Overwrite `companies.yaml` instead of adding to it |

Only three major ATS providers are currently covered. Workday, Taleo, iCIMS, and
SmartRecruiters are not, which is why some large enterprises, universities, and
hospitals will be missing.

## 3. Pull live postings

```powershell
python sponsorscan.py fetch-jobs --replace
```

This reads the boards listed in `companies.yaml`, fetches current postings, and
stores them in `sponsorscan.db`.

Use `--replace` when you want the jobs table to represent the latest full fetch.
Run the same command again whenever you want refreshed postings.

## 4. Generate the standard report

```powershell
python sponsorscan.py report --out matches.csv
```

The report defaults to locations anywhere in the United States. Pass
`--locations` to narrow it.

| Flag | Effect |
|---|---|
| `--locations "san francisco,santa clara,remote"` | Match comma-separated location substrings |
| `--any-location` | Keep postings that miss the location filter instead of dropping them |
| `--sponsors-only` | Drop employers with no certified LCAs on record |
| `--include-senior` | Stop filtering Senior, Staff, Principal, and Director titles |
| `--top 40` | Control how many rows print in the terminal; the CSV still contains every match |

The generated CSV is written in the current folder. It can be opened directly in
Excel or imported into Google Sheets.

## What the score means

| Signal | Points |
|---|---|
| Employer has certified LCAs | +40 |
| 25 or more certified | +10 |
| Half or more filings at prevailing wage Level III/IV | +10 |
| A quarter or more filings denied or withdrawn | -10 |
| Has filed for a similar job title | +15 |
| Entry-level title | +20 |
| Location match | +15 |
| Posting explicitly states sponsorship | +10 |

Anything hitting a disqualifier is dropped before scoring, not penalized.
Disqualifiers in the standard report include citizenship requirements, security
clearance, ITAR/export-control restrictions, and sponsorship-negation language.

## Personalized reporting

The standard `report` command ranks jobs primarily from employer sponsorship
history, title, and location. A personalized report can add stricter candidate
eligibility checks and resume-weighted ranking.

A personalized setup may account for:

- target role families;
- skills demonstrated on the candidate's resume;
- maximum acceptable experience requirements;
- degree requirements;
- seniority indicators;
- work authorization;
- company preferences;
- posting age;
- previously seen jobs.

The personalized report script is run after `fetch-jobs` and reads from the same
`sponsorscan.db` database:

```powershell
python sponsor_daily_report.py `
  --hours 48 `
  --out matches_48h.csv `
  --new-out new_jobs_48h.csv
```

PowerShell uses the backtick character for line continuation. The same command
can be entered on one line:

```powershell
python sponsor_daily_report.py --hours 48 --out matches_48h.csv --new-out new_jobs_48h.csv
```

On macOS or Linux:

```bash
python sponsor_daily_report.py \
  --hours 48 \
  --out matches_48h.csv \
  --new-out new_jobs_48h.csv
```

The report keeps a local state file so `new_jobs_48h.csv` contains jobs that were
not present in the previous run. Keep each user's state file separate when
running reports for more than one person.

## Work-authorization configurations

Work-authorization filtering should be configured separately from resume
scoring.

### OPT and STEM OPT

An OPT-oriented configuration should normally continue to consider jobs that
say they do not provide future visa sponsorship, because the candidate may
already have temporary employment authorization. It should still reject jobs
that explicitly:

- exclude OPT, STEM OPT, or CPT candidates;
- require permanent or unrestricted work authorization;
- require U.S. citizenship or permanent residence;
- require an active security clearance;
- impose an incompatible ITAR or export-control restriction.

Whether a role is suitable for a STEM OPT extension also depends on the employer
and the candidate's individual circumstances. Always verify the posting and the
employer before applying.

### U.S. citizens

A citizen-oriented configuration can keep postings that require U.S.
citizenship or U.S.-person status. Citizenship does not automatically mean that
the candidate holds an active security clearance, so clearance-required roles
should remain separately configurable.

Resume skills, target roles, seniority, experience limits, and degree filters
should still be personalized for the individual candidate.

## Optional Google Sheets synchronization

A Google Sheets integration can replace the contents of two worksheet tabs after
each report:

- `All Matches 48h`
- `New Jobs 48h`

A typical uploader reads:

```text
GOOGLE_SERVICE_ACCOUNT_JSON
GOOGLE_SPREADSHEET_ID
```

from environment variables or GitHub Actions secrets.

The Google service account must be given Editor access to the destination
spreadsheet. Do not commit the service-account JSON file or its contents to the
repository.

A workflow step can call the uploader after report generation:

```yaml
- name: Update Google Sheet
  env:
    GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
    GOOGLE_SPREADSHEET_ID: ${{ secrets.GOOGLE_SPREADSHEET_ID }}
  run: python update_google_sheet.py
```

If the uploader uses the Google Sheets API, add its required client libraries to
`requirements.txt`.

## Optional email notifications

Email notifications can be sent only when `new_jobs_48h.csv` contains at least
one job. A Gmail-based sender typically reads:

```text
GMAIL_ADDRESS
GMAIL_APP_PASSWORD
NOTIFICATION_EMAIL
```

from environment variables or GitHub Actions secrets.

`GMAIL_ADDRESS` must be a real Gmail account used to send the message. It is not
the Google Sheets service-account address. `GMAIL_APP_PASSWORD` must belong to
the same Gmail account.

A workflow step can call the sender after the Google Sheets update:

```yaml
- name: Email new job matches
  env:
    GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}
    GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}
    NOTIFICATION_EMAIL: ${{ secrets.NOTIFICATION_EMAIL }}
  run: python send_job_email.py
```

Do not commit email passwords, app passwords, recipient addresses, or other
private configuration to the repository.

## GitHub Actions automation

The complete process can run on a schedule through GitHub Actions:

1. Check out the repository.
2. Install Python dependencies.
3. Restore or build `sponsorscan.db`.
4. Fetch current postings.
5. Generate the personalized report.
6. Update Google Sheets.
7. Send an email when new matches exist.
8. Upload the CSV files as workflow artifacts.
9. Commit the updated state file when it changes.

A scheduled workflow can also include `workflow_dispatch` so it can be tested
manually from the Actions tab.

Store credentials under:

```text
Repository Settings
  -> Secrets and variables
  -> Actions
```

Never place secret values directly in the workflow YAML.

## Running the offline checks

```powershell
python selftest.py
```

The same command works on Windows, macOS, and Linux while the virtual environment
is active.

## Caveats, read these

**The network code was originally written in a sandbox where dol.gov and the ATS
endpoints were unavailable.** The parsing, matching, filtering, and ranking logic
is covered by `selftest.py`. If a provider changes its JSON structure, the
corresponding `fetch_*` function is the first place to inspect.

**An LCA filing is not a job offer or a guarantee.** It means the employer filed
for someone at some point in the disclosure period. It is evidence of prior
willingness to sponsor, nothing more.

**Employer name matching is imperfect.** DOL sees legal entity names, while job
boards show brand names. Instacart, for example, files as Maplebear Inc. Fuzzy
matching at cutoff 90 handles many differences, but set `name:` explicitly in
`companies.yaml` when you know the legal name and check the `signals` column for
fuzzy-match notes.

**`discover` guesses slugs, and a guess can land on the wrong company.** A probe
only proves that the board exists, not that it belongs to the employer whose
name produced the guess. Single-token guesses on multi-word names are
deliberately avoided, but collisions can still occur. If a company's postings
look unrelated to the company, remove its line from `companies.yaml`.

**No employer is excluded by name.** An earlier version filtered out anything
whose name matched "consulting" or "staffing." That dropped Palantir Technologies
Inc. as a false positive while keeping firms such as Tata Consultancy, Infosys,
Accenture, and Cognizant. The available filing data is a better signal than a
company-name heuristic.

Two CSV columns are especially useful:

- `lca_senior_wage_share` — the share of filings at prevailing wage Level III/IV.
- `lca_denied_withdrawn_rate` — the share of filings that were denied or
  withdrawn.

Judge employers using those signals rather than company name alone.

**Expect a low discovery hit rate.** Most employers will not match any slug guess
because they use a different provider or because their board URL does not
resemble their legal name. A few hundred confirmed boards out of a few thousand
candidates is still substantially broader than a hand-maintained list.

**OPT candidates do not necessarily need immediate sponsorship.** A candidate's
current employment authorization and a company's willingness to sponsor later
are separate questions. The eligibility filter matters for present
applications; LCA history is evidence that may matter for a later transition.

**Cap-exempt employers are underrepresented.** Universities,
university-affiliated nonprofits, and national laboratories often use ATS
providers that SponsorScan does not yet support. Those employers may require a
separate fetcher.

## Security and privacy

The following files and values should never be committed:

- `sponsorscan.db`;
- service-account JSON files;
- Gmail app passwords;
- personal resume data;
- private profile files;
- generated state files containing personal tracking history;
- local CSV reports, unless intentionally published.

Add local and personalized files to `.gitignore` before enabling automation.

## To be added
