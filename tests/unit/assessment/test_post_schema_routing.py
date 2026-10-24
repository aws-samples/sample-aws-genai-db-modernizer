"""``assess()`` runs the pipeline's post-schema routing after schema design (#1492).

A statement a design reports unsupported moves, through the move ledger, to its best
remaining engine in the engine scope; only the engines the move touches are designed
again, and the result is read at the new version. The local pipeline reaches the same
core entry point (``core.run_post_schema_routing_core``). Tests never call a model: the
model entry points are the shared stand-ins (``tests/assess_models.py``, the list the
consumer contract tests patch too), with the designer's stand-in reporting a statement
unsupported.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.assessment import AssessmentResult, assess
from src.assessment.runner import AssessmentRunError
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import DESIGNER, models_patched, report_unsupported, write_fake_design
from tests.unit.assessment.conftest import collection

DB = "wordpress"

CORE_ENTRY = "src.atx_orchestrator.core.run_post_schema_routing_core"


class _FlaggingDesigner:
    """Writes the designed wordpress job's designs; the first DynamoDB design reports
    one statement unsupported: the first DynamoDB read (by id) in no placement unit,
    so it moves alone. ``fail_redesign`` makes every design after the first round
    raise."""

    def __init__(self, flag: bool = True, fail_redesign: bool = False) -> None:
        self.flag = flag
        self.fail_redesign = fail_redesign
        self.calls: list[tuple[str, int]] = []
        self.first_version: int | None = None
        self.flagged: str | None = None

    def __call__(self, **kw: Any) -> None:
        engine, version, store = kw["target_type"], kw["assignment_version"], kw["store"]
        self.calls.append((engine, version))
        if self.first_version is None:
            self.first_version = version
        if self.fail_redesign and version != self.first_version:
            raise RuntimeError("model unavailable")
        write_fake_design(kw["job_id"], kw["database_name"], engine, store, version)
        if self.flag and engine == "dynamodb" and version == self.first_version:
            self._report_unsupported(store, kw["database_name"], kw["job_id"], version)

    def _report_unsupported(self, store: Any, db: str, job_id: str, version: int) -> None:
        self.flagged = report_unsupported(store, db, job_id, version)

    def at(self, version: int) -> list[str]:
        return sorted(engine for engine, v in self.calls if v == version)


def _run(
    job_id: str, designer: _FlaggingDesigner, root: Any, **options: Any
) -> tuple[AssessmentResult, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=str(root))
    with models_patched() as mocks:
        # The shared designer stand-in, with this test's unsupported report on top.
        mocks[DESIGNER].side_effect = designer
        result = assess(
            {
                "database_name": DB,
                "collection": collection(DB),
                "options": {
                    "run_through": "schema_design",
                    "deterministic": False,
                    "include_artifacts": True,
                    **options,
                },
            },
            store=store,
            job_id=job_id,
        )
    return result, store


def _statement(result: AssessmentResult, query_id: str | None) -> Any:
    return next(s for s in result.statements if s.query_id == query_id)


def test_a_statement_a_design_cannot_serve_moves_and_only_touched_engines_redesign(
    tmp_path: Any,
) -> None:
    designer = _FlaggingDesigner()
    result, store = _run("psrmove", designer, tmp_path)
    first = designer.first_version
    assert first is not None and designer.flagged

    moved = _statement(result, designer.flagged)
    # The only non-cache engine left once DynamoDB cannot serve it.
    assert moved.engine == "aurora_mysql"
    assert moved.reason_code == "post_schema_reroute"
    assert result.summary.assignment_version == first + 1
    # Only the engine it left and the one it joined are designed again.
    assert designer.at(first) == ["aurora_mysql", "dynamodb", "elasticache"]
    assert designer.at(first + 1) == ["aurora_mysql", "dynamodb"]
    # Every other statement keeps its engine and reason.
    for statement in result.statements:
        if statement.query_id != designer.flagged:
            assert statement.reason_code != "post_schema_reroute"

    assert result.label == "designed"
    assert result.summary.designed_engines == ["aurora_mysql", "dynamodb", "elasticache"]
    assert result.artifacts is not None
    blocks = result.artifacts.model_dump()
    for engine, key in blocks["artifact_keys"]["schema_designs"].items():
        assert key == f"{DB}/psrmove/schema-{engine}/v{first + 1}/schema_output.json"
        assert store.exists(key)
    # The move is the ledger's: one post_schema_flag Move on the new version.
    moves = blocks["assignment"]["moves"]
    assert [(m["query_id"], m["pass"], m["to_engine"]) for m in moves] == [
        (designer.flagged, "post_schema_flag", "aurora_mysql")
    ]
    assert blocks["assignment"]["source"] == "post_schema_routing"


def test_nothing_moves_when_no_design_flags_a_statement(tmp_path: Any) -> None:
    designer = _FlaggingDesigner(flag=False)
    result, store = _run("psrnone", designer, tmp_path)
    first = designer.first_version
    assert result.summary.assignment_version == first
    assert designer.at(first) == ["aurora_mysql", "dynamodb", "elasticache"]
    assert len(designer.calls) == 3
    assert "post_schema_reroute" not in {s.reason_code for s in result.statements}
    assert not store.exists(f"{DB}/psrnone/assignment/v{first + 1}/assignment.json")


def test_a_reroute_never_leaves_the_engine_scope(tmp_path: Any) -> None:
    """Without Aurora MySQL in scope, the flagged DynamoDB read has no remaining engine
    (a cache is never a destination): it stays where it is, listed as terminal, and no
    version or redesign follows."""
    designer = _FlaggingDesigner()
    result, store = _run("psrscope", designer, tmp_path, target_engines=["dynamodb", "elasticache"])
    first = designer.first_version
    assert first is not None and designer.flagged
    flagged = _statement(result, designer.flagged)
    assert flagged.engine == "dynamodb"
    assert flagged.reason_code != "post_schema_reroute"
    assert result.summary.assignment_version == first
    assert {s.engine for s in result.statements} <= {"dynamodb", None}
    assert designer.at(first) == ["dynamodb", "elasticache"]
    assert len(designer.calls) == 2
    router = store.read_json(f"{DB}/psrscope/post-schema-router/router_output.json")
    assert router["routings"] == []
    assert router["terminal_queries"] == [designer.flagged]


def test_a_failed_redesign_builds_no_result(tmp_path: Any) -> None:
    """The routing writes the rollback version first, then the run fails."""
    designer = _FlaggingDesigner(fail_redesign=True)
    with pytest.raises(AssessmentRunError, match="post-schema routing failed"):
        _run("psrfail", designer, tmp_path)
    first = designer.first_version
    assert first is not None
    store = LocalArtifactStore(base_dir=str(tmp_path))
    rollback = store.read_json(f"{DB}/psrfail/assignment/v{first + 2}/assignment.json")
    assert rollback["status"] == "reroute_rolled_back"
    # The flagged statement is back on DynamoDB, through the reverse Move.
    engines = {qa["query_id"]: qa["assigned_engine"] for qa in rollback["query_assignments"]}
    assert engines[designer.flagged] == "dynamodb"
    assert [
        (m["query_id"], m["pass"], m["reason_code"], m["from_engine"], m["to_engine"])
        for m in rollback["moves"]
    ] == [
        (
            designer.flagged,
            "post_schema_flag",
            "rollback.redesign_failed",
            "aurora_mysql",
            "dynamodb",
        )
    ]
    # Every other statement is where the version before the reroute had it.
    before = store.read_json(f"{DB}/psrfail/assignment/v{first}/assignment.json")
    assert engines == {qa["query_id"]: qa["assigned_engine"] for qa in before["query_assignments"]}


def test_synthesis_after_a_reroute_reads_the_new_version(tmp_path: Any) -> None:
    """At ``"synthesis"`` the report is written for the reroute's version and the
    result, its designs and its report are all read at that version."""
    designer = _FlaggingDesigner()
    result, store = _run("psrsyn", designer, tmp_path, run_through="synthesis")
    first = designer.first_version
    assert first is not None and designer.flagged
    rerouted = first + 1
    assert result.summary.run_through_reached == "synthesis"
    assert result.summary.assignment_version == rerouted
    moved = _statement(result, designer.flagged)
    assert (moved.engine, moved.reason_code) == ("aurora_mysql", "post_schema_reroute")

    assert result.artifacts is not None
    blocks = result.artifacts.model_dump()
    key = f"{DB}/psrsyn/synthesis/v{rerouted}/report.json"
    assert blocks["artifact_keys"]["synthesis_report"] == key
    assert not store.exists(f"{DB}/psrsyn/synthesis/v{first}/report.json")
    report = store.read_json(key)
    assert blocks["synthesis_report"] == report
    assert blocks["assignment"]["version"] == rerouted
    for engine, design_key in blocks["artifact_keys"]["schema_designs"].items():
        assert design_key == f"{DB}/psrsyn/schema-{engine}/v{rerouted}/schema_output.json"
    # The report states the reroute: the flagged statement, on the engine it joined.
    reroutes = report.get("post_schema_reroutes") or []
    assert [(r["query_id"], r["to_engine"]) for r in reroutes] == [
        (designer.flagged, "aurora_mysql")
    ]


def test_assess_and_the_local_pipeline_call_one_entry_point(tmp_path: Any) -> None:
    """``assess()`` and ``LocalOrchestrator`` (the CLI's and the API's pipeline) both
    route through ``core.run_post_schema_routing_core``."""
    from src.atx_orchestrator import core
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    spy = MagicMock(wraps=core.run_post_schema_routing_core)
    with patch(CORE_ENTRY, spy):
        _run("psrspy", _FlaggingDesigner(flag=False), tmp_path / "assess")
    assert spy.call_count == 1
    assert spy.call_args.args == ("psrspy", DB)

    store = LocalArtifactStore(base_dir=str(tmp_path / "local"))
    entry = MagicMock()
    with patch(CORE_ENTRY, entry):
        LocalOrchestrator(store, llm_mode="bedrock")._run_post_schema_routing("job-1", DB)
    entry.assert_called_once_with("job-1", DB, store=store, llm_mode="bedrock")
