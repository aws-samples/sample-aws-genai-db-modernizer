#!/usr/bin/env python3
"""Declarative deploy/update/destroy harness for the AWS Transform ATX agent fleet.

One container image (Dockerfile.atx), N AgentCore runtimes that differ only by
environment variables, each registered in the AWS Transform Agent Registry. This
script is the reproducible, environment-prefixed form of what has been done by
hand until now (the v2 runtimes reached version 16 through manual updates).

Self-contained: boto3 + AWS CLI + a container builder (finch/docker) only. No
Kiro/MCP dependency, so it runs the same way from the GitLab deploy pipeline and
from a laptop (personal alias fleets).

Verbs
-----
  build    Build + push the ARM64 image, print the image URI (with digest).
  apply    Create-or-update each agent's runtime for an environment prefix, and
           register/publish/enable it. Re-running after a code change updates the
           runtime in place and publishes a new (patch-bumped) registry version,
           so refreshed agentCard metadata is what the platform serves.
  destroy  Deregister each agent and delete its runtime for an environment prefix.
  status   Show runtime + registry state for an environment prefix.

Environments are distinguished by an --env suffix that forms the agent-name
prefix ``dbmod-<env>`` (e.g. ``dbmod-estserna``). The
orchestrator resolves its subagents by that same prefix via its AGENT_NAME_PREFIX
env var, so a fleet is fully isolated per environment.

Examples
--------
  # Build the image from the current working tree
  ./atx_deploy.py build

  # Stand up a 3-agent slice to prove the A2A path, using the just-built image
  ./atx_deploy.py apply --env estserna --agents orchestrator,collector,triage \
      --image-uri 754955336423.dkr.ecr.us-east-1.amazonaws.com/modernizer-dev-atx@sha256:...

  # Later, after another code change: rebuild + update the same slice in place
  ./atx_deploy.py build && ./atx_deploy.py apply --env estserna \
      --agents orchestrator,collector,triage --image-uri <new-digest>

  # Tear the environment down
  ./atx_deploy.py destroy --env estserna --agents orchestrator,collector,triage
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess  # nosec B404 - only fixed-argv container-builder calls below, never a shell
import tempfile
import time
from dataclasses import dataclass, field

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# --------------------------------------------------------------------------- #
# Constants (this account / region). Override via flags where it makes sense.
# --------------------------------------------------------------------------- #

REGION = "us-east-1"
REGISTRY_ENDPOINT = "https://iad.prod.agent-registry-external.elastic-gumby.ai.aws.dev"
ECR_REPO = "modernizer-dev-atx"
DOCKERFILE = "src/atx_orchestrator/Dockerfile.atx"
EXECUTION_ROLE = "AgentCoreExecutionRole"
INVOKE_ROLE = "AWSTransformAgentInvokeRole"

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_S3_BUCKET = "modernizer-atx-poc-754955336423"
DEFAULT_STAGE = "prod"
OWNER_NAME = "wwso-database-modernizer"
OWNER_CONTACT = "wwso-database-modernizer"
BASE_CHAT_LABEL = "DB Modernization Assessment"

# The registry serves the most recently published version (there is no
# promote/set-current API), so every apply publishes a fresh, patch-bumped version
# to push updated agentCard metadata. New agents start here.
INITIAL_VERSION = "1.0.0"

# Fleet name prefix. Deliberately short ("dbmod", not "db-modernization"): AgentCore
# runtime names are capped at 48 chars, and the prefix + env + longest agent suffix
# (analysis-aurora-mysql) must fit, e.g. dbmod-<env>-analysis-aurora-mysql.
NAME_PREFIX = "dbmod"

# AgentCore runtime names: [a-zA-Z][a-zA-Z0-9_]{0,47}, no hyphens, <= 48 chars.
_RUNTIME_NAME_MAX = 48


# --------------------------------------------------------------------------- #
# Fleet definition
#
# ``suffix`` is what the orchestrator resolves over A2A (from tools.py) and forms
# the registry name ``<prefix>-<suffix>``. ``agent_type`` is the AGENT_TYPE env
# var the container dispatches on (from atx_entrypoint.py). They diverge for
# three phases: triage/assignment/synthesis.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Agent:
    suffix: str  # registry-name suffix + A2A resolution name
    agent_type: str  # AGENT_TYPE env var (atx_entrypoint dispatch key)
    orchestrator: bool = False
    # A2A subagent suffixes this orchestrator drives (orchestrator only).
    dependencies: tuple[str, ...] = field(default_factory=tuple)


_ANALYSIS = ("dynamodb", "documentdb", "elasticache", "opensearch", "aurora-pg", "aurora-mysql")
_SCHEMA = _ANALYSIS

_SUBAGENTS: list[Agent] = [
    Agent("collector", "collector"),
    Agent("triage", "referee-triage"),
    *[Agent(f"analysis-{e}", f"analysis-{e}") for e in _ANALYSIS],
    Agent("assignment", "assignment-resolver"),
    *[Agent(f"schema-{e}", f"schema-{e}") for e in _SCHEMA],
    Agent("synthesis", "referee-synthesis"),
]

_ORCHESTRATOR = Agent(
    "orchestrator",
    "orchestrator",
    orchestrator=True,
    dependencies=tuple(a.suffix for a in _SUBAGENTS),
)

# Keyed by a short CLI alias. "triage"/"assignment"/"synthesis" alias to the
# registry suffix so --agents stays terse.
FLEET: dict[str, Agent] = {a.suffix: a for a in _SUBAGENTS}
FLEET["orchestrator"] = _ORCHESTRATOR


# --------------------------------------------------------------------------- #
# Clients
# --------------------------------------------------------------------------- #


def _clients():
    cfg = Config(region_name=REGION, retries={"max_attempts": 5, "mode": "standard"})
    session = boto3.Session()
    return {
        "sts": session.client("sts", config=cfg),
        "ecr": session.client("ecr", config=cfg),
        "acc": session.client("bedrock-agentcore-control", config=cfg),
        "reg": session.client(
            "atxagentregistryexternal", config=cfg, endpoint_url=REGISTRY_ENDPOINT
        ),
    }


def _account_id(sts) -> str:
    return str(sts.get_caller_identity()["Account"])


def _role_arn(account: str, role: str) -> str:
    return f"arn:aws:iam::{account}:role/{role}"


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


def registry_name(prefix: str, agent: Agent) -> str:
    return f"{prefix}-{agent.suffix}"


def runtime_name(prefix: str, agent: Agent) -> str:
    name = registry_name(prefix, agent).replace("-", "_")
    if len(name) > _RUNTIME_NAME_MAX:
        raise SystemExit(
            f"Runtime name {name!r} is {len(name)} chars (>{_RUNTIME_NAME_MAX}). "
            f"Use a shorter --env prefix."
        )
    if not name[0].isalpha():
        raise SystemExit(f"Runtime name {name!r} must start with a letter.")
    return name


def _env_for(agent: Agent, prefix: str, model_id: str, s3_bucket: str) -> dict[str, str]:
    env = {
        "AGENT_TYPE": agent.agent_type,
        "MODEL_ID": model_id,
        "S3_BUCKET": s3_bucket,
        "REGION": REGION,
        "STAGE": DEFAULT_STAGE,
    }
    if agent.orchestrator:
        # Only the orchestrator resolves subagents by name; the prefix is the
        # environment's fleet prefix (e.g. dbmod-estserna).
        env["AGENT_NAME_PREFIX"] = prefix
    return env


# --------------------------------------------------------------------------- #
# Image build
# --------------------------------------------------------------------------- #


def _detect_builder(preferred: str | None = None) -> str:
    # Prefer docker: finch is sometimes installed but its VM is not initialized.
    order = [preferred] if preferred else ["docker", "finch"]
    for tool in order:
        if tool and shutil.which(tool):
            return tool
    raise SystemExit("Neither finch nor docker found on PATH; cannot build the ARM64 image.")


def cmd_build(args, clients) -> None:
    account = _account_id(clients["sts"])
    ecr = clients["ecr"]
    repo = args.ecr_repo
    registry = f"{account}.dkr.ecr.{REGION}.amazonaws.com"
    image = f"{registry}/{repo}"
    tag = args.tag

    # Ensure the ECR repo exists.
    try:
        ecr.describe_repositories(repositoryNames=[repo])
    except ClientError as e:
        if e.response["Error"]["Code"] == "RepositoryNotFoundException":
            print(f"Creating ECR repo {repo}")
            ecr.create_repository(repositoryName=repo)
        else:
            raise

    builder = _detect_builder(args.builder)
    print(f"Building with {builder} (linux/arm64) -> {image}:{tag}")

    # Isolate the docker config so `docker login` does not use the macOS keychain
    # credential helper, which fails with "-25299 item already exists" when the
    # ECR entry is already cached. With no credsStore, the token is written
    # base64 into this throwaway config.json instead.
    run_env = dict(os.environ)
    if builder == "docker":
        cfg_dir = tempfile.mkdtemp(prefix="atx-docker-cfg-")
        with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
            json.dump({"auths": {}}, fh)
        run_env["DOCKER_CONFIG"] = cfg_dir

    token = ecr.get_authorization_token()["authorizationData"][0]
    user, pw = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    # Fixed argv, shell=False; builder is docker/finch and all args are trusted
    # constants or ECR-issued values, so there is no shell-injection surface.
    subprocess.run(  # nosec B603
        [builder, "login", "--username", user, "--password-stdin", registry],
        input=pw.encode(),
        check=True,
        env=run_env,
    )
    subprocess.run(  # nosec B603
        [
            builder,
            "build",
            "--platform",
            "linux/arm64",
            "-t",
            f"{image}:{tag}",
            "-f",
            DOCKERFILE,
            ".",
        ],
        check=True,
        env=run_env,
    )
    subprocess.run([builder, "push", f"{image}:{tag}"], check=True, env=run_env)  # nosec B603

    # Resolve the pushed digest so callers can pin by digest (what v2 does).
    desc = ecr.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
    digest = desc["imageDetails"][0]["imageDigest"]
    image_uri = f"{image}@{digest}"
    print(f"\nIMAGE_URI={image_uri}")


# --------------------------------------------------------------------------- #
# Runtime lifecycle
# --------------------------------------------------------------------------- #


def _find_runtime(acc, name: str) -> str | None:
    paginator_args: dict = {}
    while True:
        resp = acc.list_agent_runtimes(**paginator_args)
        for rt in resp.get("agentRuntimes", []):
            if rt.get("agentRuntimeName") == name:
                return str(rt["agentRuntimeId"])
        token = resp.get("nextToken")
        if not token:
            return None
        paginator_args = {"nextToken": token}


def _wait_ready(acc, runtime_id: str, timeout: float = 600.0) -> None:
    start = time.monotonic()
    while True:
        status = acc.get_agent_runtime(agentRuntimeId=runtime_id)["status"]
        if status in ("READY", "ACTIVE"):
            return
        if status in ("FAILED", "DELETE_FAILED"):
            raise SystemExit(f"Runtime {runtime_id} reached {status}")
        if time.monotonic() - start > timeout:
            raise SystemExit(f"Runtime {runtime_id} still {status} after {timeout}s")
        time.sleep(5)


def _wait_deregistered(reg, name: str, timeout: float = 120.0) -> bool:
    """Poll until the registry entry for ``name`` is gone.

    Deregistration is asynchronous. Deleting the runtime before it finalizes
    orphans the workflow, leaving a stuck entry that still shows as a WebApp job
    type. Wait here (runtime still present) so it can finalize. Returns True once
    the agent is gone, False on timeout.
    """
    start = time.monotonic()
    while True:
        try:
            reg.get_agent(name=name)
        except ClientError:
            return True
        if time.monotonic() - start > timeout:
            return False
        time.sleep(5)


def _upsert_runtime(acc, name: str, image_uri: str, role_arn: str, env: dict[str, str]) -> str:
    artifact = {"containerConfiguration": {"containerUri": image_uri}}
    network = {"networkMode": "PUBLIC"}
    existing = _find_runtime(acc, name)
    if existing:
        print(f"  update runtime {name} ({existing})")
        acc.update_agent_runtime(
            agentRuntimeId=existing,
            agentRuntimeArtifact=artifact,
            roleArn=role_arn,
            networkConfiguration=network,
            environmentVariables=env,
        )
        _wait_ready(acc, existing)
        return str(acc.get_agent_runtime(agentRuntimeId=existing)["agentRuntimeArn"])
    print(f"  create runtime {name}")
    resp = acc.create_agent_runtime(
        agentRuntimeName=name,
        agentRuntimeArtifact=artifact,
        roleArn=role_arn,
        networkConfiguration=network,
        environmentVariables=env,
    )
    runtime_id = resp["agentRuntimeId"]
    _wait_ready(acc, runtime_id)
    return str(resp["agentRuntimeArn"])


# --------------------------------------------------------------------------- #
# Registry lifecycle
# --------------------------------------------------------------------------- #


def _agent_card(name: str, account: str, dependencies: list[str], version: str) -> dict:
    return {
        "id": name,
        "name": name,
        "description": f"ATX agent: {name}",
        "version": version,
        "capabilities": {
            "restartable": True,
            "a2aSupported": True,
            "legacyDashboard": False,
            "legacyTaskLink": False,
            "webAppV2": True,
            "legacyRestartable": False,
            "extensions": [
                {
                    "name": "Agent Provider",
                    "description": "Agent publisher details",
                    "params": {
                        "name": OWNER_NAME,
                        "accountId": account,
                        "ownerType": "DIRECT_AGENT",
                        "contactInfo": [{"type": "email", "value": OWNER_CONTACT}],
                    },
                },
                {
                    "name": "Agent Dependencies",
                    "description": "Runtime dependencies",
                    "params": {"agentDependencies": dependencies},
                },
                {
                    "name": "Agent Connectors",
                    "description": "Agent connector configurations",
                    "params": {"connectors": []},
                },
            ],
        },
    }


def _bump_patch(version: str) -> str:
    """Return ``version`` with its patch component incremented.

    The InvokeAgent version pattern only accepts numeric ``major.minor.patch``
    (optionally a ``-dev-<alnum>`` suffix), so any suffix is dropped and the patch
    is bumped. Falls back to the initial version if the input is not parseable.
    """
    parts = version.split("-", 1)[0].split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return INITIAL_VERSION
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _next_version(reg, name: str) -> str:
    """Next version to publish for ``name``: current patch + 1, or the initial
    version if the agent has no published version yet."""
    try:
        current = reg.get_agent_version(name=name).get("version")
    except ClientError:
        return INITIAL_VERSION
    return _bump_patch(current) if current else INITIAL_VERSION


def _register_and_publish(
    reg, agent: Agent, name: str, runtime_arn: str, account: str, chat_label: str, deps: list[str]
) -> None:
    metadata: dict[str, object] = {
        "type": "ORCHESTRATOR_AGENT" if agent.orchestrator else "SUB_AGENT",
        "description": f"ATX agent: {name}",
        "ownerName": OWNER_NAME,
        "ownerContactInfo": OWNER_CONTACT,
        "ownerType": "DIRECT_AGENT",
        "customerConfigurationRequired": False,
    }
    if agent.orchestrator:
        metadata["jobOrchestrator"] = True
        metadata["jobOrchestratorMetadata"] = {
            "chatUILabel": chat_label,
            "chatAgentIdentifier": name,
            "a2aSupported": True,
        }
    try:
        reg.register_agent(name=name, metadata=metadata)
        print(f"  registered {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in (
            "ConflictException",
            "ValidationException",
        ) and _already_registered(reg, name):
            print(f"  already registered {name} (skipping register)")
        else:
            raise

    version = _next_version(reg, name)
    agent_card = _agent_card(name, account, deps, version)
    config = {
        "shortDescription": f"ATX agent: {name}",
        "computeConfiguration": {
            "provisionedComputeConfiguration": {
                "agentCoreConfiguration": {
                    "atxAccessRoleArn": _role_arn(account, INVOKE_ROLE),
                    "runtimeArn": runtime_arn,
                    "qualifier": "DEFAULT",
                }
            }
        },
        "agentCard": agent_card,
        "inputPayloadSchema": {"type": "object"},
        "outputPayloadSchema": {"type": "object"},
        "monitoringType": "HEALTHCHECK",
        "notificationsEnabled": "ENABLED",
        "objectiveNegotiationPrompt": "",
        "agentResiliencyConfiguration": {
            "partnerControllerRetryWindowMinutes": 6,
            "agentRecoveryConfiguration": {"recoveryWaitTimeSeconds": 60},
        },
    }
    for _ in range(8):
        try:
            reg.publish_agent_version(name=name, version=version, configuration=config)
            print(f"  published {name} v{version}")
            break
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConflictException":
                # Version already exists (repeat/concurrent publish). Step past it.
                version = _bump_patch(version)
                agent_card["version"] = version
                continue
            raise
    else:
        raise SystemExit(f"could not publish a new version for {name} after retries")

    reg.update_publisher_access_control(
        agentName=name, customerAccountId=account, accessControl="ENABLED"
    )
    print(f"  access ENABLED for {name}")


def _already_registered(reg, name: str) -> bool:
    try:
        reg.get_agent(name=name)
        return True
    except ClientError:
        return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def _select(agents_arg: str | None) -> list[Agent]:
    if not agents_arg:
        return list(FLEET.values())
    out = []
    for key in [a.strip() for a in agents_arg.split(",") if a.strip()]:
        if key not in FLEET:
            raise SystemExit(f"Unknown agent {key!r}. Known: {', '.join(sorted(FLEET))}")
        out.append(FLEET[key])
    return out


def cmd_apply(args, clients) -> None:
    account = _account_id(clients["sts"])
    acc, reg = clients["acc"], clients["reg"]
    prefix = f"{NAME_PREFIX}-{args.env}"
    chat_label = f"{BASE_CHAT_LABEL} - {args.env}"
    role_arn = _role_arn(account, EXECUTION_ROLE)
    selected = _select(args.agents)

    if not args.image_uri:
        raise SystemExit("--image-uri is required for apply (run `build` first, or pass a digest).")

    selected_suffixes = {a.suffix for a in selected}

    print(f"apply env={args.env} prefix={prefix} agents={[a.suffix for a in selected]}")
    if args.dry_run:
        for agent in selected:
            print(
                f"  [dry-run] {registry_name(prefix, agent)} "
                f"runtime={runtime_name(prefix, agent)} env={_env_for(agent, prefix, args.model, args.s3_bucket)}"
            )
        return

    for agent in selected:
        name = registry_name(prefix, agent)
        rt_name = runtime_name(prefix, agent)
        env = _env_for(agent, prefix, args.model, args.s3_bucket)
        print(f"- {name}")
        runtime_arn = _upsert_runtime(acc, rt_name, args.image_uri, role_arn, env)
        # Declare dependencies only on subagents co-deployed in this environment;
        # AWS Transform auto-provisions declared deps at job creation, so listing
        # agents that are not registered would break job start.
        deps = (
            [f"{prefix}-{d}" for d in agent.dependencies if d in selected_suffixes]
            if agent.orchestrator
            else []
        )
        _register_and_publish(reg, agent, name, runtime_arn, account, chat_label, deps)
    print("apply complete")


def cmd_destroy(args, clients) -> None:
    acc, reg = clients["acc"], clients["reg"]
    prefix = f"{NAME_PREFIX}-{args.env}"
    selected = _select(args.agents)
    print(f"destroy env={args.env} prefix={prefix} agents={[a.suffix for a in selected]}")
    if args.dry_run:
        for agent in selected:
            print(f"  [dry-run] deregister+delete {registry_name(prefix, agent)}")
        return

    for agent in selected:
        name = registry_name(prefix, agent)
        rt_name = runtime_name(prefix, agent)
        print(f"- {name}")
        # Deprecate first: synchronous and reliable, so the agent stops appearing
        # as a WebApp job type immediately, even if the async deregistration below
        # lags or stalls. This is what keeps automated teardowns from leaving
        # stale, still-selectable agents behind.
        try:
            reg.update_agent(name=name, deprecated=True)
        except ClientError as e:
            if e.response["Error"]["Code"] not in (
                "ResourceNotFoundException",
                "ValidationException",
            ):
                raise
        deregistering = False
        try:
            result = reg.deregister_agent(name=name, force=args.force)
            print(f"  deregister: {result.get('deregistrationStatus', result)}")
            deregistering = True
        except ClientError as e:
            msg = e.response["Error"].get("Message", str(e))
            if "active instances" in msg and not args.force:
                print(
                    f"  WARNING active instances: {msg}\n  re-run destroy with --force to proceed."
                )
            elif e.response["Error"]["Code"] in (
                "ResourceNotFoundException",
                "ValidationException",
            ):
                print(f"  not registered ({e.response['Error']['Code']}); skipping deregister")
            else:
                raise
        # Let the async deregistration finalize while the runtime still exists;
        # deleting it first orphans the workflow and the entry gets stuck. The
        # agent is already deprecated, so a slow finalize is not user-visible.
        if deregistering and not _wait_deregistered(reg, name):
            print("  deregistration still in progress; agent is deprecated so it stays hidden")
        rid = _find_runtime(acc, rt_name)
        if rid:
            acc.delete_agent_runtime(agentRuntimeId=rid)
            print(f"  deleted runtime {rt_name}")
        else:
            print(f"  no runtime {rt_name}")
    print("destroy complete")


def cmd_status(args, clients) -> None:
    acc, reg = clients["acc"], clients["reg"]
    prefix = f"{NAME_PREFIX}-{args.env}"
    for agent in _select(args.agents):
        name = registry_name(prefix, agent)
        rt_name = runtime_name(prefix, agent)
        rid = _find_runtime(acc, rt_name)
        rt_status = acc.get_agent_runtime(agentRuntimeId=rid)["status"] if rid else "MISSING"
        try:
            reg.get_agent(name=name)
            reg_status = "REGISTERED"
        except ClientError:
            reg_status = "UNREGISTERED"
        print(f"{name:50s} runtime={rt_status:10s} registry={reg_status}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build", help="build + push the ARM64 image")
    pb.add_argument("--ecr-repo", default=ECR_REPO)
    pb.add_argument("--tag", default="latest")
    pb.add_argument(
        "--builder",
        choices=["docker", "finch"],
        default=None,
        help="container builder (default: auto, prefers docker)",
    )

    for verb, fn in (("apply", cmd_apply), ("destroy", cmd_destroy), ("status", cmd_status)):
        sp = sub.add_parser(verb)
        sp.add_argument("--env", required=True, help="environment suffix, e.g. estserna")
        sp.add_argument("--agents", help="comma-separated agent keys; default = full fleet")
        sp.add_argument("--dry-run", action="store_true")
        if verb == "apply":
            sp.add_argument("--image-uri", help="ECR image URI (with @sha256 digest)")
            sp.add_argument("--model", default=DEFAULT_MODEL_ID)
            sp.add_argument("--s3-bucket", default=DEFAULT_S3_BUCKET)
        if verb == "destroy":
            sp.add_argument(
                "--force", action="store_true", help="async deregister running instances"
            )
        sp.set_defaults(func=fn)

    pb.set_defaults(func=cmd_build)
    args = p.parse_args()
    clients = _clients()
    args.func(args, clients)


if __name__ == "__main__":
    main()
