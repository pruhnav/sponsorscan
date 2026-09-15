#!/usr/bin/env python3
"""
SponsorScan personalized daily report.

Reads the database built by `sponsorscan.py fetch-jobs`, applies candidate
eligibility rules from a profile, ranks what survives, and writes two CSVs:
everything currently matching, and everything new since the previous run.

Run it after a fetch:

    python sponsorscan.py fetch-jobs --replace
    python sponsor_daily_report.py --profile profiles/my_profile.json

On the first run every match counts as new, because there is no prior snapshot.
"""

import argparse
import csv
import json
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from profile_loader import ProfileError, load_profile, describe_profile
from sponsorscan import HAVE_RAPIDFUZZ, SENIOR_TITLE, match_employer, norm_employer

DB_PATH = Path(os.environ.get("SPONSORSCAN_DB", "sponsorscan.db"))
DEFAULT_STATE = Path(".sponsorscan_state.json")


# ---------------------------------------------------------------- role fit
#
# Every role family is defined once: display name, the patterns that identify
# it in a job title, and the base score it contributes. `build_target_roles`
# resolves the names a profile asks for against this table, so a profile and
# the built-in default always agree on what a family means.

ROLE_DEFINITIONS = [
    ("Machine Learning Engineer",
     [r"\bmachine learning engineer\b", r"\bml engineer\b",
      r"\bml platform engineer\b"], 64),
    ("AI/ML Engineer",
     [r"\bai[ /-]?ml engineer\b", r"\bai engineer\b",
      r"\bartificial intelligence engineer\b", r"\bgenerative ai engineer\b",
      r"\bgenai engineer\b"], 64),
    ("Research Engineer",
     [r"\bresearch engineer\b", r"\bmachine learning research engineer\b",
      r"\bai research engineer\b"], 62),
    ("Applied Scientist",
     [r"\bapplied scientist\b", r"\bapplied machine learning scientist\b"], 61),
    ("Computer Vision Engineer",
     [r"\bcomputer vision engineer\b", r"\bvision engineer\b"], 60),
    ("ML Researcher",
     [r"\bml researcher\b", r"\bmachine learning researcher\b",
      r"\bai researcher\b"], 59),
    ("Research Scientist",
     [r"\bresearch scientist\b", r"\bai scientist\b"], 58),
    ("Data Scientist",
     [r"\bdata scientist\b", r"\bmachine learning scientist\b"], 57),
    ("Data Engineer",
     [r"\bdata engineer\b", r"\banalytics engineer\b"], 52),
    ("Backend Engineer",
     [r"\bback[ -]?end (?:software )?engineer\b", r"\bbackend developer\b"], 50),
    ("Full Stack Engineer",
     [r"\bfull[ -]?stack (?:software )?engineer\b",
      r"\bfull[ -]?stack developer\b"], 50),
    ("Python Engineer",
     [r"\bpython engineer\b", r"\bpython developer\b"], 49),
    ("Software Engineer",
     [r"\bsoftware engineer\b", r"\bsoftware developer\b",
      r"\bsoftware development engineer\b", r"\bsde\b"], 47),
]

# Names a profile may use for a family listed above under a different label.
ROLE_NAME_ALIASES = {
    "AI Engineer": "AI/ML Engineer",
    "MLE": "Machine Learning Engineer",
    "SWE": "Software Engineer",
}

DEFAULT_ROLE_SCORE = 47

TARGET_ROLES = [(name, [re.compile(p, re.I) for p in pats], score)
                for name, pats, score in ROLE_DEFINITIONS]

_ROLE_BY_NAME = {name: (pats, score) for name, pats, score in ROLE_DEFINITIONS}


def build_target_roles(profile):
    """Compile the profile's target roles, falling back to the built-in set."""
    roles = (profile or {}).get("target_roles") or []
    if not roles:
        return TARGET_ROLES

    compiled = []
    for role in roles:
        canonical = ROLE_NAME_ALIASES.get(role, role)
        patterns, score = _ROLE_BY_NAME.get(
            canonical, ([rf"\b{re.escape(role)}\b"], DEFAULT_ROLE_SCORE))
        compiled.append(
            (role, [re.compile(p, re.I) for p in patterns], score))
    return compiled


# --------------------------------------------------------------- resume fit
#
# SKILL_PATTERNS is the only place a skill's regexes live. DEFAULT_RESUME_WEIGHTS
# assigns weights to a subset of them for the no-profile case. A profile naming
# a skill not listed here falls back to a literal word match on the name.

SKILL_PATTERNS = {
    "Python": [r"\bpython\b"],
    "Java": [r"\bjava\b"],
    "JavaScript": [r"\bjavascript\b"],
    "TypeScript": [r"\btypescript\b"],
    "C++": [r"\bc\+\+\b"],
    "SQL": [r"\bsql\b", r"\bmysql\b", r"\bpostgres(?:ql)?\b", r"\bsqlite\b"],
    "AWS": [r"\baws\b", r"\bamazon web services\b"],
    "Azure": [r"\bazure\b"],
    "GCP": [r"\bgcp\b", r"\bgoogle cloud\b"],
    "React": [r"\breact\b"],
    "Node.js": [r"\bnode\.?js\b"],
    "Docker": [r"\bdocker\b"],
    "Git": [r"\bgit\b", r"\bgithub\b"],
    "Machine Learning": [r"\bmachine learning\b", r"\bml\b"],
    "Deep Learning": [r"\bdeep learning\b", r"\bneural network"],
    "TensorFlow": [r"\btensorflow\b"],
    "Keras": [r"\bkeras\b"],
    "TensorFlow/Keras": [r"\btensorflow\b", r"\bkeras\b"],
    "PyTorch": [r"\bpytorch\b"],
    "Scikit-learn": [r"\bscikit.?learn\b", r"\bsklearn\b"],
    "Pandas": [r"\bpandas\b"],
    "NumPy": [r"\bnumpy\b"],
    "Pandas/NumPy": [r"\bpandas\b", r"\bnumpy\b"],
    "NLP": [r"\bnlp\b", r"\bnatural language processing\b"],
    "LLMs": [r"\bllm", r"\blarge language model"],
    "NLP/LLMs": [r"\bnlp\b", r"\bllm", r"\blarge language model"],
    "RAG": [r"\brag\b", r"\bretrieval.?augmented\b"],
    "LangChain": [r"\blangchain\b"],
    "RAG/LangChain": [r"\brag\b", r"\bretrieval.?augmented\b", r"\blangchain\b"],
    "Computer Vision": [r"\bcomputer vision\b", r"\bimage processing\b"],
    "Time Series/Forecasting": [r"\btime.?series\b", r"\bforecast"],
    "Research/Publications": [r"\bresearch\b", r"\bpublication\b", r"\bieee\b",
                              r"\bscientific\b"],
    "Flask": [r"\bflask\b"],
    "FastAPI": [r"\bfastapi\b"],
    "Streamlit": [r"\bstreamlit\b"],
    "Flask/Streamlit": [r"\bflask\b", r"\bstreamlit\b"],
    "APIs/Cloud": [r"\bapi\b", r"\baws\b", r"\bazure\b", r"\bgcp\b", r"\bcloud\b"],
    "React/Node": [r"\breact\b", r"\bnode\.?js\b"],
}

DEFAULT_RESUME_WEIGHTS = {
    "Python": 7, "Machine Learning": 7, "Deep Learning": 6,
    "TensorFlow/Keras": 6, "PyTorch": 6, "Research/Publications": 6,
    "NLP/LLMs": 5, "RAG/LangChain": 5, "Time Series/Forecasting": 5,
    "SQL": 4, "Scikit-learn": 4, "Pandas/NumPy": 4, "Computer Vision": 4,
    "APIs/Cloud": 3, "React/Node": 3, "Flask/Streamlit": 3,
    "C++": 2, "Java": 2, "JavaScript": 2,
}

MAX_SKILL_POINTS = 42


def _compile_skills(weights):
    compiled = []
    for skill, weight in weights.items():
        patterns = SKILL_PATTERNS.get(skill, [rf"\b{re.escape(skill)}\b"])
        compiled.append(
            (skill, [re.compile(p, re.I) for p in patterns], int(weight)))
    return compiled


RESUME_SKILLS = _compile_skills(DEFAULT_RESUME_WEIGHTS)


def build_resume_skills(profile):
    """Compile the profile's weighted skills, or the built-in resume set."""
    skills = (profile or {}).get("skills") or {}
    return _compile_skills(skills) if skills else RESUME_SKILLS


# ----------------------------------------------------------- career level

POSITIVE_LEVEL_PATTERNS = [
    ("new grad", re.compile(r"\bnew ?grad(?:uate)?\b", re.I), 18),
    ("university graduate",
     re.compile(r"\b(?:university|college) grad(?:uate)?\b", re.I), 16),
    ("early career", re.compile(r"\bearly career\b", re.I), 14),
    ("entry level", re.compile(r"\bentry[ -]?level\b", re.I), 14),
    ("0-2 years", re.compile(r"\b0\s*(?:-|–|to)\s*2\s*(?:years?|yrs?)\b", re.I), 14),
    ("0-1 years", re.compile(r"\b0\s*(?:-|–|to)\s*1\s*(?:years?|yrs?)\b", re.I), 14),
    ("1-2 years", re.compile(r"\b1\s*(?:-|–|to)\s*2\s*(?:years?|yrs?)\b", re.I), 11),
    ("1-3 years", re.compile(r"\b1\s*(?:-|–|to)\s*3\s*(?:years?|yrs?)\b", re.I), 8),
    ("associate", re.compile(r"\bassociate\b", re.I), 7),
    ("level I", re.compile(r"\b(?:engineer|scientist|developer)\s+i\b", re.I), 8),
    ("internship", re.compile(r"\bintern(?:ship)?\b", re.I), 8),
    ("bachelor's", re.compile(r"\bbachelor'?s?(?: degree)?\b", re.I), 5),
]

PHD_TITLE = re.compile(
    r"\b(?:ph\.?\s*d\.?|doctoral|doctorate|post[ -]?doc(?:toral)?)\b", re.I)

LEVEL_II_PLUS_TITLE = re.compile(
    r"(?:"
    # Roman numerals
    r"\b(?:engineer|scientist|developer|analyst|researcher|software engineer|"
    r"machine learning engineer|data scientist)\s*(?:,|-)?\s*(?:ii|iii|iv|v)\b"
    # Numeric levels (Engineer 2, Software Engineer 3, ...)
    r"|\b(?:engineer|scientist|developer|analyst|researcher|software engineer|"
    r"machine learning engineer|data scientist)\s*(?:,|-)?\s*(?:2|3|4|5)\b"
    # Generic level labels
    r"|\b(?:level|lvl)\s*(?:2|3|4|5|ii|iii|iv|v)\b"
    # SWE/SDE abbreviations
    r"|\b(?:swe|sde|mle)\s*(?:2|3|4|5|ii|iii|iv|v)\b"
    r")",
    re.I,
)

_DEGREE_REQUIRED_TEMPLATE = (
    r"(?:"
    r"\b(?:must|requires?|required|need(?:ed)?|minimum qualification(?:s)?|"
    r"basic qualification(?:s)?)\b[^.\n]{{0,120}}\b(?:{degree})\b"
    r"|\b(?:{degree})\b[^.\n]{{0,80}}\b(?:required|must have|is required|minimum)\b"
    r"|\byou (?:hold|have)\b[^.\n]{{0,40}}\b(?:a\s+)?(?:{degree})\b"
    r")"
)

PHD_REQUIRED_RE = re.compile(
    _DEGREE_REQUIRED_TEMPLATE.format(
        degree=r"ph\.?\s*d\.?|doctorate|doctoral degree"), re.I)

MASTERS_REQUIRED_RE = re.compile(
    _DEGREE_REQUIRED_TEMPLATE.format(
        degree=r"master'?s degree|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree"), re.I)

# Any mention at all, required or not. Much blunter than the two above: it
# also fires on "a Master's is a plus", so it is opt-in.
PHD_MENTION_RE = re.compile(
    r"\b(?:ph\.?\s*d\.?|doctorate|doctoral(?: degree)?)\b", re.I)

MASTERS_MENTION_RE = re.compile(
    r"\b(?:master'?s(?: degree)?|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree)\b", re.I)

ACADEMIC_ROLE_RE = re.compile(
    r"\b(?:postdoctoral|post-doc|postdoc|faculty|professor)\b", re.I)

# Years-of-experience requirements, such as:
#   "10+ years of overall experience"
#   "2+ years of production experience developing ..."
#   "3 years' software engineering experience"
#   "1-2 years of relevant professional experience"
#   "experience: 5+ years"
#
# The word "experience" is mandatory, so that durations belonging to benefits
# and company history ("401(k) vests after 3 years of service", "founded 10
# years ago") are not read as requirements. Up to six descriptive words may sit
# between the duration and "experience".
EXPERIENCE_REQUIREMENT_RE = re.compile(
    r"(?:"
    r"(?P<min>\d{1,2})\s*"
    r"(?:\+|(?:-|–|—|to)\s*(?P<max>\d{1,2}))?\s*"
    r"(?:years?|yrs?)\b['’]?\s*"
    r"(?:of\s+|in\s+|with\s+)?"
    r"(?:[\w/+#.&-]+\s+){0,6}?"
    r"experience\b"
    r"|"
    r"experience\b\s*[:–-]?\s*(?:of\s+|at least\s+|minimum\s+(?:of\s+)?)?"
    r"(?P<min2>\d{1,2})\s*"
    r"(?:\+|(?:-|–|—|to)\s*(?P<max2>\d{1,2}))?\s*"
    r"(?:years?|yrs?)\b"
    r")",
    re.I,
)


def iter_experience_requirements(text):
    """Yield (minimum, maximum) year pairs stated as experience requirements."""
    for match in EXPERIENCE_REQUIREMENT_RE.finditer(text or ""):
        minimum = match.group("min") or match.group("min2")
        maximum = match.group("max") or match.group("max2")
        if minimum is None:
            continue
        yield int(minimum), int(maximum) if maximum else None


# ------------------------------------------------------------ sponsorship
#
# OPT-aware hard exclusions.
#
# A generic "no visa sponsorship available" is NOT enough on its own to drop a
# job, because an OPT/STEM OPT candidate may already hold temporary employment
# authorization. Only language that clearly blocks OPT candidates, demands
# permanent or unrestricted authorization, limits the role to citizens or
# permanent residents, or requires clearance/export status is disqualifying.

DISQUALIFIERS = [
    # Explicitly refuses candidates who need sponsorship now OR later.
    r"\b(?:cannot|can't|will not|won't|unable to|do not|does not|not able to)\b"
    r"[^.\n]{0,100}\b(?:sponsor|provide sponsorship)\b[^.\n]{0,100}"
    r"\b(?:now\s*(?:or|and)\s*(?:in the )?future|currently\s*(?:or|and)\s*(?:in the )?future)\b",
    r"\b(?:no|without)\b[^.\n]{0,80}\b(?:current or future|now or future)\b"
    r"[^.\n]{0,80}\b(?:visa|immigration|employment)\s+sponsorship\b",
    r"\b(?:must|need to)\b[^.\n]{0,80}\b(?:not require|never require)\b"
    r"[^.\n]{0,80}\bsponsorship\b",
    r"\bcandidates?\s+(?:requiring|who require)\b[^.\n]{0,80}"
    r"\b(?:now or in the future|current or future)\b[^.\n]{0,80}"
    r"\b(?:are not eligible|will not be considered)\b",

    # Explicitly excludes OPT/CPT or temporary employment authorization.
    r"\b(?:opt|stem opt|cpt)\b[^.\n]{0,80}"
    r"\b(?:not accepted|not eligible|not supported|will not be considered)\b",
    r"\b(?:not accepting|cannot employ|unable to employ)\b[^.\n]{0,80}"
    r"\b(?:opt|stem opt|cpt)\b",

    # Requires permanent or unrestricted work authorization.
    r"\b(?:permanent|unrestricted)\s+(?:u\.?\s?s\.?\s+)?work authorization\b",
    r"\bauthorized to work\b[^.\n]{0,100}\bwithout\b[^.\n]{0,60}"
    r"\b(?:current or future|now or future)\b[^.\n]{0,60}\bsponsorship\b",
    r"\bmust be\b[^.\n]{0,80}\b(?:permanent resident|green card holder)\b",

    # Citizenship and regulated-access restrictions.
    r"\bmust be (?:a |an )?(?:u\.?\s?s\.?|united states)\s?(?:citizen|person|national)\b",
    r"\b(?:u\.?\s?s\.?|united states)\s?citizenship (?:is )?required\b",
    r"\b(?:u\.?\s?s\.?|united states)\s?(?:citizens?|persons?)\s+only\b",
    r"\bsecurity clearance\b",
    r"\bitar\b",
    r"\bexport control(?:led|s)?\b",
]

SPONSOR_POSITIVE = [
    r"\bvisa sponsorship (?:is )?available\b",
    r"\bwe (?:will )?sponsor\b",
    r"\bh-?1b sponsorship\b",
    r"\bsponsorship (?:is )?(?:available|offered|provided)\b",
]

DISQ_RE = [re.compile(p, re.I) for p in DISQUALIFIERS]
SPONSOR_POS_RE = [re.compile(p, re.I) for p in SPONSOR_POSITIVE]

# Each authorization check, keyed by the profile flag that enables it.
AUTHORIZATION_CHECKS = {
    "reject_opt_excluded": [
        re.compile(r"\b(?:opt|stem opt|cpt)\b[^.\n]{0,100}"
                   r"\b(?:not accepted|not eligible|not supported|"
                   r"will not be considered|cannot be hired)\b", re.I),
        re.compile(r"\b(?:not accepting|cannot employ|unable to employ)\b"
                   r"[^.\n]{0,100}\b(?:opt|stem opt|cpt)\b", re.I),
    ],
    "reject_permanent_authorization_required": [
        re.compile(r"\b(?:permanent|unrestricted)\s+"
                   r"(?:u\.?\s?s\.?\s+)?work authorization\b", re.I),
        re.compile(r"\bmust be\b[^.\n]{0,100}"
                   r"\b(?:permanent resident|green card holder)\b", re.I),
    ],
    "reject_citizenship_required": [
        re.compile(r"\bmust be (?:a |an )?(?:u\.?\s?s\.?|united states)\s?"
                   r"(?:citizen|person|national)\b", re.I),
        re.compile(r"\b(?:u\.?\s?s\.?|united states)\s?"
                   r"citizenship (?:is )?required\b", re.I),
        re.compile(r"\b(?:u\.?\s?s\.?|united states)\s?"
                   r"(?:citizens?|persons?)\s+only\b", re.I),
    ],
    "reject_clearance_roles": [
        re.compile(r"\bsecurity clearance\b", re.I),
        re.compile(r"\bitar\b", re.I),
        re.compile(r"\bexport control(?:led|s)?\b", re.I),
    ],
}


def violates_work_authorization(blob, profile):
    """True when the posting conflicts with the profile's authorization rules."""
    if not profile:
        return any(pattern.search(blob) for pattern in DISQ_RE)

    return any(
        pattern.search(blob)
        for flag, patterns in AUTHORIZATION_CHECKS.items()
        if profile.get(flag, True)
        for pattern in patterns
    )


# ----------------------------------------------------------- company fit
#
# A starting point only. A profile's `company_tiers` replaces this table
# outright, so the ranking reflects whoever the report is being run for.

DEFAULT_COMPANY_TIERS = {
    5: {"openai", "anthropic", "waymo", "databricks", "snowflake", "nvidia",
        "scale ai", "figma", "roblox"},
    4: {"pinterest", "reddit", "airbnb", "twilio", "robinhood", "samsara",
        "zoox", "palantir", "stripe", "coreweave", "datadog", "mongodb",
        "jane street", "moloco"},
    3: {"spotify", "toast", "roku", "gen digital", "nuro", "doordash",
        "benchling", "netflix"},
}

COMPANY_TIER_POINTS = {5: 16, 4: 11, 3: 6}
DEFAULT_TIER = 2
DEFAULT_TIER_POINTS = 2

PREFERRED_COMPANY_POINTS = 8
PREFERRED_LOCATION_POINTS = 6


def build_company_tiers(profile):
    """Resolve the tier table, preferring the profile's own if it has one."""
    configured = (profile or {}).get("company_tiers") or {}
    tiers = {}
    for tier, names in configured.items():
        cleaned = {norm_employer(n) for n in (names or []) if str(n).strip()}
        if cleaned:
            tiers[int(tier)] = cleaned
    return tiers or DEFAULT_COMPANY_TIERS


def company_tier(company, tiers=None):
    """Highest tier whose name appears as whole words in the company name.

    Matching is whole-word and one-directional: the tier name must appear in
    the company name, not the reverse. A looser test would place "Fig Inc" in
    tier 5 on the strength of "figma", and "Red" in tier 4 on "reddit".
    """
    tiers = tiers or DEFAULT_COMPANY_TIERS
    normalized = norm_employer(company)
    if not normalized:
        return DEFAULT_TIER
    for tier in sorted(tiers, reverse=True):
        for name in tiers[tier]:
            if name and re.search(rf"\b{re.escape(name)}\b", normalized):
                return tier
    return DEFAULT_TIER


def matches_preferred_company(company, preferred):
    normalized = norm_employer(company)
    return any(
        norm_employer(p) and re.search(rf"\b{re.escape(norm_employer(p))}\b", normalized)
        for p in (preferred or [])
    )


def matches_preferred_location(location, preferred):
    loc = (location or "").lower()
    return any(str(p).strip().lower() in loc for p in (preferred or []) if str(p).strip())


# ------------------------------------------------------------ US location

US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}

NON_US = {
    "canada", "india", "united kingdom", "ireland", "germany", "france",
    "spain", "italy", "netherlands", "belgium", "sweden", "norway", "denmark",
    "finland", "poland", "romania", "portugal", "switzerland", "austria",
    "australia", "new zealand", "singapore", "japan", "china", "taiwan",
    "south korea", "korea", "israel", "brazil", "mexico", "argentina",
    "colombia", "chile", "philippines", "indonesia", "malaysia", "thailand",
    "vietnam", "hong kong", "uae", "dubai", "berlin", "toronto", "vancouver",
    "london", "dublin", "paris", "amsterdam", "munich",
    # Regions that are not the US but read as "remote-friendly".
    "europe", "emea", "apac", "latam", "worldwide", "anywhere in the world",
}

US_EXPLICIT = (
    "united states", "usa", "u.s.", "remote - us", "remote, us", "remote us",
    "us remote", "remote (us)", "north america",
)

# A state code at the end of a segment, optionally followed by a ZIP:
# "San Francisco, CA", "Austin, TX 78701". Deliberately anchored, because a
# bare two-letter scan treats the "in" of "Remote in Europe" as Indiana.
_STATE_CODE_TAIL = re.compile(r"\b([a-z]{2})\b\.?\s*(?:\d{5}(?:-\d{4})?)?$")

_LOCATION_SPLIT = re.compile(r"[,/|;]|\s+-\s+|–|\bor\b|\band\b")


def is_us_location(location):
    loc = (location or "").strip().lower()
    if not loc:
        return False
    if any(term in loc for term in NON_US):
        return False
    if any(term in loc for term in US_EXPLICIT):
        return True
    if any(state in loc for state in US_STATE_NAMES):
        return True
    for segment in _LOCATION_SPLIT.split(loc):
        segment = segment.strip().rstrip(".")
        if not segment:
            continue
        if segment in US_STATE_CODES:
            return True
        tail = _STATE_CODE_TAIL.search(segment)
        if tail and tail.group(1) in US_STATE_CODES:
            return True
    return False


# ------------------------------------------------------------------ scoring

def normalize_url(url):
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        # Drop tracking query strings, keep job identifiers held in the path.
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                           parts.path.rstrip("/"), "", ""))
    except ValueError:
        return url.strip()


def stable_job_key(company, title, location, url):
    normalized_url = normalize_url(url)
    if normalized_url:
        return normalized_url
    return "|".join([
        norm_employer(company),
        re.sub(r"\s+", " ", (title or "").lower()).strip(),
        re.sub(r"\s+", " ", (location or "").lower()).strip(),
    ])


def classify_role(title, target_roles=None):
    for family, patterns, base in (target_roles or TARGET_ROLES):
        if any(p.search(title or "") for p in patterns):
            return family, base
    return None, 0


def score_skills(title, description, resume_skills=None):
    text = f"{title or ''}\n{description or ''}"
    matched, points = [], 0
    for name, patterns, value in (resume_skills or RESUME_SKILLS):
        if any(p.search(text) for p in patterns):
            matched.append(name)
            points += value
    return matched, min(points, MAX_SKILL_POINTS)


def career_level_score(title, description, profile=None):
    """Return (bonus, exclusion_reason, signals, years_required).

    `bonus` is None when the posting is excluded outright, and
    `exclusion_reason` says why.
    """
    title_text = title or ""
    description_text = description or ""
    text = f"{title_text}\n{description_text}"
    profile = profile or {}

    if PHD_TITLE.search(title_text):
        return None, "PhD/doctoral/postdoc title", [], None

    if profile.get("reject_level_ii_plus_titles", True) and \
            LEVEL_II_PLUS_TITLE.search(title_text):
        return None, "level II or above title", [], None

    if profile.get("reject_senior_titles", True) and SENIOR_TITLE.search(title_text):
        return None, "senior-level title", [], None

    if ACADEMIC_ROLE_RE.search(text):
        return None, "postdoctoral/faculty role", [], None

    # A stated requirement for a graduate degree. Distinct from a passing
    # mention, and on by default because it is a genuine hard blocker.
    if profile.get("reject_phd_required", True) and \
            PHD_REQUIRED_RE.search(description_text):
        return None, "PhD required in description", [], None

    if profile.get("reject_masters_required", True) and \
            MASTERS_REQUIRED_RE.search(description_text):
        return None, "Master's required in description", [], None

    # Any mention whatsoever, including "a PhD is a plus". Opt-in.
    if profile.get("reject_phd_mentions", False) and \
            PHD_MENTION_RE.search(description_text):
        return None, "PhD mentioned in description", [], None

    if profile.get("reject_masters_mentions", False) and \
            MASTERS_MENTION_RE.search(description_text):
        return None, "Master's mentioned in description", [], None

    signals = []
    bonus = 0
    for label, pattern, points in POSITIVE_LEVEL_PATTERNS:
        if pattern.search(text):
            signals.append(label)
            bonus = max(bonus, points)

    max_experience = profile.get("max_required_experience", 1)
    requirements = list(iter_experience_requirements(description_text))

    over_limit = [minimum for minimum, _ in requirements if minimum > max_experience]
    if over_limit:
        required = max(over_limit)
        return None, f"{required}+ years required", signals, required

    years_required = max((minimum for minimum, _ in requirements), default=None)

    if years_required == 0:
        bonus += 10
        signals.append("0 years required")
    elif years_required == 1:
        bonus += 6
        signals.append("1 year requirement")

    return bonus, None, signals, years_required


def load_employers(con):
    try:
        rows = con.execute(
            "SELECT employer_norm, employer_display, certified, denied, withdrawn, "
            "titles, states, lvl1, lvl2, lvl3, lvl4 FROM employers"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Could not read employers table: {exc}")

    output = {}
    for row in rows:
        levels = [row[7] or 0, row[8] or 0, row[9] or 0, row[10] or 0]
        level_total = sum(levels)
        case_total = (row[2] or 0) + (row[3] or 0) + (row[4] or 0)
        try:
            titles = json.loads(row[5] or "[]")
        except json.JSONDecodeError:
            titles = []

        output[row[0]] = {
            "display": row[1],
            "certified": row[2] or 0,
            "titles": [str(x).lower() for x in titles],
            "senior_share": ((levels[2] + levels[3]) / level_total) if level_total else None,
            "trouble_rate": (((row[3] or 0) + (row[4] or 0)) / case_total) if case_total else None,
        }
    return output


def sponsorship_score(emp, title, blob, fuzzy_confidence=100):
    score = 0
    signals = []

    if emp and emp["certified"] > 0:
        score += 28
        signals.append(f"{emp['certified']} certified LCAs")

        if fuzzy_confidence < 100:
            signals.append(f"fuzzy match {fuzzy_confidence} to '{emp['display']}'")

        if emp["certified"] >= 100:
            score += 12
        elif emp["certified"] >= 25:
            score += 8
        elif emp["certified"] >= 5:
            score += 4

        if emp["senior_share"] is not None and emp["senior_share"] >= 0.5:
            score += 5

        words = [w for w in re.findall(r"[a-z]+", (title or "").lower()) if len(w) >= 5]
        if words and any(any(word in old for word in words) for old in emp["titles"]):
            score += 10
            signals.append("historical LCA title overlap")

        if emp["trouble_rate"] is not None and emp["trouble_rate"] >= 0.25:
            score -= 8
            signals.append("higher denied/withdrawn share")

    if any(p.search(blob) for p in SPONSOR_POS_RE):
        score += 12
        signals.append("posting mentions sponsorship")

    return score, signals


def priority_label(score, resume_fit):
    if score >= 155 and resume_fit >= 78:
        return "P1 - Apply ASAP"
    if score >= 135 and resume_fit >= 68:
        return "P2 - Strong Apply"
    if score >= 115:
        return "P3 - Apply"
    return "P4 - Review"


# --------------------------------------------------------------- state I/O

def load_previous_state(path):
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("active_job_keys", []))
    except (json.JSONDecodeError, OSError):
        return set()


def save_state(path, keys):
    payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "active_job_keys": sorted(keys),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_posted_datetime(value):
    """Return an aware UTC datetime, or None when the ATS supplied no usable date."""
    if value is None:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    # Unix timestamps in seconds or milliseconds.
    if re.fullmatch(r"\d{10,13}", raw):
        stamp = int(raw)
        if len(raw) == 13:
            stamp /= 1000
        try:
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    # Date-only values are handled before the ISO parser, which would read
    # them as midnight. Greenhouse and Ashby both supply `updated_at` truncated
    # to 10 characters, so treating those as midnight backdates the posting by
    # up to a day and drops it from the window early. There is no posting time
    # in the data, so assume the end of that UTC day.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.replace(hour=23, minute=59, second=59, tzinfo=timezone.utc)

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


CSV_FIELDS = [
    "application_priority", "combined_score", "resume_fit_score",
    "career_level_score", "company_tier", "company_fit_points",
    "sponsorship_score", "is_new_since_last_run", "role_family",
    "matched_resume_skills", "experience_years_detected", "company",
    "title", "location", "posted", "source", "why_ranked", "url",
    "lca_certified", "job_key",
]


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--profile", help="Path to a candidate profile JSON file")
    parser.add_argument("--db", default=None,
                        help="SQLite database (default: $SPONSORSCAN_DB or sponsorscan.db)")
    parser.add_argument("--out", default=None, help="All currently matching jobs")
    parser.add_argument("--new-out", default=None,
                        help="Only jobs new since the previous run")
    parser.add_argument("--state", default=None,
                        help="Snapshot file used to identify new jobs")
    parser.add_argument("--top", type=int, default=50, help="How many top jobs to print")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Minimum combined score to keep")
    parser.add_argument("--fuzzy-cutoff", type=int, default=90,
                        help="Fuzzy employer-name match threshold (0 disables)")
    parser.add_argument("--include-non-us", action="store_true")
    parser.add_argument("--include-internships", action="store_true",
                        help="Internships are included by default only if the "
                             "title matches a target role")
    parser.add_argument("--reset-state", action="store_true",
                        help="Treat all current matches as new")
    parser.add_argument("--hours", type=float, default=None,
                        help="Keep only jobs posted within this many hours")
    parser.add_argument("--include-unknown-posted", action="store_true",
                        help="Also keep jobs whose ATS provides no usable posting date")
    return parser


def main():
    args = build_parser().parse_args()

    profile = None
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            raise SystemExit(f"Invalid profile: {exc}")
        print(f"Loaded profile: {describe_profile(profile)}")
        for warning in authorization_warnings(profile):
            print(f"  warning: {warning}")

    output_config = (profile or {}).get("output_files", {})
    args.out = args.out or output_config.get("all_matches") or "matches_48h.csv"
    args.new_out = args.new_out or output_config.get("new_matches") or "new_jobs_48h.csv"
    args.state = args.state or output_config.get("state") or str(DEFAULT_STATE)
    if args.min_score is None:
        args.min_score = (profile or {}).get("minimum_score", 95)
    if args.hours is None:
        args.hours = (profile or {}).get("report_hours", 48)

    if args.hours <= 0:
        raise SystemExit("--hours must be greater than 0.")

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        raise SystemExit(f"Could not find {db_path}. Run `sponsorscan.py fetch-jobs` first.")

    target_roles = build_target_roles(profile)
    resume_skills = build_resume_skills(profile)
    tiers = build_company_tiers(profile)
    preferred_companies = (profile or {}).get("preferred_companies") or []
    preferred_locations = (profile or {}).get("preferred_locations") or []

    state_path = Path(args.state)
    previous_keys = set() if args.reset_state else load_previous_state(state_path)

    now_utc = datetime.now(timezone.utc)
    posted_cutoff = now_utc - timedelta(hours=args.hours)

    con = sqlite3.connect(db_path)
    try:
        employers = load_employers(con)
        try:
            jobs = con.execute(
                "SELECT company, company_norm, title, location, url, posted, "
                "description, source FROM jobs"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise SystemExit(f"Could not read jobs table: {exc}")
    finally:
        con.close()

    employer_keys = list(employers.keys())
    fuzzy_cutoff = args.fuzzy_cutoff if HAVE_RAPIDFUZZ else 0

    results = []
    counts = {
        "disqualifier": 0, "role": 0, "career": 0,
        "phd_title": 0, "level_title": 0, "experience_over_limit": 0,
        "phd_required": 0, "masters_required": 0,
        "location": 0, "score": 0,
        "posted_too_old": 0, "posted_unknown": 0,
    }

    for company, company_norm, title, location, url, posted, description, source in jobs:
        parsed_posted = parse_posted_datetime(posted)
        if parsed_posted is None:
            if not args.include_unknown_posted:
                counts["posted_unknown"] += 1
                continue
        elif parsed_posted < posted_cutoff:
            counts["posted_too_old"] += 1
            continue

        blob = f"{title or ''}\n{description or ''}"

        if violates_work_authorization(blob, profile):
            counts["disqualifier"] += 1
            continue

        role_family, role_base = classify_role(title, target_roles)
        if not role_family:
            counts["role"] += 1
            continue

        level_score, exclusion_reason, level_signals, years_required = \
            career_level_score(title, description, profile)
        if level_score is None:
            counts["career"] += 1
            if exclusion_reason == "PhD/doctoral/postdoc title":
                counts["phd_title"] += 1
            elif exclusion_reason == "level II or above title":
                counts["level_title"] += 1
            elif exclusion_reason and exclusion_reason.endswith("+ years required"):
                counts["experience_over_limit"] += 1
            elif exclusion_reason and exclusion_reason.startswith("PhD"):
                counts["phd_required"] += 1
            elif exclusion_reason and exclusion_reason.startswith("Master's"):
                counts["masters_required"] += 1
            continue

        if not args.include_non_us and not is_us_location(location):
            counts["location"] += 1
            continue

        skills, skill_score = score_skills(title, description, resume_skills)
        resume_fit = min(100, role_base + skill_score)

        tier = company_tier(company, tiers)
        company_points = COMPANY_TIER_POINTS.get(tier, DEFAULT_TIER_POINTS)

        bonus_signals = []
        if matches_preferred_company(company, preferred_companies):
            company_points += PREFERRED_COMPANY_POINTS
            bonus_signals.append("preferred company")
        if matches_preferred_location(location, preferred_locations):
            company_points += PREFERRED_LOCATION_POINTS
            bonus_signals.append("preferred location")

        # Exact employer key first, then fuzzy. DOL records legal names while
        # boards show brands, so Instacart's postings sit under Maplebear Inc.
        matched_key, confidence = match_employer(
            company_norm, employers, employer_keys, fuzzy_cutoff)
        emp = employers.get(matched_key) if matched_key else None
        sponsor_points, sponsor_signals = sponsorship_score(
            emp, title, blob, confidence)

        combined_score = resume_fit + level_score + company_points + sponsor_points
        if combined_score < args.min_score:
            counts["score"] += 1
            continue

        key = stable_job_key(company, title, location, url)

        reasons = [f"{resume_fit}/100 resume fit", role_family, f"company tier {tier}/5"]
        reasons.extend(bonus_signals)
        reasons.extend(level_signals)
        reasons.extend(sponsor_signals)

        results.append({
            "application_priority": "",  # filled in once the score is final
            "combined_score": combined_score,
            "resume_fit_score": resume_fit,
            "career_level_score": level_score,
            "company_tier": tier,
            "company_fit_points": company_points,
            "sponsorship_score": sponsor_points,
            "is_new_since_last_run": "YES" if key not in previous_keys else "NO",
            "role_family": role_family,
            "matched_resume_skills": ", ".join(skills),
            "experience_years_detected": years_required if years_required is not None else "",
            "company": company,
            "title": title,
            "location": location,
            "posted": posted,
            "source": source,
            "why_ranked": "; ".join(reasons),
            "url": url,
            "lca_certified": emp["certified"] if emp else 0,
            "job_key": key,
            "_posted_ts": parsed_posted.timestamp() if parsed_posted else 0.0,
        })

    results.sort(key=lambda r: (
        -r["_posted_ts"], -r["combined_score"], -r["resume_fit_score"],
        -r["company_tier"], r["company"].lower(),
    ))

    for row in results:
        row["application_priority"] = priority_label(
            row["combined_score"], row["resume_fit_score"])
        row.pop("_posted_ts", None)

    new_results = [row for row in results if row["is_new_since_last_run"] == "YES"]

    write_csv(Path(args.out), results, CSV_FIELDS)
    write_csv(Path(args.new_out), new_results, CSV_FIELDS)
    save_state(state_path, {row["job_key"] for row in results})

    print(f"Wrote {len(results):,} jobs posted within the last {args.hours:g} hours "
          f"to {args.out}")
    print(f"Wrote {len(new_results):,} jobs new since the previous run to {args.new_out}")
    print(f"Posting cutoff (UTC): {posted_cutoff.isoformat()}")
    if not previous_keys:
        print("No previous snapshot was found, so all current matches count as new.")
    print()
    print(f"{counts['posted_too_old']:,} dropped as older than {args.hours:g} hours")
    print(f"{counts['posted_unknown']:,} dropped for having no usable posting time")
    print(f"{counts['disqualifier']:,} dropped on work-authorization, citizenship "
          f"or clearance restrictions")
    print(f"{counts['career']:,} dropped on career level")
    print(f"  - {counts['phd_title']:,} PhD/doctoral/postdoc in the title")
    print(f"  - {counts['level_title']:,} Level II/III/IV/V titles")
    print(f"  - {counts['experience_over_limit']:,} over the configured experience limit")
    print(f"  - {counts['phd_required']:,} required a PhD")
    print(f"  - {counts['masters_required']:,} required a master's degree")
    print(f"{counts['role']:,} dropped outside the target role list")
    print(f"{counts['location']:,} dropped as non-US or unrecognized")
    print(f"{counts['score']:,} dropped below combined score {args.min_score}")
    if not HAVE_RAPIDFUZZ:
        print("rapidfuzz is not installed, so employer matching was exact-only.")
    print()

    display_rows = new_results if new_results else results
    heading = "TOP NEW JOBS" if new_results else "TOP CURRENT JOBS"
    print(heading)
    print("=" * len(heading))

    for row in display_rows[:args.top]:
        print(f"[{row['combined_score']:>3}] {row['application_priority']} | "
              f"{row['company']} - {row['title']}")
        print(f"      {row['location'] or '?'} | {row['role_family']} | "
              f"Resume {row['resume_fit_score']}/100 | Company {row['company_tier']}/5")
        print(f"      Skills: {row['matched_resume_skills'] or 'title match only'}")
        print(f"      {row['url']}\n")


def authorization_warnings(profile):
    """Flag profiles whose work_authorization contradicts their reject_* flags.

    `work_authorization` is a label; the reject_* booleans are what actually
    filter. A profile set to "us_citizen" that still rejects citizen-only
    postings is unlikely to mean it, and the mismatch is reported rather than
    resolved automatically so that the profile stays the single source of truth.
    """
    auth = profile.get("work_authorization")
    warnings = []

    if auth in ("us_citizen", "permanent_resident"):
        if profile.get("reject_citizenship_required", True):
            warnings.append(
                f"work_authorization is '{auth}' but reject_citizenship_required "
                "is true, so citizen-only postings will still be dropped.")
        if profile.get("reject_permanent_authorization_required", True):
            warnings.append(
                f"work_authorization is '{auth}' but "
                "reject_permanent_authorization_required is true, so postings "
                "requiring permanent authorization will still be dropped.")
    elif auth in ("opt", "stem_opt"):
        if not profile.get("reject_citizenship_required", True):
            warnings.append(
                f"work_authorization is '{auth}' but reject_citizenship_required "
                "is false, so citizen-only postings will be kept.")

    return warnings


if __name__ == "__main__":
    main()
