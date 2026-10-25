"""Hook 7: a Bash call must not print a cloud credential helper's output (#1165)."""

from __future__ import annotations

import pytest

from scripts.hooks.block_credential_output import violation
from tests.unit.hooks.conftest import bash

BLOCKED = [
    "ada credentials print --account 123456789012 --role Admin",
    "ada credentials print --account 123456789012 --role Admin --format json",
    "ada credentials print --account 1 --role r | jq -r .SecretAccessKey",
    "ada credentials print --account 1 --role r | tee out.txt",
    "echo $(ada credentials print --account 1 --role r)",
    'echo "$(ada credentials print --account 1 --role r)"',
    "cat <<< $(ada credentials print --account 1 --role r)",
    "aws configure export-credentials --profile p",
    "aws --profile p configure export-credentials --format env",
    "aws sts get-session-token",
    "aws sts assume-role --role-arn arn:aws:iam::1:role/r --role-session-name s",
    "aws sts assume-role-with-web-identity --role-arn x --web-identity-token y",
    "aws sts get-federation-token --name n",
    "aws ecr get-login-password --region us-east-1",
    "aws codeartifact get-authorization-token --domain d",
    "aws eks get-token --cluster-name c",
    "gcloud auth print-access-token",
    "gcloud auth application-default print-access-token",
    "az account get-access-token",
    "sudo ada credentials print --account 1 --role r",
    "cd /tmp && aws sts get-session-token",
    "ls; aws configure export-credentials",
    "aws sts get-session-token 2>&1",
    "aws sts get-session-token >&2",
    "aws sts get-session-token 2>/dev/null | cat",
    "/usr/local/bin/aws sts get-session-token",
    "/opt/homebrew/bin/aws configure export-credentials",
    "~/bin/ada credentials print --account 1 --role r",
    '"aws" sts get-session-token',
    "aws 'sts' get-session-token",
    'aws sts "get-session-token"',
    "\\aws sts get-session-token",
    "aws sts get-session-token > /dev/stdout",
    "aws sts get-session-token > /dev/stderr",
    "aws sts get-session-token >/dev/tty",
    "aws sts get-session-token 1> /dev/fd/2",
    "aws sts get-session-token | tee /dev/stderr > /dev/null",
    "aws sts get-session-token | tee /dev/tty",
    "aws ecr get-login-password | docker logout",
    "aws ecr get-login-password | docker login --username AWS",
    "aws ecr get-login-password | docker login --password-stdin | cat",
]
ALLOWED = [
    "aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 1.dkr.ecr.us-east-1.amazonaws.com",
    "aws ecr get-login-password | podman login --username AWS --password-stdin registry.example.com",
    "aws ecr get-login-password | helm registry login --username AWS --password-stdin registry.example.com",
    "aws sts get-session-token | tee /tmp/creds.json > /dev/null",
    "ada credentials print --account 1 --role r > /tmp/creds.json",
    "ada credentials print --account 1 --role r >> creds.json",
    "ada credentials print --account 1 --role r --format json 1> creds.json",
    "ada credentials print --account 1 --role r &> /dev/null",
    "ada credentials print --account 1 --role r | jq -r .SecretAccessKey > key.txt",
    "AWS_SECRET_ACCESS_KEY=$(ada credentials print --account 1 --role r | jq -r .SecretAccessKey)",
    'export TOKEN="$(aws ecr get-login-password --region us-east-1)"',
    "export TOKEN=$(gcloud auth print-access-token)",
    "TOKEN=`az account get-access-token --query accessToken -o tsv`",
    "local pw=$(aws ecr get-login-password)",
    'eval "$(aws configure export-credentials --format env)"',
    "eval $(ada credentials print --account 1 --role r --format env)",
    "source <(aws configure export-credentials --format env)",
    "aws ecr get-login-password | docker login --username AWS --password-stdin 1.dkr.ecr.us-east-1.amazonaws.com > /dev/null",
    "aws sts get-caller-identity",
    "aws sts get-caller-identity --query Account",
    "aws s3 ls",
    "aws configure list",
    "aws configure get region",
    "aws ecr describe-repositories",
    "gcloud auth list",
    "az account show",
    "ada credentials update --account 1 --role r",
    "git commit -m 'docs: ada credentials print writes to a file'",
    "gh issue comment 1 --body 'do not run aws sts get-session-token here'",
    "echo 'aws configure export-credentials'",
    "grep -rn 'ada credentials print' docs/",
    "uv run python scripts/run_assessment.py --file x",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command: str, run_hook) -> None:
    assert violation(command)
    res = run_hook("block_credential_output.py", bash(command))
    assert res.blocked
    assert res.stderr.count("\n") == 1 and "Capture it" in res.stderr
    assert "file" not in res.stderr  # never suggest writing credentials to disk


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command: str, run_hook) -> None:
    assert violation(command) is None
    assert run_hook("block_credential_output.py", bash(command)).code == 0


def test_the_reason_names_the_helper() -> None:
    assert "aws sts get-session-token" in (violation("aws sts get-session-token") or "")
    assert "ada credentials print" in (violation("ada credentials print --x 1") or "")


@pytest.mark.parametrize("event", ["", "not json", "[]", '{"tool_input": 3}', '{"tool_input": {}}'])
def test_bad_input_fails_open(event: str, run_hook) -> None:
    assert run_hook("block_credential_output.py", event).code == 0


def test_other_tools_are_not_checked(run_hook) -> None:
    event = {"tool_name": "Read", "tool_input": {"command": "aws sts get-session-token"}}
    assert run_hook("block_credential_output.py", event).code == 0


def test_the_match_is_linear_in_the_command_length() -> None:
    """Repeated words must not backtrack: a 6 KB command once took ~42 s (#1165 review)."""
    from tests.unit.tools.sql._timing import assert_linear

    sizes = (20_000, 160_000)
    for unit in ("gcloud auth ", "aws sts ", "FOO=bar ", "ada credentials ", "aws ", "$(", "x | "):

        def make_text(n: int, unit: str = unit) -> str:
            return (unit * (n // len(unit) + 1))[:n]

        assert_linear(make_text, violation, sizes=sizes)
