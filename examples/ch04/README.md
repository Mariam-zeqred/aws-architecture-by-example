# Chapter 4 example: event-driven order processing

The CDK stack, three Lambda functions, simulator and cost model that [chapter 4](../../ch04-event-driven-order-processing.md) walks through.

```
app.py                  CDK app; `python app.py` synthesises to cdk.out/ with no CLI and no credentials
stack.py                OrdersStack: HTTP API -> accept function -> orders table + EventBridge bus;
                        rules -> payment queue -> charge function, -> fulfilment queue -> fulfil function,
                        -> SNS for the two "a person must look" events; a DLQ per queue and per rule
                        target, an archive on the bus, five alarms
lambda/
  orders_common.py        the order's status machine (forward-only, conditional), announce-again, envelope
  accept.py               POST /orders: conditional put on the client's order_id, publish OrderPlaced
  charge.py               OrderPlaced consumer: provider call with an idempotency key, placed -> paid
  fulfil.py               OrderPaid consumer: one transaction moves paid -> reserved and takes the stock
sim.py                  the three functions against fake at-least-once queues, a provider that loses
                        responses and a runtime that dies after writes; guarded vs naive, in memory
cost.py                 monthly cost at 2 000 / 100 000 / 10 000 000 orders, and other plumbing
prices.json             list prices, us-east-1, from the AWS Price List API (dates and versions inside)
tests/                  35 pytest tests: each guard, the simulator, template assertions, cost
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check          # ruff + pytest + synth + cost tables + ch03 run + the simulation below
make -C ../.. ch04-sim       # just the simulation: 500 orders, guarded vs naive
python sim.py --orders 2000 --seed 7
python app.py && ls cdk.out  # just the CloudFormation template (43 resources)
python cost.py               # just the cost tables
```

`sim.py` prints one table: the same 500 orders through the functions as written and through "naive" versions with the guards removed (server-made ids, no idempotency key, unconditional writes). With seed 1 the guarded column charges each order exactly once and the shelf matches the reservations; the naive column stores 554 orders for 500, charges 117 of them twice or more and oversells one SKU. The failure rates at the top of `sim.py` are illustrative parameters, not measurements of anything.

## Deploy it yourself

The repo never deploys anything. From a machine with AWS credentials and Node.js:

```bash
npm install -g aws-cdk
cdk bootstrap                                   # once per account/region
cdk deploy
aws sns subscribe --topic-arn <AlertsTopic> --protocol email --notification-endpoint you@example.com

# Put something on the shelf, then place an order (the client makes the order_id):
aws dynamodb put-item --table-name <StockTable> --item '{"sku":{"S":"FEED-25KG"},"available":{"N":"100"}}'
ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -s -X POST <ApiUrl>orders -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ID\",\"farm\":\"farm-007\",\"lines\":[{\"sku\":\"FEED-25KG\",\"qty\":3,\"unit_cents\":50000}]}"
curl -s -X POST <ApiUrl>orders ... (the same body again)   # 200, "already accepted, announced again"
# ... a few seconds later:
aws dynamodb get-item --table-name <OrdersTable> --key "{\"order_id\":{\"S\":\"$ID\"}}"   # status: reserved
aws dynamodb get-item --table-name <StockTable> --key '{"sku":{"S":"FEED-25KG"}}'        # available: 97
# A total ending in 99 cents is declined by the stub provider: status payment_failed, one email.
cdk destroy                                     # -c retain_data=true at deploy time keeps the tables
```

Leaving the stack up costs about $0.65 a month at list price (`cost.py`), nearly all of it the five alarms and Lambda's idle polling of two queues.

## Known limits

- Synthesised and tested here, not deployed. The functions are tested against fakes of DynamoDB (`sim.FakeTable` interprets only the expressions the functions use), SQS, EventBridge and a payment provider; the first deploy is where the IAM grants, the queue policies EventBridge needs and `TransactWriteItems`' cancellation reasons are exercised for real.
- There is no real payment provider. `charge.StubProvider` approves everything except totals ending in 99 cents and remembers idempotency keys only inside one Lambda container; a real provider keeps the key server-side, which is what makes the retry safe. Replacing it is one class with one method.
- The simulator's Lambda runtime crashes the whole batch on an unhandled exception, as the real one does, so an innocent message can share a batch with a crash several times; with `maxReceiveCount` 5 that no longer dead-letters anything in the seeds tried, but it is possible, which is why the DLQ alarm leads to a redrive, not a delete.
- `cost.py`'s idle-polling line assumes Lambda keeps five 20-second long polls open per queue all month (the documented minimum for standard queues); one month on a real idle queue would measure it.
