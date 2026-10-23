# Deploying a stack update with change sets

A change set shows what CloudFormation will do to a running stack before it does it: which resources are modified, which are replaced, and which properties change. For bucket stacks this matters because a lifecycle or policy change applies to data that already exists. This page is the command sequence used with `templates/s3-bucket-baseline.yaml`, plus what to do when an update fails and the rollback fails too.

All commands take `--profile` and `--region` like any AWS CLI call. They are left out below for readability. Set them every time, because the profile decides which account you are changing.

## 1. Create the change set

```bash
STACK=analytics-raw-bucket
CS=update-$(date +%Y%m%d-%H%M%S)

aws cloudformation create-change-set \
  --stack-name "$STACK" \
  --change-set-name "$CS" \
  --template-body file://templates/s3-bucket-baseline.yaml \
  --parameters \
      ParameterKey=Environment,UsePreviousValue=true \
      ParameterKey=Application,UsePreviousValue=true \
      ParameterKey=BucketName,UsePreviousValue=true \
      ParameterKey=KmsKeyArn,UsePreviousValue=true \
      ParameterKey=ArchiveAccessDays,ParameterValue=90

aws cloudformation wait change-set-create-complete \
  --stack-name "$STACK" --change-set-name "$CS"
```

List every parameter the stack already has. A parameter you leave out does not keep its current value. CloudFormation uses the template default instead. In this template `Environment` has no default, so leaving it out makes the call fail, which is the intent. In a template where `Environment` defaults to `dev`, the same mistake would silently switch a production bucket to the 30-day expiration rule, and the change set would show it only as a modified `LifecycleConfiguration`. `UsePreviousValue=true` keeps the deployed value.

Add `--capabilities CAPABILITY_IAM` only if the template creates IAM resources. This one does not.

If the wait fails with "The submitted information didn't contain changes", the template and parameters match what is deployed. Delete the change set and check you edited the right file.

## 2. Read the change set

```bash
aws cloudformation describe-change-set \
  --stack-name "$STACK" --change-set-name "$CS" \
  --include-property-values \
  --query 'Changes[].ResourceChange.{Action:Action,Resource:LogicalResourceId,Replace:Replacement,Details:Details[].Target.Name}'
```

Check three things before executing:

- `Replacement` must be `False` for the bucket. A replacement of an `AWS::S3::Bucket` means a new, empty bucket. The template sets `DeletionPolicy: Retain` so the old bucket survives, but anything that points at the bucket name would break.
- The property names under `Details` should be only the ones you meant to change. An unexpected `LifecycleConfiguration` change usually means a parameter fell back to its default.
- With `--include-property-values`, each detail has a `BeforeValue` and `AfterValue`. For a lifecycle change, read the after value. An expiration rule that appears in a production bucket is the mistake to catch here.

## 3. Execute and wait

```bash
aws cloudformation execute-change-set --stack-name "$STACK" --change-set-name "$CS"
aws cloudformation wait stack-update-complete --stack-name "$STACK"
```

Executing one change set deletes the stack's other change sets, since they were computed against the old state.

If the wait command exits with an error, the update failed. Go to the next section.

## When an update fails

Start with the events, newest first:

```bash
aws cloudformation describe-stack-events --stack-name "$STACK" \
  --query 'StackEvents[?contains(ResourceStatus, `FAILED`)].[Timestamp,LogicalResourceId,ResourceStatus,ResourceStatusReason]' \
  --output table
```

The first `UPDATE_FAILED` event (the oldest of the failures) is the cause. Later failures are usually dependent resources that were cancelled.

### Seeing the template that was submitted

```bash
aws cloudformation get-template --stack-name "$STACK" --template-stage Processed \
  --query TemplateBody --output text > deployed.yaml

aws cloudformation get-template --stack-name "$STACK" --change-set-name "$CS" \
  --template-stage Original --query TemplateBody --output text > submitted.yaml
```

`--template-stage` takes `Original` or `Processed`, with that exact capitalisation. `PROCESSED` fails validation. `Original` is the template as submitted, and `Processed` is the template after transforms such as `AWS::Serverless-2016-10-31` were expanded. For a template without transforms the two are the same, and `Processed` is the default.

The second command reads the template stored with the change set, which is exactly what you tried to deploy, as long as the change set still exists. Which template the stack itself reports after a failed update depends on how far the rollback got, so diff both files against your local copy instead of assuming.

### When the rollback fails as well

A failed update normally rolls back to the previous state. If the rollback also fails, the stack is left in `UPDATE_ROLLBACK_FAILED` and refuses new change sets:

```
Stack ... is in UPDATE_ROLLBACK_FAILED state and can not be updated.
```

A common way to get here with bucket stacks: the old bucket policy named an IAM user or role in the `Principal` element, and that user or role has since been deleted. The update replaces the policy, something else fails, and the rollback tries to put the old policy back. S3 validates every ARN in `Principal` when a policy is written, so the rollback fails with `Invalid principal in policy`. Hard-coded account IDs in principals cause the same error when a template is deployed to a different account.

First fix what you can outside CloudFormation (for example recreate a deleted role if it should exist), then retry the rollback:

```bash
aws cloudformation continue-update-rollback --stack-name "$STACK"
```

If the resource cannot be rolled back, skip it:

```bash
aws cloudformation continue-update-rollback --stack-name "$STACK" \
  --resources-to-skip BucketPolicy
```

Only resources whose rollback failed can be skipped. CloudFormation marks them `UPDATE_COMPLETE` without touching them, so afterwards the stack's record of those resources no longer matches reality. AWS warns that the next update may fail, and the stack may become unrecoverable, unless you bring the two back in line first. In practice: look at the real resource (`aws s3api get-bucket-policy`), make the template describe what is there or what you want, and deploy that as the next change set. Skip the fewest resources you can, because dependent resources often succeed once the root one is skipped.

Do not delete the stack to get out of this state. `delete-stack` removes the stack, and `--retain-resources` only applies to resources that failed to delete. It is not a way to reset a stuck update.

### Avoiding it next time

- Refer to roles that may come and go with `aws:PrincipalArn` in a `Condition` instead of naming them in `Principal`. Condition values are plain strings, so a deleted role does not make the policy invalid. The baseline template does this.
- Never hard-code an account ID or region in an ARN. Use `${AWS::AccountId}`, `${AWS::Region}` and `${AWS::Partition}`.
- Make one kind of change per change set: policy changes separately from lifecycle changes. A smaller update has a smaller rollback.
