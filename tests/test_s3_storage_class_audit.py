"""Tests for the S3 audit's analysis and planning logic. No AWS calls."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit"))

import s3_storage_class_audit as audit  # noqa: E402

IT_RULE = {
    "ID": "it",
    "Status": "Enabled",
    "Filter": {},
    "Transitions": [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}],
}
EXPIRE_30 = {"ID": "exp", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 30}}
NONCURRENT = {
    "ID": "nc",
    "Status": "Enabled",
    "Filter": {},
    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
}
ARCHIVE = [
    {
        "Id": "archive",
        "Status": "Enabled",
        "Tierings": [
            {"AccessTier": "DEEP_ARCHIVE_ACCESS", "Days": 180},
            {"AccessTier": "ARCHIVE_ACCESS", "Days": 90},
        ],
    }
]


def analyse(rules, archive=(), versioning="Disabled"):
    return audit.analyse_bucket(audit.BucketReport("b"), list(rules), list(archive), versioning)


@pytest.mark.parametrize(
    "rule,expected",
    [
        ({"Filter": {}}, "whole bucket"),
        ({"Filter": {"Prefix": ""}}, "whole bucket"),
        ({"Filter": {"Prefix": "raw/"}}, "prefix=raw/"),
        ({"Prefix": "legacy/"}, "prefix=legacy/"),
        ({"Prefix": ""}, "whole bucket"),
        (
            {"Filter": {"And": {"Prefix": "a/", "Tags": [{"Key": "k", "Value": "v"}], "ObjectSizeGreaterThan": 131072}}},
            "prefix=a/,tag=k=v,size>131072",
        ),
    ],
)
def test_rule_scope(rule, expected):
    assert audit.rule_scope(rule) == expected


def test_compliant_prod_bucket_has_no_findings():
    rep = analyse([IT_RULE, NONCURRENT], ARCHIVE, "Enabled")
    assert rep.it_transition == "day 0"
    assert rep.it_archive_config == "archive(ARCHIVE_ACCESS:90,DEEP_ARCHIVE_ACCESS:180)"
    assert rep.noncurrent_expiration == "30d"
    assert rep.findings == []


def test_archive_config_without_transition_is_flagged():
    # The mistake this repo exists to catch: archive tiers configured, but
    # nothing ever moves objects into INTELLIGENT_TIERING.
    rep = analyse([NONCURRENT], ARCHIVE, "Enabled")
    assert rep.it_transition == "no"
    assert "archive tiers configured but nothing transitions into INTELLIGENT_TIERING" in rep.findings
    assert "objects never transition or expire" in rep.findings


def test_bucket_without_lifecycle():
    rep = analyse([], versioning="Enabled")
    assert "no lifecycle configuration" in rep.findings
    assert "noncurrent versions never expire" in rep.findings
    assert "incomplete multipart uploads never aborted" in rep.findings


def test_disabled_rules_do_not_count():
    disabled = dict(IT_RULE, Status="Disabled")
    rep = analyse([disabled])
    assert rep.it_transition == "no"
    assert "lifecycle rules exist but none are enabled" in rep.findings


def test_delete_marker_only_expiration_is_not_expiration():
    rule = {"ID": "dm", "Status": "Enabled", "Filter": {}, "Expiration": {"ExpiredObjectDeleteMarker": True}}
    assert analyse([rule]).expiration == "no"


def test_plan_adds_rule_and_keeps_existing():
    new_rules, reason = audit.plan_transition_rule([NONCURRENT], 0)
    assert reason == "add transition rule"
    assert new_rules[0] == NONCURRENT
    assert new_rules[1]["ID"] == audit.TRANSITION_RULE_ID
    assert new_rules[1]["Transitions"] == [{"Days": 0, "StorageClass": "INTELLIGENT_TIERING"}]


def test_plan_matches_legacy_prefix_style():
    legacy = {"ID": "old", "Status": "Enabled", "Prefix": "", "NoncurrentVersionExpiration": {"NoncurrentDays": 30}}
    new_rules, _ = audit.plan_transition_rule([legacy], 0)
    assert new_rules[1]["Prefix"] == ""
    assert "Filter" not in new_rules[1]
    new_rules, _ = audit.plan_transition_rule([], 0)
    assert new_rules[0]["Filter"] == {"Prefix": ""}


def test_plan_does_not_mutate_input():
    rules = [NONCURRENT]
    audit.plan_transition_rule(rules, 0)
    assert rules == [NONCURRENT]


def test_plan_skips_when_transition_exists():
    new_rules, reason = audit.plan_transition_rule([IT_RULE], 0)
    assert new_rules is None
    assert "already transitions" in reason


def test_plan_skips_short_lived_buckets():
    new_rules, reason = audit.plan_transition_rule([EXPIRE_30], 0)
    assert new_rules is None
    assert "expire after 30 days" in reason


def test_plan_ignores_prefix_scoped_expiration():
    prefixed = dict(EXPIRE_30, Filter={"Prefix": "tmp/"})
    new_rules, _ = audit.plan_transition_rule([prefixed], 0)
    assert new_rules is not None


def test_plan_does_not_duplicate_disabled_rule_id():
    disabled = dict(IT_RULE, ID=audit.TRANSITION_RULE_ID, Status="Disabled")
    new_rules, reason = audit.plan_transition_rule([disabled], 0)
    assert new_rules is None
    assert "disabled" in reason


def test_apply_requires_explicit_scope_for_real_writes():
    with pytest.raises(SystemExit):
        audit.parse_args(["--apply", "--no-dry-run"])
    args = audit.parse_args(["--apply", "--no-dry-run", "--bucket", "x"])
    assert args.dry_run is False
    assert audit.parse_args(["--apply"]).dry_run is True
