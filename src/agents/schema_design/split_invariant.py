# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The design split's invariant, checked on a job's own artifacts (#2141).

For each assignment version and each engine designed in parts (a
``schema-<engine>/v<N>/groups_manifest.json``), every placement unit routing placed as
one (``write_groups.assignment_placement_groups``: co-dependency groups, write groups
and routine units, merged) has its statements in one part, unless the manifest's
``unit_cuts`` records the cut, with exactly the parts that hold them; and a table's
writes (``write_groups.written_tables``) sit only in the part that owns it
(``primary_tables``), unless the write is in a recorded cut whose parts include the
owner's (#2153: a non-owner part
designs no write of a context table, so a write elsewhere is designed by nobody).

The parts' statements are read from the part inputs (``pattern_input.input_query_ids``:
a query pattern's members included), and the units from routing's own function, not the
splitter's, so a splitter that drifts is caught. The e2e suite runs it on every
deterministic and LLM job (``tests/e2e/test_design_split_units.py``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.storage.artifact_store import ArtifactStore

_MANIFEST = re.compile(r"/schema-([a-z_]+)/v(\d+)/groups_manifest\.json$")
_ASSIGNMENT = re.compile(r"/assignment/v(\d+)/assignment\.json$")


@dataclass
class SplitCheck:
    """What :func:`check_split_units` read and found."""

    manifests: list[str] = field(default_factory=list)
    units_checked: int = 0
    recorded_cuts: int = 0
    writes_checked: int = 0
    writes_off_owner: int = 0
    problems: list[str] = field(default_factory=list)


def manifest_keys(store: ArtifactStore, db: str, job_id: str) -> dict[tuple[str, int], str]:
    """``{(engine, version): manifest key}`` for every design in parts the job has."""
    found: dict[tuple[str, int], str] = {}
    for key in store.list_prefix(f"{db}/{job_id}/"):
        m = _MANIFEST.search(key)
        if m and key.startswith(f"{db}/{job_id}/schema-"):
            found[(m.group(1), int(m.group(2)))] = key
    return dict(sorted(found.items()))


def _part_statements(
    store: ArtifactStore, manifest_key: str, manifest: Mapping
) -> dict[int, set[str]]:
    from src.agents.schema_design.pattern_input import input_query_ids

    base = manifest_key.rsplit("/", 1)[0]
    parts: dict[int, set[str]] = {}
    for group in manifest.get("groups") or []:
        data = store.read_json(f"{base}/{group['input_file']}")
        parts[int(group["group_index"])] = set(
            input_query_ids((data or {}).get("collector_output") or {})
        )
    return parts


def check_split_units(store: ArtifactStore, db: str, job_id: str) -> SplitCheck:
    """Check every design in parts of the job against its assignment version's units."""
    from src.agents.referee.table_resolution import SourceScope, TableNameResolver
    from src.agents.referee.write_groups import assignment_placement_groups, written_tables
    from src.agents.schema_design.placement_units import unit_id_of
    from src.storage.collector_for_models import read_collector_for_models
    from src.tools.sql.query_facts import source_engine_of

    result = SplitCheck()
    collector = read_collector_for_models(store, db, job_id, missing_ok=True)
    statements = {
        str(q.get("query_id")): q
        for q in (collector or {}).get("queries", {}).get("query_patterns") or []
        if isinstance(q, Mapping) and q.get("query_id")
    }
    resolver = TableNameResolver.from_collector(collector) if collector else None
    dialect = source_engine_of(collector)  # the source's SQL dialect, never the target's
    scope = SourceScope.from_collector(collector or {})
    assignments: dict[int, str] = {}
    for key in store.list_prefix(f"{db}/{job_id}/assignment/"):
        m = _ASSIGNMENT.search(key)
        if m:
            assignments[int(m.group(1))] = key
    for (target, version), key in manifest_keys(store, db, job_id).items():
        where = f"schema-{target}/v{version}"
        result.manifests.append(where)
        if version not in assignments:
            result.problems.append(f"{where}: no assignment v{version} to check it against")
            continue
        assignment: Any = store.read_json(assignments[version])
        manifest: Any = store.read_json(key)
        try:
            parts = _part_statements(store, key, manifest)
        except (OSError, ValueError, KeyError) as exc:
            result.problems.append(f"{where}: a part's input cannot be read ({exc})")
            continue
        recorded = {
            str(c.get("unit_id")): sorted(int(p) for p in c.get("parts") or [])
            for c in manifest.get("unit_cuts") or []
            if isinstance(c, Mapping)
        }
        result.recorded_cuts += len(recorded)

        def canon(table: str) -> str:
            return (resolver.resolve(table) if resolver else None) or table

        def writes_of(qid: str) -> set[str]:
            query = statements.get(qid)
            return {canon(t) for t in written_tables(query, dialect, scope)} if query else set()

        # A recorded cut excuses only its own statements' writes, and only when the
        # written table's owner is one of the cut's parts: the cut put the write away
        # from its owner. A write whose owner is outside the cut is not explained by it.
        excused: dict[str, set[int]] = {}
        for c in manifest.get("unit_cuts") or []:
            if not isinstance(c, Mapping):
                continue
            cut_parts = {int(p) for p in c.get("parts") or []}
            for q in c.get("query_ids") or []:
                excused.setdefault(str(q), set()).update(cut_parts)
        owner_of = {
            str(t): int(g["group_index"])
            for g in manifest.get("groups") or []
            for t in g.get("primary_tables") or []
        }
        for idx, ids in sorted(parts.items()):
            for qid in sorted(ids):
                for table in sorted(writes_of(qid)):
                    if table not in owner_of:
                        continue
                    result.writes_checked += 1
                    off = owner_of[table] != idx
                    if off and owner_of[table] not in excused.get(qid, set()):
                        result.writes_off_owner += 1
                        result.problems.append(
                            f"{where}: {qid} writes {table} in part {idx}, "
                            f"owned by part {owner_of[table]}"
                        )
        part_of: dict[str, set[int]] = {}
        for idx, ids in parts.items():
            for q in ids:
                part_of.setdefault(q, set()).add(idx)
        for unit in assignment_placement_groups(assignment, collector):
            held = [q for q in unit if q in part_of]
            if len(held) < 2:
                continue
            result.units_checked += 1
            spanned = sorted({p for q in held for p in part_of[q]})
            if len(spanned) < 2:
                continue
            uid = unit_id_of(unit)
            if uid not in recorded:
                result.problems.append(
                    f"{where}: {uid} ({len(held)} statements) is in parts {spanned} "
                    "and unit_cuts does not record the cut"
                )
            elif recorded[uid] != spanned:
                result.problems.append(
                    f"{where}: {uid} is in parts {spanned}, unit_cuts records {recorded[uid]}"
                )
    return result
