"""Deterministic schema-design arithmetic (#626).

The schema-design skills used to ask the model to compute ``design_rps``, row
and document sizes, OpenSearch shard counts, the query coverage ratio and the
DynamoDB hot-partition utilization, and the PE reviewers re-checked the same
numbers by hand, so one arithmetic slip could cost a review round-trip. These
functions compute them once:

* :func:`build_design_metrics` turns the projected input (collector tables and
  query patterns, in the full-contract or the ``Agent*`` projection shape: both
  use the same field names) into a :class:`DesignMetricsContract`. Every input
  path carries it as ``design_metrics``: the Bedrock ``load_agent_input``
  results, the external-mode request (``prepare_schema_design_input``) and the
  per-group inputs (``build_group_input``).
* :func:`check_design_numbers` compares a design against those numbers and
  returns one message per disagreement. Only checks that are cheap and
  unambiguous are made: a pattern's ``design_rps`` (the sum over its
  ``query_ids``), a DynamoDB hot-partition entry's ``partition_limit``,
  ``utilization_pct`` and ``at_risk``, a DocumentDB collection's document sizes
  against its own embedded array lengths, and an OpenSearch index's or data
  stream's primary shard count.

No model, network or Strands dependency.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.contracts.design_metrics import (
    DEFAULT_GROWTH_MULTIPLIER,
    DEFAULT_PEAK_TO_AVG_RATIO,
    CoverageMetrics,
    DesignMetricsContract,
    DynamoDBPartitionLimits,
    EmbeddingSizeMetrics,
    OpenSearchShardMetrics,
    OpenSearchShardRules,
    QueryDesignMetrics,
    TableDesignMetrics,
)

#: Engines whose designers read ``design_metrics``.
METRIC_ENGINES = frozenset({"dynamodb", "documentdb", "opensearch"})

BYTES_PER_MB = 1_048_576
#: Stand-in for an unknown ``size_mb`` (the skills' long-standing rule).
UNKNOWN_SIZE_MB = 0.01
#: Value size of a string-like column with no ``max_length``.
DEFAULT_STRING_BYTES = 50
#: Value size cap for a string-like column whose ``max_length`` is larger
#: (TEXT/LONGTEXT/BLOB report up to 4 GB): its stored values are not that size,
#: and the measured ``avg_row_size_bytes`` is the better figure for such a table.
LARGE_STRING_BYTES = 1024
_NUMERIC_TYPES = frozenset({"integer", "decimal", "floating_point"})

#: ``design_rps`` agrees when within 1% or 0.01 RPS (models round to 2 places).
RPS_REL_TOLERANCE = 0.01
RPS_ABS_TOLERANCE = 0.01
#: ``utilization_pct`` agrees within half a percentage point.
UTILIZATION_TOLERANCE_PCT = 0.5
#: Document sizes disagree only when off by more than 10% *and* 1 KB, so a few
#: denormalized lookup fields the arithmetic does not see never fail a design.
DOC_SIZE_REL_TOLERANCE = 0.10
DOC_SIZE_ABS_TOLERANCE_KB = 1.0


# ---------------------------------------------------------------------------
# Building the metrics
# ---------------------------------------------------------------------------


def base_rps(calls_per_second: float | None, frequency_per_hour: float | None) -> float:
    """The measured rate: ``calls_per_second``, else ``frequency_per_hour / 3600``, else 0."""
    if calls_per_second is not None:
        return float(calls_per_second)
    if frequency_per_hour:
        return float(frequency_per_hour) / 3600.0
    return 0.0


def design_rps(
    calls_per_second: float | None,
    frequency_per_hour: float | None,
    peak_to_avg_ratio: float = DEFAULT_PEAK_TO_AVG_RATIO,
    growth_multiplier: float = DEFAULT_GROWTH_MULTIPLIER,
) -> float:
    """``base_rps × peak_to_avg_ratio × growth_multiplier``, rounded to 4 decimals."""
    rate = base_rps(calls_per_second, frequency_per_hour)
    return round(rate * peak_to_avg_ratio * growth_multiplier, 4)


def avg_row_size_bytes(size_mb: float | None, row_count: int | None) -> tuple[int, bool]:
    """``(size_mb ?? 0.01) × 1,048,576 / max(row_count, 1)`` and whether size_mb was unknown."""
    estimated = size_mb is None
    size = UNKNOWN_SIZE_MB if size_mb is None else float(size_mb)
    return round(size * BYTES_PER_MB / max(int(row_count or 0), 1)), estimated


def column_value_bytes(column: Mapping[str, Any]) -> int:
    """The skill's value-size rule: numeric 8, boolean 1, else max_length (at most 1024) or 50."""
    kind = _enum_value(column.get("normalized_data_type"))
    if kind in _NUMERIC_TYPES:
        return 8
    if kind == "boolean":
        return 1
    max_length = column.get("max_length")
    return min(int(max_length), LARGE_STRING_BYTES) if max_length else DEFAULT_STRING_BYTES


def schema_item_size_bytes(columns: Iterable[Mapping[str, Any]]) -> int:
    """Sum of ``len(column_name) + value size`` over the columns."""
    total = 0
    for column in columns or []:
        total += len(str(column.get("column_name") or "")) + column_value_bytes(column)
    return total


def shard_count(data_gb: float, max_shard_gb: float, node_count: int) -> int:
    """1 shard up to ``max_shard_gb``; else ceil(data / max) rounded up to a node multiple."""
    if data_gb <= max_shard_gb:
        return 1
    raw = math.ceil(data_gb / max_shard_gb)
    return math.ceil(raw / node_count) * node_count


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def capped_text_columns(columns: Iterable[Mapping[str, Any]]) -> list[str]:
    """String-like columns whose ``max_length`` exceeds :data:`LARGE_STRING_BYTES`."""
    return [
        str(c.get("column_name") or "")
        for c in columns or []
        if _enum_value(c.get("normalized_data_type")) not in _NUMERIC_TYPES | {"boolean"}
        and int(c.get("max_length") or 0) > LARGE_STRING_BYTES
    ]


def _table_metrics(table: Mapping[str, Any]) -> TableDesignMetrics:
    size_mb = table.get("size_mb")
    row_bytes, estimated = avg_row_size_bytes(size_mb, table.get("row_count"))
    return TableDesignMetrics(
        table_id=str(table.get("table_id") or table.get("table_name") or ""),
        row_count=int(table.get("row_count") or 0),
        avg_row_size_bytes=row_bytes,
        avg_row_size_kb=round(row_bytes / 1024, 3),
        size_estimated=estimated,
        schema_item_size_bytes=schema_item_size_bytes(table.get("columns") or []),
        data_gb=round(float(size_mb or 0) / 1024, 4),
        capped_text_columns=capped_text_columns(table.get("columns") or []),
    )


#: Key ``filter_collector_for_assignment`` records the unfiltered non-OTHER
#: pattern count under, in the collector's ``queries`` block, so coverage still
#: compares the whole log against ``total_queries_analyzed`` (#626).
UNFILTERED_PATTERNS_KEY = "_unfiltered_in_scope_patterns"


def in_scope_pattern_count(queries: Iterable[Mapping[str, Any]]) -> int:
    """Query patterns whose type is not OTHER."""
    return sum(1 for q in queries if _enum_value(q.get("query_type")) != "OTHER")


def _coverage(
    queries: Sequence[Mapping[str, Any]], total: int | None, in_scope: int | None
) -> CoverageMetrics | None:
    if not total:
        return None
    if in_scope is None:
        in_scope = in_scope_pattern_count(queries)
    return CoverageMetrics(
        in_scope_patterns=in_scope,
        total_queries_analyzed=int(total),
        coverage_ratio=round(in_scope / int(total), 4),
    )


def _embeddings(
    tables: Sequence[Mapping[str, Any]], sizes: dict[str, TableDesignMetrics]
) -> list[EmbeddingSizeMetrics]:
    """One entry per foreign key whose parent is in the input (child = the FK's table)."""
    by_key: dict[str, Mapping[str, Any]] = {}
    for t in tables:
        by_key[str(t.get("table_id") or "")] = t
        by_key.setdefault(str(t.get("table_name") or ""), t)
    out: list[EmbeddingSizeMetrics] = []
    seen: set[tuple[str, str]] = set()
    for child in tables:
        child_id = str(child.get("table_id") or "")
        for fk in child.get("foreign_keys") or []:
            parent = by_key.get(str(fk.get("referenced_table") or ""))
            if parent is None:
                continue
            parent_id = str(parent.get("table_id") or "")
            if (parent_id, child_id) in seen:
                continue
            seen.add((parent_id, child_id))
            avg_children = round(
                int(child.get("row_count") or 0) / max(int(parent.get("row_count") or 0), 1), 2
            )
            parent_kb = sizes[parent_id].avg_row_size_kb
            child_kb = sizes[child_id].avg_row_size_kb
            embedded = round(child_kb * avg_children, 3)
            out.append(
                EmbeddingSizeMetrics(
                    parent_table=parent_id,
                    child_table=child_id,
                    avg_children_per_parent=avg_children,
                    parent_row_size_kb=parent_kb,
                    child_row_size_kb=child_kb,
                    embedded_avg_kb=embedded,
                    doc_avg_kb_if_embedded=round(parent_kb + embedded, 3),
                )
            )
    return sorted(out, key=lambda e: (e.parent_table, e.child_table))


def build_design_metrics(
    engine: str,
    tables: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    *,
    total_queries_analyzed: int | None = None,
    unfiltered_in_scope_patterns: int | None = None,
    context: Mapping[str, Any] | None = None,
) -> DesignMetricsContract:
    """The metrics for ``engine``'s designer from its (already scoped) tables and queries.

    Coverage counts ``unfiltered_in_scope_patterns`` when given (the whole query
    log before the assignment scoped it to ``engine``), else ``queries``.
    """
    context = context or {}
    growth = float(context.get("growth_multiplier") or DEFAULT_GROWTH_MULTIPLIER)
    peak = float(context.get("peak_to_avg_ratio") or DEFAULT_PEAK_TO_AVG_RATIO)
    table_metrics = [_table_metrics(t) for t in tables]
    sizes = {m.table_id: m for m in table_metrics}
    metrics = DesignMetricsContract(
        engine=engine,
        growth_multiplier=growth,
        peak_to_avg_ratio=peak,
        queries=sorted(
            (
                QueryDesignMetrics(
                    query_id=str(q.get("query_id") or ""),
                    base_rps=round(
                        base_rps(q.get("calls_per_second"), q.get("frequency_per_hour")), 6
                    ),
                    design_rps=design_rps(
                        q.get("calls_per_second"), q.get("frequency_per_hour"), peak, growth
                    ),
                )
                for q in queries
            ),
            key=lambda m: m.query_id,
        ),
        tables=sorted(table_metrics, key=lambda m: m.table_id),
        coverage=_coverage(queries, total_queries_analyzed, unfiltered_in_scope_patterns),
    )
    if engine == "dynamodb":
        metrics.dynamodb_partition_limits = DynamoDBPartitionLimits()
    elif engine == "documentdb":
        metrics.documentdb_embeddings = _embeddings(tables, sizes)
    elif engine == "opensearch":
        rules = OpenSearchShardRules()
        rules.tables = [
            OpenSearchShardMetrics(
                table_id=m.table_id,
                data_gb=m.data_gb,
                search_shards=shard_count(
                    m.data_gb, rules.search_max_shard_gb, rules.assumed_node_count
                ),
                timeseries_shards=shard_count(
                    m.data_gb, rules.timeseries_max_shard_gb, rules.assumed_node_count
                ),
            )
            for m in metrics.tables
        ]
        metrics.opensearch_shards = rules
    return metrics


def design_metrics_from_collector(
    engine: str, collector_output: Mapping[str, Any], context: Mapping[str, Any] | None = None
) -> dict:
    """:func:`build_design_metrics` over a collector dict (full-contract shape), as JSON."""
    queries_block = collector_output.get("queries") or {}
    return build_design_metrics(
        engine,
        (collector_output.get("database_schema") or {}).get("tables") or [],
        queries_block.get("query_patterns") or [],
        total_queries_analyzed=queries_block.get("total_queries_analyzed"),
        unfiltered_in_scope_patterns=queries_block.get(UNFILTERED_PATTERNS_KEY),
        context=context,
    ).model_dump(mode="json")


def design_metrics_from_projection(
    engine: str, agent_input: Mapping[str, Any], unfiltered_in_scope_patterns: int | None = None
) -> dict:
    """:func:`build_design_metrics` over a ``load_agent_input`` result (``Agent*`` shape).

    The projection drops the collector's ``_``-prefixed keys, so the caller
    passes :data:`UNFILTERED_PATTERNS_KEY` from the raw collector file.
    """
    collector = agent_input.get("collector") or {}
    queries_block = collector.get("queries") or {}
    return build_design_metrics(
        engine,
        collector.get("tables") or [],
        queries_block.get("query_patterns") or [],
        total_queries_analyzed=queries_block.get("total_queries_analyzed"),
        unfiltered_in_scope_patterns=unfiltered_in_scope_patterns,
        context=agent_input.get("context"),
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Checking a design against them
# ---------------------------------------------------------------------------


def _rps_agrees(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= max(RPS_ABS_TOLERANCE, RPS_REL_TOLERANCE * expected)


def _check_rps(
    patterns: Iterable[Mapping[str, Any]], ids_key: str, rates: Mapping[str, float]
) -> list[str]:
    errors = []
    for p in patterns:
        ids = p.get(ids_key) or []
        if isinstance(ids, str):
            ids = [ids]
        if not ids or any(i not in rates for i in ids) or p.get("design_rps") is None:
            continue  # unknown query IDs are the scope check's business
        expected = round(sum(rates[i] for i in ids), 4)
        actual = float(p["design_rps"])
        if not _rps_agrees(actual, expected):
            errors.append(
                f"access pattern {p.get('pattern_id') or ids}: design_rps {actual:g} disagrees "
                f"with design_metrics ({expected:g} = sum of design_rps for {', '.join(ids)}); "
                "copy it from design_metrics.queries"
            )
    return errors


def expected_utilization_pct(units_per_second: float, partition_limit: float) -> float:
    """``min(100, units / limit × 100)`` to one decimal (the merge's own rule)."""
    return round(min(100.0, units_per_second / partition_limit * 100), 1)


def check_hot_partition_entry(entry: Mapping[str, Any], label: str = "entry") -> list[str]:
    """Limit, utilization and at-risk flag of one ``HotPartitionEntry`` (dict)."""
    limits = DynamoDBPartitionLimits()
    expected_limit = (
        limits.read_units_per_second
        if entry.get("operation") == "read"
        else limits.write_units_per_second
    )
    errors: list[str] = []
    try:
        limit = float(entry.get("partition_limit"))  # type: ignore[arg-type]
        units = float(entry.get("rcu_or_wcu_per_second"))  # type: ignore[arg-type]
        utilization = float(entry.get("utilization_pct"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return errors  # the contract reports missing or non-numeric fields
    if limit != expected_limit:
        errors.append(
            f"hot_partition_analysis {label}: partition_limit {limit:g} for a "
            f"{entry.get('operation')} must be {expected_limit:g}"
        )
        return errors
    expected = expected_utilization_pct(units, limit)
    if abs(utilization - expected) > UTILIZATION_TOLERANCE_PCT:
        errors.append(
            f"hot_partition_analysis {label}: utilization_pct {utilization:g} disagrees with "
            f"rcu_or_wcu_per_second / partition_limit × 100 = {expected:g}"
        )
    at_risk = expected > limits.at_risk_threshold_pct
    if bool(entry.get("at_risk")) != at_risk:
        errors.append(
            f"hot_partition_analysis {label}: at_risk must be {str(at_risk).lower()} "
            f"(utilization {expected:g}% vs the {limits.at_risk_threshold_pct:g}% threshold)"
        )
    return errors


def _check_hot_partitions(design: Mapping[str, Any]) -> list[str]:
    errors = []
    for index, entry in enumerate(design.get("hot_partition_analysis") or []):
        if isinstance(entry, Mapping):
            label = f"[{index}] ({entry.get('table_name')}/{entry.get('gsi_name') or 'base'})"
            errors.extend(check_hot_partition_entry(entry, label))
    return errors


def _close(actual: float, expected: float) -> bool:
    diff = abs(actual - expected)
    return diff <= DOC_SIZE_ABS_TOLERANCE_KB or diff <= DOC_SIZE_REL_TOLERANCE * expected


def _embed_parent_path(path: str) -> str | None:
    """The enclosing array's ``embed_path`` (``posts.comments`` -> ``posts``), or None at top level."""
    cleaned = path.replace("[]", "").replace("$", "").strip(".")
    return cleaned.rsplit(".", 1)[0] if "." in cleaned else None


def _nesting(
    embedded: list[Mapping[str, Any]], fk_parents: Mapping[str, set[str]]
) -> dict[int, int | None]:
    """For each embedded entity (by index), the index of the entity it sits inside, or None.

    ``embed_path`` decides first (``posts.comments`` sits inside the entity
    whose path is ``posts``); without a dotted path, an entity whose source
    table has a foreign key to exactly one other embedded table sits inside it.
    """
    by_path = {
        str(e.get("embed_path") or "").replace("[]", "").replace("$", "").strip("."): i
        for i, e in enumerate(embedded)
    }
    by_table: dict[str, list[int]] = {}
    for i, e in enumerate(embedded):
        by_table.setdefault(str(e.get("source_table")), []).append(i)
    parent_of: dict[int, int | None] = {}
    for i, e in enumerate(embedded):
        parent_path = _embed_parent_path(str(e.get("embed_path") or ""))
        if parent_path is not None and parent_path in by_path:
            parent_of[i] = by_path[parent_path]
            continue
        candidates = [
            j
            for table in fk_parents.get(str(e.get("source_table")), set())
            for j in by_table.get(table, [])
            if j != i
        ]
        parent_of[i] = candidates[0] if len(candidates) == 1 else None
    return parent_of


def _embedded_size_kb(
    index: int | None,
    root_kb: float,
    embedded: list[Mapping[str, Any]],
    parent_of: Mapping[int, int | None],
    tables: Mapping[str, float],
    length_key: str,
    depth: int = 0,
) -> float:
    """Row size of ``index`` (None = the collection's parent) plus everything embedded in it.

    A child's array length is per enclosing document: 10 comments per post.
    """
    own = root_kb if index is None else tables[str(embedded[index]["source_table"])]
    if depth > len(embedded):  # a cycle in the paths: stop rather than recurse forever
        return own
    return own + sum(
        float(child.get(length_key) or 0)
        * _embedded_size_kb(j, root_kb, embedded, parent_of, tables, length_key, depth + 1)
        for j, child in enumerate(embedded)
        if parent_of.get(j) == index and child.get("strategy") == "embed"
    )


def _check_doc_sizes(
    design: Mapping[str, Any],
    tables: Mapping[str, float],
    fk_parents: Mapping[str, set[str]] | None = None,
) -> list[str]:
    """Each collection's sizes against its row sizes and its own array lengths.

    A document is the parent's row plus, for every ``embed`` entity directly
    inside it, ``array length × (that child's row + whatever is embedded in the
    child)``, recursively, so nested embeds (``posts.comments``) count per
    enclosing element. A ``reference`` entity adds nothing. Skipped when the
    parent is ambiguous (not exactly one non-embedded source table, e.g. a
    polymorphic collection), a table is unknown, or an entity is ``hybrid``
    (only part of the child is embedded).
    """
    errors = []
    for c in design.get("collections") or []:
        embedded = list(c.get("embedded_entities") or [])
        children = {e.get("source_table") for e in embedded}
        parents = [t for t in c.get("source_tables") or [] if t not in children]
        if len(parents) != 1 or parents[0] not in tables:
            continue
        if any(
            e.get("strategy") == "hybrid" or e.get("source_table") not in tables for e in embedded
        ):
            continue
        parent_of = _nesting(embedded, fk_parents or {})
        for field, length_key in (
            ("estimated_avg_doc_size_kb", "avg_array_length"),
            ("estimated_max_doc_size_kb", "max_array_length_estimate"),
        ):
            if c.get(field) is None:
                continue
            expected = round(
                _embedded_size_kb(
                    None, tables[parents[0]], embedded, parent_of, tables, length_key
                ),
                3,
            )
            actual = float(c[field])
            if not _close(actual, expected):
                errors.append(
                    f"collection {c.get('collection_name')}: {field} {actual:g} disagrees with "
                    f"{expected:g} KB: the parent row plus, per embedded entity, {length_key} × "
                    "(its row + what is embedded inside it), row sizes from "
                    "design_metrics.tables avg_row_size_kb"
                )
    return errors


def _check_shards(design: Mapping[str, Any], metrics: Mapping[str, Any]) -> list[str]:
    rules = OpenSearchShardRules.model_validate(metrics.get("opensearch_shards") or {})
    data_gb = {t["table_id"]: float(t["data_gb"]) for t in metrics.get("tables") or []}
    errors = []
    designs = [
        ("index", d.get("index_name"), d.get("source_tables"), d.get("settings"), "search")
        for d in design.get("index_designs") or []
    ] + [
        (
            "data stream",
            d.get("data_stream_name"),
            d.get("source_tables"),
            (d.get("index_template") or {}).get("settings"),
            "timeseries",
        )
        for d in design.get("data_stream_designs") or []
    ]
    for kind, name, sources, settings, workload in designs:
        if not settings or not sources or any(s not in data_gb for s in sources):
            continue
        nodes = int(settings.get("assumed_node_count") or rules.assumed_node_count)
        total = round(sum(data_gb[s] for s in sources), 4)
        max_gb = (
            rules.search_max_shard_gb if workload == "search" else rules.timeseries_max_shard_gb
        )
        expected = shard_count(total, max_gb, nodes)
        actual = settings.get("number_of_shards")
        if actual != expected:
            errors.append(
                f"{kind} {name}: number_of_shards {actual} disagrees with the shard rule: "
                f"{total:g} GB over {', '.join(sources)}, at most {max_gb:g} GB per shard, "
                f"{nodes} nodes -> {expected}"
            )
    return errors


def check_design_numbers(
    engine: str, design: Mapping[str, Any], metrics: Mapping[str, Any] | None
) -> list[str]:
    """One message per number in ``design`` that disagrees with ``metrics`` (or the rules).

    ``metrics`` is a :class:`DesignMetricsContract` as JSON; with none, only the
    self-contained DynamoDB hot-partition arithmetic is checked.
    """
    if engine not in METRIC_ENGINES or not isinstance(design, Mapping):
        return []
    errors: list[str] = []
    rates = {q["query_id"]: float(q["design_rps"]) for q in (metrics or {}).get("queries") or []}
    if engine == "dynamodb":
        errors += _check_hot_partitions(design)
        ids_key = "query_ids"
    elif engine == "documentdb":
        ids_key = "source_query_ids"
    else:
        ids_key = "query_ids"
    if metrics:
        errors += _check_rps(design.get("access_patterns") or [], ids_key, rates)
        if engine == "documentdb":
            row_kb = {
                t["table_id"]: float(t["avg_row_size_kb"]) for t in metrics.get("tables") or []
            }
            fk_parents: dict[str, set[str]] = {}
            for pair in metrics.get("documentdb_embeddings") or []:
                fk_parents.setdefault(pair["child_table"], set()).add(pair["parent_table"])
            errors += _check_doc_sizes(design, row_kb, fk_parents)
        elif engine == "opensearch":
            errors += _check_shards(design, metrics)
    return errors


def unfiltered_pattern_count(raw_collector: Any) -> int | None:
    """:data:`UNFILTERED_PATTERNS_KEY` from a raw collector dict, if it is there."""
    if not isinstance(raw_collector, Mapping):
        return None
    value = (raw_collector.get("queries") or {}).get(UNFILTERED_PATTERNS_KEY)
    return int(value) if isinstance(value, int) else None
