"""CDK app for chapter 1. ``python app.py`` synthesises to ``cdk.out/``.

The CDK CLI (``npx cdk synth|diff|deploy``) runs this same file; running it
directly needs no CLI and no AWS credentials, which is how ``make synth``
checks that the stack is well-formed.
"""

import os

import aws_cdk as cdk

from stack import HelloStack

# Run by the CDK CLI, CDK_OUTDIR is set; run directly, write next to this file.
app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
HelloStack(
    app,
    "Ch01HelloStack",
    retain_data=bool(app.node.try_get_context("retain_data")),
    description="Chapter 1: static site + API + DB (aws-architecture-by-example)",
)
app.synth()
