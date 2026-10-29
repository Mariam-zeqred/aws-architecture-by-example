.PHONY: check lint test synth cost example ch03-run ch04-sim ch05-drill ch06-review

PY ?= python3

check: lint test synth cost ch03-run ch04-sim ch05-drill ch06-review

lint:
	ruff check examples
	ruff format --check examples

test:
	$(PY) -m pytest -q

# Synthesise every chapter's stack to CloudFormation without the CDK CLI and
# without AWS credentials. Nothing in this repo deploys anything; see each
# examples/chNN/README.md for the deploy commands you run yourself.
synth:
	cd examples/ch01 && $(PY) app.py && test -f cdk.out/Ch01HelloStack.template.json
	cd examples/ch02 && $(PY) app.py && test -f cdk.out/Ch02TelemetryStack.template.json
	cd examples/ch03 && $(PY) app.py && test -f cdk.out/Ch03BatchMlStack.template.json
	cd examples/ch04 && $(PY) app.py && test -f cdk.out/Ch04OrdersStack.template.json
	cd examples/ch05 && $(PY) app.py && test -f cdk.out/Ch05PrimaryStack.template.json && test -f cdk.out/Ch05StandbyStack.template.json

# Print each chapter's cost table from its prices.json and assumptions.
cost:
	cd examples/ch01 && $(PY) cost.py
	cd examples/ch02 && $(PY) cost.py
	cd examples/ch03 && $(PY) cost.py
	cd examples/ch04 && $(PY) cost.py
	cd examples/ch05 && $(PY) cost.py

# Chapter 3's pipeline end to end on a SYNTHETIC archive (10 devices, 21 days):
# train a candidate, gate it, score every device. Writes to examples/ch03/local/.
example: ch03-run ch04-sim ch05-drill ch06-review

ch03-run:
	cd examples/ch03 && rm -rf local && $(PY) pipeline/synth_archive.py local/archive --devices 10 --days 21
	cd examples/ch03/pipeline && $(PY) -m cli train --archive ../local/archive --artifacts ../local/artifacts --run-id 2026-10-19 --days 21
	cd examples/ch03 && $(PY) lambda/gate.py --artifacts local/artifacts --run-id 2026-10-19
	cd examples/ch03/pipeline && $(PY) -m cli score --archive ../local/archive --artifacts ../local/artifacts --run-id 2026-10-19

# Chapter 4's three functions against fake queues that deliver twice, a
# provider that loses responses and a runtime that dies after writes, with
# and without their guards. In memory; prints the chapter's section 4 table.
ch04-sim:
	cd examples/ch04 && $(PY) sim.py --orders 500 --seed 1

# Chapter 5's two drills: four disasters against four recovery strategies
# (arithmetic over the parameters at the top of drill.py), then a counter
# decremented from two regions of a fake global table with last-writer-wins
# replication. In memory; prints the chapter's section 4 tables.
ch05-drill:
	cd examples/ch05 && $(PY) drill.py && $(PY) drill.py conflict

# Chapter 6's review: synthesise chapters 1-5 in their production flavour
# (data retained) into examples/ch06/local/, ask every template the 26
# checklist questions, apply accepted.json, print the idle floor, then add
# the five cost models up. review.py exits 1 on any finding that is neither
# fixed nor accepted with a reason, so this target is the gate.
ch06-review:
	cd examples/ch06 && $(PY) review.py synth && $(PY) review.py && $(PY) rollup.py
