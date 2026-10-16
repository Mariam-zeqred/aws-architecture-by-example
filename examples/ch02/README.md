# Chapter 2 example: IoT telemetry at scale

The CDK stack, two Lambda functions and cost model that [chapter 2](../../ch02-iot-telemetry-at-scale.md) walks through.

```
app.py              CDK app; `python app.py` synthesises to cdk.out/ with no CLI and no credentials
stack.py            TelemetryStack: IoT policy + things, topic rule, ingest Lambda, status + history
                    tables, rejects queue, Firehose -> S3 archive, Glue table, Athena workgroup,
                    stale-device scan on a schedule, three alarms -> SNS
lambda/ingest.py    validates one message, writes history, moves status forward only
lambda/stale_scan.py  counts silent devices, publishes one fleet metric
cost.py             monthly cost at 20 / 1 000 / 100 000 devices, plus the tuned variants
prices.json         list prices, us-east-1, from the AWS Price List API (dates and versions inside)
tests/              28 pytest tests: handlers against fakes, template assertions, cost sanity
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check          # ruff + pytest + synth + cost tables, for every chapter
python app.py && ls cdk.out  # just the CloudFormation template
python cost.py               # just the cost tables
```

## Deploy it yourself

The repo never deploys anything. From a machine with AWS credentials and Node.js:

```bash
npm install -g aws-cdk
cdk bootstrap                         # once per account/region
cdk diff
cdk deploy                            # ~41 resources; prints the tables, bucket, queue, topic

# One certificate for field-1, attached to the thing and the fleet policy.
# Keys are written to your disk, never into a template or this repo.
aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile field-1.crt --private-key-outfile field-1.key \
  --query certificateArn --output text > field-1.arn
aws iot attach-thing-principal --thing-name field-1 --principal "$(cat field-1.arn)"
aws iot attach-policy --policy-name farm-monitor --target "$(cat field-1.arn)"
aws iot describe-endpoint --endpoint-type iot:Data-ATS   # the broker hostname

# Publish one reading as field-1 (mosquitto_pub, or any MQTT client, port 8883).
# The topic is the PublishTopicForField1 output: $aws/rules/FarmTelemetry/farm/telemetry/field-1
mosquitto_pub -h <endpoint> -p 8883 --cafile AmazonRootCA1.pem \
  --cert field-1.crt --key field-1.key -i field-1 \
  -t '$aws/rules/FarmTelemetry/farm/telemetry/field-1' \
  -m '{"temp": 22.4, "hum": 55, "ts": '"$(date +%s)"'}'

aws dynamodb get-item --table-name <StatusTable> --key '{"device": {"S": "field-1"}}'
# After ~5 minutes: an object under s3://<ArchiveBucket>/raw/dt=YYYY-MM-DD/,
# and the StaleDevices alarm will fire once field-1 has been quiet for 10 minutes.
cdk destroy                           # -c retain_data=true at deploy time keeps tables + archive
```

Expected cost at 20 devices is about $3 a month at list price (`cost.py`). Try publishing as `field-1` to `.../farm/telemetry/field-2`: the broker disconnects you, which is the policy doing its job.

## Known limits

- Synthesised and tested here, not deployed: IAM, the Basic Ingest path, Firehose delivery and the Glue/Athena pair are asserted from the template, not observed.
- The things are created by the stack only so the demo has something to attach a certificate to. A real fleet uses fleet provisioning (chapter text, section 4).
- The status API and page from chapter 1 are not repeated here; point chapter 1's `TABLE_NAME` at the status table.
- Cost: list prices, no free tier; only IoT messaging and CloudWatch metrics are tiered, everything else is first tier (over-estimates in the safe direction).
