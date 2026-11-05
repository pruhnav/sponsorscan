"""Filtering and scoring rules in sponsor_daily_report.py.

Each test pins one rule. Most of them guard against over-filtering, since a
rule that is slightly too broad removes jobs a candidate should have seen and
leaves no trace in the output.
"""

import pytest

import sponsor_daily_report as sdr
import sponsorscan as ss


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
    "Indianapolis, Indiana",  # "india" must not read as a foreign country
    "Indiana - Indianapolis; Texas - Austin",
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


@pytest.mark.parametrize("location, verdict", [
    ("New York or London", "us"),        # a US option among several
    ("Bangalore, India; Austin, TX", "us"),
    ("Paris, TX", "us"),
    ("San Francisco", "unclear"),        # a city with no state or country
    ("Seattle", "unclear"),
    ("Remote", "unclear"),
    ("Hybrid", "unclear"),
    ("2 Locations", "unclear"),
    ("", "unclear"),
    ("Toronto", "non_us"),
    ("London, UK", "non_us"),
    ("Remote - Canada", "non_us"),
    ("Remote or Oregon-adjacent, EMEA", "non_us"),
])
def test_location_verdict(location, verdict):
    assert sdr.location_verdict(location) == verdict


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


@pytest.mark.parametrize("blob", [
    "We are unable to sponsor now or in the future.",
    "We will not sponsor employment visas now or in the future.",
    "Candidates requiring sponsorship now or in the future will not be considered.",
])
def test_refusing_future_sponsorship_blocks_an_opt_profile(blob):
    # OPT ends; a posting that will never sponsor is a dead end, and its
    # "now or in the future" question gets an automatic no.
    assert sdr.violates_work_authorization(blob, {"work_authorization": "opt"})


def test_future_sponsorship_refusal_can_be_kept_by_choice():
    blob = "We are unable to sponsor now or in the future."
    assert not sdr.violates_work_authorization(
        blob, {"work_authorization": "opt", "reject_no_future_sponsorship": False})


def test_future_sponsorship_refusal_never_blocks_a_citizen():
    blob = "We are unable to sponsor now or in the future."
    assert not sdr.violates_work_authorization(
        blob, {"work_authorization": "us_citizen", "reject_no_future_sponsorship": True})


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


# -------------------------------------------------- extended skill vocabulary
#
# The original table was Python/ML oriented, so a backend or data candidate
# fell back to literal matching for most of their stack. Each case below is a
# phrase a real posting would use.

NEW_SKILL_CASES = [
    ("PostgreSQL", "Experience with PostgreSQL and Redis"),
    ("PostgreSQL", "We run Postgres in production"),
    ("MongoDB", "Familiarity with MongoDB"),
    ("MongoDB", "Data stored in Mongo"),
    ("Redis", "Caching with Redis"),
    ("Kubernetes", "Deploy to Kubernetes"),
    ("Kubernetes", "Experience with K8s"),
    ("Terraform", "Infrastructure as code using Terraform"),
    ("Linux", "Comfortable on Linux"),
    ("CI/CD", "Build and maintain CI/CD pipelines"),
    ("CI/CD", "Experience with continuous integration"),
    ("Rust", "Systems programming in Rust"),
    ("C#", "Backend services in C#"),
    (".NET", "Building on .NET"),
    ("Ruby", "Our API is written in Ruby"),
    ("PHP", "Legacy PHP services"),
    ("Kotlin", "Android development with Kotlin"),
    ("Swift", "iOS development in Swift"),
    ("Scala", "Data pipelines in Scala"),
    ("Spark", "Large scale processing with Apache Spark"),
    ("Spark", "Experience with PySpark"),
    ("Kafka", "Event streaming via Kafka"),
    ("Airflow", "Orchestration with Airflow"),
    ("Snowflake", "Warehouse on Snowflake"),
    ("Databricks", "We use Databricks"),
    ("Tableau", "Dashboards in Tableau"),
    ("Power BI", "Reporting with Power BI"),
    ("Angular", "Frontend in Angular"),
    ("Vue", "Components written in Vue.js"),
    ("Next.js", "Built with Next.js"),
    ("Django", "Python web services using Django"),
    ("Spring", "Java services on Spring Boot"),
    ("GraphQL", "Our API uses GraphQL"),
    ("REST APIs", "Design and build REST APIs"),
    ("HTML/CSS", "Strong HTML and CSS fundamentals"),
    ("MATLAB", "Simulation in MATLAB"),
    ("Bash/Shell", "Comfortable writing bash scripts"),
    ("Agile/Scrum", "We work in two week Scrum sprints"),
    ("Selenium", "Automated browser tests with Selenium"),
    ("Go", "Experience with Go, Python, and Rust"),
    ("Go", "Backend written in Golang"),
]


@pytest.mark.parametrize("skill,posting", NEW_SKILL_CASES)
def test_extended_vocabulary_matches_real_phrasing(skill, posting):
    assert skill in sdr.SKILL_PATTERNS, f"{skill} missing from SKILL_PATTERNS"
    compiled = sdr.build_resume_skills({"skills": {skill: 5}})
    matched, _ = sdr.score_skills("Engineer", posting, compiled)
    assert matched == [skill], f"{skill!r} did not match {posting!r}"


# Names that are ordinary English words need to stay narrow, or they match
# prose in every posting and flatten the ranking.

FALSE_POSITIVE_CASES = [
    ("Go", "We want someone who can go fast and ship features"),
    ("Spring", "Summer and Spring 2027 internships are open"),
    ("Excel", "Candidates who excel at communication"),
    ("Excel", "You will excel in a fast paced team"),
]


@pytest.mark.parametrize("skill,posting", FALSE_POSITIVE_CASES)
def test_ambiguous_names_do_not_match_ordinary_prose(skill, posting):
    compiled = sdr.build_resume_skills({"skills": {skill: 5}})
    matched, _ = sdr.score_skills("Engineer", posting, compiled)
    assert matched == [], f"{skill!r} wrongly matched {posting!r}"


def test_excel_still_matches_the_actual_tool():
    compiled = sdr.build_resume_skills({"skills": {"Excel": 5}})
    matched, _ = sdr.score_skills("Analyst", "Advanced Microsoft Excel required", compiled)
    assert matched == ["Excel"]


# ------------------------------------------------------ senior vs early career

@pytest.mark.parametrize("title", [
    "Technical Program Manager Intern",
    "Team Lead Intern, Summer 2027",
    "Product Manager, New Grad",
    "Solutions Architect - Early Career",
    "Entry-Level Engineering Manager Apprentice",
])
def test_early_career_wording_beats_a_senior_word(title):
    score, reason, _, _ = sdr.career_level_score(title, "")
    assert score is not None, reason


@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Senior Associate, Data",
    "Lead Data Scientist",
    "Staff Engineer, Junior Mentorship Program",
])
def test_senior_titles_stay_rejected(title):
    score, reason, _, _ = sdr.career_level_score(title, "")
    assert score is None and reason == "senior-level title"



# ------------------------------------------------ sponsorship for citizens

HEAVY_FILER = {"certified": 500, "display": "Big Co", "senior_share": 0.6,
               "titles": ["software engineer"], "trouble_rate": 0.0}
CITIZEN = {"work_authorization": "us_citizen"}


def test_citizen_scores_the_same_with_or_without_filing_history():
    blob = "Visa sponsorship is available."
    heavy = sdr.sponsorship_score(HEAVY_FILER, "Software Engineer", blob, profile=CITIZEN)
    none = sdr.sponsorship_score(None, "Software Engineer", "", profile=CITIZEN)
    assert heavy[0] == none[0] == sdr.CITIZEN_SPONSORSHIP_POINTS


def test_opt_candidate_still_scores_filing_history():
    opt = {"work_authorization": "opt"}
    heavy, _ = sdr.sponsorship_score(HEAVY_FILER, "Software Engineer", "", profile=opt)
    none, _ = sdr.sponsorship_score(None, "Software Engineer", "", profile=opt)
    assert heavy > none == 0


# ------------------------------------------------- false drops (career level)
#
# Each "keep" case was dropped by the filters before; each "drop" case is a
# real requirement that must keep dropping.

@pytest.mark.parametrize("description", [
    "3+ years of experience with SQL preferred.",
    "Preferred qualifications:\n3+ years of experience building APIs.",
    "Bachelor's degree or 4 years of equivalent experience.",
    "Our team brings 20 years of experience serving clients.",
    "We have 15+ years of experience in fintech.",
    "BS with 2+ years of experience, or MS with 0 years of experience.",
    "Required skills: proficiency with MS Excel and MS SQL Server.",
    "Must have strong communication skills and familiarity with MS Office.",
    "Required: Bachelor's or Master's degree in Computer Science.",
    "Master's degree or equivalent practical experience required.",
])
def test_not_a_requirement_is_kept(description):
    bonus, reason, _, _ = sdr.career_level_score("Software Engineer", description, {})
    assert bonus is not None, reason


@pytest.mark.parametrize("description, reason", [
    ("Five (5) years of professional experience required.", "5+ years required"),
    ("Three years of experience required.", "3+ years required"),
    ("5+ years of experience required; 7+ years preferred.", "5+ years required"),
    ("You have 3+ years of experience with distributed systems.", "3+ years required"),
    ("We are looking for someone with 3+ years of experience.", "3+ years required"),
    ("Master's degree in Computer Science required.", "Master's required in description"),
    ("PhD or Master's degree required.", "Master's required in description"),
    ("Minimum qualifications: PhD in machine learning.", "PhD required in description"),
])
def test_real_requirement_still_drops(description, reason):
    assert sdr.career_level_score("Software Engineer", description, {})[1] == reason


# ------------------------------------------ false drops (work authorization)

OPT_RULES = {"work_authorization": "opt"}


@pytest.mark.parametrize("blob", [
    "This position does not require a security clearance.",
    "No security clearance is needed for this role.",
    "A security clearance is not required.",
    "This role may be subject to U.S. export control laws; we sponsor visas.",
    "Our products are subject to export controls.",
])
def test_negated_or_boilerplate_restriction_is_kept(blob):
    assert not sdr.violates_work_authorization(blob, OPT_RULES)
    assert not sdr.violates_work_authorization(blob, None)
    assert not any(p.search(blob) for p in ss.DISQ_RE)


@pytest.mark.parametrize("blob", [
    "Must be able to obtain and maintain a security clearance.",
    "An active security clearance is required.",
    "This role requires access to export-controlled technology.",
    "This program is ITAR-regulated.",
    "Only U.S. citizens are eligible for this role.",
])
def test_real_restriction_still_drops(blob):
    assert sdr.violates_work_authorization(blob, OPT_RULES)
    assert sdr.violates_work_authorization(blob, None)
    assert any(p.search(blob) for p in ss.DISQ_RE)


def test_withdrawn_or_denied_filings_do_not_lower_the_score():
    # A withdrawal is usually the candidate taking another offer or the role
    # closing, and "certified - withdrawn" is routine at large sponsors. Neither
    # says the employer will not sponsor.
    opt = {"work_authorization": "opt"}
    clean = sdr.sponsorship_score(HEAVY_FILER, "Software Engineer", "", profile=opt)
    churn = sdr.sponsorship_score(dict(HEAVY_FILER, trouble_rate=0.6),
                                  "Software Engineer", "", profile=opt)
    assert churn == clean


# ------------------------------------------------------ employer signals

def test_signals_name_years_client_sites_and_new_hires():
    emp = dict(HEAVY_FILER, filed=100, secondary=60, new_hires=25,
               first_fy=2024, last_fy=2026)
    signals = sdr.employer_signals(emp, current_fy=2026)
    assert signals["lca_years"] == "FY2024-FY2026"
    assert signals["lca_client_site_share"] == 0.6
    assert signals["lca_new_hire_share"] == 0.25
    assert "most filings place workers at client sites (60%)" in signals["notes"]


def test_a_sponsor_that_stopped_filing_is_flagged():
    emp = dict(HEAVY_FILER, filed=10, secondary=0, new_hires=5,
               first_fy=2023, last_fy=2024)
    assert "no filings since FY2024" in sdr.employer_signals(emp, current_fy=2026)["notes"]


def test_signals_are_blank_without_an_employer():
    signals = sdr.employer_signals(None, current_fy=2026)
    assert signals == {"lca_years": "", "lca_client_site_share": "",
                       "lca_new_hire_share": "", "notes": []}
