# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""IAM least privilege in the automation template (#1723).

No AWS call: the template is decoded with cfn-lint's parser and its policy
statements are read as data. The Secrets Manager scope is checked against the
secret names the code really uses, and IAM wildcards in actions are expanded
from botocore's API model, so ``secretsmanager:*`` cannot widen it unnoticed.
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
