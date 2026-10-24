"""``run_through="schema_design"`` and ``"synthesis"`` (#1387).

Both phases need a model, so ``deterministic=True`` is refused with them. Tests never
call a model: the schema designer (``run_schema_design_auto``, the entry point
``run_schema_design_core`` calls) writes the designed wordpress job's designs
(``tests/e2e/designed_run.py``), and the synthesis and reality-check model calls are
patched as the reality-check tests patch them.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.assessment import SUPPORTED_RUN_THROUGH, AssessmentOptions, AssessmentResult, assess
from src.assessment.runner import AssessmentRunError
from src.storage.local_store import LocalArtifactStore
from tests.assess_models import fake_designer
from tests.unit.assessment.conftest import collection

DB = "wordpress"
JOB = "assess87"
PHASES = ("analysis", "reality_check", "schema_design", "synthesis")
DESIGNER = "src.agents.schema_design.handler.run_schema_design_auto"
SYNTHESIS = "src.agents.referee.synthesis_handler"
REALITY_CHECK = "src.agents.referee.reality_check_handler"


class _Models:
    """The patched model entry points of one run."""

    def __init__(self) -> None:
        self.designer = MagicMock(side_effect=fake_designer)
        self.summary = MagicMock(return_value=None)
        self.validate = MagicMock(return_value=[])

    def patches(self) -> list[Any]:
        return [
            patch(DESIGNER, self.designer),
            patch(f"{SYNTHESIS}.require_valid_guardrail_config"),
            patch(f"{SYNTHESIS}.generate_executive_summary", self.summary),
            patch(f"{REALITY_CHECK}.require_valid_guardrail_config"),
            patch(f"{REALITY_CHECK}.validate_consolidations", self.validate),
            patch(f"{REALITY_CHECK}._generate_executive_summary", return_value=None),
        ]


def _run(
    run_through: str, store: Any = None, job_id: str = JOB, **options: Any
) -> tuple[AssessmentResult, _Models]:
    models = _Models()
    request = {
        "database_name": DB,
        "collection": collection(DB),
        "options": {
            "run_through": run_through,
            "deterministic": run_through in ("analysis", "reality_check"),
            **options,
        },
    }
    patches = models.patches()
    for p in patches:
        p.start()
    try:
        result = assess(request, store=store, job_id=job_id)
    finally:
        for p in reversed(patches):
            p.stop()
    return result, models


@cache
def _kept(run_through: str) -> tuple[AssessmentResult, _Models, LocalArtifactStore]:
    import tempfile

    store = LocalArtifactStore(base_dir=tempfile.mkdtemp(prefix=f"assess-{run_through}-"))
    result, models = _run(run_through, store=store, include_artifacts=True)
    return result, models, store


def kept(run_through: str) -> tuple[AssessmentResult, _Models, LocalArtifactStore]:
    result, models, store = _kept(run_through)
    return result.model_copy(deep=True), models, store


# --------------------------------------------------------------------------- request


def test_every_phase_is_supported() -> None:
    assert SUPPORTED_RUN_THROUGH == PHASES


@pytest.mark.parametrize("run_through", ["schema_design", "synthesis"])
@pytest.mark.parametrize("deterministic", [True, None])
def test_a_model_phase_with_no_model_is_refused(run_through: str, deterministic: Any) -> None:
    """The default (``deterministic=True``) and an explicit True are both refused, at
    validation, with a message that says the phase needs a model and how to ask for one."""
    options: dict[str, Any] = {"run_through": run_through}
    if deterministic is not None:
        options["deterministic"] = deterministic
    with pytest.raises(ValidationError, match="needs a model") as raised:
        AssessmentOptions.model_validate(options)
    assert "deterministic=False" in str(raised.value)


@pytest.mark.parametrize("run_through", ["schema_design", "synthesis"])
def test_a_model_phase_with_a_model_is_accepted(run_through: str) -> None:
    options = AssessmentOptions.model_validate({"run_through": run_through, "deterministic": False})
    assert options.run_through == run_through


def test_a_refused_model_phase_runs_nothing(tmp_path: Any) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with pytest.raises(ValidationError, match="needs a model"):
        assess(
            {
                "database_name": DB,
                "collection": collection(DB),
                "options": {"run_through": "synthesis"},
            },
            store=store,
        )
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------- schema design


def test_schema_design_designs_every_engine_the_assignment_keeps() -> None:
    from src.storage.assignment_versioning import schema_design_engines

    result, models, store = kept("schema_design")
    version = result.summary.assignment_version
    assert version is not None
    engines = schema_design_engines(store, DB, JOB, version)
    assert engines == ["aurora_mysql", "dynamodb", "elasticache"]
    called = sorted(c.kwargs["target_type"] for c in models.designer.call_args_list)
    assert called == sorted(engines)
    assert {c.kwargs["assignment_version"] for c in models.designer.call_args_list} == {version}
    # Owners and the cache layer: every engine with a statement or a cached read.
    placed = {s.engine for s in result.statements if s.engine}
    cache_engines = {e.engine for e in result.summary.engines if e.role == "cache_layer"}
    assert set(engines) == placed | cache_engines
    models.summary.assert_not_called()


def test_schema_design_result_is_designed_and_carries_the_designs() -> None:
    result, _, store = kept("schema_design")
    version = result.summary.assignment_version
    assert result.label == "designed"
    assert result.summary.run_through_reached == "schema_design"
    assert result.summary.designed_engines == ["aurora_mysql", "dynamodb", "elasticache"]
    assert result.summary.design_note is None
    assert result.artifacts is not None
    blocks = result.artifacts.model_dump()
    assert set(blocks["schema_designs"]) == {"aurora_mysql", "dynamodb", "elasticache"}
    for engine, design in blocks["schema_designs"].items():
        key = f"{DB}/{JOB}/schema-{engine}/v{version}/schema_output.json"
        assert blocks["artifact_keys"]["schema_designs"][engine] == key
        assert design == store.read_json(key)
    assert "synthesis_report" not in blocks
    assert "synthesis_report" not in blocks["artifact_keys"]


def test_full_designs_are_only_in_the_artifacts_block() -> None:
    """The full designs stay out of the stable surface: without ``include_artifacts``
    the result carries the placement, the label and the stable design block
    (``designs``, #1493), never a design's internal contract."""
    result, models = _run("schema_design", job_id="nodesign")
    assert models.designer.called
    assert result.artifacts is None
    assert result.label == "designed"
    assert result.designs is not None
    dumped = result.model_dump_json()
    assert "table_definitions" not in dumped
    assert "key_designs" not in dumped


def test_schema_design_keeps_the_engine_scope() -> None:
    scope = ["aurora_mysql", "dynamodb"]
    result, models = _run("schema_design", job_id="scoped", target_engines=scope)
    called = sorted(c.kwargs["target_type"] for c in models.designer.call_args_list)
    placed = {s.engine for s in result.statements if s.engine}
    cache_engines = {e.engine for e in result.summary.engines if e.role == "cache_layer"}
    assert placed | cache_engines == set(scope)
    assert called == sorted(scope)
    assert result.summary.designed_engines == ["aurora_mysql", "dynamodb"]
    assert result.label == "designed"


def test_a_scope_with_no_engine_to_design_is_a_recommendation() -> None:
    """With only a cache in scope, no statement can be placed, so no engine is designed:
    the result says how far it ran and that nothing was designed, and is no
    ``designed`` result (#1387 review)."""
    from src.assessment.result_builder import NO_DESIGN_NOTE

    for phase in ("schema_design", "synthesis"):
        result, models = _run(
            phase, job_id=f"cacheonly-{phase[:5]}", target_engines=["elasticache"]
        )
        models.designer.assert_not_called()
        assert {s.reason_code for s in result.statements} == {"unservable"}
        assert result.summary.run_through_reached == phase
        assert result.summary.designed_engines == []
        assert result.summary.design_note == NO_DESIGN_NOTE
        assert result.label == "recommendation"


def test_a_failed_design_builds_no_result() -> None:
    """One engine of three fails: the run fails and names only that engine, though the
    other two designed (no result from a partial set of designs)."""
    models = _Models()

    def one_fails(**kw: Any) -> Any:
        if kw["target_type"] == "elasticache":
            raise RuntimeError("model unavailable")
        return fake_designer(**kw)

    models.designer.side_effect = one_fails
    with patch(DESIGNER, models.designer), patch(f"{REALITY_CHECK}.require_valid_guardrail_config"):
        with patch(f"{REALITY_CHECK}.validate_consolidations", return_value=[]):
            with patch(f"{REALITY_CHECK}._generate_executive_summary", return_value=None):
                with pytest.raises(
                    AssessmentRunError, match=r"^schema design failed for elasticache; no result"
                ):
                    assess(
                        {
                            "database_name": DB,
                            "collection": collection(DB),
                            "options": {"run_through": "schema_design", "deterministic": False},
                        }
                    )
    called = {c.kwargs["target_type"] for c in models.designer.call_args_list}
    assert called == {"aurora_mysql", "dynamodb", "elasticache"}


# --------------------------------------------------------------------------- synthesis


def test_synthesis_writes_the_report_once_with_a_model() -> None:
    result, models, store = kept("synthesis")
    version = result.summary.assignment_version
    models.summary.assert_called_once()
    assert result.label == "designed"
    assert result.summary.run_through_reached == "synthesis"
    assert result.artifacts is not None
    blocks = result.artifacts.model_dump()
    key = f"{DB}/{JOB}/synthesis/v{version}/report.json"
    assert blocks["artifact_keys"]["synthesis_report"] == key
    assert blocks["synthesis_report"] == store.read_json(key)
    assert set(blocks["schema_designs"]) == {"aurora_mysql", "dynamodb", "elasticache"}


def test_synthesis_tables_are_the_reports_owner_lines() -> None:
    """At synthesis every table the report states an owner line for has that line in
    ``summary.tables``; the tables it does not map keep the assignment's line."""
    result, _, _ = kept("synthesis")
    assert result.artifacts is not None
    report = result.artifacts.model_dump()["synthesis_report"]
    stated = report["facts"]["table_owner_lines"]["table_lines"]
    assert len(stated) >= 10
    lines = {t.table_id: t for t in result.summary.tables}
    for line in stated:
        mine = lines[line["table"]]
        assert (mine.owner, mine.owner_basis, mine.also_read_by, mine.cached_by) == (
            line["owner"],
            line["owner_basis"],
            line["also_read_by"],
            line["cached_by"],
        ), line["table"]
    assert [t.table_id for t in result.summary.tables] == sorted(lines)


def test_synthesis_engine_reasons_are_the_reports() -> None:
    result, _, _ = kept("synthesis")
    assert result.artifacts is not None
    report = result.artifacts.model_dump()["synthesis_report"]
    why = {e["engine"]: e["rationale"] for e in report["facts"]["engines"]}
    kept_engines = [e for e in result.summary.engines if e.role in ("owner", "cache_layer")]
    assert kept_engines
    for e in kept_engines:
        assert e.reason == why[e.engine], e.engine


# --------------------------------------------------------------------------- ordering

#: A statement's own facts, read from the collection: the same at every phase.
STATEMENT_FACTS = (
    "query_id",
    "query_tags",
    "query_type",
    "join_count",
    "tables",
    "calls_per_second",
    "rewritten_by_caller",
    "signals",
)
#: What a schema design adds to an engine's "why" text: one whole sentence of the
#: executive report's (``synthesis_report``'s rationale joins its sentences with ". ").
DESIGN_SENTENCE_START = "Schema design: "


def _reason_parts(reason: str) -> tuple[list[str], list[str]]:
    """``(the other sentences, the design sentences)`` of an engine's reason, split the
    way the report joins them. A period inside a sentence splits both sides alike."""
    parts = reason.removesuffix(".").split(". ")
    design = [p for p in parts if p.startswith(DESIGN_SENTENCE_START)]
    return [p for p in parts if not p.startswith(DESIGN_SENTENCE_START)], design


def _without_design_sentence(summary: dict[str, Any]) -> dict[str, Any]:
    """The summary with each engine's design sentence taken out of its reason, after
    checking it is at most one whole sentence."""
    for e in summary["engines"]:
        others, design = _reason_parts(e["reason"])
        assert len(design) <= 1, e["reason"]
        e["reason"] = others
    return summary


def test_run_through_is_monotonic() -> None:
    """Each later phase's result keeps the earlier phases' stable fields unchanged.

    From ``analysis``: the statements and each statement's own facts, the groups'
    members, the source engine, the counts and the threshold. From ``reality_check``
    on, the placement is final: every statement field, every group, and the summary
    except ``run_through_reached``. The later phases only add: ``designed_engines``, a
    schema design's sentence in an engine's reason, and an owner line for a table only
    a design maps.
    """
    results = {p: kept(p)[0] for p in PHASES}
    analysis = results["analysis"]
    facts = [{k: getattr(s, k) for k in STATEMENT_FACTS} for s in analysis.statements]
    for phase in PHASES[1:]:
        later = results[phase]
        mine = {s.query_id: {k: getattr(s, k) for k in STATEMENT_FACTS} for s in later.statements}
        assert [mine[f["query_id"]] for f in facts] == facts, phase
        assert [(g.group_id, g.query_ids) for g in later.groups] == [
            (g.group_id, g.query_ids) for g in analysis.groups
        ], phase
        for field in ("source_engine", "statement_count", "unservable_count"):
            assert getattr(later.summary, field) == getattr(analysis.summary, field), phase
        assert later.summary.unique_delta_threshold == analysis.summary.unique_delta_threshold

    placed = results["reality_check"]
    for earlier, phase in (("reality_check", "schema_design"), ("schema_design", "synthesis")):
        before, after = results[earlier], results[phase]
        assert [s.model_dump() for s in after.statements] == [
            s.model_dump() for s in before.statements
        ], phase
        assert [g.model_dump() for g in after.groups] == [g.model_dump() for g in before.groups]
        # The design phase's own fields are compared below.
        own = {"run_through_reached", "designed_engines", "design_note"}
        b = _without_design_sentence(before.summary.model_dump(exclude=own))
        a = _without_design_sentence(after.summary.model_dump(exclude=own))
        b_tables = {t["table_id"]: t for t in b.pop("tables")}
        a_tables = {t["table_id"]: t for t in a.pop("tables")}
        assert a == b, phase
        assert {t: a_tables[t] for t in b_tables} == b_tables, phase
        assert set(a_tables) - set(b_tables) <= _design_only_tables(after), phase
        assert after.options.run_through == phase
    assert placed.summary.engines
    assert placed.summary.designed_engines == []
    designed = results["schema_design"].summary
    assert designed.designed_engines == results["synthesis"].summary.designed_engines
    assert designed.design_note is None and results["synthesis"].summary.design_note is None


def _design_only_tables(result: AssessmentResult) -> set[str]:
    """The tables a schema design maps that no placed statement reads."""
    read = {t for s in result.statements for t in s.tables}
    return {t.table_id for t in result.summary.tables if t.table_id not in read}


def test_the_reality_check_review_runs_once_per_model_run() -> None:
    _, models, _ = kept("synthesis")
    assert models.validate.called


@pytest.fixture
def no_model() -> Iterator[None]:
    """Guard: a deterministic phase reaches none of the model entry points."""
    with patch(DESIGNER) as designer, patch(f"{SYNTHESIS}.generate_executive_summary") as summary:
        yield
        designer.assert_not_called()
        summary.assert_not_called()


@pytest.mark.passes_on_main
@pytest.mark.usefixtures("no_model")
@pytest.mark.parametrize("run_through", ["analysis", "reality_check"])
def test_the_deterministic_phases_call_no_designer(run_through: str) -> None:
    result = assess(
        {"database_name": DB, "collection": collection(DB), "options": {"run_through": run_through}}
    )
    assert result.summary.run_through_reached == run_through


def test_synthesis_reads_the_lines_from_the_report(tmp_path: Any) -> None:
    """The table and engine lines at synthesis are the report's, not recomputed: a
    report that states another owner line, a design-only table and another "why" text
    is what the result says."""
    import shutil

    from src.assessment.models import AssessmentRequest
    from src.assessment.result_builder import build_result, load_run_state

    result, _, store = kept("synthesis")
    shutil.copytree(store.base_dir, tmp_path, dirs_exist_ok=True)
    copy = LocalArtifactStore(base_dir=str(tmp_path))
    key = f"{DB}/{JOB}/synthesis/v{result.summary.assignment_version}/report.json"
    report = copy.read_json(key)
    lines = report["facts"]["table_owner_lines"]["table_lines"]
    first = lines[0]
    first["also_read_by"] = ["opensearch"]
    lines.append(
        {
            "table": "wordpress.zz_design_only",
            "owner": "dynamodb",
            "owner_basis": "design",
            "also_read_by": [],
            "cached_by": None,
        }
    )
    owner = next(e for e in report["facts"]["engines"] if e["role"] == "Retained")
    owner["rationale"] = "The report's own text."
    copy.write_json(key, report)

    state = load_run_state(copy, DB, JOB, "synthesis")
    request = AssessmentRequest(
        database_name=DB,
        collection={"x": 1},
        options=AssessmentOptions(run_through="synthesis", deterministic=False),
    )
    rebuilt = build_result(state, request)
    tables = {t.table_id: t for t in rebuilt.summary.tables}
    assert tables[first["table"]].also_read_by == ["opensearch"]
    added = tables["wordpress.zz_design_only"]
    assert (added.owner, added.owner_basis) == ("dynamodb", "design")
    assert (
        added.reason == "DynamoDB owns it in the schema design; no statement in scope queries it."
    )
    engines = {e.engine: e for e in rebuilt.summary.engines}
    assert engines[owner["engine"]].reason == "The report's own text."


@pytest.mark.parametrize("run_through", ["schema_design", "synthesis"])
def test_a_designed_result_validates_against_the_published_schema(run_through: str) -> None:
    import json

    from jsonschema import Draft202012Validator

    from scripts import render_contract_schemas as rcs

    result, _, _ = kept(run_through)
    schema = json.loads(rcs.schema_path("assess_result").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(json.loads(result.model_dump_json()))
    without = result.model_copy(update={"artifacts": None})
    Draft202012Validator(schema).validate(json.loads(without.model_dump_json()))
