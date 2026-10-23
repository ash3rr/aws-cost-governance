"""AWS Config custom rule: CloudWatch Logs log groups must have a retention setting.

Why a custom rule: the managed rule cw-loggroup-retention-period-check marks a
log group with "Never expire" retention as COMPLIANT (it checks for a minimum
retention, for audit purposes). For cost control the opposite is wanted, so
this rule marks never-expire log groups NON_COMPLIANT.

The rule is periodic. On each run it lists log groups with DescribeLogGroups,
evaluates each one, and marks log groups that were evaluated before but no
longer exist as NOT_APPLICABLE so stale results do not linger.

Rule parameters (InputParameters JSON):
  maxRetentionDays   Optional. Log groups that keep data longer than this are
                     also NON_COMPLIANT. 0 or absent means no upper limit.
  exemptPrefixes     Optional. Comma-separated log group name prefixes to skip
                     (they are reported NOT_APPLICABLE).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RESOURCE_TYPE = "AWS::Logs::LogGroup"
PUT_EVALUATIONS_BATCH = 100  # API maximum per call
MAX_ANNOTATION = 256  # API maximum annotation length


# ---------------------------------------------------------------------------
# Evaluation logic (pure functions)
# ---------------------------------------------------------------------------
def parse_parameters(raw: str | None) -> tuple[int, list[str]]:
    params = json.loads(raw) if raw else {}
    max_days = int(params.get("maxRetentionDays") or 0)
    exempt = [p.strip() for p in str(params.get("exemptPrefixes") or "").split(",") if p.strip()]
    return max_days, exempt


def evaluate_log_group(group: dict, max_days: int, exempt: list[str]) -> tuple[str, str]:
    """Return (compliance type, annotation) for one DescribeLogGroups entry."""
    name = group["logGroupName"]
    if any(name.startswith(prefix) for prefix in exempt):
        return "NOT_APPLICABLE", "Exempt by prefix"
    retention = group.get("retentionInDays")
    if retention is None:
        return "NON_COMPLIANT", "Retention is Never expire. Set a retention period."
    if max_days and retention > max_days:
        return "NON_COMPLIANT", f"Retention {retention} days exceeds the {max_days} day limit."
    return "COMPLIANT", f"Retention {retention} days."


def build_evaluations(
    groups: list[dict],
    previously_evaluated: set[str],
    max_days: int,
    exempt: list[str],
    timestamp: datetime,
) -> list[dict]:
    evaluations = []
    current = set()
    for group in groups:
        name = group["logGroupName"]
        current.add(name)
        compliance, annotation = evaluate_log_group(group, max_days, exempt)
        evaluations.append(_evaluation(name, compliance, annotation, timestamp))
    for name in sorted(previously_evaluated - current):
        evaluations.append(
            _evaluation(name, "NOT_APPLICABLE", "Log group no longer exists.", timestamp)
        )
    return evaluations


def _evaluation(resource_id: str, compliance: str, annotation: str, ts: datetime) -> dict:
    return {
        "ComplianceResourceType": RESOURCE_TYPE,
        "ComplianceResourceId": resource_id,
        "ComplianceType": compliance,
        "Annotation": annotation[:MAX_ANNOTATION],
        "OrderingTimestamp": ts,
    }


# ---------------------------------------------------------------------------
# AWS access
# ---------------------------------------------------------------------------
def list_log_groups(logs) -> list[dict]:
    groups: list[dict] = []
    for page in logs.get_paginator("describe_log_groups").paginate():
        groups.extend(page.get("logGroups", []))
    return groups


def list_previous_results(config, rule_name: str) -> set[str]:
    ids: set[str] = set()
    paginator = config.get_paginator("get_compliance_details_by_config_rule")
    for page in paginator.paginate(
        ConfigRuleName=rule_name, ComplianceTypes=["COMPLIANT", "NON_COMPLIANT"]
    ):
        for result in page.get("EvaluationResults", []):
            qualifier = result["EvaluationResultIdentifier"]["EvaluationResultQualifier"]
            if qualifier.get("ResourceType") == RESOURCE_TYPE:
                ids.add(qualifier["ResourceId"])
    return ids


def handler(event, context):
    import boto3  # Lambda runtime provides boto3

    invoking = json.loads(event["invokingEvent"])
    if invoking.get("messageType") != "ScheduledNotification":
        logger.info("Ignoring message type %s", invoking.get("messageType"))
        return

    max_days, exempt = parse_parameters(event.get("ruleParameters"))
    timestamp = datetime.fromisoformat(
        invoking["notificationCreationTime"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    logs = boto3.client("logs")
    config = boto3.client("config")
    groups = list_log_groups(logs)
    previous = list_previous_results(config, event["configRuleName"])
    evaluations = build_evaluations(groups, previous, max_days, exempt, timestamp)

    non_compliant = sum(1 for e in evaluations if e["ComplianceType"] == "NON_COMPLIANT")
    logger.info(
        "Evaluated %d log groups, %d non-compliant, %d stale",
        len(groups),
        non_compliant,
        len(evaluations) - len(groups),
    )

    for start in range(0, len(evaluations), PUT_EVALUATIONS_BATCH):
        batch = evaluations[start : start + PUT_EVALUATIONS_BATCH]
        resp = config.put_evaluations(Evaluations=batch, ResultToken=event["resultToken"])
        failed = resp.get("FailedEvaluations", [])
        if failed:
            raise RuntimeError(f"Config rejected {len(failed)} evaluations: {failed[:3]}")
