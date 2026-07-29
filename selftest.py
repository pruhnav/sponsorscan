"""Exercise everything that does not require network access."""
import csv, os, sqlite3, subprocess, sys, time

os.environ["SPONSORSCAN_DB"] = "test.db"
for f in ("test.db", "test_lca.csv", "test_out.csv"):
    if os.path.exists(f):
        os.remove(f)

# --- synthetic LCA file, mimicking DOL column names -------------------------
HEADER = ["CASE_NUMBER", "CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE",
          "SOC_TITLE", "WORKSITE_STATE", "WAGE_RATE_OF_PAY_FROM", "WAGE_UNIT_OF_PAY",
          "PW_WAGE_LEVEL"]
ROWS = []
# heavy sponsor, name written three different ways
for i in range(60):
    ROWS.append([f"I-1-{i}", "CERTIFIED", "DATABRICKS, INC.", "Software Engineer",
                 "Software Developers", "CA", "165000", "Year",
                 "III" if i % 2 else "IV"])
ROWS.append(["I-1-x", "CERTIFIED", "Databricks Inc", "Data Scientist",
             "Data Scientists", "CA", "180000", "Year", "IV"])
ROWS.append(["I-1-y", "DENIED", "databricks llc", "Data Scientist",
             "Data Scientists", "CA", "180000", "Year", "IV"])
# light sponsor
for i in range(4):
    ROWS.append([f"I-2-{i}", "CERTIFIED", "Benchling, Inc.", "Research Engineer",
                 "Software Developers", "CA", "150000", "Year", "I"])
# hourly wage, tests unit conversion
ROWS.append(["I-3-0", "CERTIFIED", "Nuro Inc", "Robotics Engineer",
             "Engineers", "CA", "80", "Hour", "II"])
# certified-withdrawn should not count as certified
ROWS.append(["I-4-0", "CERTIFIED-WITHDRAWN", "Plaid Inc.", "Engineer",
             "Software Developers", "CA", "170000", "Year", "III"])

with open("test_lca.csv", "w", newline="") as fh:
    w = csv.writer(fh); w.writerow(HEADER); w.writerows(ROWS)


def run(*args):
    r = subprocess.run([sys.executable, "sponsorscan.py", *args],
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout, r.stderr); sys.exit(f"FAILED: {args}")
    return r.stdout


print("=" * 62)
print(run("load-lca", "test_lca.csv", "--replace").strip())

con = sqlite3.connect("test.db")
rows = dict(con.execute("SELECT employer_norm, certified FROM employers").fetchall())
assert "databricks" in rows, rows
assert rows["databricks"] == 61, f"3 name variants should merge to 61 certified, got {rows['databricks']}"
assert rows["plaid"] == 0, "certified-withdrawn must not count as certified"
print("PASS  name variants merge; withdrawn excluded from certified count")

lv = dict((r[0], r[1:]) for r in con.execute(
    "SELECT employer_norm, lvl1, lvl2, lvl3, lvl4 FROM employers").fetchall())
assert sum(lv["databricks"]) == 62, f"wage levels should be captured, got {lv['databricks']}"
assert lv["databricks"][2] + lv["databricks"][3] == 62, "databricks filed all at III/IV"
assert lv["benchling"][0] == 4, "benchling filed all at level I"
print("PASS  prevailing wage levels captured per employer")

# the old name blacklist dropped Palantir and kept TCS; it is gone now
import sponsorscan as _ss
assert not hasattr(_ss, "STAFFING_RE"), "name-based blacklist must not come back"
assert "consultanc" not in open("sponsorscan.py").read(), "no name blacklist"
print("PASS  no name-based employer exclusion remains")

# --- synthetic postings (stands in for fetch-jobs) --------------------------
JOBS = [
    # company,            title,                          location,        description
    ("Databricks",        "Software Engineer, New Grad",  "San Francisco, CA", "Join us."),
    ("Databricks",        "Senior Staff Engineer",        "San Francisco, CA", "Join us."),
    ("Benchling",         "Data Scientist I",             "San Francisco, CA", "Visa sponsorship is available."),
    ("Nuro",              "Robotics Intern",              "Mountain View, CA", "Great team."),
    ("Plaid",             "Software Engineer, Entry",     "San Francisco, CA", "Fun role."),
    ("Acme Defense Labs", "Junior Engineer",              "Santa Clara, CA",   "Applicants must be a US citizen."),
    ("Foo Systems",       "Associate Engineer",           "San Jose, CA",      "We do not offer visa sponsorship."),
    ("Bar Analytics",     "Data Analyst",                 "Austin, TX",        "Nice place."),
    ("Baz Corp",          "New Grad Engineer",            "Remote",            "Will not now or in the future require sponsorship."),
    ("Databricks",        "Associate Engineer",           "Austin, TX",        "Texas office."),
]
sys.path.insert(0, ".")
from sponsorscan import norm_employer  # noqa: E402

con.executemany(
    "INSERT OR REPLACE INTO jobs (job_key, source, company, company_norm, title, "
    "location, url, posted, description, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
    [(f"test:{i}", "test", c, norm_employer(c), t, loc,
      f"https://example.com/{i}", "2026-07-25", d, time.strftime("%Y-%m-%d"))
     for i, (c, t, loc, d) in enumerate(JOBS)])
con.commit()
con.close()

BAY = "san francisco,santa clara,mountain view,san jose,remote"
out = run("report", "--out", "test_out.csv", "--top", "10", "--locations", BAY)
print(out.strip())

with open("test_out.csv") as fh:
    res = list(csv.DictReader(fh))
titles = [r["title"] for r in res]

assert not any("Senior Staff" in t for t in titles), "senior titles must be filtered"
assert not any(t == "Junior Engineer" for t in titles), "citizenship req must be dropped"
assert not any(t == "Associate Engineer" and r["company"] == "Foo Systems"
               for t, r in zip(titles, res)), "no-sponsorship must be dropped"
assert not any(t == "New Grad Engineer" for t in titles), "future-sponsorship must be dropped"
assert not any(r["location"] == "Austin, TX" for r in res), "location filter"
assert res[0]["company"] == "Databricks", f"heaviest sponsor + new grad should rank first, got {res[0]}"
assert int(res[0]["lca_certified"]) == 61
assert float(res[0]["lca_senior_wage_share"]) == 1.0, res[0]
bench = next(r for r in res if r["company"] == "Benchling")
assert float(bench["lca_senior_wage_share"]) == 0.0, bench
assert "level III/IV" in res[0]["signals"], res[0]["signals"]
print("PASS  wage-level mix surfaced and scored")
print("PASS  seniority, disqualifier, location filters and ranking all behave")

run("report", "--out", "test_out.csv", "--any-location", "--top", "3", "--locations", BAY)
with open("test_out.csv") as fh:
    res2 = list(csv.DictReader(fh))
assert any(r["location"] == "Austin, TX" for r in res2), "--any-location should re-admit"
print("PASS  --any-location re-admits out-of-area postings")

# default is now nationwide: no --locations means nothing is dropped on geography
run("report", "--out", "test_out.csv", "--top", "3")
with open("test_out.csv") as fh:
    res3 = list(csv.DictReader(fh))
assert any(r["location"] == "Austin, TX" for r in res3), "default should be nationwide"
print("PASS  default report is nationwide")

# --- slug generation + discover, with the network probe stubbed --------------
import sponsorscan as ss  # noqa: E402

assert ss.slug_candidates("DATABRICKS, INC.") == ["databricks"]
assert "paloaltonetworks" in ss.slug_candidates("Palo Alto Networks, Inc.")
assert "palo-alto-networks" in ss.slug_candidates("Palo Alto Networks, Inc.")
# multi-word names must not produce a bare first-token guess
assert "definitive" not in ss.slug_candidates("Definitive Intelligence LLC")
# corporate filler dropped, but a variant keeping it is still offered
cands = ss.slug_candidates("Benchling Technologies Inc")
assert cands[0] == "benchling" and "benchlingtechnologies" in cands
print("PASS  slug candidates are sane and avoid risky single-token guesses")

FAKE_BOARDS = {("greenhouse", "databricks"): (True, 42),
               ("ashby", "benchling"): (True, 7),
               ("lever", "nuro"): (True, 3)}
ss.probe = lambda provider, slug, timeout=12: FAKE_BOARDS.get((provider, slug), (False, 0))

if os.path.exists("test_companies.yaml"):
    os.remove("test_companies.yaml")


class A:  # stand-in for argparse.Namespace
    out = "test_companies.yaml"; min_certified = 1; roles = ""; states = ""
    limit = 100; workers = 4; max_certified = 0; merge = True


ss.DB_PATH = "test.db"
ss.cmd_discover(A())

import yaml as _yaml  # noqa: E402
with open("test_companies.yaml") as fh:
    got = _yaml.safe_load(fh)["companies"]
slugs = {p: {e["slug"] for e in v} for p, v in got.items()}
assert slugs.get("greenhouse") == {"databricks"}, slugs
assert slugs.get("ashby") == {"benchling"}, slugs
assert "nuro" in slugs.get("lever", set()), slugs
assert "plaid" not in slugs.get("greenhouse", set()), "0 certified must not qualify"
print("PASS  discover probes candidates and writes only confirmed boards")

# second run should hit the cache and add nothing new
before = open("test_companies.yaml").read()
ss.cmd_discover(A())
assert open("test_companies.yaml").read() == before, "re-run should be idempotent"
print("PASS  discover is idempotent and caches probe results")

for f in ("test.db", "test_lca.csv", "test_out.csv", "test_companies.yaml"):
    if os.path.exists(f):
        os.remove(f)

print("=" * 62)
print("All offline checks passed.")
