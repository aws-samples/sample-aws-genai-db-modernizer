"""The source's buffer-cache hit ratio a read is estimated with (#1197).

An engine that bills a read only on a buffer-cache miss (DocumentDB's storage I/O) is
estimated with the share of reads the source's own cache served. A query that reports
its own hit ratio (``cache_hit_ratio_pct``) uses it; one that reports none falls back to
the collection's instance-wide ratio (collector contract 3.5,
``metrics.source_host.buffer_cache_hit_ratio_pct``); with neither, every read is a miss.
Each estimate names which basis it used.

The collection's ratio is an instance figure the collector measured, not something
another query contributes: it is the same for every query of the collection.

The ratio is bounded (maintainer decisions on #1222, 2026-10-22; #1000): the source's
buffer pool is not the target's, and the source's cache never makes the target's read
I/O free.

- Every read, whatever its basis, has at least ``MIN_BILLED_MISS_SHARE`` (1%) billed as
  a miss: a query's own ratio is used as reported, capped at 99%.
- The collection's ratio is also scaled down, never up, by the target's memory over the
  source's (the instance the engine's estimate prices, ``estimate_instance_memory_gb`` in
  its engine file, over ``metrics.source_host.physical_memory_gb``):
  ``min(1, target / source)``. A hit ratio does not grow linearly with memory, so a
  smaller source never raises it. When either memory is unknown it is not scaled.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

HitRatioBasis = Literal["query", "collection", "none"]

# The basis an estimate states when the workload it prices has no reads (an engine sized
# for writes only, #1374): there is nothing to bound, whatever the source reported.
NO_READS_BASIS = "no_reads"

# The key under which a caller that already decided the collection's cache hands it to an
# estimator that is given only part of the collection (``CollectionCache.from_collector``).
DECIDED_CACHE_KEY = "_collection_cache"

_BASIS_ORDER: tuple[HitRatioBasis, ...] = ("query", "collection", "none")

# The least share of every read billed as a miss, whatever hit ratio it is estimated with
# (maintainer decisions on #1222, 2026-10-22): 1%.
MIN_BILLED_MISS_SHARE = 0.01


def _capped(pct: float) -> float:
    return round(min(pct, 100.0 * (1.0 - MIN_BILLED_MISS_SHARE)), 6)


def percent_or_none(value: object) -> float | None:
    """``value`` as a percent clamped to 0..100, or None when it is not a finite number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return min(100.0, max(0.0, number))


def _positive_or_none(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _source_host(collector_output: Mapping | None) -> Mapping:
    return ((collector_output or {}).get("metrics") or {}).get("source_host") or {}


def collection_hit_ratio_pct(collector_output: Mapping | None) -> float | None:
    """The collection's instance-wide buffer-cache hit ratio, or None if not reported."""
    return percent_or_none(_source_host(collector_output).get("buffer_cache_hit_ratio_pct"))


def _copied_instance_ratio(collector_output: Mapping | None) -> float | None:
    """The one ratio every query of an old collection carries, or None (see
    :meth:`CollectionCache.from_collector`)."""
    patterns = ((collector_output or {}).get("queries") or {}).get("query_patterns") or []
    ratios = {percent_or_none(q.get("cache_hit_ratio_pct")) for q in patterns}
    if len(patterns) < 2 or len(ratios) != 1:
        return None
    (ratio,) = ratios
    return ratio


@dataclass(frozen=True)
class CollectionCache:
    """The collection's buffer-cache hit ratio as collected, and the source's memory."""

    hit_ratio_pct: float
    source_memory_gb: float | None = None
    # True for a collector output saved before #1197 by a live or agent path, which
    # copied the instance ratio, rounded, onto every query: those per-query figures are
    # the instance's, so a read uses this ratio, not its "own" (see ``from_collector``).
    from_queries: bool = False

    @classmethod
    def from_collector(cls, collector_output: Mapping | None) -> CollectionCache | None:
        """None when the collection reported no hit ratio.

        A collection with no ``source_host`` ratio whose queries (two or more) all carry
        the same ``cache_hit_ratio_pct`` is a file the old collector wrote by copying the
        instance ratio onto each query: that one figure is read as the instance ratio,
        scaled and stated as such. That rule reads the whole collection's queries, so it is
        decided once, on the whole collection: a caller that hands an estimator only some of
        the queries (the TCO sizing each engine for its assigned ones, #1374) passes the
        decided cache under ``DECIDED_CACHE_KEY`` (:meth:`decided_in`), and routing and the
        sized row bill a read with the same ratio."""
        if collector_output is not None and DECIDED_CACHE_KEY in collector_output:
            decided = collector_output[DECIDED_CACHE_KEY]
            return decided if isinstance(decided, CollectionCache) else None
        memory = _positive_or_none(_source_host(collector_output).get("physical_memory_gb"))
        pct = collection_hit_ratio_pct(collector_output)
        if pct is not None:
            return cls(hit_ratio_pct=pct, source_memory_gb=memory)
        copied = _copied_instance_ratio(collector_output)
        if copied is None:
            return None
        return cls(hit_ratio_pct=copied, source_memory_gb=memory, from_queries=True)

    @staticmethod
    def decided_in(collector_output: Mapping, subset: Mapping) -> dict:
        """``subset`` (part of ``collector_output``) carrying the whole collection's cache,
        decided once, so an estimator run on it does not decide again from fewer queries."""
        return {**subset, DECIDED_CACHE_KEY: CollectionCache.from_collector(collector_output)}

    def memory_scale(self, target_memory_gb: float | None) -> float | None:
        """``min(1, target / source)``, or None when either memory is unknown: a smaller
        source never raises the ratio."""
        target = _positive_or_none(target_memory_gb)
        if target is None or self.source_memory_gb is None:
            return None
        return min(1.0, target / self.source_memory_gb)

    def billed_hit_ratio_pct(self, target_memory_gb: float | None) -> float:
        """The ratio a read that falls back to the collection is billed with: scaled down
        by the memory ratio when both memories are known, and never above 99%."""
        scale = self.memory_scale(target_memory_gb)
        pct = self.hit_ratio_pct * scale if scale is not None else self.hit_ratio_pct
        return _capped(pct)


def bound_fields(collection: CollectionCache | None, target_memory_gb: float | None) -> dict:
    """The bound a cache-miss-only estimate states (``cost_components``, TCO rows): the
    floor every read has, and the collection's ratio and its scaling when there is one."""
    fields: dict = {
        "collection_hit_ratio_pct": None,
        "min_billed_miss_pct": round(MIN_BILLED_MISS_SHARE * 100.0, 6),
    }
    if collection is not None:
        fields.update(
            {
                "collection_hit_ratio_pct": collection.hit_ratio_pct,
                "collection_hit_ratio_billed_pct": collection.billed_hit_ratio_pct(
                    target_memory_gb
                ),
                "source_memory_gb": collection.source_memory_gb,
                "target_memory_gb": _positive_or_none(target_memory_gb),
            }
        )
    return fields


def read_hit_ratio(
    query: Mapping,
    collection: CollectionCache | None,
    target_memory_gb: float | None = None,
) -> tuple[float, HitRatioBasis]:
    """The hit ratio ``query``'s reads are estimated with, and where it came from.

    A query's own ratio is used as reported, capped at 99%; the collection's is also
    scaled down (:meth:`CollectionCache.billed_hit_ratio_pct`)."""
    own = percent_or_none(query.get("cache_hit_ratio_pct"))
    if own is not None and not (collection is not None and collection.from_queries):
        return _capped(own), "query"
    if collection is not None:
        return collection.billed_hit_ratio_pct(target_memory_gb), "collection"
    return 0.0, "none"


def combined_basis(bases: Iterable[HitRatioBasis]) -> str:
    """One stated basis for a set of reads: the bases they used, joined with ``+`` in
    the order query, collection, none (``"query+none"``: some reads reported their own
    ratio and the rest were billed in full). ``"none"`` also when there were no reads."""
    used = set(bases)
    return "+".join(b for b in _BASIS_ORDER if b in used) or "none"


def format_pct(value: float) -> str:
    """A hit ratio as text, to the 6 places the collector keeps (99.99993, not 99.9999)."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


_BOUND_KEYS = (
    "collection_hit_ratio_billed_pct",
    "source_memory_gb",
    "target_memory_gb",
    "min_billed_miss_pct",
)


def _collection_phrase(fields: Mapping) -> str | None:
    """How the collection's ratio billed the reads that fell back to it, or None when
    the collection reported none. A row written before the bound states the ratio only."""
    collected = percent_or_none(fields.get("collection_hit_ratio_pct"))
    if collected is None:
        return None
    phrase = f"the source's instance-wide buffer-cache hit ratio ({format_pct(collected)}%)"
    billed = percent_or_none(fields.get("collection_hit_ratio_billed_pct"))
    if billed is None:
        return phrase
    source = _positive_or_none(fields.get("source_memory_gb"))
    target = _positive_or_none(fields.get("target_memory_gb"))
    if source is not None and target is not None:
        phrase += (
            f" scaled by the target's {format_pct(target)} GiB over the source's "
            f"{format_pct(source)} GiB of memory"
        )
    elif source is None:
        phrase += ", not scaled to the target's memory (the source reported no memory size)"
    else:
        phrase += ", not scaled to the target's memory (the target's memory is not known)"
    return phrase + f", billed at {format_pct(billed)}%"


def _basis_parts(basis: str, fields: Mapping) -> list[str]:
    used = basis.split("+")
    parts = []
    if "query" in used:
        parts.append("each query's own buffer-cache hit ratio")
    collection = _collection_phrase(fields) if "collection" in used else None
    if collection is not None:
        parts.append(collection)
    if "none" in used:
        parts.append("every read billed where neither was reported")
    return parts


def _floor_sentence(fields: Mapping) -> str:
    """The floor every read has, as a sentence; empty for a row written before it."""
    floor = percent_or_none(fields.get("min_billed_miss_pct"))
    if not floor:
        return ""
    return f" At least {format_pct(floor)}% of every read is billed as a miss."


def read_io_basis_text(basis: str, fields: Mapping, *, has_reads: bool = True) -> str:
    """How a read I/O estimate was made, as one sentence (the cost estimate's stated
    basis, #1197). ``fields`` holds the collection's ratio and its bound
    (:meth:`CollectionCache.bound_fields`)."""
    if not has_reads:
        return "Read I/O: the workload has no reads"
    if basis.split("+") == ["none"]:
        return "Read I/O: every read billed (no buffer-cache hit ratio reported)"
    return (
        "Read I/O: only cache misses billed, at "
        + ", then ".join(_basis_parts(basis, fields))
        + "."
        + _floor_sentence(fields)
    )


def cost_basis_fields(cost_estimate: Mapping | None) -> dict:
    """The read I/O basis an analysed ``cost_estimate`` states, as the TCO row fields
    ``read_io_hit_ratio_basis``, ``collection_hit_ratio_pct`` and the bound applied to
    it (``collection_hit_ratio_billed_pct``, ``source_memory_gb``, ``target_memory_gb``,
    ``min_billed_miss_pct``); empty for an estimate that bills no read by the cache
    (every engine but DocumentDB)."""
    components = (cost_estimate or {}).get("cost_components") or {}
    basis = components.get("read_io_hit_ratio_basis")
    if not isinstance(basis, str) or not basis:
        return {}
    out: dict = {
        "read_io_hit_ratio_basis": basis,
        "collection_hit_ratio_pct": percent_or_none(components.get("collection_hit_ratio_pct")),
    }
    for key in _BOUND_KEYS:
        value = components.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = float(value)
    return out


def cost_basis_sentence(engine_name: str, row: Mapping) -> str | None:
    """The customer-facing note on how an engine's estimate billed its reads (#1197),
    from a TCO row's :func:`cost_basis_fields`; None when the row states no basis."""
    basis = row.get("read_io_hit_ratio_basis")
    if not isinstance(basis, str) or not basis:
        return None
    if basis == NO_READS_BASIS:
        return (
            f"{engine_name}'s estimate is sized for writes only: it has no read I/O for "
            "a buffer-cache hit ratio to bound."
        )
    if basis.split("+") == ["none"]:
        return (
            f"{engine_name}'s estimate bills every read I/O: the source reported no "
            "buffer-cache hit ratio."
        )
    return (
        f"{engine_name}'s estimate bills a read I/O only on a buffer-cache miss, at "
        + ", then ".join(_basis_parts(basis, row))
        + "."
        + _floor_sentence(row)
    )
