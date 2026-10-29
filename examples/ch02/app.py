"""CDK app for chapter 2. ``python app.py`` synthesises to ``cdk.out/``.

The CDK CLI (``npx cdk synth|diff|deploy``) runs this same file; running it
directly needs no CLI and no AWS credentials, which is how ``make synth``
checks that the stack is well-formed.
"""

import os

import aws_cdk as cdk

from stack import TelemetryStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
# Chapter 6's review (COST-06): every taggable resource carries the project
# and chapter, so the bill can be split by chapter in Cost Explorer.
cdk.Tags.of(app).add("project", "aws-architecture-by-example")
cdk.Tags.of(app).add("chapter", "ch02")
TelemetryStack(
    app,
    "Ch02TelemetryStack",
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 2: IoT telemetry at scale (aws-architecture-by-example)",
)
app.synth()
