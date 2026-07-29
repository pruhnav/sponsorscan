# sponsorscan

Finds entry-level postings at employers with real H-1B filing history, and drops
the ones that disqualify you outright.

It joins two sources that cannot go stale on someone else's schedule:

- **DOL OFLC LCA disclosure data.** Quarterly, official, a legal filing
  requirement. Free bulk download, no API key.
- **Public ATS job board APIs** (Greenhouse, Lever, Ashby). Served straight from
  the employer, no aggregator in between.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## 1. Get the LCA data

Go to <https://www.dol.gov/agencies/eta/foreign-labor/performance> and download
the most recent **LCA Programs (H-1B, H-1B1, E-3)** disclosure file. It's an
.xlsx, typically 100-400 MB. As of this writing FY2026 Q2 is the newest release.

```bash
python sponsorscan.py load-lca ~/Downloads/LCA_Disclosure_Data_FY2026_Q2.xlsx --replace
```

Takes a few minutes. Builds a local SQLite database of employers with certified,
denied and withdrawn counts, the roles they file for, and which states.

Re-run once a quarter when DOL publishes. That's the only maintenance.

## 2. Build the company list

```bash
python sponsorscan.py discover
```

This is what keeps the tool from being a hand-typed watchlist. It reads the
employers you just loaded, keeps the ones nationwide who filed for tech and data
roles, guesses job board slugs from their legal names, probes all three
providers, and writes every confirmed board into `companies.yaml`.

First run takes a while (thousands of HTTP probes, 12 at a time). Results are
cached in the database as they land, so Ctrl-C is safe and a re-run resumes.

| Flag | Effect |
|---|---|
| `--states CA,NY,WA` | Restrict to certain states; blank is nationwide (default) |
| `--roles "machine learning,data scien"` | Title keywords; blank uses a tech/data default set |
| `--min-certified 15` | Only employers with heavier sponsorship history (default 5) |
| `--max-certified 2000` | Skip employers above that many certified LCAs (0 = no cap) |
| `--no-merge` | Overwrite the list instead of adding to it |

Only three of the big ATS providers are covered. Workday, Taleo, iCIMS and
SmartRecruiters are not, which is why large enterprises, universities and
hospitals will be missing.

## 3. Pull live postings

```bash
python sponsorscan.py fetch-jobs --replace
```

## 4. Report

```bash
python sponsorscan.py report --out matches.csv
```

Defaults to anywhere in the US. Pass `--locations` to narrow.

| Flag | Effect |
|---|---|
| `--locations "san francisco,santa clara,remote"` | Comma-separated substrings matched against posting location |
| `--any-location` | Keep postings that miss the location filter instead of dropping them |
| `--sponsors-only` | Drop employers with no certified LCAs on record |
| `--include-senior` | Stop filtering out Senior/Staff/Principal/Director titles |
| `--top 40` | How many to print (the CSV always has everything) |

## What the score means

| Signal | Points |
|---|---|
| Employer has certified LCAs | +40 |
| 25 or more certified | +10 |
| Half or more filings at prevailing wage Level III/IV | +10 |
| A quarter or more of filings denied or withdrawn | -10 |
| Has filed for a similar job title | +15 |
| Entry-level title | +20 |
| Location match | +15 |
| Posting explicitly states sponsorship | +10 |

Anything hitting a disqualifier is dropped before scoring, not penalized.
Disqualifiers are citizenship requirements, security clearance, ITAR/export
control, and any negation appearing near "sponsor" in the same sentence.

## Caveats, read these

**The network code is untested.** It was written in a sandbox where dol.gov and
the ATS endpoints were firewalled off. The parsing, matching, filtering and
ranking logic is covered by `selftest.py` and passes. The three `fetch_*`
functions and the URL-download path in `load-lca` have never made a real
request. If a provider changed its JSON shape, that's the first place to look.

```bash
python selftest.py   # runs the offline checks
```

**An LCA filing is not a job offer, and not a guarantee.** It means the employer
filed for someone at some point in that quarter. It's evidence of willingness,
nothing more.

**Employer name matching is imperfect.** DOL sees legal entity names, job boards
show brand names. Instacart files as Maplebear Inc. Fuzzy matching at cutoff 90
handles most of it, but set `name:` explicitly in `companies.yaml` when you know
the legal name, and check the `signals` column for "fuzzy match" notes.

**`discover` guesses slugs, and a guess can land on the wrong company.** A probe
only proves the board exists, not that it belongs to the employer whose name
produced the guess. Single-token guesses on multi-word names are deliberately
not attempted for this reason, but collisions are still possible. If a company's
postings look unrelated to the company, delete its line from `companies.yaml`.

**No employer is excluded by name.** An earlier version filtered out anything
whose name matched "consulting" or "staffing". That dropped Palantir Technologies
Inc as a false positive while happily keeping Tata Consultancy, Infosys,
Accenture and Cognizant, which is exactly backwards. Accenture and Deloitte hire
new grads directly and sponsor at volume, so they belong in your results.

What actually separates a good sponsor from a bad one is in the data, not the
name. Two columns in the CSV carry it:

- `lca_senior_wage_share` — the share of filings at prevailing wage Level III/IV.
  Higher means better pay for the role, and better odds under the FY2027 weighted
  lottery, which favours those levels. An employer filing almost everything at
  Level I for roles that should be Level III is the actual warning sign.
- `lca_denied_withdrawn_rate` — the share of filings that didn't go through. A
  high rate suggests either sloppy filing practice or speculative bench filing.

Judge with those rather than by company name.

**Expect a low hit rate, and that's fine.** Most employers won't match any slug
guess, either because they use a different provider or because their board URL
doesn't resemble their legal name. A few hundred confirmed boards out of a few
thousand candidates is a normal result and still far more than a hand-typed list.

**You are on OPT, so you don't need sponsorship right now.** Your EAD is your
authorization. The disqualifier filter is the part that matters today; the LCA
score matters for the OPT-to-H-1B handoff 18 to 36 months out. Don't let a low
sponsor score stop you applying somewhere you're eligible for now.

**Cap-exempt employers are mostly missing.** Universities, university-affiliated
nonprofits and national labs run Workday or Taleo, not these three providers, so
they need a fetcher this doesn't have yet. They're also the category where you
skip the lottery entirely, so it's the most valuable thing to add.
