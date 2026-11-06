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
5. Optional: open **Integrations** to set up Telegram, email, Google Sheets or a
   schedule. Each card gives the steps and copyable terminal commands.

Statuses are manual. Opening a posting does not submit an application or mark
it as applied. There is no resume scanner or automatic application submission
in this version. Profile fit is a weighted ranking heuristic, not an ATS score.

## What each page shows

### Matches

- Each row shows the company logo, the role, its location and posting date,
  your profile fit, and tags for new postings, required experience and the
  skills you matched. **Open posting** goes straight to the employer's page.
- **H-1B filings** is a switch in the toolbar. It shows or hides the H-1B and
  green card columns, and the dashboard remembers your choice. The switch
  appears once filing history is loaded, for profiles that need sponsorship.
- Sort by most recent (the default), oldest, highest or lowest fit, or most
  H-1B filings. Clicking the Fit, Posted or H-1B filings column header sorts
  by that column. Clicking Fit or Posted again reverses the order.
- The bookmark saves a role to Applications. Clicking it again unsaves it,
  with an Undo. When a count at the top goes up, a green +1 rises from it.
- Opening a role shows:
  - **Profile fit**, broken into its parts, such as role match 62/62 plus
    skills in the posting 5/42. The parts come from the same scoring as the
    report, so they always add up to the total. Below them is a list of the
    skills the posting asks for that your profile does not have.
  - **Sponsorship history**: H-1B filings by year, green card (PERM) filings,
    the share of filings for client sites and for new hires, and the DOL
    employer name the company was matched to.
  - **Why this job matched**, and your application status and notes.
- After you open a posting and come back to the dashboard, it asks **Did you
  apply?** Yes marks the role as applied. Not yet leaves it as it was.
- A built match list is reused for 15 minutes. Fetching jobs or editing your
  profile rebuilds it right away.

### Applications

Your saved and tracked roles, with search, a status filter and CSV export.
Remove asks for a second click before it deletes a record.

### Integrations

One card per extra: Telegram, Gmail alerts, Google Sheets and a GitHub Actions
schedule, then a final card that checks your profile and data and runs a
report. Each card has
numbered steps, buttons that open the right pages, and commands with a Copy
button. The commands already use your profile path and CSV file names. **Show
commands for** switches between PowerShell and macOS or Linux.

Everything still runs in your own terminal. Passwords and bot tokens are typed
into hidden prompts there, and the dashboard never sees them.

### Theme

The sun, moon and monitor buttons at the top right switch between light, dark
and your system setting. The dashboard follows the system setting until you
choose, then remembers your choice.

## Company logos

Logos are fetched once from each company's own site, using its `domain:` in
`companies.yaml`, and cached in the workspace's `logos/` folder. A company
without a usable logo keeps a colored letter tile, and its site is asked again
after a week. A fetched file is used only if it is a real image, and logo
requests never go to your own computer or local network.

Some sites turn away scripts. For those, a company entry can give a `logo:`
image address to use instead, or you can drop a file named `<domain>.png` (for
example `doordash.com.png`) into the `logos/` folder.

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
| `logos/` | Cached company logos, and any you add by hand |

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
Python server changes require restarting the command. Hanken Grotesk is
bundled under its SIL Open Font License in `dashboard/OFL.txt`.

The dashboard reuses `sponsor_daily_report.generate_report`, which opens the
job database read-only. The report CLI uses the same ranking engine and retains
its CSV and notification-state behavior.

Run the offline tests with `python -m pytest tests -q` and `python selftest.py`.
Dashboard handler tests use in-memory request streams and do not open network
connections or contact job boards.
