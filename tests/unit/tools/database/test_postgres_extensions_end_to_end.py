# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL's installed extensions reach the relational-move gap check (#1078).

``scripts/collect-postgresql.sql`` and the live collector read ``pg_extension`` (name and
version); both offline builders and the live one keep them on ``metadata.extensions``.
A collection written before #1078 has no extension list: it loads with ``None`` (not
collected), and the extension gap rules then find nothing, as before.

No real database runs here: the live collector's database calls are mocked and its
stages canned, as in ``test_collector_source_settings.py``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import sqlglot

from src.agents.collector import postgres_collector
from src.agents.collector.mysql_collector import source_extensions
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_input import CollectorInput
from src.contracts.collector_output import CollectorOutputContract, SourceExtension
from src.tools.analysis.relational_gaps import detect_relational_gaps
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "collect-postgresql.sql"

# As the script writes them: one entry per installed extension.
EXTENSIONS = [
    {"name": "plpgsql", "version": "1.0"},
    {"name": "hstore", "version": "1.8"},
    {"name": "vector", "version": "0.8.0"},
    {"name": "acme_geo", "version": "2.1"},
]


# -- the collection script -------------------------------------------------------------


def _extensions_block() -> str:
    """The script's ``extensions_data`` CTE body (the SELECT inside it)."""
    text = SCRIPT.read_text()
    match = re.search(r"\nextensions_data AS \((.*?)\n\)", text, re.S)
    assert match, "collect-postgresql.sql has an extensions_data block"
    return match.group(1)


def test_the_script_reads_every_installed_extension_and_its_version():
    block = _extensions_block()

    assert "FROM pg_extension" in block
    assert "'name', extname" in block or "extname AS name" in block
    assert "extversion AS version" in block or "'version', extversion" in block
    assert "'extensions', (SELECT data FROM extensions_data)" in SCRIPT.read_text()


def test_the_extensions_block_parses_as_postgresql():
    tree = sqlglot.parse_one(_extensions_block(), read="postgres")

    assert {t.name for t in tree.find_all(sqlglot.exp.Table)} == {"pg_extension"}


# -- the offline builder ---------------------------------------------------------------


def _raw(extensions: Any = None, *, with_section: bool = True) -> dict:
    raw: dict[str, Any] = {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "app"},
        "tables": [{"table_name": "t", "row_count": 10}],
        "columns": [
            {"table_name": "t", "column_name": "id", "data_type": "integer", "is_nullable": "NO"}
        ],
        "queries": [],
    }
    if with_section:
        raw["extensions"] = EXTENSIONS if extensions is None else extensions
    return raw


def _offline(raw: dict) -> CollectorOutputContract:
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1078", database_name="app", s3_key="k"
    )
    return CollectorOutputContract.model_validate(output)


def test_the_offline_builder_keeps_each_extension_and_its_version():
    contract = _offline(_raw())

    assert contract.metadata.extensions == [SourceExtension(**e) for e in EXTENSIONS]


def test_a_collection_without_the_section_has_no_extension_list():
    assert _offline(_raw(with_section=False)).metadata.extensions is None


def test_an_empty_section_is_a_list_with_nothing_installed():
    assert _offline(_raw([])).metadata.extensions == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("vector", None),
        ({"name": "vector"}, None),
        ([], []),
        (
            [{"name": " Vector ", "version": "0.8.0"}, {"name": "pg_trgm", "version": None}],
            [SourceExtension(name="vector", version="0.8.0"), SourceExtension(name="pg_trgm")],
        ),
        # An entry with no name is not an extension; a duplicate is listed once.
        (
            [{"version": "1.0"}, "x", {"name": ""}, {"name": "citext"}, {"name": "citext"}],
            [SourceExtension(name="citext")],
        ),
    ],
)
def test_only_named_extensions_are_kept(raw: Any, expected: Any):
    assert source_extensions(raw) == expected


def test_an_entry_the_contract_refuses_is_dropped_with_a_warning(caplog):
    """A hand-made offline file: one bad entry costs that entry, not the collection."""
    raw = [
        {"name": "x" * 129, "version": "1.0"},
        {"name": "hstore", "version": "9" * 65},
        {"name": "citext", "version": "1.6"},
    ]
    with caplog.at_level(logging.WARNING):
        kept = source_extensions(raw)
    assert kept == [SourceExtension(name="citext", version="1.6")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and all("extension entry" in w for w in warnings)
    # The warning names the position, never the entry's text.
    assert not any("xxxx" in w or "9999" in w for w in warnings)


def test_a_collection_with_a_bad_entry_still_builds():
    contract = _offline(_raw([{"name": "x" * 129}, {"name": "hstore", "version": "1.8"}]))
    assert contract.metadata.extensions == [SourceExtension(name="hstore", version="1.8")]


def test_an_older_collector_output_loads_without_extensions():
    output = json.loads(_offline(_raw()).model_dump_json())
    del output["metadata"]["extensions"]

    assert CollectorOutputContract.model_validate(output).metadata.extensions is None


# -- the live collector ----------------------------------------------------------------


def test_the_live_tools_read_pg_extension():
    ssm = MagicMock()
    ssm.run_sql_json.return_value = [{"name": "hstore", "version": "1.8"}]
    db = PostgreSQLRemoteCollector(
        ssm=ssm, host="h", port=5432, database="d", secret_arn="s", region="r"
    )

    assert db.collect_extensions() == [{"name": "hstore", "version": "1.8"}]
    sql = ssm.run_sql_json.call_args.kwargs["sql"]
    assert re.search(r"\bFROM pg_extension\b", sql)
    assert re.search(r"\bextname AS name\b", sql) and re.search(r"\bextversion AS version\b", sql)


class _Checkpoints:
    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages

    def load_or_run(self, name: str, fn: Any) -> Any:
        return self.stages[name] if name in self.stages else fn()


def _run_live(schema_extra: dict[str, Any]) -> CollectorOutputContract:
    inp = CollectorInput.model_validate(
        {
            "job_id": "j1078",
            "engine": "postgresql",
            "cluster_endpoint": "db.example",
            "port": 5432,
            "database_name": "app",
            "mode": "live",
            "live_config": {"secret_arn": "s", "automation_instance_id": "i"},
        }
    )
    table = {
        "table_id": "app.t",
        "table_name": "t",
        "columns": [{"column_name": "id", "data_type": "integer", "is_nullable": "NO"}],
    }
    stages = {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "db_size_gb": 1.0},
        "schema": {"tables": [table], "global_stats": {}, **schema_extra},
        "queries": [],
        "aws_metrics": {},
    }
    with (
        patch.object(postgres_collector, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(postgres_collector, "SSMExecutor", MagicMock()),
        patch.object(postgres_collector, "PostgreSQLRemoteCollector", MagicMock()),
    ):
        return postgres_collector._collect_live(inp, _Checkpoints(stages))


def test_a_live_collection_keeps_the_extensions_it_read():
    result = _run_live({"extensions": EXTENSIONS})

    assert result.metadata.extensions == [SourceExtension(**e) for e in EXTENSIONS]


def test_a_live_collection_from_an_older_checkpoint_has_no_extension_list():
    assert _run_live({}).metadata.extensions is None


def test_the_schema_stage_reads_the_extensions_and_survives_a_failure():
    db = MagicMock()
    db.collect_tables.return_value = []
    db.collect_extensions.return_value = [{"name": "hstore", "version": "1.8"}]
    assert postgres_collector._collect_schema_raw(db)["extensions"] == [
        {"name": "hstore", "version": "1.8"}
    ]

    db.collect_extensions.side_effect = RuntimeError("denied")
    assert postgres_collector._collect_schema_raw(db)["extensions"] is None


# -- end to end: a collection file through run_collect.py to the gap check -------------


def _discourse(dest: Path) -> dict:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "discourse" / "discourse.zip") as z:
        z.extractall(dest)
    text = (dest / "discourse-collection.json").read_text()
    data: dict = json.loads(text[text.index("{") :])
    return data


def _collect(src: Path, root: Path) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "sample",
            "--job-id",
            "ext01078",
            "--artifact-root",
            str(root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    output: dict = json.loads(
        (root / "sample" / "ext01078" / "collector" / "output.json").read_text()
    )
    return output


def test_run_collect_carries_the_extensions_to_the_gap_check(tmp_path: Path):
    raw = _discourse(tmp_path / "in")
    raw["extensions"] = EXTENSIONS
    src = tmp_path / "with-extensions.json"
    src.write_text(json.dumps(raw))

    output = _collect(src, tmp_path / "a")

    assert output["metadata"]["extensions"] == EXTENSIONS
    gaps = detect_relational_gaps(output, "aurora_postgresql") or {}
    by_rule = {g["rule_id"]: g for g in gaps["gaps"]}
    not_offered = by_rule["postgresql-extension-not-offered"]
    assert not_offered["impact"] == "blocks_move"
    # Only the extension Aurora PostgreSQL does not offer; hstore, vector, plpgsql it does.
    assert not_offered["objects"] == [{"kind": "extension", "id": "acme_geo", "note": None}]


def test_the_bundled_discourse_sample_has_no_extension_list(tmp_path: Path):
    raw = _discourse(tmp_path / "in")
    src = tmp_path / "discourse.json"
    src.write_text(json.dumps(raw))

    output = _collect(src, tmp_path / "a")

    assert output["metadata"]["extensions"] is None
    gaps = detect_relational_gaps(output, "aurora_postgresql") or {}
    assert not any(g["rule_id"].startswith("postgresql-extension-") for g in gaps["gaps"])
