"""Tests for the log group audit's row conversion. No AWS calls."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "audit"))

import cloudwatch_log_groups_audit as audit  # noqa: E402


def test_never_expire_is_flagged():
    row = audit.to_row("eu-west-1", {"logGroupName": "/app/a", "storedBytes": 10})
    assert row.never_expires
    assert row.log_class == "STANDARD"
    assert row.finding() == "never expires"
    assert row.as_row()["retention_days"] == "never"


def test_long_retention_is_flagged():
    row = audit.to_row("eu-west-1", {"logGroupName": "a", "retentionInDays": 731})
    assert row.finding(365) == "retention over 365 days"
    assert row.finding(3653) == ""


def test_class_and_creation_time_are_read():
    row = audit.to_row(
        "eu-west-1",
        {"logGroupName": "a", "retentionInDays": 14, "logGroupClass": "INFREQUENT_ACCESS", "creationTime": 0},
    )
    assert row.log_class == "INFREQUENT_ACCESS"
    assert row.created is None  # 0 is treated as missing
    row = audit.to_row("eu-west-1", {"logGroupName": "a", "creationTime": 1_700_000_000_000})
    assert row.as_row()["created"] == "2023-11-14"


def test_human_bytes():
    assert audit.human_bytes(512) == "512 B"
    assert audit.human_bytes(1536) == "1.5 KiB"
    assert audit.human_bytes(5 * 1024**3) == "5.0 GiB"
