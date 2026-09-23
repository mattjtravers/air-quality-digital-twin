---
parent: high-level-design
prefix: INFRA
---

# Infrastructure

## Context and Design Philosophy

Every AWS resource the twin depends on is declared in a template in this repository and deployed
from it. This component owns those templates, the dispatch Lambda that turns a schedule firing
into a pipeline run, and the tooling that deploys them. It owns no data and no pipeline logic:
the observation store owns what goes in the archive, the pipeline owns what a run does and over
what window, and this component owns the resources they need and how those resources come into
being.

Three principles:

1. **Stacks are split by lifetime, not by service.** Resources that must outlive every redeploy
   sit in one stack; resources expected to be torn down and recreated sit in another. A component
   under active development must never share a stack with the system of record.
2. **The dispatcher is a translator, not a runner.** The Lambda turns one schedule firing into one
   GitHub `workflow_dispatch` call. It never reads or writes the archive, never imports the
   pipeline package, and holds no window logic — the run it starts resolves its own window, as it
   would for any other trigger.
3. **Secrets are referenced, never declared.** A template names a secret and grants access to it.
   The value is set out of band, so no credential is ever expressed in a file in this repository.

## Stacks

Two CloudFormation stacks, both deployed with AWS SAM.

| Stack | Name | Lifetime | Holds |
|---|---|---|---|
| Foundation | `aqdt-foundation` | Durable; outlives every other stack | The GeoParquet archive bucket and the IAM role GitHub Actions runners assume |
| Dispatch | `aqdt-dispatch` | Disposable; torn down and redeployed freely | Four EventBridge schedules, the dispatch Lambda, its execution role and log group |

The archive is the system of record (HLD § Persistence). Deleting the dispatch stack must cost
nothing, and it would not be safe to make that true if the bucket shared the stack.

### Foundation stack

**Archive bucket.** `air-quality-digital-twin-585949919812-us-east-1-archive`, named explicitly in
the template rather than generated, so that `AQDT_ARCHIVE_URI` stays a value set once in the
repository variable and the Codespaces secret with nothing reading stack outputs. The
account-and-region suffix is what makes a globally unique S3 name.

- `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`, so neither deleting the stack nor a
  template edit that would replace the bucket can take the archive with it.
- Public access blocked in full; bucket-owner-enforced object ownership.
- Versioning enabled. PurpleAir history cannot be re-fetched — the `/sensors` endpoint returns
  only the latest reading — so a partition damaged by a bad write is not recoverable from
  upstream, and the archive's read-merge-write rewrites a whole daily partition on every run.
- One lifecycle configuration bounds what versioning accumulates, with three rules: expire
  noncurrent versions after 30 days; expire delete markers once no version remains behind them;
  abort incomplete multipart uploads after 7 days. A PurpleAir partition is rewritten ~96 times a
  day, so noncurrent versions and their delete markers are the volume that matters, and neither
  expires on its own.

**Outputs.** The stack exports the bucket's name and the archive prefix, so the value
`AQDT_ARCHIVE_URI` must agree with can be read from the stack rather than remembered.

That agreement is the most likely operational mistake this component can produce: a stack that
manages one bucket while every run writes to another is not a visible failure, it is a quiet
divergence. Two checks close it. The bucket name is also a constant in the observation store,
which owns archive-URI resolution, and a test asserts the template's `BucketName` equals that
constant — drift between template and code is caught offline, in CI, with no AWS. At run time the
store rejects an `AQDT_ARCHIVE_URI` naming any other bucket, before a request or a write, so a
stale variable fails on the first run rather than after hours of writing somewhere unmanaged.

**Runner role.** A GitHub OIDC identity provider and an IAM role that this repository's workflows
assume, with a trust policy restricted to `repo:mattjtravers/air-quality-digital-twin:*` and a
permission policy scoped to the archive prefix only — `s3:GetObject`, `s3:PutObject`,
`s3:DeleteObject` on `{bucket}/{ArchivePrefix}/*` and `s3:ListBucket` on the bucket, conditioned
on that prefix. `ArchivePrefix` is a parameter defaulting to `dc-metro` rather than a literal,
because the same prefix is part of `AQDT_ARCHIVE_URI` and the two must not drift.

An IAM OIDC provider is an account-level singleton per issuer URL, and one for
`token.actions.githubusercontent.com` may already exist from another project in this account.
Creating a second fails the stack. The foundation stack therefore takes a boolean parameter
`CreateGitHubOidcProvider`: when true it creates the provider, when false it takes the existing
provider's ARN as a second parameter and only creates the role. The provider carries
`DeletionPolicy: Retain` either way, so deleting this stack cannot remove an identity provider
that other stacks in the account depend on.

The workflows authenticate with the long-lived access keys of a hand-made runner IAM user; the
role sits beside it, unused, until the workflows adopt it. Adopting it changes `PIPE-CFG-001` and
all four workflow files — a cascade into the pipeline segment (pipeline LLD, open question 5)
rather than a change this component can make alone.

### Dispatch stack

**Schedules.** Four `AWS::Scheduler::Schedule` resources, each targeting the Lambda with the
workflow it should dispatch:

| Schedule | Expression (UTC) | Target workflow |
|---|---|---|
| `aqdt-ingest-purpleair` | `cron(0/15 * * * ? *)` | `ingest-purpleair.yaml` |
| `aqdt-ingest-airnow` | `cron(20 * * * ? *)` | `ingest-airnow.yaml` |
| `aqdt-calibrate-fit` | `cron(30 0 * * ? *)` | `calibrate-fit.yaml` |
| `aqdt-calibrate-apply` | `cron(40 * * * ? *)` | `calibrate-apply.yaml` |

EventBridge cron expressions carry six fields and a `?` in either the day-of-month or day-of-week
position, so they do not transcribe directly from the five-field form. The cadences themselves and
why each one is what it is belong to the pipeline LLD; this table is the rendering of them.

`FlexibleTimeWindow` is `OFF` on every schedule: firing at the stated minute is the property this
whole design exists to obtain.

**Invocation role.** EventBridge Scheduler invokes a target under an IAM role of its own, separate
from the Lambda's execution role. The stack declares one role trusted by `scheduler.amazonaws.com`
whose only permission is `lambda:InvokeFunction` on this stack's function, and every schedule
names it.

**State, and the maintenance switch.** Every schedule is created with `State: DISABLED` and
enabled deliberately: a stack deploy is not a decision to start dispatching into `main`, and a
schedule live the instant the template lands would fire before the token or the workflows are
necessarily ready.

The template's `DISABLED` applies on every deploy that touches a schedule, not only the first.
`bin/schedules.sh` changes `State` outside CloudFormation, and a stack update re-sends each
changed schedule's whole definition, template `State` included — so redeploying the dispatch
stack with a change that reaches the schedules (their expressions, targets, or the function they
invoke) turns them off. After any dispatch-stack deploy, `bin/schedules.sh status` says whether
they are still on, and `on` restores them.

A schedule's `State` is also how scheduled runs are turned off and on for a maintenance window.
Disabling the four schedules stops the dispatch at its source: no Lambda invocation, no workflow
run, nothing to skip. Manual `workflow_dispatch` runs are unaffected, so a backfill or a
verification run works while the schedules are off.

`bin/schedules.sh {on|off}` wraps the four `aws scheduler update-schedule` calls, because a
switch that takes four commands to flip is a switch that gets flipped partially. `status` prints
each schedule's current state, so the answer to "are the schedules on?" comes from AWS rather than
from memory.

Because the switch lives with the schedules, the workflows carry no activation condition of their
own: any `workflow_dispatch` they receive, they run.

**Input.** Each schedule passes a JSON object, not a bare string:

```json
{"workflow": "ingest-purpleair.yaml"}
```

The handler requires the `workflow` key, requires its value to be one of the four workflow file
names it knows, and raises otherwise. The allow-list matters because the value becomes a path
segment in the URL the function calls; nothing but these four schedules should ever be able to
name a workflow, and an unknown name is a misconfiguration worth failing loudly rather than
forwarding to GitHub.

**Retries.** Each schedule sets `MaximumRetryAttempts: 2` and `MaximumEventAgeInSeconds: 300`.
EventBridge Scheduler's default is up to 185 attempts over 24 hours, which for a failure that will
not clear — an expired token, a deleted secret — means hundreds of errored invocations, and for
one that does clear means a run dispatched hours after the window it was meant for. Two quick
retries cover a transient GitHub error; beyond that the next occurrence is the retry, exactly as
it is for a failed run.

**Lambda.** One function serves all four schedules, each naming its workflow in the input.
Its name is `${AWS::StackName}-dispatch` — `aqdt-dispatch-dispatch` for the `aqdt-dispatch`
stack — and its log group (`/aws/lambda/aqdt-dispatch-dispatch`) and alarm
(`aqdt-dispatch-dispatch-errors`) derive from the same expression. Python 3.13, handler
`handler.lambda_handler`, `CodeUri` `infra/dispatch/app/`. The name is a replacement property:
changing it creates a new function and a new log group, and repoints every schedule.

The function has no dependencies to install: `boto3` is present in the Lambda runtime and
`urllib.request` issues the POST, so the deployment package is one file of a few kilobytes and
`sam deploy` packages it with no build step. It lives outside `src/aqdt/` deliberately — `CodeUri` bundles what it points at,
and the pipeline package pulls in GeoPandas, Pandera, and PyArrow, none of which a dispatcher
needs.

On invocation it reads the GitHub token from Secrets Manager, then issues:

```
POST https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/actions/workflows/{workflow}/dispatches
Authorization: Bearer {token}
{"ref": "{GITHUB_REF}"}
```

GitHub answers `204 No Content` on success. Every other outcome raises, so the invocation is
recorded as an error rather than passing silently, but the message distinguishes the cases,
because GitHub returns the same status for several very different faults:

| Outcome | Meaning | What the message must say |
|---|---|---|
| `204` | Dispatched | — |
| `401` | Token invalid or expired | Name the secret, not the token value |
| `403` | Token lacks `Actions: write`, or rate limited | Distinguish the two by the `x-ratelimit-remaining` header |
| `404` | Workflow file absent, `workflow_dispatch` trigger absent, ref absent, or token cannot see the repository | Name the workflow and the ref, and say that a fine-grained token without repository access also lands here |
| other / transport | — | Status and body, truncated |

A 404 collapsing four distinct faults is the reason the message matters: without it, an expired
token and a renamed workflow file look identical in CloudWatch.

Configuration reaches the function as template parameters rendered into environment variables:
`GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_REF` (default `main`), and `GITHUB_TOKEN_SECRET_NAME`. Each
parameter carries the value this project uses as its `Default`, so a deploy needs no overrides and
the template reads as the complete description of what it creates. Timeout 10 s, memory 128 MB.

Its role grants `secretsmanager:GetSecretValue` and nothing else beyond log writes. The resource
is `arn:aws:secretsmanager:{region}:{account}:secret:{name}-??????` — a Secrets Manager ARN ends
in a six-character suffix AWS assigns at creation, so the ARN cannot be composed from the name
alone, and the six-character wildcard is the narrowest grant expressible from a name. A secret
deleted and recreated gets a new suffix but matches the same pattern, so rotation by recreation
needs no redeploy.

Its log group is declared explicitly with `RetentionInDays: 30`, so logs do not accumulate under
the default of never expiring.

The body carries `ref` and nothing else. Omitting `inputs` is what makes a scheduled run resolve
its own routine window instead of being handed bounds, and `ref` is always `main`: a scheduled run
executes the default branch's workflow definition, and running another branch stays a deliberate
manual act.

Dispatch is at-least-once. A POST that succeeded at GitHub but whose response was lost is retried,
producing a second workflow run over the same window; the workflow's concurrency group queues it
and idempotence makes it a no-op. Nothing tries to detect or suppress that, because the cure —
tracking dispatch identity across invocations — would cost more state than the duplicate costs
runner minutes.

**The GitHub token.** A fine-grained personal access token scoped to this repository alone with
the `Actions: write` permission, held in Secrets Manager as `aqdt/github-dispatch-token`. The
secret is created once by hand and its value never appears in a template; the dispatch stack takes
the secret's name as a parameter and grants read on its ARN.

The secret must exist before the dispatch stack is deployed. Nothing in the stack enforces that —
a deploy against a missing secret succeeds and every invocation then fails with `AccessDenied` —
so it is the first step of the deploy order below. Rotation replaces the value in place, which
needs no redeploy; the ARN suffix only changes if the secret itself is deleted and recreated,
which the wildcard grant already tolerates. A fine-grained token carries an expiry date, and an
expired one stops every schedule at once while leaving the archive quietly stale, so the expiry is
the single most important date in this component's operation.

## Failure Visibility

A scheduled run that fails is visible in the Actions tab, and that is what the HLD's falsification
signal relies on. A *dispatch* that fails produces no workflow run at all, so the same staleness
would appear with nothing in GitHub to explain it — the failure exists only in CloudWatch.

The dispatch stack therefore declares a CloudWatch alarm on the Lambda's `Errors` metric: sum over
a 15-minute period, threshold `>= 1`, one evaluation period, and `TreatMissingData: notBreaching`
so that the quiet periods between the sparser schedules do not themselves alarm. One failed
dispatch is worth knowing about, because the retry policy is deliberately short and the schedules
that fire once a day have no second chance that day.

The alarm carries no `AlarmActions`. It changes state in the CloudWatch console and notifies
nobody, because the monitoring surface for this system is the Lambda and CloudWatch consoles read
by hand. There is one operator, who is also the developer, and a notification path would cost a
topic, a subscription confirmed out of band, and an address in a template to report something that
operator is already in the console to see.

Two failures sit outside what the alarm can reach, and both are accepted. A dispatcher that stops
being invoked at all — a schedule disabled, deleted, or never enabled — produces no errors, so no
alarm state changes; the archive going stale is the only signal. A token approaching expiry is
likewise unwatched, and the alarm reports its expiry only once it has already stopped a dispatch.
In both cases the staleness is the falsification signal the HLD names, and a system checked by its
operator rather than paged at reaches it soon enough.

## Deployment

`aws-sam-cli` is a dev dependency in `pyproject.toml`, so `uv sync --all-groups` installs it and
every invocation is `uv run sam ...`; no devcontainer feature and no separate install step. The
`aws-cli` devcontainer feature, already present, carries the credentials and makes the one-off
calls that create the secret.

**The deploying principal.** The credentials the Codespace holds by default belong to the runner
IAM user, whose policy reaches the archive prefix and nothing else; it cannot create a stack.
Deploys run instead as a separate IAM user, `aqdt-deploy`, kept as a named AWS CLI profile in the
Codespace. Selecting it takes more than naming the profile: the Codespace receives the runner
user's credentials as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, and environment variables
outrank a profile in the AWS credential chain, so a deploy command is prefixed
`env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY AWS_PROFILE=aqdt-deploy`. Naming the profile
alone silently runs as the runner user. Separating the two matters
because the runner user's access keys are also this repository's Actions secrets, so widening that
user enough to deploy would widen every workflow run with it. `aqdt-deploy` is created by hand and
appears in no template: a template cannot create the identity that deploys it.

`samconfig.toml` holds one config environment per stack, selected with `--config-env`:

```
uv run sam deploy --config-env foundation
uv run sam deploy --config-env dispatch
```

Neither deploy runs `sam build` first. The foundation stack declares no code at all, and the
dispatch function has no dependency to install, so a build step would copy `handler.py` and
nothing else — while requiring a `python3.13` interpreter on `PATH` to match the function's
runtime, or a container image pulled to stand in for one. The Codespace carries neither, because
the project's own interpreter tracks the version the pipeline package needs rather than the
version Lambda runs. `sam deploy` packages the `CodeUri` directory directly, which for a
single-file function is the same artifact the build would have produced.

Both environments pin `region` to the archive bucket's region rather than inheriting
`AWS_DEFAULT_REGION`, so a deploy cannot land in a different region from the archive depending on
a shell variable. Each stack creates roles, so both acknowledge `CAPABILITY_IAM`; the foundation
stack also acknowledges `CAPABILITY_NAMED_IAM`, because its runner role carries an explicit
`RoleName` so the workflows can name the role they assume.
`.aws-sam/` is git-ignored.

`samconfig.toml` is committed and holds stack names, the region, and capabilities; the parameter
values themselves are `Default`s on the templates' own parameters. This does not contradict the
project's rule that configuration is environment variables and never files: that rule governs what
a *run* reads at runtime, where a file in the repository would fork the archive or leak a key. A
template parameter is a deploy-time description of a resource, it belongs in version control
beside the template that consumes it, and none of these values is a credential — the one secret is
named, never expressed.

### Deploy order

The stacks and the hand-made secret have a required order, and nothing in the templates enforces
it:

1. Create the token secret (`aws secretsmanager create-secret`). The dispatch stack deploys
   happily without it and then fails at every invocation.
2. Deploy the foundation stack. It creates the bucket the archive lives in, which every run needs.
3. Deploy the dispatch stack. Its schedules arrive `DISABLED`.
4. Enable the schedules once a manual `workflow_dispatch` run has been seen to succeed against the
   archive.

Any later dispatch-stack deploy ends with `bin/schedules.sh status`, and `on` if the deploy
disabled them (§ Dispatch stack, State).

### Rollback and teardown

A stack whose *first* create fails lands in `ROLLBACK_COMPLETE`, which cannot be updated and must
be deleted before retrying. Because the bucket and the OIDC provider carry `DeletionPolicy:
Retain`, that delete leaves them behind, so a retry of the foundation stack must either import
them or use `CreateGitHubOidcProvider: false` and a bucket that the retry expects to already
exist. This is the one path where a failed deploy leaves work to do by hand.

`sam delete --config-env dispatch` removes the schedules, the function, its roles, the log group,
and the alarm; the archive and the runner role are in the other stack and are unaffected, which is
the property the two-stack split exists to give. Deleting the foundation stack leaves a retained,
now-unmanaged bucket and OIDC provider behind — recovering management of them would be a resource
import, so the foundation stack is not something to delete casually.

Both stacks are deployed by a developer from the Codespace under the `aqdt-deploy` profile, with
the runner user's environment credentials unset for the command. There is no deployment from CI,
and nothing detects a template that was merged but never deployed.

## Package Layout

```
infra/
  foundation/
    template.yaml        # bucket, OIDC provider, runner role
  dispatch/
    template.yaml        # schedules, Lambda, role, log group, alarm
    app/
      handler.py         # lambda_handler; no dependencies beyond the runtime
bin/
  schedules.sh           # on | off | status across the four schedules
samconfig.toml
tests/infrastructure/
  test_foundation_template.py
  test_dispatch_template.py
  test_handler.py
```

## Testing

Templates are tested as data: parsed with a PyYAML loader that passes through SAM's `!Ref`,
`!GetAtt`, and `!Sub` short-form tags, then asserted on structure — that the bucket carries both
retain policies, that four schedules exist with the expected expressions and
`FlexibleTimeWindow: OFF`, that the Lambda's policy names exactly one secret, that the runner
role's trust policy names this repository. No test calls AWS, so the suite runs in CI without
credentials, as every other test in this project does. One test shells out to `sam validate` and
skips when the SAM CLI is absent.

`sam validate` runs with an explicit `--region`, so it resolves nothing from ambient AWS
configuration and needs no credentials; without that it passes on a developer's machine and fails
in CI, where the SAM CLI is installed by `uv sync --all-groups` but no AWS configuration exists.

One further test reads `.github/workflows/` and asserts that the handler's allow-list is exactly
the set of workflow file names the schedules target. The allow-list is a list of another segment's
artifacts, and this is what keeps adding or renaming a workflow from silently leaving it behind.

The handler is tested with `urllib.request` and the Secrets Manager client stubbed: that it posts
to the URL its input names, that it sends the token as a bearer credential, that the body carries
`ref` and no `inputs`, that a 204 succeeds, that an input outside the allow-list raises before any
request, and that each distinguished failure status raises with its own message.

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| IaC tool | AWS SAM | The stack is a Lambda plus schedules, which is what SAM is shaped for, and plain `AWS::Scheduler::Schedule` resources sit beside the function in the same template. CloudFormation keeps stack state server-side in AWS, which matters here because the Codespace holding any local state is disposable. |
| SAM CLI delivery | `aws-sam-cli` as a dev dependency, invoked `uv run sam` | `uv sync --all-groups` becomes the whole install, with the version pinned in the lockfile like every other tool. |
| Stack split | Two stacks, by lifetime: foundation and dispatch | The dispatch stack must be safe to delete and redeploy during development; the bucket holding the system of record must not share that property. |
| Archive bucket name | Pinned explicitly in the template | `AQDT_ARCHIVE_URI` stays a value set once in the repository variable and the Codespaces secret, with nothing plumbing stack outputs into GitHub. |
| Bucket protection | `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain`, versioning with 30-day noncurrent expiry | PurpleAir history is not re-fetchable, so a damaged partition cannot be rebuilt from upstream. Retain guards the bucket; versioning guards its contents; expiry bounds the cost of a partition rewritten ~96 times a day. |
| Runner credential | GitHub OIDC provider and an assumable role scoped to the archive prefix | Short-lived credentials leave no access key in repository secrets, and the principal can do exactly what a run needs. Declaring it is additive; adopting it is a pipeline-segment cascade. |
| One Lambda for four schedules | Target workflow passed as the schedule's `Input` | A new cadence becomes a new schedule rather than new code, and there is one dispatch path to test. |
| Lambda source location | `infra/dispatch/app/`, outside `src/aqdt/` | `CodeUri` bundles what it points at; the pipeline package carries GeoPandas, Pandera, and PyArrow, which a dispatcher must not ship. |
| Lambda dependencies | None beyond the runtime (`boto3`, `urllib.request`) | No `requirements.txt` means the deployment package is one file of kilobytes, `sam deploy` packages it with no build step, and there is no dependency to keep patched in a function that holds a credential. |
| Schedule jitter | `FlexibleTimeWindow: OFF` | Firing at the stated minute is the property this design exists to obtain. |
| Token storage | Fine-grained PAT, `Actions: write` on this repository only, in Secrets Manager | The narrowest permission that can start a workflow, held where the template can reference it without expressing it. |
| Log retention | Log group declared explicitly, `RetentionInDays: 30` | An undeclared Lambda log group retains forever; 30 days outlasts any investigation this dispatcher will prompt. |
| Deploy region | Pinned in `samconfig.toml` for both stacks | A deploy cannot land in a different region from the archive because of a shell variable. |
| Template testing | Parse as data and assert structure offline; `sam validate` when the CLI is present | Matches the rest of the suite, which runs in CI without AWS credentials, and keeps infrastructure inside the tests-before-code discipline. |
| Schedule initial state | `DISABLED`; enabled deliberately after a manual run succeeds | Deploying a template is not a decision to start dispatching into `main`, and a live schedule at deploy time would fire before the token or the workflows are necessarily ready. |
| Maintenance switch | The schedules' own `State`, wrapped by `bin/schedules.sh` | Turning runs off at the source creates no workflow run to skip, and leaves manual dispatch working. Because every run arrives as a `workflow_dispatch` event, a gate on the event type cannot tell a scheduled run from a hand-started one, so the distinction has to live where the schedule does. |
| Scheduler invocation role | A second role, trusted by `scheduler.amazonaws.com`, holding only `lambda:InvokeFunction` on this stack's function | EventBridge Scheduler invokes its target under its own role rather than the function's; keeping it separate from the execution role keeps each grant to one purpose. |
| Schedule retry policy | `MaximumRetryAttempts: 2`, `MaximumEventAgeInSeconds: 300` | The service default of up to 185 attempts over 24 h turns a non-clearing failure into hundreds of errors and a clearing one into a run dispatched hours after its window. Beyond two quick retries the next occurrence is the retry, as it is for a failed run. |
| Secret grant | `...:secret:{name}-??????` | A Secrets Manager ARN ends in a six-character suffix assigned at creation, so it cannot be composed from the name; the wildcard is the narrowest grant expressible from a name, and it survives a secret recreated under the same name. |
| OIDC provider creation | Parameterized: create it, or take an existing provider's ARN; `DeletionPolicy: Retain` either way | An IAM OIDC provider is an account-level singleton per issuer URL, so creating a second fails and deleting this stack would otherwise remove one that other stacks depend on. |
| Schedule input | A JSON object with a `workflow` key, validated against an allow-list of the four workflow file names | The value becomes a path segment in the URL the function calls, so it is checked rather than forwarded; an object leaves room for a second key without changing the contract. |
| Alarm configuration | `Errors`, sum over 15 min, `>= 1`, one evaluation period, `TreatMissingData: notBreaching` | One failed dispatch is worth knowing about given short retries and a daily schedule with no second chance; `notBreaching` keeps the gaps between sparse schedules from alarming on their own. |
| `samconfig.toml` in version control | Committed, carrying stack names, region, and capabilities; parameter values are the templates' own `Default`s | The project's "configuration is environment variables, never files" rule governs what a run reads at runtime; a deploy-time parameter describes a resource and belongs beside the template that consumes it. No value in it is a credential. |
| Ref that scheduled runs execute | `main`, fixed in the stack; no schedule carries a ref | A scheduled run should execute the reviewed definition on the default branch. Running another branch stays possible by hand, where the person doing it chose the branch. |
| Window bounds on a scheduled dispatch | None; the body carries `ref` only | A dispatched run resolves its own routine window, exactly as a hand-run command does, so the trigger holds no window logic and there is one code path for both. |
| Duplicate dispatch | Neither detected nor suppressed | Dispatch is at-least-once; tracking dispatch identity across invocations would cost more state than a duplicate costs runner minutes, and the duplicate is a no-op because runs are idempotent. |
| Archive prefix in the runner policy | A template parameter, defaulting to `dc-metro` | The same prefix is part of `AQDT_ARCHIVE_URI`; writing it into the policy as a literal would let the two drift without anything noticing. |
| Monitoring surface | The Lambda and CloudWatch consoles, read by hand; the `Errors` alarm carries no action and nothing watches for missing invocations or a token nearing expiry | One operator, who is also the developer, already opens the console to look at a run. A notification path costs a topic, a subscription confirmed out of band, and an address in a template to tell that person what the console shows them. The archive going stale remains the falsification signal for everything the alarm cannot reach. |
| Deploying principal | A separate hand-made IAM user, `aqdt-deploy`, as a named AWS CLI profile | The runner user's policy reaches the archive prefix only, so it cannot deploy; and its access keys are this repository's Actions secrets, so widening it to deploy would widen every workflow run. A template cannot create the identity that deploys it, so this one identity stays outside the templates. |
| Guarding `AQDT_ARCHIVE_URI` against the managed bucket | A bucket-name constant in the observation store, asserted equal to the template offline, and enforced at run start | A stack managing one bucket while runs write to another is a quiet divergence rather than a failure. Checking template against code catches drift in CI; checking the variable at run start catches a stale value on the first run. |

## Open Questions & Future Decisions

1. Whether anything should detect a template merged to `main` but never deployed. Deployment is a
   developer action from a disposable Codespace, so deployed state can diverge from the repository
   with nothing reporting it.
2. Whether the four schedules should carry an `AWS::Scheduler::ScheduleGroup`, which would give
   them a shared namespace, though not a single on/off switch — a group's schedules are still
   enabled and disabled one at a time, which is what `bin/schedules.sh` exists to hide.
3. Whether the foundation stack should also declare the PostGIS-side resources if the serving
   layer ever moves out of the Codespace. Nothing depends on this today.
4. Retiring the hand-made runner IAM user once the workflows adopt the OIDC role. `aqdt-deploy`
   stays either way — it is the identity that deploys, not one a run uses.

## References

- `docs/high-level-design.md` — Execution model (why scheduling is external); Environment; Persistence.
- `docs/intent/pipeline/pipeline-design.md` — the cadences these schedules render, the routine
  windows a dispatched run resolves, and the workflows the Lambda targets.
- `docs/intent/observation-store/observation-store-design.md` — what the archive bucket holds,
  `AQDT_ARCHIVE_URI`, and the read-merge-write that makes versioning worth its cost.
- AWS SAM — `AWS::Serverless::Function`, `samconfig.toml` config environments.
- EventBridge Scheduler — `AWS::Scheduler::Schedule`, six-field cron expressions,
  `FlexibleTimeWindow`.
- GitHub REST API — `POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`;
  fine-grained PAT `Actions: write`.
- GitHub OIDC for AWS — `token.actions.githubusercontent.com` trust policy conditions.
