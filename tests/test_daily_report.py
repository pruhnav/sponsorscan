"""Filtering and scoring rules in sponsor_daily_report.py.

Each test pins one rule. Most of them guard against over-filtering, since a
rule that is slightly too broad removes jobs a candidate should have seen and
leaves no trace in the output.
"""

import pytest

import sponsor_daily_report as sdr


# ------------------------------------------------- experience requirements

@pytest.mark.parametrize("text", [
    "Our 401(k) vests after 3 years of service.",
    "The company was founded 10 years ago.",
    "Unlimited PTO, and a sabbatical every 5 years.",
    "We have been profitable for 8 years running.",
])
def test_years_without_experience_is_not_a_requirement(text):
    """A duration on its own is not an experience requirement.

    Benefits and company history routinely quote a number of years, and
    reading those as requirements removes entry-level postings.
    """
    assert list(sdr.iter_experience_requirements(text)) == []


@pytest.mark.parametrize("text,expected", [
    ("5+ years of experience", (5, None)),
    ("1-2 years of relevant professional experience", (1, 2)),
    ("2+ years of production experience developing services", (2, None)),
    ("3 years' software engineering experience", (3, None)),
    ("Minimum 7 years of industry experience required", (7, None)),
    ("Experience: 4+ years", (4, None)),
])
def test_real_experience_requirements_are_detected(text, expected):
    assert list(sdr.iter_experience_requirements(text))[0] == expected


def test_new_grad_posting_survives_benefit_boilerplate():
    description = (
        "We are hiring new graduates. Our 401(k) vests after 3 years of "
        "service and the company was founded 10 years ago."
    )
    bonus, reason, signals, years = sdr.career_level_score(
        "Software Engineer, New Grad", description, {})
    assert reason is None
    assert bonus is not None
    assert "new grad" in signals
    assert years is None


def test_experience_over_limit_still_rejects():
    bonus, reason, _, years = sdr.career_level_score(
        "Software Engineer", "We need 6+ years of experience.", {})
    assert bonus is None
    assert reason == "6+ years required"
    assert years == 6


# ---------------------------------------------------------- degree filters

def test_degree_required_is_rejected_by_default():
    _, reason, _, _ = sdr.career_level_score(
        "Research Engineer", "A PhD in computer science is required.", {})
    assert reason == "PhD required in description"


def test_degree_as_a_bonus_is_kept_by_default():
    """"A Master's is a plus" is not a requirement and must not disqualify."""
    bonus, reason, _, _ = sdr.career_level_score(
        "Software Engineer", "A Master's degree is a plus. New grads welcome.", {})
    assert reason is None
    assert bonus is not None


def test_mentions_flag_is_stricter_than_required_flag():
    profile = {"reject_masters_mentions": True}
    _, reason, _, _ = sdr.career_level_score(
        "Software Engineer", "A Master's degree is a plus.", profile)
    assert reason == "Master's mentioned in description"


def test_degree_required_flag_can_be_disabled():
    profile = {"reject_phd_required": False}
    bonus, reason, _, _ = sdr.career_level_score(
        "Research Engineer", "A PhD is required.", profile)
    assert reason is None
    assert bonus is not None


def test_postdoc_and_faculty_always_rejected():
    _, reason, _, _ = sdr.career_level_score(
        "Research Engineer", "This is a postdoctoral appointment.", {})
    assert reason == "postdoctoral/faculty role"


# ----------------------------------------------------------- US locations

@pytest.mark.parametrize("location", [
    "San Francisco, CA",
    "Austin, TX 78701",
    "New York",
    "Remote - US",
    "Seattle, Washington",
    "United States",
    "CA",
])
def test_us_locations(location):
    assert sdr.is_us_location(location) is True


@pytest.mark.parametrize("location", [
    "Remote in Europe",   # the word "in" must not read as Indiana
    "Remote or Oregon-adjacent, EMEA",
    "London, UK",
    "Bengaluru, India",
    "Toronto, Canada",
    "Berlin",
    "",
])
def test_non_us_locations(location):
    assert sdr.is_us_location(location) is False


# ------------------------------------------------------------ company tier

@pytest.mark.parametrize("company", ["Fig Inc", "Red", "Scal", "Open"])
def test_partial_names_do_not_inherit_a_tier(company):
    """A short name that is a substring of a tier entry must not inherit it."""
    assert sdr.company_tier(company) == sdr.DEFAULT_TIER


@pytest.mark.parametrize("company,tier", [
    ("Nvidia Corporation", 5),
    ("OpenAI", 5),
    ("Reddit, Inc.", 4),
    ("Netflix", 3),
    ("Some Unlisted Startup LLC", sdr.DEFAULT_TIER),
])
def test_full_names_get_their_tier(company, tier):
    assert sdr.company_tier(company) == tier


def test_profile_company_tiers_replace_the_defaults():
    tiers = sdr.build_company_tiers({"company_tiers": {"5": ["Acme Robotics"], "4": []}})
    assert sdr.company_tier("Acme Robotics Inc", tiers) == 5
    # Once a profile supplies its own table, the built-in entries do not apply.
    assert sdr.company_tier("OpenAI", tiers) == sdr.DEFAULT_TIER


def test_empty_profile_tiers_fall_back_to_defaults():
    tiers = sdr.build_company_tiers({"company_tiers": {"5": [], "4": [], "3": []}})
    assert tiers == sdr.DEFAULT_COMPANY_TIERS


# ------------------------------------------------------------- preferences

def test_preferred_company_and_location_matching():
    assert sdr.matches_preferred_company("Stripe, Inc.", ["Stripe"]) is True
    assert sdr.matches_preferred_company("Striped Socks Co", ["Stripe"]) is False
    assert sdr.matches_preferred_location("Remote, California", ["california"]) is True
    assert sdr.matches_preferred_location("Austin, TX", ["california"]) is False


# ------------------------------------------------------- work authorization

def test_authorization_flags_gate_their_own_patterns():
    clearance = "An active security clearance is required."
    assert sdr.violates_work_authorization(clearance, {"reject_clearance_roles": True})
    assert not sdr.violates_work_authorization(
        clearance, {"reject_clearance_roles": False,
                    "reject_opt_excluded": False,
                    "reject_citizenship_required": False,
                    "reject_permanent_authorization_required": False})


def test_citizen_only_posting_blocked_for_opt_profile():
    blob = "Applicants must be a US citizen."
    assert sdr.violates_work_authorization(blob, {"reject_citizenship_required": True})


def test_generic_no_sponsorship_is_not_disqualifying_for_opt():
    """An OPT candidate may already hold authorization, so this is not a block."""
    blob = "We do not offer visa sponsorship for this role."
    assert not sdr.violates_work_authorization(blob, {})


def test_now_or_in_the_future_is_disqualifying():
    blob = ("Candidates requiring sponsorship now or in the future "
            "will not be considered.")
    assert sdr.violates_work_authorization(blob, None)


def test_authorization_warnings_flag_contradictions():
    warnings = sdr.authorization_warnings({
        "work_authorization": "us_citizen",
        "reject_citizenship_required": True,
        "reject_permanent_authorization_required": True,
    })
    assert len(warnings) == 2
    assert all("us_citizen" in w for w in warnings)


def test_coherent_profile_produces_no_warnings():
    assert sdr.authorization_warnings({
        "work_authorization": "us_citizen",
        "reject_citizenship_required": False,
        "reject_permanent_authorization_required": False,
    }) == []


# ------------------------------------------------------------ roles/skills

def test_role_aliases_resolve_to_the_shared_definition():
    """A profile saying "AI Engineer" must get the AI/ML patterns and score."""
    roles = sdr.build_target_roles({"target_roles": ["AI Engineer"]})
    name, patterns, score = roles[0]
    assert name == "AI Engineer"
    assert score == 64
    assert any(p.search("Senior AI Engineer") for p in patterns)


def test_unknown_role_falls_back_to_a_literal_match():
    roles = sdr.build_target_roles({"target_roles": ["Quantum Wrangler"]})
    family, base = sdr.classify_role("Quantum Wrangler II", roles)
    assert family == "Quantum Wrangler"
    assert base == sdr.DEFAULT_ROLE_SCORE


def test_default_roles_used_when_profile_lists_none():
    assert sdr.build_target_roles({}) is sdr.TARGET_ROLES
    assert sdr.classify_role("Software Engineer") == ("Software Engineer", 47)


def test_profile_skills_use_the_shared_pattern_table():
    skills = sdr.build_resume_skills({"skills": {"GCP": 5}})
    matched, points = sdr.score_skills("Engineer", "Experience with Google Cloud", skills)
    assert matched == ["GCP"]
    assert points == 5


def test_skill_points_are_capped():
    _, points = sdr.score_skills(
        "Engineer", " ".join(sdr.SKILL_PATTERNS) + " python pytorch tensorflow sql")
    assert points == sdr.MAX_SKILL_POINTS


# ----------------------------------------------------------- posting dates

@pytest.mark.parametrize("value", [None, "", "not a date", "tomorrow"])
def test_unparseable_dates_return_none(value):
    assert sdr.parse_posted_datetime(value) is None


def test_date_only_values_use_end_of_day():
    parsed = sdr.parse_posted_datetime("2026-07-25")
    assert (parsed.hour, parsed.minute) == (23, 59)


def test_millisecond_and_second_timestamps_agree():
    assert (sdr.parse_posted_datetime("1750000000")
            == sdr.parse_posted_datetime("1750000000000"))


def test_naive_iso_is_treated_as_utc():
    assert sdr.parse_posted_datetime("2026-07-25T12:00:00").tzinfo is not None


# --------------------------------------------------------------- job keys

def test_tracking_parameters_do_not_create_a_new_job():
    a = sdr.stable_job_key("Acme", "Engineer", "SF", "https://Acme.com/jobs/1?utm_source=x")
    b = sdr.stable_job_key("Acme", "Engineer", "SF", "https://acme.com/jobs/1/")
    assert a == b


def test_key_falls_back_to_company_and_title_without_a_url():
    key = sdr.stable_job_key("Acme, Inc.", "Software  Engineer", "SF", "")
    assert key == "acme|software engineer|sf"


# -------------------------------------------------------------- priorities

@pytest.mark.parametrize("score,fit,expected", [
    (160, 80, "P1 - Apply ASAP"),
    (140, 70, "P2 - Strong Apply"),
    (120, 50, "P3 - Apply"),
    (100, 50, "P4 - Review"),
])
def test_priority_labels(score, fit, expected):
    assert sdr.priority_label(score, fit) == expected
