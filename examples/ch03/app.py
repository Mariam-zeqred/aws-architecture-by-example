"""CDK app for chapter 3. ``python app.py`` synthesises to ``cdk.out/``.

``-c archive_bucket=<name>`` points the stack at chapter 2's archive bucket;
without it the stack creates an empty archive bucket of its own.
"""

import os

import aws_cdk as cdk

from stack import BatchMlStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
BatchMlStack(
    app,
    "Ch03BatchMlStack",
    archive_bucket_name=app.node.try_get_context("archive_bucket") or None,
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 3: batch ML on a schedule (aws-architecture-by-example)",
)
app.synth()
