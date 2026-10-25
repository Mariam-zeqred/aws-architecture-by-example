# Chapter 5 example: multi-region, backups, RPO and RTO

The two-region CDK app, the API that runs in both regions, the two drills and the cost model that [chapter 5](../../ch05-multi-region-backups-rpo-rto.md) walks through.

```
app.py                  CDK app; `python app.py` synthesises BOTH regions to cdk.out/ with no CLI and no
                        credentials: Ch05StandbyStack (eu-west-1) and Ch05PrimaryStack (us-east-1)
stack.py                RegionStack, one class for both roles. Primary: the orders global table with its
                        replica, two replicated buckets, a daily backup copied to the standby's vault, the
                        health check, the PRIMARY failover record, CloudFront with an origin group, five
                        alarms and a rule for failed backup jobs. Standby: the destination buckets, the
                        copy vault, the same API reading the replica, the SECONDARY record
lambda/api.py           GET /health (reads the table), GET /orders/{id}, POST /orders (chapter 4's
                        conditional put, tagged with the region that accepted it)
drill.py                `python drill.py`: four disasters x four strategies from named parameters;
                        `python drill.py conflict`: a stock counter decremented from two regions of a
                        fake global table with last-writer-wins replication
cost.py                 what each strategy adds a month at two sizes, and this chapter's design by line
prices.json             list prices, us-east-1 and eu-west-1, from the AWS Price List API (dates inside)
tests/                  38 pytest tests: both templates, the handler against a fake table, both drills, cost
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check          # ruff + pytest + synth + cost tables + the earlier chapters' runs + the drills
make -C ../.. ch05-drill     # just the two drills
python drill.py              # the disasters x strategies table (parameters at the top of the file)
python drill.py conflict     # ten units, two regions, twelve reserved
python app.py && ls cdk.out  # both CloudFormation templates (42 + 23 resources)
python cost.py               # the two cost tables
```

The drill table is arithmetic over the parameters at the top of `drill.py` (outage length, replication lag, how long a person takes, how long a restore takes); none is a measurement. The conflict drill is a real simulation of last-writer-wins over two copies, in memory.

## Deploy it yourself

The repo never deploys anything. You need a Route 53 public hosted zone you own (the defaults in `cdk.json`, `coop.example` and a placeholder zone id, synthesise but belong to nobody) and credentials in a machine with Node.js. Deploy the standby first: it owns the replication destinations and the copy vault that the primary refers to by name.

```bash
npm install -g aws-cdk
cdk bootstrap aws://<account>/eu-west-1 && cdk bootstrap aws://<account>/us-east-1   # once each
cdk deploy Ch05StandbyStack -c zone_name=example.org -c hosted_zone_id=Z...
cdk deploy Ch05PrimaryStack -c zone_name=example.org -c hosted_zone_id=Z...
aws sns subscribe --topic-arn <AlertsTopic> --protocol email --notification-endpoint you@example.com

# The same order through the shared hostname, then read it from each region directly:
ID=$(python -c 'import uuid; print(uuid.uuid4())')
curl -s -X POST https://api.example.org/orders -H 'content-type: application/json' \
  -d "{\"order_id\":\"$ID\",\"farm\":\"farm-007\",\"lines\":[{\"sku\":\"FEED-25KG\",\"qty\":3,\"unit_cents\":50000}]}"
curl -s <primary ApiUrl>orders/$ID     # served_by us-east-1, accepted_in us-east-1
sleep 2; curl -s <standby ApiUrl>orders/$ID   # served_by eu-west-1: the replica has it
# Fail over on purpose: invert the health check in the console (or break /health), wait ~2.5 min,
# `dig api.example.org` answers with the Ireland domain; POST again and `accepted_in` is eu-west-1.
cdk destroy Ch05PrimaryStack && cdk destroy Ch05StandbyStack   # -c protect_data=false at deploy time to
                                                               # let destroy remove tables and buckets
```

Leaving both stacks up costs about $4 a month at list price (`cost.py`) with the cooperative's data in them, most of it the alarms and S3 replication metrics; the standby API costs nothing idle.

## Known limits

- Synthesised and tested here, not deployed. The first deploy is where these are exercised for real: the replication role's permissions on both buckets, the backup plan's copy into a vault in another region, the certificate validation records in your zone, the health check reaching the execute-api hostname, CloudFront reading the standby bucket under the account-wide policy.
- The handler is tested against a fake table that implements the one condition it uses. In the standby, the function reads a replica that exists only after the primary has deployed.
- `drill.py`'s timings are parameters: the restore time in particular (20 minutes plus 5 per GB) has not been measured; one real PITR restore of the orders table would replace it.
- `prices.json` names two things the sandbox could not fetch from the AWS documentation: that global-table replication traffic is billed at the inter-region transfer rate (it rounds to zero here anyway) and that S3 replication metrics are four CloudWatch custom metrics per rule, the largest line at the cooperative's size.
- Nothing in this chapter moves chapter 4's bus, queues and consumers to the standby; the chapter explains why they run in one region at a time, and the move is a person's step, not a resource.
