#!/usr/bin/env python3
"""
SponsorScan Personalized Daily Report — 48H + OPT-AWARE FILTER

Features
--------
V2: Experience/education-aware filtering and scoring
V3: Resume-weighted skill matching
V4: Company-fit ranking
V5: Application priority and explanation columns
V6: Daily "new since last run" report and history tracking
V7: Keeps only jobs posted within the requested number of hours

Place this file beside sponsorscan.db, then run after fetch-jobs:

    python sponsorscan.py fetch-jobs --replace
    python sponsor_daily_report.py --out pranav_matches.csv --new-out todays_new_jobs.csv --top 50

First run:
    All matching jobs are treated as new because no prior snapshot exists.

Later runs:
    todays_new_jobs.csv contains only jobs that were not present in the previous run.
"""

import argparse
import csv
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from profile_loader import ProfileError, load_profile, describe_profile

DB_PATH = Path("sponsorscan.db")
DEFAULT_STATE = Path(".sponsorscan_pranav_state.json")

# ---------------------------- Role fit ---------------------------------

TARGET_ROLES = [
    ("Machine Learning Engineer",
     [r"\bmachine learning engineer\b", r"\bml engineer\b", r"\bml platform engineer\b"], 64),
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
     [r"\bml researcher\b", r"\bmachine learning researcher\b", r"\bai researcher\b"], 59),
    ("Research Scientist",
     [r"\bresearch scientist\b", r"\bai scientist\b"], 58),
    ("Data Scientist",
     [r"\bdata scientist\b", r"\bmachine learning scientist\b"], 57),
    ("Data Engineer",
     [r"\bdata engineer\b", r"\banalytics engineer\b"], 52),
    ("Backend Engineer",
     [r"\bback[ -]?end (?:software )?engineer\b", r"\bbackend developer\b"], 50),
    ("Full Stack Engineer",
     [r"\bfull[ -]?stack (?:software )?engineer\b", r"\bfull[ -]?stack developer\b"], 50),
    ("Python Engineer",
     [r"\bpython engineer\b", r"\bpython developer\b"], 49),
    ("Software Engineer",
     [r"\bsoftware engineer\b", r"\bsoftware developer\b",
      r"\bsoftware development engineer\b", r"\bsde\b"], 47),
]
TARGET_ROLES = [
    (name, [re.compile(p, re.I) for p in pats], score)
    for name, pats, score in TARGET_ROLES
]


ROLE_ALIASES = {
    "Machine Learning Engineer": [
        r"\bmachine learning engineer\b",
        r"\bml engineer\b",
        r"\bml platform engineer\b",
    ],
    "AI Engineer": [
        r"\bai[ /-]?ml engineer\b",
        r"\bai engineer\b",
        r"\bartificial intelligence engineer\b",
        r"\bgenerative ai engineer\b",
        r"\bgenai engineer\b",
    ],
    "AI/ML Engineer": [
        r"\bai[ /-]?ml engineer\b",
        r"\bai engineer\b",
        r"\bartificial intelligence engineer\b",
        r"\bgenerative ai engineer\b",
        r"\bgenai engineer\b",
    ],
    "Research Engineer": [
        r"\bresearch engineer\b",
        r"\bmachine learning research engineer\b",
        r"\bai research engineer\b",
    ],
    "Applied Scientist": [
        r"\bapplied scientist\b",
        r"\bapplied machine learning scientist\b",
    ],
    "Computer Vision Engineer": [
        r"\bcomputer vision engineer\b",
        r"\bvision engineer\b",
    ],
    "ML Researcher": [
        r"\bml researcher\b",
        r"\bmachine learning researcher\b",
        r"\bai researcher\b",
    ],
    "Research Scientist": [
        r"\bresearch scientist\b",
        r"\bai scientist\b",
    ],
    "Data Scientist": [
        r"\bdata scientist\b",
        r"\bmachine learning scientist\b",
    ],
    "Data Engineer": [
        r"\bdata engineer\b",
        r"\banalytics engineer\b",
    ],
    "Backend Engineer": [
        r"\bback[ -]?end (?:software )?engineer\b",
        r"\bbackend developer\b",
    ],
    "Full Stack Engineer": [
        r"\bfull[ -]?stack (?:software )?engineer\b",
        r"\bfull[ -]?stack developer\b",
    ],
    "Python Engineer": [
        r"\bpython engineer\b",
        r"\bpython developer\b",
    ],
    "Software Engineer": [
        r"\bsoftware engineer\b",
        r"\bsoftware developer\b",
        r"\bsoftware development engineer\b",
        r"\bsde\b",
    ],
}

DEFAULT_ROLE_BASE = {
    "Machine Learning Engineer": 64,
    "AI Engineer": 64,
    "AI/ML Engineer": 64,
    "Research Engineer": 62,
    "Applied Scientist": 61,
    "Computer Vision Engineer": 60,
    "ML Researcher": 59,
    "Research Scientist": 58,
    "Data Scientist": 57,
    "Data Engineer": 52,
    "Backend Engineer": 50,
    "Full Stack Engineer": 50,
    "Python Engineer": 49,
    "Software Engineer": 47,
}


def build_target_roles(profile):
    roles = profile.get("target_roles") or []
    if not roles:
        return TARGET_ROLES

    compiled = []
    for role in roles:
        patterns = ROLE_ALIASES.get(role)
        if patterns is None:
            patterns = [rf"\b{re.escape(role)}\b"]
        compiled.append(
            (
                role,
                [re.compile(pattern, re.I) for pattern in patterns],
                DEFAULT_ROLE_BASE.get(role, 47),
            )
        )
    return compiled

# ---------------------------- Resume fit -------------------------------

# Higher weights reflect the user's strongest demonstrated experience.
RESUME_SKILLS = [
    ("Python", [r"\bpython\b"], 7),
    ("Machine Learning", [r"\bmachine learning\b", r"\bml\b"], 7),
    ("Deep Learning", [r"\bdeep learning\b", r"\bneural network"], 6),
    ("TensorFlow/Keras", [r"\btensorflow\b", r"\bkeras\b"], 6),
    ("PyTorch", [r"\bpytorch\b"], 6),
    ("Research/Publications", [r"\bresearch\b", r"\bpublication\b", r"\bieee\b",
                                r"\bscientific\b"], 6),
    ("NLP/LLMs", [r"\bnlp\b", r"\bllm", r"\blarge language model"], 5),
    ("RAG/LangChain", [r"\brag\b", r"\bretrieval.?augmented\b", r"\blangchain\b"], 5),
    ("Time Series/Forecasting", [r"\btime.?series\b", r"\bforecast"], 5),
    ("SQL", [r"\bsql\b", r"\bmysql\b", r"\bpostgres(?:ql)?\b", r"\bsqlite\b"], 4),
    ("Scikit-learn", [r"\bscikit.?learn\b", r"\bsklearn\b"], 4),
    ("Pandas/NumPy", [r"\bpandas\b", r"\bnumpy\b"], 4),
    ("Computer Vision", [r"\bcomputer vision\b", r"\bimage processing\b"], 4),
    ("APIs/Cloud", [r"\bapi\b", r"\baws\b", r"\bazure\b", r"\bgcp\b",
                     r"\bcloud\b"], 3),
    ("React/Node", [r"\breact\b", r"\bnode\.?js\b"], 3),
    ("Flask/Streamlit", [r"\bflask\b", r"\bstreamlit\b"], 3),
    ("C++", [r"\bc\+\+\b"], 2),
    ("Java", [r"\bjava\b"], 2),
    ("JavaScript", [r"\bjavascript\b", r"\btypescript\b"], 2),
]
RESUME_SKILLS = [
    (name, [re.compile(p, re.I) for p in pats], score)
    for name, pats, score in RESUME_SKILLS
]


SKILL_ALIASES = {
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
    "PyTorch": [r"\bpytorch\b"],
    "Scikit-learn": [r"\bscikit.?learn\b", r"\bsklearn\b"],
    "Pandas": [r"\bpandas\b"],
    "NumPy": [r"\bnumpy\b"],
    "NLP": [r"\bnlp\b", r"\bnatural language processing\b"],
    "LLMs": [r"\bllm", r"\blarge language model"],
    "RAG": [r"\brag\b", r"\bretrieval.?augmented\b"],
    "LangChain": [r"\blangchain\b"],
    "Computer Vision": [r"\bcomputer vision\b", r"\bimage processing\b"],
    "Flask": [r"\bflask\b"],
    "FastAPI": [r"\bfastapi\b"],
    "Streamlit": [r"\bstreamlit\b"],
}


def build_resume_skills(profile):
    skills = profile.get("skills") or {}
    if not skills:
        return RESUME_SKILLS

    compiled = []
    for skill, weight in skills.items():
        patterns = SKILL_ALIASES.get(skill, [rf"\b{re.escape(skill)}\b"])
        compiled.append(
            (
                skill,
                [re.compile(pattern, re.I) for pattern in patterns],
                int(weight),
            )
        )
    return compiled

# -------------------------- Career-level fit ---------------------------

POSITIVE_LEVEL_PATTERNS = [
    ("new grad", re.compile(r"\bnew ?grad(?:uate)?\b", re.I), 18),
    ("university graduate", re.compile(r"\b(?:university|college) grad(?:uate)?\b", re.I), 16),
    ("early career", re.compile(r"\bearly career\b", re.I), 14),
    ("entry level", re.compile(r"\bentry[ -]?level\b", re.I), 14),
    ("0–2 years", re.compile(r"\b0\s*(?:-|–|to)\s*2\s*(?:years?|yrs?)\b", re.I), 14),
    ("0–1 years", re.compile(r"\b0\s*(?:-|–|to)\s*1\s*(?:years?|yrs?)\b", re.I), 14),
    ("1–2 years", re.compile(r"\b1\s*(?:-|–|to)\s*2\s*(?:years?|yrs?)\b", re.I), 11),
    ("1–3 years", re.compile(r"\b1\s*(?:-|–|to)\s*3\s*(?:years?|yrs?)\b", re.I), 8),
    ("associate", re.compile(r"\bassociate\b", re.I), 7),
    ("level I", re.compile(r"\b(?:engineer|scientist|developer)\s+i\b", re.I), 8),
    ("internship", re.compile(r"\bintern(?:ship)?\b", re.I), 8),
    ("bachelor's", re.compile(r"\bbachelor'?s?(?: degree)?\b", re.I), 5),
]

SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|distinguished|lead|architect|manager|"
    r"director|head of|vp|vice president|chief|fellow)\b", re.I
)

PHD_TITLE = re.compile(
    r"\b(?:ph\.?\s*d\.?|doctoral|doctorate|post[ -]?doc(?:toral)?)\b",
    re.I,
)

LEVEL_II_PLUS_TITLE = re.compile(
    r"(?:"
    # Roman numerals
    r"\b(?:engineer|scientist|developer|analyst|researcher|software engineer|machine learning engineer|data scientist)\s*(?:,|-)?\s*(?:ii|iii|iv|v)\b"
    # Numeric levels (Engineer 2, Software Engineer 3, etc.)
    r"|\b(?:engineer|scientist|developer|analyst|researcher|software engineer|machine learning engineer|data scientist)\s*(?:,|-)?\s*(?:2|3|4|5)\b"
    # Generic level labels
    r"|\b(?:level|lvl)\s*(?:2|3|4|5|ii|iii|iv|v)\b"
    # SWE/SDE abbreviations
    r"|\b(?:swe|sde|mle)\s*(?:2|3|4|5|ii|iii|iv|v)\b"
    r")",
    re.I,
)

HARD_EXCLUDE_PATTERNS = [
    (
        "PhD required in description",
        re.compile(
            r"(?:"
            r"\b(?:must|requires?|required|need(?:ed)?|minimum qualification(?:s)?|basic qualification(?:s)?)\b"
            r"[^.\n]{0,120}\b(?:ph\.?\s*d\.?|doctorate|doctoral degree)\b"
            r"|\b(?:ph\.?\s*d\.?|doctorate|doctoral degree)\b"
            r"[^.\n]{0,80}\b(?:required|must have|is required|minimum)\b"
            r"|\byou (?:hold|have)\b[^.\n]{0,40}\b(?:a\s+)?(?:ph\.?\s*d\.?|doctorate|doctoral degree)\b"
            r")",
            re.I,
        ),
    ),
    (
        "Master's required in description",
        re.compile(
            r"(?:"
            r"\b(?:must|requires?|required|need(?:ed)?|minimum qualification(?:s)?|basic qualification(?:s)?)\b"
            r"[^.\n]{0,120}\b(?:master'?s degree|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree)\b"
            r"|\b(?:master'?s degree|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree)\b"
            r"[^.\n]{0,80}\b(?:required|must have|is required|minimum)\b"
            r"|\byou (?:hold|have)\b[^.\n]{0,40}\b(?:a\s+)?(?:master'?s degree|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree)\b"
            r")",
            re.I,
        ),
    ),
    (
        "postdoctoral/faculty role",
        re.compile(r"\b(?:postdoctoral|post-doc|postdoc|faculty|professor)\b", re.I),
    ),
]

# Detect requirements such as:
#   "10+ years of overall experience"
#   "2+ years of production experience developing..."
#   "6+ years of deep experience architecting..."
#   "3 years' software engineering experience"
#   "1-2 years of relevant professional experience"
#
# Up to six descriptive words are allowed before "experience", which catches
# common wording without treating unrelated year numbers as experience rules.
EXPERIENCE_REQUIREMENT_RE = re.compile(
    r"\b(?P<min>\d{1,2})\s*"
    r"(?:\+|(?:-|–|—|to)\s*(?P<max>\d{1,2}))?\s*"
    r"(?:years?|yrs?)\b",
    re.I,
)

# -------------------------- Sponsorship fit ----------------------------

# OPT-aware hard exclusions.
#
# A generic statement such as "no visa sponsorship available" is NOT enough by
# itself to remove a job, because an OPT/STEM OPT candidate may already have
# temporary employment authorization. We only reject language that clearly
# blocks OPT candidates, requires permanent/unrestricted authorization, limits
# the role to citizens/permanent residents, or requires clearance/export status.
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
    r"\b(?:now or in the future|current or future)\b[^.\n]{0,80}\b(?:are not eligible|will not be considered)\b",

    # Explicitly excludes OPT/CPT or temporary employment authorization.
    r"\b(?:opt|stem opt|cpt)\b[^.\n]{0,80}\b(?:not accepted|not eligible|not supported|will not be considered)\b",
    r"\b(?:not accepting|cannot employ|unable to employ)\b[^.\n]{0,80}\b(?:opt|stem opt|cpt)\b",

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

# ---------------------------- Company fit ------------------------------

COMPANY_TIERS = {
    5: {
        "openai", "anthropic", "waymo", "databricks", "snowflake", "nvidia",
        "scale ai", "figma", "roblox"
    },
    4: {
        "pinterest", "reddit", "airbnb", "twilio", "robinhood", "samsara",
        "zoox", "palantir", "stripe", "coreweave", "datadog", "mongodb",
        "jane street", "moloco"
    },
    3: {
        "spotify", "toast", "roku", "gen digital", "nuro", "doordash",
        "benchling", "netflix"
    },
}
COMPANY_TIER_POINTS = {5: 16, 4: 11, 3: 6}

# ---------------------------- US location ------------------------------

US_STATE_CODES = {
    "al","ak","az","ar","ca","co","ct","de","fl","ga","hi","id","il","in",
    "ia","ks","ky","la","me","md","ma","mi","mn","ms","mo","mt","ne","nv",
    "nh","nj","nm","ny","nc","nd","oh","ok","or","pa","ri","sc","sd","tn",
    "tx","ut","vt","va","wa","wv","wi","wy","dc",
}
US_STATE_NAMES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    "district of columbia",
}
NON_US = {
    "canada","india","united kingdom","ireland","germany","france","spain","italy",
    "netherlands","belgium","sweden","norway","denmark","finland","poland",
    "romania","portugal","switzerland","austria","australia","new zealand",
    "singapore","japan","china","taiwan","south korea","korea","israel","brazil",
    "mexico","argentina","colombia","chile","philippines","indonesia","malaysia",
    "thailand","vietnam","hong kong","uae","dubai","berlin","toronto","vancouver",
    "london","dublin","paris","amsterdam","munich",
}


def normalize_company(name):
    value = (name or "").lower()
    value = re.sub(r"\b(incorporated|corporation|company|limited|llc|inc|corp|ltd)\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url):
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        # Remove tracking query strings but preserve job identifiers embedded in path.
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))
    except Exception:
        return url.strip()


def stable_job_key(company, title, location, url):
    normalized_url = normalize_url(url)
    if normalized_url:
        return normalized_url
    text = "|".join([
        normalize_company(company),
        re.sub(r"\s+", " ", (title or "").lower()).strip(),
        re.sub(r"\s+", " ", (location or "").lower()).strip(),
    ])
    return text


def classify_role(title, target_roles=None):
    active_roles = target_roles or TARGET_ROLES
    for family, patterns, base in active_roles:
        if any(p.search(title or "") for p in patterns):
            return family, base
    return None, 0


def score_skills(title, description, resume_skills=None):
    text = f"{title or ''}\n{description or ''}"
    active_skills = resume_skills or RESUME_SKILLS
    matched, points = [], 0
    for name, patterns, value in active_skills:
        if any(p.search(text) for p in patterns):
            matched.append(name)
            points += value
    return matched, min(points, 42)


def career_level_score(title, description, profile=None):
    title_text = title or ""
    description_text = description or ""
    text = f"{title_text}\n{description_text}"
    profile = profile or {}

    reject_senior = profile.get("reject_senior_titles", True)
    reject_level_ii = profile.get("reject_level_ii_plus_titles", True)
    reject_masters = profile.get("reject_masters_mentions", False)
    reject_phd = profile.get("reject_phd_mentions", False)
    max_experience = profile.get("max_required_experience", 1)

    if PHD_TITLE.search(title_text):
        return None, "PhD/doctoral/postdoc title", [], None

    if reject_level_ii and LEVEL_II_PLUS_TITLE.search(title_text):
        return None, "level II or above title", [], None

    if reject_senior and SENIOR_TITLE.search(title_text):
        return None, "senior-level title", [], None

    if re.search(r"\b(?:postdoctoral|post-doc|postdoc|faculty|professor)\b", text, re.I):
        return None, "postdoctoral/faculty role", [], None

    if reject_phd and re.search(
        r"\b(?:ph\.?\s*d\.?|doctorate|doctoral(?: degree)?)\b",
        description_text,
        re.I,
    ):
        return None, "PhD mentioned in description", [], None

    if reject_masters and re.search(
        r"\b(?:master'?s(?: degree)?|m\.?\s*s\.?|m\.?\s*sc\.?|graduate degree)\b",
        description_text,
        re.I,
    ):
        return None, "Master's mentioned in description", [], None

    signals = []
    bonus = 0
    for label, pattern, points in POSITIVE_LEVEL_PATTERNS:
        if pattern.search(text):
            signals.append(label)
            bonus = max(bonus, points)

    requirements = []
    for match in EXPERIENCE_REQUIREMENT_RE.finditer(description_text):
        minimum = int(match.group("min"))
        maximum = int(match.group("max")) if match.group("max") else None
        requirements.append((minimum, maximum))

    disqualifying = [
        minimum
        for minimum, _ in requirements
        if minimum > max_experience
    ]
    if disqualifying:
        required = max(disqualifying)
        return (
            None,
            f"{required}+ years required",
            signals,
            required,
        )

    years_required = max(
        (minimum for minimum, _ in requirements),
        default=None,
    )

    if years_required == 0:
        bonus += 10
        signals.append("0 years required")
    elif years_required == 1:
        bonus += 6
        signals.append("1 year requirement")

    return bonus, None, signals, years_required


def company_tier(company):
    normalized = normalize_company(company)
    for tier, names in COMPANY_TIERS.items():
        if any(name in normalized or normalized in name for name in names):
            return tier
    return 2


def is_us_location(location):
    loc = (location or "").strip().lower()
    if not loc:
        return False
    if any(term in loc for term in NON_US):
        return False
    if any(term in loc for term in (
        "united states", "usa", "u.s.", "remote - us", "remote, us",
        "remote us", "us remote", "remote (us)", "north america"
    )):
        return True
    if any(state in loc for state in US_STATE_NAMES):
        return True
    tokens = set(re.findall(r"\b[a-z]{2}\b", loc))
    return bool(tokens & US_STATE_CODES)


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


def violates_work_authorization(blob, profile):
    """
    Return True when the posting conflicts with the profile's authorization rules.
    """
    if not profile:
        return any(pattern.search(blob) for pattern in DISQ_RE)

    checks = []

    if profile.get("reject_opt_excluded", True):
        checks.extend([
            re.compile(
                r"\b(?:opt|stem opt|cpt)\b[^.\n]{0,100}"
                r"\b(?:not accepted|not eligible|not supported|will not be considered|cannot be hired)\b",
                re.I,
            ),
            re.compile(
                r"\b(?:not accepting|cannot employ|unable to employ)\b"
                r"[^.\n]{0,100}\b(?:opt|stem opt|cpt)\b",
                re.I,
            ),
        ])

    if profile.get("reject_permanent_authorization_required", True):
        checks.extend([
            re.compile(
                r"\b(?:permanent|unrestricted)\s+"
                r"(?:u\.?\s?s\.?\s+)?work authorization\b",
                re.I,
            ),
            re.compile(
                r"\bmust be\b[^.\n]{0,100}"
                r"\b(?:permanent resident|green card holder)\b",
                re.I,
            ),
        ])

    if profile.get("reject_citizenship_required", True):
        checks.extend([
            re.compile(
                r"\bmust be (?:a |an )?"
                r"(?:u\.?\s?s\.?|united states)\s?"
                r"(?:citizen|person|national)\b",
                re.I,
            ),
            re.compile(
                r"\b(?:u\.?\s?s\.?|united states)\s?"
                r"citizenship (?:is )?required\b",
                re.I,
            ),
            re.compile(
                r"\b(?:u\.?\s?s\.?|united states)\s?"
                r"(?:citizens?|persons?)\s+only\b",
                re.I,
            ),
        ])

    if profile.get("reject_clearance_roles", True):
        checks.extend([
            re.compile(r"\bsecurity clearance\b", re.I),
            re.compile(r"\bitar\b", re.I),
            re.compile(r"\bexport control(?:led|s)?\b", re.I),
        ])

    return any(pattern.search(blob) for pattern in checks)


def sponsorship_score(emp, title, blob):
    score = 0
    signals = []

    if emp and emp["certified"] > 0:
        score += 28
        signals.append(f"{emp['certified']} certified LCAs")

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


def priority_label(score, resume_fit, career_score):
    if score >= 155 and resume_fit >= 78 and career_score >= 0:
        return "P1 — Apply ASAP"
    if score >= 135 and resume_fit >= 68:
        return "P2 — Strong Apply"
    if score >= 115:
        return "P3 — Apply"
    return "P4 — Review"


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

    normalized = raw.replace("Z", "+00:00")

    # Common ISO-like formats.
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(raw, fmt)
            if fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                # A date-only ATS value has no exact posting time. Use the end of
                # that UTC day so a newly posted job is not prematurely discarded.
                parsed = parsed.replace(hour=23, minute=59, second=59)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    return None


def is_recent_post(posted, cutoff):
    parsed = parse_posted_datetime(posted)
    return parsed is not None and parsed >= cutoff


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile",
                        help="Path to a candidate profile JSON file")
    parser.add_argument("--out", default=None,
                        help="All currently matching jobs")
    parser.add_argument("--new-out", default=None,
                        help="Only jobs new since the previous run")
    parser.add_argument("--state", default=None,
                        help="Snapshot file used to identify new jobs")
    parser.add_argument("--top", type=int, default=50,
                        help="How many top jobs to print")
    parser.add_argument("--min-score", type=int, default=None,
                        help="Minimum combined score to keep")
    parser.add_argument("--include-non-us", action="store_true")
    parser.add_argument("--include-internships", action="store_true",
                        help="Internships are included by default only if title matches target roles")
    parser.add_argument("--reset-state", action="store_true",
                        help="Treat all current matches as new")
    parser.add_argument("--hours", type=float, default=None,
                        help="Keep only jobs posted within this many hours")
    parser.add_argument("--include-unknown-posted", action="store_true",
                        help="Also keep jobs whose ATS provides no usable posting date")
    args = parser.parse_args()

    profile = None
    if args.profile:
        try:
            profile = load_profile(args.profile)
        except ProfileError as exc:
            raise SystemExit(f"Invalid profile: {exc}")
        print(f"Loaded profile: {describe_profile(profile)}")

    output_config = (profile or {}).get("output_files", {})
    args.out = args.out or output_config.get("all_matches") or "matches_48h.csv"
    args.new_out = (
        args.new_out
        or output_config.get("new_matches")
        or "new_jobs_48h.csv"
    )
    args.state = (
        args.state
        or output_config.get("state")
        or str(DEFAULT_STATE)
    )
    args.min_score = (
        args.min_score
        if args.min_score is not None
        else (profile or {}).get("minimum_score", 95)
    )
    args.hours = (
        args.hours
        if args.hours is not None
        else (profile or {}).get("report_hours", 48)
    )

    target_roles = build_target_roles(profile or {})
    resume_skills = build_resume_skills(profile or {})

    if not DB_PATH.exists():
        raise SystemExit("Could not find sponsorscan.db in this folder.")

    state_path = Path(args.state)
    previous_keys = set() if args.reset_state else load_previous_state(state_path)

    if args.hours <= 0:
        raise SystemExit("--hours must be greater than 0.")
    now_utc = datetime.now(timezone.utc)
    posted_cutoff = now_utc - timedelta(hours=args.hours)

    con = sqlite3.connect(DB_PATH)
    employers = load_employers(con)

    try:
        jobs = con.execute(
            "SELECT company, company_norm, title, location, url, posted, description, source "
            "FROM jobs"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        raise SystemExit(f"Could not read jobs table: {exc}")

    results = []
    counts = {
        "disqualifier": 0, "role": 0, "career": 0,
        "phd_title": 0, "level_title": 0, "experience_2plus": 0,
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

        level_score, exclusion_reason, level_signals, years_required = career_level_score(
            title, description, profile
        )
        if level_score is None:
            counts["career"] += 1
            if exclusion_reason == "PhD/doctoral/postdoc title":
                counts["phd_title"] += 1
            elif exclusion_reason == "level II or above title":
                counts["level_title"] += 1
            elif exclusion_reason and exclusion_reason.endswith("+ years required"):
                counts["experience_2plus"] += 1
            elif exclusion_reason == "PhD mentioned in description":
                counts["phd_required"] += 1
            elif exclusion_reason == "Master's mentioned in description":
                counts["masters_required"] += 1
            continue

        if not args.include_non_us and not is_us_location(location):
            counts["location"] += 1
            continue

        skills, skill_score = score_skills(title, description, resume_skills)
        resume_fit = min(100, role_base + skill_score)

        tier = company_tier(company)
        company_points = COMPANY_TIER_POINTS.get(tier, 2)

        emp = employers.get(company_norm)
        sponsor_points, sponsor_signals = sponsorship_score(emp, title, blob)

        combined_score = (
            resume_fit
            + level_score
            + company_points
            + sponsor_points
        )

        if combined_score < args.min_score:
            counts["score"] += 1
            continue

        key = stable_job_key(company, title, location, url)
        is_new = key not in previous_keys

        reasons = [
            f"{resume_fit}/100 resume fit",
            role_family,
            f"company tier {tier}/5",
        ]
        reasons.extend(level_signals)
        reasons.extend(sponsor_signals)

        results.append({
            "application_priority": "",  # assigned after score is known
            "combined_score": combined_score,
            "resume_fit_score": resume_fit,
            "career_level_score": level_score,
            "company_tier": tier,
            "company_fit_points": company_points,
            "sponsorship_score": sponsor_points,
            "is_new_since_last_run": "YES" if is_new else "NO",
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
        })

    results.sort(
        key=lambda r: (
            -(parse_posted_datetime(r["posted"]).timestamp()
              if parse_posted_datetime(r["posted"]) else 0),
            -r["combined_score"],
            -r["resume_fit_score"],
            -r["company_tier"],
            r["company"].lower(),
        )
    )

    for row in results:
        row["application_priority"] = priority_label(
            row["combined_score"],
            row["resume_fit_score"],
            row["career_level_score"],
        )

    new_results = [row for row in results if row["is_new_since_last_run"] == "YES"]

    fields = [
        "application_priority", "combined_score", "resume_fit_score",
        "career_level_score", "company_tier", "company_fit_points",
        "sponsorship_score", "is_new_since_last_run", "role_family",
        "matched_resume_skills", "experience_years_detected", "company",
        "title", "location", "posted", "source", "why_ranked", "url",
        "lca_certified", "job_key",
    ]

    write_csv(Path(args.out), results, fields)
    write_csv(Path(args.new_out), new_results, fields)
    save_state(state_path, {row["job_key"] for row in results})

    print(f"Wrote {len(results):,} jobs posted within the last {args.hours:g} hours to {args.out}")
    print(f"Wrote {len(new_results):,} jobs new since the previous run to {args.new_out}")
    print(f"Posting cutoff (UTC): {posted_cutoff.isoformat()}")
    if not previous_keys:
        print("No previous snapshot was found, so all current matches were counted as new.")
    print()
    print(f"{counts['posted_too_old']:,} dropped because they were older than {args.hours:g} hours")
    print(f"{counts['posted_unknown']:,} dropped because no usable posting time was supplied")
    print(f"{counts['disqualifier']:,} dropped for hard work-authorization/citizenship/clearance restrictions")
    print(f"{counts['career']:,} dropped for career-level restrictions")
    print(f"  - {counts['phd_title']:,} had PhD/doctoral/postdoc in the title")
    print(f"  - {counts['level_title']:,} had Level II/III/IV/V titles")
    print(f"  - {counts['experience_2plus']:,} exceeded the configured experience limit")
    print(f"  - {counts['phd_required']:,} mentioned a PhD in the description")
    print(f"  - {counts['masters_required']:,} mentioned a master's degree in the description")
    print(f"{counts['role']:,} dropped outside your target role list")
    print(f"{counts['location']:,} dropped as non-US or unrecognized")
    print(f"{counts['score']:,} dropped below combined score {args.min_score}")
    print()

    display_rows = new_results if new_results else results
    heading = "TOP NEW JOBS" if new_results else "TOP CURRENT JOBS"
    print(heading)
    print("=" * len(heading))

    for row in display_rows[:args.top]:
        print(
            f"[{row['combined_score']:>3}] {row['application_priority']} | "
            f"{row['company']} — {row['title']}"
        )
        print(
            f"      {row['location'] or '?'} | {row['role_family']} | "
            f"Resume {row['resume_fit_score']}/100 | Company {row['company_tier']}/5"
        )
        print(f"      Skills: {row['matched_resume_skills'] or 'title match only'}")
        print(f"      {row['url']}\n")


if __name__ == "__main__":
    main()
