"""DynamoDB group-draft reconciliation (issues #223, #229).

DynamoDB schema design always runs split -> per-group drafts -> merge. Groups
are designed in parallel, so two groups whose queries touch the same source
table each design a home for it, and each numbers its access patterns from
``DDB-AP-1``. :func:`group_merger.merge_group_drafts` uses this module to turn
the drafts into one coherent design.

Merge rule
==========

0. **Draft order.** The drafts are first sorted by their content
   (:func:`canonical_draft_order`: the query IDs they cover, then their JSON),
   and every choice below breaks ties by content, never by draft position or
   group number (#809). The same drafts in any order give byte-identical
   output; renumbering the groups changes only the group labels in messages.
1. **Pattern IDs.** When ``access_patterns[].pattern_id`` collide across drafts,
   every draft is renumbered *before* the drafts are combined:
   ``DDB-AP-1..N`` in that sorted order. Each draft's old -> new map is applied to
   every ``DDB-AP-<n>`` mention in that draft's text (justifications,
   trade-offs, migration notes, hot-partition entries) and in its design trace,
   so references keep pointing at the same pattern.
1a. **Shared tables are reconciled** (#772): after renumbering, before anything is
    combined, :mod:`dynamodb_key_reconcile` rewrites the drafts so that tables
    parallel groups designed for the same rows agree where that is
    deterministic -- key-attribute names, entity labels, GSI names, one key
    schema per single-entity table (another group's key becomes a GSI), and a
    single-entity table folded into the item collection that holds its rows.
    Its trade-offs record each change. With the split's table ownership
    (``owners``, #1243), a context group's single-entity placeholder for a table
    another group owns is first pointed at the owner's table, and the owner's
    key schema is the one kept. The rules below then apply.
2. **Compatible tables merge.** Tables that share a source table (or a table
   name) are merged into one when they have the same partition/sort
   key names and types, the same shape (both item collections, or both
   single-entity tables) and nothing conflicts: item collections must share at
   least one identical entity (type, source table, PK/SK templates; not needed
   when they have the same table name) and may not
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
   (table, GSI, operation), and a trade-off records the merge. Tables are
   taken in :func:`_table_order`: the design the most other groups' tables
   can merge with comes first, so it is the one that survives (with its name)
   and the one a disagreeing design is reported against. Merged tables never
   get names that differ only in letter case (``Terms`` and ``terms``, #868):
   the table more groups shaped keeps the name, the other takes another name
   its groups use or ``<name>_<key attributes>``, and ``merge_reconciliation``
   in the design trace says why.
3. **True conflicts fail validation** (``validation_failures`` entries with
   prefix :data:`MERGE_FAILURE_PREFIX`, ``validation_passed=false``): one table
   name used for two different designs that rule 1a could not reconcile, the
   same entity type for the same source table with contradictory PK/SK
   templates (tokens compared by source column) in tables from different
   groups, a merged table with more GSIs than DynamoDB allows by default, and
   a table or GSI name over DynamoDB's 255-character limit (#866), and an access
   pattern or hot-partition entry on a table or GSI the merged design does not
   define (:func:`dangling_references`, #1243).
4. **Independent homes get one authoritative home.** When a source table is
   still the *primary entity* (its own entity in an item collection, or the
   base entity of a single-entity table) of two or more tables designed by
   different groups -- rules 1a and 2 could not give the rows one home -- the
   merge picks one authoritative home (:func:`_authoritative`: the table whose
   access patterns on those rows serve the most statements, then the most
   ``design_rps``, then by name; never the draft order) and keeps the others
   as derived copies, kept in sync from its DynamoDB stream. A review
   trade-off (description prefix :data:`OVERLAP_PREFIX`) names the tables,
   keys and the choice, and each write pattern on a derived copy gets a
   ``(merged design: ...)`` note pointing it at the authoritative home; no
   access pattern is moved or dropped. :func:`merge_warnings` reports the
   review; it never fails validation. A group's own trade-off naming the
   overlap does not replace the review (#333): it names the conflict without
   resolving it.
5. **Not flagged at all:** denormalized copies (a source table listed only in
   a table's ``source_tables`` or in attribute-level ``source_table``, without
   its own entity) and any overlap whose tables all come from one group (that
   group designed it on purpose).
"""

from __future__ import annotations

import json
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
# 0. Draft order (#809)
# ---------------------------------------------------------------------------


def _draft_order_key(draft: dict, group: int) -> tuple[list[str], str, int]:
    queries = sorted(
        {
            str(q)
            for ap in _dicts(draft.get("access_patterns"))
            for q in ap.get("query_ids") or []
            if isinstance(q, (str, int))
        }
    )
    return queries, json.dumps(draft, sort_keys=True, default=str), group


def canonical_draft_order(
    drafts: list[dict], group_indices: list[int]
) -> tuple[list[dict], list[int]]:
    """``drafts`` (with their group indices) in an order set by their content alone.

    Sorted by the query IDs each draft covers, then by the draft's JSON, so
    neither the order the drafts arrive in nor how the splitter numbered the
    groups changes the merged design (the group index only breaks a tie
    between identical drafts).
    """
    pairs = sorted(
        zip(drafts, group_indices, strict=True), key=lambda p: _draft_order_key(p[0], p[1])
    )
    return [d for d, _ in pairs], [g for _, g in pairs]


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
    """Why ``b`` cannot be merged into ``a`` (``None`` when it can).

    Item collections with the same table name need not share an entity: two
    groups that named the same table designed it as one table.
    """
    if _key_schema(a) != _key_schema(b):
        pairs = list(zip(_key_schema(a), _key_schema(b), strict=True))
        if all((x and y and x[0] == y[0]) or (x is None and y is None) for x, y in pairs):
            types = ", ".join(f"{x[0]} {x[1]} vs {y[1]}" for x, y in pairs if x and y and x != y)
            return f"key attribute types differ ({types})"
        return f"key schemas differ ({_describe_keys(a)} vs {_describe_keys(b)})"
    shape = _shape(a)
    if shape != _shape(b):
        return "one is an item collection, the other a single-entity table"
    if shape == "item_collection":
        a_entities = {e.get("entity_type"): e for e in _dicts(a.get("entities"))}
        b_entities = _dicts(b.get("entities"))
        a_sigs = {_entity_signature(e) for e in a_entities.values()}
        same_name = a.get("table_name") == b.get("table_name")
        if not same_name and not any(_entity_signature(e) in a_sigs for e in b_entities):
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


def _table_order(drafts: list[dict]) -> list[tuple[int, int, dict]]:
    """Every draft's tables as ``(draft position, index, table)``, in merge order.

    First the table that the most tables of other drafts could merge with
    (same name or a shared source table, and no conflict); then the one whose
    name fewer other designs use for something else; then the one more access
    patterns run on; then the one that holds more source tables' rows; then
    by table name and content. So the design most groups agree on becomes the
    home the others merge into (or conflict with), whatever the draft order or
    group numbering. Its name is voted afterwards (:func:`_voted_name`).
    """
    items = [
        (pos, i, table)
        for pos, draft in enumerate(drafts)
        for i, table in enumerate(_dicts(draft.get("table_definitions")))
    ]
    sources = [set(_designed_sources(t)) for _, _, t in items]
    support = [0] * len(items)
    contested = [0] * len(items)
    for a, (pos_a, _, table_a) in enumerate(items):
        for b in range(a + 1, len(items)):
            pos_b, _, table_b = items[b]
            if pos_a == pos_b:
                continue
            same_name = table_a.get("table_name") == table_b.get("table_name")
            if not same_name and not sources[a] & sources[b]:
                continue
            if _merge_conflict(table_a, table_b) is None:
                support[a] += 1
                support[b] += 1
            elif same_name:
                contested[a] += 1
                contested[b] += 1
    patterns = [
        sum(
            1
            for ap in _dicts(drafts[pos].get("access_patterns"))
            if ap.get("table_name") == table.get("table_name")
        )
        for pos, _, table in items
    ]
    order = sorted(
        range(len(items)),
        key=lambda k: (
            -support[k],
            contested[k],
            -patterns[k],
            -len(_primary_sources(items[k][2])),
            str(items[k][2].get("table_name") or "").casefold(),
            json.dumps(items[k][2], sort_keys=True, default=str),
            items[k][0],
            items[k][1],
        ),
    )
    return [items[k] for k in order]


@dataclass
class _Member:
    pos: int
    name: str
    group: int
    sources: set[str]
    patterns: int = 0


def _voted_name(
    members: list[_Member], taken: set[str], folded: frozenset[str] = frozenset()
) -> str | None:
    """The table name the most groups use (case-insensitively), not in ``taken``.

    Then the spelling most of those groups use. When groups are tied, the name
    more access patterns use; an exact tie goes to the alphabetically first
    name, ignoring case. A name whose case-folded form is in ``folded`` (a
    merged table already has it in some letter case) is skipped (#868).
    """
    groups: dict[str, dict[str, set[int]]] = {}
    patterns: dict[str, int] = {}
    for m in members:
        if m.name:
            groups.setdefault(m.name.casefold(), {}).setdefault(m.name, set()).add(m.pos)
            patterns[m.name] = patterns.get(m.name, 0) + m.patterns

    def rank(item: tuple[str, dict[str, set[int]]]) -> tuple:
        folded, spellings = item
        used = len(set().union(*spellings.values()))
        return -used, -sum(patterns[n] for n in spellings), folded

    for folded_name, spellings in sorted(groups.items(), key=rank):
        if folded_name in folded:
            continue
        by_spelling = sorted(spellings.items(), key=lambda s: (-len(s[1]), -patterns[s[0]], s[0]))
        for name, _ in by_spelling:
            if name not in taken:
                return name
    return None


def _case_distinct_name(name: str, table: dict, taken_folded: set[str]) -> str:
    """``name`` with a suffix naming ``table``'s keys, so no merged table shares it in any case.

    ``<name>_<key attributes>`` (the GSI rename convention), then ``2``, ``3``
    ... when that is taken too; always within DynamoDB's name limit.
    """
    from src.agents.schema_design.dynamodb_key_reconcile import _pascal, fit_name

    pk, sk = _key_schema(table)
    suffix = "".join(_pascal(k[0]) for k in (pk, sk) if k) or "Table"
    wanted = f"{name}_{suffix}"
    final, n = fit_name(wanted), 2
    while final.casefold() in taken_folded:
        final, n = fit_name(f"{wanted}{n}"), n + 1
    return final


def reconcile_tables(
    drafts: list[dict], group_indices: list[int], notes: list[str] | None = None
) -> TableReconciliation:
    """Merge compatible tables across ``drafts`` (rule 2) and flag name conflicts.

    Tables are taken in :func:`_table_order`; each merged table then takes the
    name most of its groups use (:func:`_voted_name`). Two merged tables never
    get names that differ only in letter case (#868): the table more groups
    shaped keeps the name, the other takes another of its groups' names or
    ``<name>_<key attributes>``, and a note for ``notes`` (the design trace)
    says why. The merged tables are listed in the drafts' order (the first
    table that went into each).
    """
    homes: list[_Home] = []
    members: list[list[_Member]] = []
    dropped_into: list[list[tuple[int, str]]] = []
    first_seen: list[tuple[int, int]] = []
    renames: list[dict[str, str]] = [{} for _ in drafts]
    failures: list[str] = []
    trade_offs: list[dict] = []
    ap_queries: list[dict[str, list[str]]] = []
    for draft in drafts:
        queries: dict[str, list[str]] = {}
        for ap in _dicts(draft.get("access_patterns")):
            queries.setdefault(str(ap.get("table_name")), []).extend(
                q for q in ap.get("query_ids") or [] if isinstance(q, str)
            )
        ap_queries.append(queries)

    def ap_count(pos: int, name: str) -> int:
        return sum(
            1 for ap in _dicts(drafts[pos].get("access_patterns")) if ap.get("table_name") == name
        )

    for pos, index, table in _table_order(drafts):
        group = group_indices[pos]
        name = str(table.get("table_name") or "")
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
                dropped_into[i].append((pos, name))
                dropped = True
                break
        if dropped:
            continue
        if target is None:
            home = _Home(table=table, groups=[group])
            home.add_primary(table, group)
            homes.append(home)
            members.append([_Member(pos, name, group, sources, ap_count(pos, name))])
            dropped_into.append([])
            first_seen.append((pos, index))
            continue

        home = homes[target]
        first_seen[target] = min(first_seen[target], (pos, index))
        home.table = _absorb(home.table, table)
        home.absorbed = True
        home.add_primary(table, group)
        members[target].append(_Member(pos, name, group, sources, ap_count(pos, name)))
        if group not in home.groups:
            home.groups.append(group)

    # Name every merged table by vote; never give two merged tables one name.
    order = sorted(range(len(homes)), key=lambda i: (-len(members[i]), first_seen[i]))
    finals: dict[str, int] = {}  # case-folded final name -> home
    for i in order:
        others = {m.name for j in range(len(homes)) if j != i for m in members[j]}
        home = homes[i]
        plain = _voted_name(members[i], others) or str(home.table.get("table_name"))
        final = _voted_name(members[i], others, frozenset(finals))
        if final is None and plain.casefold() not in finals:
            final = plain
        if final is None:
            taken = set(finals) | {n.casefold() for n in others}
            final = _case_distinct_name(plain, home.table, taken)
        if final != plain and notes is not None and plain.casefold() in finals:
            kept = homes[finals[plain.casefold()]]
            reason = _merge_conflict(kept.table, home.table) or "they hold different rows"
            notes.append(
                f"{final}: table {plain} ({_groups_label(home.groups)}) renamed {final}; "
                f"table {kept.table.get('table_name')} ({_groups_label(kept.groups)}) has "
                f"the same name in other letter case and a different design ({reason}), "
                "so the merge keeps two tables with names that differ beyond letter case"
            )
        finals[final.casefold()] = i
        home.table = {**home.table, "table_name": final}
        for pos, name in [(m.pos, m.name) for m in members[i]] + dropped_into[i]:
            if name and name != final:
                renames[pos][name] = final
        final_groups = [m.group for m in members[i] if m.name == final]
        for m in members[i]:
            if not m.name or m.name == final:
                continue
            shared = [
                s
                for s in home.table.get("source_tables") or []
                if normalize_table_name(s) in m.sources
            ]
            verb = "has" if len(shared) == 1 else "have"
            designs = (
                f"Group designs {final} ({_groups_label(final_groups)}) and {m.name}"
                if final_groups
                else f"The group design {m.name}"
            )
            trade_offs.append(
                {
                    "description": (
                        f"{designs} (group {m.group}) both model {', '.join(shared)} with the "
                        f"same keys ({_describe_keys(home.table)}); the merge combines them into "
                        f"{final}."
                        if final_groups
                        else f"{designs} (group {m.group}) models {', '.join(shared)} with the "
                        f"same keys as other groups' tables ({_describe_keys(home.table)}); the "
                        f"merge combines them into {final}."
                    ),
                    "impact": (
                        f"{', '.join(shared)} {verb} one DynamoDB home, {final}; "
                        f"access patterns designed against {m.name} run against {final}."
                    ),
                    "source_tables": shared,
                    "target_tables": [final],
                    "query_ids": list(dict.fromkeys(ap_queries[m.pos].get(m.name, []))),
                    "engine": "dynamodb",
                }
            )
    listed = sorted(range(len(homes)), key=lambda i: first_seen[i])
    return TableReconciliation([homes[i] for i in listed], renames, failures, trade_offs)


def _renamed(name: Any, renames: dict[str, str]) -> Any:
    """Map a table (or ``Table.GSI``) reference through ``renames``."""
    if not isinstance(name, str):
        return name
    if name in renames:
        return renames[name]
    table, dot, rest = name.partition(".")
    return f"{renames[table]}.{rest}" if dot and table in renames else name


def _renamed_note(ap: Any, renames: dict[str, str]) -> Any:
    """Point a ``(merged design: in <table> as ...)`` note at the table's final name."""
    text = ap.get("key_condition") if isinstance(ap, dict) else None
    if not isinstance(text, str) or "(merged design:" not in text:
        return ap
    head, marker, notes = text.partition("(merged design:")
    for old, new in renames.items():
        notes = notes.replace(f"in {old} as ", f"in {new} as ")
    return {**ap, "key_condition": head + marker + notes}


def apply_renames(draft: dict, renames: dict[str, str]) -> dict:
    """Re-point one draft's references to tables the merge absorbed."""
    if not renames:
        return draft
    result = dict(draft)
    if isinstance(draft.get("access_patterns"), list):
        draft = {
            **draft,
            "access_patterns": [_renamed_note(ap, renames) for ap in draft["access_patterns"]],
        }
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


def entity_conflicts(homes: list[_Home]) -> list[str]:
    """Same entity type for the same source table with contradictory PK/SK templates."""
    seen: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for i, home in enumerate(homes):
        for entity in _dicts(home.table.get("entities")):
            sig = _entity_signature(entity)
            if _known(sig[1]):
                seen.setdefault(sig[:2], []).append((i, entity))
    from src.agents.schema_design.dynamodb_key_reconcile import canon_template

    failures: list[str] = []
    for (entity_type, _), defs in seen.items():
        # Tokens compared by the source column they copy: {ID} and {post_id}
        # (an attribute copying ID) are the same key.
        variants = {
            (
                canon_template(str(e.get("pk_template") or ""), e),
                canon_template(str(e.get("sk_template") or ""), e),
            )
            for _, e in defs
        }
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


WRITE_OPERATIONS = frozenset(
    {"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"}
)


def _on_rows(ap: dict, table: str, sources: set[str]) -> bool:
    """``ap`` runs on ``table`` and touches ``sources`` (or names no source table)."""
    if ap.get("table_name") != table:
        return False
    named = {normalize_table_name(s) for s in ap.get("source_tables") or [] if isinstance(s, str)}
    return not named or bool(named & sources)


def _float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _authoritative(names: list[str], sources: set[str], access_patterns: list[dict]) -> int:
    """Index in ``names`` of the home that serves ``sources``' rows best (#333).

    The table whose access patterns on those rows serve the most statements
    (distinct query IDs), then the most summed ``design_rps``, then by name
    (case-insensitive, then exact): the content alone decides, never the draft
    order or group numbering.
    """

    def rank(i: int) -> tuple[int, float, str, str]:
        patterns = [ap for ap in access_patterns if _on_rows(ap, names[i], sources)]
        statements = {
            str(q) for ap in patterns for q in ap.get("query_ids") or [] if isinstance(q, str)
        }
        rps = sum(_float(ap.get("design_rps")) for ap in patterns)
        return -len(statements), -rps, names[i].casefold(), names[i]

    return min(range(len(names)), key=rank)


def overlap_home(trade_off: dict, access_patterns: list[dict]) -> tuple[str, list[str]] | None:
    """The authoritative home and the derived copies of an overlap trade-off
    (:func:`merge_overlaps`), chosen as :func:`independent_homes` chose them
    (:func:`_authoritative`) from the merged design's ``access_patterns``; None when
    the trade-off names fewer than two target tables (#1528)."""
    names = [t for t in trade_off.get("target_tables") or [] if isinstance(t, str)]
    if len(names) < 2:
        return None
    sources = {
        normalize_table_name(s) for s in trade_off.get("source_tables") or [] if isinstance(s, str)
    }
    kept = names[_authoritative(names, sources, _dicts(access_patterns))]
    return kept, [n for n in names if n != kept]


def _add_note(ap: dict, note: str) -> None:
    """Append ``note`` to ``ap``'s ``(merged design: ...)`` note, or start one."""
    text = str(ap.get("key_condition") or "")
    if note in text:
        return  # a draft copied from an earlier merge already carries it
    if "(merged design:" in text and text.endswith(")"):
        ap["key_condition"] = f"{text[:-1]}; {note})"
    else:
        ap["key_condition"] = f"{text} (merged design: {note})".strip()


def independent_homes(homes: list[_Home], access_patterns: list[dict]) -> list[dict]:
    """Review trade-offs for source tables that several groups each gave their own entity.

    Each names the authoritative home (:func:`_authoritative`) and the derived
    copies; write patterns on a copy get a note pointing them at the
    authoritative home (``access_patterns`` is edited in place).
    """
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
        normalised = {normalize_table_name(s) for s in sources}
        kept = names[_authoritative(names, normalised, access_patterns)]
        copies = [n for n in names if n != kept]
        one = len(copies) == 1
        shown_copies = ", ".join(copies)
        shown_sources = ", ".join(sources)
        for ap in access_patterns:
            if ap.get("operation") in WRITE_OPERATIONS and any(
                _on_rows(ap, c, normalised) for c in copies
            ):
                _add_note(
                    ap,
                    f"write {kept}, the authoritative home of {shown_sources}; "
                    f"{ap.get('table_name')} is a derived copy kept in sync from {kept}'s "
                    "DynamoDB Streams",
                )
        notes.append(
            {
                "description": (
                    f"{OVERLAP_PREFIX}{shown_sources} modelled independently by design "
                    f"groups in {tables}; the merge keeps {kept} as the authoritative home "
                    f"(chosen by the statements its access patterns serve, then their design "
                    f"RPS) and {shown_copies} as "
                    f"{'a derived copy' if one else 'derived copies'}; review before migration."
                ),
                "impact": (
                    f"Writes to {shown_sources} go to {kept}; {shown_copies} "
                    f"{'is' if one else 'are'} updated from {kept}'s DynamoDB Streams, "
                    f"eventually consistent, so reads on {'it' if one else 'them'} can "
                    "return rows older than the latest write."
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


def merge_overlaps(schema_output: Any) -> list[dict]:
    """Return the unresolved cross-group overlap trade-offs in ``schema_output``.

    Each is the full trade-off :func:`independent_homes` built (description,
    impact, ``source_tables``, ``target_tables``, ``query_ids``, ``engine``) --
    the ``OVERLAP_PREFIX`` check lives only here, so a reader never has to know
    the prefix to find them. Deduplicated by (``source_tables``, ``target_tables``):
    a schema output copied from an earlier merge, or merged more than once,
    must not double an open conflict.
    """
    if not isinstance(schema_output, dict):
        return []
    seen: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()
    overlaps: list[dict] = []
    for t in _dicts(schema_output.get("trade_offs")):
        if not str(t.get("description") or "").startswith(OVERLAP_PREFIX):
            continue
        key = (
            tuple(s for s in t.get("source_tables") or [] if isinstance(s, str)),
            tuple(s for s in t.get("target_tables") or [] if isinstance(s, str)),
        )
        if key in seen:
            continue
        seen.add(key)
        overlaps.append(t)
    return overlaps


def merge_warnings(schema_output: Any) -> list[str]:
    """Return the review notes (warnings) the DynamoDB merge added to ``schema_output``."""
    return [str(t["description"]) for t in merge_overlaps(schema_output)]


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
    drafts: list[dict],
    group_indices: list[int],
    unique_keys: dict[str, list[frozenset[str]]] | None = None,
    notes: list[str] | None = None,
    column_types: dict[str, dict[str, str]] | None = None,
    owners: dict[str, int] | None = None,
) -> tuple[list[dict], TableReconciliation, bool]:
    """Renumber, reconcile tables and re-point references, per draft, before combining.

    Returns the prepared drafts (ready for list concatenation), the table
    reconciliation, and whether all drafts passed their own validation.
    ``unique_keys``, ``notes``, ``column_types`` and ``owners`` (the split's table
    ownership, #1243) are passed to rule 1a
    (:func:`dynamodb_key_reconcile.reconcile_shared_tables`).
    """
    from src.agents.schema_design.dynamodb_key_reconcile import reconcile_shared_tables

    passed = all(_draft_passed(d) for d in drafts)
    cleaned = [_without_stale(d) for d in drafts]
    renumbered = [
        renumber_draft(d, m) for d, m in zip(cleaned, pattern_id_maps(cleaned), strict=True)
    ]
    owner_failures: list[str] = []  # what the split's ownership cannot reconcile (#1243)
    renumbered, key_trade_offs = reconcile_shared_tables(
        renumbered,
        group_indices,
        unique_keys=unique_keys,
        notes=notes,
        column_types=column_types,
        owners=owners,
        failures=owner_failures,
    )
    reconciliation = reconcile_tables(renumbered, group_indices, notes)
    reconciliation.trade_offs[:0] = key_trade_offs
    reconciliation.failures[:0] = [f"{MERGE_FAILURE_PREFIX}{f}" for f in owner_failures]
    prepared = [
        apply_renames(d, r) for d, r in zip(renumbered, reconciliation.renames, strict=True)
    ]
    return prepared, reconciliation, passed


def dangling_references(design: dict) -> list[str]:
    """A failure for each access pattern or hot-partition entry naming a table or GSI
    the design does not define (#1243): such a design cannot run its queries, so it
    never passes validation."""
    gsis = {
        str(t.get("table_name")): {str(g.get("gsi_name")) for g in _dicts(t.get("gsis"))}
        for t in _dicts(design.get("table_definitions"))
    }
    failures: list[str] = []
    for list_field, label in (
        ("access_patterns", "access pattern"),
        ("hot_partition_analysis", "hot-partition entry"),
    ):
        for item in _dicts(design.get(list_field)):
            what = f"{label} {item.get('pattern_id') or ''}".rstrip()
            table, gsi = str(item.get("table_name") or ""), item.get("gsi_name")
            if table not in gsis:
                failures.append(
                    f"{MERGE_FAILURE_PREFIX}{what} runs on table '{table}', but no table "
                    "of that name is defined"
                )
            elif gsi and str(gsi) not in gsis[table]:
                failures.append(
                    f"{MERGE_FAILURE_PREFIX}{what} uses GSI '{gsi}', but table '{table}' "
                    "has no GSI of that name"
                )
    return failures


def finish_merge(
    merged: dict,
    reconciliation: TableReconciliation,
    passed: bool,
    notes: list[str] | None = None,
) -> dict:
    """Apply rules 2-4 to the combined design (tables, conflicts, review notes).

    GSIs with the same keys on one merged table are combined first (one GSI per
    key schema, the wider projection kept).
    """
    from src.agents.schema_design.dynamodb_key_reconcile import (
        dedupe_gsis,
        gsi_limit_failures,
        name_length_failures,
    )

    homes = reconciliation.homes
    if "table_definitions" in merged or homes:
        merged["table_definitions"] = [h.table for h in homes]
        dedupe_gsis(merged, notes)
    if "hot_partition_analysis" in merged:
        merged["hot_partition_analysis"] = aggregate_hot_partitions(
            merged["hot_partition_analysis"],
            {str(h.table.get("table_name")) for h in homes if h.absorbed},
        )
    trade_offs = _dicts(merged.get("trade_offs")) + reconciliation.trade_offs
    trade_offs += independent_homes(homes, _dicts(merged.get("access_patterns")))
    if "trade_offs" in merged or trade_offs:
        merged["trade_offs"] = trade_offs

    failures = (
        reconciliation.failures
        + entity_conflicts(homes)
        + gsi_limit_failures([h.table for h in homes], MERGE_FAILURE_PREFIX)
        + name_length_failures([h.table for h in homes], MERGE_FAILURE_PREFIX)
        + dangling_references(merged)
    )
    existing = [f for f in merged.get("validation_failures") or [] if isinstance(f, str)]
    if "validation_failures" in merged or failures:
        merged["validation_failures"] = existing + failures
    merged["validation_passed"] = passed and not failures
    return merged
