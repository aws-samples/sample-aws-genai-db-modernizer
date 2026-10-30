# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IAM least privilege in the automation and AWS Transform templates (#1723, #1736).

No AWS call: the template is decoded with cfn-lint's parser and its policy
statements are read as data. The Secrets Manager scope is checked against the
secret names the code really uses, and IAM wildcards in actions are expanded
from botocore's API model, so ``secretsmanager:*`` cannot widen it unnoticed.
The AWS Transform execution role's transform-agents actions are pinned to the
calls the agents make; botocore has no model for that service, so its wildcards
are expanded from the model the AWS Transform SDK ships.
"""

from __future__ import annotations

import fnmatch
import functools
import re
from pathlib import Path
from typing import Any

import botocore.session
import pytest
from cfnlint.decode import decode_str

REPO_ROOT = Path(__file__).resolve().parents[4]
AUTOMATION = REPO_ROOT / "infrastructure" / "cloudformation" / "automation.yaml"

ACCOUNT = "111122223333"
REGION = "us-east-1"


#: IAM actions that are not an API operation name of the same service.
_EXTRA_ACTIONS: dict[str, set[str]] = {}


@functools.cache
def _known_actions(service: str) -> frozenset[str]:
    """Every action of a service, from botocore's API model (no network)."""
    model = botocore.session.get_session().get_service_model(service)
    names = set(model.operation_names) | _EXTRA_ACTIONS.get(service, set())
    return frozenset(f"{service}:{name}".lower() for name in names)


def _template(path: Path) -> dict[str, Any]:
    template, matches = decode_str(path.read_text(encoding="utf-8"))
    assert not matches, matches
    assert template is not None
    return dict(template)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _render(value: Any, params: dict[str, str]) -> str:
    """Resolve a ``Fn::Sub`` string (or a literal) to a concrete ARN."""
    if isinstance(value, dict) and "Fn::Sub" in value:
        value = value["Fn::Sub"]
    assert isinstance(value, str), value
    subs = {"AWS::Region": REGION, "AWS::AccountId": ACCOUNT, **params}
    return re.sub(r"\$\{([^}]+)\}", lambda m: subs[m.group(1)], value)


def _statements(policy_document: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_as_list(policy_document["Statement"]))


def _actions(statement: dict[str, Any]) -> set[str]:
    """The statement's actions, with IAM wildcards expanded for Secrets Manager."""
    out: set[str] = set()
    for action in (a.lower() for a in _as_list(statement["Action"])):
        service = action.split(":")[0]
        if any(c in action for c in "*?") and service == "secretsmanager":
            out |= {a for a in _known_actions(service) if fnmatch.fnmatchcase(a, action)}
        else:
            out.add(action)
    return out


def _resources(statement: dict[str, Any], params: dict[str, str]) -> list[str]:
    return [_render(r, params) for r in _as_list(statement["Resource"])]


# ---------------------------------------------------------------------------
# automation.yaml: Secrets Manager
# ---------------------------------------------------------------------------


def _secret_statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    role = template["Resources"]["AutomationRole"]["Properties"]
    out: list[dict[str, Any]] = []
    for policy in role["Policies"]:
        if isinstance(policy, dict) and "Fn::If" in policy:
            policy = policy["Fn::If"][1]
        for statement in _statements(policy["PolicyDocument"]):
            if "secretsmanager:getsecretvalue" in _actions(statement):  # pragma: allowlist secret
                out.append(statement)
    return out


def _secret_arn(name: str) -> str:
    # Secrets Manager appends a hyphen and six random characters to the name.
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-AbCdEf"


def _automation_can_read(name: str) -> bool:
    statement = _secret_statements(_template(AUTOMATION))[0]
    patterns = _resources(statement, {"ProjectName": "modernizer"})
    return any(fnmatch.fnmatchcase(_secret_arn(name), p) for p in patterns)


def test_automation_secret_read_is_not_account_wide():
    for statement in _secret_statements(_template(AUTOMATION)):
        for resource in _as_list(statement["Resource"]):
            if isinstance(resource, dict) and "Ref" in resource:
                continue  # the optional AdditionalSecretArns parameter
            arn = _render(resource, {"ProjectName": "modernizer"})
            assert not arn.endswith(":secret:*"), arn
            assert arn != "*"


@pytest.mark.parametrize(
    "name",
    [
        # RDS-managed master user secrets, the form the API accepts by name
        # (src/api/models/requests.py, src/api/README.md).
        "rds!db-0a1b2c3d-1111-2222-3333-444455556666",
        "rds!cluster-0a1b2c3d-1111-2222-3333-444455556666",
        # Secrets the project names under its own prefix.
        "modernizer/wordpress/collector",
    ],
)
@pytest.mark.passes_on_main
def test_automation_reads_the_collection_secrets(name):
    assert _automation_can_read(name)


@pytest.mark.parametrize("name", ["prod/payments/api-key", "other-app/db", "modernizerx/db"])
def test_automation_cannot_read_unrelated_secrets(name):
    assert not _automation_can_read(name)


def test_automation_extra_secrets_are_opt_in():
    template = _template(AUTOMATION)
    param = template["Parameters"]["AdditionalSecretArns"]
    assert param["Type"] == "CommaDelimitedList"
    assert param["Default"] == ""
    statements = _secret_statements(template)
    extra = [s for s in statements if s["Resource"] == {"Ref": "AdditionalSecretArns"}]
    assert len(extra) == 1


#: Secrets Manager actions that read a secret's value or material.
_READS = {
    "secretsmanager:getsecretvalue",
    "secretsmanager:batchgetsecretvalue",
}  # pragma: allowlist secret


def _role_statements() -> list[dict[str, Any]]:
    role = _template(AUTOMATION)["Resources"]["AutomationRole"]["Properties"]
    out: list[dict[str, Any]] = []
    for policy in role["Policies"]:
        if isinstance(policy, dict) and "Fn::If" in policy:
            policy = policy["Fn::If"][1]
        out.extend(_statements(policy["PolicyDocument"]))
    return out


def test_no_statement_reads_every_secret():
    for statement in _role_statements():
        if not _actions(statement) & _READS:
            continue
        for resource in _as_list(statement["Resource"]):
            if isinstance(resource, dict) and "Ref" in resource:
                continue  # the optional AdditionalSecretArns parameter
            arn = _render(resource, {"ProjectName": "modernizer"})
            assert arn != "*" and not arn.endswith(":secret:*"), (statement.get("Sid"), arn)


@pytest.mark.parametrize(
    "action", ["secretsmanager:*", "secretsmanager:Get*", "secretsmanager:*SecretValue"]
)
def test_a_wildcard_action_is_expanded(action):
    assert _actions({"Action": action}) & _READS


@pytest.mark.parametrize(
    "value", ["*", "arn:*", "arn:aws:s3:::bucket", "arn:aws:secretsmanager:*:*:secret:*"]
)
def test_additional_secret_arns_rejects_wildcards(value):
    pattern = _template(AUTOMATION)["Parameters"]["AdditionalSecretArns"]["AllowedPattern"]
    assert not re.fullmatch(pattern, value)


@pytest.mark.parametrize(
    "value", ["", "arn:aws:secretsmanager:us-east-1:111122223333:secret:app/db-*"]
)
def test_additional_secret_arns_accepts_secret_arns(value):
    pattern = _template(AUTOMATION)["Parameters"]["AdditionalSecretArns"]["AllowedPattern"]
    assert re.fullmatch(pattern, value)


# ---------------------------------------------------------------------------
# pipeline/iam-roles.yaml: transform-agents (#1736)
# ---------------------------------------------------------------------------

IAM_ROLES = REPO_ROOT / "pipeline" / "iam-roles.yaml"
ATX_SRC = REPO_ROOT / "src" / "atx_orchestrator"

#: botocore has no transform-agents model; the AWS Transform SDK ships it
#: (``agent_builder_sdk/botocore_models``, signing name ``transform-agents``).
_TRANSFORM_AGENTS_MODEL = "transformagenticservice"

#: transform-agents operations the SDK calls by itself, under the role, for
#: every agent the image runs (agent-builder-sdk-aws-transform 1.1.0).
_SDK_CALLS: dict[str, str] = {
    "RefreshAuthToken": "_auth_token_refresher.py, started by AgentRuntimeServer",
    "GetAgentInstance": "agent_lifecycle.get_agent_status, at AgentRuntimeServer start",
    "UpdateAgentInstance": "agent_lifecycle.set_agent_running, at AgentRuntimeServer start",
    "GetJob": "JobManager.get_job_status, from auto_transition_job_to_executing (default on)",
    "UpdateJobStatus": "JobManager.transition_to_executing_if_assessing, same path",
    "ListAgentInstances": "agent_lifecycle.get_subagent_instances, on orchestrator stop",
    "StopAgent": "agent_lifecycle.shutdown_subagent_instance, on orchestrator stop",
}

#: SDK wrapper methods the code calls, and the operations each one makes.
_SDK_WRAPPERS: dict[str, set[str]] = {
    "update_status": {"UpdateAgentInstance"},  # AgentInstanceManager
    "update_job_status": {"UpdateJobStatus"},  # JobManager
    "download_artifact": {"CreateArtifactDownloadUrl"},  # ArtifactStore
    "upload_artifact": {
        "CreateArtifactUploadUrl",
        "GetArtifactMetadata",
        "CompleteArtifactUpload",
    },  # ArtifactStore
}

#: Every tool the in-container agentic MCP server registers, each one reviewed
#: (#1736): exposed to the model, so its actions are granted, unless it is in
#: ``src.atx_orchestrator.mcp_tools.UNUSED_MCP_TOOLS``. A tool a new server
#: version adds fails ``test_every_agentic_mcp_tool_has_a_decision`` until it
#: is listed here, after deciding whether to expose or exclude it.
_REVIEWED_MCP_TOOLS = frozenset(
    {
        "invoke_agent",
        "get_agent_instance",
        "update_agent_instance",
        "stop_agent",
        "list_agent_instances",
        "create_artifact_upload_url",
        "upload_artifact",
        "complete_artifact_upload",
        "create_artifact_download_url",
        "download_artifact",
        "list_artifacts",
        "get_artifact_metadata",
        "copy_artifact",
        "create_hitl_task",
        "get_hitl_task",
        "start_hitl_task",
        "close_hitl_task",  # clears stale approval tasks from the Tasks tab
        "list_hitl_tasks",  # finds them after a resume
        "retrieve_hitl_output_as_string",
        "create_s3_connector_hitl_task",
        "create_autoform_hitl_task",
        "create_autotable_hitl_task",
        "get_job",
        "update_job_status",
        "create_worklog",  # the job's Worklogs tab
        "list_worklogs",
        "put_job_plan",
        "list_job_plan_steps",
        "update_job_plan_step",
        "delete_job_plan_step",  # excluded
        "retrieve_from_knowledge_base",  # read-only product answers
    }
)

#: The AWS Transform packages the image installs, pinned in one file that CI
#: installs too, so the tool list read below is the image's.
_SDK_REQUIREMENTS = ATX_SRC / "requirements-sdk.txt"

#: Run in a fresh interpreter: importing the server registers its tools and
#: configures logging, which must not leak into this test process. For each
#: registered tool it lists every method or function name the tool calls,
#: following calls into the server package's own functions.
_MCP_PROBE = """
import ast, asyncio, inspect, json, textwrap, types
from agent_builder_agentic_mcp.server import mcp

def names(fn, seen):
    fn = inspect.unwrap(fn)
    if fn in seen:
        return set()
    seen.add(fn)
    out = set()
    for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(fn)))):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            out.add(node.func.id)
            target = fn.__globals__.get(node.func.id)
            if isinstance(target, types.FunctionType) and target.__module__.startswith(
                "agent_builder_agentic_mcp"
            ):
                out |= names(target, seen)
    return out

tools = asyncio.run(mcp.list_tools())
manager = mcp._tool_manager
print(json.dumps({t.name: sorted(names(manager.get_tool(t.name).fn, set())) for t in tools}))
"""


@functools.cache
def _mcp_tool_operations() -> dict[str, frozenset[str]]:
    """Each tool the installed agentic MCP server registers, and its operations."""
    import json
    import subprocess  # nosec B404 -- runs this interpreter on a fixed script
    import sys

    done = subprocess.run(  # nosec B603 -- fixed argv, no shell
        [sys.executable, "-I", "-c", _MCP_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, (
        "agent-builder-agentic-mcp-aws-transform is not importable; install "
        f"{_SDK_REQUIREMENTS.relative_to(REPO_ROOT)}:\n{done.stderr[-2000:]}"
    )
    by_snake = {_snake(op): op for op in _transform_agents_operations()}
    calls: dict[str, list[str]] = json.loads(done.stdout.strip().splitlines()[-1])
    return {
        tool: frozenset(by_snake[n] for n in names if n in by_snake)
        for tool, names in calls.items()
    }


@functools.cache
def _transform_agents_operations() -> frozenset[str]:
    """The service's operation names, from the model the SDK ships (no network)."""
    import importlib.util

    import botocore.loaders

    spec = importlib.util.find_spec("agent_builder_sdk")
    assert spec is not None and spec.origin, "agent-builder-sdk-aws-transform is not installed"
    models = Path(spec.origin).parent / "botocore_models"
    loader = botocore.loaders.Loader(extra_search_paths=[str(models)])
    model = loader.load_service_model(_TRANSFORM_AGENTS_MODEL, "service-2")
    assert model["metadata"]["signingName"] == "transform-agents"
    return frozenset(model["operations"])


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _expand_transform_agents(action: str) -> set[str]:
    """``transform-agents:Get*`` and the like, expanded from the SDK's model."""
    _, pattern = action.split(":", 1)
    return {
        f"transform-agents:{op}"
        for op in _transform_agents_operations()
        if fnmatch.fnmatchcase(op.lower(), pattern.lower())
    }


def _transform_agents_actions(role: str) -> set[str]:
    resource = _template(IAM_ROLES)["Resources"][role]["Properties"]
    out: set[str] = set()
    for policy in resource.get("Policies", []):
        for statement in _statements(policy["PolicyDocument"]):
            for action in _as_list(statement["Action"]):
                if action.lower().startswith("transform-agents:"):
                    out |= _expand_transform_agents(action)
    return out


def _code_calls() -> set[str]:
    """transform-agents operations the code in src/atx_orchestrator calls."""
    by_snake = {_snake(op): op for op in _transform_agents_operations()}
    out: set[str] = set()
    for path in ATX_SRC.rglob("*.py"):
        for name in re.findall(r"\.([a-z][a-z0-9_]*)\(", path.read_text(encoding="utf-8")):
            if name in by_snake:
                out.add(by_snake[name])
            out |= _SDK_WRAPPERS.get(name, set())
    return out


def _reachable_operations() -> set[str]:
    from src.atx_orchestrator.mcp_tools import UNUSED_MCP_TOOLS

    mcp: set[str] = set()
    for tool, ops in _mcp_tool_operations().items():
        if tool not in UNUSED_MCP_TOOLS:
            mcp |= ops
    return _code_calls() | set(_SDK_CALLS) | mcp


def test_the_execution_role_grants_only_the_transform_agents_calls_the_code_makes():
    granted = _transform_agents_actions("AgentCoreExecutionRole")
    needed = {f"transform-agents:{op}" for op in _reachable_operations()}
    assert sorted(granted - needed) == [], "granted but never called"
    assert sorted(needed - granted) == [], "called but not granted"


@pytest.mark.passes_on_main  # main already grants every direct call
def test_every_direct_call_is_granted():
    granted = _transform_agents_actions("AgentCoreExecutionRole")
    calls = {f"transform-agents:{op}" for op in _code_calls()}
    assert calls, "the scan found no transform-agents call"
    assert sorted(calls - granted) == []


@pytest.mark.passes_on_main  # main lists no unknown action either
def test_every_granted_transform_agents_action_exists():
    template = _template(IAM_ROLES)
    for role in ("AgentCoreExecutionRole", "AWSTransformAgentInvokeRole"):
        for policy in template["Resources"][role]["Properties"].get("Policies", []):
            for statement in _statements(policy["PolicyDocument"]):
                for action in _as_list(statement["Action"]):
                    if action.lower().startswith("transform-agents:"):
                        assert _expand_transform_agents(action), action


@pytest.mark.passes_on_main  # main already drops transform-agents from the invoke role
def test_the_invoke_role_grants_no_transform_agents_action():
    assert _transform_agents_actions("AWSTransformAgentInvokeRole") == set()


@pytest.mark.passes_on_main  # checks this file's expansion helper, not src/
@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("transform-agents:*", "transform-agents:CreateWorklog"),
        ("transform-agents:List*", "transform-agents:ListWorklogs"),
        ("transform-agents:*HitlTask", "transform-agents:CloseHitlTask"),
    ],
)
def test_a_transform_agents_wildcard_is_expanded(action, expected):
    assert expected in _expand_transform_agents(action)


@pytest.mark.passes_on_main  # checks this file's scan helper against main's calls too
def test_the_scan_maps_wrapper_and_client_calls():
    calls = _code_calls()
    # a client call (runtime/hitl.py) and an SDK wrapper (runtime/job_status.py)
    assert {"CreateHitlTask", "UpdateJobStatus"} <= calls
    # MCP-only and unused operations are not in the code
    assert not {"CreateWorklog", "RetrieveFromKnowledgeBase", "PutMigrationPlan"} & calls


def test_every_agentic_mcp_tool_has_a_decision():
    installed = set(_mcp_tool_operations())
    assert sorted(installed - _REVIEWED_MCP_TOOLS) == [], "new tool: expose or exclude it"
    assert sorted(_REVIEWED_MCP_TOOLS - installed) == [], "tool gone: drop it here"


def test_every_excluded_tool_is_a_registered_one():
    from src.atx_orchestrator.mcp_tools import UNUSED_MCP_TOOLS

    assert UNUSED_MCP_TOOLS <= set(_mcp_tool_operations())


@pytest.mark.passes_on_main  # checks this file's probe, not src/
def test_the_probe_follows_a_tool_into_its_helpers():
    ops = _mcp_tool_operations()
    assert ops["create_worklog"] == {"CreateWorklog"}
    # create_autoform_hitl_task -> create_hitl_task_with_json_input -> upload_...
    assert {"CreateArtifactUploadUrl", "CreateHitlTask", "StartHitlTask"} <= ops[
        "create_autoform_hitl_task"
    ]
    assert ops["download_artifact"] == frozenset()  # downloads from a URL it is given


@pytest.mark.passes_on_main  # the pins are unchanged; CI now installs them
def test_the_installed_aws_transform_packages_match_the_image_pins():
    from importlib.metadata import version

    pins = {}
    for line in _SDK_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name, pinned = line.split("==")
            pins[name] = pinned
    assert "agent-builder-agentic-mcp-aws-transform" in pins
    assert {name: version(name) for name in pins} == pins
