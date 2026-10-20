"""ADR-004 Decision 5: IAM roles, no passwords in code, input or environment (#702).

Database credentials live in Secrets Manager and are fetched through the IAM role; the
collector's input names a secret ARN, never a password; the command run on the automation
instance fetches the secret there instead of carrying it.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts.collector_input import CollectorInput
from src.tools.aws.rds_tools import get_database_credentials
from src.tools.aws.ssm_executor import _build_sql_command
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

REPO_ROOT = Path(__file__).resolve().parents[4]
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:creds-AbCdEf"  # nosec B105  # pragma: allowlist secret
PASSWORD_ENV_READ = re.compile(
    r"""(?:environ(?:\.get)?\s*[\[(]|getenv\s*\()\s*["'][A-Za-z_]*(?:PASS|PWD)""", re.IGNORECASE
)


def _key_names(node: object) -> set[str]:
    """Every dict key and JSON-schema property name, at any depth."""
    if isinstance(node, dict):
        return {str(k) for k in node} | set().union(*(_key_names(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_key_names(v) for v in node)) if node else set()
    return set()


class TestDatabaseCredentialsComeFromSecretsManager:
    def test_the_secret_is_fetched_by_arn_through_the_iam_role(self) -> None:
        cred_mgr = StubbedCredMgr()
        cred_mgr.stub("secretsmanager").add_response(
            "get_secret_value",
            {"SecretString": json.dumps({"username": "app", "password": "from-the-secret"})},
            {"SecretId": SECRET_ARN},
        )

        creds = get_database_credentials(cred_mgr, SECRET_ARN)

        cred_mgr.stubbers["secretsmanager"].assert_no_pending_responses()
        assert (creds["username"], creds["password"]) == ("app", "from-the-secret")

    @pytest.mark.parametrize("variable", ["DB_PASSWORD", "MYSQL_PWD", "PGPASSWORD", "PASSWORD"])
    def test_an_environment_password_is_never_used(self, variable, monkeypatch) -> None:
        monkeypatch.setenv(variable, "from-the-environment")
        cred_mgr = StubbedCredMgr()
        cred_mgr.stub("secretsmanager").add_response(
            "get_secret_value",
            {"SecretString": json.dumps({"username": "app", "password": "from-the-secret"})},
        )

        creds = get_database_credentials(cred_mgr, SECRET_ARN)

        assert creds["password"] == "from-the-secret"  # pragma: allowlist secret

    def test_no_collector_side_module_reads_a_password_from_the_environment(self) -> None:
        packages = [REPO_ROOT / "src" / "tools" / "aws", REPO_ROOT / "src" / "agents" / "collector"]
        contract = REPO_ROOT / "src" / "contracts" / "collector_input.py"
        files = [contract, *(f for d in packages for f in sorted(d.rglob("*.py")))]
        assert contract.is_file()
        assert all(any(d.rglob("*.py")) for d in packages)
        offenders = [f.name for f in files if PASSWORD_ENV_READ.search(f.read_text())]
        assert not offenders, f"passwords read from the environment in: {offenders}"


class TestTheCollectorInputCarriesNoPassword:
    def test_the_contract_has_no_password_field(self) -> None:
        names = _key_names(CollectorInput.model_json_schema())
        assert not [n for n in names if re.search(r"pass|pwd", n, re.IGNORECASE)], names

    def test_a_password_never_reaches_the_collector_input(self) -> None:
        """Either the input is rejected or the password is dropped; it is never kept."""
        payload = {
            "job_id": "j",
            "engine": "mysql",
            "cluster_endpoint": "db.example.internal",
            "port": 3306,
            "database_name": "app",
            "password": "top-level",  # pragma: allowlist secret
            "live_config": {
                "secret_arn": SECRET_ARN,
                "automation_instance_id": "i-0123",
                "password": "nested",  # pragma: allowlist secret
            },
        }

        try:
            inp = CollectorInput.model_validate(payload)
        except ValidationError:
            return  # rejecting a password is as good as dropping it

        dumped = json.dumps(inp.model_dump(mode="json"))
        assert "top-level" not in dumped
        assert "nested" not in dumped


class TestTheAutomationCommandCarriesNoPassword:
    ENGINES = ["mysql", "mariadb", "postgresql", "sqlserver", "oracle"]

    def test_the_builder_takes_a_secret_arn_and_no_password(self) -> None:
        params = inspect.signature(_build_sql_command).parameters
        assert "secret_arn" in params
        assert not [p for p in params if re.search(r"pass|pwd", p, re.IGNORECASE)]

    @pytest.mark.parametrize("engine", ENGINES)
    def test_the_secret_is_fetched_on_the_instance_and_only_a_variable_is_passed(
        self, engine
    ) -> None:
        command = _build_sql_command(
            engine, "db.example.internal", 3306, "app", SECRET_ARN, "SELECT 1", "us-east-1"
        )

        assert f"aws secretsmanager get-secret-value --secret-id '{SECRET_ARN}'" in command
        assert "$DB_PASS" in command
        # Strip the only places the word may appear: the variable and the key read from the
        # secret on the instance. Anything left would be a password written into the command.
        remainder = re.sub(r"\$\{?DB_PASS\}?|DB_PASS=|PGPASSWORD=|\['password'\]", "", command)
        assert not re.search(r"pass|pwd", remainder, re.IGNORECASE), remainder
