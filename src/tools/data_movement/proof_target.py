# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-proof test cluster (#1946): a throwaway Aurora Serverless v2 stack.

``infrastructure/cloudformation/data-movement/routine-proof-target.yaml`` is a static template:
this module chooses its parameters from the job's step 1 plan (the planned target's engine and
version line, as ``aurora-target.yaml`` does), and deploys, reads and deletes it with the
caller's clients. ``scripts/run_dms_step.py`` runs it as ``proof-target-plan``,
``proof-target-deploy``, ``proof-target-status`` and ``proof-target-teardown``.

- The cluster holds only synthetic rows and the carried routines, never customer data.
- It is never reachable from a network: ``PubliclyAccessible`` false, and a security group with
  no inbound rule. The proof reaches it through the RDS Data API (HTTPS to the RDS service,
  signed with the caller's IAM credentials), with the master secret RDS keeps in Secrets
  Manager (``ManageMasterUserPassword``). This module never reads a secret's value.
- The version is the newest one the region offers on the target's line that Serverless v2 and
  the Data API support; ``MinCapacity`` is 0 (the cluster pauses when idle) where that version
  supports it, else 0.5.
- Deleting the stack deletes the cluster with no final snapshot (``DeletionPolicy: Delete``).

Ownership, as step 1's teardown establishes it from AWS only, never from a local file: the stack
carries this job's ``modernizer:job-id`` tag and the ``modernizer:purpose`` tag
``routine-proof``; its resources are exactly the template's four (a create that never completed,
or a delete in progress or failed, may hold fewer, never another); and its cluster, while it
exists, carries this job's ``modernizer:job-id`` tag. A stack these checks refuse is never
written to.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.shared.engine_names import display_engine

REPO_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_PATH = (
    REPO_ROOT / "infrastructure" / "cloudformation" / "data-movement" / "routine-proof-target.yaml"
)
TEMPLATE_NAME = "routine-proof-target.yaml"
INPUTS_DIR = "data-movement/routine-proof"
JOB_TAG = "modernizer:job-id"
PURPOSE_TAG, PURPOSE = "modernizer:purpose", "routine-proof"
#: The logical ids and types the template creates: the commands act only on a stack of these.
RESOURCES: dict[str, str] = {
    "SubnetGroup": "AWS::RDS::DBSubnetGroup",
    "SecurityGroup": "AWS::EC2::SecurityGroup",
    "Cluster": "AWS::RDS::DBCluster",
    "Writer": "AWS::RDS::DBInstance",
}
#: Aurora MySQL's stack also holds its cluster parameter group (the event scheduler off, #1945).
MYSQL_RESOURCES: dict[str, str] = {
    **RESOURCES,
    "ClusterParameters": "AWS::RDS::DBClusterParameterGroup",
}


def resources_for(stack: dict[str, Any]) -> dict[str, str]:
    """The resources a stack of this template holds, by the engine it was created with."""
    engine = next((p.get("ParameterValue") for p in stack.get("Parameters") or []
                   if p.get("ParameterKey") == "Engine"), None)  # fmt: skip
    return MYSQL_RESOURCES if engine == "aurora-mysql" else RESOURCES


def parameter_group_family(engine: str, version: str) -> str:
    """The parameter group family of an engine version (``aurora-mysql8.0``)."""
    if engine == "aurora_mysql":
        return "aurora-mysql8.4" if version.startswith("8.4") else "aurora-mysql8.0"
    return f"aurora-postgresql{version.split('.')[0]}"


#: The lowest version of each line with RDS Data API on Serverless v2 (Amazon Aurora User Guide,
#: "Supported Regions and Aurora DB engines for RDS Data API", checked 2026-11-14).
DATA_API_MINIMUM: dict[str, dict[str, str]] = {
    "aurora_postgresql": {"14": "14.8", "15": "15.3", "16": "16.1", "17": "17.4", "18": "18.3"},
    "aurora_mysql": {"8.0": "8.0.mysql_aurora.3.07.0", "8.4": "8.4.mysql_aurora.8.4.7"},
}  # fmt: skip
#: The regions the same page lists with the Data API, per engine: a region not listed (one AWS
#: adds later, say) is refused until it is added here, never assumed to have it.
_DATA_API_COMMERCIAL = frozenset({
    "us-east-1", "us-east-2", "us-west-1", "us-west-2", "af-south-1", "ap-east-1", "ap-east-2",
    "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
    "ap-southeast-4", "ap-southeast-5", "ap-southeast-6", "ap-southeast-7", "ap-northeast-1",
    "ap-northeast-2", "ap-northeast-3", "ca-central-1", "ca-west-1", "eu-central-1",
    "eu-central-2", "eu-west-1", "eu-west-2", "eu-west-3", "eu-south-1", "eu-south-2",
    "eu-north-1", "me-south-1", "me-central-1", "mx-central-1", "sa-east-1",
})  # fmt: skip
DATA_API_REGIONS: dict[str, frozenset[str]] = {
    "aurora_postgresql": _DATA_API_COMMERCIAL | {"us-gov-east-1", "us-gov-west-1"},
    "aurora_mysql": _DATA_API_COMMERCIAL,
}
#: A plain release (16.4, 8.0.mysql_aurora.3.08.0): no "-limitless" or other variant.
_RELEASE = re.compile(r"^\d+\.\d+(\.mysql_aurora\.\d+\.\d+\.\d+)?$")
DEFAULT_MAX_CAPACITY = 2
#: The routine proof on Aurora MySQL runs only as a restricted runner user, and the RDS Data
#: API takes no bound password in ``CREATE USER`` (Aurora MySQL 3.14.0), so the proof cannot
#: run there yet (#2298). Until it can, the plan, the deploy and the run refuse an Aurora MySQL
#: target before any AWS call: the customer is never asked to approve a billed cluster the
#: proof cannot use. Status and teardown still read and delete a stack deployed earlier.
MYSQL_PROOF_AVAILABLE = False
MYSQL_NOT_AVAILABLE = (
    "The routine proof on Aurora MySQL is not available yet (#2298): it runs the routines only "
    "as a restricted user, which the RDS Data API cannot create yet, and it never falls back to "
    "the master user. Nothing was deployed and no AWS call was made; the routine scripts are "
    "still written. The proof runs on Aurora PostgreSQL."
)


def mysql_refusal(engine: str) -> dict[str, str] | None:
    """The refusal of an Aurora MySQL proof while it cannot run (#2298), or None."""
    if engine in ("aurora_mysql", "aurora-mysql", "mysql") and not MYSQL_PROOF_AVAILABLE:
        return {"status": "error", "reason_code": "mysql_proof_not_available",
                "message": MYSQL_NOT_AVAILABLE}  # fmt: skip
    return None


DATA_NOTE = ("The test cluster holds only synthetic rows and the carried routines, never "
             "customer data.")  # fmt: skip
DELETED = ("the stack's resources: the cluster with every row in it (no final snapshot), "
           "its writer, its master secret (RDS deletes it with the cluster), its subnet group "
           "and its security group, and on Aurora MySQL its cluster parameter group; the VPC and "
           "subnets are yours and stay")  # fmt: skip
COST = ("Aurora Serverless v2 bills ACU-hours while the cluster runs, plus storage and the "
        "master secret; a cluster at MinCapacity 0 pauses after SecondsUntilAutoPause idle "
        "seconds and then bills storage and the secret only. At MinCapacity 0.5 it never pauses: "
        "tear it down after the proof.")  # fmt: skip


def _engine_id(engine: str) -> str:
    """RDS's engine name (``aurora-postgresql``), as step 1's templates name it."""
    from src.tools.data_movement.dms_step import AURORA_ENGINES

    return AURORA_ENGINES[engine]


class ProofTargetError(ValueError):
    """A refusal: printed as ``{"status": "error", "message": ...}``."""


_PROFILE = r"^[A-Za-z0-9_.+=,@/:-]{1,128}$"


class ProofTargetInputs(BaseModel):
    """The customer's network for the test cluster: ids only, never a credential."""

    model_config = ConfigDict(extra="forbid")

    region: str = Field(..., pattern=r"^[a-z]{2}(-[a-z]+)+-\d$")
    vpc_id: str = Field(..., pattern=r"^vpc-[0-9a-f]{1,32}$")
    subnet_ids: list[str] = Field(..., min_length=2)
    max_capacity: int = Field(DEFAULT_MAX_CAPACITY, ge=1, le=16)
    stack_name: str | None = Field(None, pattern=r"^[A-Za-z][-A-Za-z0-9]{0,127}$")
    read_profile: str | None = Field(None, pattern=_PROFILE)
    write_profile: str | None = Field(None, pattern=_PROFILE)

    def model_post_init(self, _context: Any) -> None:
        bad = [s for s in self.subnet_ids if not re.fullmatch(r"subnet-[0-9a-f]{1,32}", s)]
        if bad:
            raise ValueError(f"subnet_ids: {', '.join(bad)} is not a subnet-... id")
        if len(set(self.subnet_ids)) != len(self.subnet_ids):
            raise ValueError("subnet_ids: a subnet is listed twice")


@dataclass(frozen=True)
class ProofTargetClients:
    cloudformation: Any
    rds: Any


@dataclass(frozen=True)
class Target:
    """The planned Aurora target: its engine, version line and where the line comes from."""

    engine: str
    line: str
    line_source: str


def stack_name(inputs: ProofTargetInputs, job_id: str) -> str:
    """``dm-<job>-routine-proof``: letters, digits and single hyphens."""
    text = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9-]", "-", f"dm-{job_id}-routine-proof"))
    return inputs.stack_name or text[:128].strip("-")


def stack_tags(job_id: str) -> list[dict[str, str]]:
    return [{"Key": JOB_TAG, "Value": job_id}, {"Key": PURPOSE_TAG, "Value": PURPOSE}]


def planned_target(store: Any, db: str, job_id: str) -> Target:
    """The step 1 plan's target engine and version line (the line ``aurora-target.yaml``
    renders); a plan naming no line gets the newest line with standard support."""
    from src.shared.catalog.gaps import aurora_version_lines
    from src.tools.data_movement.dms_step import DmsStepError, load_step1

    try:
        inputs = load_step1(store, db, job_id, with_ddl=False)
    except DmsStepError as exc:
        raise ProofTargetError(
            f"the test cluster takes the planned target's engine: {exc}"
        ) from None
    engine = inputs.contract.target.engine
    from src.tools.data_movement.dms_step import AURORA_ENGINES

    if engine not in AURORA_ENGINES:
        raise ProofTargetError(f"the planned target {engine} is not an Aurora engine")
    line = inputs.plan.replication.target_engine_line if inputs.plan.replication else None
    if line:
        return Target(engine, line, "the step 1 plan's target line")
    newest = list(aurora_version_lines()[engine].lines)[-1]
    return Target(engine, newest, "the newest line with standard support (the plan names none)")


def check_region(engine: str, region: str) -> None:
    if region not in DATA_API_REGIONS[engine]:
        raise ProofTargetError(f"{display_engine(engine)} has no RDS Data API listed for {region}, and "
                               "the proof reaches the test cluster only through it: choose another region")  # fmt: skip


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def pick_version(rds: Any, target: Target) -> dict[str, Any]:
    """The newest available release of the target's line that Serverless v2 and the Data API
    support (read-only: ``DescribeDBEngineVersions``), with its lowest capacity."""
    minimum = DATA_API_MINIMUM[target.engine].get(target.line)
    if minimum is None:
        raise ProofTargetError(f"{display_engine(target.engine)} {target.line} has no RDS Data API")
    found: list[tuple[tuple[int, ...], str, float]] = []
    pages = rds.get_paginator("describe_db_engine_versions").paginate(
        Engine=_engine_id(target.engine)
    )
    for page in pages:
        for v in page.get("DBEngineVersions") or []:
            version = v.get("EngineVersion") or ""
            serverless = v.get("ServerlessV2FeaturesSupport")
            if (
                _RELEASE.fullmatch(version)
                and version.startswith(f"{target.line}.")
                and v.get("Status", "available") == "available"
                and "provisioned" in (v.get("SupportedEngineModes") or [])
                and serverless
                and _numbers(version) >= _numbers(minimum)
            ):
                found.append(
                    (_numbers(version), version, float(serverless.get("MinCapacity", 0.5)))
                )
    if not found:
        raise ProofTargetError(
            f"no {display_engine(target.engine)} {target.line} version in this region runs Serverless v2 "
            f"with the RDS Data API ({minimum} or later)"
        )
    _, version, lowest = max(found)
    min_capacity: float | int = 0 if lowest == 0 else 0.5
    return {"engine_version": version, "min_capacity": min_capacity, "auto_pause": lowest == 0}


def plan(clients: ProofTargetClients, inputs: ProofTargetInputs, target: Target,
         job_id: str) -> dict[str, Any]:  # fmt: skip
    """What deploy sends, and the questions to ask before deploy and teardown."""
    check_region(target.engine, inputs.region)
    picked = pick_version(clients.rds, target)
    name = stack_name(inputs, job_id)
    params = {"JobId": job_id, "Engine": _engine_id(target.engine),
              "EngineVersion": picked["engine_version"], "MinCapacity": str(picked["min_capacity"]),
              "MaxCapacity": str(inputs.max_capacity), "VpcId": inputs.vpc_id,
              "SubnetIds": ",".join(inputs.subnet_ids),
              "ParameterGroupFamily": parameter_group_family(target.engine, picked["engine_version"])}  # fmt: skip
    return {
        "status": "plan", "stack_name": name, "region": inputs.region,
        "engine": target.engine, "version_line": target.line, "version_line_source": target.line_source,
        **picked, "max_capacity": inputs.max_capacity, "data_api": True,
        "vpc_id": inputs.vpc_id, "subnet_ids": inputs.subnet_ids,
        "template": f"infrastructure/cloudformation/data-movement/{TEMPLATE_NAME}",
        "parameters": params, "tags": stack_tags(job_id),
        "question": deploy_question(name, inputs, target, picked),
        "run_question": run_question(name, inputs.region),
        "teardown_question": teardown_question(name, inputs.region),
        "data": DATA_NOTE, "cost": COST,
    }  # fmt: skip


def deploy_question(name: str, inputs: ProofTargetInputs, target: Target,
                    picked: dict[str, Any] | None = None) -> str:  # fmt: skip
    version = f" {picked['engine_version']}" if picked else f" {target.line}"
    capacity = (f" from {picked['min_capacity']} to {inputs.max_capacity} ACUs" if picked
                else f" up to {inputs.max_capacity} ACUs")  # fmt: skip
    return (f"Deploy the routine-proof test cluster stack {name} in {inputs.region}: "
            f"{display_engine(target.engine)}{version}, Aurora Serverless v2{capacity}, in subnets "
            f"{', '.join(inputs.subnet_ids)} of {inputs.vpc_id}, with no public access and no "
            "inbound rule (the proof uses the RDS Data API)? It holds synthetic rows and the "
            "carried routines only.")  # fmt: skip


def run_question(name: str, region: str) -> str:
    """The question to ask before the routine proof (#1945) runs on the test cluster."""
    return (f"Run the routine proof on the test cluster of stack {name} in {region}? It creates a "
            "scratch database there, loads synthetic rows and the carried routines, calls them "
            "and drops the database.")  # fmt: skip


def teardown_question(name: str, region: str) -> str:
    return (f"Delete the routine-proof test cluster stack {name} in {region}, with its cluster, "
            "its master secret and every row in it (no final snapshot)?")  # fmt: skip


# --- AWS: CloudFormation and RDS only -----------------------------------------------------------


def _stack(clients: ProofTargetClients, name: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    try:
        stacks = clients.cloudformation.describe_stacks(StackName=name).get("Stacks") or []
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "ValidationError" and "does not exist" in error.get("Message", ""):
            return None
        raise
    if not stacks or stacks[0].get("StackStatus") == "DELETE_COMPLETE":
        return None
    stack: dict[str, Any] = stacks[0]
    return stack


def _not_ours(name: str, why: str) -> ProofTargetError:
    return ProofTargetError(f"stack {name} is not this job's routine-proof test stack: {why}; the "
                            f"proof-target commands act only on the stack {TEMPLATE_NAME} created "
                            "for this job (end any other stack yourself)")  # fmt: skip


def _tag_values(tags: Any, key: str) -> list[Any]:
    return [t.get("Value") for t in tags or [] if t.get("Key") == key]


def owned_resources(clients: ProofTargetClients, name: str, stack: dict[str, Any],
                    job_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:  # fmt: skip
    """(resources, cluster): the stack's resources and its cluster (None once gone), once the
    stack, its resources and its cluster are this job's."""
    from src.tools.data_movement.stack_states import DEPLOYED

    for key, value in ((JOB_TAG, job_id), (PURPOSE_TAG, PURPOSE)):
        values = _tag_values(stack.get("Tags"), key)
        if values != [value]:
            raise _not_ours(name, f"its {key} tag is {values or 'missing'}, not [{value!r}]")
    found = _resources(clients, name)
    kinds = {k: r["ResourceType"] for k, r in found.items()}
    # A deployed stack holds all four; any other (a create in progress or never completed, a
    # delete in progress or failed, a failed update) may hold fewer, never another.
    partial = stack["StackStatus"] not in DEPLOYED
    allowed = resources_for(stack)
    expected = {k: v for k, v in allowed.items() if k in kinds} if partial else allowed
    if kinds != expected:
        extra = sorted(f"{k} ({v})" for k, v in kinds.items() if allowed.get(k) != v)
        missing = sorted(k for k in expected if kinds.get(k) != expected[k])
        parts = [f"unexpected: {', '.join(extra)}"] if extra else []
        parts += [f"missing: {', '.join(missing)}"] if missing else []
        raise _not_ours(name, f"its resources are not the {len(allowed)} of {TEMPLATE_NAME} "
                              f"({'; '.join(parts)})")  # fmt: skip
    cluster = _cluster(clients, found.get("Cluster"))
    if cluster is not None:
        values = _tag_values(cluster.get("TagList"), JOB_TAG)
        if values != [job_id]:
            raise _not_ours(name, f"its Cluster's {JOB_TAG} tag is {values or 'missing'}, "
                                  f"not [{job_id!r}]")  # fmt: skip
    return found, cluster


def _cluster(clients: ProofTargetClients, resource: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stack's cluster, or None once it is gone (or was never made)."""
    from botocore.exceptions import ClientError

    if not resource or resource.get("ResourceStatus") == "DELETE_COMPLETE":
        return None
    identifier = resource.get("PhysicalResourceId")
    if not identifier:
        return None
    try:
        clusters = clients.rds.describe_db_clusters(DBClusterIdentifier=identifier).get(
            "DBClusters"
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "DBClusterNotFoundFault":
            return None
        raise
    return clusters[0] if clusters else None


def _wait(clients: ProofTargetClients, stack_id: str, command: str, until: Callable[[str], bool],
          wait_seconds: int, interval: int, sleep: Callable[[float], None]) -> tuple[str, int, dict[str, Any] | None]:  # fmt: skip
    """Poll the stack by its id until ``until(status)`` or the time is up: (status, waited, stack)."""
    import sys

    waited, state, current = 0, "", None
    while waited + interval <= wait_seconds:
        sleep(interval)
        waited += interval
        current = _by_id(clients, stack_id)
        state = "DELETE_COMPLETE" if current is None else current["StackStatus"]
        print(f"{command}: the stack is {state} (waited {waited} s of {wait_seconds} s)",
              file=sys.stderr)  # fmt: skip
        if until(state):
            break
    return state, waited, current


def _by_id(clients: ProofTargetClients, stack_id: str) -> dict[str, Any] | None:
    stacks = clients.cloudformation.describe_stacks(StackName=stack_id).get("Stacks") or []
    return stacks[0] if stacks else None


def deploy(clients: ProofTargetClients, inputs: ProofTargetInputs, target: Target, job_id: str,
           wait_seconds: int, interval: int, *,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:  # fmt: skip
    """Create this job's test stack and wait for it; a stack already created is left as it is
    (kept scaled to zero for re-tests). Its only write is ``CreateStack``."""
    from src.tools.data_movement.stack_states import HEALTHY, stack_kind

    check_region(target.engine, inputs.region)
    name = stack_name(inputs, job_id)
    stack = _stack(clients, name)
    if stack is not None:
        owned_resources(clients, name, stack, job_id)
        status = stack["StackStatus"]
        if status in HEALTHY:
            return {"status": "already_deployed", "stack_name": name, "stack_id": stack.get("StackId"),
                    "stack_status": status, "region": inputs.region}  # fmt: skip
        if stack_kind(status) == "changing":
            raise ProofTargetError(f"stack {name} is {status}: wait for it with proof-target-status "
                                   "--wait-seconds 540")  # fmt: skip
        raise ProofTargetError(f"stack {name} is {status} and takes no new deploy: run "
                               "proof-target-teardown first, then deploy again")  # fmt: skip
    planned = plan(clients, inputs, target, job_id)
    stack_id = clients.cloudformation.create_stack(
        StackName=name, TemplateBody=TEMPLATE_PATH.read_text(),
        Parameters=[{"ParameterKey": k, "ParameterValue": v} for k, v in planned["parameters"].items()],
        Tags=stack_tags(job_id), OnFailure="ROLLBACK", EnableTerminationProtection=False,
    )["StackId"]  # fmt: skip
    state, waited, current = _wait(clients, stack_id, "proof-target-deploy",
                                   lambda s: s != "CREATE_IN_PROGRESS", wait_seconds, interval, sleep)  # fmt: skip
    out = {"stack_name": name, "stack_id": stack_id, "region": inputs.region,
           "engine_version": planned["engine_version"], "min_capacity": planned["min_capacity"],
           "stack_status": state or "CREATE_IN_PROGRESS", "waited_seconds": waited}  # fmt: skip
    if state == "CREATE_COMPLETE":
        return {"status": "deployed", **out, "reached": True}
    if state in ("", "CREATE_IN_PROGRESS"):
        return {"status": "deploying", **out, "reached": False}
    return {"status": "create_failed", **out, "reached": False,
            "stack_reason": (current or {}).get("StackStatusReason"),
            "next": "run proof-target-status to read the stack, then proof-target-teardown"}  # fmt: skip


def status(clients: ProofTargetClients, inputs: ProofTargetInputs, job_id: str,
           wait_seconds: int, interval: int, *,
           sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:  # fmt: skip
    """Read-only: the stack and its cluster, once both are this job's; with ``wait_seconds``,
    wait while CloudFormation is still working on the stack."""
    from src.tools.data_movement.stack_states import stack_kind

    name = stack_name(inputs, job_id)
    base = {"stack_name": name, "region": inputs.region,
            "teardown_question": teardown_question(name, inputs.region)}  # fmt: skip
    stack = _stack(clients, name)
    if stack is None:
        return {"status": "not_deployed", **base}
    _, cluster = owned_resources(clients, name, stack, job_id)
    state = stack["StackStatus"]
    if stack_kind(state) == "changing" and wait_seconds:
        state, _, current = _wait(clients, stack.get("StackId") or name, "proof-target-status",
                                  lambda s: stack_kind(s) != "changing", wait_seconds, interval, sleep)  # fmt: skip
        if current is None or state == "DELETE_COMPLETE":
            return {"status": "not_deployed", **base}
        stack = current
        cluster = _cluster(clients, _resources(clients, name).get("Cluster"))
    kind = stack_kind(state)
    out: dict[str, Any] = {"status": "deployed" if kind == "deployed" else kind, **base,
                           "stack_id": stack.get("StackId"), "stack_status": state,
                           "stack_reason": stack.get("StackStatusReason")}  # fmt: skip
    if cluster is not None:
        scaling = cluster.get("ServerlessV2ScalingConfiguration") or {}
        out["cluster"] = {
            "identifier": cluster.get("DBClusterIdentifier"), "arn": cluster.get("DBClusterArn"),
            "status": cluster.get("Status"), "engine": cluster.get("Engine"),
            "engine_version": cluster.get("EngineVersion"),
            "min_capacity": scaling.get("MinCapacity"), "max_capacity": scaling.get("MaxCapacity"),
            "data_api": bool(cluster.get("HttpEndpointEnabled")),
            "storage_encrypted": bool(cluster.get("StorageEncrypted")),
            "master_user_secret_arn": (cluster.get("MasterUserSecret") or {}).get("SecretArn"),
            "database_name": cluster.get("DatabaseName"),
            "master_username": cluster.get("MasterUsername"),
        }  # fmt: skip
    return out


def _resources(clients: ProofTargetClients, name: str) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    pages = clients.cloudformation.get_paginator("list_stack_resources").paginate(StackName=name)
    for page in pages:
        for r in page.get("StackResourceSummaries") or []:
            found[r["LogicalResourceId"]] = r
    return found


def teardown(clients: ProofTargetClients, inputs: ProofTargetInputs, job_id: str,
             wait_seconds: int, interval: int, *,
             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:  # fmt: skip
    """Delete this job's test stack and wait until it is gone. At most one ``DeleteStack`` per
    run, on the stack id it checked; a stack already being deleted is only waited on, and a
    ``DELETE_FAILED`` is reported, so another delete takes another run (and approval)."""
    from src.tools.data_movement.stack_states import protection_message

    name = stack_name(inputs, job_id)
    stack = _stack(clients, name)
    if stack is None:
        return {"status": "deleted", "stack_name": name, "region": inputs.region,
                "already_deleted": True}  # fmt: skip
    status_now = stack["StackStatus"]
    if status_now.endswith("_IN_PROGRESS") and status_now not in ("DELETE_IN_PROGRESS",
                                                                  "REVIEW_IN_PROGRESS"):  # fmt: skip
        raise ProofTargetError(f"stack {name} is {status_now}: wait for it with proof-target-status "
                               "--wait-seconds 540")  # fmt: skip
    owned_resources(clients, name, stack, job_id)
    stack_id = stack.get("StackId") or name
    if status_now != "DELETE_IN_PROGRESS":
        if stack.get("EnableTerminationProtection"):
            raise ProofTargetError(
                f"proof-target-teardown sends no delete: {protection_message(name)}"
            )
        clients.cloudformation.delete_stack(StackName=stack_id)
    state, waited, current = _wait(clients, stack_id, "proof-target-teardown",
                                   lambda s: s in ("DELETE_COMPLETE", "DELETE_FAILED"),
                                   wait_seconds, interval, sleep)  # fmt: skip
    out = {"stack_name": name, "stack_id": stack_id, "region": inputs.region,
           "already_deleted": False, "waited_seconds": waited, "deleted": DELETED}  # fmt: skip
    if state == "DELETE_FAILED":
        return {"status": "delete_failed", **out, "reached": False,
                "stack_reason": (current or {}).get("StackStatusReason")}  # fmt: skip
    reached = state == "DELETE_COMPLETE"
    return {"status": "deleted" if reached else "deleting", **out, "reached": reached}
