# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How a query reaches its rows by key, an engine-neutral fact (#920, catalog PR B).

Key-value and wide-column engines serve a read only through a key: a ``GetItem`` on
the full primary key, a ``Query`` on a partition key value with an optional range on
the sort key, a key-anchored item collection. Several detectors used to approximate
that with the collector's ``filter_columns`` (empty on every bundled sample query), a
range plus any ``ORDER BY``, or a count of joins. This module reads it once, from the
access-pattern classifier's facts (:func:`src.agents.referee.access_pattern.
query_access`: the class, and per scope the pinned, ranged and joined columns), plus
three facts about each table's own workload in the collection. It reports facts, never
a score; the pattern catalog turns them into each engine's score (ADR-031 Decision 8).

Per query (:class:`KeyAccess`):

- ``key_lookup_read``: a SELECT whose class is a key lookup (one row, or a batch of
  rows, by a full primary or unique key);
- ``keyed``: the class is a key lookup or a key-scoped read (rows a key value
  addresses);
- ``anchored_join``: ``keyed`` and some scope joins two or more tables, every one
  reached by key from a pinned one (Amendment 5's key-anchored join);
- ``pins_value``: some scope pins a column to a value (no partition key value is
  possible without one, whatever the key design);
- ``partition_range``: some scope reads one table, pins a key column of it to a value
  and bounds another of its columns by a range (the facade's ``ranges``): the shape of
  a partition-key equality plus a sort-key range. Defined once, by the classifier
  (``AccessClassification.facts``), which the class scores read too.

Per table (:class:`KeyAccessReader`): ``is_junction`` (two or more foreign keys and a
composite primary key), ``append_only`` (the workload writes it, only with INSERTs)
and ``read_mostly`` (its read calls are at least ``READ_MOSTLY_RATIO`` times its write
calls).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from src.agents.referee.access_pattern import KEY_LOOKUP, KEY_SCOPED, SchemaKeys, query_access

# Reads per write at or above which a table is read-mostly (reference data written
# rarely, the metadata/config shape).
READ_MOSTLY_RATIO = 10.0

_KEYED_CLASSES = frozenset({KEY_LOOKUP, KEY_SCOPED})
_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"})


def is_junction_table(table: Mapping) -> bool:
    """Two or more foreign keys and a composite primary key: a many-to-many link."""
    fks = table.get("foreign_keys") or []
    pk = table.get("primary_key") or []
    return len(fks) >= 2 and len(pk) >= 2


@dataclass(frozen=True)
class KeyAccess:
    """How one query reaches its rows by key (see the module docstring)."""

    access_class: str
    key_lookup_read: bool
    keyed: bool
    anchored_join: bool
    partition_range: bool
    pins_value: bool = False
    tables: frozenset[str] = field(default_factory=frozenset)


@dataclass
class _TableWorkload:
    reads: float = 0.0
    writes: float = 0.0
    write_kinds: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class KeyAccessReader:
    """The schema keys and each table's workload facts for one collection."""

    keys: SchemaKeys
    junctions: frozenset[str]
    workload: Mapping[str, _TableWorkload]
    source_engine: str = ""

    @classmethod
    def from_collector(cls, collector_output: Mapping, source_engine: str = "") -> KeyAccessReader:
        schema = collector_output.get("database_schema") or {}
        junctions = frozenset(
            str(t.get("table_id") or "") for t in schema.get("tables") or [] if is_junction_table(t)
        )
        workload: dict[str, _TableWorkload] = {}
        queries = (collector_output.get("queries") or {}).get("query_patterns") or []
        for q in queries:
            kind = str(q.get("query_type") or "").upper()
            cps = float(q.get("calls_per_second") or 0.0)
            for tid in q.get("tables_accessed") or []:
                facts = workload.setdefault(tid, _TableWorkload())
                if kind in _WRITES:
                    facts.writes += cps
                    facts.write_kinds.add(kind)
                elif kind == "SELECT":
                    facts.reads += cps
        return cls(
            keys=SchemaKeys.from_collector(collector_output),
            junctions=junctions,
            workload=workload,
            source_engine=source_engine,
        )

    def access(self, query: Mapping) -> KeyAccess:
        read = query_access(query, self.keys, self.source_engine)
        name = read.classification.name
        keyed = name in _KEYED_CLASSES
        select = str(query.get("query_type") or "").upper() == "SELECT"
        anchored_join = keyed and any(len(s.tables) >= 2 and s.joined for s in read.scopes)
        partition_range = "partition_range" in read.classification.facts
        return KeyAccess(
            access_class=name,
            key_lookup_read=select and name == KEY_LOOKUP,
            keyed=keyed,
            anchored_join=anchored_join,
            partition_range=partition_range,
            pins_value=any(s.pinned for s in read.scopes),
            tables=frozenset(t for s in read.scopes for t in s.tables),
        )

    def is_junction(self, table_id: str) -> bool:
        return table_id in self.junctions

    def append_only(self, table_id: str) -> bool:
        kinds = self.workload.get(table_id, _TableWorkload()).write_kinds
        return kinds == {"INSERT"}

    def read_mostly(self, table_id: str) -> bool:
        facts = self.workload.get(table_id, _TableWorkload())
        return facts.reads >= READ_MOSTLY_RATIO * facts.writes and facts.reads > 0
