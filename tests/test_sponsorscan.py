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


# ------------------------------------------------------------ speedyapply

APPLY = '<img src="https://i.imgur.com/x.png" alt="Apply" width="70"/>'
FEED = f"""# 2027 SWE Jobs

<!-- TABLE_FAANG_START -->
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://www.stripe.com"><strong>Stripe</strong></a> | Software Engineer, New Grad | Seattle, WA | $150k/yr | <a href="https://job-boards.greenhouse.io/stripe/jobs/1?gh_src=x">{APPLY}</a> | 0d |
<!-- TABLE_FAANG_END -->

<!-- TABLE_OTHER_START -->
| Company | Position | Location | Posting | Age |
|---|---|---|---|---|
| <strong>Tom &amp; Co</strong> | Data Engineer Intern | Austin, TX<br>Remote | <a href="https://jobs.example.com/2">{APPLY}</a> | 3d |
<!-- TABLE_OTHER_END -->
"""
DAY = 86400
NOW = 20000 * DAY  # 2024-10-04 00:00 UTC


class _Text:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_speedyapply_rows_carry_their_own_company(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Text(FEED))
    jobs = ss.fetch_speedyapply("speedyapply/2027-SWE-College-Jobs/README.md", today=NOW)

    assert [(j["company"], j["title"], j["posted"]) for j in jobs] == [
        ("Stripe", "Software Engineer, New Grad", "2024-10-04"),
        ("Tom & Co", "Data Engineer Intern", "2024-10-01"),
    ]
    assert jobs[1]["location"] == "Austin, TX; Remote"
    assert jobs[0]["url"] == "https://job-boards.greenhouse.io/stripe/jobs/1?gh_src=x"
    assert jobs[0]["job_key"] == "speedyapply:job-boards.greenhouse.io/stripe/jobs/1"


def test_speedyapply_without_rows_is_a_failure(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Text("# moved to a new repo\n"))
    with pytest.raises(ValueError, match="no job rows"):
        ss.fetch_speedyapply("speedyapply/2027-SWE-College-Jobs/README.md")


def test_speedyapply_slug_must_name_a_file():
    with pytest.raises(ValueError, match="owner/repo/path"):
        ss.fetch_speedyapply("speedyapply/2027-SWE-College-Jobs")


def test_feed_defers_to_a_fetched_board(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    def stripe_board(slug):
        return [{"job_key": "greenhouse:stripe:9", "source": "greenhouse",
                 "title": "Backend Engineer", "location": "Seattle, WA",
                 "url": "https://job-boards.greenhouse.io/stripe/jobs/9",
                 "posted": "2024-10-04", "description": "Python."}]

    def feed(slug):
        return [
            {"job_key": "speedyapply:a", "source": "speedyapply", "company": "Stripe",
             "title": "Software Engineer, New Grad", "location": "Seattle, WA",
             "url": "https://job-boards.greenhouse.io/stripe/jobs/1",
             "posted": "2024-10-04", "description": ""},
            {"job_key": "speedyapply:b", "source": "speedyapply", "company": "Globex Inc.",
             "title": "Data Engineer Intern", "location": "Austin, TX",
             "url": "https://jobs.example.com/2", "posted": "2024-10-04", "description": ""},
            {"job_key": "speedyapply:c", "source": "speedyapply", "company": "Globex",
             "title": "Data Engineer Intern", "location": "Austin, TX",
             "url": "https://jobs.example.com/2/?ref=newgrad", "posted": "2024-10-04",
             "description": ""},
        ]

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FETCHERS", {"greenhouse": stripe_board})
    monkeypatch.setattr(ss, "FEED_FETCHERS", {"speedyapply": feed})
    companies = tmp_path / "companies.yaml"
    companies.write_text(
        "companies:\n  greenhouse:\n    - {slug: stripe, name: Stripe}\n"
        "feeds:\n  speedyapply:\n    - o/r/README.md\n", encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(companies=str(companies), replace=True, delay=0,
                                          workday_days=3))

    con = sqlite3.connect(tmp_path / "t.db")
    jobs = con.execute("SELECT company, source FROM jobs ORDER BY job_key").fetchall()
    fetched = {r[0] for r in con.execute("SELECT company_norm FROM fetched_companies")}
    con.close()
    assert jobs == [("Stripe", "greenhouse"), ("Globex Inc.", "speedyapply")], \
        "a fetched board's company and a repeated URL must not come in twice"
    assert fetched == {"stripe"}, "a feed's companies are not boards"


def test_failed_feed_is_recorded(monkeypatch, tmp_path):
    import argparse
    import sqlite3

    def down(slug):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ss, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(ss, "FEED_FETCHERS", {"speedyapply": down})
    companies = tmp_path / "companies.yaml"
    companies.write_text("feeds:\n  speedyapply:\n    - o/r/README.md\n", encoding="utf-8")

    ss.cmd_fetch_jobs(argparse.Namespace(companies=str(companies), replace=True, delay=0,
                                          workday_days=3))

    con = sqlite3.connect(tmp_path / "t.db")
    boards = [r[0] for r in con.execute("SELECT board FROM fetch_failures")]
    con.close()
    assert boards == ["speedyapply/o/r/README.md"]


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
