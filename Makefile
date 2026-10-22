.PHONY: check lint test synth cost example ch03-run ch04-sim

PY ?= python3

check: lint test synth cost ch03-run ch04-sim

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

# Print each chapter's cost table from its prices.json and assumptions.
cost:
	cd examples/ch01 && $(PY) cost.py
	cd examples/ch02 && $(PY) cost.py
	cd examples/ch03 && $(PY) cost.py
	cd examples/ch04 && $(PY) cost.py

# Chapter 3's pipeline end to end on a SYNTHETIC archive (10 devices, 21 days):
# train a candidate, gate it, score every device. Writes to examples/ch03/local/.
example: ch03-run ch04-sim

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
