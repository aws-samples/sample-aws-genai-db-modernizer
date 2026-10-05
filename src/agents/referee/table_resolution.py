"""Canonical table/view name resolution against the collector's schema (#316, #225).

A name the SQL parser puts in a query's ``tables_accessed``/``source_tables``
is not always spelled the way the collector's own schema enumeration spells
``table_id``:

- live MySQL: ``table_id`` is schema-qualified (``wordpress.wp_posts``), but
  the SQL parser emits the bare table name (``wp_posts``);
- live PostgreSQL: ``table_id`` is schema-qualified (``public.topics``), but
  the parser strips the ``public.`` prefix (``topics``);
- offline PostgreSQL parsing keeps a ``public.`` prefix the collector's own
  ``table_id`` does not use (``discourse.topics``);
- quoting and case can differ too (``` `WP_USERS` ```, ``"wp_users"``).

Matching by exact string equality against ``table_id`` drops every real
table under any of those conditions. :class:`TableNameResolver` is the single
place that resolves a parsed name to the canonical ``table_id``/``view_id``
the collector actually assigned, so assignment resolution
(``assignment_resolver.derive_table_assignments``, #316), the migration-waves
filter and synthesis's own ``known_tables`` check (both #225) agree on what a
"known table" is.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from src.agents.schema_design.scope import normalize_table_name

# Pseudo "tables" a parser or the dialect itself introduces (MySQL's dummy
# ``DUAL`` target, a column or expression a parser mistook for a table). They
# are not source tables -- real or misspelled -- so they are silently dropped
# wherever table names are resolved or reported, never counted as unresolved
# noise and never claimed as a table a wave moves. Single source of truth:
# previously duplicated between ``migration_waves`` and ``synthesis_handler``.
PSEUDO_TABLES = frozenset({"DUAL", "unknown"})


class TableNameResolver:
    """Resolves a parsed name to the collector's canonical ``table_id``/``view_id``.

    A name resolves, in order, against:

    1. an exact match on a known ``table_id``/``view_id``;
    2. an exact match on a table's/view's bare ``table_name``/``view_name``;
    3. a match on :func:`normalize_table_name` (unquoted, lower-case, no
       schema/db prefix) of either of the above.

    Steps 2 and 3 are accepted only when exactly one table or view matches --
    an ambiguous name (two different tables that happen to share a bare name
    or normalize to the same key) is left unresolved rather than silently
    mapped to the wrong table.
    """

    def __init__(
        self,
        canonical_ids: frozenset[str],
        by_name: Mapping[str, frozenset[str]],
        by_normalized: Mapping[str, frozenset[str]],
    ):
        self._canonical_ids = canonical_ids
        self._by_name = by_name
        self._by_normalized = by_normalized

    @classmethod
    def from_collector(cls, collector_output: Mapping) -> TableNameResolver | None:
        """Build a resolver from ``collector_output``, or ``None`` with no schema.

        ``None`` when the collector schema is missing or has no table or view
        at all, so callers know to skip resolution and keep every name, the
        pre-#316 behavior, rather than treat every name as unresolved against
        a schema known to be empty or absent.
        """
        schema = collector_output.get("database_schema") or {}
        canonical_ids: set[str] = set()
        by_name: dict[str, set[str]] = defaultdict(set)
        by_normalized: dict[str, set[str]] = defaultdict(set)

        def add(rows, id_field: str, name_field: str) -> None:
            for row in rows or []:
                canonical = row.get(id_field)
                if not canonical:
                    continue
                canonical = str(canonical)
                canonical_ids.add(canonical)
                by_normalized[normalize_table_name(canonical)].add(canonical)
                bare = row.get(name_field)
                if bare:
                    bare = str(bare)
                    by_name[bare].add(canonical)
                    by_normalized[normalize_table_name(bare)].add(canonical)

        add(schema.get("tables"), "table_id", "table_name")
        add(schema.get("views"), "view_id", "view_name")

        if not canonical_ids:
            return None

        return cls(
            frozenset(canonical_ids),
            {name: frozenset(ids) for name, ids in by_name.items()},
            {key: frozenset(ids) for key, ids in by_normalized.items()},
        )

    @classmethod
    def from_known_ids(cls, known_ids: Iterable[str]) -> TableNameResolver | None:
        """Build a resolver from a flat set of already-canonical ids.

        For a caller that only has the reduced ``known_tables`` set (table and
        view ids, no row dicts with a bare name alongside), such as the
        migration-waves builder: resolution falls back to
        :func:`normalize_table_name` only (no ``table_name``/``view_name``
        step, since no bare name is available here). ``None`` with an empty
        ``known_ids``, same convention as :meth:`from_collector`.
        """
        canonical_ids = {str(i) for i in known_ids}
        if not canonical_ids:
            return None
        by_normalized: dict[str, set[str]] = defaultdict(set)
        for cid in canonical_ids:
            by_normalized[normalize_table_name(cid)].add(cid)
        return cls(
            frozenset(canonical_ids),
            {},
            {key: frozenset(ids) for key, ids in by_normalized.items()},
        )

    def resolve(self, name: str) -> str | None:
        """The canonical ``table_id``/``view_id`` for ``name``, or ``None`` if unresolved."""
        if name in self._canonical_ids:
            return name
        candidates = self._by_name.get(name)
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        candidates = self._by_normalized.get(normalize_table_name(name))
        if candidates and len(candidates) == 1:
            return next(iter(candidates))
        return None

    def known_ids(self) -> frozenset[str]:
        """Every canonical ``table_id``/``view_id`` this resolver knows about."""
        return self._canonical_ids
