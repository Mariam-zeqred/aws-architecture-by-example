"""CDK app for chapter 5. ``python app.py`` synthesises both regions to ``cdk.out/``.

Two stacks from one class: the standby in eu-west-1 (deploy it first; it
owns the replication destinations and the copy vault) and the primary in
us-east-1 (the global table, the backup plan, the health check, the site).
The hosted zone is passed as context; the defaults are placeholders that
synthesise but belong to nobody.
"""

import os

import aws_cdk as cdk

from stack import PRIMARY_REGION, STANDBY_REGION, RegionStack

app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", "cdk.out"))
zone_name = app.node.try_get_context("zone_name") or "coop.example"
hosted_zone_id = app.node.try_get_context("hosted_zone_id") or "Z0000000000000000000A"
protect_data = app.node.try_get_context("protect_data")
protect_data = True if protect_data is None else bool(protect_data)
account = os.environ.get("CDK_DEFAULT_ACCOUNT")

# Chapter 6's review (COST-06): every taggable resource carries the project
# and chapter, so the bill can be split by chapter in Cost Explorer.
cdk.Tags.of(app).add("project", "aws-architecture-by-example")
cdk.Tags.of(app).add("chapter", "ch05")
for role, region in (("standby", STANDBY_REGION), ("primary", PRIMARY_REGION)):
    RegionStack(
        app,
        f"Ch05{role.title()}Stack",
        role=role,
        zone_name=zone_name,
        hosted_zone_id=hosted_zone_id,
        protect_data=protect_data,
        env=cdk.Environment(account=account, region=region),
        description=f"Chapter 5: {role} region (aws-architecture-by-example)",
    )
app.synth()
