.PHONY: check lint test synth cost example ch03-run

PY ?= python3

check: lint test synth cost ch03-run

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

# Print each chapter's cost table from its prices.json and assumptions.
cost:
	cd examples/ch01 && $(PY) cost.py
	cd examples/ch02 && $(PY) cost.py
	cd examples/ch03 && $(PY) cost.py

# Chapter 3's pipeline end to end on a SYNTHETIC archive (10 devices, 21 days):
# train a candidate, gate it, score every device. Writes to examples/ch03/local/.
example: ch03-run

ch03-run:
	cd examples/ch03 && rm -rf local && $(PY) pipeline/synth_archive.py local/archive --devices 10 --days 21
	cd examples/ch03/pipeline && $(PY) -m cli train --archive ../local/archive --artifacts ../local/artifacts --run-id 2026-10-19 --days 21
	cd examples/ch03 && $(PY) lambda/gate.py --artifacts local/artifacts --run-id 2026-10-19
	cd examples/ch03/pipeline && $(PY) -m cli score --archive ../local/archive --artifacts ../local/artifacts --run-id 2026-10-19
