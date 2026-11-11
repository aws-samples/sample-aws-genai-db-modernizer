# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2141: the design split keeps every placement unit in one part, or records the cut.

A placement unit (a co-dependency group, a write group or a routine unit, merged where
they share a statement: ``write_groups.placement_groups``, ADR-031 Amendment 9) is
placed by routing as one. The split keeps its statements in one design part: chunking
packs whole units, halving moves whole units, and a unit too large for one part by
itself is cut at its weakest link, with the cut and its reason in the groups manifest's
``unit_cuts``. Synthetic shapes: a cluster whose table chunking cut a write group, a
unit larger than a part, wide tables that force halving, a routine unit spanning two
tables' clusters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.synthesis_data import load_synthesis_data
from src.agents.schema_design.group_splitter import (
    build_groups,
    fit_groups_to_budget,
    fit_units_to_budget,
    split_schema_input,
)
from src.agents.schema_design.pattern_input import MEMBERS_KEY, expand
from src.agents.schema_design.placement_units import (
    PlacementUnit,
    SplitUnits,
    assignment_placement_units,
    placement_units,
)
from src.agents.schema_design.split_invariant import check_split_units
from src.agents.schema_design.wave_redesign import _with_placement_groups
from src.contracts.schema_design_input import SchemaDesignGroupsManifest
from src.storage.local_store import LocalArtifactStore

DB = "db"
JOB = "j"


def _q(qid: str, table: str, column: str, *, kind: str = "SELECT", also: str = "") -> dict:
    """One statement with its own access pattern (its own filter column)."""
    tables = [f"{DB}.{table}"] + ([f"{DB}.{also}"] if also else [])
    if kind == "UPDATE":
        text = f"UPDATE {table} SET v = ? WHERE {column} = ?"
    elif also:
        text = f"SELECT * FROM {table} JOIN {also} ON {table}.id = {also}.id WHERE {column} = ?"
    else:
        text = f"SELECT * FROM {table} WHERE {column} = ?"
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": kind,
        "frequency_per_hour": 3600.0,
        "calls_per_second": 1.0,
        "tables_accessed": tables,
    }


def _table(name: str, refs: tuple[str, ...] = (), columns: int = 3) -> dict:
    return {
        "table_id": f"{DB}.{name}",
        "table_name": name,
        "schema_name": DB,
        "row_count": 1000,
        "columns": [
            {
                "column_name": f"column_{name}_{n:04d}_with_a_long_descriptive_name",
                "ordinal_position": n + 1,
                "data_type": "varchar(255)",
            }
            for n in range(columns)
        ],
        "foreign_keys": [{"referenced_table": f"{DB}.{r}"} for r in refs],
    }


def _collector(tables: list[dict], queries: list[dict]) -> dict:
    return {
        "database_schema": {"tables": tables},
        "queries": {"query_patterns": queries},
        "metadata": {"database_name": DB, "engine": "postgresql"},
    }


def _unit(ids: list[str], kind: str = "write_group") -> PlacementUnit:
    return PlacementUnit(f"unit:{ids[0]}", kind, tuple(ids))


def _part_of(groups: list[dict]) -> dict[str, int]:
    return {str(m["query_id"]): n for n, g in enumerate(groups) for m in expand(g["queries"])}


# ---------------------------------------------------------------------------
# Chunking packs whole units
# ---------------------------------------------------------------------------


def _three_tables() -> tuple[dict, list[dict], list[str]]:
    """One FK cluster of three tables: t1 with 15 statements, t2 with 10, t3 with 5. Table
    chunking puts t1 in one chunk and t2 with t3 in the next, so a write group of a t1
    statement and a t3 statement (one transaction's writes, say) was cut."""
    queries = [_q(f"a{i}", "t1", f"c{i}") for i in range(15)]
    queries += [_q(f"b{i}", "t2", f"c{i}") for i in range(10)]
    queries += [_q(f"c{i}", "t3", f"c{i}", kind="UPDATE") for i in range(5)]
    tables = [_table("t1", ("t2",)), _table("t2", ("t3",)), _table("t3")]
    return _collector(tables, queries), queries, ["a0", "c0"]


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_without_units_table_chunking_puts_the_pair_in_two_chunks() -> None:
    """The shape reproduces the cut: with no units the pair lands in two chunks."""
    collector, queries, pair = _three_tables()
    groups = build_groups(queries, DB, collector)
    part_of = _part_of(groups)
    assert len(groups) == 2
    assert part_of[pair[0]] != part_of[pair[1]]


def test_a_chunk_packs_a_whole_write_group() -> None:
    collector, queries, pair = _three_tables()
    units = SplitUnits([_unit(pair)], [q["query_id"] for q in queries])
    groups = build_groups(queries, DB, collector, units=units)
    part_of = _part_of(groups)
    assert part_of[pair[0]] == part_of[pair[1]]
    assert len(groups) == 2  # no extra part
    assert max(len(g["queries"]) for g in groups) <= 20
    assert units.reasons == {}


def test_a_write_group_larger_than_a_part_is_cut_at_its_weakest_link() -> None:
    """25 statements in one unit, cap 20: 13 on ta, 11 on tb, and one join of both. The
    weakest link leaves ta's statements and the join on one side and tb's on the other:
    one shared table, the fewest statements touching it."""
    a = [_q(f"a{i}", "ta", f"c{i}", kind="UPDATE") for i in range(13)]
    bridge = [_q("x0", "ta", "j", also="tb")]
    b = [_q(f"b{i}", "tb", f"c{i}", kind="UPDATE") for i in range(11)]
    queries = a + bridge + b
    collector = _collector([_table("ta", ("tb",)), _table("tb")], queries)
    ids = [q["query_id"] for q in queries]
    units = SplitUnits([_unit(ids)], ids)
    groups = build_groups(queries, DB, collector, units=units)
    part_of = _part_of(groups)
    assert len(groups) == 2
    assert {part_of[q["query_id"]] for q in a + bridge} == {part_of["a0"]}
    assert {part_of[q["query_id"]] for q in b} == {part_of["b0"]} != {part_of["a0"]}
    assert units.reasons == {"unit:a0": "over_part_size"}
    cuts = units.unit_cuts([[q for q, n in part_of.items() if n == i] for i in range(2)])
    assert cuts == [
        {
            "unit_id": "unit:a0",
            "kind": "write_group",
            "query_ids": ids,
            "parts": [0, 1],
            "reason": "over_part_size",
        }
    ]


def test_a_unit_that_fits_is_never_cut_by_the_plain_chunk_fallback() -> None:
    """Patterns kept whole can take more chunks than a plain cut, and then the plain cut
    was kept (#1146): it must not cut a unit."""
    queries = [_q(f"p{i}", "t1", f"c{i}") for i in range(21)]
    collector = _collector([_table("t1")], queries)
    unit = ["p19", "p20"]
    units = SplitUnits([_unit(unit)], [q["query_id"] for q in queries])
    groups = build_groups(queries, DB, collector, units=units)
    part_of = _part_of(groups)
    assert part_of["p19"] == part_of["p20"]
    assert units.reasons == {}


def _in_list(qid: str, pattern: int, n: int) -> dict:
    """A statement of query pattern ``pattern``: its IN list has ``n + 1`` values."""
    q = _q(qid, "t1", f"k{pattern}")
    q["query_text"] = f"SELECT * FROM t1 WHERE k{pattern} IN ({', '.join(['?'] * (n + 1))})"
    return q


def _three_patterns() -> tuple[dict, list[dict]]:
    """40 statements of one table in three query patterns of 11, 10 and 19: kept whole
    they take three chunks, so the plain two-chunk cut was kept (#1146 review), cutting
    the second pattern's last two statements apart."""
    queries = [_in_list(f"a{i}", 1, i) for i in range(11)]
    queries += [_in_list(f"b{i}", 2, i) for i in range(10)]
    queries += [_in_list(f"c{i}", 3, i) for i in range(19)]
    return _collector([_table("t1")], queries), queries


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_without_units_the_plain_chunk_cut_splits_the_pair() -> None:
    collector, queries = _three_patterns()
    part_of = _part_of(build_groups(queries, DB, collector))
    assert part_of["b8"] != part_of["b9"]


def test_the_plain_chunk_cut_is_not_kept_when_it_cuts_a_unit() -> None:
    collector, queries = _three_patterns()
    units = SplitUnits([_unit(["b8", "b9"])], [q["query_id"] for q in queries])
    groups = build_groups(queries, DB, collector, units=units)
    part_of = _part_of(groups)
    assert part_of["b8"] == part_of["b9"]
    assert max(len(g["queries"]) for g in groups) <= 20
    assert units.reasons == {}


# ---------------------------------------------------------------------------
# Halving moves whole units
# ---------------------------------------------------------------------------


def _items(n: int) -> list[dict]:
    return [
        {**_q(f"i{k}", "t1", f"c{k}"), MEMBERS_KEY: [_q(f"i{k}", "t1", f"c{k}")]} for k in range(n)
    ]


def _per_item(group: dict) -> int:
    return 1000 * len(group["queries"])


def _group(items: list[dict]) -> dict:
    return {"group_name": "t1", "primary_tables": [f"{DB}.t1"], "queries": items}


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_without_units_halving_cuts_at_the_middle() -> None:
    """The shape reproduces the cut: six items over a three-item budget halve 3 + 3."""
    parts = fit_units_to_budget([_group(_items(6))], _per_item, max_chars=3000)
    assert [[u["query_id"] for u in p["queries"]] for p in parts] == [
        ["i0", "i1", "i2"],
        ["i3", "i4", "i5"],
    ]


def test_halving_moves_whole_units() -> None:
    items = _items(6)
    units = SplitUnits([_unit(["i2", "i3", "i4"])], [u["query_id"] for u in items])
    parts = fit_units_to_budget([_group(items)], _per_item, max_chars=3000, units=units)
    assert [[u["query_id"] for u in p["queries"]] for p in parts] == [
        ["i0", "i1"],
        ["i2", "i3", "i4"],
        ["i5"],
    ]
    assert units.reasons == {}


def test_a_unit_over_the_budget_by_itself_is_cut_and_recorded() -> None:
    items = _items(4)
    ids = [u["query_id"] for u in items]
    units = SplitUnits([_unit(ids, "routine_unit")], ids)
    parts = fit_units_to_budget([_group(items)], _per_item, max_chars=2000, units=units)
    assert [len(p["queries"]) for p in parts] == [2, 2]
    assert units.reasons == {"unit:i0": "over_input_budget"}


def test_a_pattern_in_a_unit_is_cut_between_members_only_when_it_is_all_that_is_left() -> None:
    members = [_q(f"m{k}", "t1", "c") for k in range(4)]
    pattern = {**members[0], MEMBERS_KEY: members}
    other = {**_q("o0", "t1", "d"), MEMBERS_KEY: [_q("o0", "t1", "d")]}
    units = SplitUnits([_unit(["m0", "m1", "m2", "m3"])], ["m0", "m1", "m2", "m3", "o0"])

    def size(group: dict) -> int:
        return 1000 * len(expand(group["queries"]))

    parts = fit_units_to_budget([_group([pattern, other])], size, max_chars=2000, units=units)
    held = [[m["query_id"] for m in expand(p["queries"])] for p in parts]
    assert held[-1] == ["o0"]
    assert units.reasons == {"unit:m0": "over_input_budget"}


def test_halving_brings_a_units_items_together_first() -> None:
    """A kept part can list a unit's items apart (a post-schema reroute keeps its
    order): halving gathers them before it cuts."""
    items = _items(4)
    units = SplitUnits([_unit(["i0", "i2"])], [u["query_id"] for u in items])
    parts = fit_units_to_budget([_group(items)], _per_item, max_chars=2000, units=units)
    part_of = _part_of(parts)
    assert part_of["i0"] == part_of["i2"]
    assert units.reasons == {}


def test_a_pattern_whose_members_sit_in_two_units_joins_them() -> None:
    """A DynamoDB pattern unit is designed in one part, so the two placement units its
    members belong to move as one."""
    first = _q("m0", "t1", "c")
    pattern = {**first, MEMBERS_KEY: [first, _q("m1", "t1", "c")]}
    items = [pattern, *_items(3)]
    units = SplitUnits([_unit(["m0", "i1"]), _unit(["m1", "i2"])], ["m0", "m1", "i0", "i1", "i2"])
    parts = fit_units_to_budget([_group(items)], _per_item, max_chars=3000, units=units)
    part_of = _part_of(parts)
    assert part_of["m0"] == part_of["m1"] == part_of["i1"] == part_of["i2"]
    assert units.reasons == {}


def test_a_pattern_in_two_units_pulls_both_into_its_cluster() -> None:
    """The same at clustering: a t1 pattern whose members share units with two t2
    statements; t1 and t2 are clusters of their own, each large enough for its own part."""
    first = _q("m0", "t1", "c")
    items = [{**first, MEMBERS_KEY: [first, _q("m1", "t1", "c")]}]
    items += [_q(f"f{i}", "t1", f"c{i}") for i in range(5)]
    items += [_q("x", "t2", "x"), _q("y", "t2", "y")] + [
        _q(f"g{i}", "t2", f"c{i}") for i in range(5)
    ]
    collector = _collector([_table("t1"), _table("t2")], expand(items))
    ids = [m["query_id"] for m in expand(items)]
    assert len(build_groups(items, DB, collector)) == 2
    units = SplitUnits([_unit(["m0", "x"]), _unit(["m1", "y"])], ids)
    part_of = _part_of(build_groups(items, DB, collector, units=units))
    assert part_of["m0"] == part_of["x"] == part_of["y"]


def test_a_reroute_keeps_its_parts_and_records_a_unit_they_cut(tmp_path: Path) -> None:
    """A post-schema reroute keeps the parts it came from (#1650): a newcomer joins the
    one kept part holding the rest of its unit, and a unit two kept parts hold is
    recorded as kept_prior_parts, never left unrecorded."""
    from src.agents.schema_design.group_splitter import keep_prior_parts

    queries = [_q(q, "t1", f"c{n}") for n, q in enumerate(["a", "b", "c", "d", "e"])]
    prior = [
        {"group_name": "g0", "query_ids": ["a", "b"], "primary_tables": [f"{DB}.t1"]},
        {"group_name": "g1", "query_ids": ["c"], "primary_tables": []},
    ]
    units = SplitUnits([_unit(["a", "d"]), _unit(["b", "c", "e"])], "abcde")
    groups = keep_prior_parts(queries, prior, DB, _collector([_table("t1")], queries), units=units)
    part_of = _part_of(groups)
    assert part_of["d"] == part_of["a"] == 0
    assert part_of["c"] == 1
    assert units.reasons == {"unit:b": "kept_prior_parts"}


def test_statement_halving_moves_whole_units_too() -> None:
    """The engines designed per statement halve with ``fit_groups_to_budget``."""
    queries = [_q(f"s{k}", "t1", f"c{k}") for k in range(6)]
    pattern_of = {q["query_id"]: n for n, q in enumerate(queries)}
    group = _group(queries)
    plain = fit_groups_to_budget([group], _per_item, max_chars=3000, pattern_of=pattern_of)
    assert [len(p["queries"]) for p in plain] == [3, 3]
    units = SplitUnits([_unit(["s2", "s3", "s4"])], list(pattern_of))
    parts = fit_groups_to_budget(
        [group], _per_item, max_chars=3000, pattern_of=pattern_of, units=units
    )
    part_of = _part_of(parts)
    assert part_of["s2"] == part_of["s3"] == part_of["s4"]
    assert units.reasons == {}


def test_statement_halving_prefers_a_pattern_boundary_outside_the_unit() -> None:
    """Four two-statement patterns, a unit across the second and third: the cut goes to
    the pattern boundary nearest the middle that is outside the unit, not into a
    pattern."""
    queries = [_q(f"s{k}", "t1", f"c{k}") for k in range(8)]
    pattern_of = {q["query_id"]: n // 2 for n, q in enumerate(queries)}
    units = SplitUnits([_unit(["s3", "s4"])], list(pattern_of))
    parts = fit_groups_to_budget(
        [_group(queries)], _per_item, max_chars=6000, pattern_of=pattern_of, units=units
    )
    assert [[q["query_id"] for q in p["queries"]] for p in parts] == [
        ["s0", "s1"],
        ["s2", "s3", "s4", "s5", "s6", "s7"],
    ]


def test_statement_halving_cuts_a_unit_over_the_budget_by_itself() -> None:
    queries = [_q(f"s{k}", "t1", f"c{k}") for k in range(4)]
    ids = [q["query_id"] for q in queries]
    units = SplitUnits([_unit(ids)], ids)
    parts = fit_groups_to_budget([_group(queries)], _per_item, max_chars=2000, units=units)
    assert [len(p["queries"]) for p in parts] == [2, 2]
    assert units.reasons == {"unit:s0": "over_input_budget"}


# ---------------------------------------------------------------------------
# Through --split: wide tables, routine units, the manifest
# ---------------------------------------------------------------------------


WIDE = ("t1", "t2", "t3", "t4")


def _wide_split(tmp_path: Path, units: list[PlacementUnit] | None) -> tuple[dict, dict[str, int]]:
    """Four wide tables in one FK cluster, three patterns each: a part's input holds two
    tables' definitions, so the twelve patterns are halved (wide tables fill a part after
    a few patterns)."""
    queries = [_q(f"{t}p{i}", t, f"c{i}") for t in WIDE for i in range(3)]
    tables = [_table(t, WIDE[n + 1 : n + 2], columns=300) for n, t in enumerate(WIDE)]
    store = LocalArtifactStore(str(tmp_path))
    extra: dict[str, Any] = {"placement_units": units} if units is not None else {}
    split_schema_input(
        JOB, DB, "dynamodb", _collector(tables, queries), {}, queries, store, **extra
    )
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    part_of: dict[str, int] = {}
    for g in manifest["groups"]:
        data = store.read_json(f"{base}/{g['input_file']}")
        for q in data["collector_output"]["queries"]["query_patterns"]:
            part_of[q["query_id"]] = g["group_index"]
    return manifest, part_of


# A write group across t2 and t3: halving at the middle cuts it.
UNIT = ["t2p1", "t2p2", "t3p0", "t3p1"]


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_wide_tables_force_halving_that_cut_the_unit(tmp_path: Path) -> None:
    manifest, part_of = _wide_split(tmp_path, None)
    assert len(manifest["groups"]) == 2
    assert len({part_of[q] for q in UNIT}) == 2


def test_wide_tables_halve_around_the_unit(tmp_path: Path) -> None:
    manifest, part_of = _wide_split(tmp_path, [_unit(UNIT)])
    assert len({part_of[q] for q in UNIT}) == 1
    assert manifest["unit_cuts"] == []


def test_a_routine_unit_spanning_two_tables_clusters_is_designed_in_one_part(
    tmp_path: Path,
) -> None:
    """A procedure reading orders and writing audit: two clusters of their own, each
    large enough for its own part; the routine's two statements share one."""
    queries = [_q(f"o{i}", "orders", f"c{i}") for i in range(6)]
    queries += [_q(f"u{i}", "audit", f"c{i}", kind="UPDATE") for i in range(6)]
    units = placement_units([], {}, {"proc_close_order": ["o0", "u0"]})
    assert units == [PlacementUnit("unit:o0", "routine_unit", ("o0", "u0"))]
    store = LocalArtifactStore(str(tmp_path))
    collector = _collector([_table("orders"), _table("audit")], queries)
    plain = split_schema_input(JOB, DB, "dynamodb", collector, {}, queries, store)
    assert plain.total_groups == 2
    manifest = split_schema_input(
        JOB, DB, "dynamodb", collector, {}, queries, store, placement_units=units
    )
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    holding = [
        g.group_index
        for g in manifest.groups
        if {"o0", "u0"}
        & {
            q["query_id"]
            for q in store.read_json(f"{base}/{g.input_file}")["collector_output"]["queries"][
                "query_patterns"
            ]
        }
    ]
    assert len(holding) == 1
    assert manifest.unit_cuts == []


def test_a_recorded_cut_names_the_parts_in_the_manifest(tmp_path: Path) -> None:
    """One unit of all twelve wide patterns cannot fit one part: it is cut, and the
    manifest names its parts and why."""
    every = [f"{t}p{i}" for t in WIDE for i in range(3)]
    manifest, part_of = _wide_split(tmp_path, [_unit(every, "co_dependency+write_group")])
    (cut,) = manifest["unit_cuts"]
    assert cut["unit_id"] == "unit:t1p0"
    assert cut["kind"] == "co_dependency+write_group"
    assert cut["reason"] == "over_input_budget"
    assert cut["parts"] == sorted({part_of[q] for q in every})
    assert len(cut["parts"]) > 1


def test_a_manifest_written_before_unit_cuts_still_loads() -> None:
    old = {
        "contract_version": "1.7",
        "job_id": JOB,
        "database_name": DB,
        "target_engine": "dynamodb",
        "total_queries": 1,
        "total_groups": 1,
        "groups": [
            {
                "group_index": 0,
                "group_name": "t1",
                "primary_tables": [f"{DB}.t1"],
                "query_count": 1,
                "table_count": 1,
                "input_file": "input_group_0.json",
            }
        ],
    }
    assert SchemaDesignGroupsManifest.model_validate(old).unit_cuts == []


# ---------------------------------------------------------------------------
# The units: ids, kinds, the assignment's sources
# ---------------------------------------------------------------------------


def test_units_carry_every_kind_they_merge() -> None:
    units = placement_units(
        [["q1", "q2"]], {"db.t": ["q2", "q3"], "db.solo": ["q9"]}, {"r1": ["q7", "q8"]}
    )
    assert units == [
        PlacementUnit("unit:q1", "co_dependency+write_group", ("q1", "q2", "q3")),
        PlacementUnit("unit:q7", "routine_unit", ("q7", "q8")),
    ]


def test_the_assignments_units_are_routings_placement_groups() -> None:
    from src.agents.referee.write_groups import assignment_placement_groups

    assignment = {
        "co_dependency_groups": [["q1", "q2"]],
        "write_groups": {"db.t": ["q2", "q3"], "db.u": ["q5", "q6"]},
    }
    units = assignment_placement_units(assignment)
    assert [list(u.query_ids) for u in units] == assignment_placement_groups(assignment)


def test_a_unit_with_one_statement_on_the_engine_is_left_out() -> None:
    units = SplitUnits([_unit(["a", "b"]), _unit(["c", "d"])], ["a", "b", "c"])
    assert [u.unit_id for u in units.units] == ["unit:a"]


# ---------------------------------------------------------------------------
# run_schema_split threads the assignment's units (ADR-027's path)
# ---------------------------------------------------------------------------


def test_run_schema_split_keeps_the_assignments_write_group_whole(tmp_path: Path) -> None:
    from src.agents.schema_design.handler import run_schema_split

    collector, queries, pair = _three_tables()
    collector["metadata"] = {"database_name": DB}
    store = LocalArtifactStore(str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "query_assignments": [
                {"query_id": q["query_id"], "assigned_engine": "documentdb", "in_scope": True}
                for q in queries
            ],
            # A join of a0 with c0, and t3's own write group: one unit of six.
            "co_dependency_groups": [pair],
            "write_groups": {f"{DB}.t3": [f"c{i}" for i in range(5)]},
        },
    )
    store.write_json(f"{DB}/{JOB}/analysis-documentdb/analysis.json", {})
    run_schema_split(JOB, DB, "documentdb", store, assignment_version=1)
    check = check_split_units(store, DB, JOB)
    assert check.manifests == ["schema-documentdb/v1"]
    assert check.units_checked == 1
    assert check.problems == []


# ---------------------------------------------------------------------------
# The guard (split_invariant) and the redesign closure
# ---------------------------------------------------------------------------


def _saved_split(
    tmp_path: Path, parts: list[list[str]], cuts: list[dict] | None = None
) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {"co_dependency_groups": [], "write_groups": {f"{DB}.t": ["q1", "q2"]}},
    )
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    groups = []
    for n, ids in enumerate(parts):
        store.write_json(
            f"{base}/input_group_{n}.json",
            {"collector_output": {"queries": {"query_patterns": [{"query_id": q} for q in ids]}}},
        )
        groups.append(
            {
                "group_index": n,
                "group_name": f"g{n}",
                "primary_tables": [],
                "query_count": len(ids),
                "table_count": 0,
                "input_file": f"input_group_{n}.json",
            }
        )
    manifest = {
        "job_id": JOB,
        "database_name": DB,
        "target_engine": "dynamodb",
        "total_queries": sum(map(len, parts)),
        "total_groups": len(parts),
        "groups": groups,
    }
    if cuts is not None:
        manifest["unit_cuts"] = cuts
    store.write_json(f"{base}/groups_manifest.json", manifest)
    return store


def test_the_guard_passes_a_whole_unit(tmp_path: Path) -> None:
    check = check_split_units(_saved_split(tmp_path, [["q1", "q2"], ["q3"]]), DB, JOB)
    assert (check.units_checked, check.problems) == (1, [])


def test_the_guard_fails_a_unit_cut_without_a_record(tmp_path: Path) -> None:
    """The mutation: a saved manifest whose parts split a unit, with no record."""
    check = check_split_units(_saved_split(tmp_path, [["q1"], ["q2", "q3"]]), DB, JOB)
    assert check.problems == [
        "schema-dynamodb/v1: unit:q1 (2 statements) is in parts [0, 1] and unit_cuts "
        "does not record the cut"
    ]


def test_the_guard_passes_a_recorded_cut_and_fails_one_naming_other_parts(
    tmp_path: Path,
) -> None:
    cut = {
        "unit_id": "unit:q1",
        "kind": "write_group",
        "query_ids": ["q1", "q2"],
        "parts": [0, 1],
        "reason": "over_input_budget",
    }
    store = _saved_split(tmp_path / "a", [["q1"], ["q2"], ["q3"]], [cut])
    assert check_split_units(store, DB, JOB).problems == []
    wrong = _saved_split(tmp_path / "b", [["q1"], ["q3"], ["q2"]], [cut])
    assert check_split_units(wrong, DB, JOB).problems == [
        "schema-dynamodb/v1: unit:q1 is in parts [0, 2], unit_cuts records [0, 1]"
    ]


@pytest.mark.passes_on_main  # #2130's closure, kept: it now grows only on a recorded cut
def test_the_redesign_closure_grows_only_where_the_split_cut_a_unit(tmp_path: Path) -> None:
    whole = _saved_split(tmp_path / "a", [["q1", "q2"], ["q3"]])
    assert _with_placement_groups(whole, DB, JOB, 1, {0: ["q1", "q2"], 1: ["q3"]}, {0}) == [0]
    cut = _saved_split(tmp_path / "b", [["q1"], ["q2", "q3"]])
    assert _with_placement_groups(cut, DB, JOB, 1, {0: ["q1"], 1: ["q2", "q3"]}, {0}) == [0, 1]


# ---------------------------------------------------------------------------
# Synthesis states a recorded cut
# ---------------------------------------------------------------------------


def test_synthesis_reads_the_cuts_of_a_design_in_parts(tmp_path: Path) -> None:
    cut = {
        "unit_id": "unit:q1",
        "kind": "write_group",
        "query_ids": ["q1", "q2"],
        "parts": [0, 1],
        "reason": "over_input_budget",
    }
    store = _saved_split(tmp_path, [["q1"], ["q2"]], [cut])
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": "dynamodb"}]},
    )
    store.write_json(f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json", {"tables": []})
    data = load_synthesis_data(store, JOB, DB, assignment_version=1)
    assert data.engines["dynamodb"].unit_cuts == [cut]


def test_the_risk_assessment_states_a_recorded_cut() -> None:
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
    from src.agents.referee.synthesis_report import build_risk_assessment

    data = SynthesisData(job_id=JOB, database_name=DB)
    data.triage = {"selected_agents": [{"agent_type": "dynamodb"}]}
    cut = {"unit_id": "unit:q1", "query_ids": ["q1", "q2"], "reason": "over_input_budget"}
    data.engines["dynamodb"] = EngineArtifacts(
        engine="dynamodb", analysis={}, schema_design={"tables": []}, unit_cuts=[cut]
    )
    (risk,) = build_risk_assessment(data)["risks"]
    assert risk["description"].startswith("[dynamodb] Designed across parts: 1 group")
    assert risk["query_ids"] == ["q1", "q2"]
    data.engines["dynamodb"].unit_cuts = []
    assert build_risk_assessment(data)["risks"] == []


def test_a_recorded_cut_is_a_low_risk_naming_its_statements() -> None:
    from src.agents.referee.synthesis_report import _unit_cut_risk

    cut = {"unit_id": "unit:q1", "query_ids": ["q2", "q1"], "reason": "over_part_size"}
    risk = _unit_cut_risk(7, "dynamodb", [cut], {"q1": ["db.t"]})
    assert risk["risk_id"] == "RISK-007"
    assert risk["severity"] == "LOW"
    assert risk["query_ids"] == ["q1", "q2"]
    assert risk["affected_tables"] == ["db.t"]
    assert risk["description"] == (
        "[dynamodb] Designed across parts: 1 group of statements that routing places "
        "together was designed in two or more design parts, because it is larger than "
        "one design part holds; the merge reconciled the parts."
    )


# ---------------------------------------------------------------------------
# A co-dependency group over the part size, on a tight page budget (claims audit)
# ---------------------------------------------------------------------------


def _join_group_split(
    tmp_path: Path, n: int, size: int, units: list[PlacementUnit] | None
) -> tuple[SchemaDesignGroupsManifest, dict[str, int], LocalArtifactStore]:
    """``n`` JOIN statements over two FK-linked tables, designed per statement
    (DocumentDB: ``fit_groups_to_budget`` and ``_halve_to_fit`` with the real page
    measure). ``size`` sets how many columns each statement returns, so how tight the
    page budget is."""
    queries = [
        _q(f"j{i}", "orders" if i % 2 else "lines", f"c{i}", also="lines" if i % 2 else "orders")
        for i in range(n)
    ]
    for q in queries:  # long statements: the statement count drives a part's size
        q["query_text"] = q["query_text"].replace(
            "SELECT *", "SELECT " + ", ".join(f"{q['query_id']}_col_{k:04d}" for k in range(size))
        )
    tables = [_table("orders", ("lines",)), _table("lines")]
    store = LocalArtifactStore(str(tmp_path))
    extra: dict[str, Any] = {"placement_units": units} if units is not None else {}
    manifest = split_schema_input(
        JOB, DB, "documentdb", _collector(tables, queries), {}, queries, store, **extra
    )
    base = f"{DB}/{JOB}/schema-documentdb/v1"
    part_of: dict[str, int] = {}
    for g in manifest.groups:
        data = store.read_json(f"{base}/{g.input_file}")
        for q in data["collector_output"]["queries"]["query_patterns"]:
            part_of[q["query_id"]] = g.group_index
    return manifest, part_of, store


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_without_units_a_21_member_join_group_splits_11_and_10(tmp_path: Path) -> None:
    manifest, part_of, _ = _join_group_split(tmp_path, 21, 0, None)
    assert sorted(g.query_count for g in manifest.groups) == [10, 11]


def test_a_co_dependency_group_over_the_part_size_is_cut_once_and_recorded(
    tmp_path: Path,
) -> None:
    ids = [f"j{i}" for i in range(21)]
    unit = placement_units([ids], {})
    manifest, part_of, _ = _join_group_split(tmp_path, 21, 0, unit)
    (cut,) = manifest.unit_cuts
    assert (cut.unit_id, cut.kind, cut.reason) == ("unit:j0", "co_dependency", "over_part_size")
    assert cut.parts == sorted(set(part_of.values())) and len(cut.parts) == 2


# Six statements across the middle of twelve.
STRADDLE = [f"j{i}" for i in range(3, 9)]


@pytest.mark.passes_on_main  # the shape reproduces the cut on main's split
def test_without_units_page_budget_halving_cuts_a_join_group(tmp_path: Path) -> None:
    """Long statements: twelve are over the page budget and six fit, so ``_halve_to_fit``
    halves them 6 + 6, cutting a six-member group across the middle."""
    manifest, part_of, _ = _join_group_split(tmp_path, 12, 400, None)
    assert len(manifest.groups) == 2
    assert len({part_of[q] for q in STRADDLE}) > 1


def test_page_budget_halving_keeps_a_join_group_that_fits_whole(tmp_path: Path) -> None:
    """The same twelve statements, six of them a co-dependency group across the middle:
    halving moves the group whole, since it fits a part, and records nothing."""
    unit = placement_units([STRADDLE], {})
    manifest, part_of, _ = _join_group_split(tmp_path, 12, 400, unit)
    assert len(manifest.groups) >= 2
    assert len({part_of[q] for q in STRADDLE}) == 1
    assert manifest.unit_cuts == []


def test_a_group_over_both_limits_is_cut_and_every_cut_recorded(tmp_path: Path) -> None:
    """A 24-member group on wide tables: over the part size and the page budget. Every
    part holding it is named in its record, and the guard finds nothing unrecorded."""
    ids = [f"j{i}" for i in range(24)]
    manifest, part_of, store = _join_group_split(tmp_path, 24, 400, placement_units([ids], {}))
    (cut,) = manifest.unit_cuts
    assert cut.parts == sorted({part_of[q] for q in ids})
    assert len(cut.parts) > 2
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {"co_dependency_groups": [ids]})
    check = check_split_units(store, DB, JOB)
    assert (check.units_checked, check.problems) == (1, [])


# ---------------------------------------------------------------------------
# A table's owner is the part that holds its writes (#2153)
# ---------------------------------------------------------------------------


def _writes(q: dict) -> list[str]:
    return list(q["tables_accessed"][:1]) if q["query_type"] == "UPDATE" else []


def _owner_shape() -> list[dict]:
    """Three reads of t1 in one part, t1's two writes in the other: by reads alone the
    first part owned t1, and the second, told not to design a context table's writes,
    left both writes designed by nobody (discourse's topic_users and
    sidebar_section_links)."""
    reads = [_q(f"r{i}", "t1", f"c{i}") for i in range(3)]
    writes = [_q(f"w{i}", "t1", f"k{i}", kind="UPDATE") for i in range(2)]
    table = [f"{DB}.t1"]
    return [
        {"group_name": "a", "primary_tables": table, "queries": reads},
        {"group_name": "b", "primary_tables": [], "queries": writes},
    ]


@pytest.mark.passes_on_main  # the shape reproduces the orphaned writes on main
def test_without_writes_the_reads_part_owns_the_table() -> None:
    from src.agents.schema_design.group_splitter import assign_table_owners

    owned = assign_table_owners(_owner_shape(), DB)
    assert owned[0]["primary_tables"] == [f"{DB}.t1"]
    assert owned[1]["context_tables"] == [f"{DB}.t1"]


def test_the_part_holding_a_tables_writes_owns_it() -> None:
    from src.agents.schema_design.group_splitter import assign_table_owners

    owned = assign_table_owners(_owner_shape(), DB, written=_writes)
    assert owned[1]["primary_tables"] == [f"{DB}.t1"]
    assert owned[0]["primary_tables"] == []
    assert owned[0]["context_owners"] == {f"{DB}.t1": 1}


def test_split_schema_input_gives_each_written_table_to_its_writes_part(tmp_path: Path) -> None:
    """Through --split: t1's six reads and its write group (two UPDATEs joined with t2 in
    one co-dependency group) land in different parts; the writes' part owns t1, and the
    guard counts no write off its owner."""
    reads = [_q(f"r{i}", "t1", f"c{i}") for i in range(6)]
    writes = [_q(f"w{i}", "t1", f"k{i}", kind="UPDATE") for i in range(2)]
    others = [_q(f"o{i}", "t2", f"c{i}") for i in range(6)]
    queries = others + reads + writes  # the unit lands in t2's cluster, o0's
    collector = _collector([_table("t1"), _table("t2")], queries)
    unit = placement_units([["o0", "w0", "w1"]], {f"{DB}.t1": ["w0", "w1"]})
    store = LocalArtifactStore(str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {"co_dependency_groups": [["o0", "w0", "w1"]], "write_groups": {f"{DB}.t1": ["w0", "w1"]}},
    )
    manifest = split_schema_input(
        JOB, DB, "dynamodb", collector, {}, queries, store, placement_units=unit
    )
    owner = {t: g.group_index for g in manifest.groups for t in g.primary_tables}
    check = check_split_units(store, DB, JOB)
    assert (check.writes_checked, check.writes_off_owner, check.problems) == (2, 0, [])
    base = f"{DB}/{JOB}/schema-dynamodb/v1"
    for g in manifest.groups:
        ids = {
            q["query_id"]
            for q in store.read_json(f"{base}/{g.input_file}")["collector_output"]["queries"][
                "query_patterns"
            ]
        }
        if "w0" in ids:
            assert owner[f"{DB}.t1"] == g.group_index


def test_the_guard_counts_a_write_off_its_owner(tmp_path: Path) -> None:
    """The mutation: a saved manifest whose owner of t1 is not the part with t1's write."""
    store = _saved_split(tmp_path, [["q3"], ["q1", "q2"]])
    collector = _collector(
        [_table("t")],
        [_q("q1", "t", "a", kind="UPDATE"), _q("q2", "t", "b", kind="UPDATE"), _q("q3", "t", "c")],
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    key = f"{DB}/{JOB}/schema-dynamodb/v1/groups_manifest.json"
    manifest = store.read_json(key)
    manifest["groups"][0]["primary_tables"] = [f"{DB}.t"]
    store.write_json(key, manifest)
    check = check_split_units(store, DB, JOB)
    assert check.writes_off_owner == 2
    assert "schema-dynamodb/v1: q1 writes db.t in part 1, owned by part 0" in check.problems
    manifest["groups"][0]["primary_tables"] = []
    manifest["groups"][1]["primary_tables"] = [f"{DB}.t"]
    store.write_json(key, manifest)
    assert check_split_units(store, DB, JOB).writes_off_owner == 0
