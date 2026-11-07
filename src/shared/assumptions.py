# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The assumptions an assessment rests on, each with its origin (#1990).

Design metrics used to multiply every measured rate by a fixed 3 (peak) and 10
(growth), never shown to the reader, and the result set risk severity. Neither is
synthetic any more:

* **Peak.** When the source's CloudWatch metrics (``metrics.rds_cloudwatch_metrics``)
  cover :data:`PEAK_MIN_DAYS` to :data:`PEAK_MAX_DAYS` days, their observed
  peak-to-average ratio (each metric's highest maximum over the mean of its periods'
  averages, the higher of read and write IOPS) is applied to every rate that is a
  long-window average, and stated. A statement whose rate was sampled
  (``rate_window_basis`` ``sampled``, #1908) is a short window's own load and gets no
  extra factor. Otherwise the peak is "not measured" and the factor is 1.
* **Growth.** :data:`DEFAULT_GROWTH` (5×) unless the customer sets it
  (``decisions/assumptions.json``, written by ``--growth``). It is never applied to a
  design rate, a risk's severity or the headline cost: the deliverables state it as a
  separate scenario line, with the cost and the busiest partition key value at each of
  :data:`SCENARIO_GROWTHS`.

:func:`assumption_items` is the one assumptions block: growth, peak source, the rate
window and divisor, and the region and price date, each with its origin (``default``,
``collected`` or ``customer``). The approval gate and every deliverable print its
lines; approving the gate approves them, and nothing asks about them.

Extended by #1991, which adds the rate window's length and dates and its
representativeness check to the same block (the ``rate_window`` item), and the
assumptions file and settings panel through which the customer changes the others.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

Origin = Literal["default", "collected", "customer"]
PeakBasis = Literal["cloudwatch", "sampled", "not_measured", "customer"]

DEFAULT_GROWTH = 5.0
#: The growth multiples every deliverable shows the figures at.
SCENARIO_GROWTHS = (1.0, 5.0, 10.0)
#: The CloudWatch coverage a peak-to-average ratio is taken from.
PEAK_MIN_DAYS = 7.0
PEAK_MAX_DAYS = 14.0
#: The metrics whose ratio is the peak: request-driven I/O, read and write.
PEAK_METRICS = ("read_iops", "write_iops")

ASSUMPTIONS_FILE = "decisions/assumptions.json"


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _cloudwatch(collector: Mapping[str, Any] | None) -> Mapping[str, Any]:
    cw = ((collector or {}).get("metrics") or {}).get("rds_cloudwatch_metrics")
    return cw if isinstance(cw, Mapping) else {}


def _queries(collector: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [
        q
        for q in ((collector or {}).get("queries") or {}).get("query_patterns") or []
        if isinstance(q, Mapping)
    ]


def _days(n: float) -> str:
    return f"{n:.1f}".rstrip("0").rstrip(".")


def _ratio(n: float) -> str:
    return f"{n:.2f}".rstrip("0").rstrip(".")


def peak_assumption(
    collector: Mapping[str, Any] | None, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The peak factor the design rates take, its basis and the sentence stating it.

    ``ratio`` is the factor a long-window rate is multiplied by (1.0 unless the peak is
    measured or the customer set it); ``coverage_days`` the days the metrics cover."""
    custom = _num((context or {}).get("peak_to_avg_ratio"))
    if custom is not None and custom > 0:
        return {
            "basis": "customer",
            "ratio": custom,
            "coverage_days": None,
            "statement": f"{_ratio(custom)}× peak-to-average ratio, set by the customer",
        }
    cw = _cloudwatch(collector)
    period = _num(cw.get("period_seconds"))
    ratios: list[float] = []
    covered: list[float] = []
    for key in PEAK_METRICS:
        stats = cw.get(key)
        if not isinstance(stats, Mapping):
            continue
        avg, peak, points = (
            _num(stats.get("avg")),
            _num(stats.get("max")),
            _num(stats.get("datapoints")),
        )
        if not period or not points or not avg or peak is None:
            continue
        days = points * period / 86_400
        covered.append(days)
        if PEAK_MIN_DAYS <= days <= PEAK_MAX_DAYS:
            ratios.append(max(peak / avg, 1.0))
    if ratios:
        ratio = round(max(ratios), 2)
        days = round(max(covered), 1)
        return {
            "basis": "cloudwatch",
            "ratio": ratio,
            "coverage_days": days,
            "statement": (
                f"{_ratio(ratio)}× peak-to-average ratio, observed in the source's CloudWatch "
                f"read and write IOPS over {_days(days)} days and applied to each average rate"
            ),
        }
    queries = _queries(collector)
    if queries and all(q.get("rate_window_basis") == "sampled" for q in queries):
        return {
            "basis": "sampled",
            "ratio": 1.0,
            "coverage_days": None,
            "statement": (
                "the collection's sampled window: each rate is that window's own load, "
                "with no extra factor"
            ),
        }
    why = (
        f"the source's metrics cover {_days(round(max(covered), 1))} days, not "
        f"{_days(PEAK_MIN_DAYS)} to {_days(PEAK_MAX_DAYS)}"
        if covered
        else "the collection has no CloudWatch metrics covering 7 to 14 days"
    )
    return {
        "basis": "not_measured",
        "ratio": 1.0,
        "coverage_days": round(max(covered), 1) if covered else None,
        "statement": f"not measured ({why}): the rates are used as measured",
    }


def query_peak_factor(query: Mapping[str, Any], peak: Mapping[str, Any]) -> float:
    """The factor one statement's rate takes: the peak ratio, except for a sampled rate."""
    if peak.get("basis") in ("cloudwatch", "customer") and (
        query.get("rate_window_basis") != "sampled" or peak.get("basis") == "customer"
    ):
        return float(peak.get("ratio") or 1.0)
    return 1.0


def growth_assumption(decision: Mapping[str, Any] | None) -> tuple[float, Origin]:
    """The growth multiple and its origin: the customer's recorded value, else 5×."""
    value = _num((decision or {}).get("growth_multiplier"))
    if value is not None and value > 0:
        return value, "customer"
    return DEFAULT_GROWTH, "default"


def scenario_growths(growth: float) -> list[float]:
    """1×, 5× and 10×, and the job's own growth when it is none of them."""
    return sorted({*SCENARIO_GROWTHS, float(growth)})


def _times(n: float) -> str:
    return f"{n:g}×"


def assumption_items(
    collector: Mapping[str, Any] | None,
    *,
    growth: float,
    growth_origin: Origin,
    price_basis: Mapping[str, Any] | None,
    rate_window_statement: str | None,
    context: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The assumptions block: one item per assumption, with its value, origin and line."""
    peak = peak_assumption(collector, context)
    peak_origin: Origin = {
        "cloudwatch": "collected",
        "sampled": "collected",
        "customer": "customer",
    }.get(
        peak["basis"], "default"
    )  # type: ignore[assignment]
    items: list[dict[str, Any]] = [
        {
            "key": "growth",
            "value": growth,
            "origin": growth_origin,
            "basis": None,
            "line": (
                f"Growth: {_times(growth)} ({growth_origin}), stated as a separate scenario; "
                "never applied to the design rates, the risks or the headline cost"
            ),
        },
        {
            "key": "peak",
            "value": peak["ratio"] if peak["basis"] in ("cloudwatch", "customer") else None,
            "origin": peak_origin,
            "basis": peak["basis"],
            "line": f"Peak: {peak['statement']} ({peak_origin})",
        },
    ]
    if rate_window_statement:
        items.append(
            {
                "key": "rate_window",
                "value": None,
                "origin": "collected",
                "basis": None,
                "line": f"Rate window: {rate_window_statement} (collected)",
            }
        )
    if price_basis:
        region = price_basis.get("region")
        date = price_basis.get("retrieved_on")
        items.append(
            {
                "key": "pricing",
                "value": region,
                "origin": "default",
                "basis": None,
                "line": (
                    f"Region and prices: {region}, {price_basis.get('pricing_model') or 'on-demand'}"
                    f" prices from the {price_basis.get('source') or 'AWS Price List'}"
                    + (f" retrieved {date}" if date else "")
                    + " (default)"
                ),
            }
        )
    return items


def assumption_lines(items: Iterable[Mapping[str, Any]]) -> list[str]:
    return [str(i["line"]) for i in items if i.get("line")]


__all__ = [
    "ASSUMPTIONS_FILE",
    "DEFAULT_GROWTH",
    "PEAK_MAX_DAYS",
    "PEAK_MIN_DAYS",
    "SCENARIO_GROWTHS",
    "assumption_items",
    "assumption_lines",
    "growth_assumption",
    "peak_assumption",
    "query_peak_factor",
    "scenario_growths",
]


# ---------------------------------------------------------------------------
# Growth scenarios: the figures that depend on growth, at 1×, 5× and 10×
# ---------------------------------------------------------------------------


def scaled_collector(collector: Mapping[str, Any], growth: float) -> dict[str, Any]:
    """``collector`` with every statement's rate multiplied by ``growth`` (a copy)."""
    queries = dict((collector or {}).get("queries") or {})
    patterns = []
    for q in queries.get("query_patterns") or []:
        if not isinstance(q, Mapping):
            patterns.append(q)
            continue
        row = dict(q)
        for key in ("calls_per_second", "frequency_per_hour"):
            value = _num(row.get(key))
            if value is not None:
                row[key] = value * growth
        patterns.append(row)
    queries["query_patterns"] = patterns
    return {**dict(collector or {}), "queries": queries}


def _usd(n: float) -> str:
    return f"${n:,.2f}"


def _share(n: float) -> str:
    if n >= 1:
        return f"{n:.1f}".rstrip("0").rstrip(".") + "%"
    return f"{n:.2g}%"


def growth_scenario_facts(
    collector: Mapping[str, Any] | None,
    *,
    growth: float,
    origin: Origin,
    headline_cost: float,
    price: Any,
    busiest_key: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The cost and the busiest partition key value at each scenario growth (#1990).

    ``price`` re-prices a collector (the TCO's own computation, ``projected_monthly_cost``)
    so a scenario's cost is the headline's arithmetic at a scaled rate; at 1× it is the
    headline itself. ``busiest_key`` is the design entry whose estimated key value is the
    busiest (``partition_load.busiest_key``), scaled linearly. Nothing here reaches a
    risk's severity or the headline."""
    rows: list[dict[str, Any]] = []
    for g in scenario_growths(growth):
        cost = (
            float(headline_cost)
            if g == 1.0
            else round(float(price(scaled_collector(collector or {}, g))), 2)
        )
        pct = busiest_key.get("per_key_pct_of_limit") if busiest_key else None
        rows.append(
            {
                "growth": g,
                "monthly_cost_usd": round(cost, 2),
                "busiest_key_pct_of_limit": None if pct is None else round(pct * g, 4),
            }
        )
    costs = ", ".join(
        f"{_times(r['growth'])} {_usd(r['monthly_cost_usd'])}/mo"
        + (" (the headline)" if r["growth"] == 1.0 else "")
        + (f" (the {origin} growth)" if r["growth"] == growth and growth != 1.0 else "")
        for r in rows
    )
    lines = [f"Growth scenario: {costs}; not in the headline cost or the risks"]
    if busiest_key and busiest_key.get("per_key_pct_of_limit") is not None:
        where = busiest_key["table_name"] + (
            f" index {busiest_key['gsi_name']}" if busiest_key.get("gsi_name") else ""
        )
        shares = ", ".join(
            f"{_times(r['growth'])} {_share(r['busiest_key_pct_of_limit'])}" for r in rows
        )
        headroom = (
            100 / busiest_key["per_key_pct_of_limit"]
            if busiest_key["per_key_pct_of_limit"]
            else None
        )
        lines.append(
            f"Busiest estimated partition key value ({where} {busiest_key['operation']}s), "
            f"share of its limit: {shares}"
            + (f"; at the design rate it can take {headroom:,.0f}× its load" if headroom else "")
        )
    return {
        "growth": growth,
        "origin": origin,
        "scenarios": rows,
        "busiest_key": (
            {k: busiest_key.get(k) for k in ("engine", "table_name", "gsi_name", "operation")}
            if busiest_key and busiest_key.get("per_key_pct_of_limit") is not None
            else None
        ),
        "lines": lines,
    }


def gate_assumption_lines(
    collector: Mapping[str, Any] | None, decision: Mapping[str, Any] | None
) -> list[str]:
    """The assumptions block as the approval gate states it, before any cost exists: the
    same items synthesis states (today's price table, the collection's rate windows)."""
    from src.shared.catalog.prices import price_basis
    from src.shared.rate_window import rate_window_facts

    if not collector:
        return []
    growth, origin = growth_assumption(decision)
    patterns = _queries(collector)
    return assumption_lines(
        assumption_items(
            collector,
            growth=growth,
            growth_origin=origin,
            price_basis=price_basis(),
            rate_window_statement=rate_window_facts(list(patterns)).get("statement"),
        )
    )


def record_growth(store: Any, database_name: str, job_id: str, growth: float, via: str) -> dict:
    """Record the customer's growth for the job (``decisions/assumptions.json``, #1990).

    Growth reaches only the scenario lines synthesis writes, so a change re-runs
    synthesis alone. Raises ValueError for a value that is not a positive number."""
    from datetime import UTC, datetime

    value = _num(growth)
    if value is None or value <= 0:
        raise ValueError(f"growth must be a positive number, got {growth!r}")
    record = {
        "job_id": job_id,
        "database_name": database_name,
        "growth_multiplier": value,
        "origin": "customer",
        "via": via,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    store.write_json(f"{database_name}/{job_id}/{ASSUMPTIONS_FILE}", record)
    return record
