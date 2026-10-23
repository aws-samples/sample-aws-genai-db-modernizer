"""Each source table has one owning design group (#1243).

The split used to give every chunk of a large cluster the whole cluster's
``primary_tables``: on wordpress, groups ``wp_users_part1`` and ``wp_users_part2``
both listed the same 9 tables, so both designed ``wp_posts`` and ``wp_users`` with
different key schemas and the first ``--merge`` failed. Now every table is primary in
exactly one group; a group whose queries touch a table another group owns gets it in
``context_tables`` (and its input in ``group_context_tables`` with the owner's index),
designed by the owner, never redesigned. A large cluster is chunked keeping each
table's queries in one chunk when they fit, so fewer tables need to be context at all.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this interpreter on a fixed snippet
import sys
from collections import Counter
from pathlib import Path

import pytest

from src.agents.prompt_framing import is_framed
from src.agents.schema_design.group_splitter import (
    MAX_GROUP_SIZE,
    assign_table_owners,
    build_groups,
    split_schema_input,
)
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, run_pipeline
from tests.shared_runs import cached_run

COLLECTOR = {"metadata": {"source_database": {"engine": "postgresql"}}}


def _q(qid: str, tables: list[str], col: str = "a") -> dict:
    sql = f"SELECT {col} FROM {tables[0].split('.')[-1]} WHERE {qid} = $1"
    return {"query_id": qid, "query_text": sql, "tables_accessed": tables}


def _fk_collector(*pairs: tuple[str, str]) -> dict:
    tables: dict[str, list[dict]] = {}
    for child, parent in pairs:
        tables.setdefault(child, []).append({"referenced_table": parent})
        tables.setdefault(parent, [])
    return {
        **COLLECTOR,
        "database_schema": {
            "tables": [
                {"table_id": t, "table_name": t.split(".")[-1], "foreign_keys": fks}
                for t, fks in sorted(tables.items())
            ]
        },
    }


def _owners_unique(groups: list[dict]) -> Counter[str]:
    return Counter(t for g in groups for t in g["primary_tables"])


def test_a_large_clusters_chunks_never_share_a_primary_table() -> None:
    """25 queries on ``a`` and 5 on ``b`` (FK-linked): two chunks, each table once."""
    queries = [_q(f"a{i}", ["db.a"]) for i in range(25)] + [
        _q(f"b{i}", ["db.b", "db.a"]) for i in range(5)
    ]
    groups = build_groups(queries, "db", _fk_collector(("db.b", "db.a")))
    assert len(groups) == 2
    counts = _owners_unique(groups)
    assert counts == Counter({"db.a": 1, "db.b": 1})
    # The chunk that holds a's overflow, or b's join on a, reads a as context.
    owner_a = next(n for n, g in enumerate(groups) if "db.a" in g["primary_tables"])
    other = groups[1 - owner_a]
    assert "db.a" in other["context_tables"]
    assert other["context_owners"]["db.a"] == owner_a


def test_a_table_whose_queries_fit_stays_in_one_chunk() -> None:
    """Tables of 12, 12 and 6 queries: a plain 20-query cut splits the second table;
    keeping tables whole needs no more groups and splits none."""
    pairs = (("db.b", "db.a"), ("db.c", "db.a"))
    queries = (
        [_q(f"a{i}", ["db.a"]) for i in range(12)]
        + [_q(f"b{i}", ["db.b"]) for i in range(12)]
        + [_q(f"c{i}", ["db.c"]) for i in range(6)]
    )
    groups = build_groups(queries, "db", _fk_collector(*pairs))
    assert len(groups) == 2
    for table in ("a", "b", "c"):
        holders = {
            n for n, g in enumerate(groups) for q in g["queries"] if q["query_id"][0] == table
        }
        assert len(holders) == 1, table
    assert all(not g["context_tables"] for g in groups)
    assert _owners_unique(groups) == Counter({"db.a": 1, "db.b": 1, "db.c": 1})


def test_the_owner_serves_most_of_the_tables_queries_and_ties_go_to_the_first() -> None:
    groups = [
        {"group_name": "g0", "primary_tables": ["db.t"], "queries": [_q("x", ["db.t"])]},
        {
            "group_name": "g1",
            "primary_tables": ["db.t"],
            "queries": [_q("y", ["db.t"]), _q("z", ["db.t"])],
        },
        {"group_name": "g2", "primary_tables": ["db.u"], "queries": [_q("w", ["db.u"])]},
        {"group_name": "g3", "primary_tables": ["db.u"], "queries": [_q("v", ["db.u"])]},
    ]
    owned = assign_table_owners(groups, "db")
    assert [g["primary_tables"] for g in owned] == [[], ["db.t"], ["db.u"], []]
    assert owned[0]["context_tables"] == ["db.t"]
    assert owned[0]["context_owners"] == {"db.t": 1}
    assert owned[3]["context_owners"] == {"db.u": 2}
    # The queries are untouched.
    assert [[q["query_id"] for q in g["queries"]] for g in owned] == [
        ["x"],
        ["y", "z"],
        ["w"],
        ["v"],
    ]


def test_a_table_only_joined_from_another_group_is_context_there() -> None:
    """A misc batch query joining a table another cluster owns reads it as context."""
    groups = [
        {"group_name": "g0", "primary_tables": ["db.t"], "queries": [_q("x", ["db.t"])]},
        {"group_name": "g1", "primary_tables": ["db.u"], "queries": [_q("y", ["db.u", "db.t"])]},
    ]
    owned = assign_table_owners(groups, "db")
    assert owned[0]["primary_tables"] == ["db.t"] and owned[0]["context_tables"] == []
    assert owned[1]["primary_tables"] == ["db.u"] and owned[1]["context_tables"] == ["db.t"]


def test_an_empty_split_has_no_owners() -> None:
    assert assign_table_owners([], "db") == []


def test_split_writes_context_tables_to_the_manifest_and_the_group_input(tmp_path: Path) -> None:
    queries = [_q(f"a{i}", ["db.a"]) for i in range(25)] + [
        _q(f"b{i}", ["db.b", "db.a"]) for i in range(5)
    ]
    collector = _fk_collector(("db.b", "db.a"))
    collector["queries"] = {"query_patterns": queries}
    store = LocalArtifactStore(base_dir=str(tmp_path))
    manifest = split_schema_input("j", "db", "dynamodb", collector, {}, queries, store)
    entries = manifest.groups
    assert Counter(t for g in entries for t in g.primary_tables) == Counter({"db.a": 1, "db.b": 1})
    ctx = [g for g in entries if g.context_tables]
    assert ctx, "a's overflow chunk reads a as context"
    for entry in ctx:
        data = json.loads((tmp_path / "db/j/schema-dynamodb/v1" / entry.input_file).read_text())
        owners = {c["table"]: c["owner_group"] for c in data["group_context_tables"]}
        assert sorted(owners) == entry.context_tables
        for table, owner in owners.items():
            assert table in entries[owner].primary_tables
            assert owner != entry.group_index


# ---------------------------------------------------------------------------
# The bundled samples' DynamoDB split
# ---------------------------------------------------------------------------

SAMPLES = ("wordpress", "discourse", "trailgear")


def _latest_assignment(job: Path) -> int:
    return max(int(p.name[1:]) for p in (job / "assignment").glob("v*") if p.name[1:].isdigit())


@pytest.fixture(scope="module", params=SAMPLES)
def sample_split(request, tmp_path_factory) -> tuple[str, dict, dict[int, dict], set[str]]:
    from src.agents.schema_design.handler import run_schema_split

    sample: str = request.param
    job_id = f"job-split-{sample}"
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-{sample}-{job_id}",
        lambda d: run_pipeline(sample, d / "artifacts", job_id),
    )
    # The split writes into the job: give it a private store over a copy.
    import shutil

    root = tmp_path_factory.mktemp(f"split-{sample}") / "artifacts"
    shutil.copytree(cached / "artifacts", root)
    store = LocalArtifactStore(base_dir=str(root))
    version = _latest_assignment(root / sample / job_id)
    run_schema_split(job_id, sample, "dynamodb", store, assignment_version=version)
    base = f"{sample}/{job_id}/schema-dynamodb/v{version}"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    inputs = {
        g["group_index"]: store.read_json(f"{base}/{g['input_file']}") for g in manifest["groups"]
    }
    assignment = store.read_json(f"{sample}/{job_id}/assignment/v{version}/assignment.json")
    assigned = {
        qa["query_id"]
        for qa in assignment.get("query_assignments") or []
        if qa.get("assigned_engine") == "dynamodb" and qa.get("in_scope", True)
    }
    return sample, manifest, inputs, assigned


def test_no_sample_table_is_primary_in_two_groups(sample_split) -> None:
    sample, manifest, _, _ = sample_split
    counts = Counter(t for g in manifest["groups"] for t in g["primary_tables"])
    assert {t: n for t, n in counts.items() if n > 1} == {}, sample
    if sample == "wordpress":  # the two tables #1243 found designed twice
        assert counts["wordpress.wp_posts"] == counts["wordpress.wp_users"] == 1


@pytest.mark.passes_on_main  # the regrouping must keep it
def test_every_sample_query_is_in_exactly_one_group(sample_split) -> None:
    sample, manifest, inputs, assigned = sample_split
    placed = Counter(
        q["query_id"]
        for data in inputs.values()
        for q in data["collector_output"]["queries"]["query_patterns"]
    )
    assert all(n == 1 for n in placed.values()), sample
    excluded = {q["query_id"] for q in manifest.get("excluded_queries") or []}
    assert set(placed) | excluded == assigned, sample
    assert not set(placed) & excluded


@pytest.mark.passes_on_main  # the regrouping must keep it
def test_every_sample_group_holds_at_most_the_cap(sample_split) -> None:
    sample, manifest, _, _ = sample_split
    assert max(g["query_count"] for g in manifest["groups"]) <= MAX_GROUP_SIZE, sample


def test_a_sample_table_read_in_another_group_is_marked_context(sample_split) -> None:
    sample, manifest, inputs, _ = sample_split
    groups = {g["group_index"]: g for g in manifest["groups"]}
    owner = {t: n for n, g in groups.items() for t in g["primary_tables"]}
    for n, data in inputs.items():
        touched = {t["table_id"] for t in data["collector_output"]["database_schema"]["tables"]}
        expected = sorted(t for t in touched if t in owner and owner[t] != n)
        assert groups[n].get("context_tables", []) == expected, (sample, n)
        listed = {c["table"]: c["owner_group"] for c in data.get("group_context_tables") or []}
        assert listed == {t: owner[t] for t in expected}, (sample, n)


# ---------------------------------------------------------------------------
# Determinism and the Bedrock designer's prompt
# ---------------------------------------------------------------------------

_SNIPPET = """
import json
from tests.unit.agents.schema_design.test_split_one_owner import _q, _fk_collector
from src.agents.schema_design.group_splitter import (
    assign_table_owners, build_groups, fit_groups_to_budget, query_pattern_index,
)
qs = [_q(f"{t}{i}", [f"db.{t}", "db.hub"]) for t in "pqrstuvw" for i in range(4)]
qs += [_q(f"hub{i}", ["db.hub"]) for i in range(9)]
c = _fk_collector(*[(f"db.{t}", "db.hub") for t in "pqrstuvw"])
gs = build_groups(qs, "db", c)
# The second owner pass, after the over-budget halving (more than 7 queries is over).
fitted = fit_groups_to_budget(
    gs, lambda g: 10 * len(g["queries"]), 70, pattern_of=query_pattern_index(qs, c)
)
owned = assign_table_owners(fitted, "db")
assert len(owned) > len(gs)
print(json.dumps([[[g["group_name"], g["primary_tables"], g["context_tables"],
                   g["context_owners"], [q["query_id"] for q in g["queries"]]] for g in step]
                  for step in (gs, owned)]))
"""


def test_the_split_is_hash_seed_independent() -> None:
    outs = set()
    for seed in ("0", "1", "12345"):
        proc = subprocess.run(  # nosec B603 -- this interpreter on a fixed snippet
            [sys.executable, "-c", _SNIPPET],
            cwd=REPO,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
        )
        outs.add(proc.stdout)
    assert len(outs) == 1


def test_the_bedrock_designer_is_told_which_tables_are_context_only() -> None:
    from src.tools.schema.dynamodb_schema_agent import context_tables_prompt

    context = [{"table": "db.wp_posts", "owner_group": 0}]
    text = context_tables_prompt(context)
    assert "db.wp_posts" in text and "group 0" in text
    assert is_framed(text, json.dumps(context, indent=2))
    assert context_tables_prompt([]) == ""
    assert context_tables_prompt(None) == ""


class _Stop(Exception):
    pass


def test_the_bedrock_group_design_hands_the_designer_its_context_tables(tmp_path_factory) -> None:
    """On trailgear, the group that reads ``Shipment`` from its owner gets its context
    tables file; a group with none gets no such file."""
    import shutil
    from unittest.mock import patch

    from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
    from src.agents.schema_design.handler import run_schema_design_auto

    sample, job_id = "trailgear", "job-split-trailgear"
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-{sample}-{job_id}",
        lambda d: run_pipeline(sample, d / "artifacts", job_id),
    )
    root = tmp_path_factory.mktemp("bedrock-context") / "artifacts"
    shutil.copytree(cached / "artifacts", root)
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, sample, job_id)
    version = _latest_assignment(root / sample / job_id)
    handed: dict[str, list | None] = {}

    def dispatch(target_type, collector_path=None, analysis_path=None, **kwargs):
        data = json.loads(Path(collector_path).read_text(encoding="utf-8"))
        group = data["queries"]["_group_name"]
        path = kwargs.get("context_tables_path")
        handed[group] = json.loads(Path(path).read_text(encoding="utf-8")) if path else None
        raise _Stop

    with (
        patch("src.agents.schema_design.handler._dispatch_schema_agent", side_effect=dispatch),
        pytest.raises(_Stop),
    ):
        run_schema_design_auto(job_id, sample, "dynamodb", store, version, llm_mode="bedrock")
    manifest = store.read_json(f"{sample}/{job_id}/schema-dynamodb/v{version}/groups_manifest.json")
    expected = {
        g["group_name"]: (
            [
                {"table": t, "owner_group": n}
                for t in g["context_tables"]
                for n, o in enumerate(manifest["groups"])
                if t in o["primary_tables"]
            ]
            or None
        )
        for g in manifest["groups"]
    }
    # Every group is submitted before the first one fails, so each was dispatched.
    assert handed == expected
    assert any(expected.values()) and not all(expected.values())
