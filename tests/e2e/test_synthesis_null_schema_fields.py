# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis survives a schema design whose list fields are explicit nulls (#757, #1094).

Synthesis reads ``schema_output.json`` as a raw dict (``synthesis_data._read_artifact``),
so a design with ``"access_patterns": null`` reaches it unvalidated. ``dict.get(key, [])``
returns ``None`` for a key that is present with a null value, which crashed synthesis in
``build_ranking`` before any model call. On a designed wordpress job (DynamoDB, the cache
layer and Aurora MySQL designs, no model), each list field of each design is nulled in
turn -- top level, then on every item -- and synthesis runs end to end.

The exception (#2199): an Aurora design's ``generated_ddl`` is rebuilt on every read from
its table definitions, their columns, primary keys, indexes and foreign keys. Nulling one
of those leaves a script the definitions no longer rebuild, so the read refuses the design
(``UnsafeArtifactError``, the refusal synthesis reports) instead of loading SQL text it
cannot check. Those variants must be refused, never crash or load.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.referee.synthesis_handler import run_synthesis
from src.storage.assignment_versioning import resolve_downstream_assignment_version
from src.storage.local_store import LocalArtifactStore
from tests.e2e.designed_run import run_designed_wordpress
from tests.e2e.pipeline import PipelineResult, real_designs

pytestmark = pytest.mark.e2e

DB = "wordpress"
JOB = "null-fld"


@pytest.fixture(scope="module")
def designed(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(tmp_path_factory.mktemp("null-fields"), JOB)


# The fields an Aurora design's DDL is rebuilt from (#2199): nulled, the read refuses it.
_AURORA = ("aurora_mysql", "aurora_postgresql")
_DDL_SOURCES = {"table_definitions"} | {
    f"table_definitions[].{k}" for k in ("columns", "primary_key", "indexes", "foreign_keys")
}


def _refused(engine: str, label: str, design: dict) -> bool:
    """Whether nulling ``label`` must make the read refuse ``design``: an Aurora design's
    DDL source that held something (a null where the script lists nothing still rebuilds)."""
    if engine not in _AURORA or label not in _DDL_SOURCES:
        return False
    tables = design.get("table_definitions") or []
    if label == "table_definitions":
        return bool(tables)
    field = label.removeprefix("table_definitions[].")
    return any(isinstance(t, dict) and t.get(field) for t in tables)


def _designs(job_dir: Path) -> dict[str, Path]:
    """engine -> its newest schema_output.json."""
    return {p.parent.parent.name.removeprefix("schema-"): p for p in real_designs(job_dir)}


def _variants(design: dict) -> list[tuple[str, dict]]:
    """(label, design) with one list field nulled: each top-level list, then each
    list-valued key on every item of a top-level list (all items at once)."""
    variants = []
    for key, value in design.items():
        if isinstance(value, list):
            nulled = copy.deepcopy(design)
            nulled[key] = None
            variants.append((key, nulled))
    for key, value in design.items():
        if not isinstance(value, list):
            continue
        item_keys = sorted(
            {
                k
                for item in value
                if isinstance(item, dict)
                for k, v in item.items()
                if isinstance(v, list)
            }
        )
        for item_key in item_keys:
            nulled = copy.deepcopy(design)
            for item in nulled[key]:
                if isinstance(item, dict) and isinstance(item.get(item_key), list):
                    item[item_key] = None
            variants.append((f"{key}[].{item_key}", nulled))
    return variants


def _synthesize(root: Path) -> dict:
    store = LocalArtifactStore(base_dir=str(root))
    version = resolve_downstream_assignment_version(store, DB, JOB)
    run_synthesis(JOB, DB, store, version, llm_mode="none")
    return store.read_json(f"{DB}/{JOB}/synthesis/v{version}/report.json")


def test_the_designs_carry_list_fields_to_null(designed: PipelineResult) -> None:
    designs = _designs(designed.job_dir())
    assert {"dynamodb", "elasticache", "aurora_mysql"} <= set(designs)
    for engine, path in designs.items():
        labels = [label for label, _ in _variants(json.loads(path.read_text()))]
        assert "access_patterns" in labels, engine
        assert any("[]." in label for label in labels), engine


def test_synthesis_survives_each_nulled_list_field(designed: PipelineResult) -> None:
    root = designed.artifact_root
    failures = []
    runs = 0
    for engine, path in _designs(designed.job_dir()).items():
        original = path.read_text()
        try:
            saved = json.loads(original)
            for label, variant in _variants(saved):
                path.write_text(json.dumps(variant))
                runs += 1
                try:
                    report = _synthesize(root)
                except Exception as exc:  # noqa: BLE001 -- collect every crash, then fail once
                    if (
                        _refused(engine, label, saved)
                        and type(exc).__name__ == "UnsafeArtifactError"
                    ):
                        continue
                    failures.append(f"{engine}.{label}: {type(exc).__name__}: {exc}")
                    continue
                if _refused(engine, label, saved):
                    failures.append(f"{engine}.{label}: loaded a script it cannot rebuild")
                    continue
                assert report.get("query_groups") is not None, f"{engine}.{label}"
        finally:
            path.write_text(original)
    assert runs > 20, f"only {runs} variants ran; the designs lost their list fields"
    assert not failures, "synthesis crashed on a null list field:\n" + "\n".join(failures)


def test_synthesis_survives_every_item_list_field_nulled_at_once(designed: PipelineResult) -> None:
    designs = _designs(designed.job_dir())
    originals = {engine: path.read_text() for engine, path in designs.items()}
    try:
        for engine, path in designs.items():
            design = json.loads(originals[engine])
            for key, value in design.items():
                for item in value if isinstance(value, list) else []:
                    if isinstance(item, dict):
                        for k, v in list(item.items()):
                            if isinstance(v, list) and not _refused(
                                engine, f"{key}[].{k}", json.loads(originals[engine])
                            ):
                                item[k] = None
            path.write_text(json.dumps(design))
        report = _synthesize(designed.artifact_root)
        assert report.get("query_groups") is not None
    finally:
        for engine, path in designs.items():
            path.write_text(originals[engine])
