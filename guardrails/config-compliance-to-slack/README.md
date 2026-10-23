# Config compliance to Slack

Posts a Slack message when an S3 bucket or a CloudWatch Logs log group drifts out of the cost baseline. It replaces a periodic spreadsheet of non-compliant resources with a continuous check that names the resource the moment it is created or changed.

A message looks like this:

```
AWS Config: resource is NON_COMPLIANT
Resource: example-raw-data-bucket
Type: AWS::S3::Bucket
Account: <ACCOUNT_ID>
Region: eu-central-1
Rule: cost-s3-lifecycle-policy-present
Previous state: COMPLIANT
Console: https://eu-central-1.console.aws.amazon.com/config/home?region=eu-central-1#/rules/details?configRuleName=cost-s3-lifecycle-policy-present
```

## How it works

```
Config rule evaluates resource
  |  NON_COMPLIANT
  v
EventBridge rule (Config Rules Compliance Change, NON_COMPLIANT, these two rules only)
  |
  v
Lambda slack_notifier  ---reads--->  Secrets Manager (webhook URL)
  |                    ---posts--->  Slack incoming webhook
  v (delivery or function failures, after retries)
SQS dead-letter queue  --->  CloudWatch alarm
```

EventBridge retries delivery to Lambda for up to an hour. Once delivered, Lambda runs the function asynchronously and retries a failed run (for example Slack returning an error) before sending the event to the same dead-letter queue.

Two rules are deployed.

`cost-s3-lifecycle-policy-present` uses the AWS managed rule `S3_LIFECYCLE_POLICY_CHECK` with no parameters. A bucket is NON_COMPLIANT when it has no enabled lifecycle rule. It is triggered by configuration changes, so a new bucket is evaluated within minutes of being recorded. The managed rule can also check a specific transition storage class and day count (`targetTransitionStorageClass`, `targetTransitionDays`), but that would flag every dev bucket that correctly uses expiration only, so it is left out.

`cost-log-group-retention-set` is a custom Lambda rule. The managed rule `cw-loggroup-retention-period-check` was the obvious choice but does the opposite of what a cost check needs: per its documentation it marks a log group with "Never expire" retention as COMPLIANT, because it checks for a minimum retention (an audit requirement). The custom rule marks never-expire log groups NON_COMPLIANT, and optionally anything over `LogRetentionMaxDays`. It runs on a schedule and lists log groups itself, so it does not depend on AWS Config recording the `AWS::Logs::LogGroup` resource type.

There is no managed rule that checks for an Intelligent-Tiering transition. If you want that, add a `targetTransitionStorageClass: INTELLIGENT_TIERING` parameter to the S3 rule scoped to production buckets with `Scope.TagKey`, or deploy a second copy of it.

## Prerequisites

- AWS Config recording enabled in the account and region, including `AWS::S3::Bucket`. The S3 rule cannot evaluate anything otherwise.
- A Slack app with an incoming webhook for the target channel.
- AWS SAM CLI for packaging (the template uses `AWS::Serverless::Function`).

## Deploy

Store the webhook URL first. The value can be the bare URL or `{"webhook_url": "..."}`.

```bash
aws secretsmanager create-secret \
  --name cost-guardrails/slack-webhook \
  --secret-string 'https://hooks.slack.com/services/...'
```

Then build and deploy:

```bash
cd guardrails/config-compliance-to-slack
sam build
sam deploy --guided \
  --stack-name cost-guardrails \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides WebhookSecretArn=<secret ARN>
```

To also flag long retention, add `LogRetentionMaxDays=365`. To skip log groups that must be kept long (for example audit trails), add `LogRetentionExemptPrefixes=/audit/,/security/`.

## Test

Unit tests cover the message formatter and the retention rule's evaluation logic. They make no AWS calls.

```bash
pip install pytest
pytest -q guardrails/config-compliance-to-slack/tests
```

End to end, after deploying:

```bash
aws lambda invoke --function-name <NotifierFunctionName> \
  --payload fileb://tests/events/non_compliant_bucket.json out.json

aws configservice start-config-rules-evaluation \
  --config-rule-names cost-s3-lifecycle-policy-present cost-log-group-retention-set
```

## Limits and behaviour to know

- The first evaluation after deployment reports every existing non-compliant resource. In a large account that is a burst of messages. The notifier runs with a reserved concurrency of 1 to stay under Slack's webhook rate limit, and throttled events are retried. Reserving concurrency fails in accounts whose Lambda concurrency quota is still at the reduced new-account level. Remove `ReservedConcurrentExecutions` there. If the backlog is large, deploy with the EventBridge rule disabled, export the initial list with `aws configservice get-compliance-details-by-config-rule`, then enable the rule.
- A message is sent on a change to NON_COMPLIANT, not on every evaluation. A resource that stays non-compliant is not repeated. Use the Config console or the audit scripts in `audit/` for the standing list.
- Config is regional. Deploy the stack in each region you use, or use an aggregator and a central account for a single channel.
- Config charges per rule evaluation and per configuration item recorded. Check the AWS Config pricing page for the current rates before enabling in an account with many buckets that change often.
- Only resources Config can see are evaluated. Buckets in regions where Config is not recording are invisible to the S3 rule.
