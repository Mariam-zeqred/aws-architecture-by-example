# Chapter 1 example: static site + API + DB

The CDK stack, Lambda handler, static page and cost model that [chapter 1](../../ch01-static-site-api-db.md) walks through.

```
app.py            CDK app; `python app.py` synthesises to cdk.out/ with no CLI and no credentials
stack.py          HelloStack: DynamoDB table, Lambda, HTTP API + throttle, private S3 bucket, CloudFront
lambda/handler.py the API (GET /api/health, GET /api/readings/{device}, POST /api/readings)
site/index.html   the page; fetches /api/readings/<device> on its own origin
cost.py           monthly cost model; prints the chapter's table from prices.json
prices.json       list prices, us-east-1, copied from the AWS Price List API on 2026-10-13
tests/            21 pytest tests: handler against a fake table, template assertions, cost sanity
```

## Run it here (no AWS account needed)

```bash
pip install -r ../../requirements.txt
make -C ../.. check        # ruff + pytest + synth + cost table
python app.py && ls cdk.out  # just the CloudFormation template and assets
python cost.py               # just the cost table
```

## Deploy it yourself

The repo never deploys anything. From a machine with AWS credentials and Node.js:

```bash
npm install -g aws-cdk            # the CDK CLI
cdk bootstrap                     # once per account/region
cdk diff                          # what would be created (the "plan")
cdk deploy                        # creates ~26 resources; prints SiteUrl, ApiUrl, TableName
curl -s -X POST "<SiteUrl>/api/readings" -d '{"device":"field-1","temp":22.4,"hum":55}'
open "<SiteUrl>"                  # the page should list the reading you just posted
cdk destroy                       # removes everything; deploy with -c retain_data=true to keep the table
```

Expected monthly cost at the brief's traffic is about $8 at list price (see `cost.py`); the CloudFront distribution takes a few minutes to create and to delete.

## Known limits

- Synthesised and tested here, not deployed: IAM and network behaviour are asserted from the template, not observed.
- The static page is deliberately minimal; it is the API path that matters in this chapter.
- Only the first pricing tier is modelled; above 10 TB/month the real transfer price is lower.
