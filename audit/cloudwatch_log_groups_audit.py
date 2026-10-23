#!/usr/bin/env python3
"""Audit CloudWatch Logs log groups for retention and log class.

For every log group in the chosen regions the script reports the log class
(STANDARD, INFREQUENT_ACCESS or DELIVERY), the retention setting, stored bytes,
metric filter count and age. Log groups that never expire are flagged, largest
first, because retention is the setting that controls how much is stored.

The script is read-only. Retention can be changed on an existing log group with
PutRetentionPolicy. The log class cannot: AWS documents that a log group's
class can't be changed after it is created, and Infrequent Access only lowers
the ingestion price, not storage. See the README for the full reasoning.

Exit codes: 0 audit complete, 1 fatal error, 2 audit complete but one or more
regions could not be read.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_CSV_PATH = "cloudwatch_log_groups_audit.csv"

# Retention above this many days is flagged as long. Never-expire is always flagged.
LONG_RETENTION_DAYS = 365

# How many rows the console table shows. The CSV always has every log group.
DEFAULT_TOP = 50

BOTO_CONFIG = Config(retries={"max_attempts": 10, "mode": "adaptive"})

CSV_COLUMNS = [
    "region",
    "log_group",
    "log_class",
    "retention_days",
    "stored_bytes",
    "stored_gib",
    "metric_filters",
    "created",
    "age_days",
    "finding",
]


@dataclass
class LogGroupRow:
    region: str
    log_group: str
    log_class: str
    retention_days: int | None
    stored_bytes: int
    metric_filters: int
    created: datetime | None

    @property
    def never_expires(self) -> bool:
        return self.retention_days is None

    @property
    def age_days(self) -> int | None:
        if self.created is None:
            return None
        return (datetime.now(timezone.utc) - self.created).days

    def finding(self, long_retention_days: int = LONG_RETENTION_DAYS) -> str:
        if self.never_expires:
            return "never expires"
        if self.retention_days > long_retention_days:
            return f"retention over {long_retention_days} days"
        return ""

    def as_row(self, long_retention_days: int = LONG_RETENTION_DAYS) -> dict:
        return {
            "region": self.region,
            "log_group": self.log_group,
            "log_class": self.log_class,
            "retention_days": "never" if self.never_expires else self.retention_days,
            "stored_bytes": self.stored_bytes,
            "stored_gib": f"{self.stored_bytes / 1024**3:.3f}",
            "metric_filters": self.metric_filters,
            "created": self.created.date().isoformat() if self.created else "",
            "age_days": "" if self.age_days is None else self.age_days,
            "finding": self.finding(long_retention_days),
        }


def to_row(region: str, group: dict) -> LogGroupRow:
    """Convert one DescribeLogGroups entry into a row."""
    created_ms = group.get("creationTime")
    return LogGroupRow(
        region=region,
        log_group=group["logGroupName"],
        # Older log groups may omit the field. They are STANDARD.
        log_class=group.get("logGroupClass", "STANDARD"),
        retention_days=group.get("retentionInDays"),
        stored_bytes=int(group.get("storedBytes", 0)),
        metric_filters=int(group.get("metricFilterCount", 0)),
        created=(
            datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
            if created_ms
            else None
        ),
    )


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def list_log_groups(logs, region: str) -> list[LogGroupRow]:
    rows = []
    for page in logs.get_paginator("describe_log_groups").paginate():
        rows.extend(to_row(region, g) for g in page.get("logGroups", []))
    return rows


def enabled_regions(session: boto3.session.Session, seed_region: str) -> list[str]:
    ec2 = session.client("ec2", region_name=seed_region, config=BOTO_CONFIG)
    resp = ec2.describe_regions(AllRegions=False)
    return sorted(r["RegionName"] for r in resp["Regions"])


def print_summary(rows: list[LogGroupRow], top: int) -> None:
    total = sum(r.stored_bytes for r in rows)
    never = [r for r in rows if r.never_expires]
    never_bytes = sum(r.stored_bytes for r in never)
    by_class: dict[str, int] = {}
    for r in rows:
        by_class[r.log_class] = by_class.get(r.log_class, 0) + 1

    print(f"Log groups: {len(rows)}")
    print("By class: " + ", ".join(f"{k}={v}" for k, v in sorted(by_class.items())))
    print(f"Stored: {human_bytes(total)}")
    share = (never_bytes / total * 100) if total else 0.0
    print(
        f"Never expire: {len(never)} log groups holding "
        f"{human_bytes(never_bytes)} ({share:.0f}% of stored bytes)"
    )


def print_table(rows: list[LogGroupRow], top: int) -> None:
    flagged = sorted(
        (r for r in rows if r.finding()), key=lambda r: r.stored_bytes, reverse=True
    )[:top]
    if not flagged:
        print("\nNo log groups flagged.")
        return
    print(f"\nTop {len(flagged)} flagged log groups by stored bytes:")
    headers = ["Region", "Log group", "Class", "Retention", "Stored", "Finding"]
    cells = [
        [
            r.region,
            r.log_group if len(r.log_group) <= 60 else r.log_group[:57] + "...",
            r.log_class,
            "never" if r.never_expires else str(r.retention_days),
            human_bytes(r.stored_bytes),
            r.finding(),
        ]
        for r in flagged
    ]
    widths = [max([len(h)] + [len(c[i]) for c in cells]) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for c in cells:
        print("  ".join(v.ljust(w) for v, w in zip(c, widths)))


def write_csv(path: str, rows: list[LogGroupRow], long_retention_days: int) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for r in sorted(rows, key=lambda r: r.stored_bytes, reverse=True):
            writer.writerow(r.as_row(long_retention_days))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit CloudWatch Logs log groups for retention and log class."
    )
    parser.add_argument("--profile", help="AWS named profile to use")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--region",
        action="append",
        default=[],
        help="Region to audit. Repeat for several. Default: the profile's region.",
    )
    group.add_argument(
        "--all-regions",
        action="store_true",
        help="Audit every region enabled for the account",
    )
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help="CSV output path")
    parser.add_argument(
        "--top", type=int, default=DEFAULT_TOP, help="Rows to show in the console table"
    )
    parser.add_argument(
        "--long-retention-days",
        type=int,
        default=LONG_RETENTION_DAYS,
        help=f"Flag retention above this (default: {LONG_RETENTION_DAYS})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        session = boto3.session.Session(profile_name=args.profile)
        if args.all_regions:
            regions = enabled_regions(session, session.region_name or "us-east-1")
        elif args.region:
            regions = args.region
        elif session.region_name:
            regions = [session.region_name]
        else:
            print("No region configured. Pass --region or --all-regions.", file=sys.stderr)
            return 1
    except (ClientError, BotoCoreError) as exc:
        print(f"Could not start: {exc}", file=sys.stderr)
        return 1

    rows: list[LogGroupRow] = []
    failed: list[str] = []
    for region in regions:
        print(f"Reading log groups in {region}", file=sys.stderr)
        try:
            logs = session.client("logs", region_name=region, config=BOTO_CONFIG)
            rows.extend(list_log_groups(logs, region))
        except (ClientError, BotoCoreError) as exc:
            print(f"  {region}: {exc}", file=sys.stderr)
            failed.append(region)

    print()
    print_summary(rows, args.top)
    print_table(rows, args.top)
    write_csv(args.csv, rows, args.long_retention_days)
    print(f"\nCSV written to {args.csv}")
    if failed:
        print(f"Regions that could not be read: {', '.join(failed)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
