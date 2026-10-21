"""Query-text facts for the live collectors, read through the SQL facade (#795).

The offline parser reads every query's table references and its
``has_joins``/``join_count``/``has_aggregations``/``has_subqueries`` facts from
:func:`src.tools.sql.facade.read_sql` (#553, #796). The live collectors
(``mysql_tools``, also used by ``mariadb_tools``, ``postgres_tools``,
``sqlserver_tools``, ``oracle_tools`` and the two ``rds_tools`` call sites)
read them here, from the same facade in their own engine's dialect, so a live
and an offline collection of the same query text agree. Their ``has_text_search``
and ``text_search_type`` are the ``text_search`` query fact and its parts
(:mod:`src.tools.sql.query_facts`, #929), the definition triage and the cache
overlay read next to them. The facade's regex
fallback is the only regex scan left; a collection logs how many of its
queries used it, as the offline parser does.

Each module keeps its own spelling of a table name (the shape it returned
before), so its downstream consumers see no format change:

- a reference is spelled as written (``[catalog.][schema.]name``, quotes
  removed), the way the offline parser keeps a name it can't resolve;
- PostgreSQL drops a ``public.`` qualifier (``public.topics`` is ``topics``);
- Oracle lower-cases the name;
- references into a system catalog (``sys``, ``information_schema``) are
  skipped, the offline parser's rule.

Resolving a name to a collection table id (``<database>.<table>``) stays with
the pipeline (``TableNameResolver``); a live collector reads its queries
before it has a table list to resolve against.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from src.tools.sql import facade
from src.tools.sql.facade import SqlReading, TableRef, TableReferences
from src.tools.sql.query_facts import query_facts, remember_stored_facts
from src.tools.sql.query_tags import QueryTags
from src.tools.sql.stored_facts import stored_reading

logger = logging.getLogger(__name__)

_SYSTEM_CATALOGS = frozenset({"sys", "information_schema"})
_POSTGRES_ENGINES = frozenset({"postgresql", "postgres"})


def read(sql: str, source_engine: str, query_id: str = "", *, log_trace: bool = True) -> SqlReading:
    """The facade's reading of ``sql``; the regex reading if the facade raises.

    The facade never raises by contract. If it does anyway the regex answers
    are used, labelled ``fallback_reason="facade_error"``, so a parser bug
    can't fail a collection. A warning with the stack trace is logged when
    ``log_trace`` (a :class:`LiveQueryReader` logs it once per collection and
    counts the rest in its summary line).
    """
    try:
        return facade.read_sql(sql or "", source_engine)
    except Exception:  # noqa: BLE001 -- the regex answer keeps the collection going
        if log_trace:
            logger.warning(
                "live collector: sql facade failed on query %s, using the regex scans",
                query_id or "?",
                exc_info=True,
            )
        return facade.regex_reading(sql or "", source_engine, "facade_error")


def table_names(refs: TableReferences, source_engine: str) -> list[str]:
    """The live spelling of each table reference, system catalogs skipped,
    duplicates collapsed in first-seen order."""
    names: list[str] = []
    for ref in refs.tables:
        if _is_system_catalog(ref):
            continue
        names.append(_live_name(ref, source_engine))
    return list(dict.fromkeys(names))


def extract_tables(
    sql: str, source_engine: str, empty: tuple[str, ...] = ("unknown",)
) -> list[str]:
    """The live ``tables_accessed`` for ``sql``: :func:`table_names`, or
    ``empty`` when the text names no table."""
    return table_names(read(sql, source_engine).tables, source_engine) or list(empty)


def _is_system_catalog(ref: TableRef) -> bool:
    if ref.qualifiers and ref.qualifiers[-1].lower() in _SYSTEM_CATALOGS:
        return True
    return ref.name.lower() in _SYSTEM_CATALOGS


def _live_name(ref: TableRef, source_engine: str) -> str:
    engine = (source_engine or "").strip().lower()
    if engine in _POSTGRES_ENGINES and not ref.catalog and (ref.schema or "").lower() == "public":
        return ref.name
    if engine == "oracle":
        return ref.written.lower()
    return ref.written


class LiveQueryReader:
    """Reads one live collection's query texts through the facade.

    ``fields`` returns the query-text keys of one query pattern (the text-search ones
    included, #929, and the leading comment tags, #1180); ``log_fallbacks``
    logs, once per collection, how many queries used the regex fallback. A
    facade error logs its stack trace for the first query it hits only.
    """

    def __init__(self, source_engine: str, collector: str, empty: tuple[str, ...] = ("unknown",)):
        self.source_engine = source_engine
        self.collector = collector
        self.empty = empty
        self._read = 0
        self._fallbacks: Counter[str] = Counter()

    def fields(self, query_id: str, sql: str) -> dict[str, Any]:
        first_error = self._fallbacks["facade_error"] == 0
        reading = read(sql, self.source_engine, query_id, log_trace=first_error)
        self._read += 1
        if reading.tables.fallback_reason:
            self._fallbacks[reading.tables.fallback_reason] += 1
        facts = reading.facts
        stored = stored_reading(sql, self.source_engine, reading)
        # The text-search fact from this same reading, not a second parse (#934).
        remember_stored_facts(sql, self.source_engine, stored)
        searches = query_facts(sql, self.source_engine)
        return {
            "tables_accessed": table_names(reading.tables, self.source_engine) or list(self.empty),
            "has_joins": facts.has_joins,
            "join_count": facts.join_count,
            "has_aggregations": facts.has_aggregation,
            "has_subqueries": facts.has_subquery,
            "has_text_search": searches.text_search,
            "text_search_type": searches.text_search_type(),
            "parsed_facts": stored,
            # The leading comment tags, read as the offline parser reads them (#1180).
            **QueryTags.of([sql], self.source_engine).fields(),
        }

    def log_fallbacks(self) -> None:
        if not self._fallbacks:
            return
        logger.info(
            "%s: table references: %d/%d queries used the regex fallback (%s)",
            self.collector,
            sum(self._fallbacks.values()),
            self._read,
            ", ".join(f"{reason}={n}" for reason, n in sorted(self._fallbacks.items())),
        )
