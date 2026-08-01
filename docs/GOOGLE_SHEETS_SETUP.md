# Google Sheets Setup

This guide explains how to synchronize SponsorScan report CSV files with a
Google Sheets spreadsheet.

The Google Sheets integration is optional. It should run after the personalized
report generates the all-matches and new-matches CSV files.

## 1. Requirements

You need:

- a Google Cloud project;
- the Google Sheets API enabled;
- a Google service account;
- a service-account JSON key;
- a Google spreadsheet shared with the service-account email;
- `update_google_sheet.py` in the repository;
- generated report CSV files.

Do not commit the service-account JSON file or its contents.

## 2. Create a Google Cloud project

Open the Google Cloud Console and create a new project for SponsorScan.

Use a descriptive name such as:

```text
SponsorScan Automation
```

Select that project before continuing.

## 3. Enable the Google Sheets API

In the Google Cloud project:

```text
APIs & Services
  -> Library
  -> Google Sheets API
  -> Enable
```

The uploader only needs the Sheets API unless you later add separate Google
Drive functionality.

## 4. Create a service account

In the Google Cloud project:

```text
IAM & Admin
  -> Service Accounts
  -> Create Service Account
```

Use a descriptive name such as:

```text
sponsorscan-sheet-bot
```

After creating the service account, create a JSON key:

```text
Service Account
  -> Keys
  -> Add Key
  -> Create new key
  -> JSON
```

Download the JSON file and store it securely.

The service-account email will look similar to:

```text
sponsorscan-sheet-bot@your-project-id.iam.gserviceaccount.com
```

## 5. Create and share the spreadsheet

Create a Google spreadsheet for SponsorScan.

Copy the spreadsheet ID from its URL.

For a URL like:

```text
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890/edit
```

the spreadsheet ID is:

```text
1AbCdEfGhIjKlMnOpQrStUvWxYz1234567890
```

Share the spreadsheet with the service-account email and grant it Editor access.

The service account cannot update the sheet until it has access.

## 6. Add GitHub Actions secrets

In the repository, open:

```text
Settings
  -> Secrets and variables
  -> Actions
  -> New repository secret
```

Create:

| Secret | Purpose |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full contents of the downloaded JSON key |
| `GOOGLE_SPREADSHEET_ID` | Destination spreadsheet ID |

Paste the entire JSON object into `GOOGLE_SERVICE_ACCOUNT_JSON`.

Do not upload the JSON key file into the repository.

## 7. Add the uploader script

Recommended location:

```text
scripts/update_google_sheet.py
```

The script should:

1. read the all-matches CSV;
2. read the new-matches CSV;
3. authenticate with the service-account JSON;
4. create worksheet tabs if they do not exist;
5. clear old worksheet contents;
6. write the CSV headers and rows;
7. freeze the header row;
8. exit with a clear error if required environment variables are missing.

Recommended worksheet names:

```text
All Matches 48h
New Jobs 48h
```

## 8. Add the workflow step

If the uploader is stored under `scripts/`:

```yaml
- name: Update Google Sheet
  env:
    GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
    GOOGLE_SPREADSHEET_ID: ${{ secrets.GOOGLE_SPREADSHEET_ID }}
    ALL_MATCHES_CSV: matches_48h.csv
    NEW_JOBS_CSV: new_jobs_48h.csv
  run: python scripts/update_google_sheet.py
```

Keep the filenames consistent with the active profile.

For example, a citizen profile may use:

```text
citizen_matches_48h.csv
citizen_new_jobs_48h.csv
```

An OPT profile may use:

```text
opt_matches_48h.csv
opt_new_jobs_48h.csv
```

Set `ALL_MATCHES_CSV` and `NEW_JOBS_CSV` accordingly.

## 9. Add required Python packages

The exact dependencies depend on the uploader implementation.

A common setup uses:

```text
google-api-python-client
google-auth
google-auth-httplib2
```

Add the required packages to `requirements.txt`, then install them:

```powershell
pip install -r requirements.txt
```

## 10. Test locally

Set temporary environment variables in PowerShell:

```powershell
$env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content `
  "C:\path\to\service-account.json" -Raw

$env:GOOGLE_SPREADSHEET_ID = "your-spreadsheet-id"
$env:ALL_MATCHES_CSV = "matches_48h.csv"
$env:NEW_JOBS_CSV = "new_jobs_48h.csv"

python .\scripts\update_google_sheet.py
```

These values apply only to the current PowerShell session.

On macOS or Linux:

```bash
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"
export GOOGLE_SPREADSHEET_ID="your-spreadsheet-id"
export ALL_MATCHES_CSV="matches_48h.csv"
export NEW_JOBS_CSV="new_jobs_48h.csv"

python scripts/update_google_sheet.py
```

Do not print the service-account JSON in logs.

## 11. Test through GitHub Actions

Open the repository's Actions tab and manually run the SponsorScan workflow.

Verify:

- the report step succeeds;
- the Google Sheets step is green;
- the expected worksheet tabs exist;
- headers appear in row 1;
- old contents are replaced;
- the all-matches tab contains the full report;
- the new-jobs tab contains only newly discovered jobs.

## 12. Multiple profiles

Each profile can write to:

- a separate spreadsheet;
- separate worksheet tabs in one spreadsheet;
- or separate CSV-specific tabs.

For a shared spreadsheet, use distinct tab names such as:

```text
Pranav - All Matches
Pranav - New Jobs
Citizen - All Matches
Citizen - New Jobs
```

Do not let one profile overwrite another profile's worksheet tabs.

## Troubleshooting

### Permission denied

Confirm that the spreadsheet is shared with the exact service-account email and
that the service account has Editor access.

### The spreadsheet ID is invalid

Copy only the value between `/d/` and `/edit` in the spreadsheet URL.

### The workflow reports invalid JSON

Confirm that `GOOGLE_SERVICE_ACCOUNT_JSON` contains the complete JSON object.

Do not paste only part of the file.

### The script cannot find a CSV

Confirm the report step runs before the Google Sheets step and that the
environment-variable filenames match the generated files.

For debugging:

```yaml
- name: List generated files
  run: ls -la
```

### A worksheet cannot be created

When using the Google Sheets batch update API, the frozen row setting belongs
inside `gridProperties`.

Correct structure:

```python
{
    "addSheet": {
        "properties": {
            "title": sheet_name,
            "gridProperties": {
                "frozenRowCount": 1
            }
        }
    }
}
```

Placing `frozenRowCount` directly under `properties` causes an API error.

### Existing data remains after an update

The uploader should clear the worksheet before writing the new CSV contents.

### The service-account email is being used for Gmail

The Google Sheets service account is only for API access. It is not the Gmail
account used to send SponsorScan notifications.

Use a real Gmail account and Gmail App Password for email alerts.

## Security

Never commit:

- service-account JSON keys;
- the raw `GOOGLE_SERVICE_ACCOUNT_JSON` value;
- spreadsheet IDs for private spreadsheets when avoidable;
- `.env` files containing credentials;
- generated reports containing personal data.

Use GitHub Actions secrets or local environment variables for all sensitive
values.
