# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live SQL Server command reads a whole statement (#1884).

sqlcmd shows ``NVARCHAR(MAX)`` (what ``sys.dm_exec_sql_text`` returns) at 256
characters unless ``-y 0`` is given, and ``-W`` cannot be combined with ``-y 0``.
So the live command runs ``-y 0`` and the tabular parser strips the padding that
``-W`` used to remove. There is no SQL Server here: the output below is shaped
like sqlcmd's (header, dashes line, space-padded columns, row-count footer).
"""

from __future__ import annotations

import hashlib
import shlex
from unittest.mock import MagicMock

import pytest

from src.tools.aws.ssm_executor import SSMExecutor, _build_sql_command, _parse_tabular_output
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA"  # pragma: allowlist secret  # nosec B105 — test fixture ARN
OLD_WIDTH = 256


def _command() -> str:
    return _build_sql_command(
        "sqlserver", "db.example", 1433, "app", SECRET_ARN, "SELECT 1", "us-east-1"
    )


def _sqlcmd_args(command: str) -> list[str]:
    return shlex.split(command.split("sqlcmd ", 1)[1].replace("$'\\t'", "TAB"))


def _padded(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    """sqlcmd without -W: every column padded to its display width, tab separated."""

    def line(cells: list[str]) -> str:
        return "\t".join(c.ljust(w) for c, w in zip(cells, widths, strict=True))

    out = [line(headers), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    out.append(f"\n({len(rows)} rows affected)")
    return "\n".join(out)


class TestTheCommand:
    def test_runs_with_unlimited_width_and_not_the_trim_flag(self) -> None:
        args = _sqlcmd_args(_command())
        assert args[args.index("-y") + 1] == "0"
        # -W cannot be combined with -y 0 (sqlcmd rejects it).
        assert "-W" not in args

    @pytest.mark.passes_on_main
    def test_keeps_the_other_flags(self) -> None:
        args = _sqlcmd_args(_command())
        assert args[args.index("-s") + 1] == "TAB"
        assert args[args.index("-k") + 1] == "1"
        assert "-C" in args
        assert "-h" not in args  # the parser needs the header line

    @pytest.mark.passes_on_main
    def test_every_live_sqlcmd_call_goes_through_the_builder(self) -> None:
        from pathlib import Path

        hits = [
            str(p)
            for p in Path("src").rglob("*.py")
            if "sqlcmd -S {" in p.read_text(encoding="utf-8")
        ]
        assert hits == ["src/tools/aws/ssm_executor.py"]


@pytest.mark.passes_on_main  # the parser already strips; these pin what -y 0 relies on
class TestPaddedOutput:
    def test_padding_is_stripped_from_every_column_and_header(self) -> None:
        raw = _padded(
            ["Name", "Count", "Stamp"],
            [["alpha", "         7", "2026-01-01"], ["b", "        42", "2026-01-02"]],
            [12, 10, 23],
        )
        assert _parse_tabular_output(raw) == [
            {"name": "alpha", "count": 7, "stamp": "2026-01-01"},
            {"name": "b", "count": 42, "stamp": "2026-01-02"},
        ]

    def test_null_and_empty_padded_cells(self) -> None:
        raw = _padded(["a", "b"], [["NULL", ""], ["x", "y"]], [8, 8])
        assert _parse_tabular_output(raw) == [{"a": None, "b": None}, {"a": "x", "b": "y"}]

    def test_a_value_beyond_the_old_width_is_whole(self) -> None:
        text = "SELECT " + ", ".join(f"col_{i}" for i in range(200)) + " FROM dbo.Wide"
        assert len(text) > 3 * OLD_WIDTH
        raw = _padded(["query_id", "query_text", "n"], [["0xAB", text, "3"]], [10, len(text), 5])
        (row,) = _parse_tabular_output(raw)
        assert row["query_text"] == text

    def test_long_value_with_encoded_line_breaks_is_not_cut(self) -> None:
        # Two characters per CR, LF, tab and backslash, as the encoded form carries them.
        text = "SELECT a,\\r\\n\\tb\\\\c\\r\\nFROM t\\r\\n" * 30 + "WHERE x = 1"
        assert len(text) > 3 * OLD_WIDTH
        raw = _padded(["query_text", "n"], [[text, "1"]], [len(text) + 40, 5])
        (row,) = _parse_tabular_output(raw)
        assert row["query_text"] == text
        assert row["n"] == 1


@pytest.mark.passes_on_main  # the parser already strips; these pin what -y 0 relies on
class TestThroughTheCollector:
    def _collector(self, raw: str) -> SQLServerRemoteCollector:
        cred = MagicMock()
        cred.region = "us-east-1"
        ssm = SSMExecutor(cred, "i-0123456789abcdef0")
        ssm.run_command = MagicMock(return_value=raw)  # type: ignore[method-assign]
        return SQLServerRemoteCollector(
            ssm=ssm,
            host="db.example",
            port=1433,
            database="app",
            secret_arn=SECRET_ARN,
            region="us-east-1",
        )

    HEADERS = [
        "query_id", "query_text", "execution_count", "total_logical_reads",
        "total_physical_reads", "total_worker_time", "total_elapsed_time", "total_rows",
        "min_elapsed_time", "max_elapsed_time", "creation_time", "last_execution_time",
    ]  # fmt: skip

    def _row(self, query_id: str, text: str) -> list[str]:
        return [query_id, text, "12", "100", "0", "500", "900", "40", "1", "9",
                "2026-01-01 00:00:00.000", "2026-01-02 00:00:00.000"]  # fmt: skip

    def test_a_statement_longer_than_256_characters_arrives_whole(self) -> None:
        text = "SELECT " + ", ".join(f"c{i}" for i in range(150)) + " FROM dbo.T WHERE id = 1"
        raw = _padded(self.HEADERS, [self._row("0xAB12", text)], [10, len(text)] + [12] * 10)
        (p,) = self._collector(raw).collect_query_patterns()
        assert p["query_text"] == text
        assert p["query_id"] == "0xAB12"

    @pytest.mark.parametrize("text", ["SELECT 1 FROM dbo.T", "UPDATE dbo.T SET a = 1 WHERE id = 2"])
    def test_the_text_hash_fallback_is_what_it_was_for_untruncated_text(self, text: str) -> None:
        # No query_hash: the id is a hash of the text. Old (-W) and new (-y 0) shapes agree.
        old = "query_id\tquery_text\n--------\t----------\n\t" + text + "\n(1 rows affected)"
        new = _padded(["query_id", "query_text"], [["", text]], [10, len(text) + 30])
        want = hashlib.sha256(text.encode()).hexdigest()[:16]
        for raw in (old, new):
            rows = _parse_tabular_output(raw)
            assert rows[0]["query_text"] == text
            assert hashlib.sha256(str(rows[0]["query_text"]).encode()).hexdigest()[:16] == want

    def test_a_long_multiline_statement_decodes_whole_and_hashes_whole(self) -> None:
        stmt = "SELECT a -- note\r\n\tFROM t\r\n" * 30 + "WHERE p = 'C:\\x'"
        encoded = (
            stmt.replace("\\", "\\\\")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            .replace("\t", "\\t")
        )
        assert len(encoded) > 3 * OLD_WIDTH
        raw = _padded(["query_id", "query_text"], [["", encoded]], [10, len(encoded) + 20])
        (p,) = self._collector(raw).collect_query_patterns()
        assert p["query_text"] == stmt.strip()
        flat = stmt.strip().replace("\r", " ").replace("\n", " ").replace("\t", " ")
        assert p["query_id"] == hashlib.sha256(flat.encode()).hexdigest()[:16]
