"""Profile loading, merging and validation."""

import json

import pytest

from profile_loader import (
    DEFAULT_PROFILE,
    ProfileError,
    describe_profile,
    load_profile,
    validate_profile,
)


def write_profile(tmp_path, data):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_defaults_fill_in_omitted_fields(tmp_path):
    profile = load_profile(write_profile(tmp_path, {"profile_id": "min"}))
    assert profile["profile_id"] == "min"
    assert profile["max_required_experience"] == DEFAULT_PROFILE["max_required_experience"]
    assert profile["output_files"]["state"] == DEFAULT_PROFILE["output_files"]["state"]


def test_degree_required_flags_default_to_on(tmp_path):
    profile = load_profile(write_profile(tmp_path, {"profile_id": "x"}))
    assert profile["reject_phd_required"] is True
    assert profile["reject_masters_required"] is True
    # The blunter "any mention" variants stay opt-in.
    assert profile["reject_phd_mentions"] is False
    assert profile["reject_masters_mentions"] is False


def test_nested_output_files_merge_rather_than_replace(tmp_path):
    profile = load_profile(write_profile(tmp_path, {
        "profile_id": "x",
        "output_files": {"all_matches": "mine.csv"},
    }))
    assert profile["output_files"]["all_matches"] == "mine.csv"
    assert "state" in profile["output_files"]


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "absent.json")


def test_malformed_json_is_reported_clearly(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProfileError, match="invalid JSON"):
        load_profile(path)


def test_top_level_must_be_an_object(tmp_path):
    with pytest.raises(ProfileError, match="one top-level object"):
        load_profile(write_profile(tmp_path, ["nope"]))


@pytest.mark.parametrize("override,message", [
    ({"work_authorization": "h1b"}, "work_authorization"),
    ({"max_required_experience": -1}, "cannot be negative"),
    ({"minimum_score": -5}, "cannot be negative"),
    ({"report_hours": 0}, "greater than 0"),
    ({"profile_id": "   "}, "cannot be empty"),
    ({"target_roles": ["", "ok"]}, "non-empty string"),
    ({"skills": {"Python": -3}}, "non-negative integer"),
    ({"reject_phd_required": "yes"}, "must be of type bool"),
])
def test_invalid_values_are_rejected(override, message):
    profile = dict(DEFAULT_PROFILE, **override)
    with pytest.raises(ProfileError, match=message):
        validate_profile(profile)


def test_missing_output_file_keys_are_rejected():
    profile = dict(DEFAULT_PROFILE, output_files={"all_matches": "a.csv"})
    with pytest.raises(ProfileError, match="missing"):
        validate_profile(profile)


def test_shipped_example_profiles_are_valid():
    """The examples are what people copy, so they have to pass validation."""
    from pathlib import Path

    examples = sorted((Path(__file__).parent.parent / "profiles").glob("*.example.json"))
    assert examples, "no example profiles found"
    for example in examples:
        profile = load_profile(example)
        assert describe_profile(profile)
