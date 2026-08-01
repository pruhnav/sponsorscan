# U.S. Citizen Setup

This guide explains how to configure SponsorScan for a U.S. citizen candidate.

The citizen profile keeps roles that require U.S. citizenship or permanent work
authorization, while still applying entry-level, resume, degree, and seniority
filters.

## 1. Copy the example profile

From the repository root:

### Windows PowerShell

```powershell
Copy-Item ".\profiles\citizen_profile.example.json" `
  ".\profiles\citizen_profile.json"
```

### macOS and Linux

```bash
cp profiles/citizen_profile.example.json profiles/citizen_profile.json
```

The copied file is your personal configuration. Do not edit the example file
directly if you want to keep a clean template for future users.

## 2. Update the profile

Open:

```text
profiles/citizen_profile.json
```

At minimum, update:

```json
{
  "profile_id": "your_name_citizen",
  "name": "Your Name"
}
```

Use a short, unique `profile_id` because SponsorScan may use it when naming
state files and outputs.

## 3. Configure eligibility rules

The citizen example includes these defaults:

```json
{
  "work_authorization": "us_citizen",
  "max_required_experience": 1,
  "reject_senior_titles": true,
  "reject_level_ii_plus_titles": true,
  "reject_masters_mentions": false,
  "reject_phd_mentions": false,
  "reject_clearance_roles": true,
  "reject_citizenship_required": false,
  "reject_permanent_authorization_required": false,
  "reject_opt_excluded": false
}
```

### Field behavior

| Field | Meaning |
|---|---|
| `max_required_experience` | Highest acceptable minimum years-of-experience requirement |
| `reject_senior_titles` | Reject Senior, Staff, Principal, Lead, Manager, and similar titles |
| `reject_level_ii_plus_titles` | Reject Engineer II/III/IV/V and numeric Level 2+ titles |
| `reject_masters_mentions` | Reject postings that mention a master's degree |
| `reject_phd_mentions` | Reject postings that mention a PhD or doctorate |
| `reject_clearance_roles` | Reject roles that require a security clearance |
| `reject_citizenship_required` | Keep or reject U.S.-citizenship-required roles |
| `reject_permanent_authorization_required` | Keep or reject permanent-work-authorization roles |
| `reject_opt_excluded` | Relevant mainly to OPT profiles; normally false for citizens |

A U.S. citizen can generally keep citizenship-required roles, so
`reject_citizenship_required` is false by default.

A citizen may still not hold an active security clearance. Keep
`reject_clearance_roles` set to true unless the candidate is specifically
targeting clearance-required positions.

## 4. Add target roles

Edit the `target_roles` list to match the candidate's search.

Example:

```json
"target_roles": [
  "Software Engineer",
  "Backend Engineer",
  "Full Stack Engineer",
  "Data Engineer"
]
```

Keep role names specific enough to avoid unrelated listings.

## 5. Add resume skills

The `skills` object controls resume-weighted ranking.

Example:

```json
"skills": {
  "Python": 7,
  "Java": 6,
  "SQL": 6,
  "AWS": 5,
  "React": 4,
  "Docker": 3
}
```

Higher values indicate stronger or more relevant skills.

Recommended scale:

| Weight | Meaning |
|---|---|
| 7 | Core strength with strong project, work, or research evidence |
| 5-6 | Strong practical experience |
| 3-4 | Working knowledge |
| 1-2 | Familiarity or supporting skill |

Only include skills that the candidate can reasonably discuss in an interview.

## 6. Configure locations

Update `preferred_locations`:

```json
"preferred_locations": [
  "Remote",
  "California",
  "Texas"
]
```

Use terms that are likely to appear in job descriptions or ATS location fields.

## 7. Configure output files

Keep report and state files separate from other profiles:

```json
"output_files": {
  "all_matches": "citizen_matches_48h.csv",
  "new_matches": "citizen_new_jobs_48h.csv",
  "state": ".sponsorscan_citizen_state.json"
}
```

The state file tracks jobs already seen by this profile. Do not reuse one state
file across multiple users.

## 8. Run the profile locally

After fetching current jobs:

```powershell
python sponsorscan.py fetch-jobs --replace
```

Run the personalized report:

```powershell
python sponsor_daily_report.py `
  --profile ".\profiles\citizen_profile.json"
```

The same command on macOS or Linux:

```bash
python sponsor_daily_report.py \
  --profile profiles/citizen_profile.json
```

The report script should read output names, time window, minimum score, roles,
skills, and eligibility rules from the profile.

## 9. Keep personal files private

Do not commit a real candidate profile if it contains personal information.

Add the personal file to `.gitignore`:

```gitignore
profiles/*.json
!profiles/*.example.json
```

This keeps example profiles in the repository while ignoring private copies.

Also avoid committing:

- generated CSV reports;
- state files;
- resume content;
- email addresses;
- Google credentials;
- Gmail app passwords.

## 10. Optional automation

After the local profile works, it can be used with GitHub Actions, Google Sheets,
and email notifications.

See:

```text
docs/EMAIL_SETUP.md
docs/GOOGLE_SHEETS_SETUP.md
```

Each automated user should have:

- a separate profile;
- separate output files;
- a separate state file;
- separate notification settings where applicable.

## Troubleshooting

### Citizenship-required roles are missing

Confirm:

```json
"reject_citizenship_required": false
```

### Clearance-required roles are missing

Confirm:

```json
"reject_clearance_roles": false
```

Only disable this when the candidate intentionally wants clearance-required
positions.

### Too many experienced roles are appearing

Lower:

```json
"max_required_experience": 1
```

and confirm that senior and Level II+ title filtering remain enabled.

### Good roles are being removed because they mention graduate degrees

Set:

```json
"reject_masters_mentions": false,
"reject_phd_mentions": false
```

A degree mention is not always a hard requirement, so these settings should match
the candidate's actual preferences.
