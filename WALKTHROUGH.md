# Walkthrough (for Mariam)

This file is for you, not for readers of the course. It grows by one section per chapter.

## Why the course is shaped this way

Your AWS certificates say you know the services; this course shows you can *design* with them, which is what edge/cloud labs will probe. Every chapter follows the same six-part skeleton (brief, architecture, choices with alternatives, cost, failure modes, CDK skeleton) so that by chapter 6 you can defend any design with the same vocabulary. The code is CDK in Python because Python is your strongest language and because `aws-cdk-lib` synthesises CloudFormation in-process, so `make check` proves the stacks are well-formed without an AWS account. Nothing is deployed from the cloud agent; deploying is your step, about $8 a month for chapter 1 if you leave it up.

## File map

- `chNN-<slug>.md`, one chapter: 1 500–3 000 words, one worked example, exercises with collapsed answers, "What to read next".
- `examples/chNN/`, the chapter's CDK app (`app.py`, `stack.py`), any Lambda code, a `cost.py` + `prices.json`, and `tests/`. `make check` runs all chapters.
- `README.md`, the table of contents; a chapter's row is filled in when it lands.

## Chapter 1 - Static site + API + DB

*What it teaches.* Extract constraints from a brief; put a static site and an API behind one CloudFront hostname (no CORS, one certificate); choose serverless when traffic is unknown and the team is small; price the design from list prices and see that data transfer, not compute, dominates at scale; walk a failure table and check each row has a line in the stack.

*The example.* `examples/ch01/stack.py` is 150 lines, 26 resources. The handler takes its table as a parameter so tests run against a fake. `cost.py` prints the chapter's table from `prices.json`, which was copied from the AWS Price List API on 2026-10-13 (offer versions are recorded in the file). The CDK CLI's warning that `minimum_protocol_version` is ignored without a custom certificate was real; the chapter uses it as a teaching point and exercise 4.

*Questions a professor may ask.*
1. *Why DynamoDB and not a relational database?* Because the brief has exactly one access pattern (newest N readings for one device), which maps to one partition-key-plus-sort-key query, and because the cheapest always-on RDS instance alone exceeds the budget. Say when you would switch: the moment the questions need joins across entities.
2. *Why is the API behind CloudFront rather than called directly?* One hostname means no CORS, one TLS certificate, one place to attach a WAF, and the static page keeps serving from edge caches even if the region is down. The cost is a little latency on `/api/*` and the need to disable caching on that behaviour, which a test asserts.
3. *Your cost table says $739 at 200 000 visits a day. What would you cut first?* Page weight: 84 % of that bill is data transfer at 1.2 MB per visit; compute is $29. Trimming the page to 300 kB saves more than any Lambda tuning. You can show exercise 1's numbers.

*What was and was not verified.* Verified here: ruff, 21 tests, `python app.py` and `cdk synth` (CLI 2.1145.0, lib 2.273.0) both produce the template, and the script reproduces the cost table. Not verified: a real deploy, so the bucket policy and the IAM grant are asserted from the template, not exercised. If you deploy it once and `curl` the API, say so in the README.

*One thing you could extend.* Deploy it, record the real cold-start and warm latencies from CloudWatch for the 256 MB ARM function, and replace the chapter's "120 ms" assumption in `cost.py` with the measured value.
