# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved job from an older release still loads through today's read paths (#1159).

``tests/fixtures/old_jobs/<tag>/`` holds the artifacts of a real job that release ``<tag>``
wrote (see its README.md: how it was made, what was left out). Saved jobs outlive the code
that wrote them, and a renamed id or enum value breaks them silently: a contract version
guard covers the version stamp, not the spelling of a stored value (#911, #1073, #818).

This test opens the folder with the code a user's saved job meets:

* the artifact store (every file through ``check_artifact``, every registered contract
  through ``load_contract``) and the assignment version walkers,
* the API routes the UI loads a finished job with, on a real ``LocalArtifactStore``,
* the report renderers, re-rendering the deliverables from the old ``report.json``,
* the context graph, rebuilt from the old artifacts.

The rule it enforces (CONTRIBUTING.md, "Renaming an id or an enum value"): an id or enum
value an older release stored keeps resolving. ``test_every_stored_id_still_resolves`` reads
the fixture's engine ids, access classes, pattern keys, catalog pattern ids, capabilities and
move reason codes, and fails on a value today's vocabulary no longer holds; the fix is a legacy-map entry (a
``status = "legacy"`` engine file, a ``penalty_aliases`` entry, a ``ReasonCode`` member kept,
``LEGACY_CAPABILITY_IDS``), not an edit of the fixture. Adding a release's job is a new
folder next to this one plus an entry in ``OLD_JOBS``.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.agents.referee.access_pattern import UTILITY
from src.api.main import app
from src.api.routes import assessments, assignments, graph, phases, query_journeys, results
from src.api.services.local_execution import LocalExecutionService
from src.api.services.local_s3 import LocalS3Service
from src.contracts.assignment_models import Assignment
from src.contracts.move_ledger import ReasonCode
from src.graph import GraphStoreCache
from src.graph.persistence import GraphPersistence
from src.orchestrator import create_orchestrator
from src.report.deliverables import render_deliverables, synthesis_report_key
from src.shared.capability_labels import LEGACY_CAPABILITY_IDS
from src.shared.catalog.access_classes import access_classes
from src.shared.catalog.engines import engine_ids
from src.shared.catalog.patterns import pattern, patterns, penalty_pattern
from src.shared.contract_versioning import contract_for_payload, load_contract
from src.storage.assignment_versioning import (
    read_assignment,
    read_reality_check_output,
    resolve_effective_assignment_version,
    synthesis_report_candidates,
)
from src.storage.local_store import LocalArtifactStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "old_jobs"
# tag -> (database, job id, assignment versions the job holds, queries in its collection)
OLD_JOBS = {"v0.4.0-beta.2": ("wordpress", "aa4e840a", (1, 2), 107)}

client = TestClient(app)


@pytest.fixture(params=sorted(OLD_JOBS))
def old_job(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[dict[str, Any]]:
    """The old job copied into a fresh artifact root, with the API wired to it."""
    tag = request.param
    root = tmp_path / "artifacts"
    shutil.copytree(FIXTURES / tag, root)
    store = LocalArtifactStore(str(root))
    sfn, s3 = LocalExecutionService(store), LocalS3Service(store)
    saved = {
        (mod, name): getattr(mod, name)
        for mod, name in [
            (assessments, "sfn_service"),
            (assessments, "s3_service"),
            (results, "sfn_service"),
            (results, "s3_service"),
            (query_journeys, "sfn_service"),
            (query_journeys, "artifact_store"),
            (assignments, "artifact_store"),
            (phases, "orchestrator"),
            (phases, "sfn_service"),
            (graph, "artifact_store"),
            (graph, "graph_cache"),
            (graph, "graph_persistence"),
            (graph, "sfn_service"),
        ]
    }
    assessments.sfn_service = results.sfn_service = sfn
    query_journeys.sfn_service = phases.sfn_service = sfn
    assessments.s3_service = results.s3_service = s3
    assignments.artifact_store = query_journeys.artifact_store = store
    phases.orchestrator = create_orchestrator(store=store)
    graph.artifact_store, graph.sfn_service = store, sfn
    graph.graph_cache = GraphStoreCache(max_size=2, base_dir=str(root))
    graph.graph_persistence = GraphPersistence(store)
    db, job, versions, queries = OLD_JOBS[tag]
    yield {
        "tag": tag,
        "root": root,
        "store": store,
        "db": db,
        "job": job,
        "versions": versions,
        "queries": queries,
    }
    for (mod, name), value in saved.items():
        setattr(mod, name, value)


def _json_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.json") if not p.relative_to(root).parts[0] == ".meta")


# -- the artifact store and the version walkers -------------------------------------------


@pytest.mark.passes_on_main  # main already reads the beta.2 job; these keep it so
def test_every_stored_file_reads_and_every_contract_loads(old_job: dict[str, Any]) -> None:
    store, root = old_job["store"], old_job["root"]
    files = _json_files(root)
    assert files, "the fixture folder is empty"
    loaded = 0
    for path in files:
        key = path.relative_to(root).as_posix()
        data = store.read_json(key)  # check_artifact: the version guard and the MAJOR adapters
        model = contract_for_payload(key, data)
        if model is not None:
            load_contract(model, data, artifact=key)
            loaded += 1
    assert loaded >= 10, "too few registered contracts were opened to guard anything"


@pytest.mark.passes_on_main  # main already reads the beta.2 job; these keep it so
def test_assignment_versions_walk_and_the_lineage_resolves(old_job: dict[str, Any]) -> None:
    store, db, job = old_job["store"], old_job["db"], old_job["job"]
    versions = old_job["versions"]
    assert resolve_effective_assignment_version(store, db, job) == max(versions)
    assert synthesis_report_candidates(store, db, job)[0].endswith(
        f"synthesis/v{max(versions)}/report.json"
    )
    for version in versions:
        stored = read_assignment(store, db, job, version)
        assert stored is not None
        assignment = load_contract(Assignment, stored, artifact=f"assignment/v{version}")
        assert assignment.version == version
        assert assignment.query_assignments
    reality_check = read_reality_check_output(store, db, job)
    assert reality_check is not None
    assert reality_check["eliminated_engines"]


# -- the API routes the UI loads a finished job with ---------------------------------------


@pytest.mark.passes_on_main  # main already reads the beta.2 job; these keep it so
def test_the_api_serves_the_old_job(old_job: dict[str, Any]) -> None:
    job, db = old_job["job"], old_job["db"]
    base = f"/api/v1/assessments/{job}"

    detail = client.get(base)
    assert detail.status_code == 200, detail.text
    assert detail.json()["database_name"] == db

    collector = client.get(f"{base}/collector")
    assert collector.status_code == 200, collector.text
    assert len(collector.json()["queries"]["query_patterns"]) == old_job["queries"]

    triage = client.get(f"{base}/triage")
    assert triage.status_code == 200, triage.text
    assert "aurora_mysql" in json.dumps(triage.json())

    analysis = client.get(f"{base}/analysis/dynamodb")
    assert analysis.status_code == 200, analysis.text

    reality_check = client.get(f"{base}/reality-check")
    assert reality_check.status_code == 200, reality_check.text

    final = client.get(f"{base}/results")
    assert final.status_code == 200, final.text
    assert final.json()["synthesis"]["ranking"]

    # The beta.2 report has no per-table mappings (queries are the unit, ADR-031): the route
    # serves the empty list rather than failing on the old shape.
    mappings = client.get(f"{base}/results/table-mappings")
    assert mappings.status_code == 200, mappings.text
    assert mappings.json()["total_count"] == 0

    # Served from the context graph, built on demand from the old artifacts.
    journeys = client.get(f"{base}/query-journeys", params={"page_size": 200})
    assert journeys.status_code == 200, journeys.text
    assert journeys.json()["total"] == old_job["queries"]

    gate = client.get(f"{base}/assignments", params={"database_name": db})
    assert gate.status_code == 200, gate.text
    assert gate.json()["assignment"]["version"] == max(old_job["versions"])

    phase_progress = client.get(f"{base}/phases")
    assert phase_progress.status_code == 200, phase_progress.text


# -- the report renderers ------------------------------------------------------------------


@pytest.mark.passes_on_main  # main already reads the beta.2 job; these keep it so
def test_the_renderers_re_render_the_old_report(old_job: dict[str, Any]) -> None:
    store, db, job = old_job["store"], old_job["db"], old_job["job"]
    version = max(old_job["versions"])
    key = synthesis_report_key(store, db, job, 0)
    assert key == f"{db}/{job}/synthesis/v{version}/report.json"
    rendered = render_deliverables(store, job, db, key, assignment_version=version)
    assert rendered.errors == []
    files = rendered.folder_files()
    assert files, "nothing was rendered"
    assert all(item.content for _, item in files)


# -- the context graph ---------------------------------------------------------------------


@pytest.mark.passes_on_main  # main already reads the beta.2 job; these keep it so
def test_the_graph_rebuilds_from_the_old_artifacts(old_job: dict[str, Any], tmp_path: Path) -> None:
    from src.graph.populators import rebuild_graph
    from src.graph.queries import query_count
    from src.graph.store import GraphStore

    graph = GraphStore(str(tmp_path / "old.lbug"))
    try:
        stats = rebuild_graph(old_job["db"], old_job["job"], old_job["store"], graph)
        assert stats["complete"] is True
        assert query_count(graph) == old_job["queries"]
    finally:
        graph.close()


# -- the rule: a stored id or enum value keeps resolving -----------------------------------
#
# The scan walks every JSON value of the fixture (no hand-picked files) and reads each key by
# its name: a key names the vocabulary its value (or, for a map, its keys) belongs to.
#
#   engine id       a key `engine`, `*_engine`, `*_engines`, or `target(s)`; the keys of the
#                   engine-keyed maps below. Today's vocabulary: the engine catalog, where a
#                   retired id stays as `status = "legacy"`.
#   access class    a key `class`; the keys of `by_class`. Today: the access-class catalog.
#   pattern key     a key `pattern` or `anti_pattern_type`. Today: a pattern file's id or a
#                   `penalty_aliases` entry.
#   pattern id      a key `pattern_id` in an analysis file (the id the engine's catalog
#                   gives it, `documentdb-01`); `report.json`'s own `PAT-n` ids are written
#                   per report and not a vocabulary.
#   capability      a key `capability` or `*capabilities*`, and the lists under the keys of
#                   `query_capabilities`. Today: a legacy id mapped by LEGACY_CAPABILITY_IDS,
#                   else a capability some engine has.
#   reason code     a key `reason_code`. Today: the `ReasonCode` members.
#
# Not scanned: the collector's files (source data), source-side engines (`source_engine`,
# `engines_in_use`: a customer's MySQL is not a target), the display names (`engine_name`),
# and values the scan cannot tell from free text. `engine: "reality-check"` is the
# attribution of a consolidation trade-off, not an engine.

_ENGINE_KEYED_MAPS = {
    "class_scores",
    "group_scores",
    "owners",
    "parity_anchors",
    "engines",
    "engine_dates",
    "moving_by_engine",
    "served_by_engine",
}
_NOT_ENGINES = {"reality-check"}
_SOURCE_SIDE = {"source_engine", "source_database_engine"}


def _engine_key(key: str) -> bool:
    if key in _SOURCE_SIDE:
        return False
    return (
        key == "engine"
        or key.endswith(("_engine", "_engines"))
        or key in {"target", "targets", "target_database"}
    )


def _capability_key(key: str) -> bool:
    return "capabilit" in key


def _stored_ids(node: Any, key: str | None = None) -> Iterator[tuple[str, str]]:
    """``(vocabulary, value)`` for every id the node holds, read by key name."""
    if isinstance(node, dict):
        for k, value in node.items():
            if isinstance(value, dict) and (k in _ENGINE_KEYED_MAPS or _engine_key(k)):
                for sub in value:
                    yield "engine", sub
            elif isinstance(value, dict) and k == "by_class":
                for sub in value:
                    yield "class", sub
            if k == "query_capabilities" and isinstance(value, dict):  # keyed by query id
                for names in value.values():
                    yield from _stored_ids(names, "capability")
                continue
            yield from _stored_ids(value, k)
    elif isinstance(node, list):
        for item in node:
            yield from _stored_ids(item, key)
    elif isinstance(node, str) and key is not None:
        if _engine_key(key):
            yield "engine", node
        elif key in {"class", "pattern_id", "reason_code"}:
            yield key, node
        elif key in {"pattern", "anti_pattern_type"}:
            yield "pattern", node
        elif _capability_key(key):
            yield "capability", node


def _capability_ids() -> set[str]:
    from src.agents.referee.capability_registry import ENGINE_CAPABILITIES

    return {c for caps in ENGINE_CAPABILITIES.values() for c in caps}


def _catalog_pattern_ids() -> set[str]:
    return {
        support.catalog_id
        for entry in patterns()
        for support in entry.engines.values()
        if support.catalog_id
    }


def _resolves(vocabulary: str, value: str) -> bool:
    if vocabulary == "engine":
        return value in engine_ids() or value in _NOT_ENGINES
    if vocabulary == "class":
        # The catalog's classes, and the one the classifier gives a utility statement.
        return value in {c.id for c in access_classes()} | {UTILITY}
    if vocabulary == "reason_code":
        return value in {code.value for code in ReasonCode}
    if vocabulary == "pattern_id":
        return value in _catalog_pattern_ids()
    if vocabulary == "capability":
        return LEGACY_CAPABILITY_IDS.get(value, value) in _capability_ids()
    try:
        pattern(value)
        return True
    except KeyError:
        return penalty_pattern(value) is not None


def _scan(root: Path) -> tuple[dict[str, set[str]], list[str]]:
    seen: dict[str, set[str]] = {}
    unresolved: list[str] = []
    for path in _json_files(root):
        rel = path.relative_to(root).as_posix()
        if "collector" in path.parts:
            continue  # source data, not our vocabulary
        for vocabulary, value in _stored_ids(json.loads(path.read_text(encoding="utf-8"))):
            if vocabulary == "pattern_id" and "/analysis-" not in f"/{rel}":
                continue  # a report's own PAT-n ids
            seen.setdefault(vocabulary, set()).add(value)
            if not _resolves(vocabulary, value):
                unresolved.append(f"{rel}: {vocabulary}={value!r}")
    return seen, unresolved


@pytest.mark.passes_on_main  # main already reads the beta.2 job; this keeps it so
def test_every_stored_id_still_resolves(old_job: dict[str, Any]) -> None:
    seen, unresolved = _scan(old_job["root"])
    assert not unresolved, (
        f"{old_job['tag']} stored ids today's code no longer resolves. A renamed id or enum "
        "value needs a legacy-map entry (CONTRIBUTING.md, 'Renaming an id or an enum value'); "
        "keep the fixture as the release wrote it:\n" + "\n".join(sorted(set(unresolved)))
    )
    # The scan is only a guard while it reads values: every vocabulary must have been seen,
    # and the engine scan must reach ids that only unscanned-by-name fields hold.
    assert seen.keys() >= {
        "engine",
        "class",
        "pattern",
        "pattern_id",
        "capability",
        "reason_code",
    }
    assert {"aurora", "keyspaces", "aurora_postgresql"} <= seen["engine"]  # `targets` / map keys


@pytest.mark.passes_on_main  # checks the scanner itself, which main has no equivalent of
def test_the_id_scan_catches_a_renamed_value() -> None:
    # What the rule rejects: a value no vocabulary holds. Without this the scan could
    # pass by resolving everything.
    assert not _resolves("engine", "aurora_mysql_v2")
    assert not _resolves("class", "key_anchored")
    assert not _resolves("reason_code", "highest-score")
    assert not _resolves("pattern", "inverted_index")
    assert not _resolves("pattern_id", "documentdb-91")
    assert not _resolves("capability", "text_pattern_search")
    assert _resolves("engine", "aurora")  # retired id kept as status "legacy"
    assert _resolves("capability", "inverted_index")  # mapped by LEGACY_CAPABILITY_IDS
    # ... and the walk reads the keys of an engine-keyed map, of by_class, and query capabilities.
    found = set(_stored_ids({"class_scores": {"dynamodb": 1}, "by_class": {"key_lookup": 3}}))
    assert found == {("engine", "dynamodb"), ("class", "key_lookup")}
    assert set(_stored_ids({"query_capabilities": {"q1": ["aggregation"]}})) == {
        ("capability", "aggregation")
    }
