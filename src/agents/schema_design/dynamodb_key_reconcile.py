"""Deterministic key reconciliation for DynamoDB group drafts (issue #772).

DynamoDB schema design always runs in groups of about 20 queries, designed in
parallel. A hub source table (users, posts, topics, ...) is touched by queries
in many groups, so each of those groups designs a home for it, and they
disagree: different key-attribute names for the same column, different key
schemas, different GSIs under the same name, different labels for the same
entity. :mod:`dynamodb_merge` only merges tables whose keys already agree, so
before this module every disagreement was a merge failure that a model-driven
fix round had to resolve, and on a large workload (31 groups on the discourse
sample) that fix round did not fit in the run's budget.

:func:`reconcile_shared_tables` rewrites the drafts, before they are combined,
so the merge sees compatible tables. Every step is deterministic and only
touches tables from *different* drafts (a group that designs two homes for the
same rows did it on purpose). A group's trade-off that names the source table
and every table involved ("a second, independent home ...") does not keep them
apart (#333): it is a model's text, not a decision. The tables are reconciled as
if it were absent, and the trade-off, which describes the design the merge
replaced, is dropped; the merge trace names the dropped home
(:meth:`_Drafts.settle`).

With the split's ownership (``owners``, #1243), a context group's single-entity
placeholder for rows another group owns is reconciled with the owner's home for
them. Before anything else, a placeholder takes the name of the owner's
single-entity table for those rows (:func:`_point_context_tables`), so the rules
below treat it as that table. After rule 4, a placeholder the owner keeps in an
item collection is folded into it with its GSIs, the collection's key kept
(:func:`_fold_into_owner_collections`). A placeholder whose owner only copies the
rows stays as it is. Without ownership (a manifest written before it) none of this
runs.

0. **A partition-key type disagreement** (``N`` vs ``S``) between tables meant
   as one table, with the same key-attribute names, takes the type of the
   source column behind the key (integer -> ``N``, text -> ``S``, from the
   collector's schema), with a trade-off: a partition key's type only affects
   hashing. This narrows the rule that type disagreements stay failures; a
   sort-key type disagreement (it changes ordering and range conditions), an
   unknown or mixed source type, or a prefixed PK template still fail.
1. **Item collections meant as one table** (the same name, or an entity for
   the same rows -- same source table and partition-key template -- with the
   same PK type, and transitively) take one key-attribute name per key and
   type. Entities for the same rows take one ``entity_type`` and pair of
   templates when their sort-key templates use the same source columns (only
   the static text differs, e.g. ``PROFILE`` vs ``USER``) and they share the
   label or the sort key: ``USER``/``PROFILE`` and ``USER_STATS``/``STATS``
   are two items of one row and stay two entities (#867). Each is the one
   most of the tables use, then the longer one, then alphabetical (never the
   draft order). Template tokens are compared by the source column they copy, so
   ``{ID}`` and ``{post_id}`` (an attribute copying ``ID``) are the same.
2. **Single-entity tables for the same rows** (same base source table) name a
   key or GSI-key attribute that copies the same source column the same way:
   the owner group's name when the split gives the rows an owner (#1243), else
   the name most of those tables use, then the longer name, then alphabetical
   (never the draft order).
3. **Single-entity tables for the same rows with different key schemas** keep
   one key schema, and only a key that is safe for every group:

   - it identifies every item each other group writes: the other key's
     columns are among its columns, or it holds a unique key of the source
     table (primary key or unique index, from the collector's schema). A key
     with fewer columns would make writes overwrite each other's rows;
   - no other group has a conditional write (``attribute_not_exists`` and the
     like) on its own, replaced key: the condition would check another item.
     A write is conditional when its ``condition_expression`` is set (#810).
     When the field is null or missing (a draft written before it existed),
     its ``key_condition`` and ``description`` text is matched as well: that
     can only keep more keys. The text match misses a condition described
     only in other words, which is why the field exists;
   - no pattern that would have to go through a GSI needs a strongly
     consistent read or a transaction, which a GSI cannot give.

   Among the safe keys the merge keeps the one that serves the most access
   patterns directly, then one holding the source primary key, then by name.
   With the split's ownership (#1243), the owner group's key is the only one
   kept: when it is not safe, nothing is rewritten and the merge reports it.
   With no safe key, nothing is rewritten and the merge reports the conflict.
   Every other table takes the kept key schema; its own key becomes a GSI
   (one with the same keys is reused and widened if needed) when a pattern
   still needs it. Reads on that key become eventually consistent ``Query``
   calls on the GSI (projection ``ALL``); updates and deletes find the item's
   table key through the GSI first, a non-atomic, eventually consistent
   read-then-write (a ``KEYS_ONLY`` GSI is enough for those). A ``PutItem``,
   or a single-item operation that knows the kept key's columns, runs as is.
   Any other attribute-type disagreement (``N`` vs ``S``) is never changed:
   the merge reports it. One trade-off per table records a change that alters how a
   pattern runs, with the new GSIs' storage and write cost.
4. **A single-entity table for rows an item collection holds** is folded into
   it when its key maps onto the entity (the PK template is the key's
   partition column, the SK template static or one prefix plus the key's sort
   column), or, for a collection with the table's own name and partition key
   that has no entity for those rows yet, as a new entity (type and static SK
   = the source table name, upper case). It becomes a part of that collection,
   so the merge combines them; its access patterns get the collection's SK
   condition. When the folded table had writes into an existing entity, a
   trade-off says writes must carry the union of the attributes.
5. **GSIs with the same name and different keys** in tables that will merge
   (same name, or the same rows): the keys most of those tables give the name
   keep it (then fewer key attributes), every other GSI of that name is
   renamed after its own keys (``<name>_<KeyAttributes>``). A new or renamed
   GSI name longer than :data:`NAME_LIMIT` is cut with a hash of the full name
   (:func:`fit_name`), so it stays unique and the same in every run (#866).

Finally :func:`dedupe_gsis` keeps one GSI per key schema on each merged table.
Mechanical changes (names, labels, folds without writes, renamed or combined
GSIs) go to the merge notes (``merge_reconciliation`` in ``design_trace.json``), not the
trade-offs; each rewritten access pattern gets one ``(merged design: ...)``
note in its ``key_condition``.

What is left (a table name used for different rows, a key no group can safely
give up, sort-key templates over different columns, key attribute types that
disagree, an item collection and a single-entity table that do not map, more
than :data:`GSI_LIMIT` GSIs on one merged table, a table or GSI name over
:data:`NAME_LIMIT` characters) stays a merge failure for the fix round.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.agents.schema_design.scope import normalize_table_name

GSI_LIMIT = 20
"""GSIs DynamoDB allows on one table by default."""

NAME_LIMIT = 255
"""Longest table or index name DynamoDB accepts (#866)."""

READ_OPERATIONS = frozenset({"GetItem", "Query", "BatchGetItem", "TransactGetItems"})
_TOKEN_RE = re.compile(r"\{([^{}]+)\}")


# ---------------------------------------------------------------------------
# Small helpers (kept local: dynamodb_merge imports this module)
# ---------------------------------------------------------------------------


def _dicts(value: Any) -> list[dict]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _key_name(key: Any) -> str | None:
    name = key.get("attribute_name") if isinstance(key, dict) else None
    return name if isinstance(name, str) and name else None


def _key_type(key: Any) -> str:
    return str(key.get("attribute_type") or "") if isinstance(key, dict) else ""


def _shape(table: dict) -> str:
    if _dicts(table.get("entities")):
        return "item_collection"
    if _dicts(table.get("attributes")):
        return "single"
    return "empty"


def _known(name: str) -> bool:
    return bool(name) and name != "unknown"


def _direct_column(attr: dict | None, base: str) -> str | None:
    """The source column ``attr`` copies unchanged from ``base`` (else None)."""
    if not isinstance(attr, dict) or attr.get("join") or attr.get("calculation"):
        return None
    column = attr.get("source_column")
    if not isinstance(column, str) or not column:
        return None
    if normalize_table_name(str(attr.get("source_table") or "")) != base:
        return None
    return column.lower()


def _base_entity(table: dict) -> str:
    """Normalised source table of the partition-key attribute (else the first source)."""
    pk = _key_name(table.get("partition_key"))
    for attr in _dicts(table.get("attributes")):
        if pk and attr.get("name") == pk and isinstance(attr.get("source_table"), str):
            return normalize_table_name(attr["source_table"])
    sources = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
    return normalize_table_name(sources[0]) if sources else ""


def _shown(base: str, *tables: dict) -> str:
    """The source table name as the drafts spell it (``base`` is normalised)."""
    for table in tables:
        names = [s for s in table.get("source_tables") or [] if isinstance(s, str)]
        names += [
            str(a.get("source_table"))
            for a in _dicts(table.get("attributes")) + _dicts(table.get("entities"))
            if isinstance(a.get("source_table"), str)
        ]
        for name in names:
            if normalize_table_name(name) == base:
                return name
    return base


def _attributes(table: dict) -> dict[str, dict]:
    return {a["name"]: a for a in _dicts(table.get("attributes")) if isinstance(a.get("name"), str)}


def _gsi_key_names(gsi: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    def names(value: Any) -> tuple[str, ...]:
        return tuple(n for n in (_key_name(k) for k in _dicts(value)) if n)

    return names(gsi.get("partition_key")), names(gsi.get("sort_key"))


def _gsi_signature(gsi: dict) -> tuple:
    def keys(value: Any) -> tuple:
        return tuple((_key_name(k), _key_type(k)) for k in _dicts(value))

    return keys(gsi.get("partition_key")), keys(gsi.get("sort_key"))


def _table_key(table: dict) -> tuple[str | None, str | None]:
    return _key_name(table.get("partition_key")), _key_name(table.get("sort_key"))


def _describe(pk: str | None, sk: str | None) -> str:
    text = f"PK {pk}" if pk else "no PK"
    return f"{text}, SK {sk}" if sk else text


def _pascal(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^0-9A-Za-z]+", name) if p)


def _mentions(text: str, name: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text))


_NOTES = "_merge_notes"


def _note(ap: dict, note: str) -> None:
    """Queue a note for ``ap``; :func:`_flush_notes` appends them once, in one parenthesis."""
    notes = ap.setdefault(_NOTES, [])
    if note not in notes:
        notes.append(note)


def _flush_notes(drafts: list[dict]) -> None:
    for draft in drafts:
        for ap in _dicts(draft.get("access_patterns")):
            notes = ap.pop(_NOTES, None)
            if notes:
                text = str(ap.get("key_condition") or "")
                ap["key_condition"] = f"{text} (merged design: {'; '.join(notes)})".strip()


def _covers(trade_off: dict, source: str, targets: Iterable[str]) -> bool:
    """A draft's own trade-off that names ``source`` and every table in ``targets``.

    Such a trade-off describes those tables as separate homes; :meth:`_Drafts.settle`
    drops it when the merge gives the rows one home (#333)."""
    sources = {
        normalize_table_name(s) for s in trade_off.get("source_tables") or [] if isinstance(s, str)
    }
    if source not in sources:
        return False
    named = {str(t).lower() for t in trade_off.get("target_tables") or [] if isinstance(t, str)}
    return all(
        t.lower() in named or any(n.startswith(t.lower() + ".") for n in named) for t in targets
    )


# ---------------------------------------------------------------------------
# Draft bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _Ref:
    """One table definition inside one draft (the dict is edited in place)."""

    pos: int
    group: int
    table: dict

    @property
    def name(self) -> str:
        return str(self.table.get("table_name") or "")


class _Drafts:
    def __init__(self, drafts: list[dict], group_indices: list[int]) -> None:
        self.drafts = drafts
        self.groups = group_indices
        self.trade_offs: list[dict] = []
        self.notes: list[str] = []  # mechanical changes, for the merge trace
        self.failures: list[str] = []  # what the merge must fail on (#1243)

    def refs(self) -> list[_Ref]:
        return [
            _Ref(pos, self.groups[pos], table)
            for pos, draft in enumerate(self.drafts)
            for table in _dicts(draft.get("table_definitions"))
        ]

    def patterns(self, ref: _Ref) -> list[dict]:
        """The access patterns of ``ref``'s draft that run against ``ref``'s table."""
        return [
            ap
            for ap in _dicts(self.drafts[ref.pos].get("access_patterns"))
            if ap.get("table_name") == ref.name
        ]

    def hot_partitions(self, ref: _Ref) -> list[dict]:
        return [
            e
            for e in _dicts(self.drafts[ref.pos].get("hot_partition_analysis"))
            if e.get("table_name") == ref.name
        ]

    def settle(self, source: str, kept: _Ref, dropped: list[_Ref]) -> None:
        """Drop each draft trade-off that kept ``dropped`` apart from ``kept`` (#333).

        Call it just before the rewrite that gives ``source``'s rows one home,
        ``kept``. A trade-off naming ``source`` and every one of those tables
        describes the design the rewrite replaces, so it is removed; the trace
        note names the tables only, never the trade-off's (model-written) text.
        """
        names = list(dict.fromkeys(r.name for r in [kept, *dropped]))
        if len(names) < 2:
            return
        for draft in self.drafts:
            trade_offs = draft.get("trade_offs")
            if not isinstance(trade_offs, list):
                continue
            kept_offs = [
                t for t in trade_offs if not (isinstance(t, dict) and _covers(t, source, names))
            ]
            if len(kept_offs) == len(trade_offs):
                continue
            draft["trade_offs"] = kept_offs
            others = ", ".join(
                f"{r.name} (group {r.group})"
                for r in sorted(dropped, key=lambda r: (r.name, r.group))
                if r.name != kept.name
            )
            self.notes.append(
                f"{_shown(source, kept.table, *(r.table for r in dropped))}: a design group's "
                f"trade-off kept {others} apart from {kept.name} (group {kept.group}) as "
                f"another home for these rows; the merge keeps one home, {kept.name}, and "
                "drops that trade-off"
            )

    def rename_table(self, ref: _Ref, new: str) -> None:
        """Re-point ``ref``'s draft from its table name to ``new`` and rename the table."""
        old = ref.name
        if old == new:
            return
        draft = self.drafts[ref.pos]
        for list_field in ("access_patterns", "hot_partition_analysis"):
            for item in _dicts(draft.get(list_field)):
                name = item.get("table_name")
                if name == old:
                    item["table_name"] = new
                elif isinstance(name, str) and name.startswith(old + "."):
                    item["table_name"] = new + name[len(old) :]
        for trade_off in _dicts(draft.get("trade_offs")):
            targets = trade_off.get("target_tables")
            if isinstance(targets, list):
                trade_off["target_tables"] = list(
                    dict.fromkeys(
                        (
                            new
                            if t == old
                            else (
                                new + t[len(old) :]
                                if isinstance(t, str) and t.startswith(old + ".")
                                else t
                            )
                        )
                        for t in targets
                    )
                )
        ref.table["table_name"] = new

    def rename_gsi(self, ref: _Ref, old: str, new: str) -> None:
        for gsi in _dicts(ref.table.get("gsis")):
            if gsi.get("gsi_name") == old:
                gsi["gsi_name"] = new
        for item in self.patterns(ref) + self.hot_partitions(ref):
            if item.get("gsi_name") == old:
                item["gsi_name"] = new


def _rename_attributes(drafts: _Drafts, ref: _Ref, renames: dict[str, str]) -> None:
    """Rename key/GSI-key attributes of one table and note it on its access patterns."""
    if not renames:
        return
    drafts.notes.append(
        f"{ref.name} (group {ref.group}): key attribute "
        + ", ".join(f"{old} renamed {new}" for old, new in renames.items())
    )
    table = ref.table
    for key_field in ("partition_key", "sort_key"):
        key = table.get(key_field)
        if isinstance(key, dict) and key.get("attribute_name") in renames:
            key["attribute_name"] = renames[key["attribute_name"]]

    def rename_list(attrs: list[dict]) -> list[dict]:
        names = {a.get("name") for a in attrs}
        result: list[dict] = []
        for attr in attrs:
            new = renames.get(attr.get("name"))  # type: ignore[arg-type]
            if new is None:
                result.append(attr)
            elif new not in names:  # the same column already has the new name: keep that one
                result.append({**attr, "name": new})
        return result

    if isinstance(table.get("attributes"), list):
        table["attributes"] = rename_list(_dicts(table["attributes"]))
    for entity in _dicts(table.get("entities")):
        entity["attributes"] = rename_list(_dicts(entity.get("attributes")))
    for gsi in _dicts(table.get("gsis")):
        for key_field in ("partition_key", "sort_key"):
            for key in _dicts(gsi.get(key_field)):
                if key.get("attribute_name") in renames:
                    key["attribute_name"] = renames[key["attribute_name"]]
        if isinstance(gsi.get("projected_attributes"), list):
            gsi["projected_attributes"] = list(
                dict.fromkeys(renames.get(a, a) for a in gsi["projected_attributes"])
            )
        if gsi.get("sparse_attribute") in renames:
            gsi["sparse_attribute"] = renames[gsi["sparse_attribute"]]
    for ap in drafts.patterns(ref):
        if isinstance(ap.get("returned_attributes"), list):  # #1509: what the read returns
            ap["returned_attributes"] = list(
                dict.fromkeys(renames.get(a, a) for a in ap["returned_attributes"])
            )
        text = str(ap.get("key_condition") or "")
        said = [f"{old} is {new}" for old, new in renames.items() if _mentions(text, old)]
        if said:
            _note(ap, f"key attribute {', '.join(said)}")


# ---------------------------------------------------------------------------
# 1. Item collections with the same name
# ---------------------------------------------------------------------------


def _entity_names(table: dict) -> set[str]:
    return {
        str(a.get("name"))
        for e in _dicts(table.get("entities"))
        for a in _dicts(e.get("attributes"))
    }


def _rows(entity: dict) -> tuple[str, str]:
    """(source table, partition-key template by source column): the rows an entity holds."""
    return (
        normalize_table_name(str(entity.get("source_table") or "")),
        canon_template(str(entity.get("pk_template") or ""), entity),
    )


def _same_collection(a: dict, b: dict) -> bool:
    """Item collections two groups meant as one table: same name, or same rows and PK type."""
    if a.get("table_name") == b.get("table_name"):
        return True
    if _key_type(a.get("partition_key")) != _key_type(b.get("partition_key")):
        return False
    rows = {_rows(e) for e in _dicts(a.get("entities")) if _known(_rows(e)[0])}
    return any(_rows(e) in rows for e in _dicts(b.get("entities")))


def _collection_clusters(drafts: _Drafts) -> list[list[_Ref]]:
    """Item collections from different drafts meant as one table.

    Two collections are meant as one table when they have the same name or
    hold the same rows (:func:`_same_collection`); clusters are the transitive
    closure of that, so the result does not depend on which one comes first.
    """
    collections = [r for r in drafts.refs() if _shape(r.table) == "item_collection"]
    parent = list(range(len(collections)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(collections):
        for j in range(i + 1, len(collections)):
            b = collections[j]
            if a.pos != b.pos and (a.name == b.name or _same_collection(a.table, b.table)):
                parent[find(j)] = find(i)
    clusters: dict[int, list[_Ref]] = {}
    for i, ref in enumerate(collections):
        clusters.setdefault(find(i), []).append(ref)
    return [c for c in clusters.values() if len({r.pos for r in c}) > 1]


def _align_collections(drafts: _Drafts) -> None:
    """Give item collections meant as one table the same key names and entity labels.

    Each choice is a vote across the cluster (see :func:`_vote_order`): the
    name or label most of its tables use, then the longer one, then
    alphabetical -- never the draft order.
    """
    for cluster in _collection_clusters(drafts):
        for key_field in ("partition_key", "sort_key"):
            votes: dict[str, dict[str, int]] = {}  # attribute type -> name -> tables
            for ref in cluster:
                key = ref.table.get(key_field)
                if _key_name(key):
                    names = votes.setdefault(_key_type(key), {})
                    names[str(_key_name(key))] = names.get(str(_key_name(key)), 0) + 1
            canonical = {t: sorted(n.items(), key=_vote_order)[0][0] for t, n in votes.items()}
            for ref in cluster:
                have = ref.table.get(key_field)
                have_name, want_name = _key_name(have), canonical.get(_key_type(have))
                if not have_name or not want_name or have_name == want_name:
                    continue
                own_keys = {n for n in _table_key(ref.table) if n}
                if want_name in own_keys or want_name in _entity_names(ref.table):
                    continue  # the name means something else in this table
                _rename_attributes(drafts, ref, {have_name: want_name})
        _align_entities(drafts, cluster)


def canon_template(template: str, entity: dict) -> str:
    """``template`` with each ``{token}`` naming the source column it copies."""
    source = normalize_table_name(str(entity.get("source_table") or ""))
    columns = {
        str(a.get("name")): _direct_column(a, source) for a in _dicts(entity.get("attributes"))
    }

    def column(match: re.Match) -> str:
        token = match.group(1)
        return "{" + (columns.get(token) or token.lower()) + "}"

    return _TOKEN_RE.sub(column, template)


def unresolved_tokens(template: str, entity: dict) -> list[str]:
    """The ``{token}`` names of ``template`` that no attribute of ``entity`` carries (#1532).

    A token names a DynamoDB attribute, as :func:`canon_template` reads it, so a token that
    matches an attribute (case-insensitively) is resolved through it: a direct attribute's
    ``source_column`` is checked on its own, and a derived one is built by its join or
    calculation. Only the tokens returned here have to be source columns. Both the schema
    design's column check and the data movement plan use this one reading."""
    named = {str(a.get("name")).lower() for a in _dicts(entity.get("attributes"))}
    return [t for t in _TOKEN_RE.findall(template) if t.lower() not in named]


def _entity_bucket(entity: dict) -> tuple[tuple[str, str], tuple[str, ...]]:
    """(rows, sort-key columns): entities in one bucket are the same rows, keyed alike."""
    sk = canon_template(str(entity.get("sk_template") or ""), entity)
    return _rows(entity), tuple(_TOKEN_RE.findall(sk))


Variant = tuple[str, str, str]
"""(entity type, PK template, SK template) of one entity, as a draft wrote it."""


def _one_item(a: Variant, b: Variant, sort_keys: dict[Variant, str]) -> bool:
    """Two variants of the same rows that name one item: the same label or the same sort key.

    A different label *and* a different sort key (``USER``/``PROFILE`` vs
    ``USER_STATS``/``STATS``) are two items of one row (#867). ``sort_keys``
    holds each variant's SK template by source column (:func:`canon_template`).
    """
    return a[0] == b[0] or sort_keys[a] == sort_keys[b]


def _item_variants(
    variants: dict[Variant, int], sort_keys: dict[Variant, str]
) -> list[dict[Variant, int]] | None:
    """``variants`` of one bucket split into the items they name, or None when ambiguous.

    Variants are one item when :func:`_one_item` links them, transitively.
    An item whose variants are not all linked pairwise (``USER``/``PROFILE``
    ~ ``USER``/``STATS`` ~ ``USER_STATS``/``STATS``) gives no safe label:
    None, so nothing in the bucket is relabelled and the merge reports it.
    """
    items: list[dict[Variant, int]] = []
    for variant, count in sorted(variants.items()):
        linked = [i for i in items if any(_one_item(variant, v, sort_keys) for v in i)]
        joined = {variant: count}
        for item in linked:
            joined.update(item)
            items.remove(item)
        items.append(joined)
    for item in items:
        pairs = [(a, b) for a in item for b in item if a < b]
        if any(not _one_item(a, b, sort_keys) for a, b in pairs):
            return None
    return items


def _align_entities(drafts: _Drafts, cluster: list[_Ref]) -> None:
    """Give entities for the same rows one label and one pair of templates.

    Same rows = same source table and the same partition-key template (tokens
    compared by the source column they copy). The sort-key templates must use
    the same columns: the same template, both static, or the same columns
    behind different static text (``PROFILE`` vs ``USER``, ``META#{meta_id}``
    vs ``M#{meta_id}``); different columns stay a conflict. Variants are one
    item only when they share the label or the sort key (:func:`_one_item`):
    ``USER``/``PROFILE`` and ``USER_STATS``/``STATS`` stay two entities even
    when no table holds both (#867). For each item, the label and templates
    kept are the ones most tables use, then the longer label, then
    alphabetical. Only when every table has at most one entity for those rows:
    a table that stores one row as several items (``PROFILE`` and ``STATS``)
    gives no single label to align to, so nothing is relabelled and the merge
    reports the disagreement.
    """
    votes: dict[tuple, dict[Variant, int]] = {}
    sort_keys: dict[tuple, dict[Variant, str]] = {}  # bucket -> variant -> SK by column
    ambiguous: set[tuple] = set()
    for ref in cluster:
        seen: set[tuple] = set()
        in_table: set[tuple] = set()
        for entity in _dicts(ref.table.get("entities")):
            bucket = _entity_bucket(entity)
            if not _known(bucket[0][0]):
                continue
            if bucket in in_table:
                ambiguous.add(bucket)  # one row stored as several items: no single label
            in_table.add(bucket)
            variant = (
                str(entity.get("entity_type") or ""),
                str(entity.get("pk_template") or ""),
                str(entity.get("sk_template") or ""),
            )
            sort_keys.setdefault(bucket, {}).setdefault(variant, canon_template(variant[2], entity))
            if (bucket, variant) not in seen:
                seen.add((bucket, variant))
                counts = votes.setdefault(bucket, {})
                counts[variant] = counts.get(variant, 0) + 1
    canonical: dict[tuple, dict[Variant, Variant]] = {}
    for bucket, counts in votes.items():
        if len(counts) < 2 or bucket in ambiguous:
            continue
        items = _item_variants(counts, sort_keys[bucket])
        if items is None:
            note = (
                f"{bucket[0][0]}: entities "
                + ", ".join(f"{t} (SK {sk})" for t, _, sk in sorted(counts))
                + " left as they are: they share labels and sort keys in a chain, so which "
                "items are the same is ambiguous"
            )
            if note not in drafts.notes:  # collections are aligned twice
                drafts.notes.append(note)
            continue
        for item in items:
            if len(item) < 2:
                continue
            keep = sorted(item.items(), key=lambda v: (-v[1], -len(v[0][0]), v[0]))[0][0]
            canonical.setdefault(bucket, {}).update({v: keep for v in item})
    if not canonical:
        return
    for ref in cluster:
        used = {e.get("entity_type") for e in _dicts(ref.table.get("entities"))}
        for entity in _dicts(ref.table.get("entities")):
            target = canonical.get(_entity_bucket(entity), {}).get(
                (
                    str(entity.get("entity_type") or ""),
                    str(entity.get("pk_template") or ""),
                    str(entity.get("sk_template") or ""),
                )
            )
            if target is None:
                continue
            label, pk_tpl, new_sk = target
            if label != entity.get("entity_type") and label in used:
                continue
            old_sk = str(entity.get("sk_template") or "")
            if (label, pk_tpl, new_sk) == (
                entity.get("entity_type"),
                entity.get("pk_template"),
                old_sk,
            ):
                continue
            used.discard(entity.get("entity_type"))
            used.add(label)
            entity["entity_type"] = label
            entity["pk_template"] = pk_tpl
            entity["sk_template"] = new_sk
            old_head, new_head = old_sk.split("{", 1)[0], new_sk.split("{", 1)[0]
            if old_head and old_head != new_head:
                for ap in drafts.patterns(ref):
                    if old_head in str(ap.get("key_condition") or ""):
                        _note(ap, f"sort key '{old_head}' is '{new_head}'")


# ---------------------------------------------------------------------------
# 0. Partition-key types from the source column
# ---------------------------------------------------------------------------

ColumnTypes = dict[str, dict[str, str]]
"""Normalised source table -> lower-case column -> ``"N"`` or ``"S"`` (known types only)."""

_INTEGER_NATIVE = re.compile(r"^((tiny|small|medium|big)?int(eger)?\d*|(small|big)?serial)\b", re.I)
_TEXT_NATIVE = re.compile(
    r"^(n?varchar|n?char|character( varying)?|(tiny|medium|long)?text|citext|uuid|string)\b",
    re.I,
)


def _column_kind(column: dict) -> str | None:
    """``N`` for an integer column, ``S`` for a text column, else None (not decided here)."""
    normalized = str(column.get("normalized_data_type") or "").lower()
    if normalized == "integer":
        return "N"
    if normalized in ("string", "text", "uuid"):
        return "S"
    if normalized:
        return None
    native = str(column.get("data_type") or "").strip()
    if _INTEGER_NATIVE.match(native):
        return "N"
    if _TEXT_NATIVE.match(native):
        return "S"
    return None


def column_types_from_collector(collector_output: Any) -> ColumnTypes:
    """Integer and text columns of the collector's tables, as DynamoDB key types."""
    result: ColumnTypes = {}
    schema = collector_output.get("database_schema") if isinstance(collector_output, dict) else None
    for table in _dicts((schema or {}).get("tables")):
        name = normalize_table_name(str(table.get("table_id") or table.get("table_name") or ""))
        kinds = {
            str(c.get("column_name")).lower(): kind
            for c in _dicts(table.get("columns"))
            if (kind := _column_kind(c))
        }
        if _known(name) and kinds:
            result[name] = kinds
    return result


def _pk_sources(table: dict) -> list[tuple[str, str]] | None:
    """(source table, column) behind the partition-key value, or None if not plain columns.

    A single-entity table's key attribute must copy a column unchanged; each
    entity of an item collection must use a PK template that is exactly one
    ``{column}`` (a prefix makes the value text whatever the column is).
    """
    if _shape(table) == "single":
        base = _base_entity(table)
        column = _direct_column(
            _attributes(table).get(_key_name(table.get("partition_key")) or ""), base
        )
        return [(base, column)] if column else None
    found: list[tuple[str, str]] = []
    for entity in _dicts(table.get("entities")):
        head, tokens, tail = _template(canon_template(str(entity.get("pk_template") or ""), entity))
        if head or tail or len(tokens) != 1:
            return None
        found.append((normalize_table_name(str(entity.get("source_table") or "")), tokens[0]))
    return found or None


def _set_key_type(table: dict, attribute: str, kind: str) -> None:
    table["partition_key"] = {**table["partition_key"], "attribute_type": kind}
    for attr in _dicts(table.get("attributes")) + [
        a for e in _dicts(table.get("entities")) for a in _dicts(e.get("attributes"))
    ]:
        if attr.get("name") == attribute:
            attr["type"] = kind
    for gsi in _dicts(table.get("gsis")):
        for field_name in ("partition_key", "sort_key"):
            for key in _dicts(gsi.get(field_name)):
                if key.get("attribute_name") == attribute:
                    key["attribute_type"] = kind


def _align_pk_types(drafts: _Drafts, column_types: ColumnTypes) -> None:
    """Resolve an N-vs-S partition-key disagreement from the source column's type.

    Only for tables meant as one (same name and shape, or single-entity tables
    for the same rows) whose partition-key attribute has the same name, and
    that all have a sort key or all have none. The source type must be known for every
    column behind the key, and agree. A sort-key type disagreement is never
    touched: it changes ordering and range conditions.
    """
    clusters: dict[tuple, list[_Ref]] = {}
    for ref in drafts.refs():
        shape = _shape(ref.table)
        if shape == "single" and _known(_base_entity(ref.table)):
            clusters.setdefault(("single", _base_entity(ref.table)), []).append(ref)
        elif shape == "item_collection":
            clusters.setdefault(("collection", ref.name), []).append(ref)
    for refs in clusters.values():
        if len({r.pos for r in refs}) < 2:
            continue
        # Sort-key names may still differ (rule 1 aligns them); sort keys must
        # be present on all or none.
        pks = {_table_key(r.table)[0] for r in refs}
        has_sk = {_table_key(r.table)[1] is not None for r in refs}
        types = {_key_type(r.table.get("partition_key")) for r in refs}
        if len(pks) != 1 or len(has_sk) != 1 or len(types) < 2 or not next(iter(pks)):
            continue
        attribute = str(next(iter(pks)))
        kinds: set[str | None] = set()
        sources: list[tuple[str, str]] = []
        for ref in refs:
            behind = _pk_sources(ref.table)
            if behind is None:
                kinds.add(None)
                continue
            for source, column in behind:
                sources.append((source, column))
                kinds.add(column_types.get(source, {}).get(column))
        if len(kinds) != 1 or None in kinds:
            continue  # unknown or mixed source type: the merge reports it
        kind = str(next(iter(kinds)))
        changed = [r for r in refs if _key_type(r.table.get("partition_key")) != kind]
        for ref in changed:
            _set_key_type(ref.table, attribute, kind)
        other = "S" if kind == "N" else "N"
        names = sorted({r.name for r in refs})
        columns = ", ".join(sorted({f"{s}.{c}" for s, c in sources}))
        drafts.trade_offs.append(
            {
                "description": (
                    f"Design groups typed partition key {attribute} of {', '.join(names)} "
                    f"differently: {_groups_text(changed)} used {other}, the others {kind}. "
                    f"The merge uses {kind}."
                ),
                "impact": (
                    "partition key type only affects hashing; matches the source column "
                    f"({columns}, {'integer' if kind == 'N' else 'text'})."
                ),
                "source_tables": sorted({_shown(s, *(r.table for r in refs)) for s, _ in sources}),
                "target_tables": names,
                "query_ids": [],
                "engine": "dynamodb",
            }
        )


def _groups_text(refs: list[_Ref]) -> str:
    groups = sorted({r.group for r in refs})
    return ("group " if len(groups) == 1 else "groups ") + ", ".join(str(g) for g in groups)


# ---------------------------------------------------------------------------
# 2./3. Single-entity tables for the same rows
# ---------------------------------------------------------------------------


def _singles_by_base(drafts: _Drafts) -> dict[str, list[_Ref]]:
    """Base source table -> single-entity tables for it, when they span drafts.

    A base that one draft gives two single-entity tables is left out: that
    group modelled the rows twice on purpose.
    """
    by_base: dict[str, list[_Ref]] = {}
    for ref in drafts.refs():
        if _shape(ref.table) == "single":
            base = _base_entity(ref.table)
            if _known(base):
                by_base.setdefault(base, []).append(ref)
    result: dict[str, list[_Ref]] = {}
    for base, refs in by_base.items():
        positions = [r.pos for r in refs]
        if len(set(positions)) < 2 or len(set(positions)) != len(positions):
            continue
        result[base] = refs
    return result


def _key_attribute_names(table: dict) -> list[str]:
    names = [n for n in _table_key(table) if n]
    for gsi in _dicts(table.get("gsis")):
        pk, sk = _gsi_key_names(gsi)
        names += [*pk, *sk]
    return list(dict.fromkeys(names))


def _vote_order(item: tuple[str, int]) -> tuple[int, int, str]:
    """Most tables, then the longer (more specific) name, then alphabetical: no draft order."""
    return -item[1], -len(item[0]), item[0]


def _align_single_names(drafts: _Drafts, owners: dict[str, int] | None = None) -> None:
    for base, refs in _singles_by_base(drafts).items():
        votes: dict[str, dict[str, int]] = {}  # column -> name -> tables using it
        owned: dict[str, str] = {}  # column -> the owner group's name for it (#1243)
        owner = (owners or {}).get(base)
        for ref in sorted(refs, key=lambda r: r.name):
            attrs = _attributes(ref.table)
            seen: set[tuple[str, str]] = set()
            for name in _key_attribute_names(ref.table):
                column = _direct_column(attrs.get(name), base)
                if column and (column, name) not in seen:
                    seen.add((column, name))
                    names = votes.setdefault(column, {})
                    names[name] = names.get(name, 0) + 1
                    if ref.group == owner:
                        owned.setdefault(column, name)
        canonical = {c: sorted(n.items(), key=_vote_order)[0][0] for c, n in votes.items()}
        canonical |= owned
        for ref in refs:
            attrs = _attributes(ref.table)
            renames: dict[str, str] = {}
            for name in _key_attribute_names(ref.table):
                column = _direct_column(attrs.get(name), base)
                new = canonical.get(column or "")
                if not new or new == name:
                    continue
                if new in attrs and _direct_column(attrs[new], base) != column:
                    continue  # the name already means another column here
                renames[name] = new
            _rename_attributes(drafts, ref, renames)


Key = tuple[str | None, str | None]

_CONDITIONAL_RE = re.compile(
    r"attribute_(not_)?exists|ConditionExpression|condition(al)?\s+(write|check|put|update|delete)"
    r"|\bif\s+not\s+exists\b|\bunique",
    re.IGNORECASE,
)
_TRANSACTIONS = frozenset({"TransactWriteItems", "TransactGetItems"})
_WRITES = frozenset({"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"})
_ITEM_OPERATIONS = frozenset(
    {
        "GetItem",
        "BatchGetItem",
        "TransactGetItems",
        "UpdateItem",
        "DeleteItem",
        "BatchWriteItem",
        "TransactWriteItems",
    }
)


def conditional_write(ap: dict) -> bool:
    """A write guarded by a condition on the item (uniqueness, existence, version).

    A set ``condition_expression`` is taken at its word (#810). When it is
    null or missing (a draft written before the field existed), the
    ``key_condition`` and ``description`` text is still matched: a null field
    is no proof that the write is unconditional, and a false match only keeps
    a key, never drops a guarantee.
    """
    if ap.get("operation") not in _WRITES:
        return False
    if str(ap.get("condition_expression") or "").strip():
        return True
    text = f"{ap.get('key_condition') or ''} {ap.get('description') or ''}"
    return bool(_CONDITIONAL_RE.search(text))


def _needs_strong(ap: dict) -> bool:
    """A pattern a GSI lookup cannot serve: strong reads, conditions, transactions."""
    return (
        bool(ap.get("strongly_consistent"))
        or conditional_write(ap)
        or ap.get("operation") in _TRANSACTIONS
    )


def _key_columns(table: dict, base: str) -> tuple[str | None, str | None]:
    """The table key as source columns (``@name`` for an attribute that is not a direct copy)."""
    attrs = _attributes(table)

    def column(name: str | None) -> str | None:
        if not name:
            return None
        return _direct_column(attrs.get(name), base) or f"@{name}"

    pk, sk = _table_key(table)
    return column(pk), column(sk)


def _columns(key: tuple[str | None, str | None]) -> set[str]:
    return {c for c in key if c}


def _served_by(ap: dict, old: tuple, new: tuple) -> bool:
    """True when ``ap``, designed for table key ``old``, runs under table key ``new`` as is.

    ``old`` and ``new`` are keys as source columns, and ``new`` is known to
    identify the rows uniquely. A ``PutItem`` writes the whole row; a
    single-item operation knows every column of ``old``, so it can address
    ``new`` when ``new``'s columns are among them; a ``Query`` needs the same
    partition column and, when it had a sort key, the same sort column.
    """
    op = ap.get("operation")
    if op == "PutItem":
        return True
    if op in _ITEM_OPERATIONS:
        return _columns(new) <= _columns(old)
    if op == "Query":
        return new[0] == old[0] and (old[1] is None or new[1] == old[1])
    return False


@dataclass
class _KeyPlan:
    """Why one key schema can (or cannot) serve every table for the same rows."""

    key: Key
    columns: tuple[str | None, str | None]
    score: int
    contains_primary: bool
    blocked: str | None  # why the key cannot be kept, or None


def _plan(
    drafts: _Drafts,
    refs: list[_Ref],
    base: str,
    ref_key: _Ref,
    unique: list[frozenset[str]],
) -> _KeyPlan:
    key = _table_key(ref_key.table)
    cols = _key_columns(ref_key.table, base)
    kept = _columns(cols)
    provably_unique = any(u <= kept for u in unique)
    score = 0
    blocked: str | None = None
    for ref in refs:
        own_cols = _key_columns(ref.table, base)
        same = _table_key(ref.table) == key
        gsis = {
            g.get("gsi_name")
            for g in _dicts(ref.table.get("gsis"))
            if _gsi_key_names(g) == ((key[0],), (key[1],) if key[1] else ())
        }
        for ap in drafts.patterns(ref):
            gsi = ap.get("gsi_name")
            if (same and not gsi) or (gsi and gsi in gsis):
                score += 1
        if same or blocked:
            continue
        if not (_columns(own_cols) <= kept or provably_unique):
            blocked = (
                f"{_describe(*key)} would not identify every item {ref.name} "
                f"(group {ref.group}; {_describe(*_table_key(ref.table))}) writes"
            )
            continue
        for ap in drafts.patterns(ref):
            if ap.get("gsi_name"):
                continue
            if conditional_write(ap):
                blocked = (
                    f"{ap.get('pattern_id')} is a conditional write on "
                    f"{_describe(*_table_key(ref.table))}"
                )
                break
            if not _served_by(ap, own_cols, cols) and _needs_strong(ap):
                blocked = (
                    f"{ap.get('pattern_id')} needs a strongly consistent or transactional "
                    f"{ap.get('operation')} on {_describe(*_table_key(ref.table))}"
                )
                break
    primary = [u for u in unique if u]
    contains_primary = bool(primary) and primary[0] <= kept
    return _KeyPlan(key, cols, score, contains_primary, blocked)


def _projected_size(table: dict, keys: int) -> int:
    """Estimated KEYS_ONLY index item: 100 bytes of index overhead plus ~16 per key attribute."""
    size = _int(table.get("item_size_bytes")) or 0
    return min(size, 100 + 16 * keys) if size else 0


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def fit_name(name: str, limit: int = NAME_LIMIT) -> str:
    """``name`` cut to ``limit`` characters, stably (#866).

    A name within the limit is returned as is. A longer one keeps its first
    characters and ends ``_<8 hex>``, a hash of the whole name, so two long
    names that share a prefix still get different results, and the same name
    always gets the same result.
    """
    if len(name) <= limit:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"{name[: limit - len(digest) - 1]}_{digest}"


def _unique_gsi_name(wanted: str, taken: set[str]) -> str:
    """``wanted`` (or ``wanted2``, ``wanted3`` ...), within :data:`NAME_LIMIT` and not taken."""
    name, n = fit_name(wanted), 2
    while name in taken:
        name, n = fit_name(f"{wanted}{n}"), n + 1
    return name


def _widen(gsi: dict, projection: str, attributes: list[str] | None = None) -> None:
    """Widen ``gsi``'s projection (KEYS_ONLY < INCLUDE < ALL), never narrow it."""
    order = {"KEYS_ONLY": 0, "INCLUDE": 1, "ALL": 2}
    have = str(gsi.get("projection") or "KEYS_ONLY")
    if order.get(projection, 0) > order.get(have, 0):
        gsi["projection"] = projection
    if gsi.get("projection") == "ALL":
        gsi.pop("projected_attributes", None)
    elif gsi.get("projection") == "INCLUDE" and attributes:
        gsi["projected_attributes"] = list(
            dict.fromkeys([*(gsi.get("projected_attributes") or []), *attributes])
        )


@dataclass
class _Rekeyed:
    ref: _Ref
    old: Key
    gsi: str | None
    gsi_new: bool
    reads: list[dict]
    lookups: list[dict]
    direct: list[dict]


def _rekey(
    drafts: _Drafts, ref: _Ref, winner: _Ref, refs: list[_Ref], base: str, plan: _KeyPlan
) -> _Rekeyed:
    """Give ``ref`` the kept key schema and keep its own key as a GSI where a pattern needs it."""
    old_pk, old_sk = ref.table.get("partition_key"), ref.table.get("sort_key")
    old = (_key_name(old_pk), _key_name(old_sk))
    old_cols = _key_columns(ref.table, base)
    new = plan.key
    patterns = [ap for ap in drafts.patterns(ref) if not ap.get("gsi_name")]
    needs_gsi = [ap for ap in patterns if not _served_by(ap, old_cols, plan.columns)]
    reads = [ap for ap in needs_gsi if ap.get("operation") in READ_OPERATIONS]

    ref.table["partition_key"] = copy.deepcopy(winner.table.get("partition_key"))
    ref.table["sort_key"] = copy.deepcopy(winner.table.get("sort_key"))
    attrs = _attributes(ref.table)
    winner_attrs = _attributes(winner.table)
    for name in (n for n in new if n):
        if name not in attrs and name in winner_attrs:
            ref.table.setdefault("attributes", []).append(copy.deepcopy(winner_attrs[name]))

    gsi_name: str | None = None
    gsi_new = False
    if needs_gsi:
        signature = (
            ((old[0], _key_type(old_pk)),),
            ((old[1], _key_type(old_sk)),) if old[1] else (),
        )
        projection = "ALL" if reads else "KEYS_ONLY"
        same_keys = [
            g for r in refs for g in _dicts(r.table.get("gsis")) if _gsi_signature(g) == signature
        ]
        if same_keys:
            gsi_name = str(same_keys[0].get("gsi_name"))
            for gsi in _dicts(ref.table.get("gsis")) + [
                g for r in refs for g in _dicts(r.table.get("gsis"))
            ]:
                if gsi.get("gsi_name") == gsi_name:
                    _widen(gsi, projection)
        else:
            taken = {str(g.get("gsi_name")) for r in refs for g in _dicts(r.table.get("gsis"))}
            wanted = (
                f"{_pascal(winner.name) or 'Table'}By{_pascal(old[0] or '')}{_pascal(old[1] or '')}"
            )
            gsi_name = _unique_gsi_name(wanted, taken)
            gsi_new = True
            item_size = _int(ref.table.get("item_size_bytes")) or 0
            ref.table.setdefault("gsis", []).append(
                {
                    "gsi_name": gsi_name,
                    "partition_key": [copy.deepcopy(old_pk)],
                    "sort_key": [copy.deepcopy(old_sk)] if old[1] else None,
                    "projection": projection,
                    "item_count": _int(ref.table.get("item_count")) or 0,
                    "item_size_bytes": (
                        item_size
                        if projection == "ALL"
                        else _projected_size(ref.table, len(_columns(old)) + len(_columns(new)))
                    ),
                }
            )

    moved_queries: set[str] = set()
    for ap in patterns:
        op = ap.get("operation")
        if ap not in needs_gsi:
            _note(ap, f"the table key is now {_describe(*new)}")
        elif op in READ_OPERATIONS:
            if op != "Query":
                _note(ap, f"was {op} on the table key; one Query per key")
                ap["operation"] = "Query"
            ap["gsi_name"] = gsi_name
            ap["strongly_consistent"] = False
            moved_queries.update(q for q in ap.get("query_ids") or [] if isinstance(q, str))
            _note(ap, f"runs on GSI {gsi_name}, the table key is now {_describe(*new)}")
        else:
            ap["strongly_consistent"] = False
            _note(
                ap,
                f"the table key is now {_describe(*new)}; find the item's key through GSI "
                f"{gsi_name} first (eventually consistent, not atomic)",
            )
    for entry in drafts.hot_partitions(ref):
        contributing = {q for q in entry.get("contributing_patterns") or [] if isinstance(q, str)}
        if (
            gsi_name
            and not entry.get("gsi_name")
            and entry.get("operation") == "read"
            and contributing
            and contributing <= moved_queries
        ):
            entry["gsi_name"] = gsi_name
    lookups = [ap for ap in needs_gsi if ap not in reads]
    direct = [ap for ap in patterns if ap not in needs_gsi]
    return _Rekeyed(ref, old, gsi_name, gsi_new, reads, lookups, direct)


def _owner_single(drafts: _Drafts, owner: int, base: str) -> _Ref | None:
    """The owner group's single-entity table whose own entity is ``base``'s rows (by name)."""
    found = sorted(
        (
            r
            for r in drafts.refs()
            if r.group == owner and _shape(r.table) == "single" and _base_entity(r.table) == base
        ),
        key=lambda r: r.name,
    )
    return found[0] if found else None


def _owner_entity(drafts: _Drafts, owner: int, base: str) -> tuple[_Ref, dict] | None:
    """The owner group's item collection and its entity for ``base``'s rows (by
    collection name, then entity type), or None."""
    found = sorted(
        (
            (r, e)
            for r in drafts.refs()
            if r.group == owner and _shape(r.table) == "item_collection"
            for e in _dicts(r.table.get("entities"))
            if normalize_table_name(str(e.get("source_table") or "")) == base
        ),
        key=lambda pair: (pair[0].name, str(pair[1].get("entity_type") or "")),
    )
    return found[0] if found else None


def _placeholder_owner(drafts: _Drafts, ref: _Ref, owners: dict[str, int]) -> int | None:
    """The owner group of ``ref``'s rows when ``ref`` is a context placeholder: a
    single-entity table in another group's draft, every source table of which that
    one owner owns. Else None."""
    if not owners or _shape(ref.table) != "single":
        return None
    base = _base_entity(ref.table)
    sources = {normalize_table_name(str(t)) for t in ref.table.get("source_tables") or []}
    owner = owners.get(base)
    if owner is None or owner == ref.group or {owners.get(t) for t in sources | {base}} != {owner}:
        return None
    return owner


def _point_context_tables(drafts: _Drafts, owners: dict[str, int]) -> None:
    """Point each context placeholder at its owner's single-entity table (#1243).

    A placeholder (:func:`_placeholder_owner`) takes the name of the owner's
    single-entity table for its rows, and its access patterns, hot-partition entries
    and trade-offs follow, so rule 3 keeps one key schema (the owner's) and the merge
    combines the two. An owner that keeps the rows in an item collection is handled
    by :func:`_fold_into_owner_collections`; an owner with no home of its own for the
    rows (it only copies them) leaves the placeholder as is. Also left as is: a draft
    that already has another table of that name.
    """
    for ref in drafts.refs():
        owner = _placeholder_owner(drafts, ref, owners)
        if owner is None:
            continue
        base = _base_entity(ref.table)
        target = _owner_single(drafts, owner, base)
        if target is None or target.name == ref.name:
            continue
        taken = {r.name for r in drafts.refs() if r.pos == ref.pos and r.table is not ref.table}
        if target.name in taken:
            continue
        drafts.settle(base, target, [ref])
        drafts.notes.append(
            f"{_shown(base, ref.table)}: group {ref.group}'s table {ref.name} is a context "
            f"placeholder; pointed at owner group {owner}'s table {target.name}"
        )
        drafts.rename_table(ref, target.name)


def _fold_into_owner_collections(drafts: _Drafts, owners: dict[str, int]) -> None:
    """Fold each context placeholder the owner keeps in an item collection into it (#1243).

    Runs after rule 4, for a placeholder still on its own whose owner has no
    single-entity table for the rows but an item collection with an entity for them.
    The collection keeps its key schema. The placeholder's GSIs join the collection
    (one with the same name and other keys is renamed) and their key attributes join
    the owner's entity; its patterns and hot-partition entries move to the
    collection. A pattern on the placeholder's own key moves only when the entity's
    PK and SK templates copy nothing but the placeholder's key columns, so the
    placeholder's key finds the entity's item. Otherwise, or when a GSI key attribute
    already means another column in the entity, nothing moves and the merge fails
    naming the pattern (``drafts.failures``). As in rule 4, every attribute of the
    placeholder joins the entity, and a write that moves onto the owner's items gets
    the trade-off that writes must carry the union of the attributes.
    """
    folded_writes: FoldedWrites = {}
    for ref in drafts.refs():
        owner = _placeholder_owner(drafts, ref, owners)
        if owner is None:
            continue
        base = _base_entity(ref.table)
        if _owner_single(drafts, owner, base) is not None:
            continue
        home = _owner_entity(drafts, owner, base)
        if home is None:
            continue
        coll, entity = home
        shown = _shown(base, ref.table, coll.table)
        etype = str(entity.get("entity_type") or "")
        pk_tpl, sk_tpl = str(entity.get("pk_template") or ""), str(entity.get("sk_template") or "")
        where = (
            f"owner group {owner}'s table {coll.name} (entity {etype}, PK {pk_tpl}, SK {sk_tpl})"
        )
        own_cols = _columns(_key_columns(ref.table, base))
        tokens = set(_TOKEN_RE.findall(canon_template(pk_tpl, entity)))
        tokens |= set(_TOKEN_RE.findall(canon_template(sk_tpl, entity)))
        problems: list[str] = []
        for ap in drafts.patterns(ref):
            if not ap.get("gsi_name") and not tokens <= own_cols:
                problems.append(
                    f"group {ref.group}'s access pattern {ap.get('pattern_id')} reads {shown} "
                    f"by its context placeholder {ref.name}'s key "
                    f"({_describe(*_table_key(ref.table))}), which {where} cannot be shown to "
                    "serve: put the pattern on a GSI of the placeholder, or design it on the "
                    "owner's entity key"
                )
        entity_attrs = {str(a.get("name")): a for a in _dicts(entity.get("attributes"))}
        placeholder_attrs = _attributes(ref.table)
        for gsi in _dicts(ref.table.get("gsis")):
            for name in [n for part in _gsi_key_names(gsi) for n in part]:
                attr = placeholder_attrs.get(name)
                if (
                    attr
                    and name in entity_attrs
                    and _direct_column(entity_attrs[name], base) != _direct_column(attr, base)
                ):
                    problems.append(
                        f"group {ref.group}'s context placeholder {ref.name} keys GSI "
                        f"{gsi.get('gsi_name')} on {name}, which entity {etype} of "
                        f"{where} uses for another column"
                    )
        if problems:
            drafts.failures.extend(problems)
            continue
        drafts.settle(base, coll, [ref])
        # Every attribute of the placeholder the entity does not have yet, as rule 4
        # copies them: a pattern of the context group uses each.
        names = {a.get("name") for a in _dicts(entity.get("attributes"))}
        entity["attributes"] = _dicts(entity.get("attributes")) + [
            copy.deepcopy(a)
            for a in _dicts(ref.table.get("attributes"))
            if a.get("name") not in names
        ]
        taken = {str(g.get("gsi_name")): g for g in _dicts(coll.table.get("gsis"))}
        renames: dict[str, str] = {}
        for gsi in _dicts(ref.table.get("gsis")):
            name = str(gsi.get("gsi_name"))
            same = taken.get(name)
            if same is not None and _gsi_signature(same) == _gsi_signature(gsi):
                continue
            if same is not None:
                renames[name] = _unique_gsi_name(name, set(taken))
            new = copy.deepcopy(gsi)
            new["gsi_name"] = renames.get(name, name)
            coll.table.setdefault("gsis", [])
            coll.table["gsis"] = _dicts(coll.table["gsis"]) + [new]
            taken[new["gsi_name"]] = new
        for ap in drafts.patterns(ref):
            if not ap.get("gsi_name"):
                _note(ap, f"in {coll.name} as {etype}: PK {pk_tpl}, SK {sk_tpl}")
        for item in drafts.patterns(ref) + drafts.hot_partitions(ref):
            if item.get("gsi_name") in renames:
                item["gsi_name"] = renames[item["gsi_name"]]
        drafts.notes.append(
            f"{shown}: group {ref.group}'s context placeholder {ref.name} folded into {where}"
            + (f"; GSIs renamed {renames}" if renames else "")
        )
        old, old_name = ref.table, ref.name
        drafts.rename_table(ref, coll.name)
        tables = drafts.drafts[ref.pos].get("table_definitions")
        if isinstance(tables, list):
            drafts.drafts[ref.pos]["table_definitions"] = [t for t in tables if t is not old]
        writes = [ap for ap in drafts.patterns(ref) if ap.get("operation") in _WRITES]
        if writes:
            text = (
                f"{old_name} (group {ref.group}) folded into {coll.name} (group "
                f"{coll.group}) as entity {etype}, PK {pk_tpl}, SK {sk_tpl}"
            )
            folded_writes.setdefault((coll.name, shown), []).append((text, writes))
    _record_folded_writes(drafts, folded_writes)


def _unify_single_keys(
    drafts: _Drafts,
    unique_keys: dict[str, list[frozenset[str]]],
    owners: dict[str, int] | None = None,
) -> None:
    for base, refs in _singles_by_base(drafts).items():
        keys = list(dict.fromkeys(_table_key(r.table) for r in refs))
        if len(keys) < 2:
            continue  # same key names; a type disagreement stays for the merge to report
        types: dict[str, set[str]] = {}
        for r in refs:
            for field_name in ("partition_key", "sort_key"):
                k = r.table.get(field_name)
                if _key_name(k):
                    types.setdefault(str(_key_name(k)), set()).add(_key_type(k))
        if any(len(t) > 1 for t in types.values()):
            continue  # N vs S for the same attribute: never changed silently
        holders = {
            k: min((r for r in refs if _table_key(r.table) == k), key=lambda r: r.name)
            for k in keys
        }
        plans = [_plan(drafts, refs, base, holders[k], unique_keys.get(base, [])) for k in keys]
        allowed = [p for p in plans if p.blocked is None]
        # The split's owner designs these rows (#1243): its key is the one kept. When
        # it is not safe for every group, nothing is rewritten (the merge reports it).
        owner = (owners or {}).get(base)
        owned = {_table_key(r.table) for r in refs if r.group == owner}
        if owned:
            blocked = [p for p in plans if p.key in owned and p.blocked is not None]
            allowed = [p for p in allowed if p.key in owned]
            if not allowed:
                reasons = "; ".join(f"{_describe(*p.key)}: {p.blocked}" for p in blocked)
                text = (
                    f"{_shown(base, *(r.table for r in refs))}: owner group {owner}'s key "
                    f"schema is not safe for every group, so no key is kept ({reasons})"
                )
                drafts.notes.append(text)
                drafts.failures.append(text)
                continue
        if not allowed:
            reasons = "; ".join(f"{_describe(*p.key)}: {p.blocked}" for p in plans)
            drafts.notes.append(f"{base}: key schemas left as a conflict ({reasons})")
            continue
        # Order-independent: most patterns served, then a key holding the source
        # primary key, then the key's attribute names.
        best = sorted(allowed, key=lambda p: (-p.score, not p.contains_primary, str(p.key)))[0]
        winner = holders[best.key]
        drafts.settle(base, winner, [r for r in refs if r is not winner])
        changes = [
            _rekey(drafts, ref, winner, refs, base, best)
            for ref in refs
            if _table_key(ref.table) != best.key
        ]
        _record_rekeys(drafts, base, winner, best, changes)


def _record_rekeys(
    drafts: _Drafts, base: str, winner: _Ref, plan: _KeyPlan, changes: list[_Rekeyed]
) -> None:
    """One trade-off per table when the key change alters behaviour, else a trace note."""
    shown = _shown(base, winner.table, *(c.ref.table for c in changes))
    others = ", ".join(f"{c.ref.name} (group {c.ref.group}; {_describe(*c.old)})" for c in changes)
    summary = (
        f"Design groups gave {shown} different keys: {winner.name} (group {winner.group}; "
        f"{_describe(*plan.key)}), {others}. The merge keeps {_describe(*plan.key)}, which "
        "identifies every row and serves the most of their access patterns directly."
    )
    behaviour = [c for c in changes if c.reads or c.lookups]
    if not behaviour:
        drafts.notes.append(summary + " No access pattern changes how it runs.")
        return
    parts: list[str] = []
    for gsi in dict.fromkeys(c.gsi for c in behaviour if c.gsi):
        mine = [c for c in behaviour if c.gsi == gsi]
        reads = sum(len(c.reads) for c in mine)
        lookups = sum(len(c.lookups) for c in mine)
        text = f"GSI {gsi} ({'new' if any(c.gsi_new for c in mine) else 'reused'})"
        if reads:
            text += f": {reads} read pattern(s) Query it, eventually consistent"
        if lookups:
            text += (
                f"{',' if reads else ':'} {lookups} update/delete pattern(s) first find the "
                "item's key through it, a non-atomic, eventually consistent read-then-write "
                "that can miss an item written just before"
            )
        parts.append(text)
    gsi_cost = [
        g
        for c in behaviour
        if c.gsi_new
        for g in _dicts(c.ref.table.get("gsis"))
        if g.get("gsi_name") == c.gsi
    ]
    cost = "; ".join(
        f"{g['gsi_name']} stores about {g.get('item_count', 0)} items of "
        f"{g.get('item_size_bytes', 0)} bytes ({g.get('projection')}) and takes one extra "
        "write for every table write that sets its keys"
        for g in gsi_cost
    )
    drafts.trade_offs.append(
        {
            "description": summary,
            "impact": "; ".join(parts) + (f". Cost: {cost}" if cost else "") + ".",
            "source_tables": [shown],
            "target_tables": [winner.name],
            "query_ids": list(
                dict.fromkeys(
                    q
                    for c in behaviour
                    for ap in c.reads + c.lookups
                    for q in ap.get("query_ids") or []
                    if isinstance(q, str)
                )
            ),
            "engine": "dynamodb",
        }
    )


# ---------------------------------------------------------------------------
# 4. Single-entity table into an item-collection entity
# ---------------------------------------------------------------------------


@dataclass
class _Fold:
    collection: _Ref
    entity: dict
    sk_prefix: str  # static SK value, or the prefix before the sort column
    new_entity: bool = False


def _template(template: str) -> tuple[str, list[str], str]:
    """(text before the first token, tokens, text after the last token)."""
    tokens = _TOKEN_RE.findall(template)
    head = template.split("{", 1)[0]
    tail = template.rsplit("}", 1)[1] if tokens else ""
    return head, [t.lower() for t in tokens], tail


def _fold_targets(single: _Ref, collections: list[_Ref], base: str) -> list[_Fold]:
    """Item-collection entities ``single``'s rows could become, best first.

    An entity of a collection with ``single``'s own name, then a new entity in
    that collection (see :func:`_new_entity`), then entities of other
    collections: the collection name the most drafts use first, then by
    collection name and entity (never the draft order).
    """
    attrs = _attributes(single.table)
    pk, sk = _table_key(single.table)
    pk_col = _direct_column(attrs.get(pk or ""), base)
    sk_col = _direct_column(attrs.get(sk or ""), base) if sk else None
    if not pk_col or (sk and not sk_col):
        return []
    # The collection's partition key holds the single table's key values, so
    # its type must match: a fold never turns an N key into S (or back).
    pk_type = _key_type(single.table.get("partition_key"))
    collections = [c for c in collections if _key_type(c.table.get("partition_key")) == pk_type]
    same_name = [c for c in collections if c.name == single.name and c.pos != single.pos]
    others = [c for c in collections if c.name != single.name and c.pos != single.pos]
    used = {c.name: len({r.pos for r in collections if r.name == c.name}) for c in collections}

    def rank(fold: _Fold) -> tuple:
        entity = fold.entity
        return (
            -used[fold.collection.name],
            fold.collection.name,
            str(entity.get("entity_type") or ""),
            str(entity.get("sk_template") or ""),
            fold.collection.pos,
        )

    found: list[_Fold] = []
    for group in (same_name, None, others):
        if group is None:
            found += sorted(
                (f for f in (_new_entity(single, c, base, pk_col) for c in same_name) if f),
                key=rank,
            )
            continue
        matches: list[_Fold] = []
        for coll in group:
            for entity in _dicts(coll.table.get("entities")):
                if normalize_table_name(str(entity.get("source_table") or "")) != base:
                    continue
                pk_tpl = canon_template(str(entity.get("pk_template") or ""), entity)
                _, pk_tokens, pk_tail = _template(pk_tpl)
                if pk_tokens != [pk_col] or pk_tail:
                    continue
                sk_tpl = canon_template(str(entity.get("sk_template") or ""), entity)
                sk_head, sk_tokens, sk_tail = _template(sk_tpl)
                static = sk_col is None and not sk_tokens and sk_head
                ranged = sk_col is not None and sk_tokens == [sk_col] and sk_head and not sk_tail
                if static or ranged:
                    matches.append(_Fold(coll, entity, sk_head))
        found += sorted(matches, key=rank)
    return found


def _new_entity(single: _Ref, coll: _Ref, base: str, pk_col: str) -> _Fold | None:
    """A new entity for ``single``'s rows in the item collection of the same name.

    Only when the collection has no entity for those rows yet, ``single`` has
    no sort key, and its partition key has the collection's attribute name and
    type: the two groups named one table and agree on what partitions it.
    """
    if _key_name(single.table.get("sort_key")):
        return None
    pk, coll_pk = single.table.get("partition_key"), coll.table.get("partition_key")
    if _key_name(coll_pk) != _key_name(pk) or _key_type(coll_pk) != _key_type(pk):
        return None
    entities = _dicts(coll.table.get("entities"))
    if any(normalize_table_name(str(e.get("source_table") or "")) == base for e in entities):
        return None
    label = base.upper()
    taken = {str(e.get("entity_type")) for e in entities} | {
        str(e.get("sk_template") or "").split("{", 1)[0] for e in entities
    }
    if label in taken:
        return None
    entity = {
        "entity_type": label,
        "source_table": _shown(base, single.table),
        "pk_template": "{" + pk_col + "}",
        "sk_template": label,
        "attributes": [],
    }
    return _Fold(coll, entity, label, new_entity=True)


FoldedWrites = dict[tuple[str, str], list[tuple[str, list[dict]]]]


def _record_folded_writes(drafts: _Drafts, folded_writes: FoldedWrites) -> None:
    """One trade-off per (collection, source table) that folded writes now share: the
    groups write the same items with different attribute sets (rule 4, and the owner
    fold of #1243)."""
    for (coll_name, shown), items in folded_writes.items():
        drafts.trade_offs.append(
            {
                "description": (
                    f"Design groups modelled {shown} both on its own and inside {coll_name}; "
                    f"the merge keeps one home, {coll_name}: "
                    + "; ".join(t for t, _ in items)
                    + "."
                ),
                "impact": (
                    "Groups now write the same items with different attribute sets: every "
                    "PutItem must carry the union of their attributes, or become an UpdateItem "
                    "of its own attributes, or it overwrites the other group's."
                ),
                "source_tables": [shown],
                "target_tables": [coll_name],
                "query_ids": list(
                    dict.fromkeys(
                        q
                        for _, writes in items
                        for ap in writes
                        for q in ap.get("query_ids") or []
                        if isinstance(q, str)
                    )
                ),
                "engine": "dynamodb",
            }
        )


def _fold_singles(drafts: _Drafts) -> None:
    refs = drafts.refs()
    collections = [r for r in refs if _shape(r.table) == "item_collection"]
    if not collections:
        return
    folded_writes: FoldedWrites = {}
    for single in [r for r in refs if _shape(r.table) == "single"]:
        base = _base_entity(single.table)
        if not _known(base):
            continue
        same_draft_names = {
            str(t.get("table_name"))
            for t in _dicts(drafts.drafts[single.pos].get("table_definitions"))
        }
        fold = next(
            (
                f
                for f in _fold_targets(single, collections, base)
                if f.collection.name == single.name or f.collection.name not in same_draft_names
            ),
            None,
        )
        if fold is None:
            continue
        coll = fold.collection
        drafts.settle(base, coll, [single])
        old_name, old_sk = single.name, _key_name(single.table.get("sort_key"))
        static = not _TOKEN_RE.search(str(fold.entity.get("sk_template") or ""))
        sk_attr = _key_name(coll.table.get("sort_key")) or "SK"
        condition = (
            f"{sk_attr}='{fold.sk_prefix}'"
            if static
            else f"{sk_attr} begins_with '{fold.sk_prefix}'"
        )
        for ap in drafts.patterns(single):
            if ap.get("gsi_name"):
                continue
            text = str(ap.get("key_condition") or "")
            if static or (old_sk and _mentions(text, old_sk) and ap.get("operation") != "Query"):
                exact = f"{sk_attr}='{fold.entity.get('sk_template')}'"
                _note(ap, f"in {coll.name} as {fold.entity.get('entity_type')}: {exact}")
            else:
                _note(ap, f"in {coll.name} as {fold.entity.get('entity_type')}: {condition}")

        entity = copy.deepcopy(fold.entity)
        names = {a.get("name") for a in _dicts(entity.get("attributes"))}
        entity["attributes"] = _dicts(entity.get("attributes")) + [
            copy.deepcopy(a)
            for a in _dicts(single.table.get("attributes"))
            if a.get("name") not in names
        ]
        table = single.table
        slice_table = {
            **{k: v for k, v in table.items() if k not in ("attributes", "sort_key")},
            "table_name": coll.name,
            "aggregate_pattern": "item_collection",
            "partition_key": copy.deepcopy(coll.table.get("partition_key")),
            "sort_key": copy.deepcopy(coll.table.get("sort_key")),
            "entities": [entity],
        }
        table.clear()
        table.update(slice_table)
        single.table = table
        table["table_name"] = old_name
        shown = _shown(base, table, coll.table)
        drafts.rename_table(single, coll.name)
        writes = [ap for ap in drafts.patterns(single) if ap.get("operation") in _WRITES]
        text = (
            f"{old_name} (group {single.group}) folded into {coll.name} (group "
            f"{coll.group}) as entity {fold.entity.get('entity_type')}, PK "
            f"{fold.entity.get('pk_template')}, SK {fold.entity.get('sk_template')}"
        )
        if writes and not fold.new_entity:
            folded_writes.setdefault((coll.name, shown), []).append((text, writes))
        else:
            drafts.notes.append(f"{shown}: {text}")
    _record_folded_writes(drafts, folded_writes)


# ---------------------------------------------------------------------------
# 5. GSI names in tables that will merge
# ---------------------------------------------------------------------------


def _merge_clusters(refs: list[_Ref]) -> list[list[_Ref]]:
    """Tables from different drafts the merge will try to combine (same name or rows)."""
    parent = list(range(len(refs)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(refs):
        for j in range(i + 1, len(refs)):
            b = refs[j]
            if a.pos == b.pos:
                continue
            same_rows = (
                _shape(a.table) == _shape(b.table) == "single"
                and _base_entity(a.table) == _base_entity(b.table)
                and _known(_base_entity(a.table))
                and _table_key(a.table) == _table_key(b.table)
            )
            same_collection = _shape(a.table) == _shape(b.table) == "item_collection" and (
                _same_collection(a.table, b.table)
            )
            if a.name == b.name or same_rows or same_collection:
                parent[find(j)] = find(i)
    clusters: dict[int, list[_Ref]] = {}
    for i, ref in enumerate(refs):
        clusters.setdefault(find(i), []).append(ref)
    return [c for c in clusters.values() if len({r.pos for r in c}) > 1]


def _reconcile_gsi_names(drafts: _Drafts) -> None:
    """Rename a GSI whose name another table in its cluster uses for other keys.

    The keys most of the cluster's tables give that name keep it (then fewer
    key attributes, then the keys' text; never the draft order); every other
    GSI of that name is renamed ``<name>_<its key attributes>`` (for example
    ``OptionsByAutoload_AutoloadOptionId``), so no group number reaches a
    GSI name.
    """
    for cluster in _merge_clusters(drafts.refs()):
        votes: dict[str, dict[tuple, int]] = {}
        for ref in cluster:
            for gsi in _dicts(ref.table.get("gsis")):
                sigs = votes.setdefault(str(gsi.get("gsi_name") or ""), {})
                sig = _gsi_signature(gsi)
                sigs[sig] = sigs.get(sig, 0) + 1
        keeps = {
            name: sorted(sigs.items(), key=lambda s: (-s[1], sum(map(len, s[0])), repr(s[0])))[0][0]
            for name, sigs in votes.items()
            if len(sigs) > 1
        }
        taken = set(votes)
        for ref in cluster:
            for gsi in list(_dicts(ref.table.get("gsis"))):
                name = str(gsi.get("gsi_name") or "")
                if name not in keeps or _gsi_signature(gsi) == keeps[name]:
                    continue
                pk_names, sk_names = _gsi_key_names(gsi)
                suffix = "".join(_pascal(n) for n in (*pk_names, *sk_names)) or "Keys"
                new = _unique_gsi_name(f"{name}_{suffix}", taken)
                taken.add(new)
                drafts.rename_gsi(ref, name, new)
                drafts.notes.append(
                    f"{ref.name}: group {ref.group}'s GSI {name} renamed {new} (another "
                    "group's GSI of that name has other keys)"
                )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


UniqueKeys = dict[str, list[frozenset[str]]]
"""Normalised source table -> its unique column sets, the primary key first."""


def unique_keys_from_collector(collector_output: Any) -> UniqueKeys:
    """Primary keys and unique (non-partial) indexes on NOT NULL columns of the collector's tables."""
    result: UniqueKeys = {}
    schema = collector_output.get("database_schema") if isinstance(collector_output, dict) else None
    for table in _dicts((schema or {}).get("tables")):
        name = normalize_table_name(str(table.get("table_id") or table.get("table_name") or ""))
        if not _known(name):
            continue
        # A unique index on a nullable column proves nothing: SQL allows many
        # NULLs under it, and DynamoDB cannot store an item with no key value.
        nullable = {
            str(c.get("column_name")).lower()
            for c in _dicts(table.get("columns"))
            if c.get("nullable")
        }
        sets: list[frozenset[str]] = []
        primary = [c for c in table.get("primary_key") or [] if isinstance(c, str)]
        if primary:
            sets.append(frozenset(c.lower() for c in primary))
        for index in _dicts(table.get("indexes")):
            columns = [c for c in index.get("columns") or [] if isinstance(c, str)]
            if (
                columns
                and (index.get("is_unique") or index.get("is_primary"))
                and not index.get("predicate")
            ):
                key = frozenset(c.lower() for c in columns)
                if index.get("is_primary") and not primary:
                    sets.insert(0, key)
                elif key not in sets:
                    sets.append(key)
        sets = [u for u in sets if not u & nullable]
        if sets:
            result[name] = sets
    return result


def reconcile_shared_tables(
    drafts: list[dict],
    group_indices: list[int],
    unique_keys: UniqueKeys | None = None,
    notes: list[str] | None = None,
    column_types: ColumnTypes | None = None,
    owners: dict[str, int] | None = None,
    failures: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Rewrite group drafts so the merge sees compatible shared tables.

    Returns new drafts (the inputs are not modified) and the trade-offs for the
    changes that alter how an access pattern runs. Mechanical changes (names,
    labels, folds without writes) are appended to ``notes`` when given, for
    the merge trace. ``unique_keys`` (from :func:`unique_keys_from_collector`)
    lets a key that holds a source unique key be kept even when another
    group's key has other columns. ``owners`` (normalised source table -> owning
    group index, from the split's manifest, #1243) points context placeholders at
    the owner's table and keeps the owner's key schema (rule 3); what that cannot
    reconcile is appended to ``failures`` when given. See the module docstring for
    the rules.
    """
    state = _Drafts(copy.deepcopy(drafts), list(group_indices))
    if len(drafts) < 2:
        return state.drafts, []
    _point_context_tables(state, owners or {})
    _align_single_names(state, owners or {})
    _align_pk_types(state, column_types or {})
    _align_collections(state)
    _unify_single_keys(state, unique_keys or {}, owners or {})
    _fold_singles(state)
    _fold_into_owner_collections(state, owners or {})
    _align_collections(state)
    _reconcile_gsi_names(state)
    _flush_notes(state.drafts)
    if notes is not None:
        notes.extend(state.notes)
    if failures is not None:
        failures.extend(state.failures)
    return state.drafts, state.trade_offs


def dedupe_gsis(merged: dict, notes: list[str] | None = None) -> None:
    """Keep one GSI per key schema on each merged table, widening its projection.

    Access patterns and hot-partition entries on a dropped duplicate are pointed
    at the GSI that stays.
    """
    for table in _dicts(merged.get("table_definitions")):
        kept: dict[tuple, dict] = {}
        renames: dict[str, str] = {}
        gsis: list[dict] = []
        for gsi in _dicts(table.get("gsis")):
            sig = _gsi_signature(gsi)
            first = kept.get(sig)
            if first is None:
                kept[sig] = gsi
                gsis.append(gsi)
                continue
            _widen(
                first, str(gsi.get("projection") or "KEYS_ONLY"), gsi.get("projected_attributes")
            )
            renames[str(gsi.get("gsi_name"))] = str(first.get("gsi_name"))
        if not renames:
            continue
        table["gsis"] = gsis
        name = table.get("table_name")
        for list_field in ("access_patterns", "hot_partition_analysis"):
            for item in _dicts(merged.get(list_field)):
                if item.get("table_name") == name and item.get("gsi_name") in renames:
                    item["gsi_name"] = renames[item["gsi_name"]]
        if notes is not None:
            notes.extend(
                f"{name}: GSI {old} has the same keys as {new}; kept {new}"
                for old, new in renames.items()
            )


def name_length_failures(tables: list[dict], prefix: str) -> list[str]:
    """A failure for each table or GSI name longer than DynamoDB accepts (#866)."""
    failures: list[str] = []
    for table in tables:
        name = str(table.get("table_name") or "")
        if len(name) > NAME_LIMIT:
            failures.append(
                f"{prefix}table name '{name[:40]}...' is {len(name)} characters, over the "
                f"DynamoDB limit of {NAME_LIMIT}. Use a shorter table name."
            )
        for gsi in _dicts(table.get("gsis")):
            gsi_name = str(gsi.get("gsi_name") or "")
            if len(gsi_name) > NAME_LIMIT:
                failures.append(
                    f"{prefix}GSI name '{gsi_name[:40]}...' on table '{name[:40]}' is "
                    f"{len(gsi_name)} characters, over the DynamoDB limit of {NAME_LIMIT}. "
                    "Use a shorter GSI name."
                )
    return failures


def gsi_limit_failures(tables: list[dict], prefix: str) -> list[str]:
    """A failure for each merged table with more GSIs than DynamoDB allows by default."""
    return [
        f"{prefix}table '{t.get('table_name')}' has {len(_dicts(t.get('gsis')))} GSIs after "
        f"the merge, over the DynamoDB default limit of {GSI_LIMIT}. Drop or combine GSIs, or "
        "move some access patterns to another table."
        for t in tables
        if len(_dicts(t.get("gsis"))) > GSI_LIMIT
    ]
