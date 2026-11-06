"""Unit tests for the pieces of sponsorscan.py that do not need a database.

The end-to-end load/report/discover flow is covered by selftest.py.
"""

import json

import pytest
import requests

import sponsorscan as ss


# ----------------------------------------------------------- html_to_text

def test_greenhouse_escaped_markup_becomes_prose():
    """Greenhouse returns HTML inside an escaped JSON string.

    Filters run on prose, so the markup and the entities both have to go.
    """
    raw = "&lt;p&gt;We don&#39;t sponsor visas.&lt;/p&gt;"
    assert ss.html_to_text(raw) == "We don't sponsor visas."


def test_block_tags_become_line_breaks():
    text = ss.html_to_text("<li>No clearance needed</li><li>We sponsor</li>")
    assert "\n" in text
    assert "No clearance needed" in text


def test_html_to_text_handles_empty_input():
    assert ss.html_to_text(None) == ""
    assert ss.html_to_text("") == ""


def test_disqualifier_survives_markup_round_trip():
    raw = "&lt;p&gt;We are &lt;b&gt;unable to&lt;/b&gt; sponsor visas.&lt;/p&gt;"
    blob = ss.html_to_text(raw)
    assert any(p.search(blob) for p in ss.DISQ_RE)


# --------------------------------------------------------- disqualifiers

def test_negation_does_not_leak_across_a_clause():
    """A negation before a semicolon must not attach to a later "sponsor"."""
    blob = "There is no cost to relocate; we sponsor visas for the right candidate."
    assert not any(p.search(blob) for p in ss.DISQ_RE)


@pytest.mark.parametrize("blob", [
    "We do not offer visa sponsorship.",
    "This role is not eligible for sponsorship.",
    "We are unable to sponsor at this time.",
    "Applicants must be a US citizen.",
    "This position requires an active security clearance.",
])
def test_real_refusals_still_match(blob):
    assert any(p.search(blob) for p in ss.DISQ_RE)


# ---------------------------------------------------------------- probing

class _Response:
    def __init__(self, status_code, payload=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def test_missing_board_is_cached_as_a_definite_miss(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(404))
    assert ss.probe("greenhouse", "nope") == (False, 0)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_failures_are_not_a_miss(monkeypatch, status):
    """A rate limit says nothing about the slug.

    Recording it as a miss would write the employer off until the database is
    rebuilt, so probe reports None and discover leaves it uncached.
    """
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(status))
    assert ss.probe("greenhouse", "acme") == (None, 0)


def test_network_errors_are_not_a_miss(monkeypatch):
    def boom(*a, **k):
        raise requests.Timeout("too slow")

    monkeypatch.setattr(requests, "get", boom)
    assert ss.probe("lever", "acme") == (None, 0)


def test_live_board_reports_its_job_count(monkeypatch):
    payload = {"jobs": [{"id": 1}, {"id": 2}]}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(200, payload))
    assert ss.probe("greenhouse", "acme") == (True, 2)


def test_empty_board_still_counts_as_real(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Response(200, {"jobs": []}))
    assert ss.probe("ashby", "acme") == (True, 0)


def test_non_json_response_is_a_miss(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: _Response(200, bad_json=True))
    assert ss.probe("greenhouse", "acme") == (False, 0)


# ------------------------------------------------------------ normalizing

@pytest.mark.parametrize("name", ["Google LLC", "GOOGLE INC.", "Google, Inc"])
def test_corporate_suffixes_collapse(name):
    assert ss.norm_employer(name) == "google"


def test_ampersand_and_the_word_and_normalize_alike():
    """"&" expands to "and", which is then dropped as filler, so both agree."""
    assert ss.norm_employer("Smith & Wesson") == "smith wesson"
    assert ss.norm_employer("Smith and Wesson") == "smith wesson"


# --------------------------------------------------------- employer match

def test_exact_match_scores_100():
    index = {"databricks": {}}
    assert ss.match_employer("databricks", index, ["databricks"], 90) == ("databricks", 100)


def test_zero_cutoff_disables_fuzzy_matching():
    index = {"maplebear": {}}
    assert ss.match_employer("instacart", index, ["maplebear"], 0) == (None, 0)


@pytest.mark.skipif(not ss.HAVE_RAPIDFUZZ, reason="rapidfuzz not installed")
def test_fuzzy_match_below_cutoff_is_rejected():
    index = {"maplebear": {}}
    key, score = ss.match_employer("totally different", index, ["maplebear"], 90)
    assert key is None


# ------------------------------------------------------------- fetch-jobs

def test_fetch_records_boards_that_answered(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    def empty_board(slug):
        return []

    def dead_board(slug):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"greenhouse": empty_board, "lever": dead_board})
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n"
        "  greenhouse:\n    - {slug: stripe, name: Stripe}\n"
        "  lever:\n    - {slug: plaid, name: Plaid}\n", encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(companies=str(companies), replace=True, delay=0,
                                          workday_days=3))

    con = sqlite3.connect(tmp_path / "t.db")
    fetched = {r[0] for r in con.execute("SELECT company_norm FROM fetched_companies")}
    con.close()
    assert fetched == {"stripe"}, "a board that failed must not count as tracked"


# ------------------------------------------------------------ feeds, removed
#
# SpeedyApply's lists had no posting text, so none of the requirement checks
# could run on their rows. A companies.yaml that still has the section keeps
# working, and rows a feed stored before are cleared.

def test_leftover_feeds_section_is_ignored_with_a_note(monkeypatch, tmp_path, capsys):
    import argparse
    import sqlite3

    def board(slug):
        return [{"job_key": "greenhouse:stripe:9", "source": "greenhouse",
                 "title": "Backend Engineer", "location": "Seattle, WA",
                 "url": "https://job-boards.greenhouse.io/stripe/jobs/9",
                 "posted": "2024-10-04", "description": "Python."}]

    def no_network(*args, **kwargs):
        raise AssertionError("a feed was fetched")

    monkeypatch.setattr(ss.requests, "get", no_network)
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"greenhouse": board})
    con = ss.connect()
    con.execute("INSERT INTO jobs (job_key, source, company, title) VALUES "
                "('speedyapply:x', 'speedyapply', 'Globex', 'Data Engineer Intern')")
    con.commit()
    con.close()
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n"
        "feeds:\n  speedyapply:\n    - o/r/README.md\n", encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(companies=str(companies), replace=False, delay=0,
                                          workday_days=3))

    assert "no longer supported" in capsys.readouterr().out
    con = sqlite3.connect(tmp_path / "t.db")
    sources = [r[0] for r in con.execute("SELECT source FROM jobs")]
    failures = con.execute("SELECT COUNT(*) FROM fetch_failures").fetchone()[0]
    con.close()
    assert sources == ["greenhouse"]
    assert failures == 0


# ---------------------------------------------------------------- workday

@pytest.mark.parametrize("text, days", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 5 Days Ago", 5),
    ("Posted 30+ Days Ago", 30), ("", None), ("Recently", None)])
def test_workday_posted_age(text, days):
    assert ss.workday_age_days(text) == days


class _FakeWorkday:
    """A tenant with 25 listed jobs, served 20 per page like the real API."""

    def __init__(self, ages, broken=()):
        self.ages = ages
        self.broken = set(broken)
        self.detail_calls = []

    def _response(self, payload, status=200):
        r = requests.Response()
        r.status_code = status
        r._content = json.dumps(payload).encode()
        return r

    def post(self, url, json=None, **kwargs):
        start = json["offset"]
        page = [{"title": f"Engineer {i}", "externalPath": f"/job/Place/Engineer_R{i}",
                 "postedOn": text}
                for i, text in list(enumerate(self.ages))[start:start + json["limit"]]]
        return self._response({"total": len(self.ages), "jobPostings": page})

    def get(self, url, **kwargs):
        i = int(url.rsplit("_R", 1)[1])
        self.detail_calls.append(i)
        if i in self.broken:
            return self._response({}, status=500)
        return self._response({"jobPostingInfo": {
            "title": f"Engineer {i}", "jobReqId": f"R{i}",
            "jobDescription": "<p>Build <b>things</b>.</p>",
            "location": "San Jose", "additionalLocations": ["Austin"],
            "country": {"descriptor": "United States of America"},
            "startDate": "2026-09-27",
            "externalUrl": f"https://acme.wd5.myworkdayjobs.com/Ext/job/Place/Engineer_R{i}",
        }})


def _install(monkeypatch, fake):
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(ss, "WORKDAY_DETAIL_DELAY", 0)


def test_workday_fetches_details_only_for_recent_jobs(monkeypatch):
    ages = ["Posted Today"] * 3 + ["Posted 10 Days Ago"] * 20 + ["Posted Yesterday"] * 2
    fake = _FakeWorkday(ages)
    _install(monkeypatch, fake)

    jobs = ss.fetch_workday("acme/wd5/Ext", max_age_days=3)

    assert sorted(fake.detail_calls) == [0, 1, 2, 23, 24], "paged past page one"
    job = next(j for j in jobs if j["job_key"] == "workday:acme/wd5/Ext:R0")
    assert job["source"] == "workday"
    assert job["posted"] == "2026-09-27"
    assert job["description"].startswith("Build things")
    assert job["url"].endswith("/Engineer_R0")
    assert job["location"] == "San Jose; Austin, United States of America"


def test_workday_skips_a_job_whose_detail_fails(monkeypatch):
    fake = _FakeWorkday(["Posted Today"] * 3, broken={1})
    _install(monkeypatch, fake)
    keys = {j["job_key"] for j in ss.fetch_workday("acme/wd5/Ext", max_age_days=3)}
    assert keys == {"workday:acme/wd5/Ext:R0", "workday:acme/wd5/Ext:R2"}


def test_workday_board_fails_when_every_detail_fails(monkeypatch):
    fake = _FakeWorkday(["Posted Today"] * 2, broken={0, 1})
    _install(monkeypatch, fake)
    with pytest.raises(requests.HTTPError):
        ss.fetch_workday("acme/wd5/Ext", max_age_days=3)


def test_workday_slug_must_have_three_parts():
    with pytest.raises(ValueError, match="tenant/wdN/site"):
        ss.fetch_workday("adobe", max_age_days=3)


def test_fetch_passes_the_workday_window(monkeypatch, tmp_path):
    import argparse
    seen = {}

    def fake_workday(slug, max_age_days):
        seen[slug] = max_age_days
        return []

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"workday": fake_workday})
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  workday:\n"
                         "    - {slug: adobe/wd5/ext, name: Adobe}\n", encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(
        companies=str(companies), replace=True, delay=0, workday_days=7))
    assert seen == {"adobe/wd5/ext": 7}


def test_workday_job_listed_twice_is_fetched_once(monkeypatch):
    fake = _FakeWorkday(["Posted Today"] * 25)
    real_post = fake.post

    def post_with_repeat(url, json=None, **kwargs):
        r = real_post(url, json=json, **kwargs)
        if json["offset"] == 20:  # unstable ordering repeats a job from page one
            body = r.json()
            body["jobPostings"].append({"title": "Engineer 0", "postedOn": "Posted Today",
                                        "externalPath": "/job/Place/Engineer_R0"})
            r._content = __import__("json").dumps(body).encode()
        return r

    fake.post = post_with_repeat
    _install(monkeypatch, fake)
    ss.fetch_workday("acme/wd5/Ext", max_age_days=3)
    assert sorted(fake.detail_calls) == list(range(25))


def test_workday_board_fails_when_a_list_page_fails(monkeypatch):
    fake = _FakeWorkday(["Posted Today"] * 45)
    real_post = fake.post

    def flaky_post(url, json=None, **kwargs):
        if json["offset"] == 20:
            return fake._response({}, status=503)
        return real_post(url, json=json, **kwargs)

    fake.post = flaky_post
    _install(monkeypatch, fake)
    with pytest.raises(requests.HTTPError):
        ss.fetch_workday("acme/wd5/Ext", max_age_days=3)


# --------------------------------------------------------- senior titles

@pytest.mark.parametrize("title, senior", [
    ("Senior Software Engineer", True),
    ("Senior Associate", True),
    ("Program Manager Intern", False),
    ("Software Engineer", False),
])
def test_is_senior_title(title, senior):
    assert ss.is_senior_title(title) is senior


# ------------------------------------------------------------- greenhouse

def _greenhouse(monkeypatch, job):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps({"jobs": [{"id": 1, "title": "Engineer", **job}]}).encode()
    monkeypatch.setattr(requests, "get", lambda *a, **k: r)
    return ss.fetch_greenhouse("acme")[0]


def test_greenhouse_posted_is_first_publication(monkeypatch):
    """Employers bulk-touch their boards, which bumps updated_at on every job;
    a 2023 posting would otherwise read as posted this week."""
    job = _greenhouse(monkeypatch, {"first_published": "2023-12-12T05:19:55-05:00",
                                    "updated_at": "2026-09-21T13:22:12-04:00"})
    assert job["posted"] == "2023-12-12"


def test_greenhouse_falls_back_to_updated_at(monkeypatch):
    job = _greenhouse(monkeypatch, {"updated_at": "2026-09-21T13:22:12-04:00"})
    assert job["posted"] == "2026-09-21"


def test_fetch_records_failed_boards(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    def dead_board(slug):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"lever": dead_board})
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies:\n  lever:\n    - {slug: plaid, name: Plaid}\n",
                         encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(
        companies=str(companies), replace=True, delay=0, workday_days=2))

    con = sqlite3.connect(tmp_path / "t.db")
    rows = con.execute("SELECT board, error FROM fetch_failures").fetchall()
    con.close()
    assert rows == [("lever/plaid", "connection refused")]


# --------------------------------------------------------------- load-lca
#
# The example workflow keeps a cached database and only refreshes it. When the
# DOL site blocks the runner, the cached employers must survive the attempt.

def _db_with_employer(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    con = ss.connect()
    con.execute("INSERT INTO employers (employer_norm, certified) VALUES ('acme', 5)")
    con.commit()
    con.close()
    return lambda: sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT COUNT(*) FROM employers").fetchone()[0]


def _blocked(*a, **k):
    r = requests.Response()
    r.status_code = 403
    r.url = "https://www.dol.gov/"
    r.raw = __import__("io").BytesIO(b"")
    return r


def test_blocked_dol_site_keeps_cached_employers(monkeypatch, tmp_path):
    import argparse
    count = _db_with_employer(monkeypatch, tmp_path)
    monkeypatch.setattr(requests, "get", _blocked)
    with pytest.raises(SystemExit):
        ss.cmd_load_lca(argparse.Namespace(path=None, latest=True, replace=True))
    assert count() == 1


def test_failed_download_exits_cleanly_and_keeps_employers(monkeypatch, tmp_path):
    import argparse
    count = _db_with_employer(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "get", _blocked)
    with pytest.raises(SystemExit, match="Could not download"):
        ss.cmd_load_lca(argparse.Namespace(
            path="https://example.com/LCA_Disclosure_Data_FY2026_Q3.xlsx",
            latest=False, replace=True))
    assert count() == 1


# ------------------------------------------------------- workday discovery
#
# Workday answers 422 for an unknown tenant or the wrong data center, 404 for
# the right tenant and data center with the wrong site, and 200 for a hit.

def _workday_server(monkeypatch, tenant, wd, site, total=7, other=None):
    def post(url, json=None, **kwargs):
        host, path = url.split("//", 1)[1].split("/", 1)
        t, w = host.split(".")[:2]
        s = path.split("/")[3]
        r = requests.Response()
        if other is not None:
            r.status_code = other
        elif t != tenant or w != wd:
            r.status_code = 422
        elif s != site:
            r.status_code = 404
        else:
            r.status_code = 200
        r._content = __import__("json").dumps({"total": total, "jobPostings": []}).encode()
        return r
    monkeypatch.setattr(requests, "post", post)


def test_workday_board_found_on_a_later_data_center(monkeypatch):
    _workday_server(monkeypatch, "acme", "wd12", "careers")
    assert ss.find_workday_board("acme", "Acme Corp") == (True, "acme/wd12/careers", 7)


def test_workday_site_built_from_the_employer_name(monkeypatch):
    _workday_server(monkeypatch, "capitalone", "wd12", "capital_one")
    ok, slug, _ = ss.find_workday_board("capitalone", "Capital One Services")
    assert (ok, slug) == (True, "capitalone/wd12/capital_one")


def test_workday_tenant_found_without_its_site(monkeypatch):
    _workday_server(monkeypatch, "acme", "wd5", "somethingbespoke")
    assert ss.find_workday_board("acme", "Acme") == (False, "acme/wd5", 0)


def test_workday_unknown_tenant_is_a_definite_miss(monkeypatch):
    _workday_server(monkeypatch, "someoneelse", "wd1", "external")
    assert ss.find_workday_board("acme", "Acme") == (False, None, 0)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_workday_transient_errors_are_not_a_miss(monkeypatch, status):
    _workday_server(monkeypatch, "acme", "wd1", "external", other=status)
    assert ss.find_workday_board("acme", "Acme") == (None, None, 0)


# ------------------------------------------------------- employer matching
#
# Shapes from real DOL data: a brand-named stub filer next to the legal entity
# that does the company's actual filing.

def _index(**certified):
    return {name.replace("_", " "): {"certified": n} for name, n in certified.items()}


def test_brand_name_resolves_to_the_heavier_filer_not_a_same_name_stub():
    index = _index(amazon=2, amazon_com_services=92132)
    assert ss.match_employer("amazon", index, list(index), 90)[0] == "amazon com services"


@pytest.mark.parametrize("order", [1, -1])
def test_match_does_not_depend_on_row_order(order):
    index = _index(meta_4=0, meta_platforms=38117)
    keys = list(index)[::order]
    assert ss.match_employer("meta", index, keys, 90)[0] == "meta platforms"


def test_exact_name_beats_a_heavier_near_miss():
    index = _index(databricks=120, databrick_solutions=5000)
    assert ss.match_employer("databricks", index, list(index), 90) == ("databricks", 100)


def test_fetch_stores_the_lca_name_beside_the_display_name(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    def board(slug):
        return [{"job_key": "greenhouse:instacart:1", "source": "greenhouse",
                 "title": "Software Engineer", "location": "San Francisco, CA",
                 "url": "https://instacart.careers/job/?gh_jid=1",
                 "posted": "2024-10-04", "description": "Python."}]

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"greenhouse": board})
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n"
        "    - {slug: instacart, name: Instacart, lca_name: Maplebear}\n",
        encoding="utf-8")
    ss.cmd_fetch_jobs(argparse.Namespace(companies=str(companies), replace=True, delay=0,
                                          workday_days=3))

    con = sqlite3.connect(tmp_path / "t.db")
    row = con.execute("SELECT company, company_norm, lca_name FROM jobs").fetchone()
    con.close()
    assert row == ("Instacart", "instacart", "Maplebear")


def test_simple_report_does_not_penalise_withdrawn_filings(monkeypatch, tmp_path, capsys):
    import argparse
    import csv

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    con = ss.connect()
    con.executemany(
        "INSERT INTO employers (employer_norm, employer_display, certified, denied, "
        "withdrawn, titles, states) VALUES (?,?,?,?,?,?,?)",
        [("steady", "Steady Inc", 100, 0, 0, "[]", "[]"),
         ("churn", "Churn Inc", 100, 0, 60, "[]", "[]")])
    con.executemany(
        "INSERT INTO jobs (job_key, source, company, company_norm, title, location, url, "
        "posted, description) VALUES (?,?,?,?,?,?,?,?,?)",
        [(f"t:{n}", "test", n.title(), n, "Software Engineer", "Austin, TX",
          f"https://example.com/{n}", "2026-11-04", "Python.") for n in ("steady", "churn")])
    con.commit()
    con.close()

    out = tmp_path / "r.csv"
    ss.cmd_report(argparse.Namespace(locations="", any_location=True, include_senior=False,
                                     sponsors_only=False, fuzzy_cutoff=90, top=0,
                                     out=str(out)))
    with open(out, encoding="utf-8-sig", newline="") as fh:
        scores = {r["company"]: r["score"] for r in csv.DictReader(fh)}
    assert scores["Churn"] == scores["Steady"]


# ------------------------------------------------- load-lca, several files
#
# DOL has published both ways: FY2025's quarterly files hold one quarter each,
# while the FY2026 Q3 file holds every decision from October through June. So
# each row is filed under the quarter its decision date falls in, and a file
# replaces exactly the quarters it contains. Nothing is counted twice, and
# nothing is lost, whichever kind of file comes in.

Q1, Q2, Q3, Q4 = "2025-12-15", "2026-03-15", "2026-06-15", "2026-09-15"


def _lca_file(path, rows):
    """A minimal disclosure CSV: one certified filing per (employer, decided)."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CASE_STATUS", "EMPLOYER_NAME", "JOB_TITLE", "WORKSITE_STATE",
                    "PW_WAGE_LEVEL", "DECISION_DATE"])
        for name, decided in rows:
            w.writerow(["Certified", name, "Software Engineer", "CA", "II", decided])
    return str(path)


def _load(path, replace=False):
    import argparse
    ss.cmd_load_lca(argparse.Namespace(path=path, latest=False, replace=replace))


def _employer(tmp_path, norm="acme"):
    import sqlite3
    con = sqlite3.connect(tmp_path / "t.db")
    row = con.execute("SELECT certified, first_fy, last_fy FROM employers "
                      "WHERE employer_norm = ?", (norm,)).fetchone()
    con.close()
    return row


ACME = "Acme Inc"


def test_two_fiscal_years_add_up(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "a.csv", [(ACME, "2025-09-30")] * 3), replace=True)
    _load(_lca_file(tmp_path / "b.csv", [(ACME, Q3)] * 2))
    assert _employer(tmp_path) == (5, 2025, 2026)


def test_single_quarter_files_add_up(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "q3.csv", [(ACME, Q3)] * 2), replace=True)
    _load(_lca_file(tmp_path / "q4.csv", [(ACME, Q4)]))
    assert _employer(tmp_path) == (3, 2026, 2026)


def test_a_year_to_date_file_replaces_the_quarters_it_covers(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "q2.csv", [(ACME, Q1), (ACME, Q2)]), replace=True)
    _load(_lca_file(tmp_path / "q3.csv", [(ACME, Q1), (ACME, Q2), (ACME, Q3)]))
    assert _employer(tmp_path) == (3, 2026, 2026)


def test_an_older_file_keeps_the_newer_quarters(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "q3.csv", [(ACME, Q1), (ACME, Q2), (ACME, Q3)]),
          replace=True)
    _load(_lca_file(tmp_path / "q2.csv", [(ACME, Q1), (ACME, Q2)]))
    assert _employer(tmp_path) == (3, 2026, 2026)


def test_reloading_the_same_file_changes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    path = _lca_file(tmp_path / "q3.csv", [(ACME, Q1), (ACME, Q3)])
    _load(path, replace=True)
    _load(path)
    assert _employer(tmp_path) == (2, 2026, 2026)


def test_replace_starts_over(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "a.csv", [(ACME, "2025-09-30")] * 3 +
                    [("Old Co", "2025-09-30")] * 4), replace=True)
    _load(_lca_file(tmp_path / "b.csv", [(ACME, Q3)] * 2), replace=True)
    assert _employer(tmp_path) == (2, 2026, 2026)
    assert _employer(tmp_path, "old") is None


def test_load_counts_client_site_placements_and_new_hires(monkeypatch, tmp_path):
    import csv
    import sqlite3
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    path = tmp_path / "q3.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CASE_STATUS", "EMPLOYER_NAME", "DECISION_DATE",
                    "SECONDARY_ENTITY", "NEW_EMPLOYMENT"])
        w.writerows([["Certified", "Placer Inc", "2026-06-30", "Yes", "1"],
                     ["Certified", "Placer Inc", "2026-06-30", "Yes", "0"],
                     ["Certified", "Placer Inc", "2026-06-30", "No", "0"],
                     ["Certified", "Placer Inc", "2026-06-30", "No", ""]])
    _load(str(path), replace=True)
    con = sqlite3.connect(tmp_path / "t.db")
    row = con.execute("SELECT secondary, new_hires FROM employers "
                      "WHERE employer_norm = 'placer'").fetchone()
    con.close()
    assert row == (2, 1)


# ------------------------------------------------------- prebuilt table
#
# export-lca writes the per-quarter table and a pointer with its checksum;
# load-lca --prebuilt reads them back. Tests use local paths, never the network.

def _employers(db):
    import sqlite3
    con = sqlite3.connect(db)
    rows = sorted(con.execute("SELECT employer_norm, certified, first_fy, last_fy, "
                              "secondary, new_hires FROM employers"))
    con.close()
    return rows


def _prebuilt(pointer, replace=False):
    import argparse
    ss.cmd_load_lca(argparse.Namespace(path=None, latest=False, prebuilt=str(pointer),
                                       replace=replace))


def _export(monkeypatch, tmp_path):
    import argparse
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "source.db"))
    _load(_lca_file(tmp_path / "a.csv", [(ACME, "2025-09-30")] * 3 +
                    [(ACME, Q1), ("Globex", Q1)]), replace=True)
    out = tmp_path / "release" / "lca.ndjson.gz"
    out.parent.mkdir()
    ss.cmd_export_lca(argparse.Namespace(out=str(out)))
    return out, out.with_name("lca.json")


def test_export_and_prebuilt_load_round_trip(monkeypatch, tmp_path):
    import json
    _, pointer = _export(monkeypatch, tmp_path)
    meta = json.loads(pointer.read_text(encoding="utf-8"))
    assert meta["file"] == "lca.ndjson.gz" and len(meta["sha256"]) == 64
    assert meta["quarters"] == "FY2025 Q4, FY2026 Q1"

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "fresh.db"))
    _prebuilt(pointer, replace=True)
    assert _employers(tmp_path / "fresh.db") == _employers(tmp_path / "source.db")


def test_a_tampered_prebuilt_file_changes_nothing(monkeypatch, tmp_path):
    data, pointer = _export(monkeypatch, tmp_path)
    data.write_bytes(data.read_bytes() + b"x")
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "fresh.db"))
    _load(_lca_file(tmp_path / "b.csv", [("Initech", Q3)]), replace=True)
    before = _employers(tmp_path / "fresh.db")
    with pytest.raises(SystemExit, match="checksum"):
        _prebuilt(pointer, replace=True)
    assert _employers(tmp_path / "fresh.db") == before


def test_prebuilt_table_combines_with_a_dol_file(monkeypatch, tmp_path):
    _, pointer = _export(monkeypatch, tmp_path)
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "fresh.db"))
    _prebuilt(pointer, replace=True)
    _load(_lca_file(tmp_path / "b.csv", [(ACME, Q3)] * 2))
    import sqlite3
    con = sqlite3.connect(tmp_path / "fresh.db")
    assert con.execute("SELECT certified, first_fy, last_fy FROM employers "
                       "WHERE employer_norm = 'acme'").fetchone() == (6, 2025, 2026)
    con.close()


def test_ties_in_top_lists_break_alphabetically():
    # So a table rebuilt from the prebuilt file lists titles in the same order
    # as the database it was exported from, whatever order the rows came in.
    assert list(ss._top({"b": 1, "a": 1, "c": 2}, 3)) == ["c", "a", "b"]
    assert list(ss._top({"a": 1, "b": 1, "c": 2}, 3)) == ["c", "a", "b"]


# ------------------------------------------------- green card (PERM) filings
#
# load-lca recognises a PERM disclosure file by its columns and stores it per
# quarter beside the LCA filings, with the same combining rules.

def _perm_file(path, rows):
    """A minimal PERM CSV in the FY2024+ layout: (employer, status, decided)."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["CASE_NUMBER", "CASE_STATUS", "DECISION_DATE", "EMP_BUSINESS_NAME",
                    "JOB_TITLE"])
        for i, (name, status, decided) in enumerate(rows):
            w.writerow([f"G-{i}", status, decided, name, "Software Engineer"])
    return str(path)


def _perm(tmp_path, norm="acme"):
    import sqlite3
    con = sqlite3.connect(tmp_path / "t.db")
    row = con.execute("SELECT certified, perm_certified, perm_filed FROM employers "
                      "WHERE employer_norm = ?", (norm,)).fetchone()
    con.close()
    return row


def test_perm_file_adds_green_card_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "lca.csv", [(ACME, Q3)] * 2), replace=True)
    _load(_perm_file(tmp_path / "perm.csv", [
        (ACME, "Certified", Q3), (ACME, "Certified - Expired", Q2),
        (ACME, "Denied", Q3), (ACME, "Withdrawn", Q1)]))
    assert _perm(tmp_path) == (2, 2, 4)


def test_perm_only_employer_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "lca.csv", [(ACME, Q3)]), replace=True)
    _load(_perm_file(tmp_path / "perm.csv", [("Globex Inc", "Certified", Q3)]))
    assert _perm(tmp_path, "globex") == (0, 1, 1)


def test_reloading_a_perm_file_changes_nothing_and_keeps_lca(monkeypatch, tmp_path):
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "lca.csv", [(ACME, Q3)] * 3), replace=True)
    path = _perm_file(tmp_path / "perm.csv", [(ACME, "Certified", Q3)])
    _load(path)
    _load(path)
    assert _perm(tmp_path) == (3, 1, 1)


def test_prebuilt_table_carries_perm_filings(monkeypatch, tmp_path):
    import argparse
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    _load(_lca_file(tmp_path / "lca.csv", [(ACME, Q3)]), replace=True)
    _load(_perm_file(tmp_path / "perm.csv", [(ACME, "Certified", Q3)] * 2))
    out = tmp_path / "release" / "lca.ndjson.gz"
    out.parent.mkdir()
    ss.cmd_export_lca(argparse.Namespace(out=str(out)))
    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "fresh.db"))
    _prebuilt(out.with_name("lca.json"), replace=True)
    import sqlite3
    con = sqlite3.connect(tmp_path / "fresh.db")
    assert con.execute("SELECT certified, perm_certified FROM employers "
                       "WHERE employer_norm = 'acme'").fetchone() == (1, 2)
    con.close()


# ------------------------------------------------------------------ amazon

def _amazon_job(n, **extra):
    job = {"id_icims": str(n), "title": f"Software Development Engineer {n}",
           "normalized_location": "Seattle, Washington, USA", "location": "US, WA, Seattle",
           "job_path": f"/en/jobs/{n}/sde", "posted_date": "November  6, 2026",
           "description": "<p>Build services.</p>",
           "basic_qualifications": "- 3+ years of professional software development experience",
           "preferred_qualifications": "- Master's degree in computer science"}
    job.update(extra)
    return job


class _AmazonAPI:
    """Serves pages of `jobs`, recording each request's parameters."""

    def __init__(self, jobs, page=None):
        self.jobs, self.page, self.calls = jobs, page, []

    def __call__(self, url, params=None, headers=None, timeout=None):
        assert url == "https://www.amazon.jobs/en/search.json"
        params = list(params)
        self.calls.append(params)
        offset, limit = int(dict(params)["offset"]), int(dict(params)["result_limit"])
        r = requests.Response()
        r.status_code = 200
        rows = self.page if self.page is not None else self.jobs[offset:offset + limit]
        r._content = json.dumps({"hits": len(self.jobs), "jobs": rows}).encode()
        return r


def test_amazon_job_becomes_a_posting_with_its_qualifications(monkeypatch):
    monkeypatch.setattr(requests, "get", _AmazonAPI([_amazon_job(7)]))
    job, = ss.fetch_amazon("software-development")
    assert job["job_key"] == "amazon:7" and job["source"] == "amazon"
    assert job["location"] == "Seattle, Washington, USA"
    assert job["url"] == "https://www.amazon.jobs/en/jobs/7/sde"
    assert job["posted"] == "2026-11-06"
    # The experience requirement lives in the qualifications, which the
    # report's filters must see.
    assert "Build services." in job["description"]
    assert "3+ years of professional software development" in job["description"]


def test_amazon_pages_us_jobs_in_the_chosen_categories(monkeypatch):
    api = _AmazonAPI([_amazon_job(n) for n in range(ss.AMAZON_PAGE + 5)])
    monkeypatch.setattr(requests, "get", api)
    jobs = ss.fetch_amazon("software-development, data-science")
    assert len(jobs) == ss.AMAZON_PAGE + 5 and len(api.calls) == 2
    first = api.calls[0]
    assert ("normalized_country_code[]", "USA") in first and ("sort", "recent") in first
    assert [v for k, v in first if k == "category[]"] == ["software-development", "data-science"]


def test_amazon_all_searches_every_category(monkeypatch):
    api = _AmazonAPI([_amazon_job(1)])
    monkeypatch.setattr(requests, "get", api)
    ss.fetch_amazon("all")
    assert not [v for k, v in api.calls[0] if k == "category[]"]


def test_amazon_stops_when_the_api_repeats_a_page(monkeypatch):
    # An API that ignores `offset` must not loop to the page cap.
    api = _AmazonAPI([], page=[_amazon_job(n) for n in range(ss.AMAZON_PAGE)])
    monkeypatch.setattr(requests, "get", api)
    assert len(ss.fetch_amazon("all")) == ss.AMAZON_PAGE
    assert len(api.calls) == 2


def test_amazon_links_only_to_its_own_site(monkeypatch):
    jobs = [_amazon_job(1, job_path="https://evil.example/steal"), _amazon_job(2, posted_date="soon")]
    monkeypatch.setattr(requests, "get", _AmazonAPI(jobs))
    first, second = ss.fetch_amazon("all")
    assert first["url"] == ""
    assert second["posted"] == ""  # an unreadable date is left blank, not guessed


# --------------------------------------------------------------- eightfold

class _EightfoldAPI:
    """A fake Eightfold PCSX tenant. `refuse` maps a path to how many 429s to
    send before answering."""

    def __init__(self, positions, refuse=None):
        self.positions, self.refuse, self.calls = positions, dict(refuse or {}), []

    def __call__(self, url, params=None, headers=None, timeout=None):
        path = url.split("/api/pcsx/")[1]
        self.calls.append((path, dict(params)))
        r = requests.Response()
        if self.refuse.get(path, 0) > 0:
            self.refuse[path] -= 1
            r.status_code, r._content = 429, b"Please try again later"
            return r
        r.status_code = 200
        if path == "search":
            start, query = int(params["start"]), params["query"]
            rows = [p for p in self.positions if query in p.get("queries", [query])][start:start + ss.EIGHTFOLD_PAGE]
            body = {"data": {"count": len(self.positions), "positions": rows}}
        else:
            position = next(p for p in self.positions if p["id"] == params["position_id"])
            body = {"data": {"jobDescription": f"<p>About {position['name']}</p>",
                             "publicUrl": position.get("publicUrl", f"https://careers.example.com/job/{position['id']}")}}
        r._content = json.dumps(body).encode()
        return r


def _position(n, name="Software Engineer", **extra):
    return {"id": n, "name": name, "locations": ["United States, Washington, Redmond"],
            "postedTs": 1793930550, "positionUrl": f"/careers/job/{n}", **extra}


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda seconds: None)


def test_eightfold_reads_us_postings_with_descriptions(monkeypatch, no_sleep):
    api = _EightfoldAPI([_position(1)])
    monkeypatch.setattr(requests, "get", api)
    job, = ss.fetch_eightfold("careers.example.com/careers?domain=example.com&query=software engineer")
    assert job["job_key"] == "eightfold:careers.example.com:1" and job["source"] == "eightfold"
    assert job["location"] == "United States, Washington, Redmond"
    assert job["url"] == "https://careers.example.com/job/1"
    assert job["posted"] == "2026-11-06"
    assert "About Software Engineer" in job["description"]
    search = api.calls[0][1]
    assert search["domain"] == "example.com" and search["location"] == "United States"
    assert search["query"] == "software engineer" and search["sort_by"] == "timestamp"


def test_eightfold_skips_senior_titles_without_asking_for_them(monkeypatch, no_sleep):
    api = _EightfoldAPI([_position(1, "Senior Software Engineer"), _position(2, "Software Engineer II"),
                         _position(3, "Principal Applied Scientist"), _position(4, "Software Engineer")])
    monkeypatch.setattr(requests, "get", api)
    jobs = ss.fetch_eightfold("careers.example.com/careers?domain=example.com")
    assert [job["job_key"] for job in jobs] == ["eightfold:careers.example.com:4"]
    assert [params["position_id"] for path, params in api.calls if path == "position_details"] == [4]


def test_eightfold_waits_out_a_rate_limit(monkeypatch, no_sleep):
    api = _EightfoldAPI([_position(1)], refuse={"search": 2})
    monkeypatch.setattr(requests, "get", api)
    assert len(ss.fetch_eightfold("careers.example.com/careers?domain=example.com")) == 1


def test_eightfold_keeps_what_it_read_when_the_site_keeps_refusing(monkeypatch, no_sleep):
    api = _EightfoldAPI([_position(1), _position(2)])
    monkeypatch.setattr(requests, "get", api)
    original = api.__call__

    def refuse_second_detail(url, params=None, **kwargs):
        if url.endswith("position_details") and params["position_id"] == 2:
            api.refuse["position_details"] = 99
        return original(url, params=params, **kwargs)

    monkeypatch.setattr(requests, "get", refuse_second_detail)
    jobs = ss.fetch_eightfold("careers.example.com/careers?domain=example.com")
    assert [job["job_key"] for job in jobs] == ["eightfold:careers.example.com:1"]


def test_eightfold_fails_when_nothing_could_be_read(monkeypatch, no_sleep):
    monkeypatch.setattr(requests, "get", _EightfoldAPI([_position(1)], refuse={"search": 99}))
    with pytest.raises(RuntimeError, match="slow down"):
        ss.fetch_eightfold("careers.example.com/careers?domain=example.com")


def test_eightfold_dedupes_across_queries_and_rejects_foreign_links(monkeypatch, no_sleep):
    api = _EightfoldAPI([_position(1, publicUrl="https://evil.example/job")])
    monkeypatch.setattr(requests, "get", api)
    jobs = ss.fetch_eightfold("careers.example.com/careers?domain=example.com&query=a&query=b")
    assert len(jobs) == 1
    assert jobs[0]["url"] == "https://careers.example.com/careers/job/1"  # its own site, not the foreign link


def test_workday_slug_can_narrow_a_big_board_by_category(monkeypatch):
    # Walmart's board is mostly store roles; its Technology category is 767.
    sent = []

    def fake_post(url, headers=None, timeout=None, json=None):
        sent.append((url, json["appliedFacets"]))
        r = requests.Response()
        r.status_code, r._content = 200, b'{"total": 0, "jobPostings": []}'
        return r

    monkeypatch.setattr(requests, "post", fake_post)
    ss.fetch_workday("walmart/wd504/WalmartExternal?jobFamilyGroup=tech&jobFamilyGroup=data")
    ss.fetch_workday("adobe/wd5/external_experienced")
    assert sent[0] == ("https://walmart.wd504.myworkdayjobs.com/wday/cxs/walmart/WalmartExternal/jobs",
                       {"jobFamilyGroup": ["tech", "data"]})
    assert sent[1][1] == {}
