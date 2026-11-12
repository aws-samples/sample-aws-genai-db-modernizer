# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2141: the design split keeps every placement unit in one part, on every run.

ADR-027's co-dependency amendment (Decision B, as corrected by #2141): a placement unit
routing places as one (a co-dependency group, a write group or a routine unit, merged,
ADR-031 Amendment 9) has its statements in one design part, unless the groups
manifest's ``unit_cuts`` records the cut. ``split_invariant.check_split_units`` reads the
job's own artifacts: every assignment version, every ``schema-<engine>/v<N>/`` design
in parts, the statements each part's input lists, and routing's own units.

Not marked ``deterministic``, so it runs on the e2e-llm job too, over the split the
headless run made, and only that: a headless job with no design in parts has nothing
to check (and is checked to have no group draft), never a re-split. The deterministic
pipeline designs nothing, so there the split of every engine that can be designed in
parts is run on a copy of the job (``--split`` needs no model).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.agents.schema_design.handler import run_schema_split
from src.agents.schema_design.split_invariant import check_split_units
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

# The engines ``--split`` designs in parts (ADR-027 Decision C: Aurora stays single-pass).
SPLIT_ENGINES = ("dynamodb", "documentdb", "opensearch", "elasticache")

_SPLITS: dict[str, tuple[LocalArtifactStore, Path]] = {}


def _versions(job_dir: Path) -> list[int]:
    return sorted(int(p.parent.name[1:]) for p in job_dir.glob("assignment/v*/assignment.json"))


def _designs_in_parts(
    run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> tuple[LocalArtifactStore, Path]:
    """A headless (LLM) job: its own store, never a re-split. A deterministic job (which
    designs nothing): a copy of the job with every engine of every assignment version
    split, since ``--split`` needs no model."""
    key = f"{run.db}/{run.job_id}"
    if key in _SPLITS:
        return _SPLITS[key]
    store = LocalArtifactStore(str(run.artifact_root))
    if run.external:
        _SPLITS[key] = (store, run.job_dir())
        return _SPLITS[key]
    root = tmp_path_factory.mktemp(f"split-{run.db}") / "artifacts"
    job_dir = root / run.db / run.job_id
    shutil.copytree(run.job_dir(), job_dir)
    copy = LocalArtifactStore(str(root))
    for version in _versions(job_dir):
        for engine in SPLIT_ENGINES:
            run_schema_split(run.job_id, run.db, engine, copy, assignment_version=version)
    _SPLITS[key] = (copy, job_dir)
    return _SPLITS[key]


def test_every_placement_unit_is_in_one_part_or_its_cut_is_recorded(
    run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> None:
    store, job_dir = _designs_in_parts(run, tmp_path_factory)
    check = check_split_units(store, run.db, run.job_id)
    if run.external and not check.manifests:
        # A headless job that designed nothing in parts has nothing to check: say so,
        # and prove it (no group draft anywhere), rather than checking a re-split.
        assert not list(job_dir.glob("schema-*/v*/schema_draft_group_*.json"))
        return
    assert check.manifests, "no design in parts to check"
    # #2153: a table's writes sit only in the part that owns it, unless a cut is recorded.
    assert check.writes_off_owner == 0
    assert not check.problems, "\n".join(check.problems)


@pytest.mark.deterministic
def test_discourse_keeps_every_dynamodb_unit_whole(
    discourse_run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The spike's case: discourse's DynamoDB split cut 3 of its 24 write groups before
    #2141; now each of them is in one part and nothing needs a cut."""
    store, job_dir = _designs_in_parts(discourse_run, tmp_path_factory)
    check = check_split_units(store, discourse_run.db, discourse_run.job_id)
    assert not check.problems
    assert check.units_checked >= 24
    # #2153: topic_users and sidebar_section_links were owned by one part and written in
    # another; now every write checked is in its table's owner.
    assert check.writes_checked > 0
    assert check.writes_off_owner == 0
    for version in _versions(job_dir):
        manifest = json.loads(
            (job_dir / f"schema-dynamodb/v{version}/groups_manifest.json").read_text()
        )
        assert manifest["unit_cuts"] == []


def _move_entry(job_dir: Path, version: int, engine: str) -> str:
    """Corrupt a saved split: move one statement of a unit that sits in one part into
    another part's input, as an unrecorded cut. Returns the statement moved."""
    from src.agents.referee.write_groups import assignment_placement_groups
    from src.storage.collector_for_models import read_collector_for_models

    base = job_dir / f"schema-{engine}/v{version}"
    manifest = json.loads((base / "groups_manifest.json").read_text())
    inputs = {
        g["group_index"]: json.loads((base / g["input_file"]).read_text())
        for g in manifest["groups"]
    }
    entries = {
        idx: [str(q["query_id"]) for q in data["collector_output"]["queries"]["query_patterns"]]
        for idx, data in inputs.items()
    }
    store = LocalArtifactStore(str(job_dir.parents[1]))
    assignment = json.loads((job_dir / f"assignment/v{version}/assignment.json").read_text())
    collector = read_collector_for_models(store, job_dir.parent.name, job_dir.name)
    for unit in assignment_placement_groups(assignment, collector):
        for idx, ids in entries.items():
            shown = [q for q in unit if q in ids]
            if len(shown) < 2 or len(inputs) < 2:
                continue
            moved = shown[-1]
            other = next(i for i in inputs if i != idx)
            src_q = inputs[idx]["collector_output"]["queries"]
            dst_q = inputs[other]["collector_output"]["queries"]
            entry = next(q for q in src_q["query_patterns"] if str(q["query_id"]) == moved)
            src_q["query_patterns"].remove(entry)
            dst_q["query_patterns"].append(entry)
            for group in list(src_q.get("query_pattern_groups") or []):
                if moved in [str(i) for i in group.get("query_ids") or []]:
                    src_q["query_pattern_groups"].remove(group)
                    dst_q.setdefault("query_pattern_groups", []).append(group)
            for i in (idx, other):
                name = next(g["input_file"] for g in manifest["groups"] if g["group_index"] == i)
                (base / name).write_text(json.dumps(inputs[i]))
            return moved
    raise AssertionError(f"no unit with two entries in one part of schema-{engine}/v{version}")


@pytest.mark.deterministic
def test_the_guard_fails_on_a_saved_split_that_cuts_a_unit_without_a_record(
    discourse_run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The mutation: one statement of a whole unit moved to another part's input, with
    no ``unit_cuts`` entry, is reported."""
    _, job_dir = _designs_in_parts(discourse_run, tmp_path_factory)
    root = tmp_path_factory.mktemp("split-mutated") / "artifacts"
    mutated = root / discourse_run.db / discourse_run.job_id
    shutil.copytree(job_dir, mutated)
    version = _versions(mutated)[0]
    _move_entry(mutated, version, "dynamodb")
    store = LocalArtifactStore(str(root))
    check = check_split_units(store, discourse_run.db, discourse_run.job_id)
    assert any(
        f"schema-dynamodb/v{version}" in p and "does not record the cut" in p
        for p in check.problems
    ), check.problems
