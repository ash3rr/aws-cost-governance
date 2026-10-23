"""Tests for the log retention Config rule's evaluation logic. No AWS calls."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "log_retention_rule"))

import log_retention_rule as rule  # noqa: E402

TS = datetime(2026, 1, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, (0, [])),
        ("", (0, [])),
        ('{"maxRetentionDays": "0", "exemptPrefixes": ""}', (0, [])),
        ('{"maxRetentionDays": 365, "exemptPrefixes": "/aws/a, /b/"}', (365, ["/aws/a", "/b/"])),
    ],
)
def test_parse_parameters(raw, expected):
    assert rule.parse_parameters(raw) == expected


def test_never_expire_is_non_compliant():
    compliance, note = rule.evaluate_log_group({"logGroupName": "g"}, 0, [])
    assert compliance == "NON_COMPLIANT"
    assert "Never expire" in note


def test_any_retention_is_compliant_without_limit():
    assert rule.evaluate_log_group({"logGroupName": "g", "retentionInDays": 3653}, 0, [])[0] == "COMPLIANT"


def test_retention_over_limit_is_non_compliant():
    group = {"logGroupName": "g", "retentionInDays": 731}
    assert rule.evaluate_log_group(group, 365, [])[0] == "NON_COMPLIANT"
    group["retentionInDays"] = 365
    assert rule.evaluate_log_group(group, 365, [])[0] == "COMPLIANT"


def test_exempt_prefix_is_not_applicable():
    group = {"logGroupName": "/audit/trail"}
    assert rule.evaluate_log_group(group, 0, ["/audit/"])[0] == "NOT_APPLICABLE"


def test_deleted_log_groups_become_not_applicable():
    groups = [{"logGroupName": "a", "retentionInDays": 30}]
    evaluations = rule.build_evaluations(groups, {"a", "gone"}, 0, [], TS)
    by_id = {e["ComplianceResourceId"]: e["ComplianceType"] for e in evaluations}
    assert by_id == {"a": "COMPLIANT", "gone": "NOT_APPLICABLE"}
    assert all(e["ComplianceResourceType"] == "AWS::Logs::LogGroup" for e in evaluations)
    assert all(e["OrderingTimestamp"] == TS for e in evaluations)
