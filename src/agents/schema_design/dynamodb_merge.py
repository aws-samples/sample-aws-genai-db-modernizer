"""DynamoDB group-draft reconciliation (issues #223, #229).

DynamoDB schema design always runs split -> per-group drafts -> merge. Groups
are designed in parallel, so two groups whose queries touch the same source
table each design a home for it, and each numbers its access patterns from
``DDB-AP-1``. :func:`group_merger.merge_group_drafts` uses this module to turn
the drafts into one coherent design.

Merge rule
==========

1. **Pattern IDs.** When ``access_patterns[].pattern_id`` collide across drafts,
   every draft is renumbered *before* the drafts are combined:
   ``DDB-AP-1..N`` in draft order. Each draft's old -> new map is applied to
   every ``DDB-AP-<n>`` mention in that draft's text (justifications,
   trade-offs, migration notes, hot-partition entries) and in its design trace,
   so references keep pointing at the same pattern.
2. **Compatible tables merge.** Tables that share a source table (or a table
   name) are merged into the first one when they have the same partition/sort
   key names and types, the same shape (both item collections, or both
   single-entity tables) and nothing conflicts: item collections must share at
   least one identical entity (type, source table, PK/SK templates) and may not
   reuse an entity type, a static SK prefix or a GSI name for different data;
   single-entity tables must store the same base entity (the source table of
   the partition-key attribute). Entities, attributes, GSIs and source tables
   are unioned. ``item_count`` adds the absorbed table's share of items for
   entity types the survivor did not have yet (its count x new types / its
   types); ``item_size_bytes`` is the item-weighted average (the larger size
   when no items are added). Renames are applied *per draft*: only the access
   patterns, ``hot_partition_analysis`` entries and trade-off ``target_tables``
   of the draft whose table was absorbed are re-pointed, so another group's
   unrelated table with the same name keeps its own references. The merged
   table's ``hot_partition_analysis`` load is re-aggregated per
   (table, GSI, operation), and a trade-off records the merge.
3. **True conflicts fail validation** (``validation_failures`` entries with
   prefix :data:`MERGE_FAILURE_PREFIX`, ``validation_passed=false``): one table
   name used for two different designs, and the same entity type for the same
   source table with contradictory PK/SK templates in tables from different
   groups.
4. **Independent homes are warnings.** When a source table is the *primary
   entity* (its own entity in an item collection, or the base entity of a
   single-entity table) of two or more tables designed by different groups,
   and no trade-off names the source table and every one of those tables, the
   merge appends a review trade-off (description prefix
   :data:`OVERLAP_PREFIX`) naming the tables and keys. :func:`merge_warnings`
   reports it; it never fails validation.
5. **Not flagged at all:** denormalized copies (a source table listed only in
   a table's ``source_tables`` or in attribute-level ``source_table``, without
   its own entity) and any overlap whose tables all come from one group (that
   group designed it on purpose).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.agents.schema_design.scope import normalize_table_name

MERGE_FAILURE_PREFIX = "DynamoDB merge: "
"""Every DynamoDB merge failure starts with this, so a re-merge drops stale ones."""

OVERLAP_PREFIX = "DynamoDB merge review: "
"""Description prefix of the review trade-off the merge adds for independent homes."""

_PATTERN_ID_RE = re.compile(r"\bDDB-AP-\d+\b")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _key_attr(key: Any) -> tuple[str, str] | None:
    if not isinstance(key, dict) or not key.get("attribute_name"):
        return None
    return str(key["attribute_name"]), str(key.get("attribute_type") or "")


def _key_schema(table: dict) -> tuple[tuple[str, str] | None, tuple[str, str] | None]:
    return _key_attr(table.get("partition_key")), _key_attr(table.get("sort_key"))


def _describe_keys(table: dict) -> str:
    pk, sk = _key_schema(table)
    text = f"PK {pk[0]}" if pk else "no PK"
    return f"{text}, SK {sk[0]}" if sk else text


def _known(name: str) -> bool:
    return bool(name) and name != "unknown"


def _designed_sources(table: dict) -> list[str]:
    """Normalised source tables a table names (table + entity level)."""
    names = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
    names += [
        e["source_table"]
        for e in _dicts(table.get("entities"))
        if isinstance(e.get("source_table"), str)
    ]
    return list(dict.fromkeys(k for k in map(normalize_table_name, names) if _known(k)))


def _shape(table: dict) -> str:
    if _dicts(table.get("entities")):
        return "item_collection"
    if _dicts(table.get("attributes")):
        return "single"
    return "empty"


def _base_entity(table: dict) -> str:
    """Source table of the partition-key attribute (else the first source table)."""
    pk = _key_attr(table.get("partition_key"))
    for attr in _dicts(table.get("attributes")):
        if pk and attr.get("name") == pk[0] and isinstance(attr.get("source_table"), str):
            return normalize_table_name(attr["source_table"])
    sources = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
    return normalize_table_name(sources[0]) if sources else ""


def _primary_sources(table: dict) -> list[str]:
    """Source tables that have their own entity in ``table`` (not denormalized copies)."""
    if _shape(table) == "item_collection":
        names = [
            normalize_table_name(str(e.get("source_table") or ""))
            for e in _dicts(table.get("entities"))
        ]
    else:
        names = [_base_entity(table)]
    return list(dict.fromkeys(n for n in names if _known(n)))


def _entity_signature(entity: dict) -> tuple[str, str, str, str]:
    return (
        str(entity.get("entity_type") or ""),
        normalize_table_name(str(entity.get("source_table") or "")),
        str(entity.get("pk_template") or ""),
        str(entity.get("sk_template") or ""),
    )


def _sk_prefix(entity: dict) -> str:
    return str(entity.get("sk_template") or "").split("{", 1)[0]


def _gsi_keys(gsi: dict) -> tuple:
    def keys(value: Any) -> tuple:
        return tuple(_key_attr(k) for k in value) if isinstance(value, list) else ()

    return keys(gsi.get("partition_key")), keys(gsi.get("sort_key"))


def _groups_label(groups: list[int] | set[int]) -> str:
    return "group " + "/".join(str(g) for g in sorted(groups))


def _union_by(items: list[dict], extra: list[dict], key: str) -> list[dict]:
    seen = {i.get(key) for i in items}
    return items + [i for i in extra if i.get(key) not in seen]


# ---------------------------------------------------------------------------
# 1. Pattern IDs (#229)
# ---------------------------------------------------------------------------


def pattern_id_maps(drafts: list[dict]) -> list[dict[str, str]]:
    """Per-draft ``old -> new`` pattern-ID maps (all empty when IDs are unique).

    Numbering is ``DDB-AP-1..N`` across drafts in draft order. A pattern ID a
    draft repeats maps to its first new number (``renumber_draft`` still gives
    each pattern its own number).
    """
    ids = [ap.get("pattern_id") for d in drafts for ap in _dicts(d.get("access_patterns"))]
    if len(drafts) < 2 or len(ids) == len(set(ids)):
        return [{} for _ in drafts]
    maps: list[dict[str, str]] = []
    number = 0
    for draft in drafts:
        mapping: dict[str, str] = {}
        for ap in _dicts(draft.get("access_patterns")):
            number += 1
            pid = ap.get("pattern_id")
            if isinstance(pid, str):
                mapping.setdefault(pid, f"DDB-AP-{number}")
        maps.append(mapping)
    return maps


def rewrite_pattern_ids(value: Any, mapping: dict[str, str]) -> Any:
    """Return ``value`` with every ``DDB-AP-<n>`` mention mapped (one pass, no chaining)."""
    if not mapping:
        return value
    if isinstance(value, str):
        return _PATTERN_ID_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), value)
    if isinstance(value, list):
        return [rewrite_pattern_ids(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: rewrite_pattern_ids(v, mapping) for k, v in value.items()}
    return value


def renumber_draft(draft: dict, mapping: dict[str, str]) -> dict:
    """Apply ``mapping`` to the draft's text and give each pattern its new ID."""
    if not mapping:
        return draft
    rewritten: dict = rewrite_pattern_ids(draft, mapping)
    first = min(int(v.rsplit("-", 1)[1]) for v in mapping.values())
    patterns: list = []
    number = first
    for ap in rewritten.get("access_patterns") or []:
        if isinstance(ap, dict):
            ap = {**ap, "pattern_id": f"DDB-AP-{number}"}
            number += 1
        patterns.append(ap)
    rewritten["access_patterns"] = patterns
    return rewritten


# ---------------------------------------------------------------------------
# 2. Merging compatible tables (#223)
# ---------------------------------------------------------------------------


def _merge_conflict(a: dict, b: dict) -> str | None:
    """Why ``b`` cannot be merged into ``a`` (``None`` when it can)."""
    if _key_schema(a) != _key_schema(b):
        return f"key schemas differ ({_describe_keys(a)} vs {_describe_keys(b)})"
    shape = _shape(a)
    if shape != _shape(b):
        return "one is an item collection, the other a single-entity table"
    if shape == "item_collection":
        a_entities = {e.get("entity_type"): e for e in _dicts(a.get("entities"))}
        b_entities = _dicts(b.get("entities"))
        a_sigs = {_entity_signature(e) for e in a_entities.values()}
        if not any(_entity_signature(e) in a_sigs for e in b_entities):
            return "the item collections share no identical entity"
        a_prefixes = {_sk_prefix(e): e for e in a_entities.values()}
        for entity in b_entities:
            same_type = a_entities.get(entity.get("entity_type"))
            if same_type is not None and _entity_signature(same_type) != _entity_signature(entity):
                return f"entity {entity.get('entity_type')} is defined differently"
            same_prefix = a_prefixes.get(_sk_prefix(entity))
            if same_prefix is not None and (
                _entity_signature(same_prefix)[:2] != _entity_signature(entity)[:2]
            ):
                return f"sort key prefix '{_sk_prefix(entity)}' is used by different entities"
    elif shape == "single" and _base_entity(a) != _base_entity(b):
        return "they store different base entities"
    a_gsis = {g.get("gsi_name"): g for g in _dicts(a.get("gsis"))}
    for gsi in _dicts(b.get("gsis")):
        same = a_gsis.get(gsi.get("gsi_name"))
        if same is not None and _gsi_keys(same) != _gsi_keys(gsi):
            return f"GSI {gsi.get('gsi_name')} is defined differently"
    return None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _absorb(a: dict, b: dict) -> dict:
    """Return ``a`` with ``b``'s entities, attributes, GSIs, sources and new items added."""
    merged = dict(a)
    sources = [s for s in a.get("source_tables") or [] if isinstance(s, str)]
    known = {normalize_table_name(s) for s in sources}
    for s in b.get("source_tables") or []:
        if isinstance(s, str) and normalize_table_name(s) not in known:
            known.add(normalize_table_name(s))
            sources.append(s)
    merged["source_tables"] = sources

    new_share = 0.0  # fraction of b's items that are entity types a does not have
    if _shape(a) == "item_collection":
        by_type = {e.get("entity_type"): dict(e) for e in _dicts(a.get("entities"))}
        b_entities = _dicts(b.get("entities"))
        new_types = [e for e in b_entities if e.get("entity_type") not in by_type]
        new_share = len(new_types) / len(b_entities) if b_entities else 0.0
        for entity in b_entities:
            same = by_type.get(entity.get("entity_type"))
            if same is None:
                by_type[entity.get("entity_type")] = dict(entity)
            else:
                same["attributes"] = _union_by(
                    _dicts(same.get("attributes")), _dicts(entity.get("attributes")), "name"
                )
        merged["entities"] = list(by_type.values())
    elif _shape(a) == "single":
        merged["attributes"] = _union_by(
            _dicts(a.get("attributes")), _dicts(b.get("attributes")), "name"
        )
    merged["gsis"] = _union_by(_dicts(a.get("gsis")), _dicts(b.get("gsis")), "gsi_name")

    a_count, b_count = _int(a.get("item_count")), _int(b.get("item_count"))
    a_size, b_size = _int(a.get("item_size_bytes")), _int(b.get("item_size_bytes"))
    added = round((b_count or 0) * new_share)
    if a_count is not None or b_count is not None:
        merged["item_count"] = (a_count or 0) + added
    if a_size is not None and b_size is not None:
        if added and a_count is not None:
            total = a_count + added
            merged["item_size_bytes"] = max(1, round((a_size * a_count + b_size * added) / total))
        else:
            merged["item_size_bytes"] = max(a_size, b_size)
    elif b_size is not None:
        merged["item_size_bytes"] = b_size
    return merged


@dataclass
class _Home:
    """One target table in the merged design and which groups shaped it."""

    table: dict
    groups: list[int]
    # normalised source table -> groups that gave it its own entity in this table
    primary: dict[str, set[int]] = field(default_factory=dict)
    absorbed: bool = False

    def add_primary(self, table: dict, group: int) -> None:
        for source in _primary_sources(table):
            self.primary.setdefault(source, set()).add(group)


@dataclass
class TableReconciliation:
    homes: list[_Home]
    renames: list[dict[str, str]]  # per draft: absorbed table name -> survivor name
    failures: list[str]
    trade_offs: list[dict]


def reconcile_tables(drafts: list[dict], group_indices: list[int]) -> TableReconciliation:
    """Merge compatible tables across ``drafts`` (rule 2) and flag name conflicts."""
    homes: list[_Home] = []
    renames: list[dict[str, str]] = [{} for _ in drafts]
    failures: list[str] = []
    trade_offs: list[dict] = []

    for pos, (draft, group) in enumerate(zip(drafts, group_indices, strict=True)):
        ap_queries: dict[str, list[str]] = {}
        for ap in _dicts(draft.get("access_patterns")):
            ap_queries.setdefault(str(ap.get("table_name")), []).extend(
                q for q in ap.get("query_ids") or [] if isinstance(q, str)
            )
        for table in _dicts(draft.get("table_definitions")):
            name = table.get("table_name")
            sources = set(_designed_sources(table))
            target: int | None = None
            dropped = False
            for i, home in enumerate(homes):
                other = home.table
                same_name = other.get("table_name") == name
                if not same_name and not sources & set(_designed_sources(other)):
                    continue
                conflict = _merge_conflict(other, table)
                if conflict is None:
                    target = i
                    break
                if same_name:
                    failures.append(
                        f"{MERGE_FAILURE_PREFIX}table '{name}' is designed differently in "
                        f"{_groups_label(home.groups)} and group {group} ({conflict}); the merge "
                        f"kept the {_groups_label(home.groups)} design. Give the tables different "
                        "names or reconcile them into one design."
                    )
                    dropped = True
                    break
            if dropped:
                continue
            if target is None:
                home = _Home(table=table, groups=[group])
                home.add_primary(table, group)
                homes.append(home)
                continue

            home = homes[target]
            survivor_name = str(home.table.get("table_name"))
            home.table = _absorb(home.table, table)
            home.absorbed = True
            home.add_primary(table, group)
            if group not in home.groups:
                home.groups.append(group)
            if name and name != survivor_name:
                renames[pos][name] = survivor_name
                shared = [
                    s
                    for s in home.table.get("source_tables") or []
                    if normalize_table_name(s) in sources
                ]
                verb = "has" if len(shared) == 1 else "have"
                trade_offs.append(
                    {
                        "description": (
                            f"Group designs {survivor_name} ({_groups_label(home.groups[:1])}) and "
                            f"{name} (group {group}) both model {', '.join(shared)} with the same "
                            f"keys ({_describe_keys(home.table)}); the merge combines them into "
                            f"{survivor_name}."
                        ),
                        "impact": (
                            f"{', '.join(shared)} {verb} one DynamoDB home, {survivor_name}; "
                            f"access patterns designed against {name} run against {survivor_name}."
                        ),
                        "source_tables": shared,
                        "target_tables": [survivor_name],
                        "query_ids": list(dict.fromkeys(ap_queries.get(name, []))),
                        "engine": "dynamodb",
                    }
                )
    return TableReconciliation(homes, renames, failures, trade_offs)


def _renamed(name: Any, renames: dict[str, str]) -> Any:
    """Map a table (or ``Table.GSI``) reference through ``renames``."""
    if not isinstance(name, str):
        return name
    if name in renames:
        return renames[name]
    table, dot, rest = name.partition(".")
    return f"{renames[table]}.{rest}" if dot and table in renames else name


def apply_renames(draft: dict, renames: dict[str, str]) -> dict:
    """Re-point one draft's references to tables the merge absorbed."""
    if not renames:
        return draft
    result = dict(draft)
    for list_field in ("access_patterns", "hot_partition_analysis"):
        if isinstance(draft.get(list_field), list):
            result[list_field] = [
                (
                    {**item, "table_name": _renamed(item.get("table_name"), renames)}
                    if isinstance(item, dict)
                    else item
                )
                for item in draft[list_field]
            ]
    if isinstance(draft.get("trade_offs"), list):
        result["trade_offs"] = [
            (
                {
                    **t,
                    "target_tables": list(
                        dict.fromkeys(_renamed(n, renames) for n in t["target_tables"])
                    ),
                }
                if isinstance(t, dict) and isinstance(t.get("target_tables"), list)
                else t
            )
            for t in draft["trade_offs"]
        ]
    return result


def aggregate_hot_partitions(entries: Any, tables: set[str]) -> Any:
    """Sum the load of ``tables``' entries per (table, GSI, operation)."""
    if not isinstance(entries, list) or not tables:
        return entries
    result: list = []
    index: dict[tuple, int] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("table_name") not in tables:
            result.append(entry)
            continue
        key = (entry.get("table_name"), entry.get("gsi_name"), entry.get("operation"))
        if key not in index:
            index[key] = len(result)
            result.append(dict(entry))
            continue
        agg = result[index[key]]
        load = float(agg.get("rcu_or_wcu_per_second") or 0) + float(
            entry.get("rcu_or_wcu_per_second") or 0
        )
        agg["rcu_or_wcu_per_second"] = round(load, 2)
        limit = agg.get("partition_limit") or entry.get("partition_limit")
        if isinstance(limit, (int, float)) and limit > 0:
            agg["utilization_pct"] = round(min(100.0, load / limit * 100), 1)
            agg["at_risk"] = agg["utilization_pct"] > 80
        agg["contributing_patterns"] = list(
            dict.fromkeys(
                [
                    *(agg.get("contributing_patterns") or []),
                    *(entry.get("contributing_patterns") or []),
                ]
            )
        )
        agg["mitigation"] = agg.get("mitigation") or entry.get("mitigation")
        if agg.get("at_risk") and not agg["mitigation"]:
            agg["mitigation"] = (
                "Combined load from the merged group designs is over 80% of the partition "
                "limit; shard the partition key or spread writes before migration."
            )
    return result


# ---------------------------------------------------------------------------
# 3./4. Conflicts and independent homes on the combined design
# ---------------------------------------------------------------------------


def _covers(trade_off: dict, source: str, targets: list[str]) -> bool:
    if str(trade_off.get("description") or "").startswith(OVERLAP_PREFIX):
        return False  # our own review note does not justify the overlap
    sources = {
        normalize_table_name(s) for s in trade_off.get("source_tables") or [] if isinstance(s, str)
    }
    if source not in sources:
        return False
    named = {str(t).lower() for t in trade_off.get("target_tables") or [] if isinstance(t, str)}
    return all(
        t.lower() in named or any(n.startswith(t.lower() + ".") for n in named) for t in targets
    )


def entity_conflicts(homes: list[_Home]) -> list[str]:
    """Same entity type for the same source table with contradictory PK/SK templates."""
    seen: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for i, home in enumerate(homes):
        for entity in _dicts(home.table.get("entities")):
            sig = _entity_signature(entity)
            if _known(sig[1]):
                seen.setdefault(sig[:2], []).append((i, entity))
    failures: list[str] = []
    for (entity_type, _), defs in seen.items():
        variants = {_entity_signature(e)[2:] for _, e in defs}
        tables = [homes[i] for i, _ in defs]
        one_group = len({g for h in tables for g in h.groups}) == 1
        if len(variants) < 2 or one_group:
            continue
        described = ", ".join(
            f"{homes[i].table.get('table_name')} ({_groups_label(homes[i].groups)}; "
            f"PK {e.get('pk_template')}, SK {e.get('sk_template')})"
            for i, e in defs
        )
        failures.append(
            f"{MERGE_FAILURE_PREFIX}entity {entity_type} for source table "
            f"'{defs[0][1].get('source_table')}' has contradictory key templates: {described}. "
            "Use one PK/SK template for the same rows, or rename one entity type."
        )
    return failures


def independent_homes(homes: list[_Home], trade_offs: list[dict]) -> list[dict]:
    """Review trade-offs for source tables that several groups each gave their own entity."""
    by_source: dict[str, list[int]] = {}
    for i, home in enumerate(homes):
        for source in home.primary:
            by_source.setdefault(source, []).append(i)
    shown = {
        normalize_table_name(s): s
        for h in homes
        for s in [
            *(h.table.get("source_tables") or []),
            *[e.get("source_table") for e in _dicts(h.table.get("entities"))],
        ]
        if isinstance(s, str)
    }
    pending: dict[tuple[int, ...], list[str]] = {}
    for source, idxs in by_source.items():
        if len(idxs) < 2:
            continue
        # Overlap designed by one group on purpose: that group is in every home.
        if set.intersection(*(homes[i].primary[source] for i in idxs)):
            continue
        names = [str(homes[i].table.get("table_name")) for i in idxs]
        if any(_covers(t, source, names) for t in trade_offs):
            continue
        pending.setdefault(tuple(idxs), []).append(shown.get(source, source))

    notes: list[dict] = []
    for home_idxs, sources in pending.items():

        def groups_of(i: int, sources: list[str] = sources) -> set[int]:
            return set().union(*(homes[i].primary[normalize_table_name(s)] for s in sources))

        tables = ", ".join(
            f"{homes[i].table.get('table_name')} "
            f"({_groups_label(groups_of(i))}; {_describe_keys(homes[i].table)})"
            for i in home_idxs
        )
        names = [str(homes[i].table.get("table_name")) for i in home_idxs]
        notes.append(
            {
                "description": (
                    f"{OVERLAP_PREFIX}{', '.join(sources)} modelled independently by design "
                    f"groups in {tables}; choose one write path or keep copies in sync — "
                    "review before migration."
                ),
                "impact": (
                    f"Until this is reviewed, rows of {', '.join(sources)} have {len(home_idxs)} "
                    f"DynamoDB homes ({', '.join(names)}): a migration must either write one of "
                    "them and drop the others, or write all of them and keep them in sync."
                ),
                "source_tables": sources,
                "target_tables": names,
                "query_ids": [],
                "engine": "dynamodb",
            }
        )
    return notes


def merge_failures(schema_output: Any) -> list[str]:
    """Return the DynamoDB merge failures recorded in ``schema_output``."""
    if not isinstance(schema_output, dict):
        return []
    failures = schema_output.get("validation_failures")
    if not isinstance(failures, list):
        return []
    return [f for f in failures if isinstance(f, str) and f.startswith(MERGE_FAILURE_PREFIX)]


def merge_warnings(schema_output: Any) -> list[str]:
    """Return the review notes (warnings) the DynamoDB merge added to ``schema_output``."""
    if not isinstance(schema_output, dict):
        return []
    return [
        str(t["description"])
        for t in _dicts(schema_output.get("trade_offs"))
        if str(t.get("description") or "").startswith(OVERLAP_PREFIX)
    ]


def _draft_passed(draft: dict) -> bool:
    """``validation_passed`` of a draft, ignoring stale DynamoDB merge messages."""
    if draft.get("validation_passed", True):
        return True
    stale = merge_failures(draft)
    own = [f for f in draft.get("validation_failures") or [] if f not in stale]
    return bool(stale) and not own


def _without_stale(draft: dict) -> dict:
    """Drop merge output a draft may have copied from an earlier merged design."""
    result = dict(draft)
    if isinstance(draft.get("validation_failures"), list):
        stale = set(merge_failures(draft))
        result["validation_failures"] = [f for f in draft["validation_failures"] if f not in stale]
    if isinstance(draft.get("trade_offs"), list):
        result["trade_offs"] = [
            t
            for t in draft["trade_offs"]
            if not (
                isinstance(t, dict) and str(t.get("description") or "").startswith(OVERLAP_PREFIX)
            )
        ]
    return result


def prepare_drafts(
    drafts: list[dict], group_indices: list[int]
) -> tuple[list[dict], TableReconciliation, bool]:
    """Renumber, reconcile tables and re-point references, per draft, before combining.

    Returns the prepared drafts (ready for list concatenation), the table
    reconciliation, and whether all drafts passed their own validation.
    """
    passed = all(_draft_passed(d) for d in drafts)
    cleaned = [_without_stale(d) for d in drafts]
    renumbered = [
        renumber_draft(d, m) for d, m in zip(cleaned, pattern_id_maps(cleaned), strict=True)
    ]
    reconciliation = reconcile_tables(renumbered, group_indices)
    prepared = [
        apply_renames(d, r) for d, r in zip(renumbered, reconciliation.renames, strict=True)
    ]
    return prepared, reconciliation, passed


def finish_merge(merged: dict, reconciliation: TableReconciliation, passed: bool) -> dict:
    """Apply rules 2-4 to the combined design (tables, conflicts, review notes)."""
    homes = reconciliation.homes
    if "table_definitions" in merged or homes:
        merged["table_definitions"] = [h.table for h in homes]
    if "hot_partition_analysis" in merged:
        merged["hot_partition_analysis"] = aggregate_hot_partitions(
            merged["hot_partition_analysis"],
            {str(h.table.get("table_name")) for h in homes if h.absorbed},
        )
    trade_offs = _dicts(merged.get("trade_offs")) + reconciliation.trade_offs
    trade_offs += independent_homes(homes, trade_offs)
    if "trade_offs" in merged or trade_offs:
        merged["trade_offs"] = trade_offs

    failures = reconciliation.failures + entity_conflicts(homes)
    existing = [f for f in merged.get("validation_failures") or [] if isinstance(f, str)]
    if "validation_failures" in merged or failures:
        merged["validation_failures"] = existing + failures
    merged["validation_passed"] = passed and not failures
    return merged
