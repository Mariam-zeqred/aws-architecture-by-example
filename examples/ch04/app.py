"""CDK app for chapter 4. ``python app.py`` synthesises to ``cdk.out/``."""

import os

import aws_cdk as cdk

from stack import OrdersStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
OrdersStack(
    app,
    "Ch04OrdersStack",
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 4: event-driven order processing (aws-architecture-by-example)",
)
app.synth()
