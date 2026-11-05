# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A database client's error fails the live query, never reads as rows (#1880 review).

sqlplus with no ``WHENEVER SQLERROR`` and sqlcmd with no ``-b`` exit 0 after an error and
print it on stdout, where the tabular parser read it as rows (or as no rows). The live
commands now stop the client on an error, so SSM reports the command Failed, and the
parser refuses output holding an error line. The outputs below are shaped like the
clients' (no database here).
"""

from __future__ import annotations

import shlex
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.tools.aws.ssm_executor import SSMExecutor, _build_sql_command, _parse_tabular_output
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 — test fixture ARN


def _client_error(match: str | None = None) -> Any:
    """``pytest.raises`` for the parser's ``SqlClientError`` (a ``RuntimeError``),
    named by its class so this file imports on main's code."""
    return pytest.raises(RuntimeError, match=match)


SQLCMD_ROWS = "query_id\tquery_text\texecution_count\n--------\t----------\t---------------\n"
SQLCMD_ROWS += "0xAB12\tSELECT 1 FROM dbo.T\t12\n"
SQLCMD_ERROR = (
    "Msg 208, Level 16, State 1, Server db, Line 1\nInvalid object name 'sys.dm_exec_nope'.\n"
)


def _sqlcmd_args() -> list[str]:
    command = _build_sql_command(
        "sqlserver", "db.example", 1433, "app", SECRET_ARN, "SELECT 1", "us-east-1"
    )
    return shlex.split(command.split("sqlcmd ", 1)[1].replace("$'\\t'", "TAB"))


class TestSqlServer:
    def test_sqlcmd_exits_on_an_error(self) -> None:
        assert "-b" in _sqlcmd_args()

    @pytest.mark.parametrize("output", [SQLCMD_ERROR, SQLCMD_ROWS + SQLCMD_ERROR])
    def test_an_error_line_raises(self, output: str) -> None:
        with _client_error("Msg 208, Level 16"):
            _parse_tabular_output(output, "sqlserver")

    def test_an_error_through_the_collector_is_not_a_pattern(self) -> None:
        cred = MagicMock()
        cred.region = "us-east-1"
        ssm = SSMExecutor(cred, "i-0123456789abcdef0")
        ssm.run_command = MagicMock(return_value=SQLCMD_ROWS + SQLCMD_ERROR)  # type: ignore[method-assign]
        db = SQLServerRemoteCollector(
            ssm=ssm, host="db.example", port=1433, database="app", secret_arn=SECRET_ARN
        )
        with _client_error():
            db.collect_query_patterns()

    @pytest.mark.passes_on_main
    def test_an_informational_message_is_not_an_error(self) -> None:
        # Severity 10 or less is informational: sqlcmd -b does not stop on it either.
        output = SQLCMD_ROWS + "Msg 50000, Level 10, State 1, Server db, Line 1\nnote\n"
        assert _parse_tabular_output(output, "sqlserver")[0]["query_id"] == "0xAB12"


class TestOracleAndOthers:
    def test_an_oracle_error_line_raises(self) -> None:
        with _client_error("ORA-01017") as err:
            _parse_tabular_output("ERROR:\nORA-01017: invalid username/password\n", "oracle")
        assert type(err.value).__name__ == "SqlClientError"
        assert str(err.value) == "oracle client error: ERROR:; ORA-01017: invalid username/password"

    @pytest.mark.passes_on_main
    def test_other_engines_and_no_engine_read_as_before(self) -> None:
        # MySQL and PostgreSQL print errors on stderr; a value is never an error there.
        out = "msg\nORA-00001 seen\n"
        assert _parse_tabular_output(out, "mysql") == [{"msg": "ORA-00001 seen"}]
        assert _parse_tabular_output(out) == [{"msg": "ORA-00001 seen"}]


class TestFailedCommand:
    def _executor(self, result: dict[str, str]) -> SSMExecutor:
        cred = MagicMock()
        cred.region = "us-east-1"
        cred.client.return_value.get_command_invocation.return_value = result
        return SSMExecutor(cred, "i-0123456789abcdef0")

    def test_names_the_client_error_lines_not_the_rows(self) -> None:
        stdout = "QUERY_ID\n--------\nfms_1 secret_row\nERROR at line 3:\nORA-00942: no table\n"
        ssm = self._executor(
            {"Status": "Failed", "StandardOutputContent": stdout, "StatusDetails": "Failed"}
        )
        with pytest.raises(RuntimeError) as err:
            ssm._wait_for_result("cmd", timeout=5)
        assert str(err.value) == "SSM command Failed: ERROR at line 3:; ORA-00942: no table"
        assert "secret_row" not in str(err.value)

    @pytest.mark.passes_on_main
    def test_stderr_still_comes_first(self) -> None:
        ssm = self._executor(
            {"Status": "Failed", "StandardErrorContent": "boom", "StandardOutputContent": ""}
        )
        with pytest.raises(RuntimeError, match="SSM command Failed: boom"):
            ssm._wait_for_result("cmd", timeout=5)
