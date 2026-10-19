# Walkthrough (for Mariam)

For you, not course readers. One section per chapter.

## Why the course is shaped this way

Your AWS certificates say you know the services; this course shows you can *design* with them, which is what edge/cloud labs will probe. Every chapter follows the same six-part skeleton (brief, architecture, choices with alternatives, cost, failure modes, CDK skeleton) so that by chapter 6 you can defend any design with the same vocabulary. The code is CDK in Python because Python is your strongest language and `aws-cdk-lib` synthesises CloudFormation in-process, so `make check` proves the stacks are well-formed without an AWS account. Nothing is deployed from the cloud agent; deploying is your step (about $8 a month for chapter 1 if left up).

## File map

- `chNN-<slug>.md`, one chapter: 1 500–3 000 words, one worked example, exercises with collapsed answers, "What to read next".
- `examples/chNN/`, the chapter's CDK app (`app.py`, `stack.py`), any Lambda code, a `cost.py` + `prices.json`, and `tests/`. `make check` runs all chapters.
- `README.md`, the table of contents; a chapter's row is filled in when it lands.

## Chapter 1 - Static site + API + DB

*What it teaches.* Extract constraints from a brief; put a static site and an API behind one CloudFront hostname (no CORS, one certificate); choose serverless when traffic is unknown and the team is small; price it from list prices and see that data transfer, not compute, dominates at scale; walk a failure table and check each row has a line in the stack.

*The example.* `examples/ch01/stack.py` is 150 lines, 26 resources. The handler takes its table as a parameter so tests run against a fake. `cost.py` prints the chapter's table from `prices.json`, which was copied from the AWS Price List API on 2026-10-13 (offer versions recorded). The CDK CLI's warning that `minimum_protocol_version` is ignored without a custom certificate was real; the chapter uses it as a teaching point and exercise 4.

*Questions a professor may ask.*
1. *Why DynamoDB and not a relational database?* Because the brief has exactly one access pattern (newest N readings for one device), which maps to one partition-key-plus-sort-key query, and because the cheapest always-on RDS instance alone exceeds the budget. Say when you would switch: the moment the questions need joins across entities.
2. *Why is the API behind CloudFront rather than called directly?* One hostname means no CORS, one TLS certificate, one place to attach a WAF, and the static page keeps serving from edge caches even if the region is down. The cost is a little latency on `/api/*` and the need to disable caching on that behaviour, which a test asserts.
3. *Your cost table says $739 at 200 000 visits a day. What would you cut first?* Page weight: 84 % of that bill is data transfer at 1.2 MB per visit; compute is $29. Trimming the page to 300 kB saves more than any Lambda tuning. Exercise 1 has the numbers.

*Verified / not.* ruff, 21 tests, `python app.py` and `cdk synth` (CLI 2.1145.0, lib 2.273.0) produce the template, `cost.py` reproduces the table. Not deployed, so the bucket policy and IAM grant are asserted, not exercised.

*Extend.* Deploy once, read the real warm latency of the 256 MB ARM function from CloudWatch, and replace the "120 ms" assumption in `cost.py`.

## Chapter 2 - IoT telemetry at scale

*What it teaches.* One X.509 certificate per device and a policy that names it (`${iot:Connection.Thing.ThingName}`), so a stolen certificate can only lie about one field; Basic Ingest, so the messaging line is zero; "latest", "30 days" and "years" in three stores because they are three access patterns; the bill at 20 / 1 000 / 100 000 devices split into per-message lines (rules, actions, Lambda, Firehose's 5 KB minimum), per-write (DynamoDB, half of it) and per-device (alarms, hence one scan and one metric).

*The example.* `examples/ch02/stack.py`, 41 resources. `lambda/ingest.py` does the conditional status write (`ts < :ts`) so redeliveries and late backlogs cannot roll status back; `lambda/stale_scan.py` is the watchdog. `cost.py` prints three tables; the "batched x5 + provisioned" column is the honest answer to "cents per device". Prices: Price List API, 2026-10-16.

*Questions a professor may ask.*
1. *Why not keep the HTTP API from chapter 1 for devices?* It works. MQTT gives a persistent connection (one TLS handshake, not 1 440 a day), per-device X.509 identity with a policy that names the device, and a rules engine that fans one message out to two stores without code. The price is a client library on the device and certificate logistics (fleet provisioning at scale).
2. *Your bill at 100 000 devices is $10 898 and half of it is DynamoDB. Is DynamoDB the wrong choice?* It is the wrong *billing mode*: on-demand is right when traffic is unknown, and a fleet on a one-minute clock is the most predictable load there is. Provisioned capacity with 20 % headroom cuts the line 2.9×; batching five readings per message cuts every per-message line 5×. Together: $2 665, 2.7 cents a device.
3. *Why is the watchdog a scan instead of an alarm per device?* An alarm plus a custom metric is about $0.40 per device-month: $8 at 20 devices, $22 000 at 100 000, twice the rest of the system. The scan replaces a per-device price with a per-byte one ($2 a month at 100 000 devices) and needs an index at a million (exercise 5). Its cost is five-minute granularity and a two-scan confirmation before paging.

*Verified / not.* ruff, 28 tests (49 with chapter 1), `python app.py` synthesises, `cost.py` prints the chapter's tables. Not deployed: Basic Ingest, the policy variables and Firehose delivery are asserted from the template and the developer guide (the `topic()` prefix behaviour included), not observed.

*Extend.* Deploy, publish as `field-1` to `field-2`'s topic and watch the broker disconnect you; then switch Firehose to Parquet and measure the bytes one day's Athena query scans before and after.

## Chapter 3 - Batch ML on a schedule

*What it teaches.* A batch job's first failure mode is not running, so the design notices silence (an alarm on "no successful execution today" with missing data as breaching); one Step Functions execution a night with a run id derived from its own start time so every key is stamped; a time-based split, not a random one (exercise 3 measures the difference: 0.54 °C against 2.01 °C on the synthetic data); the candidate, a "same as yesterday" baseline and the current model all scored on the same hold-out week before a gate decides; and that at 100 000 devices the design breaks on *time* (a 15-hour parse) before it breaks on cost, which Athena fixes for $4 a month.

*The example.* `examples/ch03/stack.py`, 45 resources: VPC without NAT, a Fargate task definition (ARM, 1 vCPU / 4 GB) with two commands, the gate Lambda, the state machine with per-task timeouts and a catch-all into a failure notification, the Scheduler cron, two alarms. `pipeline/` is the container and runs locally on a directory: `make ch03-run` writes a synthetic archive, trains, gates and scores. `cost.py` prints wall time next to dollars. Prices: Price List API, 2026-10-19 (Fargate, Step Functions, SageMaker, Scheduler, ECR, SNS).

*Questions a professor may ask.*
1. *Why Fargate and not SageMaker for the training job?* At this size the compute line is under a dollar either way, so the choice is operational: a Fargate task is a Dockerfile plus an entry point, and the same container runs on a laptop against a directory, which is how the tests run it. SageMaker's container contract, managed spot and model registry are worth adopting the day the model needs a GPU or there are many models; the pipeline code would move unchanged. Say that you priced both (section 5, third table).
2. *How do you know the new model is better?* Not by comparing last night's stored metric with tonight's: those are different weeks. The training task re-scores the current model on tonight's hold-out week, so the gate compares two models on identical rows, and also requires the candidate to beat "same as yesterday", because a model that does not beat persistence is not earning its compute. Admit the remaining bias: the hold-out week is also the selection week.
3. *What happens when it fails at 03:00?* The execution fails, the failure email names the step and error, `RunFailedAlarm` fires, the current model is untouched, and the page shows yesterday's forecast with yesterday's `dt`, which is the brief's "must not look like today's". If nothing runs at all, `NoSuccessTodayAlarm` fires because missing data is treated as breaching. The one failure that is still silent is a device with a gap being left unscored, which is exercise 5.

*Verified / not.* ruff, 22 tests (71 in all), `python app.py` synthesises, `cost.py` prints the tables, the pipeline runs end to end on synthetic data here. Not verified: the container image (no Docker daemon in the sandbox), the S3 code paths (tested against a fake client), `features.sql` in Athena, and the throughput of a Fargate vCPU, so the wall-time rows are estimates from this sandbox's CPU.

*Extend.* Deploy with `-c archive_bucket=` pointing at a chapter 2 archive, start one execution by hand, and replace the two throughput constants in `cost.py` with what the task's log reports; then try exercise 5, which is the one gap the chapter leaves open on purpose.
