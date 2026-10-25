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

## Chapter 4 - Event-driven order processing

*What it teaches.* Every delivery is at-least-once (the phone retries, SQS redelivers, the function dies after a write), so every step must be safe to repeat; then duplicates cost nothing and the only enemy is a lost message, which a dead-letter queue, an alarm and an archive make recoverable. The three guards: a client-made order id with a conditional put; a forward-only status moved only by conditional updates, so the order item is its own idempotency record; the provider's idempotency key, plus one DynamoDB transaction for the stock. The "write, then announce" gap is closed by announcing again whenever the stored status says the announcement may not have happened. Cost: at the cooperative's size you pay to listen (alarms and empty receives), and at scale events plus queues are fifty times cheaper than a Standard workflow per order, but Express is cheaper still, so events are chosen for decoupling, not price.

*The example.* `examples/ch04/stack.py`, 43 resources: HTTP API, three functions, two tables, a bus with an archive, three rules, two queues each with a DLQ, a DLQ for the rules, five alarms. `lambda/` is about 550 lines; `sim.py` runs the three functions through fake queues that deliver twice, a provider that loses responses and a runtime that dies after writes, guarded and naive, and prints the section 4 table (seed 1: 0 double charges against 128). `cost.py` prints the two tables. Prices: SQS and EventBridge from the Price List API on 2026-10-22, the rest reused.

*Questions a professor may ask.*
1. *Why not a FIFO queue, which deduplicates?* FIFO deduplicates sends within five minutes and orders delivery within a message group; it does nothing about a client retry an hour later, a provider response lost after the charge, or a function that died after writing. The guards are in the data, where every kind of repeat meets them; FIFO is bought for ordering, which this design does not need because every step checks the status (exercise 3).
2. *Where is the idempotency table?* The order item is it: a step that moves `status` with a condition has its "already done" record for free and it can never disagree with the data. A separate processed-ids table is right for a step with nothing to move (an email) and would have to be kept in step with the order by a transaction here anyway. Say that the simulator's naive column is each guard removed one line at a time.
3. *What happens when the payment provider is down for an hour?* Every message in the payment queue fails and comes back; nothing is doubled (the key) or lost (the queue), the `PaymentStuck` alarm fires at 15 minutes, and after five receives messages start dead-lettering, about 12 minutes in. The right move is to pause the event source mapping and let the backlog drain when the provider is back; the DLQ is for messages that cannot be processed, not for a dependency that is down (exercise 2).

*Verified / not.* ruff, 35 tests (106 in all), `python app.py` synthesises, `cost.py` prints the tables, `sim.py` runs and the tests pin its guarded column. Not verified: anything against real AWS (IAM grants, the queue policies EventBridge needs, `TransactWriteItems` cancellation reasons, Lambda's actual idle polling); the provider is a stub.

*Extend.* Exercise 5: replace "write, then announce" with DynamoDB Streams and an EventBridge Pipe, delete the announce-again code, and watch the simulator's `events published` row drop to one event per status change.

## Chapter 5 - Multi-region, backups, RPO and RTO

*What it teaches.* Recovery objectives are decided per data set before any copy is chosen: an RPO (how much may be lost) and an RTO (how long it may be down) for orders, stock, device status, the archive, the models, the site and the controllers, each with the cheapest copy that meets it. Then the chapter's one sentence: a replica protects you from the region, a backup protects you from yourself, because replication copies a deletion or a bad write within a second. With serverless the usual ladder (backup & restore, pilot light, warm standby, active-active) loses its middle rungs: a standby API, function and table replica cost nothing idle. Active-active is rejected on correctness, not price: a counter decremented from two regions loses writes under last-writer-wins, so chapter 4's consumers run in one region at a time and the move is a person's decision.

*The example.* `examples/ch05/stack.py` is one class deployed twice by `app.py`: the primary (us-east-1, 42 resources) with the global table and its replica, two replicated buckets, a daily AWS Backup plan copied to the other region, a Route 53 health check on `/health` and the PRIMARY failover record, CloudFront with an origin group, five alarms; the standby (eu-west-1, 23 resources) with the destination buckets, the copy vault, the same API reading the replica by name, and the SECONDARY record. `lambda/api.py` reads the table in `/health` so the check covers the whole path. `drill.py` prints the disasters-by-strategies table from named parameters and the two-region counter conflict (12 reserved from 10, six reservations lost). `cost.py`: $3.91 added a month at the cooperative's size, $42.21 at a hundred times the devices; backup & restore $1.00 and $38.51. Prices: both regions from the Price List API on 2026-10-25; two items from documentation the sandbox could not reach are named in `prices.json`.

*Questions a professor may ask.*
1. *Why not just use global tables and skip the backups?* Because a global table replicates the mistake: an item deleted in Virginia is gone from Ireland a second later, and a wrong status is wrong in both. Point-in-time recovery restores any second of the last 35 days, and a recovery point in a vault the primary's credentials cannot reach survives a leaked key. The drill's two middle rows are identical in every column for exactly this reason.
2. *Why is active-active rejected when it costs the same as the standby?* Chapter 4's condition `available >= :q` holds inside one region and fails across two: each copy sees ten, each allows six, each writes four, and last writer wins keeps four, so six reservations vanish and two units are sold that do not exist. Accepting orders in both regions is safe (a client-made id makes a collision two identical items); decrementing a counter in both is not. Hence one write region and a person's decision to move it, in both directions.
3. *How long is the shop down in a region outage, and why is it not zero?* About 150 seconds for reading and accepting orders: three failed health checks 30 seconds apart, then a 60-second alias TTL. Charging and fulfilment wait until a person moves the consumers, about 30 minutes in the drill, because starting them in both regions is question 2. Say that the parameters are at the top of `drill.py` and labelled illustrative; the shape of the table is the lesson, the exact minutes are not.

*Verified / not.* ruff, 38 tests (144 in all), both templates synthesise in-process, `cost.py` and `drill.py` print the tables the chapter quotes. Not verified: anything against real AWS (replication roles, the cross-region backup copy, certificate validation, the health check, CloudFront reading the standby bucket); the restore-time parameter; the two prices named in `prices.json`.

*Extend.* Exercise 3 as code: a `write_region` parameter in SSM read by chapter 4's consumers at start, so moving the write region is one `put-parameter` and a redeploy of nothing, and a test that a consumer refuses an order whose `accepted_in` is not its own region.
