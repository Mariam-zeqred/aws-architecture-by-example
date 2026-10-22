# AWS Architecture by Example

A six-chapter course for developers who know one or two AWS services and want to design whole systems. Every chapter starts from a brief that could have come from a client, draws a reference architecture in Mermaid, explains each service choice against its alternatives, prices it from the AWS Price List, lists how it fails, and gives an AWS CDK (Python) skeleton that synthesises in `make check`. Nothing in this repo deploys itself; the deploy commands are in each example's README for you to run from your own account.

```mermaid
flowchart LR
    B[Browser] --> CF[CloudFront]
    CF -->|"/*"| S3[(S3)]
    CF -->|"/api/*"| API[HTTP API] --> L[Lambda] --> T[(DynamoDB status)]
    D[Devices, X.509] -->|MQTT| IOT[IoT Core rule] --> I[Lambda ingest] --> T
    IOT --> FH[Firehose] --> AR[(S3 archive)] --> AT[Athena]
    SCH[Scheduler, nightly] --> SF[Step Functions] --> TR[Fargate train] --> M[(S3 models)]
    AR --> TR
    SF --> SC[Fargate score] --> P[(DynamoDB predictions)] --> API
    API -->|POST /orders| AC[Lambda accept] --> O[(DynamoDB orders)]
    AC --> EB[EventBridge bus] --> Q1[SQS] --> CH[Lambda charge] --> O
    EB --> Q2[SQS] --> FU[Lambda fulfil] --> O
    EB --> SNS[SNS -> staff]
```

*Chapters 1 to 4 together: the page and API from chapter 1 read the status table that chapter 2's device path writes, chapter 3's nightly job reads chapter 2's archive and writes a forecast per device for the same page, and chapter 4 takes the cooperative's orders from the same API through a bus and two queues. Later chapters replace or extend one part at a time.*

## Chapters

Chapters 1 to 4 are written and runnable; chapters 5 and 6 are still to come, one per build session, and the rows below say so until they land.

| # | Chapter | One line |
|---|---|---|
| 1 | [Static site + API + DB](ch01-static-site-api-db.md) | The "hello world" of architectures: one CloudFront hostname, S3 + HTTP API + Lambda + DynamoDB, priced at $8 and $739 a month, with the failure table and a 26-resource CDK stack. |
| 2 | [IoT telemetry at scale](ch02-iot-telemetry-at-scale.md) | One certificate per device, one IoT Core rule fanning out to DynamoDB (status + 30-day history) and Firehose → S3 → Athena, a fleet watchdog instead of per-device alarms; priced at $3, $110 and $10 898 a month for 20, 1 000 and 100 000 devices, and $2 665 after batching and provisioned capacity. |
| 3 | [Batch ML on a schedule](ch03-batch-ml-on-a-schedule.md) | EventBridge Scheduler → Step Functions → a Fargate container that trains on chapter 2's archive, a gate Lambda that promotes a candidate only if it beats "same as yesterday" and the current model on the same hold-out week, the same container scoring every device; priced at $0.63, $0.87 and $24.88 a month for 20, 1 000 and 100 000 devices, where the problem is a 15-hour task rather than the bill, and $3.94 with Athena building the features. |
| 4 | [Event-driven order processing](ch04-event-driven-order-processing.md) | A client-made order id and a conditional put, a forward-only status moved by conditional updates, the provider's idempotency key and one DynamoDB transaction for the stock: three guards that make every at-least-once delivery safe to repeat; EventBridge rules into one SQS queue per consumer, partial batch responses, a dead-letter queue and alarm per queue, an archive to replay from; a simulated month of bad luck charges 500 orders exactly once with the guards and 128 of them twice without; priced at $0.65, $2.36 and $171.72 a month for 2 000, 100 000 and 10 000 000 orders, against $2 000 for the messaging alone as Standard workflows. |
| 5 | Multi-region, backups, RPO and RTO | *(not yet written)* |
| 6 | Cost and security review checklist | *(not yet written)* Well-Architected mapped to the five designs. |

## Running the examples

```bash
pip install -r requirements.txt   # aws-cdk-lib, constructs, boto3, pytest, ruff
make check                         # lint, tests, synthesise every stack, print the cost tables
```

Each `examples/chNN/` folder has a README with that chapter's file map and the deploy commands.

## What the outputs look like

`make check` ends with the cost tables each chapter's `cost.py` prints; the chapters quote them verbatim. The tail of a green run:

```
python3 -m pytest -q
..........................................................................................................  [100%]
106 passed in 10.21s
cd examples/ch01 && python3 app.py && test -f cdk.out/Ch01HelloStack.template.json
cd examples/ch02 && python3 app.py && test -f cdk.out/Ch02TelemetryStack.template.json
cd examples/ch03 && python3 app.py && test -f cdk.out/Ch03BatchMlStack.template.json
cd examples/ch04 && python3 app.py && test -f cdk.out/Ch04OrdersStack.template.json
cd examples/ch01 && python3 cost.py
| **Total / month** | **$8.47** | **$739.13** |
cd examples/ch02 && python3 cost.py
| **Total / month** | **$2.85** | **$109.65** | **$10,898.12** |
cd examples/ch03 && python3 cost.py
| **Total / month** | **$0.63** | **$0.87** | **$24.88** |
| training task wall time / night | 1 min 11 s | 10 min 21 s | 15 h 37 min |
cd examples/ch04 && python3 cost.py
| **Total / month** | **$0.65** | **$2.36** | **$171.72** |
cd examples/ch03/pipeline && python3 -m cli train --archive ../local/archive ...
  "mae": 2.0079924097774433, "baseline_mae": 2.6729114055633545, "champion_mae": null
cd examples/ch03 && python3 lambda/gate.py --artifacts local/artifacts --run-id 2026-10-19
  "promoted": true, "reason": "no current model; candidate MAE 2.008"
cd examples/ch03/pipeline && python3 -m cli score ...
  "devices_scored": 10, "devices_over_threshold": 5
cd examples/ch04 && python3 sim.py --orders 500 --seed 1
| orders charged twice or more | 0 | 128 |
| oversold SKUs | [] | ['SHT31-NODE'] |
| payment DLQ | 3 | 4 |
```

`cdk.out/<Stack>.template.json` is the CloudFormation each chapter would deploy: 26 resources for chapter 1, 41 for chapter 2, 45 for chapter 3, 43 for chapter 4. Chapter 3's part of `make check` runs its pipeline on a synthetic archive (`examples/ch03/local/`, git-ignored); chapter 4's runs its three functions through fake at-least-once queues with and without their guards. The numbers both print describe their generators, not a field or a shop. Each `examples/chNN/README.md` lists the deploy commands and what to expect after them.

## Layout

```
chNN-<slug>.md          one chapter: brief, architecture, choices, worked example, cost, failures, exercises
examples/chNN/          its CDK app (app.py, stack.py), Lambda code, cost.py + prices.json, tests/, README.md
examples/ch03/pipeline/ the container chapter 3 runs on Fargate; the same code runs locally (make ch03-run)
examples/ch04/sim.py    chapter 4's functions against fake queues, provider and crashes (make ch04-sim)
Makefile                check = lint + test + synth + cost + ch03-run + ch04-sim, over every chapter
WALKTHROUGH.md          notes for the author: design rationale and likely questions, per chapter
TODO.md                 open items, with what each needs
```

## What the examples are and are not

- Every stack synthesises to CloudFormation in-process (`python app.py`), so `make check` needs no CDK CLI, no Node.js beyond what `aws-cdk-lib` itself uses, and no AWS credentials.
- Template properties the chapters rely on are asserted by tests, so a chapter's claims about its stack are checked, not just stated.
- Costs are computed by a script from a `prices.json` that records where and when each price was copied. They are list prices, first tier, no free tier.
- Nothing has been deployed from this repo. Deploying is the reader's step, and each example README says what to expect.

## Limitations

- Nothing here has been deployed; IAM, network and delivery behaviour are asserted from templates and from handlers run against fakes. Each example README says what a first deploy should show. Chapter 3's container image is not built here (no Docker daemon in the sandbox); `cdk deploy` builds it.
- Chapter 3's data is synthetic: `examples/ch03/pipeline/synth_archive.py` writes it, and every number from the local run (MAE 2.01 °C against a 2.67 °C baseline, 5 of 10 fields over 30 °C) describes that generator. The throughput constants in its cost model were measured on the sandbox's CPU, not on Fargate.
- Chapter 4's failure rates (lost replies, duplicate deliveries, lost provider responses, crashes) are parameters of `examples/ch04/sim.py`, not measurements; its functions are tested against in-memory fakes of DynamoDB, SQS and EventBridge, and its payment provider is a stub. The idle-polling line of its cost model is a documented minimum, assumed rather than measured.
- Prices are list prices for US East (N. Virginia) on the dates recorded in each `prices.json`; mostly first tier, no free tier. They are inputs to a model, not a quote.
- The two real repos the chapters refer to ([queen-bee-incubator](https://github.com/Mariam-zeqred/queen-bee-incubator), [smart-poultry-farm](https://github.com/Mariam-zeqred/smart-poultry-farm)) are described from their READMEs only.

## Next steps

- Chapter 5: multi-region, backups, RPO and RTO.
- Deploy chapters 1 to 4 once from a personal account, record what the templates could not prove (bucket policy, Basic Ingest, Firehose delivery, the image build and one real nightly execution, the queue policies and one real transaction cancellation), then `cdk destroy`.
- Open items per chapter are in [TODO.md](TODO.md).

## Licence

MIT, see [LICENSE](LICENSE).
