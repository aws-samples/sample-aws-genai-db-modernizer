"""ADR-017 Decision 3: the decision trace is a separate artifact next to ``analysis.json`` (#703).

Every analysis agent writes ``decision-trace.json`` beside ``analysis.json``, and the trace is
not part of the agent-to-agent contract: ``analysis.json`` carries no trace field and the
referee never reads the trace file. Completeness of the DynamoDB trace has its own property test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.agents.analysis.handler import run_analysis
from src.atx_orchestrator.core import _ANALYSIS_ENGINES, run_analysis_core
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.analysis_output import AnalysisOutputContract
from src.storage.artifact_store import ArtifactStore
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.fixtures.opensearch_pattern_fixtures import get_fulltext_search_fixture

DB, JOB = "mydb", "job-1"
HANDLER_ENGINES = [
    "dynamodb",
    "documentdb",
    "opensearch",
    "elasticache",
    "aurora_postgresql",
    "aurora_mysql",
]
REFEREE_DIR = Path(__file__).resolve().parents[4] / "src" / "agents" / "referee"
# Modules outside the referee directory that the referee imports to read analysis output:
# held to the same rule (#728: the access-pattern shadow's weights).
REFEREE_READERS = (
    Path(__file__).resolve().parents[4] / "src" / "tools" / "analysis" / "score_weights.py",
)


def _store(engine: str, tmp_path) -> ArtifactStore:
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    collector = (
        get_fulltext_search_fixture()
        if engine == "opensearch"
        else get_ecommerce_collector_output()
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    return store


def _assert_separate_artifacts(store: ArtifactStore, folder: str) -> None:
    analysis = store.read_json(f"{DB}/{JOB}/{folder}/analysis.json")
    trace = store.read_json(f"{DB}/{JOB}/{folder}/decision-trace.json")
    assert trace, f"{folder}: the decision trace is empty"
    # The contract is the whole of analysis.json: no key outside it, none of them a trace.
    assert set(analysis) <= set(AnalysisOutputContract.model_fields), folder
    assert not [k for k in analysis if "trace" in k], folder
    AnalysisOutputContract.model_validate(analysis)
    # And the trace content did not leak into the contract under another name.
    assert not set(trace) & set(analysis) - {"agent"}, folder


@pytest.mark.parametrize("engine", HANDLER_ENGINES)
def test_the_analysis_handler_writes_the_trace_beside_analysis_json(engine, tmp_path) -> None:
    store = _store(engine, tmp_path)
    run_analysis(JOB, DB, engine, store)
    _assert_separate_artifacts(store, f"analysis-{engine}")


@pytest.mark.parametrize("engine", sorted(_ANALYSIS_ENGINES))
def test_the_transform_core_writes_the_trace_beside_analysis_json(engine, tmp_path) -> None:
    spec = _ANALYSIS_ENGINES[engine]
    store = _store(spec.target_database, tmp_path)
    run_analysis_core(engine, JOB, DB, store)
    _assert_separate_artifacts(store, f"analysis-{spec.target_database}")


def test_the_contract_has_no_trace_field() -> None:
    assert not [name for name in AnalysisOutputContract.model_fields if "trace" in name]


def _string_pieces(path: Path) -> list[str]:
    """Every string constant in a module, including the literal parts of f-strings.

    Docstrings are skipped: they may name the artifact without reading it.
    """
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _could_name_the_trace_file(piece: str) -> bool:
    low = piece.lower().replace("_", "-")
    if "decision-trace" in low:
        return True
    # A name assembled from parts: its tail ("-trace.json", "trace.json") gives it away.
    return low.endswith("trace.json") and not low.endswith("design-trace.json")


def test_the_referee_reads_analysis_json_not_the_trace_file() -> None:
    """No referee module holds a string that names (or could be joined into) the trace file.

    ``design_trace.json`` is the schema-design trace, a different artifact. A name built from
    parts ("decision" + "-trace.json") is caught through its tail piece.
    """
    readers = {
        p.name: [s for s in _string_pieces(p) if _could_name_the_trace_file(s)]
        for p in (*REFEREE_DIR.rglob("*.py"), *REFEREE_READERS)
    }
    readers = {name: pieces for name, pieces in readers.items() if pieces}
    assert not readers, f"referee modules name the trace artifact: {readers}"


def test_the_referee_readers_outside_its_directory_exist() -> None:
    assert all(p.is_file() for p in REFEREE_READERS)


def test_the_trace_detector_catches_a_name_built_from_parts() -> None:
    assert _could_name_the_trace_file("decision-trace.json")
    assert _could_name_the_trace_file("decision_trace.json")
    assert _could_name_the_trace_file("-trace.json")
    assert not _could_name_the_trace_file("decision")
    assert not _could_name_the_trace_file("design_trace.json")
    assert not _could_name_the_trace_file("analysis.json")
