# Chapter 3 example: batch ML training and inference on a schedule

The CDK stack, container pipeline, gate function and cost model that [chapter 3](../../ch03-batch-ml-on-a-schedule.md) walks through.

```
app.py                  CDK app; `python app.py` synthesises to cdk.out/ with no CLI and no credentials
stack.py                BatchMlStack: artifacts bucket, predictions table, VPC without NAT, Fargate task
                        definition, gate Lambda, Step Functions state machine, EventBridge Scheduler
                        schedule, two alarms -> SNS
pipeline/               the container (Dockerfile) and the code it runs, also runnable on a laptop:
  storage.py              one Store over "s3://bucket/prefix" or a directory
  archive.py              streams the chapter 2 archive (gzip JSON lines under raw/dt=YYYY-MM-DD/)
  synth_archive.py        writes a SYNTHETIC archive in that shape; nothing in it is measured
  features.py             readings -> hourly table -> (24 lag means + hour of day) -> next-day peak
  model.py                time split, HistGradientBoosting fit, MAE of candidate / baseline / champion
  cli.py                  `train` and `score`, the two commands the state machine runs
  features.sql            the same hourly aggregation as an Athena UNLOAD, for the tuned design
lambda/gate.py          promotes a candidate only if it beats "same as yesterday" and the current model
cost.py                 monthly cost and task wall time at 20 / 1 000 / 100 000 devices, plus alternatives
prices.json             list prices, us-east-1, from the AWS Price List API (dates and versions inside)
tests/                  22 pytest tests: pipeline on a synthetic archive, gate rules, template assertions, cost
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check          # ruff + pytest + synth + cost tables + the local run below, for every chapter
make -C ../.. ch03-run       # just the local run: synthetic archive -> train -> gate -> score
python app.py && ls cdk.out  # just the CloudFormation template
python cost.py               # just the cost tables
```

`make ch03-run` writes `local/archive/` (10 devices, 21 days, 302 400 lines, synthetic) and `local/artifacts/` with `models/candidate/2026-10-19/`, `models/current/` and `predictions/dt=2026-10-19/`. On this data the candidate's hold-out MAE is 2.01 °C against 2.67 °C for the yesterday baseline, so the gate promotes it, and 5 of the 10 synthetic fields are forecast above 30 °C. Those numbers describe the generator in `synth_archive.py`, not any real field.

## Deploy it yourself

The repo never deploys anything. From a machine with AWS credentials, Node.js and Docker (the CDK builds `pipeline/Dockerfile` for ARM64 and pushes it to ECR during `cdk deploy`):

```bash
npm install -g aws-cdk
cdk bootstrap                                   # once per account/region
cdk deploy -c archive_bucket=<chapter 2 ArchiveBucket>   # or omit -c to get an empty archive bucket
aws sns subscribe --topic-arn <AlertsTopic> --protocol email --notification-endpoint you@example.com

# No chapter 2 fleet? Fill the stack's own archive bucket with synthetic data:
python pipeline/synth_archive.py s3://<ArchiveBucket> --devices 20 --days 30 --end $(date -u -d yesterday +%F)

# Run one night by hand instead of waiting for 02:00 UTC:
aws stepfunctions start-execution --state-machine-arn <StateMachineArn>
# ... about 3 minutes later:
aws s3 ls s3://<ArtifactsBucket>/models/current/           # model.joblib, metrics.json, pointer.json
aws dynamodb scan --table-name <PredictionsTable> --max-items 3
cdk destroy                                     # -c retain_data=true at deploy time keeps both buckets and the table
```

Expected cost at 20 devices is under a dollar a month at list price (`cost.py`), most of it S3 requests. Leaving the stack up costs nothing by the hour: there is no NAT gateway, no endpoint, no instance.

## Known limits

- Synthesised and tested here, not deployed, and the container image was not built here (no Docker daemon in the sandbox). The Dockerfile is four lines; the first `cdk deploy` will tell you if a package pin is wrong.
- The pipeline's S3 branch (`Store` with an `s3://` root, the gate's `S3Store`) is tested against a fake client, not against S3.
- `features.sql` is the Athena version of the hourly aggregation for the tuned design in section 5 of the chapter; it is not run by any test and has not been run in Athena.
- The two throughput constants in `cost.py` were measured on this sandbox's CPU with the synthetic archive (313 940 lines/s in the run above; 334 849 lines/s and about 55 000 rows/s fitting on a 100-device, 90-day archive). A Fargate vCPU is a different machine; the wall-time rows of the tables are estimates.
- The model is deliberately small: a fleet-wide gradient-boosting regressor on 26 features. The chapter is about the pipeline around it, not the model.
