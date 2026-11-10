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

#1991: a customer sets them in one place, the optional assumptions file
(``src.shared.assumptions_file``, ``run_assessment.py --assumptions``), or growth alone
with ``--growth``, or in the results page's assumptions panel (``src.api.routes.assumptions``,
which re-runs only the phases a change affects, :data:`RERUN_PHASES`).
:func:`record_assumptions` records the values under ``decisions/assumptions.json`` with
each one's origin (``flag``, ``file``, ``ui``, or ``customer`` for any other writer), and
keeps a value another writer recorded. A peak
the customer sets is every designer's peak (:func:`design_context`); growth never
reaches a designer.

#1991 adds the rate window's length and dates and the divisor reminder to the
``rate_window`` item, and its representativeness check as the ``rate_window_check``
item (``src.shared.rate_window.window_check_facts``): a window that is short, short
against the server's uptime, counted from a reset, or several times the server's own
rate labels the costs and the rate-driven risks a scenario at the measured rate.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal

#: ``flag`` and ``file`` (#1991): set with ``--growth`` or the assumptions file; ``ui``:
#: in the results page's assumptions panel; ``customer``: by any other writer, and every
#: report written before #1991.
Origin = Literal["default", "collected", "customer", "flag", "file", "ui"]
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

#: The assumptions a customer can set (#1991), by their recorded key: the file's key,
#: the lowest value (and whether it is allowed) and the highest. Growth below 1× is a
#: shrinking workload; a peak below 1× would be a busiest hour quieter than the mean.
SETTABLE: dict[str, tuple[str, float, bool, float]] = {
    "growth_multiplier": ("growth", 0.0, False, 100.0),
    "peak_to_avg_ratio": ("peak", 1.0, True, 100.0),
}
#: The origin a writer records (``via``): anything else is the customer's.
_ORIGIN_BY_VIA: dict[str, Origin] = {"cli": "flag", "file": "file", "ui": "ui"}
#: The phases a change re-runs, by recorded key, in order (#1991): growth reaches only
#: synthesis and the deliverables it renders; the peak is every designer's, so schema
#: design and everything after it.
RERUN_PHASES: dict[str, tuple[str, ...]] = {
    "growth_multiplier": ("synthesis", "report"),
    "peak_to_avg_ratio": ("schema_design", "synthesis", "report"),
}


def check_value(key: str, value: object) -> float:
    """``value`` as the assumption ``key`` takes it; ValueError naming the range if not."""
    import math

    if key not in SETTABLE:
        raise ValueError(f"{key!r} is not an assumption a customer can set")
    name, low, low_ok, high = SETTABLE[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"{name} must be a number between {low:g} and {high:g}, not NaN or infinity"
        )
    if number > high or number < low or (number == low and not low_ok):
        bound = f"{low:g} (included)" if low_ok else f"{low:g} (excluded)"
        raise ValueError(f"{name} must be between {bound} and {high:g}, got {number:g}")
    return number


def settable_ranges() -> list[dict[str, Any]]:
    """Each assumption a customer can set, as the UI's settings panel offers it (#1991):
    its name (the assumptions file's key), recorded key, range (:data:`SETTABLE`, the one
    :func:`check_value` enforces) and the phases a change re-runs (:data:`RERUN_PHASES`)."""
    return [
        {
            "name": name,
            "key": key,
            "min": low,
            "min_included": low_ok,
            "max": high,
            "reruns": list(RERUN_PHASES[key]),
        }
        for key, (name, low, low_ok, high) in SETTABLE.items()
    ]


def range_text(key: str) -> str:
    """The range :func:`check_value` enforces for ``key``, in words: "between 0
    (excluded) and 100"."""
    _name, low, low_ok, high = SETTABLE[key]
    bound = f"{low:g} (included)" if low_ok else f"{low:g} (excluded)"
    return f"between {bound} and {high:g}"


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


#: The share of a window's periods with data that counts as covering the window: a 7-day
#: window with an hour or two missing still covers 7 days (#2025 review S4).
WINDOW_COVERED_SHARE = 0.95


def _coverage_days(points: float, period: float, window: float | None) -> float:
    """The days the metrics cover: the requested window when its periods are nearly all
    there (the 7 to 14 day bounds are inclusive), else what the datapoints span."""
    spanned = points * period / 86_400
    if window and spanned >= window * WINDOW_COVERED_SHARE:
        return window
    return spanned


def peak_assumption(
    collector: Mapping[str, Any] | None, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The peak factor the rates take, its basis and the sentence stating it.

    ``ratio`` is the factor a long-window rate is multiplied by (1.0 unless the peak is
    measured or the customer set it); ``coverage_days`` the days the metrics cover."""
    custom = _num((context or {}).get("peak_to_avg_ratio"))
    if custom is not None and custom > 0:
        return {
            "basis": "customer",
            "ratio": custom,
            "coverage_days": None,
            # #1991: where the customer set it (the assumptions file), for the block; the
            # block's line says who set it.
            "statement": f"{_ratio(custom)}× peak-to-average ratio",
            "origin": (context or {}).get("peak_origin") or "customer",
        }
    cw = _cloudwatch(collector)
    period = _num(cw.get("period_seconds"))
    window = _num(cw.get("window_days"))
    ratios: list[float] = []
    covered: list[float] = []
    for key in PEAK_METRICS:
        stats = cw.get(key)
        if not isinstance(stats, Mapping):
            continue
        # The busiest period's Average against the mean of the periods' Averages
        # (#2025 review S4): one minute's Maximum (a backup or a vacuum) is not the
        # workload's peak. A collection without the busiest period's Average has no peak.
        avg, peak, points = (
            _num(stats.get("avg")),
            _num(stats.get("max_period_avg")),
            _num(stats.get("datapoints")),
        )
        if not period or not points or not avg or peak is None:
            continue
        days = _coverage_days(points, period, window)
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
                f"{_ratio(ratio)}× peak-to-average ratio (the busiest hour's average against "
                f"the mean, the higher of read and write IOPS), observed in the source's "
                f"CloudWatch metrics over {_days(days)} days and applied to each average rate"
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


def _recorded_origin(decision: Mapping[str, Any], key: str) -> Origin:
    """The origin recorded for ``key``; a decision written before #1991 has none, and
    its writer (``via``) says it: ``--growth`` recorded ``cli``."""
    origins = decision.get("origins")
    origin = origins.get(key) if isinstance(origins, Mapping) else None
    if origin in ("flag", "file", "ui", "customer"):
        return origin  # type: ignore[no-any-return]
    return _ORIGIN_BY_VIA.get(str(decision.get("via") or ""), "customer")


#: How the gate and the deliverables say who set a value (#1991 review): the facts keep
#: the origin itself.
ORIGIN_LABELS: dict[str, str] = {
    "flag": "set in the run's options",
    "file": "set in the assumptions file",
    "ui": "set in the UI",
    "customer": "set by the customer",
}


def origin_label(origin: str) -> str:
    """The words a line uses for ``origin``: ``default`` and ``collected`` as they are."""
    return ORIGIN_LABELS.get(origin, origin)


def out_of_range_notes(decision: Mapping[str, Any] | None) -> dict[str, str]:
    """By assumptions block key, why a saved value is not used (#1991 review): a job
    recorded before the ranges (any positive growth) keeps loading, at the default, and
    says so instead of stating the default silently."""
    notes: dict[str, str] = {}
    for key, (name, *_rest) in SETTABLE.items():
        if decision is None or decision.get(key) is None:
            continue
        try:
            check_value(key, decision.get(key))
        except ValueError as exc:
            value = _num(decision.get(key))
            shown = _times(value) if value is not None else "value"
            notes[name] = f"the saved {name} {shown} is out of range ({exc}), so it is not used"
    return notes


def _recorded(decision: Mapping[str, Any] | None, key: str) -> float | None:
    """The recorded value of ``key`` when it is one the customer could set."""
    try:
        return check_value(key, (decision or {}).get(key))
    except ValueError:
        return None


def growth_assumption(decision: Mapping[str, Any] | None) -> tuple[float, Origin]:
    """The growth multiple and its origin: the customer's recorded value, else 5×."""
    value = _recorded(decision, "growth_multiplier")
    if value is not None and decision is not None:
        return value, _recorded_origin(decision, "growth_multiplier")
    return DEFAULT_GROWTH, "default"


def peak_context(decision: Mapping[str, Any] | None) -> dict[str, Any]:
    """The design context of the customer's recorded peak (#1991), or ``{}``: the
    ``context`` :func:`peak_assumption` reads. Growth is never in it."""
    value = _recorded(decision, "peak_to_avg_ratio")
    if value is None or decision is None:
        return {}
    return {
        "peak_to_avg_ratio": value,
        "peak_origin": _recorded_origin(decision, "peak_to_avg_ratio"),
    }


def read_assumptions(store: Any, database_name: str, job_id: str) -> dict | None:
    """The job's recorded assumptions (``decisions/assumptions.json``), or None."""
    key = f"{database_name}/{job_id}/{ASSUMPTIONS_FILE}"
    try:
        found = store.read_json(key) if store.exists(key) else None
    except Exception:  # noqa: BLE001 -- an unreadable decision keeps the defaults, logged
        from src.shared.progress import say

        say(f"[assumptions] Unreadable assumptions decision {key}; using the defaults")
        return None
    return found if isinstance(found, dict) else None


def design_context(store: Any, database_name: str, job_id: str) -> dict[str, Any]:
    """The context every designer's ``design_metrics`` take for the job (#1991): the
    customer's peak, when one is recorded."""
    return peak_context(read_assumptions(store, database_name, job_id))


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
    rate_windows: Mapping[str, Any] | None = None,
    notes: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The assumptions block: one item per assumption, with its value, origin and line.

    ``notes`` (:func:`out_of_range_notes`): by item key, why a saved value is not used;
    the item's line says it after its origin. Each line names its origin in words
    (:func:`origin_label`); the item keeps the origin itself.

    ``rate_windows`` is the collection's rate window facts
    (``src.shared.rate_window.rate_window_facts``, #1991): when given, the rate window
    item states the divisor, the window's length and dates and the divisor reminder, and
    a ``rate_window_check`` item states whether the window is representative.
    ``rate_window_statement`` is the divisor sentence alone, for a caller without them."""
    peak = peak_assumption(collector, context)
    peak_origin: Origin = {
        "cloudwatch": "collected",
        "sampled": "collected",
        "customer": peak.get("origin") or "customer",
    }.get(
        peak["basis"], "default"
    )  # type: ignore[assignment]
    notes = notes or {}

    def said(key: str, origin: str) -> str:
        label = origin_label(origin)
        return f"({label}; {notes[key]})" if notes.get(key) else f"({label})"

    items: list[dict[str, Any]] = [
        {
            "key": "growth",
            "setting": growth,
            "origin": growth_origin,
            "basis": None,
            "line": (
                f"Growth: {_times(growth)} {said('growth', growth_origin)}, shown as a separate "
                "what-if scenario. The costs and risks in this report use the collected traffic"
            ),
        },
        {
            "key": "peak",
            "setting": peak["ratio"] if peak["basis"] in ("cloudwatch", "customer") else None,
            "origin": peak_origin,
            "basis": peak["basis"],
            "line": f"Peak: {peak['statement']} {said('peak', peak_origin)}",
        },
    ]
    windows = rate_windows or {}
    divisor = windows.get("statement") or rate_window_statement
    if divisor:
        detail = windows.get("window_statement")
        items.append(
            {
                "key": "rate_window",
                "setting": windows.get("typical_window_seconds"),
                "origin": "collected",
                "basis": None,
                "line": f"Rate window: {divisor}"
                + (f"; {detail}" if detail else "")
                + " (collected)",
            }
        )
    if windows.get("check_statement"):
        # #1991: whether the window is representative; a flag labels the rate-driven
        # figures a scenario at the measured rate (facts.rate_windows.scenario_label).
        items.append(
            {
                "key": "rate_window_check",
                "setting": (
                    "not_checked"
                    if windows.get("representative") is None
                    else "representative" if windows["representative"] else "not_representative"
                ),
                "origin": "collected",
                "basis": ",".join(windows.get("flags") or []) or None,
                "line": f"Rate window check: {windows['check_statement']} (collected)",
            }
        )
    if price_basis:
        region = price_basis.get("region")
        date = price_basis.get("retrieved_on")
        items.append(
            {
                "key": "pricing",
                "setting": region,
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
    "RERUN_PHASES",
    "SCENARIO_GROWTHS",
    "assumption_items",
    "SETTABLE",
    "assumption_lines",
    "check_value",
    "design_context",
    "growth_assumption",
    "origin_label",
    "out_of_range_notes",
    "peak_assumption",
    "peak_context",
    "query_peak_factor",
    "read_assumptions",
    "range_text",
    "record_assumptions",
    "scenario_growths",
    "settable_ranges",
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


def _growth_said(origin: str) -> str:
    """How the scenario line marks the job's own growth: "the default growth", "the
    growth set in the assumptions file"."""
    if origin in ("flag", "file", "ui"):
        return f" (the growth {origin_label(origin)})"
    return f" (the {origin} growth)"


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
        + (" (the collected traffic)" if r["growth"] == 1.0 else "")
        + (_growth_said(origin) if r["growth"] == growth and growth != 1.0 else "")
        for r in rows
    )
    lines = [
        f"Growth scenario: {costs}; a what-if only, not part of the costs and risks in this report"
    ]
    if busiest_key and busiest_key.get("per_key_pct_of_limit") is not None:
        shares = ", ".join(
            f"{_times(r['growth'])} {_share(r['busiest_key_pct_of_limit'])}" for r in rows
        )
        headroom = (
            100 / busiest_key["per_key_pct_of_limit"]
            if busiest_key["per_key_pct_of_limit"]
            else None
        )
        lines.append(
            # No table name (#2025 e2e-llm): the executive report prints these lines and
            # names no collected table; facts.busiest_partition_key carries the entry.
            f"Busiest estimated partition key value ({busiest_key['operation']}s), "
            f"share of its limit: {shares}"
            + (
                f"; that key value can take {headroom:,.0f}× its collected traffic before it reaches its limit"
                if headroom
                else ""
            )
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
    from src.shared.rate_window import collection_rate_window_facts

    if not collector:
        return []
    growth, origin = growth_assumption(decision)
    windows = collection_rate_window_facts(collector) or {}
    return assumption_lines(
        assumption_items(
            collector,
            growth=growth,
            growth_origin=origin,
            price_basis=price_basis(),
            rate_window_statement=windows.get("statement"),
            rate_windows=windows,
            context=peak_context(decision),
            notes=out_of_range_notes(decision),
        )
    )


def record_assumptions(
    store: Any,
    database_name: str,
    job_id: str,
    values: Mapping[str, object],
    *,
    via: str,
    file_sha256: str | None = None,
    origins: Mapping[str, Origin] | None = None,
) -> dict:
    """Record the customer's assumptions for the job (``decisions/assumptions.json``).

    ``values`` maps :data:`SETTABLE` keys to their values; each is recorded with the
    origin its writer gives it (``via``: ``cli`` is a flag, ``file`` the assumptions
    file, #1991). A value already recorded and not in ``values`` keeps its own value and
    origin. ``origins`` overrides the origin of some of ``values``: one write records the
    file's values and a flag's together. ``file_sha256`` identifies the file the values
    came from (its text is never recorded), and stays while any value is still the
    file's. Raises ValueError, before writing anything, for a value out of range."""
    from datetime import UTC, datetime

    checked = {key: check_value(key, value) for key, value in values.items()}
    if not checked:
        raise ValueError("no assumption to record")
    prior = read_assumptions(store, database_name, job_id) or {}
    kept = {k: v for k in SETTABLE if k not in checked and (v := _recorded(prior, k)) is not None}
    origin = _ORIGIN_BY_VIA.get(via, "customer")
    recorded = {
        **{k: _recorded_origin(prior, k) for k in kept},
        **{k: (origins or {}).get(k) or origin for k in checked},
    }
    record: dict[str, Any] = {
        "job_id": job_id,
        "database_name": database_name,
        **kept,
        **checked,
        "origins": recorded,
        "via": via,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    sha = file_sha256 or prior.get("file_sha256")
    if sha and "file" in recorded.values():
        record["file_sha256"] = sha
    store.write_json(f"{database_name}/{job_id}/{ASSUMPTIONS_FILE}", record)
    return record


def record_growth(store: Any, database_name: str, job_id: str, growth: float, via: str) -> dict:
    """Record the customer's growth for the job (``decisions/assumptions.json``, #1990).

    Growth reaches only the scenario lines synthesis writes, so a change re-runs
    synthesis alone. Raises ValueError for a value out of :data:`SETTABLE`'s range."""
    return record_assumptions(store, database_name, job_id, {"growth_multiplier": growth}, via=via)
