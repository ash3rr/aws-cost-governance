# GuardDuty S3 Protection costs in data pipeline accounts

GuardDuty S3 Protection watches object-level S3 activity for signs of data exfiltration or destruction. It is billed on how many S3 data events it analyses, so in an account where pipelines read and write millions of objects a day, it can become one of the larger security line items while mostly analysing the pipelines' own traffic. This page covers how to find out where the cost comes from, what can and cannot be changed, and what you give up.

## How it is billed

From the GuardDuty documentation:

- S3 Protection monitors CloudTrail data events for S3, the object-level API calls such as `GetObject`, `PutObject`, `ListObjects` and `DeleteObject`, in all buckets in the account and region where it is enabled.
- You do not need to turn on CloudTrail S3 data event logging yourself, and you do not pay for a trail to use it. GuardDuty reads the events from its own feed.
- Requests made with IAM or STS credentials are processed. Unauthenticated (anonymous) requests are not.
- The charge is per million events analysed, with lower rates at higher volumes. See the [GuardDuty pricing page](https://aws.amazon.com/guardduty/pricing/) for the current tiers in your region.

So the bill follows request count, not data volume and not findings. A pipeline that writes one 1 GB file generates one `PutObject` event. A pipeline that writes the same data as 100,000 small files generates 100,000. Suppression rules do not change the bill either, because they only archive findings after the events have been analysed.

## Find out where the events come from

Before deciding anything, check which accounts and buckets drive the volume.

In the GuardDuty console, the Usage page shows cost by account, by protection plan (data source) and by resource. From the CLI, in the account that holds the detector (the delegated administrator for an organization):

```bash
DETECTOR=$(aws guardduty list-detectors --query 'DetectorIds[0]' --output text)

# Cost of S3 Protection per account
aws guardduty get-usage-statistics --detector-id "$DETECTOR" \
  --usage-statistic-type TOP_ACCOUNTS_BY_FEATURE \
  --usage-criteria '{"Features": ["S3_DATA_EVENTS"]}' --unit USD

# Top resources by usage for S3 data events
aws guardduty get-usage-statistics --detector-id "$DETECTOR" \
  --usage-statistic-type TOP_RESOURCES \
  --usage-criteria '{"Features": ["S3_DATA_EVENTS"]}' --unit USD
```

The API reports usage over the last 30 days, and `TOP_RESOURCES` returns the top 50. The console projects the same usage to a monthly estimate, so its numbers can differ. If the top resources are pipeline buckets (landing, staging, processing), the cost is driven by pipeline design rather than by user activity.

To split reads from writes, query S3 server access logs or a CloudTrail Lake event data store with S3 data events for a sample day, grouped by `eventName` and bucket. That tells you whether fewer, larger writes would help or whether the volume is downstream reads.

## Options

### Reduce the number of requests

This keeps full coverage and lowers every request-based charge, not only GuardDuty.

- Write fewer, larger objects. Buffer records and flush on size or time instead of one object per record or per micro-batch. Amazon Data Firehose does this with its buffer settings.
- Compact small files early, so downstream jobs read a few large objects instead of many small ones. See [glue-vs-s3-batch-compaction.md](glue-vs-s3-batch-compaction.md).
- Look for wasteful patterns: `HeadObject` before every `GetObject`, repeated listing of the same prefix, or jobs that re-read a whole partition when only new files changed.

Multipart upload does not help on its own: each part is a separate request. Batching helps only when it reduces the number of logical objects.

### Scope by account

S3 Protection is enabled per account and per region. In an organization, the GuardDuty delegated administrator can disable it for selected member accounts while leaving it on everywhere else:

```bash
aws guardduty update-member-detectors --detector-id "$DETECTOR" \
  --account-ids <pipeline account ID> \
  --features '[{"Name": "S3_DATA_EVENTS", "Status": "DISABLED"}]'
```

Check the organization's auto-enable setting (`update-organization-configuration`, `S3_DATA_EVENTS` set to `ALL`, `NEW` or `NONE`) so the setting is not re-applied to the account.

This works best when the account structure follows data sensitivity: heavy processing in one account, curated or sensitive data served from another where S3 Protection stays on. If both live in the same account, splitting them is the larger but cleaner fix.

### Exclude specific buckets

As of October 2026, the S3 Protection documentation describes no way to exclude individual buckets: once enabled in an account and region it analyses events from all buckets there. GuardDuty Malware Protection for S3, which is enabled per bucket, is a different feature (it scans newly uploaded objects for malware and is billed per GB scanned and per object) and does not replace S3 Protection. Check the current documentation before planning around this, as it may change.

## What turning it off costs you

S3 Protection is the GuardDuty feature that detects unusual access to S3 data: access from unusual locations or anonymising networks, credentials used in an unusual way against buckets, bulk reads or deletes that do not fit the normal pattern, and changes that make data reachable from outside. Pipeline accounts often hold the most complete copy of the data, so they are not automatically low risk.

If you disable it for an account, write down the decision and the compensating controls, and have the owner of security risk sign it off. Compensating controls that reduce the chance of a leak, rather than detect it:

- Bucket policies that allow access only from known roles (`aws:PrincipalArn`), only through VPC endpoints (`aws:SourceVpce`), and deny non-TLS requests.
- VPC endpoint policies that only allow your organization's buckets, so a compromised workload cannot copy data to an outside bucket.
- Block Public Access at the account level.
- IAM Access Analyzer for external access findings.
- S3 server access logs or a CloudTrail data event trail limited to sensitive prefixes, queried with Athena on a schedule. This is detection you maintain yourself, so budget the time.

## Decision checklist

1. Which accounts and buckets make up the S3 Protection cost (usage statistics above)?
2. Is the volume mostly writes or reads, and can the pipeline produce fewer objects?
3. Does the account hold data that would be reportable if leaked?
4. Are access paths already restricted to known roles and VPC endpoints?
5. If disabling, who accepts the risk, and which compensating controls are in place before the change?
