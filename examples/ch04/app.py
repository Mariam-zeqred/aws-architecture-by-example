"""CDK app for chapter 4. ``python app.py`` synthesises to ``cdk.out/``."""

import os

import aws_cdk as cdk

from stack import OrdersStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
# Chapter 6's review (COST-06): every taggable resource carries the project
# and chapter, so the bill can be split by chapter in Cost Explorer.
cdk.Tags.of(app).add("project", "aws-architecture-by-example")
cdk.Tags.of(app).add("chapter", "ch04")
OrdersStack(
    app,
    "Ch04OrdersStack",
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 4: event-driven order processing (aws-architecture-by-example)",
)
app.synth()
