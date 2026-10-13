.PHONY: check lint test synth cost

PY ?= python3

check: lint test synth cost

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

# Print each chapter's cost table from its prices.json and assumptions.
cost:
	cd examples/ch01 && $(PY) cost.py
