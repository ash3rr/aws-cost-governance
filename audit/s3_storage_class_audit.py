#!/usr/bin/env python3
"""Audit S3 buckets for the lifecycle settings that drive storage cost.

For every general purpose bucket the caller can see, the script reports:

- whether an enabled lifecycle rule transitions objects to INTELLIGENT_TIERING
- whether an S3 Intelligent-Tiering archive configuration exists (the optional
  Archive Access and Deep Archive Access tiers)
- whether an enabled lifecycle rule expires current objects
- the bucket versioning status
- whether an enabled lifecycle rule expires noncurrent object versions
- whether an enabled lifecycle rule aborts incomplete multipart uploads
- whether the bucket belongs to a CloudFormation stack

Results go to the console as a table and to a CSV file.

With --apply the script plans a lifecycle rule that transitions objects to
INTELLIGENT_TIERING on buckets that lack one. It is a dry run unless
--no-dry-run is given. The new rule is merged with the existing rules because
PutBucketLifecycleConfiguration replaces the whole lifecycle configuration.

Note: an Intelligent-Tiering *configuration* (PutBucketIntelligentTieringConfiguration)
does not move objects into the INTELLIGENT_TIERING storage class. It only turns
on the archive tiers for objects that are already in that class. Objects get
there through a lifecycle transition or by being written with that storage
class. This script checks both settings separately for that reason.

Exit codes: 0 audit complete, 1 fatal error, 2 audit complete but one or more
buckets could not be read or changed.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CSV_PATH = "s3_storage_class_audit.csv"

# ID of the lifecycle rule that --apply adds.
TRANSITION_RULE_ID = "transition-to-intelligent-tiering"

# Days after creation before objects transition. 0 means the first lifecycle
# run after the object is written (lifecycle runs once a day, at midnight UTC).
DEFAULT_TRANSITION_DAYS = 0

# Buckets whose current objects expire within this many days are skipped by
# --apply. Intelligent-Tiering only moves an object to the Infrequent Access
# tier after 30 days without access, so short-lived objects would pay the
# per-object monitoring charge without ever reaching a cheaper tier.
MIN_EXPIRATION_DAYS_FOR_TIERING = 30

# CloudFormation tags every resource it creates with this key.
STACK_TAG_KEY = "aws:cloudformation:stack-name"
UNKNOWN_STACK = "(tags not readable)"

# Width limit for console table cells. The CSV always has full values.
MAX_CELL_WIDTH = 48

BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

CSV_COLUMNS = [
    "bucket",
    "region",
    "stack",
    "versioning",
    "it_transition",
    "it_transition_scope",
    "it_archive_config",
    "expiration",
    "noncurrent_expiration",
    "abort_incomplete_mpu",
    "lifecycle_rule_count",
    "findings",
    "action",
    "error",
]

TABLE_COLUMNS = [
    ("bucket", "Bucket"),
    ("region", "Region"),
    ("versioning", "Versioning"),
    ("it_transition", "IT transition"),
    ("it_archive_config", "IT archive"),
    ("expiration", "Expiration"),
    ("noncurrent_expiration", "Noncurrent exp"),
    ("findings", "Findings"),
]


# ---------------------------------------------------------------------------
# Pure analysis helpers (no AWS calls, unit tested)
# ---------------------------------------------------------------------------
@dataclass
class BucketReport:
    bucket: str
    region: str = ""
    stack: str = ""
    versioning: str = ""
    it_transition: str = ""
    it_transition_scope: str = ""
    it_archive_config: str = ""
    expiration: str = ""
    noncurrent_expiration: str = ""
    abort_incomplete_mpu: str = ""
    lifecycle_rule_count: int = 0
    findings: list[str] = field(default_factory=list)
    action: str = ""
    error: str = ""

    def as_row(self) -> dict[str, Any]:
        row = {name: getattr(self, name) for name in CSV_COLUMNS}
        row["findings"] = "; ".join(self.findings)
        return row


def is_enabled(rule: dict) -> bool:
    return rule.get("Status") == "Enabled"


def rule_scope(rule: dict) -> str:
    """Describe which objects a lifecycle rule applies to."""
    if "Filter" not in rule:
        # Legacy rules carry Prefix at the top level instead of in Filter.
        prefix = rule.get("Prefix", "")
        return f"prefix={prefix}" if prefix else "whole bucket"

    flt = rule.get("Filter") or {}
    parts = flt.get("And", flt)
    described = []
    if parts.get("Prefix"):
        described.append(f"prefix={parts['Prefix']}")
    tags = parts.get("Tags") or ([parts["Tag"]] if "Tag" in parts else [])
    for tag in tags:
        described.append(f"tag={tag.get('Key')}={tag.get('Value')}")
    if "ObjectSizeGreaterThan" in parts:
        described.append(f"size>{parts['ObjectSizeGreaterThan']}")
    if "ObjectSizeLessThan" in parts:
        described.append(f"size<{parts['ObjectSizeLessThan']}")
    return ",".join(described) if described else "whole bucket"


def transitions_to_intelligent_tiering(rule: dict) -> int | None:
    """Return the transition day count if the rule moves objects to IT."""
    for transition in rule.get("Transitions", []):
        if transition.get("StorageClass") == "INTELLIGENT_TIERING":
            if "Days" in transition:
                return int(transition["Days"])
            return 0  # date-based transition, already in the past or upcoming
    return None


def expiration_days(rule: dict) -> int | None:
    """Return the current-version expiration in days, if the rule has one.

    A rule whose Expiration only removes expired delete markers does not
    expire objects, so it is ignored here.
    """
    exp = rule.get("Expiration") or {}
    if "Days" in exp:
        return int(exp["Days"])
    if "Date" in exp:
        return 0
    return None


def summarise_archive_configs(configs: list[dict]) -> str:
    """Describe enabled Intelligent-Tiering archive configurations."""
    described = []
    for cfg in configs:
        if cfg.get("Status") != "Enabled":
            continue
        tiers = ",".join(
            f"{t['AccessTier']}:{t['Days']}"
            for t in sorted(cfg.get("Tierings", []), key=lambda t: t["Days"])
        )
        described.append(f"{cfg.get('Id', '?')}({tiers})")
    return " ".join(described)


def analyse_bucket(
    report: BucketReport,
    lifecycle_rules: list[dict],
    archive_configs: list[dict],
    versioning_status: str,
) -> BucketReport:
    """Fill in the report fields and findings from raw bucket settings."""
    enabled = [r for r in lifecycle_rules if is_enabled(r)]
    report.lifecycle_rule_count = len(lifecycle_rules)
    report.versioning = versioning_status

    it_rules = [
        (r, transitions_to_intelligent_tiering(r))
        for r in enabled
        if transitions_to_intelligent_tiering(r) is not None
    ]
    if it_rules:
        report.it_transition = ",".join(f"day {d}" for _, d in it_rules)
        report.it_transition_scope = " | ".join(rule_scope(r) for r, _ in it_rules)
    else:
        report.it_transition = "no"

    report.it_archive_config = summarise_archive_configs(archive_configs) or "no"

    exp = [
        f"{expiration_days(r)}d ({rule_scope(r)})"
        for r in enabled
        if expiration_days(r) is not None
    ]
    report.expiration = " | ".join(exp) or "no"

    noncurrent = [
        f"{r['NoncurrentVersionExpiration'].get('NoncurrentDays', '?')}d"
        for r in enabled
        if "NoncurrentVersionExpiration" in r
    ]
    report.noncurrent_expiration = ",".join(noncurrent) or "no"

    abort = [
        f"{r['AbortIncompleteMultipartUpload'].get('DaysAfterInitiation', '?')}d"
        for r in enabled
        if "AbortIncompleteMultipartUpload" in r
    ]
    report.abort_incomplete_mpu = ",".join(abort) or "no"

    if not lifecycle_rules:
        report.findings.append("no lifecycle configuration")
    elif not enabled:
        report.findings.append("lifecycle rules exist but none are enabled")
    if not it_rules and report.expiration == "no":
        report.findings.append("objects never transition or expire")
    if report.it_archive_config != "no" and not it_rules:
        report.findings.append(
            "archive tiers configured but nothing transitions into INTELLIGENT_TIERING"
        )
    if versioning_status in ("Enabled", "Suspended") and not noncurrent:
        report.findings.append("noncurrent versions never expire")
    if not abort:
        report.findings.append("incomplete multipart uploads never aborted")
    return report


def plan_transition_rule(
    lifecycle_rules: list[dict],
    transition_days: int,
    min_expiration_days: int = MIN_EXPIRATION_DAYS_FOR_TIERING,
) -> tuple[list[dict] | None, str]:
    """Work out the lifecycle rules to write, or why the bucket is skipped.

    Returns (new_rules, reason). new_rules is None when nothing should change.
    """
    enabled = [r for r in lifecycle_rules if is_enabled(r)]
    if any(transitions_to_intelligent_tiering(r) is not None for r in enabled):
        return None, "skip: already transitions to INTELLIGENT_TIERING"

    short_lived = [
        expiration_days(r)
        for r in enabled
        if rule_scope(r) == "whole bucket"
        and expiration_days(r) is not None
        and expiration_days(r) <= min_expiration_days
    ]
    if short_lived:
        return None, (
            f"skip: objects expire after {min(short_lived)} days, "
            "too soon to benefit from tiering"
        )

    if any(r.get("ID") == TRANSITION_RULE_ID for r in lifecycle_rules):
        return None, f"skip: a rule named {TRANSITION_RULE_ID} exists but is disabled"

    new_rule: dict[str, Any] = {
        "ID": TRANSITION_RULE_ID,
        "Status": "Enabled",
        "Transitions": [
            {"Days": transition_days, "StorageClass": "INTELLIGENT_TIERING"}
        ],
    }
    # Older configurations put Prefix on the rule instead of in Filter. Match
    # the existing style rather than mixing the two in one configuration.
    if any("Filter" not in r and "Prefix" in r for r in lifecycle_rules):
        new_rule["Prefix"] = ""
    else:
        new_rule["Filter"] = {"Prefix": ""}
    return copy.deepcopy(lifecycle_rules) + [new_rule], "add transition rule"


# ---------------------------------------------------------------------------
# AWS access
# ---------------------------------------------------------------------------
class ClientCache:
    """One S3 client per region, so requests go to the bucket's own region."""

    def __init__(self, session: boto3.session.Session):
        self.session = session
        self._clients: dict[str, Any] = {}

    def s3(self, region: str):
        if region not in self._clients:
            self._clients[region] = self.session.client(
                "s3", region_name=region, config=BOTO_CONFIG
            )
        return self._clients[region]


def error_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "Unknown")


def list_buckets(s3) -> list[dict]:
    buckets = []
    # Passing a page size makes ListBuckets return BucketRegion for each bucket.
    paginator = s3.get_paginator("list_buckets")
    for page in paginator.paginate(PaginationConfig={"PageSize": 1000}):
        buckets.extend(page.get("Buckets", []))
    return buckets


def resolve_region(s3, bucket: dict) -> str:
    if bucket.get("BucketRegion"):
        return bucket["BucketRegion"]
    location = s3.get_bucket_location(Bucket=bucket["Name"]).get("LocationConstraint")
    if not location:
        return "us-east-1"
    if location == "EU":
        return "eu-west-1"
    return location


def get_lifecycle(s3, bucket: str) -> tuple[list[dict], str | None]:
    """Return (rules, TransitionDefaultMinimumObjectSize)."""
    try:
        resp = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        if error_code(exc) == "NoSuchLifecycleConfiguration":
            return [], None
        raise
    return resp.get("Rules", []), resp.get("TransitionDefaultMinimumObjectSize")


def get_archive_configs(s3, bucket: str) -> list[dict]:
    configs: list[dict] = []
    kwargs: dict[str, Any] = {"Bucket": bucket}
    while True:
        resp = s3.list_bucket_intelligent_tiering_configurations(**kwargs)
        configs.extend(resp.get("IntelligentTieringConfigurationList", []))
        if not resp.get("IsTruncated"):
            return configs
        kwargs["ContinuationToken"] = resp["NextContinuationToken"]


def get_versioning(s3, bucket: str) -> str:
    return s3.get_bucket_versioning(Bucket=bucket).get("Status", "Disabled")


def get_stack_name(s3, bucket: str) -> str:
    """Return the owning stack name, "" if none, or UNKNOWN_STACK if unreadable."""
    try:
        tags = s3.get_bucket_tagging(Bucket=bucket).get("TagSet", [])
    except ClientError as exc:
        if error_code(exc) == "NoSuchTagSet":
            return ""
        if error_code(exc) == "AccessDenied":
            # Tags are not needed for the audit itself, only for --apply.
            return UNKNOWN_STACK
        raise
    return next((t["Value"] for t in tags if t["Key"] == STACK_TAG_KEY), "")


def audit_bucket(clients: ClientCache, home_s3, bucket: dict) -> tuple[BucketReport, list, str | None]:
    """Read one bucket's settings. Errors are recorded on the report."""
    report = BucketReport(bucket=bucket["Name"])
    rules: list[dict] = []
    min_size = None
    try:
        report.region = resolve_region(home_s3, bucket)
        s3 = clients.s3(report.region)
        rules, min_size = get_lifecycle(s3, report.bucket)
        archive = get_archive_configs(s3, report.bucket)
        versioning = get_versioning(s3, report.bucket)
        report.stack = get_stack_name(s3, report.bucket)
        analyse_bucket(report, rules, archive, versioning)
    except ClientError as exc:
        report.error = f"{error_code(exc)}: {exc.operation_name}"
    except BotoCoreError as exc:
        report.error = type(exc).__name__
    return report, rules, min_size


def apply_transition(
    clients: ClientCache,
    report: BucketReport,
    rules: list[dict],
    min_size: str | None,
    transition_days: int,
    dry_run: bool,
    include_stack_managed: bool,
) -> None:
    if report.error:
        report.action = "skip: bucket could not be read"
        return
    if report.stack == UNKNOWN_STACK and not include_stack_managed:
        report.action = "skip: tags not readable, cannot tell if a stack owns it"
        return
    if report.stack and not include_stack_managed:
        report.action = f"skip: managed by stack {report.stack}, change the template"
        return

    new_rules, reason = plan_transition_rule(rules, transition_days)
    if new_rules is None:
        report.action = reason
        return

    params: dict[str, Any] = {
        "Bucket": report.bucket,
        "LifecycleConfiguration": {"Rules": new_rules},
    }
    # Keep the bucket's existing small-object transition behaviour.
    if min_size:
        params["TransitionDefaultMinimumObjectSize"] = min_size

    if dry_run:
        report.action = "would add transition rule (dry run)"
        print(f"\n[dry run] {report.bucket}: lifecycle rules after change:")
        print(json.dumps(new_rules, indent=2, default=str))
        return

    try:
        clients.s3(report.region).put_bucket_lifecycle_configuration(**params)
        report.action = "added transition rule"
    except ClientError as exc:
        report.action = "failed"
        report.error = f"{error_code(exc)}: {exc.operation_name}"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def truncate(value: Any, width: int = MAX_CELL_WIDTH) -> str:
    text = str(value)
    return text if len(text) <= width else text[: width - 3] + "..."


def print_table(reports: list[BucketReport]) -> None:
    rows = []
    for rep in reports:
        row = rep.as_row()
        if rep.error:
            row["findings"] = f"ERROR {rep.error}"
        rows.append([truncate(row[key]) for key, _ in TABLE_COLUMNS])
    headers = [label for _, label in TABLE_COLUMNS]
    widths = [
        max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def write_csv(path: str, reports: list[BucketReport]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for rep in reports:
            writer.writerow(rep.as_row())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit S3 buckets for Intelligent-Tiering and lifecycle settings."
    )
    parser.add_argument("--profile", help="AWS named profile to use")
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="Region for the initial ListBuckets call (default: us-east-1)",
    )
    parser.add_argument(
        "--bucket",
        action="append",
        default=[],
        help="Only audit this bucket. Repeat for several.",
    )
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="CSV output path")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Add a lifecycle transition to INTELLIGENT_TIERING where missing",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="With --apply, only print the planned change (default). "
        "Use --no-dry-run to write.",
    )
    parser.add_argument(
        "--all-buckets",
        action="store_true",
        help="Required with --apply --no-dry-run when no --bucket is given",
    )
    parser.add_argument(
        "--include-stack-managed",
        action="store_true",
        help="Also change buckets that belong to a CloudFormation stack",
    )
    parser.add_argument(
        "--transition-days",
        type=int,
        default=DEFAULT_TRANSITION_DAYS,
        help=f"Days before transition (default: {DEFAULT_TRANSITION_DAYS})",
    )
    args = parser.parse_args(argv)
    if args.transition_days < 0:
        parser.error("--transition-days must be 0 or more")
    if args.apply and not args.dry_run and not args.bucket and not args.all_buckets:
        parser.error("--apply --no-dry-run needs --bucket NAME or --all-buckets")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        session = boto3.session.Session(profile_name=args.profile)
        clients = ClientCache(session)
        home_s3 = clients.s3(args.region)
        buckets = list_buckets(home_s3)
    except (ClientError, BotoCoreError) as exc:
        print(f"Could not list buckets: {exc}", file=sys.stderr)
        return 1

    if args.bucket:
        wanted = set(args.bucket)
        missing = wanted - {b["Name"] for b in buckets}
        for name in sorted(missing):
            print(f"Bucket not found in this account: {name}", file=sys.stderr)
        buckets = [b for b in buckets if b["Name"] in wanted]

    reports: list[BucketReport] = []
    for i, bucket in enumerate(sorted(buckets, key=lambda b: b["Name"]), start=1):
        print(f"[{i}/{len(buckets)}] {bucket['Name']}", file=sys.stderr)
        report, rules, min_size = audit_bucket(clients, home_s3, bucket)
        if args.apply:
            apply_transition(
                clients,
                report,
                rules,
                min_size,
                args.transition_days,
                args.dry_run,
                args.include_stack_managed,
            )
        reports.append(report)

    print()
    print_table(reports)
    write_csv(args.csv, reports)

    errors = [r for r in reports if r.error]
    print(f"\n{len(reports)} buckets audited, {len(errors)} with errors.")
    print(f"CSV written to {args.csv}")
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
