"""Tests for the Slack message formatter. No AWS calls are made."""

import copy
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "notifier"))

import slack_notifier as sn  # noqa: E402

EVENT_FILE = Path(__file__).parent / "events" / "non_compliant_bucket.json"


@pytest.fixture
def event():
    return json.loads(EVENT_FILE.read_text())


def lines(payload):
    return payload["text"].split("\n")


def test_non_compliant_event_contains_all_fields(event):
    payload = sn.format_message(event)
    text = payload["text"]
    assert lines(payload)[0] == "AWS Config: resource is NON_COMPLIANT"
    assert "Resource: example-raw-data-bucket" in text
    assert "Type: AWS::S3::Bucket" in text
    assert "Account: EXAMPLE-ACCOUNT" in text
    assert "Region: eu-central-1" in text
    assert "Rule: cost-s3-lifecycle-policy-present" in text
    assert "Previous state: COMPLIANT" in text


def test_console_link_points_at_rule_in_event_region(event):
    payload = sn.format_message(event)
    console = [line for line in lines(payload) if line.startswith("Console: ")]
    assert console == [
        "Console: https://eu-central-1.console.aws.amazon.com/config/home"
        "?region=eu-central-1#/rules/details?configRuleName=cost-s3-lifecycle-policy-present"
    ]


@pytest.mark.parametrize("compliance", ["COMPLIANT", "NOT_APPLICABLE", "INSUFFICIENT_DATA"])
def test_other_compliance_types_are_skipped(event, compliance):
    event["detail"]["newEvaluationResult"]["complianceType"] = compliance
    assert sn.format_message(event) is None


def test_missing_detail_is_skipped():
    assert sn.format_message({}) is None


def test_first_evaluation_has_no_previous_state(event):
    del event["detail"]["oldEvaluationResult"]
    assert "Previous state: none" in sn.format_message(event)["text"]


def test_falls_back_to_envelope_account_and_region(event):
    del event["detail"]["awsAccountId"]
    del event["detail"]["awsRegion"]
    text = sn.format_message(event)["text"]
    assert "Account: EXAMPLE-ACCOUNT" in text
    assert "Region: eu-central-1" in text


def test_annotation_is_included_and_truncated(event):
    event["detail"]["newEvaluationResult"]["annotation"] = "x" * 1000
    detail_line = next(l for l in lines(sn.format_message(event)) if l.startswith("Detail: "))
    assert len(detail_line) == len("Detail: ") + sn.MAX_ANNOTATION_CHARS
    assert detail_line.endswith("...")


def test_no_detail_line_without_annotation(event):
    assert not any(l.startswith("Detail:") for l in lines(sn.format_message(event)))


def test_slack_control_characters_are_escaped(event):
    event["detail"]["resourceId"] = "<!channel> & co"
    text = sn.format_message(event)["text"]
    assert "Resource: &lt;!channel&gt; &amp; co" in text
    assert "<!channel>" not in text


def test_message_is_plain_text(event):
    payload = sn.format_message(event)
    assert payload["mrkdwn"] is False
    assert set(payload) == {"text", "mrkdwn"}
    # No emoji shortcodes and nothing outside printable ASCII.
    assert not re.search(r":[a-z_]+:", payload["text"].replace("https:", ""))
    assert all(32 <= ord(c) < 127 or c == "\n" for c in payload["text"])


def test_log_group_names_are_url_encoded_in_link(event):
    event["detail"]["resourceType"] = "AWS::Logs::LogGroup"
    event["detail"]["resourceId"] = "/aws/lambda/example"
    event["detail"]["configRuleName"] = "rule with space"
    text = sn.format_message(event)["text"]
    assert "Resource: /aws/lambda/example" in text
    assert "configRuleName=rule%20with%20space" in text


@pytest.mark.parametrize(
    "region,host",
    [
        ("eu-west-1", "eu-west-1.console.aws.amazon.com"),
        ("cn-north-1", "cn-north-1.console.amazonaws.cn"),
        ("us-gov-west-1", "us-gov-west-1.console.amazonaws-us-gov.com"),
    ],
)
def test_console_host_per_partition(region, host):
    assert sn.rule_console_url(region, "r").startswith(f"https://{host}/")


def test_format_does_not_mutate_event(event):
    before = copy.deepcopy(event)
    sn.format_message(event)
    assert event == before


@pytest.mark.parametrize(
    "secret,expected",
    [
        ("https://hooks.slack.com/services/T/B/X", "https://hooks.slack.com/services/T/B/X"),
        ('{"webhook_url": "https://hooks.slack.com/services/T/B/X"}', "https://hooks.slack.com/services/T/B/X"),
        ("  https://hooks.slack.com/services/T/B/X\n", "https://hooks.slack.com/services/T/B/X"),
    ],
)
def test_parse_secret(secret, expected):
    assert sn.parse_secret(secret) == expected


def test_parse_secret_rejects_non_https():
    with pytest.raises(ValueError):
        sn.parse_secret("http://example.com/hook")
