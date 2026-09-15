"""Unit tests for the pieces of sponsorscan.py that do not need a database.

The end-to-end load/report/discover flow is covered by selftest.py.
"""

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
