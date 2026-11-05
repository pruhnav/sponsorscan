# Your local SponsorScan dashboard

The dashboard runs on your computer and uses the existing Python job-matching
engine. It has no account signup, hosting subscription, or AI API dependency.
The interface and fonts are bundled in the repository. Internet access is
needed when fetching jobs, downloading employer history, or opening postings.

## Start

Install the normal dependencies as described in the README, activate your
virtual environment, and run from the repository root:

```bash
python sponsorscan.py dashboard
```

The command starts a local server and opens http://127.0.0.1:8765. Keep its
terminal open while using the dashboard. Press Ctrl+C to stop. Application
records remain on disk after shutdown and are restored the next time you run it.

On Windows, you can also run the virtual environment's interpreter directly:

```powershell
.\.venv\Scripts\python.exe sponsorscan.py dashboard
```

## First run

1. Follow the four steps in **Profile**: About you, Your search, Your skills,
   and Review. Choose your work authorization and target roles, then rate skills
   as Strong (7), Comfortable (5), or Familiar (3), just like the terminal wizard.
   Unknown skills work through literal text matching; known names are suggested.
   The posting window starts at 48 hours. Review shows your active filters and
   offers optional email and Sheets setup preferences before anything is saved.
2. Click **Create my workspace**. The next screen offers **Load history** for
   profiles needing sponsorship and **Fetch jobs** to collect postings from
   `companies.yaml`. History uses the prebuilt table, not the larger raw DOL
   download. Both actions also remain available in Matches.
3. Browse matches. Open a role to inspect the matching reasons and employer
   history, then save it or open the employer's posting.
4. In **Applications**, record statuses and notes. **Add application** lets you
   track a role found elsewhere, even if it is not in SponsorScan's feed.

Statuses are manual. Opening a posting does not submit an application or mark
it as applied. There is no resume scanner or automatic application submission
in this version. Profile fit is a weighted ranking heuristic, not an ATS score.

## Existing profiles

If exactly one personal JSON profile exists in `profiles/`, it is selected
automatically. Example profiles are never selected. With multiple profiles,
choose the intended one explicitly:

```bash
python sponsorscan.py dashboard --profile profiles/my_profile.json
```

Otherwise the onboarding form creates `.sponsorscan-dashboard/profile.json`.
Profile edits preserve existing output filenames, company tiers, and advanced
filters. Skill weights and notification preferences change only when you edit
them; custom weights remain available in the familiarity menu. Changing work authorization
updates the four related exclusion defaults, just as the setup wizard does.
Preferred locations boost matching rank; they are not a hard geographic filter.
The current terminal defaults treat permanent residents like citizens for
citizen-only posting filters; onboarding calls this out so you can check each
role's requirements. This update does not change the underlying matching rules.

Optional email and Sheets choices record preferences only. **Read setup
instructions** uses the shared terminal setup guide with your saved profile
path and CSV filenames. Follow its connection checks in your own terminal;
passwords and service-account keys are not collected by the dashboard. Changing
a preference does not start or stop existing scripts or scheduled delivery.

Application records are separated by the resolved profile file path. Keep that
path stable when continuing an existing search. Renaming or moving a selected
profile starts a separate application view, though old records remain stored.

## Refreshes and alerts

- **Refresh matches** reads the local database and re-ranks it. It does not fetch
  postings from the internet.
- **Fetch jobs** runs the existing collector in the background. **Load employer
  history** updates the prebuilt LCA data. Only one update runs at a time.
- Updates are staged separately. A failed update or a fetch where no board
  succeeds leaves the previous database intact. Partial board failures are
  reported, and the published feed can then be incomplete.
- Saved applications keep their own job snapshots. Changing the feed or the
  report window does not remove application history.
- Browsing and saving in the dashboard do not write CSV reports, mark jobs as
  notified, or send email or Sheets updates. Existing CLI automation still works.
- Updates run only when requested while the server is open. Closing it stops
  an active update. Existing scheduled CLI jobs are independent; avoid running
  a CLI database update at the same time as a dashboard update.

If the feed is empty, check the posting window and minimum score in Profile,
fetch current jobs, and inspect the exclusion explanations. Jobs without usable
posting dates are excluded, matching the standard report's default behavior.

## Storage and backups

The default private workspace is `.sponsorscan-dashboard/`, excluded from Git:

| File | Purpose |
|---|---|
| `applications.sqlite3` | Saved job snapshots, statuses, notes, and timestamps |
| `profile.json` | Profile created by dashboard onboarding |
| `last-update.log` | Output from the latest data update |

The job catalog remains in `sponsorscan.db` (or `SPONSORSCAN_DB` / `--db`).
To back up your workspace, stop the dashboard and copy the entire private
workspace folder plus any profile selected with `--profile`. CSV export is
available for the currently filtered matches or applications, but the database
backup preserves all records and their profile ownership.

## Command options

```bash
python sponsorscan.py dashboard --port 8766 --no-browser
python sponsorscan.py dashboard --db path/to/jobs.db --companies companies.yaml
python sponsorscan.py dashboard --workspace path/to/private-workspace
```

Keep custom workspace folders out of source control yourself. The built-in
server binds only to `127.0.0.1` and serves a fixed set of interface assets.
It is a single-user local application, not a public or LAN-hosted login service.
Other devices cannot reach it through this address.

## Development

Edit the files in `dashboard/` and refresh the browser. No build step is needed.
Python server changes require restarting the command. Instrument Sans is
bundled under its SIL Open Font License in `dashboard/OFL.txt`.

The dashboard reuses `sponsor_daily_report.generate_report`, which opens the
job database read-only. The report CLI uses the same ranking engine and retains
its CSV and notification-state behavior.

Run the offline tests with `python -m pytest tests -q` and `python selftest.py`.
Dashboard handler tests use in-memory request streams and do not open network
connections or contact job boards.
