"""
SSM Run Command Executor

Executes database commands on a remote automation instance via SSM Run Command.
The automation instance has all DB drivers installed (mysql, psql, oracle, sqlserver).

Flow:
  Collector → SSM send_command → Automation EC2 → DB query → results back via SSM
"""

import logging
import re
import time
from typing import Any

from src.tools.aws.credentials import AWSCredentialManager

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300  # 5 minutes
POLL_INTERVAL = 2  # seconds


class SSMExecutor:
    """Execute commands on a remote automation instance via SSM Run Command."""

    def __init__(self, cred_mgr: AWSCredentialManager, instance_id: str):
        self.ssm = cred_mgr.client("ssm")
        self.instance_id = instance_id
        self._region = cred_mgr.region

    def run_command(self, command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """Execute a shell command on the automation instance. Returns stdout."""
        resp = self.ssm.send_command(
            InstanceIds=[self.instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
            TimeoutSeconds=timeout,
        )
        command_id = resp["Command"]["CommandId"]
        return self._wait_for_result(command_id, timeout)

    def run_sql(
        self,
        engine: str,
        host: str,
        port: int,
        database: str,
        secret_arn: str,
        sql: str,
        region: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> str:
        """
        Execute SQL via CLI on the automation instance.
        Credentials are fetched from Secrets Manager ON the automation instance.
        Password never leaves the instance or appears in SSM command history.
        """
        region = region or self._region
        cmd = _build_sql_command(engine, host, port, database, secret_arn, sql, region)
        return self.run_command(cmd, timeout)

    def run_sql_json(
        self,
        engine: str,
        host: str,
        port: int,
        database: str,
        secret_arn: str,
        sql: str,
        region: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> list[dict]:
        """Execute SQL and parse output as list of dicts."""
        raw = self.run_sql(engine, host, port, database, secret_arn, sql, region, timeout)
        return _parse_tabular_output(raw)

    def _wait_for_result(self, command_id: str, timeout: int) -> str:
        """Poll until command completes or times out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.ssm.get_command_invocation(
                    CommandId=command_id,
                    InstanceId=self.instance_id,
                )
            except Exception as e:
                if "InvocationDoesNotExist" in str(e):
                    time.sleep(  # nosemgrep: arbitrary-sleep  # polling for SSM invocation to register
                        POLL_INTERVAL
                    )
                    continue
                raise

            status = result.get("Status")
            if status == "Success":
                return result["StandardOutputContent"]  # type: ignore[no-any-return]
            if status in ("Failed", "Cancelled", "TimedOut"):
                stderr = result.get("StandardErrorContent", "")
                raise RuntimeError(
                    f"SSM command {status}: {stderr or result.get('StatusDetails', '')}"
                )
            # Status is Pending/InProgress/Delayed — keep polling
            time.sleep(POLL_INTERVAL)  # nosemgrep: arbitrary-sleep  # polling SSM command status

        raise TimeoutError(f"SSM command {command_id} timed out after {timeout}s")


def _build_sql_command(
    engine: str, host: str, port: int, database: str, secret_arn: str, sql: str, region: str
) -> str:
    """
    Build a shell command that:
    1. Fetches credentials from Secrets Manager ON the automation instance
    2. Executes SQL via the appropriate CLI
    Password never appears in the SSM command text.
    """
    safe_sql = sql.replace("'", "'\\''")

    # Credential fetch script (runs on automation instance)
    cred_fetch = (
        f"CREDS=$(aws secretsmanager get-secret-value --secret-id '{secret_arn}' "
        f"--region {region} --query SecretString --output text) && "
        f"DB_USER=$(echo $CREDS | python3 -c \"import sys,json;print(json.load(sys.stdin)['username'])\") && "
        f"DB_PASS=$(echo $CREDS | python3 -c \"import sys,json;print(json.load(sys.stdin)['password'])\")"
    )

    if engine in ("mysql", "mariadb"):
        return (
            f"{cred_fetch} && "
            f'mysql -h {host} -P {port} -u "$DB_USER" -p"$DB_PASS" '
            f"-D {database} -B -e '{safe_sql}'"
        )
    elif engine == "postgresql":
        return (
            f"{cred_fetch} && "
            f'PGPASSWORD="$DB_PASS" psql -h {host} -p {port} -U "$DB_USER" '
            f"-d {database} -F $'\\t' --no-align -c '{safe_sql}'"
        )
    elif engine == "sqlserver":
        return (
            f"{cred_fetch} && "
            f'sqlcmd -S {host},{port} -U "$DB_USER" -P "$DB_PASS" '
            f"-d {database} -s $'\\t' -W -h -1 -Q '{safe_sql}'"
        )
    elif engine == "oracle":
        return (
            f"{cred_fetch} && "
            f'echo \'{safe_sql}\' | sqlplus -S "$DB_USER"/"$DB_PASS"@'
            f"'(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST={host})(PORT={port}))"
            f"(CONNECT_DATA=(SERVICE_NAME={database})))'"
        )
    else:
        raise ValueError(f"Unsupported engine: {engine}")


def _parse_tabular_output(raw: str) -> list[dict]:
    """
    Parse tab-separated output from CLI tools into list of dicts.
    First line = headers (lowercased), remaining lines = data.
    Strips PostgreSQL footer lines like '(5 rows)'.
    """
    lines = [line for line in raw.strip().split("\n") if line.strip()]
    # Remove PostgreSQL row count footer
    lines = [line for line in lines if not re.match(r"^\(\d+ rows?\)$", line.strip())]
    if len(lines) < 2:
        return []

    headers = [h.strip().lower() for h in lines[0].split("\t")]
    rows = []
    for line in lines[1:]:
        values = [v.strip() for v in line.split("\t")]
        if len(values) == len(headers):
            row = {}
            for h, v in zip(headers, values, strict=False):
                row[h] = _coerce_value(v)
            rows.append(row)
    return rows


def _coerce_value(val: str) -> Any:
    """Try to convert string values to appropriate Python types."""
    if val in ("NULL", "\\N", ""):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val
