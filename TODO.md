# TODO

## Open

1. **Chapter 1 length.** Prose is 2 423 words (inside the 1 500–3 000 rule); with the four tables it is 3 099 and with code blocks 3 262. Decide whether tables count; if they do, trim about 100 words from section 3 or the exercise answers. Chapter 2 is 2 923 prose words, 4 421 with tables, 4 633 with code (counted 2026-10-16), so the same decision applies there with more to trim.
2. **Free-tier sentences in chapter 1 section 5 are labelled unverified** (they were from memory). Open the current AWS free-tier page (blocked from the cloud agent), confirm which of CloudFront, Lambda and DynamoDB have always-free allowances and what changed in 2025, then rewrite the paragraph with a "checked on <date>" and remove the "not verified" wording.
3. **Resource count with the CDK CLI.** `python app.py` synthesises 26 (ch01) and 41 (ch02) resources; `npx aws-cdk synth` adds one `AWS::CDK::Metadata` resource to each. The chapters quote the stacks' own counts; say "plus one CDK metadata resource" if you want the CLI number to match.
4. **`cdk.json` has no feature-flag context** in either example, so the CLI prints "feature flags are not configured". Harmless now; consider pinning the flags a `cdk init` project would carry.
5. **Nothing has been deployed.** Chapter 1: the OAC bucket policy and the Lambda's IAM grant. Chapter 2: the Basic Ingest path (that `topic(3)` is the thing name with the `$aws/rules/FarmTelemetry` prefix stripped, as the developer guide says), the `${iot:Connection.Thing.ThingName}` policy, Firehose delivery to the daily prefix, the Glue partition projection, and the stale-scan metric. One `cdk deploy` per chapter from a personal account, the README's commands, then `cdk destroy` (about $3 + $8 a month if left up).
6. **Chapter 2 exercise 4 names Kinesis Data Streams as the way around Firehose's 5 KB minimum** but `prices.json` has no Kinesis prices and the claim that Firehose bills a Kinesis source per GB without rounding is from the price list's usage-type names, not the pricing page. Fetch the Kinesis Data Streams offer file, add the line to `cost.py` as a fourth tuned column, and confirm the rounding rule on the Firehose pricing page.
7. **Chapter 2 cost model simplifications** worth a sentence if anyone quotes it: Lambda duration is a guessed 80 ms (two DynamoDB writes, warm); the stale-scan Lambda duration is approximated as 1 s per 20 000 devices; CloudWatch Logs assumes one 150-byte line per message; the archive is 12 months at 5:1 gzip. All are named constants in `cost.py`.

8. **WALKTHROUGH.md length.** STYLE.md says 300–600 words; with a section per chapter this one is about 1 500 words after chapter 3, like the other two courses'. Decide the course policy: one hub file plus `walkthrough/chNN.md`, or accept per-chapter growth.

9. **Chapter 3 container image not built.** The sandbox has no Docker daemon, so `pipeline/Dockerfile` has never been built; `cdk deploy` builds it for ARM64. Build it once locally (`docker build --platform linux/arm64 examples/ch03/pipeline`) and run `docker run <image> train --archive ... --artifacts ... --run-id ...` against `examples/ch03/local/` to confirm the entry point and the package pins.

10. **Chapter 3 throughput constants are from the sandbox CPU.** `cost.py` uses 250 000 lines/s and 50 000 rows/s (measured here: 313 940–334 849 lines/s, ~55 000 rows/s). After one real execution, read the task's `lines_per_second` and `total_seconds` from its log and replace them; the wall-time rows in section 5 move with them. A second sandbox (review, 2026-10-20) measured only 191 969 lines/s on the same `make ch03-run`, below the 250 000 constant, so the wall-time rows can be optimistic by about a quarter on a slower vCPU; one real Fargate number settles it.

11. **`features.sql` has not been run in Athena.** It is the tuned design's aggregation (UNLOAD to Parquet). Run it once against a chapter 2 archive, check the column names match `features.py`'s hourly table, and note the bytes scanned so the $1.59-a-night figure for 90 days at 100 000 devices can be checked against a real 1 000-device scan.

12. **Chapter 3 S3 paths are tested against fakes.** `Store` with an `s3://` root and the gate's `S3Store` have never touched S3. The first deploy exercises both; if `list` pagination or `copy_object` misbehaves it will show in the first execution's log.

13. **Chapter 3 word count.** Prose is about 3 000 words with the exercises and answers (4 600 with tables, 4 900 with code), the same question as item 1.

14. **Chapter 4 has never touched AWS.** The functions are tested against `sim.FakeTable` (which interprets only the condition and update expressions they use), a fake queue, a fake bus and a fake provider. One `cdk deploy`, the README's order sequence (a 201, the same POST again for a 200, a declined 99-cent total, an order larger than the shelf), then read the three DLQs and `cdk destroy`. Watch for: the queue policy EventBridge needs on the SQS targets, the `TransactWriteItems` cancellation reasons' exact `Code` strings, and whether `charge.StubProvider` keeps its keys across warm invocations as assumed.

15. **Chapter 4 idle-polling constant.** `cost.py` assumes Lambda keeps five 20-second long polls open per standard queue all month (1.3 million empty receives for two queues, $0.12). Leave the deployed stack idle for a day and read `NumberOfEmptyReceives` on the two queues; replace the constant.

16. **Chapter 4 word count.** Prose is about 3 000 words with the exercises and answers (5 000 with the tables, diagrams and code), the same question as items 1 and 13.

17. **Chapter 5 has never touched AWS.** Deploy the standby then the primary from a personal account with a real hosted zone (the `cdk.json` defaults are placeholders). Watch for: the replication role's permissions on both buckets (the stack strips the L2's fixed `CDKReplicationRole` name; confirm two roles deploy), the AWS Backup copy into the eu-west-1 vault, DNS validation of two certificates in one zone, the health check reaching the execute-api hostname, CloudFront reading the standby bucket under the account-wide policy, and that `from_table_name` in the standby grants on the replica's ARN. Then one real failover (invert the health check), one real PITR restore of the orders table, and `cdk destroy` with `-c protect_data=false`.

18. **Chapter 5 restore time is assumed.** `drill.py` uses 20 minutes plus 5 per GB for a DynamoDB restore; the drill's "50 min" and "70 min" cells move with it. Replace it with the duration of the real restore from item 17.

19. **Two chapter 5 prices are from memory.** `prices.json` says so: that global-table replication traffic is billed at the inter-region transfer rate (rounds to zero here), and that S3 replication metrics are four CloudWatch custom metrics per rule, the largest line ($2.40 of $3.91) at the cooperative's size. Open the S3 user guide "Replication metrics" page and the DynamoDB pricing page (both blocked from the sandbox) and correct `prices.json` and the chapter's section 5 if either differs.

20. **Chapter 5 word count.** Prose is about 3 300 words with the exercises, answers and reading list (5 400 with tables and code; the 2026-10-25 review added the four exercise questions that were missing, about 260 words), the same question as items 1, 13 and 16.

21. **Chapter 1 has no alarm and no topic** (chapter 6's review, REL-05, accepted until this item). Add a 5xx alarm on the HTTP API (`metric_server_error`, 5-minute period, one evaluation) with an SNS topic; the stack goes from 26 to 28 resources, so update chapter 1's text, the README row and `test_stack.py`, then delete the two acceptances in `examples/ch06/accepted.json` (the reviewer fails while they match nothing).

22. **Chapter 5's standby has no alarm of its own** (REL-05 and OPS-03, accepted until this item). A 5xx alarm on the standby API, routed to the standby's own Alerts topic, so a failover that lands on a broken standby is noticed; add it in `RegionStack` for both roles or for the standby only, update the resource counts (42 + 23) in chapter 5 and the README, and delete the two acceptances.

23. **Chapter 6 word count.** Prose is about 3 200 words with the exercises, answers and reading list (6 200 with the tables, which include the full 26 x 6 matrix), the same question as items 1, 13, 16 and 20. The matrix could move to the example README if the policy is to count tables.

24. **Chapter 6's four CDK-related acceptances describe aws-cdk-lib 2.273's provider functions** (names, runtimes, architectures, the `Resource: *` statements). After any CDK upgrade run `make ch06-review`: a renamed provider either still matches the `Custom*Provider*` patterns or is reported as a stale acceptance, and a fixed statement should take its acceptance with it.

## Done

- 2026-10-29: chapter 6 written with its example; `make check` grew a `ch06-review` target that synthesises the five chapters' production flavour, reviews them and rolls the costs up, and fails on an open finding. The review's fixes landed in the earlier chapters: `project` and `chapter` tags in every `app.py`, PITR on chapter 4's Stock table, explicit SSE-S3 on chapter 5's buckets, a throttle on chapters 4 and 5's default stages (no resource counts changed; chapters 4 and 5 gained a test each).

- 2026-10-25: chapter 5 written with its example; `make check` grew a `ch05-drill` target that runs the two drills and synthesises both regions.

- 2026-10-22: chapter 4 written with its example; `make check` grew a `ch04-sim` target that runs the three functions through the simulator.

- 2026-10-19: chapter 3 written with its example; `make check` grew a `ch03-run` target that runs the pipeline on synthetic data.

- 2026-10-16: README grown to the STYLE.md shape (outputs, layout, limitations, next steps). `cost.py` in ch01 no longer subtracts the always-free 25 GB of DynamoDB storage, so the code matches the "no free tier" label; the chapter 1 table moved from $8.44 / $739.11 to $8.47 / $739.13.
