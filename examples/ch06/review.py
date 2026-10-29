"""Chapter 6: the review checklist as code, run over chapters 1 to 5.

``python review.py synth`` synthesises every chapter's stack in its
*production* flavour (``retain_data`` / ``protect_data`` on) into
``local/templates/``; ``python review.py`` then asks each template the
questions in ``RULES`` and prints one row per question, one column per
stack. A question is answered ``pass``, ``FAIL n`` (n resources), ``acc n``
(n findings accepted in ``accepted.json`` with a written reason) or ``n/a``
(the stack has nothing the question applies to). The exit code is 1 while
any finding is neither fixed nor accepted, which is what lets ``make
check`` gate on the review.

Every rule reads the CloudFormation a chapter would deploy, not the CDK
code that produced it, so the same file reviews a hand-written template, a
Terraform ``show -json`` converted to this shape, or a stack somebody
else's tool synthesised. The rules are deliberately blunt; the judgement
lives in ``accepted.json``, where each exception carries its reason.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
EXAMPLES = HERE.parent
TEMPLATES = HERE / "local" / "templates"
ACCEPTED = HERE / "accepted.json"
PRICES = json.loads((HERE / "prices.json").read_text())

# The chapters under review and the context that makes their stacks the
# production flavour. Chapters 1-4 call it retain_data, chapter 5 protect_data.
CHAPTERS = ("ch01", "ch02", "ch03", "ch04", "ch05")
PRODUCTION_CONTEXT = {"retain_data": True, "protect_data": True}

PILLARS = {
    "SEC": "Security",
    "REL": "Reliability",
    "COST": "Cost optimization",
    "OPS": "Operational excellence",
    "PERF": "Performance efficiency",
}

CURRENT_RUNTIMES = {"python3.12", "python3.13", "python3.14", "nodejs22.x", "nodejs24.x"}
CREDENTIAL_NAMES = re.compile(r"(SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY)", re.I)
# Resource types that cost money by the hour whether or not anything uses them.
BILLED_WHILE_IDLE = {
    "AWS::EC2::NatGateway",
    "AWS::EC2::Instance",
    "AWS::RDS::DBInstance",
    "AWS::RDS::DBCluster",
    "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "AWS::ElasticLoadBalancing::LoadBalancer",
    "AWS::Kinesis::Stream",
    "AWS::ElastiCache::CacheCluster",
    "AWS::ElastiCache::ReplicationGroup",
    "AWS::OpenSearchService::Domain",
    "AWS::MSK::Cluster",
    "AWS::SageMaker::Endpoint",
    "AWS::GlobalAccelerator::Accelerator",
    "AWS::KMS::Key",
    "AWS::SecretsManager::Secret",
    "AWS::EC2::EIP",
}
# Types CloudFormation can tag and that the chapters create; the cost
# allocation report is only as complete as this list.
TAGGABLE = {
    "AWS::S3::Bucket",
    "AWS::DynamoDB::GlobalTable",
    "AWS::Lambda::Function",
    "AWS::SQS::Queue",
    "AWS::SNS::Topic",
    "AWS::Logs::LogGroup",
    "AWS::CloudFront::Distribution",
    "AWS::ApiGatewayV2::Api",
    "AWS::CloudWatch::Alarm",
    "AWS::IAM::Role",
    "AWS::Events::EventBus",
    "AWS::Events::Rule",
    "AWS::StepFunctions::StateMachine",
    "AWS::ECS::Cluster",
    "AWS::ECS::TaskDefinition",
    "AWS::EC2::VPC",
    "AWS::KinesisFirehose::DeliveryStream",
    "AWS::Backup::BackupVault",
    "AWS::Backup::BackupPlan",
    "AWS::Route53::HealthCheck",
    "AWS::CertificateManager::Certificate",
}


# --- The template, with the few helpers the rules need -----------------------


@dataclass
class Stack:
    name: str
    chapter: str
    template: dict

    @property
    def resources(self) -> dict[str, dict]:
        return self.template.get("Resources", {})

    def of_type(self, *types: str) -> dict[str, dict]:
        return {k: r for k, r in self.resources.items() if r.get("Type") in types}

    def props(self, logical_id: str) -> dict:
        return self.resources[logical_id].get("Properties", {})


@dataclass(frozen=True)
class Finding:
    resource: str
    detail: str


@dataclass(frozen=True)
class Rule:
    id: str
    pillar: str
    question: str
    taught_in: str  # the chapter whose design answered it first
    check: Callable[[Stack], list[Finding] | None]  # None: nothing to apply it to


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def refs(value) -> set[str]:
    """Every logical id a property value refers to (Ref / GetAtt), recursively."""
    out: set[str] = set()
    if isinstance(value, dict):
        if "Ref" in value and isinstance(value["Ref"], str):
            out.add(value["Ref"])
        if "Fn::GetAtt" in value:
            target = value["Fn::GetAtt"]
            out.add(target[0] if isinstance(target, list) else str(target).split(".")[0])
        for v in value.values():
            out |= refs(v)
    elif isinstance(value, list):
        for v in value:
            out |= refs(v)
    return out


def statements(document: dict) -> list[dict]:
    return [s for s in as_list(document.get("Statement")) if isinstance(s, dict)]


def is_star(x) -> bool:
    return x == "*" or (isinstance(x, str) and x.endswith(":*"))


def tags_of(resource: dict) -> dict[str, str]:
    """Tags as a dict, whatever shape the resource type uses."""
    props = resource.get("Properties", {})
    raw = props.get("Tags")
    if resource.get("Type") == "AWS::DynamoDB::GlobalTable":
        raw = [t for r in props.get("Replicas", []) for t in r.get("Tags", [])]
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out = {}
    for t in as_list(raw):
        if isinstance(t, dict) and "Key" in t:
            out[str(t["Key"])] = str(t.get("Value", ""))
    return out


# --- Security --------------------------------------------------------------------


def buckets_block_public_access(s: Stack) -> list[Finding] | None:
    buckets = s.of_type("AWS::S3::Bucket")
    if not buckets:
        return None
    out = []
    for k, b in buckets.items():
        cfg = b.get("Properties", {}).get("PublicAccessBlockConfiguration", {})
        if not all(
            cfg.get(f) is True
            for f in (
                "BlockPublicAcls",
                "BlockPublicPolicy",
                "IgnorePublicAcls",
                "RestrictPublicBuckets",
            )
        ):
            out.append(Finding(k, "PublicAccessBlockConfiguration is not all four true"))
    return out


def _policy_denies_plain_http(doc: dict) -> bool:
    for st in statements(doc):
        cond = st.get("Condition", {}).get("Bool", {})
        if st.get("Effect") == "Deny" and str(cond.get("aws:SecureTransport")).lower() == "false":
            return True
    return False


def buckets_refuse_plain_http(s: Stack) -> list[Finding] | None:
    buckets = s.of_type("AWS::S3::Bucket")
    if not buckets:
        return None
    covered = set()
    for p in s.of_type("AWS::S3::BucketPolicy").values():
        props = p.get("Properties", {})
        if _policy_denies_plain_http(props.get("PolicyDocument", {})):
            covered |= refs(props.get("Bucket"))
    return [
        Finding(k, "no bucket policy denies aws:SecureTransport=false")
        for k in buckets
        if k not in covered
    ]


def buckets_name_their_encryption(s: Stack) -> list[Finding] | None:
    buckets = s.of_type("AWS::S3::Bucket")
    if not buckets:
        return None
    return [
        Finding(
            k,
            "no BucketEncryption property (S3 encrypts by default; the template does not say so)",
        )
        for k, b in buckets.items()
        if "BucketEncryption" not in b.get("Properties", {})
    ]


def no_unconditioned_wildcards(s: Stack) -> list[Finding] | None:
    """Allow statements with Action * or Resource * and no Condition, in any policy."""
    docs: list[tuple[str, dict]] = []
    for k, r in s.of_type(
        "AWS::IAM::Policy",
        "AWS::IAM::ManagedPolicy",
        "AWS::SQS::QueuePolicy",
        "AWS::S3::BucketPolicy",
    ).items():
        docs.append((k, r.get("Properties", {}).get("PolicyDocument", {})))
    for k, r in s.of_type("AWS::IAM::Role").items():
        for p in r.get("Properties", {}).get("Policies", []):
            docs.append((k, p.get("PolicyDocument", {})))
    if not docs:
        return None
    out = []
    for k, doc in docs:
        for st in statements(doc):
            if st.get("Effect") != "Allow" or "Condition" in st:
                continue
            actions = [a for a in as_list(st.get("Action")) if is_star(a)]
            resources = [r for r in as_list(st.get("Resource")) if r == "*"]
            if actions or resources:
                what = ", ".join(sorted(set(str(a) for a in actions)) or ["Resource *"])
                out.append(Finding(k, f"Allow {what} without a Condition"))
    return out


def queues_are_encrypted_and_refuse_plain_http(s: Stack) -> list[Finding] | None:
    queues = s.of_type("AWS::SQS::Queue")
    if not queues:
        return None
    covered = set()
    for p in s.of_type("AWS::SQS::QueuePolicy").values():
        props = p.get("Properties", {})
        if _policy_denies_plain_http(props.get("PolicyDocument", {})):
            covered |= refs(props.get("Queues"))
    out = []
    for k, q in queues.items():
        props = q.get("Properties", {})
        if not (props.get("SqsManagedSseEnabled") is True or props.get("KmsMasterKeyId")):
            out.append(Finding(k, "not encrypted at rest"))
        if k not in covered:
            out.append(Finding(k, "no queue policy denies aws:SecureTransport=false"))
    return out


def device_policies_name_the_device(s: Stack) -> list[Finding] | None:
    policies = s.of_type("AWS::IoT::Policy")
    if not policies:
        return None
    out = []
    for k, p in policies.items():
        for st in statements(p.get("Properties", {}).get("PolicyDocument", {})):
            if st.get("Effect") != "Allow":
                continue
            resources = [json.dumps(r) for r in as_list(st.get("Resource"))]
            if any(r == '"*"' for r in resources) or not all(
                "${iot:Connection.Thing.ThingName}" in r for r in resources
            ):
                out.append(
                    Finding(
                        k,
                        f"{as_list(st.get('Action'))}: resource does not name the connecting thing",
                    )
                )
            if "iot:Connect" in as_list(st.get("Action")):
                cond = st.get("Condition", {}).get("Bool", {})
                if str(cond.get("iot:Connection.Thing.IsAttached")).lower() != "true":
                    out.append(Finding(k, "iot:Connect without the IsAttached condition"))
    return out


def api_stages_throttle(s: Stack) -> list[Finding] | None:
    stages = s.of_type("AWS::ApiGatewayV2::Stage")
    if not stages:
        return None
    return [
        Finding(k, "no ThrottlingRateLimit on the stage (account default: 10 000 req/s)")
        for k, st in stages.items()
        if "ThrottlingRateLimit" not in st.get("Properties", {}).get("DefaultRouteSettings", {})
    ]


def cloudfront_is_https_only(s: Stack) -> list[Finding] | None:
    dists = s.of_type("AWS::CloudFront::Distribution")
    if not dists:
        return None
    out = []
    for k, d in dists.items():
        cfg = d.get("Properties", {}).get("DistributionConfig", {})
        behaviours = [cfg.get("DefaultCacheBehavior", {})] + cfg.get("CacheBehaviors", [])
        for b in behaviours:
            if b.get("ViewerProtocolPolicy") not in ("redirect-to-https", "https-only"):
                out.append(
                    Finding(
                        k, f"behaviour {b.get('PathPattern', 'default')} allows plain HTTP viewers"
                    )
                )
        for o in cfg.get("Origins", []):
            custom = o.get("CustomOriginConfig")
            if custom and custom.get("OriginProtocolPolicy") != "https-only":
                out.append(Finding(k, f"origin {o.get('Id')} is not https-only"))
    return out


def runtimes_are_current(s: Stack) -> list[Finding] | None:
    fns = s.of_type("AWS::Lambda::Function")
    if not fns:
        return None
    return [
        Finding(k, f"runtime {f['Properties'].get('Runtime')}")
        for k, f in fns.items()
        if f.get("Properties", {}).get("Runtime") not in CURRENT_RUNTIMES
    ]


def no_credential_in_environment(s: Stack) -> list[Finding] | None:
    fns = s.of_type("AWS::Lambda::Function")
    if not fns:
        return None
    out = []
    for k, f in fns.items():
        env = f.get("Properties", {}).get("Environment", {}).get("Variables", {})
        for name, value in env.items():
            if CREDENTIAL_NAMES.search(name) and isinstance(value, str):
                out.append(Finding(k, f"environment variable {name} looks like a credential"))
    return out


# --- Reliability -------------------------------------------------------------------


def tables_have_pitr(s: Stack) -> list[Finding] | None:
    tables = s.of_type("AWS::DynamoDB::GlobalTable", "AWS::DynamoDB::Table")
    if not tables:
        return None
    out = []
    for k, t in tables.items():
        props = t.get("Properties", {})
        specs = [
            r.get("PointInTimeRecoverySpecification", {}) for r in props.get("Replicas", [])
        ] or [props.get("PointInTimeRecoverySpecification", {})]
        if not all(sp.get("PointInTimeRecoveryEnabled") is True for sp in specs):
            out.append(Finding(k, "point-in-time recovery is off"))
    return out


def data_survives_a_stack_delete(s: Stack) -> list[Finding] | None:
    stores = s.of_type(
        "AWS::DynamoDB::GlobalTable",
        "AWS::DynamoDB::Table",
        "AWS::S3::Bucket",
        "AWS::Backup::BackupVault",
    )
    if not stores:
        return None
    return [
        Finding(k, f"DeletionPolicy {r.get('DeletionPolicy', 'Delete')}")
        for k, r in stores.items()
        if r.get("DeletionPolicy") not in ("Retain", "RetainExceptOnCreate")
    ]


def work_queues_have_a_dead_letter_queue(s: Stack) -> list[Finding] | None:
    queues = s.of_type("AWS::SQS::Queue")
    if not queues:
        return None
    sinks: set[str] = set()
    for q in queues.values():
        sinks |= refs(q.get("Properties", {}).get("RedrivePolicy"))
    for r in s.of_type("AWS::Events::Rule").values():
        for t in r.get("Properties", {}).get("Targets", []):
            sinks |= refs(t.get("DeadLetterConfig"))
    for r in s.of_type("AWS::IoT::TopicRule").values():
        sinks |= refs(r.get("Properties", {}).get("TopicRulePayload", {}).get("ErrorAction"))
    return [
        Finding(k, "a work queue with no RedrivePolicy")
        for k, q in queues.items()
        if k not in sinks and "RedrivePolicy" not in q.get("Properties", {})
    ]


def alarms_have_an_action(s: Stack) -> list[Finding] | None:
    alarms = s.of_type("AWS::CloudWatch::Alarm")
    if not alarms:
        return None
    return [
        Finding(k, "no AlarmActions")
        for k, a in alarms.items()
        if not a.get("Properties", {}).get("AlarmActions")
    ]


def stack_has_alarms_and_a_topic(s: Stack) -> list[Finding] | None:
    out = []
    if not s.of_type("AWS::CloudWatch::Alarm"):
        out.append(Finding(s.name, "no CloudWatch alarm in the stack"))
    if not s.of_type("AWS::SNS::Topic"):
        out.append(Finding(s.name, "no SNS topic for a person to subscribe to"))
    return out


def queue_sources_report_partial_failures(s: Stack) -> list[Finding] | None:
    queues = set(s.of_type("AWS::SQS::Queue"))
    mappings = {
        k: m
        for k, m in s.of_type("AWS::Lambda::EventSourceMapping").items()
        if refs(m.get("Properties", {}).get("EventSourceArn")) & queues
    }
    if not mappings:
        return None
    return [
        Finding(k, "FunctionResponseTypes lacks ReportBatchItemFailures")
        for k, m in mappings.items()
        if "ReportBatchItemFailures" not in m.get("Properties", {}).get("FunctionResponseTypes", [])
    ]


# --- Cost optimization ------------------------------------------------------------


def log_groups_have_a_retention(s: Stack) -> list[Finding] | None:
    groups = s.of_type("AWS::Logs::LogGroup")
    if not groups:
        return None
    return [
        Finding(k, "RetentionInDays not set (kept forever)")
        for k, g in groups.items()
        if "RetentionInDays" not in g.get("Properties", {})
    ]


def functions_have_a_managed_log_group(s: Stack) -> list[Finding] | None:
    fns = s.of_type("AWS::Lambda::Function")
    if not fns:
        return None
    return [
        Finding(k, "no LoggingConfig.LogGroup: Lambda creates one at first run, kept forever")
        for k, f in fns.items()
        if not f.get("Properties", {}).get("LoggingConfig", {}).get("LogGroup")
    ]


def functions_run_on_arm(s: Stack) -> list[Finding] | None:
    fns = s.of_type("AWS::Lambda::Function")
    if not fns:
        return None
    return [
        Finding(k, f"architecture {f.get('Properties', {}).get('Architectures', ['x86_64'])}")
        for k, f in fns.items()
        if f.get("Properties", {}).get("Architectures") != ["arm64"]
    ]


def growing_buckets_have_a_lifecycle(s: Stack) -> list[Finding] | None:
    buckets = s.of_type("AWS::S3::Bucket")
    if not buckets:
        return None
    return [
        Finding(k, "no LifecycleConfiguration")
        for k, b in buckets.items()
        if "LifecycleConfiguration" not in b.get("Properties", {})
    ]


def nothing_is_billed_while_idle(s: Stack) -> list[Finding] | None:
    out = []
    for k, r in s.resources.items():
        t = r.get("Type")
        if t in BILLED_WHILE_IDLE:
            out.append(Finding(k, f"{t} is billed by the hour"))
        if (
            t == "AWS::EC2::VPCEndpoint"
            and r.get("Properties", {}).get("VpcEndpointType", "Gateway") != "Gateway"
        ):
            out.append(Finding(k, "interface endpoint: billed by the hour per AZ"))
        if t == "AWS::Lambda::Alias" and r.get("Properties", {}).get(
            "ProvisionedConcurrencyConfig"
        ):
            out.append(Finding(k, "provisioned concurrency is billed while idle"))
    return out


def resources_carry_the_project_tag(s: Stack) -> list[Finding] | None:
    taggable = {k: r for k, r in s.resources.items() if r.get("Type") in TAGGABLE}
    if not taggable:
        return None
    return [
        Finding(k, f"{r['Type']} has no 'project' tag")
        for k, r in taggable.items()
        if "project" not in tags_of(r)
    ]


# --- Operational excellence ------------------------------------------------------


def stack_has_a_description(s: Stack) -> list[Finding] | None:
    return [] if s.template.get("Description") else [Finding(s.name, "no stack Description")]


def alarms_have_a_description(s: Stack) -> list[Finding] | None:
    alarms = s.of_type("AWS::CloudWatch::Alarm")
    if not alarms:
        return None
    return [
        Finding(k, "no AlarmDescription")
        for k, a in alarms.items()
        if not a.get("Properties", {}).get("AlarmDescription")
    ]


def silence_is_noticed(s: Stack) -> list[Finding] | None:
    """Something fires when the system stops: a breaching-on-missing alarm or a health check."""
    if s.of_type("AWS::Route53::HealthCheck"):
        return []
    alarms = s.of_type("AWS::CloudWatch::Alarm")
    if any(a.get("Properties", {}).get("TreatMissingData") == "breaching" for a in alarms.values()):
        return []
    return [
        Finding(
            s.name,
            "no breaching-on-missing alarm and no health check: a stopped system is silent",
        )
    ]


# --- Performance efficiency ------------------------------------------------------


def functions_set_a_timeout(s: Stack) -> list[Finding] | None:
    fns = s.of_type("AWS::Lambda::Function")
    if not fns:
        return None
    return [
        Finding(k, "no Timeout (default 3 s)")
        for k, f in fns.items()
        if "Timeout" not in f.get("Properties", {})
    ]


RULES: list[Rule] = [
    Rule(
        "SEC-01",
        "SEC",
        "Does every bucket block public access?",
        "ch01",
        buckets_block_public_access,
    ),
    Rule(
        "SEC-02", "SEC", "Does every bucket refuse plain HTTP?", "ch01", buckets_refuse_plain_http
    ),
    Rule(
        "SEC-03",
        "SEC",
        "Does every bucket say how it is encrypted?",
        "ch01",
        buckets_name_their_encryption,
    ),
    Rule(
        "SEC-04",
        "SEC",
        "Is every Allow of * on actions or resources conditioned?",
        "ch02",
        no_unconditioned_wildcards,
    ),
    Rule(
        "SEC-05",
        "SEC",
        "Is every queue encrypted and refusing plain HTTP?",
        "ch02",
        queues_are_encrypted_and_refuse_plain_http,
    ),
    Rule(
        "SEC-06",
        "SEC",
        "Does every device policy name the connecting device?",
        "ch02",
        device_policies_name_the_device,
    ),
    Rule("SEC-07", "SEC", "Does every API stage throttle?", "ch01", api_stages_throttle),
    Rule(
        "SEC-08",
        "SEC",
        "Does CloudFront speak HTTPS only, to viewers and origins?",
        "ch01",
        cloudfront_is_https_only,
    ),
    Rule("SEC-09", "SEC", "Is every function on a current runtime?", "ch01", runtimes_are_current),
    Rule(
        "SEC-10",
        "SEC",
        "Is no credential in a function's environment?",
        "ch02",
        no_credential_in_environment,
    ),
    Rule(
        "REL-01", "REL", "Does every table have point-in-time recovery?", "ch05", tables_have_pitr
    ),
    Rule(
        "REL-02",
        "REL",
        "Does every data store survive a stack delete?",
        "ch05",
        data_survives_a_stack_delete,
    ),
    Rule(
        "REL-03",
        "REL",
        "Does every work queue have a dead-letter queue?",
        "ch04",
        work_queues_have_a_dead_letter_queue,
    ),
    Rule("REL-04", "REL", "Does every alarm do something?", "ch02", alarms_have_an_action),
    Rule(
        "REL-05",
        "REL",
        "Does the stack have an alarm and a topic for a person?",
        "ch02",
        stack_has_alarms_and_a_topic,
    ),
    Rule(
        "REL-06",
        "REL",
        "Does every queue consumer report partial batch failures?",
        "ch04",
        queue_sources_report_partial_failures,
    ),
    Rule("COST-01", "COST", "Does every log group expire?", "ch01", log_groups_have_a_retention),
    Rule(
        "COST-02",
        "COST",
        "Does every function log to a group the template owns?",
        "ch01",
        functions_have_a_managed_log_group,
    ),
    Rule("COST-03", "COST", "Does every function run on ARM?", "ch01", functions_run_on_arm),
    Rule(
        "COST-04",
        "COST",
        "Does every bucket have a lifecycle rule?",
        "ch02",
        growing_buckets_have_a_lifecycle,
    ),
    Rule(
        "COST-05",
        "COST",
        "Is nothing billed by the hour while idle?",
        "ch03",
        nothing_is_billed_while_idle,
    ),
    Rule(
        "COST-06",
        "COST",
        "Does every taggable resource carry the project tag?",
        "ch06",
        resources_carry_the_project_tag,
    ),
    Rule("OPS-01", "OPS", "Does the stack say what it is?", "ch01", stack_has_a_description),
    Rule("OPS-02", "OPS", "Does every alarm say what it means?", "ch02", alarms_have_a_description),
    Rule("OPS-03", "OPS", "Is silence noticed?", "ch03", silence_is_noticed),
    Rule(
        "PERF-01",
        "PERF",
        "Does every function set its own timeout?",
        "ch01",
        functions_set_a_timeout,
    ),
]


# --- Accepted findings ----------------------------------------------------------------


@dataclass(frozen=True)
class Acceptance:
    rule: str
    stack: str  # fnmatch pattern over the stack name
    resource: str  # fnmatch pattern over the logical id
    reason: str
    until: str = ""  # a date or a TODO item: when the acceptance is to be looked at again

    def covers(self, rule: str, stack: str, resource: str) -> bool:
        return (
            self.rule == rule
            and fnmatch.fnmatchcase(stack, self.stack)
            and fnmatch.fnmatchcase(resource, self.resource)
        )


def load_accepted(path: Path = ACCEPTED) -> list[Acceptance]:
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    out = []
    for a in raw.get("accepted", []):
        if not a.get("reason", "").strip():
            raise ValueError(f"accepted.json: {a.get('rule')} on {a.get('resource')} has no reason")
        out.append(
            Acceptance(
                a["rule"],
                a.get("stack", "*"),
                a.get("resource", "*"),
                a["reason"],
                a.get("until", ""),
            )
        )
    return out


# --- Running it --------------------------------------------------------------------------


@dataclass
class Cell:
    """One rule applied to one stack."""

    rule: Rule
    stack: Stack
    findings: list[Finding] | None
    accepted: list[tuple[Finding, Acceptance]] = field(default_factory=list)
    open: list[Finding] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.findings is None:
            return "n/a"
        if self.open:
            return f"FAIL {len(self.open)}"
        if self.accepted:
            return f"acc {len(self.accepted)}"
        return "pass"


@dataclass
class Report:
    stacks: list[Stack]
    cells: list[Cell]
    unused_acceptances: list[Acceptance]

    @property
    def open_findings(self) -> list[tuple[Cell, Finding]]:
        return [(c, f) for c in self.cells for f in c.open]

    @property
    def accepted_findings(self) -> list[tuple[Cell, Finding, Acceptance]]:
        return [(c, f, a) for c in self.cells for f, a in c.accepted]


def review(stacks: list[Stack], accepted: list[Acceptance], rules: list[Rule] = RULES) -> Report:
    cells = []
    used: set[int] = set()
    for rule in rules:
        for stack in stacks:
            findings = rule.check(stack)
            cell = Cell(rule, stack, findings)
            for f in findings or []:
                match = next(
                    (a for a in accepted if a.covers(rule.id, stack.name, f.resource)),
                    None,
                )
                if match:
                    cell.accepted.append((f, match))
                    used.add(id(match))
                else:
                    cell.open.append(f)
            cells.append(cell)
    return Report(stacks, cells, [a for a in accepted if id(a) not in used])


def load_stacks(root: Path = TEMPLATES) -> list[Stack]:
    stacks = []
    for chapter in CHAPTERS:
        for path in sorted((root / chapter).glob("*.template.json")):
            stacks.append(
                Stack(
                    path.name.removesuffix(".template.json"), chapter, json.loads(path.read_text())
                )
            )
    if not stacks:
        raise SystemExit(f"no templates under {root}; run `python review.py synth` first")
    return stacks


def synth(root: Path = TEMPLATES, chapters: tuple[str, ...] = CHAPTERS) -> None:
    """Run every chapter's app.py in its production flavour into root/<chapter>/."""
    env = {
        **os.environ,
        "CDK_CONTEXT_JSON": json.dumps(PRODUCTION_CONTEXT),
    }
    for chapter in chapters:
        out = root / chapter
        env["CDK_OUTDIR"] = str(out)
        subprocess.run([sys.executable, "app.py"], cwd=EXAMPLES / chapter, env=env, check=True)


# --- The idle floor ---------------------------------------------------------------------


def idle_floor(s: Stack) -> dict[str, float]:
    """What the stack costs a month with no traffic at all, from list prices."""
    p = PRICES
    alarms = len(s.of_type("AWS::CloudWatch::Alarm"))
    zones = len(s.of_type("AWS::Route53::HostedZone"))
    checks = len(s.of_type("AWS::Route53::HealthCheck"))
    metric_rules = sum(
        1
        for b in s.of_type("AWS::S3::Bucket").values()
        for r in b.get("Properties", {}).get("ReplicationConfiguration", {}).get("Rules", [])
        if r.get("Destination", {}).get("Metrics", {}).get("Status") == "Enabled"
    )
    lines = {
        f"{alarms} alarms": alarms * p["cloudwatch"]["alarm_standard_per_month"],
        f"{zones} hosted zones": zones * p["route53"]["hosted_zone_per_month"],
        f"{checks} health checks": p["route53"]["health_check_first_50_per_month"] * checks,
        f"{metric_rules * p['s3']['replication_metrics_per_rule']} replication metrics": (
            metric_rules
            * p["s3"]["replication_metrics_per_rule"]
            * p["cloudwatch"]["custom_metric_per_month"]
        ),
    }
    return {k: v for k, v in lines.items() if not k.startswith("0 ")}


# --- Printing ----------------------------------------------------------------------------


def short(name: str) -> str:
    return name.removeprefix("Ch0").replace("Stack", "")


def matrix_table(report: Report) -> str:
    stacks = report.stacks
    rows = ["| Rule | Pillar | Question | " + " | ".join(short(s.name) for s in stacks) + " |"]
    rows.append("|---|---|---|" + "---|" * len(stacks))
    by_rule: dict[str, list[Cell]] = {}
    for c in report.cells:
        by_rule.setdefault(c.rule.id, []).append(c)
    for rule in RULES:
        cells = by_rule.get(rule.id, [])
        rows.append(
            f"| {rule.id} | {PILLARS[rule.pillar]} | {rule.question} | "
            + " | ".join(c.status for c in cells)
            + " |"
        )
    return "\n".join(rows)


def findings_list(report: Report) -> str:
    lines = []
    if report.open_findings:
        lines.append("Open findings (fix, or accept with a reason in accepted.json):")
        for c, f in report.open_findings:
            lines.append(f"  {c.rule.id} {c.stack.name} {f.resource}: {f.detail}")
    if report.accepted_findings:
        lines.append("Accepted:")
        for c, f, a in report.accepted_findings:
            when = f" (until {a.until})" if a.until else ""
            lines.append(f"  {c.rule.id} {c.stack.name} {f.resource}: {a.reason}{when}")
    if report.unused_acceptances:
        lines.append("Acceptances that no longer match anything (delete them):")
        for a in report.unused_acceptances:
            lines.append(f"  {a.rule} {a.stack} {a.resource}")
    return "\n".join(lines)


def summary(report: Report) -> dict[str, int]:
    statuses = [c.status.split()[0] for c in report.cells]
    return {
        "questions": len(RULES),
        "stacks": len(report.stacks),
        "cells": len(report.cells),
        "pass": statuses.count("pass"),
        "accepted": statuses.count("acc"),
        "fail": statuses.count("FAIL"),
        "n/a": statuses.count("n/a"),
        "open findings": len(report.open_findings),
        "accepted findings": len(report.accepted_findings),
    }


def idle_table(stacks: list[Stack]) -> str:
    rows = ["| Stack | Idle floor / month | Made of |", "|---|---:|---|"]
    total = 0.0
    for s in stacks:
        lines = idle_floor(s)
        sub = sum(lines.values())
        total += sub
        made = ", ".join(f"{k} ${v:,.2f}" for k, v in lines.items()) or "nothing"
        rows.append(f"| {short(s.name)} | ${sub:,.2f} | {made} |")
    rows.append(f"| **All five designs** | **${total:,.2f}** | |")
    return "\n".join(rows)


def main(argv: list[str]) -> int:
    if argv[1:] == ["synth"]:
        synth()
        return 0
    root = Path(argv[argv.index("--templates") + 1]) if "--templates" in argv else TEMPLATES
    stacks = load_stacks(root)
    report = review(stacks, load_accepted())
    print(matrix_table(report))
    print()
    print(findings_list(report))
    print()
    print(idle_table(stacks))
    print()
    s = summary(report)
    print(
        f"{s['questions']} questions x {s['stacks']} stacks = {s['cells']} cells: "
        f"{s['pass']} pass, "
        f"{s['accepted']} accepted, {s['fail']} FAIL, {s['n/a']} n/a; "
        f"{s['open findings']} open findings, {s['accepted findings']} accepted."
    )
    return 1 if report.open_findings or report.unused_acceptances else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
