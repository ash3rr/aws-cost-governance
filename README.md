# aws-cost-governance

Scripts, templates and short write-ups for finding and fixing the AWS storage and logging costs that grow quietly: S3 buckets that keep every object and every old version forever, and CloudWatch Logs log groups that never expire. It has three parts:

- Audit scripts that list what is wrong today, as a console table and a CSV.
- Guard rails that catch new drift as it happens, with AWS Config rules that post to Slack, and a bucket template that is compliant from the start.
- Write-ups of cost decisions that needed a closer look than the first answer gave them.

The guard rails exist because a one-off audit goes stale. A spreadsheet of non-compliant buckets, followed by asking each team to fix theirs, is out of date the next time someone deploys a bucket. A Config rule names the bucket within minutes of it being created, and the template gives teams a correct default to copy.

## Contents

| Path | What it is |
|---|---|
| [audit/s3_storage_class_audit.py](audit/s3_storage_class_audit.py) | Every bucket: Intelligent-Tiering transition, archive tier configuration, expiration, versioning, noncurrent version expiration. Optional `--apply` (dry run by default) adds the missing transition. |
| [audit/cloudwatch_log_groups_audit.py](audit/cloudwatch_log_groups_audit.py) | Every log group: class, retention, stored bytes. Flags never-expire groups, largest first. Read-only. |
| [guardrails/config-compliance-to-slack/](guardrails/config-compliance-to-slack/) | SAM template: Config rules for S3 lifecycle and log retention, EventBridge on NON_COMPLIANT, Lambda that posts a plain-text Slack message. Unit tested. |
| [templates/s3-bucket-baseline.yaml](templates/s3-bucket-baseline.yaml) | CloudFormation bucket with per-environment lifecycle, versioning cleanup, public access block, TLS and KMS enforcement, and admin-only bucket changes. |
| [docs/deploying-with-change-sets.md](docs/deploying-with-change-sets.md) | Updating a bucket stack with change sets, reading a failed update, and recovering from `UPDATE_ROLLBACK_FAILED`. |
| [docs/glue-vs-s3-batch-compaction.md](docs/glue-vs-s3-batch-compaction.md) | Whether S3 Batch Operations can replace Glue small-file compaction (not by itself), and how to compare costs properly. |
| [docs/guardduty-s3-protection-costs.md](docs/guardduty-s3-protection-costs.md) | Why GuardDuty S3 Protection is expensive in pipeline accounts, and the options. |

## S3: what Intelligent-Tiering settings actually do

Two different S3 settings have "Intelligent-Tiering" in the name, and mixing them up gives a bucket that looks configured but saves nothing.

1. The storage class. S3 Intelligent-Tiering (`INTELLIGENT_TIERING`) is a storage class like `STANDARD` or `GLACIER`. Objects in it are moved automatically between a Frequent Access tier, an Infrequent Access tier (after 30 days without access) and an Archive Instant Access tier (after 90 days), with no retrieval charges. Objects get into the class in one of two ways: a lifecycle rule with a transition to `INTELLIGENT_TIERING`, or being written with `--storage-class INTELLIGENT_TIERING` (the `x-amz-storage-class` header on `PutObject`, `CopyObject` or `CreateMultipartUpload`).

2. The bucket's Intelligent-Tiering configuration. `PutBucketIntelligentTieringConfiguration` (CloudFormation `IntelligentTieringConfigurations`) turns on two optional, asynchronous tiers, Archive Access and Deep Archive Access, for objects that are already in the storage class. AWS's API reference says you only need it "if you want to automatically move objects" to those archive tiers. It does not move a single `STANDARD` object anywhere.

So a bucket with an Intelligent-Tiering configuration and no lifecycle transition keeps all its objects in `STANDARD`. The audit script reports both settings separately and flags exactly this case.

The archive tiers have minimums, per the S3 user guide: Archive Access after at least 90 consecutive days without access, Deep Archive Access after at least 180, and a maximum of 730 for both. A configuration with Archive Access after 1 day is rejected. Objects in these two tiers must be restored before they can be read, which takes minutes to hours, so turn them on only for data nobody reads directly. The template leaves them off by default.

Costs to know before transitioning everything:

- Monitoring fee. Intelligent-Tiering charges a monthly monitoring and automation fee per object (see the [S3 pricing page](https://aws.amazon.com/s3/pricing/) for your region). For buckets with many small objects this can exceed the saving.
- 128 KB minimum. Objects under 128 KB are not monitored and never move out of the Frequent Access tier. They also do not pay the monitoring fee. Lifecycle configurations created or changed since September 2024 do not transition objects under 128 KB to any storage class by default, so they stay in `STANDARD` (older, unmodified configurations keep the previous behaviour). Buckets full of tiny files gain nothing from the transition. Compacting the files is the fix (see the Glue write-up).
- Transition requests. Each lifecycle transition is a billed request per object. A one-off transition of a bucket with hundreds of millions of objects has a visible cost.
- Short-lived data. Intelligent-Tiering saves money only on objects that sit unread for 30 days or more. A bucket that expires objects after 30 days should just expire them, which is what the template does for non-production environments. The audit script's `--apply` skips such buckets.

## CloudWatch Logs: retention is the win, not the log class

Retention decides how long CloudWatch Logs stores data. A log group created by Lambda, ECS or another service without a retention setting keeps every event forever ("Never expire"), and storage keeps growing. Retention can be changed on an existing log group at any time with `PutRetentionPolicy`, affects nothing that writes to the log group, and takes effect for events already stored. It is the safe, bulk change:

```bash
aws logs put-retention-policy --log-group-name <name> --retention-in-days 30
```

Valid values are 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288 and 3653 days. Check with the owners before shortening retention on anything that might be needed for audit or incident response.

The Infrequent Access log class is narrower than it sounds. Per the CloudWatch Logs documentation (log classes page, checked 2026-10-23):

- "After a log group is created, its log class can't be changed." Moving an existing log group to Infrequent Access means creating a new log group and pointing the writer at it. The old one keeps its data until it expires or is deleted.
- "The Standard and Infrequent Access log classes differ in ingestion costs only. Storage charges and CloudWatch Logs Insights charges are the same in each log class." Infrequent Access does nothing for data that is already stored.
- Infrequent Access does not support subscription filters, metric filters, Live Tail, anomaly detection, field indexing, facets, natural language query assist, Container Insights and Lambda Insights ingestion, embedded metric format, or the `GetLogEvents` and `FilterLogEvents` API operations. Events can only be read with Logs Insights, which supports most commands.

So the log class is a choice to make when creating a high-volume log group that nothing reads except occasional Logs Insights queries. It is not something to "enforce" on existing log groups. The audit script reports the class for information and flags retention.

## Prerequisites

- Python 3.10 or later and `pip install -r requirements.txt` (boto3).
- AWS credentials for the target account, for example `aws sso login --profile <name>` and `--profile <name>` on each script.
- For the guard rail: AWS SAM CLI, and AWS Config recording turned on in each region you deploy to.

## IAM permissions

The audits are read-only. A role with these actions is enough:

| Script | Actions |
|---|---|
| `s3_storage_class_audit.py` | `s3:ListAllMyBuckets`, `s3:GetBucketLocation`, `s3:GetLifecycleConfiguration`, `s3:GetIntelligentTieringConfiguration`, `s3:GetBucketVersioning`, `s3:GetBucketTagging` |
| `s3_storage_class_audit.py --apply --no-dry-run` | the above plus `s3:PutLifecycleConfiguration` |
| `cloudwatch_log_groups_audit.py` | `logs:DescribeLogGroups`, plus `ec2:DescribeRegions` for `--all-regions` |

The AWS managed policy `ReadOnlyAccess` covers both audits.

A bucket policy can deny the audit role even when IAM allows it. Those buckets show an `AccessDenied` error in the report and the script carries on. `s3:GetBucketTagging` is only used to tell which buckets belong to a CloudFormation stack, so a denial there is not reported as an error.

The guard rail stack needs permission to create Config rules, Lambda functions, IAM roles, EventBridge rules, an SQS queue and a CloudWatch alarm. It reads the Slack webhook from a Secrets Manager secret you create first.

## How to run

```bash
pip install -r requirements.txt

# S3, every bucket in the account
python audit/s3_storage_class_audit.py --profile prod-readonly --csv s3_audit.csv

# Plan the missing transitions without changing anything
python audit/s3_storage_class_audit.py --profile prod-admin --apply

# Apply to named buckets only
python audit/s3_storage_class_audit.py --profile prod-admin --apply --no-dry-run \
  --bucket analytics-archive --bucket exports-history

# CloudWatch Logs, one region or all of them
python audit/cloudwatch_log_groups_audit.py --profile prod-readonly --region eu-west-1
python audit/cloudwatch_log_groups_audit.py --profile prod-readonly --all-regions --top 25
```

Exit codes: 0 when the audit completed, 2 when it completed but some buckets or regions could not be read, 1 when it could not start.

`--apply` writes only when `--no-dry-run` is given, and then only with `--bucket` or an explicit `--all-buckets`. It merges the new transition rule with the bucket's existing lifecycle rules (`PutBucketLifecycleConfiguration` replaces the whole configuration) and keeps the bucket's small-object transition setting. It skips buckets that already transition to Intelligent-Tiering, buckets whose objects expire within 30 days, and buckets that belong to a CloudFormation stack. Change those in their templates instead, or the next stack update will remove the rule and the stack will show drift. `--include-stack-managed` overrides that if you accept the drift.

The guard rail has its own [README](guardrails/config-compliance-to-slack/README.md).

## Tests and checks

```bash
pip install -r requirements-dev.txt
pytest -q
cfn-lint templates/*.yaml guardrails/config-compliance-to-slack/template.yaml
```

Tests cover the audit scripts' analysis logic, the Slack message formatter and the log retention rule. None of them call AWS.

## Limitations

- The S3 audit covers general purpose buckets in the account the credentials belong to. Directory buckets (S3 Express One Zone) do not support these settings and are not listed by `ListBuckets`.
- Lifecycle rules filtered by prefix, tag or object size are reported with their scope, but the audit does not work out which share of the bucket's objects they cover. A bucket with a transition on `logs/` only is reported as having a transition.
- The audit reports configuration, not cost. Use S3 Storage Lens or Cost Explorer for the bytes and requests behind each bucket before prioritising.
- Stored bytes for log groups come from `DescribeLogGroups` and can lag behind actual storage.
- The Config rules run in one region per deployment. Use a Config aggregator for an organization-wide view.
