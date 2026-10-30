# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis survives a schema design whose list fields are explicit nulls (#757, #1094).

Synthesis reads ``schema_output.json`` as a raw dict (``synthesis_data._read_artifact``),
so a design with ``"access_patterns": null`` reaches it unvalidated. ``dict.get(key, [])``
returns ``None`` for a key that is present with a null value, which crashed synthesis in
``build_ranking`` before any model call. On a designed wordpress job (DynamoDB, the cache
layer and Aurora MySQL designs, no model), each list field of each design is nulled in
turn -- top level, then on every item -- and synthesis runs end to end.
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
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

DB = "wordpress"
JOB = "null-fld"


@pytest.fixture(scope="module")
def designed(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    return run_designed_wordpress(tmp_path_factory.mktemp("null-fields"), JOB)


def _designs(job_dir: Path) -> dict[str, Path]:
    """engine -> its newest schema_output.json."""
    out = {}
    for engine_dir in sorted(job_dir.glob("schema-*")):
        newest = max(engine_dir.glob("v*/schema_output.json"), key=lambda p: int(p.parent.name[1:]))
        out[engine_dir.name.removeprefix("schema-")] = newest
    return out


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
            for label, variant in _variants(json.loads(original)):
                path.write_text(json.dumps(variant))
                runs += 1
                try:
                    report = _synthesize(root)
                except Exception as exc:  # noqa: BLE001 -- collect every crash, then fail once
                    failures.append(f"{engine}.{label}: {type(exc).__name__}: {exc}")
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
            for value in design.values():
                for item in value if isinstance(value, list) else []:
                    if isinstance(item, dict):
                        for k, v in list(item.items()):
                            if isinstance(v, list):
                                item[k] = None
            path.write_text(json.dumps(design))
        report = _synthesize(designed.artifact_root)
        assert report.get("query_groups") is not None
    finally:
        for engine, path in designs.items():
            path.write_text(originals[engine])
