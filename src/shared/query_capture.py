# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The statement cap, the minimum execution count and the capture a collection records (#384).

By default a collection keeps every distinct statement that ran at least
``min_executions`` times (10 by default): there is no cap, because the product reads
production workloads (decision on #384, 2026-11-02). A cap, ``query_limit``, applies
only when the user sets one. The collection records both settings, with how many
statements it kept, in ``metadata.query_capture``:

- a live collector sends both to its statement query (no ``LIMIT`` clause without a
  cap) and records them (:func:`live_query_capture`);
- every ingest of a collection file (``scripts/run_collect.py``,
  ``scripts/run_assessment.py``, the collector's offline mode and the library ingest)
  records the capture, and cuts the collection only to an explicit cap
  (:func:`cap_collection`).

An explicit cap never drops a write or a locking read in favour of a read: those
statements decide which engine writes each table (ADR-031 Amendment 9, one writer per
table), so a cap that kept a table's reads and dropped its writes would make it look
read-only. The cap keeps every write and locking read first, then fills the rest with
the costliest reads. When the writes alone exceed the cap, it keeps the costliest
writes and records, per table, how many writes it dropped.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from src.contracts.collector_input import DEFAULT_MIN_EXECUTIONS

if TYPE_CHECKING:
    from src.contracts.collector_input import CollectionOptions
    from src.contracts.collector_output import QueryCapture

__all__ = [
    "DEFAULT_MIN_EXECUTIONS",
    "CapResult",
    "cap_collection",
    "capture_settings",
    "live_query_capture",
    "query_merge_summary",
    "offline_query_capture",
    "parse_query_limit",
    "query_stage_rows",
    "statement_query_failed",
]


def capture_settings(limit: object, min_executions: object) -> tuple[int | None, int]:
    """Both settings as a statement query interpolates them: ``limit`` a positive whole
    number or ``None`` (no cap), ``min_executions`` a positive whole number.

    Raises:
        ValueError: a setting is not a positive whole number (a bool is not one).
    """
    checks: list[tuple[str, object, bool]] = [
        ("query limit", limit, True),
        ("minimum execution count", min_executions, False),
    ]
    out: list[int | None] = []
    for name, value, optional in checks:
        if value is None and optional:
            out.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"the {name} must be a positive whole number, got {value!r}")
        out.append(int(value))
    return out[0], int(out[1])  # type: ignore[arg-type]


def parse_query_limit(text: str) -> int:
    """A ``--query-limit`` option's value: a positive whole number.

    Raises:
        ValueError: the text is not one.
    """
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"--query-limit must be a positive whole number, got {text!r}") from None
    if value < 1:
        raise ValueError(f"--query-limit must be a positive whole number, got {text!r}")
    return value


def statement_query_failed(exc: BaseException) -> dict[str, str]:
    """The query stage a live collector keeps when its statement query fails: the
    error's type only (a message can carry connection details), never an empty list
    that would read as a complete capture of nothing."""
    return {"error": f"the statement query failed ({type(exc).__name__})"}


def query_stage_rows(stage: object) -> tuple[list[dict], str | None]:
    """A live collector's query stage as its rows and the error, if the query failed.

    The stage is the list of rows, or :func:`statement_query_failed`'s marker; a
    checkpoint an older collector wrote is always a list.
    """
    if isinstance(stage, Mapping) and isinstance(stage.get("error"), str):
        return [], stage["error"]
    return (list(stage) if isinstance(stage, list) else []), None


def live_query_capture(
    captured: int, options: CollectionOptions, error: str | None = None
) -> QueryCapture | None:
    """What a live collector captured with ``options``: ``None`` when it collected no
    statements. The source's own count is never read, so ``available`` is unknown. With
    no cap nothing is cut; with one, a capture that reached it is truncated. A failed
    statement query records its ``error``, with nothing captured and ``truncated``
    unknown."""
    from src.contracts.collector_output import QueryCapture

    if not options.collect_query_patterns:
        return None
    limit = options.query_limit
    if error is not None:
        truncated: bool | None = None
    else:
        truncated = limit is not None and captured >= limit
    return QueryCapture(
        query_limit=limit,
        min_executions=options.min_executions,
        captured=0 if error is not None else captured,
        available=None,
        truncated=truncated,
        error=error,
    )


def _whole_count(value: object) -> int | None:
    """``value`` as a positive whole number: an int, or a float a JSON writer made of
    one (Oracle's NUMBER); anything else (a bool, a string, 0) is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, int) and value >= 1 else None


def offline_query_capture(
    metadata: object, rows: int, held: int | None = None
) -> QueryCapture | None:
    """What a collection script captured: the settings it wrote into
    ``metadata.query_capture`` (``query_limit``, null when there was no cap, and
    ``min_executions``) and the ``rows`` of statements it wrote (#384).

    No cap: nothing was cut (``truncated`` false). A cap: the run is truncated when it
    wrote as many statements as the cap, so the source may hold more. A collection that
    records no settings (an older script) records no capture, so the ingest leaves
    whether it was cut unknown. So do settings that cannot be trusted: a cap that is
    not a positive whole number, or a floor that is missing or not one (a settings
    block that failed leaves both null and writes no statement, which must never read
    as a complete capture of nothing). A script that also says how many statements it
    selected (``statements``) records an error when it wrote fewer: the statement list
    came back short, as MySQL's does when it outgrows the server's
    ``max_allowed_packet``. ``held`` (default ``rows``) is how many of those rows this
    database's collection holds, once the rows of other databases are dropped: it is
    ``captured``, while the script's own cap and selected count are checked against
    ``rows`` (#2145).
    """
    from src.contracts.collector_output import QueryCapture

    held = rows if held is None else held
    settings = metadata.get("query_capture") if isinstance(metadata, Mapping) else None
    if not isinstance(settings, Mapping):
        return None
    raw_limit = settings.get("query_limit")
    limit = _whole_count(raw_limit)
    floor = _whole_count(settings.get("min_executions"))
    if (raw_limit is not None and limit is None) or floor is None:
        return None
    selected = _whole_count(settings.get("statements"))
    if selected is not None and rows < selected:
        return QueryCapture(
            query_limit=limit,
            min_executions=floor,
            captured=held,
            available=None,
            truncated=None,
            error=(
                f"the collection script selected {selected:,} statements but wrote {rows:,}: "
                "the statement list came back short (for MySQL, raise the server's "
                "max_allowed_packet)"
            ),
        )
    return QueryCapture(
        query_limit=limit,
        min_executions=floor,
        captured=held,
        available=None,
        truncated=limit is not None and rows >= limit,
    )


def query_merge_summary(script_rows: int, query_shapes: int) -> dict[str, int] | None:
    """The literal-variant merge a collect phase reports (#2145): the rows this
    database's collection holds, the query shapes routing counts and the variants folded
    into them (``script_rows - query_shapes``, so the three always add up). ``None`` when
    nothing merged. No statement, execution or elapsed time is lost by it."""
    if script_rows <= query_shapes:
        return None
    return {
        "script_rows": script_rows,
        "query_shapes": query_shapes,
        "literal_variants_merged": script_rows - query_shapes,
    }


def _total_time(pattern: Any) -> float | None:
    value = pattern.get("total_time_ms") if isinstance(pattern, dict) else None
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _source_engine(collector_data: Mapping) -> str:
    metadata = collector_data.get("metadata")
    source = metadata.get("source_database") if isinstance(metadata, Mapping) else None
    engine = source.get("engine") if isinstance(source, Mapping) else None
    return str(engine or "")


def _costliest(indexes: list[int], patterns: list, n: int) -> list[int]:
    """The ``n`` costliest of ``indexes`` by total time: an unknown total ranks last,
    an equal one keeps the earlier statement."""
    ranked = sorted(
        indexes,
        key=lambda i: (
            _total_time(patterns[i]) is None,
            -(_total_time(patterns[i]) or 0.0),
            i,
        ),
    )
    return ranked[:n]


class CapResult:
    """What :func:`cap_collection` recorded, and whether this ingest cut the collection."""

    __slots__ = ("capture", "cut", "kept_query_ids")

    def __init__(self, capture: dict, cut: bool, kept_query_ids: set[str] | None) -> None:
        self.capture = capture
        self.cut = cut
        # The ids of the statements kept, when this ingest cut the collection.
        self.kept_query_ids = kept_query_ids


def cap_collection(collector_data: dict, query_limit: int | None = None) -> CapResult:
    """Record the capture in a collector output's ``metadata.query_capture`` and, with
    an explicit ``query_limit``, cut the collection to it.

    No cap (the default): every statement is kept. With a cap, a collection holding
    more keeps every write and locking read first (``write_groups.written_tables`` in
    the source's dialect, so a write a table's ownership hangs on is never dropped
    for a read), then the costliest reads by total time, in their own order. When the
    writes alone exceed the cap, the costliest writes are kept and
    ``writes_dropped_by_table`` counts, per table, the writes dropped.

    A capture the collection already records (a live collector's) is kept and
    narrowed: the smaller cap, its minimum execution count and error, and still
    truncated if it was. A collection that records none says nothing of its own cap, so
    its minimum count and whether it was truncated are unknown, unless this cap cut
    it. A missing or null ``queries`` holds no statements; a null ``metadata`` records
    none.
    """
    limit, _ = capture_settings(query_limit, DEFAULT_MIN_EXECUTIONS)
    queries = collector_data.get("queries")
    queries = queries if isinstance(queries, dict) else {}
    patterns = list(queries.get("query_patterns") or [])
    metadata = collector_data.get("metadata")
    prior = metadata.get("query_capture") if isinstance(metadata, dict) else None
    prior = prior if isinstance(prior, dict) else None

    held = len(patterns)
    cut = limit is not None and held > limit
    dropped_writes: Counter[str] = Counter()
    kept_ids: set[str] | None = None
    if cut:
        assert limit is not None  # nosec B101 -- narrowed by ``cut``
        from src.agents.referee.write_groups import written_tables

        engine = _source_engine(collector_data)
        written = [written_tables(p, engine) if isinstance(p, Mapping) else [] for p in patterns]
        writes = [i for i in range(held) if written[i]]
        reads = [i for i in range(held) if not written[i]]
        kept_writes = _costliest(writes, patterns, limit)
        keep = set(kept_writes)
        keep.update(_costliest(reads, patterns, limit - len(kept_writes)))
        for i in writes:
            if i not in keep:
                dropped_writes.update(written[i])
        kept = [patterns[i] for i in sorted(keep)]
        queries["query_patterns"] = kept
        kept_ids = {str(p.get("query_id") or "") for p in kept if isinstance(p, Mapping)}

    # ``captured`` counts the rows this database's collection holds and ``query_shapes``
    # the shapes kept: the unit routing counts, and the only one a cap cuts (#2145).
    shapes = min(held, limit) if limit is not None else held
    captured = held
    if prior is not None:
        prior_limit = prior.get("query_limit")
        if isinstance(prior_limit, int) and limit is not None:
            recorded_limit: int | None = min(prior_limit, limit)
        else:
            recorded_limit = limit if limit is not None else prior_limit
        prior_truncated = prior.get("truncated")
        prior_dropped = prior.get("writes_dropped_by_table")
        if isinstance(prior_dropped, Mapping):
            dropped_writes.update(
                {str(t): n for t, n in prior_dropped.items() if isinstance(n, int) and n > 0}
            )
        prior_captured = prior.get("captured")
        if isinstance(prior_captured, int) and prior_captured >= held:
            # Rows the script wrote: literal variants merged into one shape stay counted,
            # before and after any cut, so a merge or a cap never changes its unit.
            captured = prior_captured
        if cut:
            truncated: bool | None = True
            # The collector's own cut hid how many the source held.
            available = None if prior_truncated else held
        else:
            truncated = prior_truncated
            available = prior.get("available")
        capture = {
            "query_limit": recorded_limit,
            "min_executions": prior.get("min_executions"),
            "captured": captured,
            "query_shapes": shapes,
            "available": available,
            "truncated": truncated,
            "error": prior.get("error"),
        }
    else:
        capture = {
            "query_limit": limit,
            "min_executions": None,
            "captured": captured,
            "query_shapes": shapes,
            "available": held if cut else None,
            "truncated": True if cut else None,
            "error": None,
        }
    capture["writes_dropped_by_table"] = dict(sorted(dropped_writes.items()))
    if isinstance(metadata, dict):
        metadata["query_capture"] = capture
    return CapResult(capture, cut, kept_ids)
