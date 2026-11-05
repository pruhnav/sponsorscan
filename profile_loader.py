#!/usr/bin/env python3
"""
Profile loading and validation utilities for SponsorScan.

Profiles are JSON files that store candidate-specific configuration such as:

- work authorization
- target roles
- maximum acceptable experience
- degree filters
- resume skills
- preferred locations and companies
- company tiers
- notification preferences

`work_authorization` is a label for the candidate's situation; the reject_*
booleans are what actually filter. `sponsor_daily_report.py` warns when the two
disagree rather than rewriting either.

`notifications` is read by whatever automation you wire up, not by the report
itself, which always writes both CSVs.

This module intentionally contains no secrets and can be committed safely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


VALID_WORK_AUTHORIZATION = {
    "opt",
    "stem_opt",
    "us_citizen",
    "permanent_resident",
    "other",
}

DEFAULT_PROFILE: dict[str, Any] = {
    "profile_id": "default",
    "name": "Candidate",
    "work_authorization": "other",
    "max_required_experience": 1,
    "reject_senior_titles": True,
    "reject_level_ii_plus_titles": True,
    # "_required" detects a stated requirement ("must hold a PhD") and is on
    # by default. "_mentions" fires on any occurrence at all, including "a
    # Master's is a plus", so it stays opt-in.
    "reject_phd_required": True,
    "reject_masters_required": True,
    "reject_masters_mentions": False,
    "reject_phd_mentions": False,
    "reject_clearance_roles": True,
    "reject_citizenship_required": True,
    "reject_permanent_authorization_required": True,
    "reject_opt_excluded": True,
    # "Unable to sponsor now or in the future". Applies only to a profile that
    # needs sponsorship; a citizen's profile can leave it on harmlessly.
    "reject_no_future_sponsorship": True,
    "target_roles": [],
    "skills": {},
    "preferred_locations": [],
    "preferred_companies": [],
    "company_tiers": {},
    "minimum_score": 95,
    "report_hours": 48,
    "output_files": {
        "all_matches": "matches_48h.csv",
        "new_matches": "new_jobs_48h.csv",
        "state": ".sponsorscan_state.json",
    },
    "notifications": {
        "email_enabled": False,
        "google_sheets_enabled": False,
    },
}


class ProfileError(ValueError):
    """Raised when a SponsorScan profile is missing or invalid."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override into base without mutating either input.
    """
    result = dict(base)

    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value

    return result


def _require_type(
    profile: dict[str, Any],
    key: str,
    expected_type: type | tuple[type, ...],
) -> None:
    if not isinstance(profile.get(key), expected_type):
        expected_name = (
            ", ".join(t.__name__ for t in expected_type)
            if isinstance(expected_type, tuple)
            else expected_type.__name__
        )
        raise ProfileError(
            f"Profile field '{key}' must be of type {expected_name}."
        )


def validate_profile(profile: dict[str, Any]) -> None:
    """
    Validate a merged SponsorScan profile.

    Raises:
        ProfileError: when a required field is invalid.
    """
    _require_type(profile, "profile_id", str)
    _require_type(profile, "name", str)
    _require_type(profile, "work_authorization", str)
    _require_type(profile, "max_required_experience", int)
    _require_type(profile, "minimum_score", int)
    _require_type(profile, "report_hours", (int, float))
    _require_type(profile, "target_roles", list)
    _require_type(profile, "skills", dict)
    _require_type(profile, "preferred_locations", list)
    _require_type(profile, "preferred_companies", list)
    _require_type(profile, "company_tiers", dict)
    _require_type(profile, "output_files", dict)
    _require_type(profile, "notifications", dict)

    if not profile["profile_id"].strip():
        raise ProfileError("Profile field 'profile_id' cannot be empty.")

    if profile["work_authorization"] not in VALID_WORK_AUTHORIZATION:
        allowed = ", ".join(sorted(VALID_WORK_AUTHORIZATION))
        raise ProfileError(
            "Profile field 'work_authorization' must be one of: "
            f"{allowed}."
        )

    if profile["max_required_experience"] < 0:
        raise ProfileError(
            "Profile field 'max_required_experience' cannot be negative."
        )

    if profile["minimum_score"] < 0:
        raise ProfileError("Profile field 'minimum_score' cannot be negative.")

    if profile["report_hours"] <= 0:
        raise ProfileError("Profile field 'report_hours' must be greater than 0.")

    for role in profile["target_roles"]:
        if not isinstance(role, str) or not role.strip():
            raise ProfileError(
                "Every entry in 'target_roles' must be a non-empty string."
            )

    for skill, weight in profile["skills"].items():
        if not isinstance(skill, str) or not skill.strip():
            raise ProfileError(
                "Every skill name in 'skills' must be a non-empty string."
            )

        if not isinstance(weight, int) or weight < 0:
            raise ProfileError(
                f"Skill weight for '{skill}' must be a non-negative integer."
            )

    for key in (
        "reject_senior_titles",
        "reject_level_ii_plus_titles",
        "reject_phd_required",
        "reject_masters_required",
        "reject_masters_mentions",
        "reject_phd_mentions",
        "reject_clearance_roles",
        "reject_citizenship_required",
        "reject_permanent_authorization_required",
        "reject_opt_excluded",
        "reject_no_future_sponsorship",
    ):
        _require_type(profile, key, bool)

    required_output_keys = {"all_matches", "new_matches", "state"}
    missing_output_keys = required_output_keys - set(profile["output_files"])
    if missing_output_keys:
        missing = ", ".join(sorted(missing_output_keys))
        raise ProfileError(
            f"Profile field 'output_files' is missing: {missing}."
        )

    for key in ("email_enabled", "google_sheets_enabled"):
        if not isinstance(profile["notifications"].get(key), bool):
            raise ProfileError(
                f"Profile notification field '{key}' must be true or false."
            )


# Authorizations that never need an employer to sponsor a visa. For these,
# LCA filing history says nothing about whether a job is worth applying to.
NO_SPONSORSHIP_NEEDED = {"us_citizen", "permanent_resident"}


def needs_sponsorship(profile: dict[str, Any] | None) -> bool:
    """False for citizens and permanent residents; True otherwise, and with
    no profile, since the tool's default audience needs sponsorship."""
    return (profile or {}).get("work_authorization") not in NO_SPONSORSHIP_NEEDED


def load_profile(path: str | Path) -> dict[str, Any]:
    """
    Load, merge, and validate a SponsorScan profile JSON file.

    Args:
        path: Path to a JSON profile.

    Returns:
        A validated profile dictionary containing all default fields.

    Raises:
        ProfileError: when the file is missing, malformed, or invalid.
    """
    profile_path = Path(path)

    if not profile_path.exists():
        raise ProfileError(f"Profile file not found: {profile_path}")

    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(
            f"Profile file contains invalid JSON: {exc}"
        ) from exc
    except OSError as exc:
        raise ProfileError(
            f"Could not read profile file '{profile_path}': {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ProfileError("Profile JSON must contain one top-level object.")

    profile = _deep_merge(DEFAULT_PROFILE, raw)
    validate_profile(profile)

    return profile


def describe_profile(profile: dict[str, Any]) -> str:
    """
    Return a short human-readable summary for logs.
    """
    return (
        f"{profile['name']} "
        f"({profile['profile_id']}) | "
        f"work authorization: {profile['work_authorization']} | "
        f"max experience: {profile['max_required_experience']} year(s) | "
        f"target roles: {len(profile['target_roles'])} | "
        f"skills: {len(profile['skills'])}"
    )


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
