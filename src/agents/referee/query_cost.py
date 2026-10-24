"""Each query's own cost dimension on each engine (ADR-031 Decision 5, #494).

The cost dimension of a classified query's score on an engine is that query's own
estimated monthly cost there, from the engine file's ``[pricing]`` inputs
(``src/catalog/engines/<engine>.toml``, read through ``src.shared.catalog``), never its
tables' cost band (``src.tools.analysis.scoring._cost``, which looks at no engine and
sums a table's traffic over every query on it). It has two parts:

- **The standing share.** The engine's standing cost (``base_monthly``) shared equally
  over the queries of the workload it could serve: the ones its hard exclusion and
  capability gates let through and it may own (ADR-031 Decision 2 steps 2 and 3). A
  model's flag does not change the count (Decision 11: a run without a model scores
  every other query the same). The count is the only thing another query contributes
  (its shape can change the count: a write, or a gated query); its call rate and size
  never reach this query's cost.
- **The variable cost.** The query's own call rate (``calls_per_second``) times the
  units each call is billed for, at the engine's per-request prices
  (``read_unit_price_per_million`` and ``read_unit_bytes`` for a read,
  ``write_unit_*`` for a write). A call is billed for the bytes it reads or writes:
  its rows (``rows_returned_avg`` for a read, ``rows_affected_avg`` for a write, at
  least one) times the width of a row of its tables (each table's ``size_mb`` over its
  ``row_count``, summed over the query's tables), rounded up to whole units, at least
  one unit. An engine whose reads are billed only on a buffer-cache miss
  (``reads_billed_on_cache_miss_only``) pays for the share the source's own cache did
  not serve: the query's own ``cache_hit_ratio_pct``, else the collection's
  instance-wide buffer-cache hit ratio (``metrics.source_host``, #1197) scaled down by
  the engine's estimate instance memory over the source's, else none (every read billed);
  at least 1% of every read is billed as a miss (:mod:`src.shared.source_cache`). An
  engine billed by capacity has no per-request
  price, so its variable cost is 0: the capacity is what its standing cost pays for.
  Scaling that capacity with load is not modelled.

The monthly total maps to a 0-100 score with the engine's own per-query cut-off
(``max_burden_per_unique_query``, the burden per unique query above which the reality
check finds an engine not worth running): ``100 * cut_off / (cut_off + monthly)``, so a
query costing nothing scores 100, one costing the cut-off scores 50, and a $300 standing
cost carried by one query scores 25. The four dimensions and their weights are
unchanged (Decision 5).

This module names no engine: every number comes from an engine file.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from src.agents.referee.cache_overlay import is_write_query
from src.shared import catalog
from src.shared.source_cache import CollectionCache, read_hit_ratio
from src.tools.sql.query_facts import source_engine_of

# The month the per-request prices are multiplied over: 30 days, the routing model's own
# (the cost estimates bill a 730-hour month since #1208; one month for both is #1216).
SECONDS_PER_MONTH = 30 * 24 * 3600
_BYTES_PER_MB = 1024 * 1024


def _number(value: object) -> float:
    try:
        number = float(value or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def row_widths(collector_output: Mapping | None) -> dict[str, float]:
    """``table_id -> average bytes a row`` for every collected table with a size and rows."""
    tables = ((collector_output or {}).get("database_schema") or {}).get("tables") or []
    widths: dict[str, float] = {}
    for table in tables:
        table_id = table.get("table_id")
        rows = _number(table.get("row_count"))
        size = _number(table.get("size_mb"))
        if table_id and rows and size:
            widths[str(table_id)] = size * _BYTES_PER_MB / rows
    return widths


@dataclass(frozen=True)
class QueryCost:
    """One query's estimated monthly cost on one engine (USD)."""

    standing_share: float
    variable: float

    @property
    def monthly(self) -> float:
        return self.standing_share + self.variable


@dataclass(frozen=True)
class QueryCostModel:
    """The per-query cost dimension for one collection (see the module docstring).

    ``workload``: the collection's queries, over which each engine's standing cost is
    shared. ``widths``: ``table_id -> average bytes a row``. The bare default has no
    workload and no widths: each scored query then carries the whole standing cost and
    is billed one unit a call.
    """

    workload: tuple[Mapping, ...] = ()
    widths: Mapping[str, float] = field(default_factory=dict)
    # The dialect the queries' text is read in (the write check, #1144).
    source_engine: str = field(kw_only=True)
    # The collection's instance-wide buffer-cache hit ratio and the source's memory, for
    # a read that reports none of its own (#1197); None when the collection reported none.
    collection_cache: CollectionCache | None = field(default=None, kw_only=True)
    # engine -> the workload's query ids it could serve, filled the first time it is asked.
    _servable: dict[str, frozenset[str]] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_collector(cls, collector_output: Mapping | None) -> QueryCostModel:
        queries = ((collector_output or {}).get("queries") or {}).get("query_patterns") or []
        return cls(
            workload=tuple(queries),
            widths=row_widths(collector_output),
            source_engine=source_engine_of(collector_output),
            collection_cache=CollectionCache.from_collector(collector_output),
        )

    def workload_ids(self) -> tuple[str, ...]:
        return tuple(str(q.get("query_id") or "") for q in self.workload)

    def servable(self, engine: str, serves: Callable[[Mapping], bool]) -> frozenset[str]:
        """The workload's query ids ``engine`` could serve (``serves`` is the gate)."""
        found = self._servable.get(engine)
        if found is None:
            found = frozenset(str(q.get("query_id") or "") for q in self.workload if serves(q))
            self._servable[engine] = found
        return found

    def variable_monthly(self, query: Mapping, engine: str) -> float:
        """``query``'s monthly cost on ``engine`` from its own call rate and size."""
        prices = catalog.pricing(engine)
        write = is_write_query(query, self.source_engine)
        if write:
            price, unit_bytes = prices.write_unit_price_per_million, prices.write_unit_bytes
        else:
            price, unit_bytes = prices.read_unit_price_per_million, prices.read_unit_bytes
        calls = _number(query.get("calls_per_second"))
        if not price or not unit_bytes or not calls:
            return 0.0
        rows = max(1.0, _number(query.get("rows_affected_avg" if write else "rows_returned_avg")))
        width = sum(self.widths.get(str(t), 0.0) for t in query.get("tables_accessed") or [])
        units = max(1, math.ceil(rows * width / unit_bytes - 1e-9))
        billed = 1.0
        if not write and prices.reads_billed_on_cache_miss_only:
            hit, _basis = read_hit_ratio(
                query, self.collection_cache, prices.estimate_instance_memory_gb
            )
            billed = 1.0 - hit / 100.0
        return calls * SECONDS_PER_MONTH * units * billed * price / 1_000_000

    def cost(self, query: Mapping, engine: str, servable: frozenset[str]) -> QueryCost:
        """``query``'s cost on ``engine``, sharing the standing cost over ``servable``
        (the query itself counts once, whether or not the workload lists it)."""
        qid = str(query.get("query_id") or "")
        sharers = len(servable | {qid})
        return QueryCost(
            standing_share=catalog.pricing(engine).base_monthly / sharers,
            variable=self.variable_monthly(query, engine),
        )

    def score(self, query: Mapping, engine: str, servable: frozenset[str]) -> int:
        """``query``'s cost dimension on ``engine`` (0-100)."""
        return self.score_for_monthly(self.cost(query, engine, servable).monthly, engine)

    @staticmethod
    def score_for_monthly(monthly: float, engine: str) -> int:
        """A monthly cost as a 0-100 cost score (see the module docstring)."""
        cut_off = catalog.pricing(engine).max_burden_per_unique_query
        monthly = max(0.0, monthly)
        if cut_off <= 0:
            return 100 if monthly == 0 else 0
        return int(round(100 * cut_off / (cut_off + monthly)))
