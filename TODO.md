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

10. **Chapter 3 throughput constants are from the sandbox CPU.** `cost.py` uses 250 000 lines/s and 50 000 rows/s (measured here: 313 940–334 849 lines/s, ~55 000 rows/s). After one real execution, read the task's `lines_per_second` and `total_seconds` from its log and replace them; the wall-time rows in section 5 move with them.

11. **`features.sql` has not been run in Athena.** It is the tuned design's aggregation (UNLOAD to Parquet). Run it once against a chapter 2 archive, check the column names match `features.py`'s hourly table, and note the bytes scanned so the $1.59-a-night figure for 90 days at 100 000 devices can be checked against a real 1 000-device scan.

12. **Chapter 3 S3 paths are tested against fakes.** `Store` with an `s3://` root and the gate's `S3Store` have never touched S3. The first deploy exercises both; if `list` pagination or `copy_object` misbehaves it will show in the first execution's log.

13. **Chapter 3 word count.** Prose is about 3 000 words with the exercises and answers (4 600 with tables, 4 900 with code), the same question as item 1.

## Done

- 2026-10-19: chapter 3 written with its example; `make check` grew a `ch03-run` target that runs the pipeline on synthetic data.

- 2026-10-16: README grown to the STYLE.md shape (outputs, layout, limitations, next steps). `cost.py` in ch01 no longer subtracts the always-free 25 GB of DynamoDB storage, so the code matches the "no free tier" label; the chapter 1 table moved from $8.44 / $739.11 to $8.47 / $739.13.
