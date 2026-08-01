# OPT and STEM OPT Setup

This guide explains how to configure SponsorScan for a candidate using OPT or
STEM OPT work authorization.

The OPT profile keeps roles that may be compatible with temporary employment
authorization while rejecting postings that clearly require citizenship,
permanent work authorization, or exclude OPT/CPT candidates.

## 1. Copy the example profile

From the repository root:

### Windows PowerShell

```powershell
Copy-Item ".\profiles\opt_profile.example.json" `
  ".\profiles\opt_profile.json"
```

### macOS and Linux

```bash
cp profiles/opt_profile.example.json profiles/opt_profile.json
```

The copied file is your personal configuration. Keep the example file unchanged
so other users have a clean template.

## 2. Update the profile identity

Open:

```text
profiles/opt_profile.json
```

At minimum, update:

```json
{
  "profile_id": "your_name_opt",
  "name": "Your Name"
}
```

Use a short, unique `profile_id` because SponsorScan may use it when naming
state files and report outputs.

## 3. Configure work-authorization rules

The OPT example includes these defaults:

```json
{
  "work_authorization": "opt",
  "reject_clearance_roles": true,
  "reject_citizenship_required": true,
  "reject_permanent_authorization_required": true,
  "reject_opt_excluded": true
}
```

### Field behavior

| Field | Meaning |
|---|---|
| `reject_clearance_roles` | Reject jobs requiring an active security clearance |
| `reject_citizenship_required` | Reject jobs requiring U.S. citizenship |
| `reject_permanent_authorization_required` | Reject jobs requiring permanent or unrestricted work authorization |
| `reject_opt_excluded` | Reject postings that explicitly exclude OPT, STEM OPT, or CPT candidates |

A generic statement such as:

```text
No visa sponsorship is available for this role.
```

should not automatically remove a posting from an OPT-oriented report. A
candidate may already have temporary employment authorization and may not need
immediate sponsorship.

SponsorScan should still reject language such as:

```text
OPT candidates are not eligible.
```

```text
Must have permanent U.S. work authorization.
```

```text
U.S. citizenship is required.
```

```text
Active security clearance required.
```

Always verify work-authorization language in the original posting before
applying.

## 4. Configure experience and seniority filters

The example profile uses:

```json
{
  "max_required_experience": 1,
  "reject_senior_titles": true,
  "reject_level_ii_plus_titles": true
}
```

This keeps jobs with a minimum requirement of 0 or 1 year and rejects jobs whose
minimum requirement is above 1 year.

Examples that should be rejected:

```text
2+ years of experience
2-5 years of software engineering experience
5 years building production systems
Senior Software Engineer
Software Engineer II
SDE 3
```

Examples that may remain eligible:

```text
No prior professional experience required
0-1 years of experience
1 year of experience
0-2 years of experience
```

For a range such as `0-2 years`, the lower bound is 0, so the role may still be
appropriate for an entry-level candidate.

## 5. Configure degree filters

The example profile keeps advanced-degree mentions by default:

```json
{
  "reject_masters_mentions": false,
  "reject_phd_mentions": false
}
```

Set either value to `true` only when you want to remove every posting that
mentions that degree.

For example:

```json
{
  "reject_masters_mentions": true,
  "reject_phd_mentions": true
}
```

is intentionally strict and may remove postings that say:

```text
Master's preferred
PhD is a plus
```

Use the settings that match the candidate's actual preferences.

## 6. Add target roles

Edit `target_roles` to match the candidate's search.

Example:

```json
"target_roles": [
  "Software Engineer",
  "Backend Engineer",
  "Full Stack Engineer",
  "Data Engineer",
  "Machine Learning Engineer",
  "AI Engineer"
]
```

Avoid adding very broad labels unless you are comfortable reviewing unrelated
matches.

## 7. Add resume skills

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

Recommended weighting:

| Weight | Meaning |
|---|---|
| 7 | Core strength with strong project, work, or research evidence |
| 5-6 | Strong practical experience |
| 3-4 | Working knowledge |
| 1-2 | Familiarity or supporting skill |

Only include skills the candidate can reasonably discuss in an interview.

## 8. Configure locations

Update `preferred_locations`:

```json
"preferred_locations": [
  "Remote",
  "California",
  "Texas",
  "New York"
]
```

Use terms likely to appear in ATS location fields.

## 9. Configure output and state files

Keep each profile's reports and state file separate:

```json
"output_files": {
  "all_matches": "opt_matches_48h.csv",
  "new_matches": "opt_new_jobs_48h.csv",
  "state": ".sponsorscan_opt_state.json"
}
```

The state file tracks previously seen jobs for this profile. Do not share one
state file across multiple candidates.

## 10. Run the profile locally

First refresh the jobs database:

```powershell
python sponsorscan.py fetch-jobs --replace
```

Then generate the personalized report.

### Windows PowerShell

```powershell
python sponsor_daily_report.py `
  --profile ".\profiles\opt_profile.json"
```

### macOS and Linux

```bash
python sponsor_daily_report.py \
  --profile profiles/opt_profile.json
```

The report script should read eligibility rules, role targets, skills, locations,
time window, score threshold, and output filenames from the profile.

## 11. Keep personal files private

Do not commit personal profile files.

Add this to `.gitignore`:

```gitignore
profiles/*.json
!profiles/*.example.json
```

Also avoid committing:

- generated CSV reports;
- state files;
- resume content;
- email addresses;
- Google credentials;
- Gmail app passwords.

## 12. Optional notifications and automation

After the local report works, it can be connected to:

- GitHub Actions for scheduled execution;
- Google Sheets for live report synchronization;
- Gmail for new-match notifications.

See:

```text
docs/EMAIL_SETUP.md
docs/GOOGLE_SHEETS_SETUP.md
```

## Troubleshooting

### A role says "no sponsorship" but is missing

Confirm that the report is not treating generic no-sponsorship language as an
automatic hard exclusion.

The OPT profile should distinguish between:

```text
No visa sponsorship is available.
```

and:

```text
OPT candidates are not eligible.
```

The second statement is a direct exclusion; the first is not necessarily one.

### Citizenship-required roles are appearing

Confirm:

```json
"reject_citizenship_required": true
```

### Permanent-work-authorization roles are appearing

Confirm:

```json
"reject_permanent_authorization_required": true
```

### Roles requiring multiple years are appearing

Confirm:

```json
"max_required_experience": 1
```

and ensure the report script uses the highest detected minimum requirement, not
the smallest one.

For example, a posting that mentions both:

```text
1 year of Python experience
5+ years of overall engineering experience
```

must be rejected because the posting still contains a 5-year requirement.

### A posting is removed because it mentions a master's or PhD

Confirm:

```json
"reject_masters_mentions": false,
"reject_phd_mentions": false
```

Set these values to `true` only when you intentionally want strict
advanced-degree filtering.
