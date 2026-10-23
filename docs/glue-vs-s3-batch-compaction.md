# Can S3 Batch Operations replace Glue jobs that compact small files?

Short answer: not by itself. S3 Batch Operations runs one operation per object. It has no operation that merges objects, so any compaction it does is really a Lambda function that you write, invoked by Batch Operations. Whether that is cheaper than Glue depends on numbers you can only get from your own job runs. This page explains why, shows how to make the comparison properly, and lists alternatives that remove the small files in the first place.

## Background

Compaction means rewriting many small objects as fewer large ones. Pipelines that land data in micro-batches (a file per event, per minute or per partition per run) produce thousands of objects of a few KB each. Small files make every later step slower and more expensive: each object is a separate request for Athena, Spark or Snowflake to open, S3 request charges are per request, and S3 Intelligent-Tiering does not tier objects under 128 KB. A common fix is a scheduled Glue Spark job that reads a partition and writes it back as a handful of larger Parquet files.

The question was whether S3 Batch Operations, which is billed per job and per object rather than per compute hour, could do this more cheaply.

## What S3 Batch Operations actually does

From the S3 documentation, Batch Operations "can perform a single operation on lists of Amazon S3 objects that you specify". The supported operations are:

- Copy objects
- Compute checksums
- Delete all object tags, replace all object tags
- Replace access control list
- Restore objects (from Glacier storage classes)
- Update object encryption
- Batch Replication of existing objects
- Object Lock retention and legal hold
- Invoke an AWS Lambda function

None of these combine objects. Copy writes each source object to one destination object. So for compaction the only option is the Lambda operation, where Batch Operations calls your function with a task per manifest entry and your function does the work.

That changes the comparison. You are no longer comparing Glue with Batch Operations. You are comparing Glue with Lambda plus Batch Operations, where Batch Operations is a scheduler and progress tracker around Lambda.

## What a Lambda compactor would have to handle

Each task in a Batch Operations job is one manifest entry, and the documented pattern is one key per task. To merge files, the manifest entry has to describe a group of objects rather than one. The documentation shows how to pack extra data into the `key` field (URL-encoded JSON), but that field is limited to 1,024 characters, so a group of keys usually has to be written somewhere else and referenced. In other words you build the grouping logic, the manifest, and the merge yourself.

Lambda limits that matter (from the Lambda quotas page):

- 15 minutes maximum per invocation.
- 128 MB to 10,240 MB of memory, with CPU allocated in proportion.
- `/tmp` storage between 512 MB and 10,240 MB.
- 625 Mbps network bandwidth per execution environment by default.
- 6 MB request and response payload for synchronous invocation.

A partition whose input does not fit in memory or in `/tmp`, or cannot be read and rewritten inside 15 minutes, has to be split into smaller groups, which brings back some of the small-file problem. Merging Parquet also means decoding and re-encoding it (a library such as pyarrow in a Lambda layer or container image), handling schema differences between files, and deleting the source objects only after the new file is written and verified. Glue's Spark runtime already handles those parts.

Byte-level concatenation without downloading is possible for row formats with S3 multipart upload (`UploadPartCopy`), but every part except the last must be at least 5 MiB, which is exactly what small files are not. It also does not work for Parquet, whose footer describes the whole file.

## Why the original comparison did not hold

The first analysis of this ticket reported 80 to 95 percent savings for Batch Operations. It was built on an assumed workload (100,000 files, 30 minutes, 10 DPUs, daily) rather than on a real job run, and it priced Batch Operations at its per-job and per-object fee only. That leaves out:

- the Lambda compute that would do the actual merging,
- the S3 GET and PUT requests the Lambda makes, which Glue also makes but which were counted on one side only,
- writing and maintaining the grouping, manifest generation and merge code.

The comparison put a whole Spark job on one side and a task scheduler on the other, so the result says nothing about which approach is cheaper.

## How to compare properly

Use one real pipeline and a representative period (a few weeks of runs, so daily variation evens out).

### 1. Measure what Glue costs today

Pull the job's recent runs:

```bash
aws glue get-job-runs --job-name <job> --max-items 200 \
  --query 'JobRuns[].{start:StartedOn,state:JobRunState,secs:ExecutionTime,workers:NumberOfWorkers,type:WorkerType,dpuSecs:DPUSeconds,class:ExecutionClass,glue:GlueVersion}'
```

- For jobs with Auto Scaling or the Flex execution class, `DPUSeconds` is the billed figure. Divide by 3,600 for DPU-hours.
- Otherwise, DPU-hours = `ExecutionTime` / 3,600 x workers x DPU per worker (1 for G.1X, 2 for G.2X, 0.25 for G.025X, and so on).
- Multiply by the DPU-hour rate for your region and execution class from the Glue pricing page.
- Cross-check against Cost Explorer filtered to the Glue service and grouped by usage type, or the Cost and Usage Report filtered by the job's tags if jobs are tagged.

Also record the input: number of objects and total bytes per run (from the job's own logging, S3 Inventory, or `aws s3 ls --recursive --summarize` on one partition).

### 2. Estimate the Lambda plus Batch Operations alternative

For the same runs:

- Batch Operations fee: jobs per period x per-job fee, plus tasks x per-task fee. A task here is a group of input files, not an input file.
- Lambda: number of invocations x average duration x memory (GB-seconds), times the Lambda rate for your architecture, plus the per-request fee. Get duration and memory from a prototype run on one real partition, not from a guess. The merge time is dominated by reading and writing the data, so measure it with realistic file sizes.
- S3 requests: GETs for every input object and PUTs for every output object, and DELETEs if the sources are removed. These apply to Glue too, so either count them on both sides or on neither.
- Engineering cost: the Glue job exists. The Lambda version has to be written, tested, monitored and maintained.

### 3. Decide

Compare cost per run and cost per GB compacted. If the difference is small, the existing Glue job wins on maintenance. If the Glue job is mostly idle startup time on tiny inputs, the cheaper fix is usually inside Glue (below), not a rewrite.

### Prices used

This page avoids fixed numbers because rates change and vary by region. As checked on 2026-10-23, the Glue pricing page listed $0.44 per DPU-hour for standard ETL jobs and $0.29 per DPU-hour for Flex, and the S3 pricing page gave a US East (N. Virginia) example of $0.25 per Batch Operations job and $1.00 per million objects processed. Look up your own region on [AWS Glue pricing](https://aws.amazon.com/glue/pricing/), [Amazon S3 pricing](https://aws.amazon.com/s3/pricing/) and [AWS Lambda pricing](https://aws.amazon.com/lambda/pricing/) before using any of these.

## Cheaper options to check first

These attack the cause (too many small writes) or the cost of the existing job, and are usually less work than a new compaction service.

- Write fewer, larger files upstream. If the producer controls its output, batch records in memory or on local disk and flush on size or time. Fewer PUTs also means fewer S3 data events, which matters if GuardDuty S3 Protection is on (see [guardduty-s3-protection-costs.md](guardduty-s3-protection-costs.md)).
- Amazon Data Firehose buffering. Firehose buffers records by size and interval before writing to S3, and can convert to Parquet on the way. Raising the buffer size and interval directly reduces the object count.
- Apache Iceberg tables. Iceberg tracks files in metadata and supports compaction as a table operation. AWS Glue Data Catalog can run automatic compaction for Iceberg tables, and Athena supports `OPTIMIZE ... REWRITE DATA`. This replaces a custom compaction job with a managed one.
- Athena CTAS or `INSERT INTO`. For periodic compaction of a partition, a `CREATE TABLE AS SELECT` that writes Parquet with a bucketing or file-count setting is billed per data scanned and needs no cluster.
- Tune the Glue job. Check whether the job is billed for far more workers than it uses (enable Auto Scaling), whether it can run on the Flex execution class (non-urgent jobs at a lower rate), whether a smaller worker type is enough, and whether it compacts the same partitions repeatedly. Use job bookmarks or partition predicates so each run touches only new data.

## Summary for the ticket

- S3 Batch Operations cannot compact files on its own. Compaction would be custom Lambda code that Batch Operations invokes.
- The earlier savings estimate compared a full Spark job with the Batch Operations scheduling fee and is not usable.
- To answer the cost question, measure DPU-hours from real Glue runs, prototype the Lambda merge on one real partition, and compare cost per GB including S3 requests and maintenance.
- Before building anything, check whether larger upstream writes, Firehose buffering, Iceberg compaction or Glue tuning removes the need.
