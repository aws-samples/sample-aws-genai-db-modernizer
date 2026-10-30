# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Redesign only what a post-schema move changed (#1650, ADR-031 Amendment 18).

A post-schema reroute writes assignment version N+1 from version N with a few
statements moved (``post_schema_routing_handler``). Every engine the moves touch is
designed again on N+1, but for an engine designed in parts (DynamoDB's per-pattern
parts, a large DocumentDB or OpenSearch workload) only the parts whose input changed
need a designer:

1. :func:`prior_parts` reads version N's parts (their statements, in input order, and
   the tables each owned) when N+1 is a post-schema reroute of N with a split design
   for the engine;
2. the splitter keeps those parts (``group_splitter.split_schema_input`` with
   ``prior_parts``): each keeps the statements it still holds, a statement that
   joined the engine goes to a new part, and a part left empty is dropped, so a move
   changes only the part or parts holding it, never the batch boundaries of the
   rest;
3. :func:`carry_forward_parts` compares what each new part's designer is given with
   version N's parts (:func:`comparable_input`, the part's input without its index,
   name and the engine-wide statement count, plus the other parts' table lists a
   headless designer is told) and copies the draft (byte for byte) and the design trace
   (with its PE review) of every identical one to N+1. Re-running the split keeps them;
   a copied review counts only while the draft is the copied one (:func:`stale_review`).

A copied draft is what the designer and the reviewer produced for exactly that input,
so the Bedrock group loop skips it (a draft that exists is reused) and the Claude Code
``--split`` line marks it ``carried_from``, so no subagent is dispatched for it. The
merge then runs on every part as usual. The parts carried are recorded in
``schema-<engine>/v<N+1>/carried_forward.json``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from src.agents.schema_design.design_trace import write_design_trace
from src.agents.schema_design.pattern_input import input_query_ids
from src.storage.artifact_store import ArtifactStore

#: The record of the parts a version carried forward (an internal artifact).
CARRIED_FORWARD_FILE = "carried_forward.json"

# The fields of a part's input that label it rather than say what to design: its
# index, its name and the engine-wide statement count. Two inputs that differ only
# there ask a designer for the same design.
_LABEL_FIELDS = ("group_index", "group_name")
_LABEL_QUERY_FIELDS = ("_group_index", "_group_name", "_original_count")


def _base_key(database_name: str, job_id: str, engine: str, version: int) -> str:
    return f"{database_name}/{job_id}/schema-{engine}/v{version}"


def _read(store: ArtifactStore, key: str) -> Any:
    try:
        return store.read_json(key) if store.exists(key) else None
    except (OSError, ValueError):
        return None


def reroute_source_version(
    store: ArtifactStore, database_name: str, job_id: str, version: int
) -> int | None:
    """The version a post-schema reroute derived ``version`` from, or None when
    ``version`` is no post-schema reroute (``source == post_schema_routing`` with
    status ``rerouted_after_approval``)."""
    from src.contracts.assignment_models import AssignmentSource, AssignmentStatus
    from src.storage.assignment_versioning import assignment_artifact_path

    if version <= 1:
        return None
    assignment = _read(store, assignment_artifact_path(database_name, job_id, version))
    if not isinstance(assignment, Mapping):
        return None
    if assignment.get("source") != AssignmentSource.POST_SCHEMA_ROUTING.value:
        return None
    if assignment.get("status") != AssignmentStatus.REROUTED_AFTER_APPROVAL.value:
        return None
    previous = assignment.get("previous_version")
    return int(previous) if isinstance(previous, int) and 0 < previous < version else None


def prior_parts(
    store: ArtifactStore, database_name: str, job_id: str, engine: str, version: int
) -> tuple[int, list[dict]] | None:
    """``(N, parts)`` for an engine whose design on ``version`` follows a post-schema
    reroute of version N that split the engine into parts; else None.

    Each part, in N's manifest order: ``group_name``, ``query_ids`` (every statement
    its input lists, in order, a query pattern's members included) and
    ``primary_tables`` (the tables it owned). A part whose input cannot be read makes the whole list unusable, so
    None: the splitter then splits afresh."""
    previous = reroute_source_version(store, database_name, job_id, version)
    if previous is None:
        return None
    base = _base_key(database_name, job_id, engine, previous)
    manifest = _read(store, f"{base}/groups_manifest.json")
    if not isinstance(manifest, Mapping) or not manifest.get("groups"):
        return None
    parts: list[dict] = []
    for group in manifest["groups"]:
        data = _read(store, f"{base}/input_group_{group['group_index']}.json")
        if not isinstance(data, Mapping):
            return None
        parts.append(
            {
                "group_name": group.get("group_name"),
                "query_ids": input_query_ids(data.get("collector_output") or {}),
                "primary_tables": list(group.get("primary_tables") or []),
            }
        )
    return previous, parts


def comparable_input(data: Mapping[str, Any]) -> str:
    """A part's input file without its labels (index, name), the
    engine-wide statement count and the index of each context table's owner (which
    part designs a table, not where it sits in the list). Canonical JSON."""
    found = copy.deepcopy(dict(data))
    for name in _LABEL_FIELDS:
        found.pop(name, None)
    queries = (found.get("collector_output") or {}).get("queries")
    if isinstance(queries, dict):
        for name in _LABEL_QUERY_FIELDS:
            queries.pop(name, None)
    for entry in found.get("group_context_tables") or []:
        if isinstance(entry, dict):
            entry.pop("owner_group", None)
    return json.dumps(found, sort_keys=True, default=str)


def _digest(store: ArtifactStore, key: str) -> str | None:
    import hashlib

    try:
        return hashlib.sha256(store.read_bytes(key)).hexdigest() if store.exists(key) else None
    except (OSError, ValueError):
        return None


def _others_tables(groups: list[dict], idx: int) -> list[list[str]]:
    """The other parts' ``primary_tables``: what a headless part designer is told the
    other parts design (``/modernize`` 6b, ``{OTHER_GROUPS}``), without their indexes."""
    return sorted(
        sorted(str(t) for t in g.get("primary_tables") or [])
        for g in groups
        if int(g["group_index"]) != idx
    )


def _designer_view(store: ArtifactStore, base: str, groups: list[dict], idx: int) -> str | None:
    """Everything a part's designer is given, Bedrock or headless: its input
    (:func:`comparable_input`) and the other parts' tables. None when unreadable."""
    data = _read(store, f"{base}/input_group_{idx}.json")
    if not isinstance(data, Mapping):
        return None
    return comparable_input(data) + json.dumps(_others_tables(groups, idx))


def carry_forward_parts(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
    engine: str,
    previous: int,
    version: int,
) -> dict[int, int]:
    """Copy version ``previous``'s draft and design trace of every part a designer would
    be given unchanged on ``version`` (:func:`_designer_view`); returns the parts that
    keep a carried design, ``{part on version: part on previous}``.

    Only a readable draft is copied (an unreadable one is designed again), never over a
    draft ``version`` already has. Idempotent: run again on the same version (a retry,
    a resumed session, a recycled run), a part whose draft is still the copied one stays
    carried, and the record keeps every part it ever carried, each with the digests of
    the draft and trace it copied, so a part designed afresh since (its draft differs)
    is no longer carried and its copied review no longer counts (:func:`stale_review`).
    """
    old_base = _base_key(database_name, job_id, engine, previous)
    new_base = _base_key(database_name, job_id, engine, version)
    old_groups = list((_read(store, f"{old_base}/groups_manifest.json") or {}).get("groups") or [])
    new_groups = list((_read(store, f"{new_base}/groups_manifest.json") or {}).get("groups") or [])
    old_by_view: dict[str, int] = {}
    for group in old_groups:
        idx = int(group["group_index"])
        view = _designer_view(store, old_base, old_groups, idx)
        draft = _read(store, f"{old_base}/schema_draft_group_{idx}.json")
        if view is not None and isinstance(draft, Mapping):
            old_by_view.setdefault(view, idx)
    record = _read(store, f"{new_base}/{CARRIED_FORWARD_FILE}")
    entries: dict[int, dict] = {
        int(e["part"]): dict(e)
        for e in (record or {}).get("parts") or []
        if isinstance(record, Mapping)
        and record.get("from_version") == previous
        and isinstance(e, Mapping)
        and isinstance(e.get("part"), int)
    }
    for group in new_groups:
        idx = int(group["group_index"])
        view = _designer_view(store, new_base, new_groups, idx)
        old = old_by_view.get(view) if view is not None else None
        if old is None:
            # This split gives the part another input: its carried copy, if it still
            # holds one, is of another part's design. Delete it with its review where
            # the store can, and record it as dropped either way, so the copy is never
            # reused as this part's draft (:func:`is_dropped_copy`).
            entry = entries.get(idx)
            if entry and not entry.get("dropped"):
                draft_key = f"{new_base}/schema_draft_group_{idx}.json"
                trace = f"{new_base}/design_trace_group_{idx}.json"
                if _digest(store, draft_key) == entry.get("draft_sha256"):
                    store.delete(draft_key)
                    if _digest(store, trace) == entry.get("trace_sha256"):
                        store.delete(trace)
                entries[idx] = {**entry, "dropped": True}
            continue
        old_draft = f"{old_base}/schema_draft_group_{old}.json"
        draft_key = f"{new_base}/schema_draft_group_{idx}.json"
        if (
            idx in entries
            and entries[idx].get("from_part") == old
            and not entries[idx].get("dropped")
        ):
            continue  # carried by an earlier run of this split: keep its record
        if store.exists(draft_key) and not is_dropped_copy(store, new_base, idx):
            continue  # a draft of its own, designed for this version
        # Byte for byte: the carried draft is exactly the one reviewed on ``previous``.
        store.write_bytes(draft_key, store.read_bytes(old_draft))
        trace_key = f"{old_base}/design_trace_group_{old}.json"
        new_trace = f"{new_base}/design_trace_group_{idx}.json"
        if store.exists(trace_key):
            write_design_trace(store, new_trace, store.read_json(trace_key))
        entries[idx] = {
            "part": idx,
            "from_part": old,
            "draft_sha256": _digest(store, draft_key),
            "trace_sha256": _digest(store, new_trace),
        }
    store.write_json(
        f"{new_base}/carried_forward.json",  # CARRIED_FORWARD_FILE, spelled out for the scan
        {"from_version": previous, "parts": [entries[k] for k in sorted(entries)]},
    )
    return carried_parts_at(store, new_base)


def _entries_at(store: ArtifactStore, base_key: str) -> list[dict]:
    record = _read(store, f"{base_key}/{CARRIED_FORWARD_FILE}")
    if not isinstance(record, Mapping):
        return []
    return [
        dict(e)
        for e in record.get("parts") or []
        if isinstance(e, Mapping) and isinstance(e.get("part"), int)
    ]


def carried_parts_at(store: ArtifactStore, base_key: str) -> dict[int, int]:
    """``{part: part on the version before}`` for the parts of the design at ``base_key``
    (``<db>/<job>/schema-<engine>/v<N>``) that still hold their carried draft, byte for
    byte. Empty when the version carried nothing."""
    return {
        int(e["part"]): int(e["from_part"])
        for e in _entries_at(store, base_key)
        if e.get("draft_sha256")
        and not e.get("dropped")
        and _digest(store, f"{base_key}/schema_draft_group_{e['part']}.json") == e["draft_sha256"]
    }


def carried_parts(
    store: ArtifactStore, database_name: str, job_id: str, engine: str, version: int
) -> dict[int, int]:
    """:func:`carried_parts_at` for ``engine``'s design on ``version``."""
    return carried_parts_at(store, _base_key(database_name, job_id, engine, version))


def is_dropped_copy(store: ArtifactStore, base_key: str, part: int) -> bool:
    """Whether ``part``'s draft is a copy a re-split dropped (the part's input changed)
    that the store could not delete: it is no design of this part, so a caller treats
    the part as having no draft."""
    draft = _digest(store, f"{base_key}/schema_draft_group_{part}.json")
    return draft is not None and any(
        e.get("part") == part and e.get("dropped") and e.get("draft_sha256") == draft
        for e in _entries_at(store, base_key)
    )


def stale_review(store: ArtifactStore, base_key: str, part: int) -> bool:
    """Whether ``part``'s design trace is still the copy a reroute carried while its
    draft is no longer the carried one: the review it holds is of another design, so it
    does not count as this draft's review."""
    for e in _entries_at(store, base_key):
        if e.get("part") != part:
            continue
        draft = _digest(store, f"{base_key}/schema_draft_group_{part}.json")
        trace = _digest(store, f"{base_key}/design_trace_group_{part}.json")
        if trace != e.get("trace_sha256"):
            return False  # a review recorded since the copy
        return bool(e.get("dropped") or draft != e.get("draft_sha256"))
    return False
