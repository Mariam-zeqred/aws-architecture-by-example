"""CDK app for chapter 3. ``python app.py`` synthesises to ``cdk.out/``.

``-c archive_bucket=<name>`` points the stack at chapter 2's archive bucket;
without it the stack creates an empty archive bucket of its own.
"""

import os

import aws_cdk as cdk

from stack import BatchMlStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
# Chapter 6's review (COST-06): every taggable resource carries the project
# and chapter, so the bill can be split by chapter in Cost Explorer.
cdk.Tags.of(app).add("project", "aws-architecture-by-example")
cdk.Tags.of(app).add("chapter", "ch03")
BatchMlStack(
    app,
    "Ch03BatchMlStack",
    archive_bucket_name=app.node.try_get_context("archive_bucket") or None,
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 3: batch ML on a schedule (aws-architecture-by-example)",
)
app.synth()
