"""One facts layer for every synthesis deliverable (#615, ADR-032).

Synthesis computes every customer-facing figure the deliverables print
(per-engine roles, table and cost figures, per-wave served/moving/kept-in-sync
counts, the table basis behind "N tables mapped", the open and resolved risk
counts, the tied-query count) exactly once, here, and writes the result to
``report.json`` as ``facts`` (``SynthesisOutputContract.facts``). The decision
report, the engineering report, the deck and its PDF, and the interactive
analysis report (the UI export) read those fields and only format them; none
of them adds, subtracts or filters a figure this module owns
(``tests/unit/report/test_renderers_only_format.py`` enforces that).

Before this module, each deliverable derived the same figure on its own, and
the copies drifted: a merged-wave headline subtracted the deduped sync list
while the "Tables migrate" tile summed the per-engine owned counts (#581), and
the deck's "Basis of the analysis" bullet trusted a partial ``table_mappings``
while the tile added the migration-wave fallback (#533).

The input is the ``report.json`` dict itself (every other field synthesis
writes), plus the migration waves it is based on -- passed in, because a
report written before ``migration_waves`` existed resolves them through
``src.report.roadmap.resolve_migration_waves``, which this layer does not
import. ``src.report.facts.report_facts`` is the one reader: it returns the
stored ``facts`` and, for a report synthesized before they existed, calls
:func:`compute_report_facts` -- the same function synthesis calls -- so even
an old report gets one answer per figure.

``src/shared`` imports nothing from ``src/agents`` or ``src/report``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.shared import catalog
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.engine_names import ENGINE_DISPLAY_NAMES
from src.shared.risk_severity import SEVERITY_ORDER, count_by_severity, severity_of
from src.shared.signal_labels import SIGNAL_NOUNS

# ---------------------------------------------------------------------------
# Engine roles
# ---------------------------------------------------------------------------

RETAINED = "Retained"
MIGRATION_TARGET = "Migration target"
CACHE_LAYER = "Cache layer"
SEARCH_READ_MODEL = "Search read model"
EVALUATED = "Evaluated"
ASSESSED = "Assessed"
# A mapped table whose engine has no role in the architecture.
OTHER_ROLE = "Other"
# Roles whose engine needs no data migration of its own.
NO_MIGRATION_ROLES = (RETAINED, CACHE_LAYER)
ROLES = (RETAINED, MIGRATION_TARGET, CACHE_LAYER, SEARCH_READ_MODEL, EVALUATED, ASSESSED)

# Search engines are read models (#303): their data is synced from the engine
# that owns each table, so they never hold system-of-record data, never need a
# "data migration", and come after their owners in the wave plan.
SEARCH_ENGINES = catalog.engine_ids(status=catalog.ACTIVE, role="search")
# Only a relational engine can be the retained, source-compatible core. Legacy ids count
# too (the generic ``aurora`` older reports carry).
RELATIONAL_ENGINES = catalog.engine_ids(status=catalog.REPORTABLE, role="relational")


def engine_role(
    engine: str,
    recommended: set,
    schema_designs: dict[str, Any],
    workload: float | None = None,
) -> str:
    """Role of an engine in the target architecture.

    ``recommended_architecture.databases`` lists only net-new migration targets —
    it drops the retained relational core and cache engines because it filters on
    a ``schema_design_available`` flag that is False for both (defect (d) for the
    cache, which has a completed design the flag does not count; by design for a
    retained engine, which has no migration design). Roles are therefore derived
    from the engine kind and its design status, not from that list alone.

    An engine carrying **no workload** is ``Evaluated``: triage selected it, analysis
    scored it, and the assignment then routed nothing to it because another engine won
    every query. It is *not* ``Retained`` — retained means the engine still serves
    queries, which is true of the source relational core and false of an engine that
    won none. Calling it Retained told the customer to provision an engine holding
    nothing, and (via ``no_move`` in ``pptx_report``) pulled the Wave 1 confidence down
    to a floor set by an engine doing no work.

    Judged on workload rather than on the engine's name because the synthesis report
    carries no ``source_engine`` field. If every query migrated off the source engine it
    would read ``Evaluated`` rather than "retired" — rare, and Evaluated still claims
    nothing false.
    """
    if engine in CACHE_ENGINES:
        return CACHE_LAYER
    if engine in SEARCH_ENGINES and workload:
        # A read model whatever its design status: never "Retained" (#296/#303)
        return SEARCH_READ_MODEL
    if engine in recommended:
        return MIGRATION_TARGET
    # Ordered deliberately: a cache engine or a real migration target keeps its role
    # whatever its workload, and this test precedes the status checks because the
    # ``skipped``/``not_available`` branch below is the one being corrected.
    if not workload:
        return EVALUATED
    status = (schema_designs.get(engine) or {}).get("status")
    if status in ("not_available", "skipped"):
        # Retained means the source-compatible relational core; any other engine
        # carrying queries without a design is still a migration target
        return RETAINED if engine in RELATIONAL_ENGINES else MIGRATION_TARGET
    if status == "completed":
        return MIGRATION_TARGET
    return ASSESSED


def design_source_tables(tables: Any) -> int:
    """Distinct source tables an engine's schema design reads from."""
    if not isinstance(tables, list):
        return 0
    return len(
        {
            str(src)
            for t in tables
            if isinstance(t, dict)
            for src in (t.get("source_tables") or [])
            if isinstance(t.get("source_tables"), list)
        }
    )


def cache_layer_counts(report: dict[str, Any], engine: str) -> tuple[int, float] | None:
    """``(cached queries, % of calls)`` of a cache-layer engine, or None (#296).

    The cache owns no query, so it has no workload share; it is described by the hot
    reads it fronts. ``None`` for a legacy report where the cache owned queries.
    """
    entry = next(
        (
            r
            for r in report.get("ranking") or []
            if isinstance(r, dict) and r.get("target") == engine
        ),
        {},
    )
    overlay = report.get("cache_overlay") or {}
    if entry.get("role") != "cache_layer" and overlay.get("engine") != engine:
        return None
    n = entry.get("cache_overlay_queries", overlay.get("query_count", 0))
    share = entry.get("cache_call_share_percent", overlay.get("call_share_percent", 0.0))
    return int(n or 0), float(share or 0.0)


def rationale_text(value: Any) -> str:
    """A rationale as text; legacy reports may carry a list of reasons or nothing."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v)
    return ""


def engine_costs(report: dict[str, Any]) -> dict[str, float]:
    """engine name -> monthly USD, from tco_analysis.cost_breakdown."""
    out: dict[str, float] = {}
    for row in (report.get("tco_analysis") or {}).get("cost_breakdown") or []:
        if not isinstance(row, dict):
            continue
        db, c = row.get("database"), row.get("monthly_cost_usd")
        if db is not None and isinstance(c, (int, float)) and not isinstance(c, bool):
            out[db] = c
    return out


# ---------------------------------------------------------------------------
# Migration waves
# ---------------------------------------------------------------------------


def is_migrating_wave(wave: dict[str, Any]) -> bool:
    """A wave that moves tables off Aurora to a purpose-built engine.

    Wave 1 (``homogeneity`` set) carries the whole collected schema to Aurora:
    it is not a purpose-built-engine move, and its tables are a superset of
    every later wave's, so it never counts towards "Tables migrate" (#321
    review, finding 4). The cache and search/analytics waves move nothing
    (``moves_from`` is empty).
    """
    return bool(wave.get("moves_from")) and wave.get("homogeneity") is None


def _owner_entries(wave: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        o
        for o in (wave.get("table_owners") or [])
        if isinstance(o, dict) and o.get("table") and o.get("owner")
    ]


def wave_facts(wave: dict[str, Any], view_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """The table figures one wave states: served, moving, kept in sync.

    ``served_tables`` is the wave's own ``table_count`` (every table its
    queries touch, #562). For a migrating wave, ``moving_tables`` is the
    tables one of the wave's own engines durably owns -- the only ones that
    actually move -- and ``kept_in_sync_tables`` is the rest, owned by an
    engine outside the wave and kept in sync from it.

    #581: for the merged DynamoDB/DocumentDB wave, ``moving_tables`` is the
    sum of ``owned_counts_by_engine`` -- the same figure the "Tables migrate"
    tile adds up -- never ``table_count - len(table_owners)``. The deduped
    ``table_owners`` also lists a table one wave engine serves while the
    other owns it (DocumentDB serving a DynamoDB table), which subtracted a
    table that does move. Such entries are ``shared_in_wave``; only an owner
    outside the wave is ``kept_in_sync``.

    ``view_ids``, when given, splits wave 1's whole-schema count into tables
    and views (#578): wave 1 carries every collected table *and* view, while
    ``source_table_count`` counts tables only.
    """
    served = int(wave.get("table_count") or 0)
    engines = [str(e) for e in (wave.get("engines") or []) if e]
    out: dict[str, Any] = {
        "wave": int(wave.get("wave") or 0),
        "engines": engines,
        "migrating": is_migrating_wave(wave),
        "served_tables": served,
        "moving_tables": None,
        "kept_in_sync_tables": None,
        "moving_by_engine": None,
        "served_by_engine": None,
        "kept_in_sync": [],
        "shared_in_wave": [],
        "views": None,
        "tables_only": None,
        "critical_high_risks": 0,
    }
    owners = _owner_entries(wave)
    outside: dict[str, dict[str, Any]] = {}
    inside: list[dict[str, Any]] = []
    for o in owners:
        if str(o["owner"]) in engines:
            inside.append(o)
        else:
            outside.setdefault(str(o["table"]), o)
    out["kept_in_sync"] = [outside[t] for t in sorted(outside)]
    out["shared_in_wave"] = sorted(inside, key=lambda o: (str(o["table"]), str(o["owner"])))
    if view_ids is not None and wave.get("homogeneity") is not None:
        views = set(view_ids)
        out["views"] = sum(1 for t in wave.get("tables") or [] if str(t) in views)
        out["tables_only"] = max(served - out["views"], 0)
    if wave.get("homogeneity") is not None:
        # Wave 1 carries the whole collected schema to Aurora: no served/moving split.
        return out
    if not is_migrating_wave(wave):
        # A read-model or cache wave moves nothing of its own; its headline
        # still says how many of its tables are kept in sync from an owner.
        out["kept_in_sync_tables"] = len(outside)
        out["moving_tables"] = max(served - len(outside), 0)
        return out
    per_served = wave.get("table_counts_by_engine")
    per_owned = wave.get("owned_counts_by_engine")
    if isinstance(per_served, dict) and isinstance(per_owned, dict) and per_owned:
        moving_by_engine = {e: int(per_owned.get(e) or 0) for e in engines}
        served_by_engine: dict[str, int] | None = {e: int(per_served.get(e) or 0) for e in engines}
        moving = sum(moving_by_engine.values())
    elif len(engines) == 1:
        moving = max(served - len(outside), 0)
        moving_by_engine = {engines[0]: moving}
        served_by_engine = {engines[0]: served}
    else:
        # A multi-engine wave written before the per-engine split existed:
        # the wave-level figure is still right, the per-engine one is unknown.
        moving = max(served - len(outside), 0)
        moving_by_engine = {}
        served_by_engine = None
    out["moving_tables"] = moving
    out["kept_in_sync_tables"] = max(served - moving, 0)
    out["moving_by_engine"] = moving_by_engine or None
    out["served_by_engine"] = served_by_engine
    return out


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


def engine_facts(report: dict[str, Any], waves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The full target architecture, one entry per engine, ordered by workload.

    Derived from ``ranking`` (every engine that carries workload) joined with
    ``tco_analysis.cost_breakdown``, ``recommended_architecture.databases`` (source
    table counts for migration targets) and ``schema_designs`` (design status and
    cache-object counts). This is the picture synthesis actually computed;
    ``recommended_architecture.databases`` alone is a lossy projection of it that
    neither reconciles to the cost total nor matches the executive summary.

    A migration target with no completed schema design yet (``table_mappings``
    and ``databases[].table_count`` stay 0) reads its tables from its migration
    wave instead (#524): ``source_tables`` is the wave's served count, and
    ``tables_migrate`` only the subset the engine durably owns (#568 review) --
    from ``owned_counts_by_engine`` for the merged DynamoDB/DocumentDB wave,
    never its union ``table_count`` credited to both engines.
    """
    ranking = [r for r in (report.get("ranking") or []) if isinstance(r, dict)]
    arch = report.get("recommended_architecture") or {}
    dbs = [d for d in (arch.get("databases") or []) if isinstance(d, dict)]
    recommended = {d.get("service") for d in dbs}
    src_tables = {d.get("service"): d.get("table_count") for d in dbs}
    schema_designs = report.get("schema_designs") or {}
    costs = engine_costs(report)

    wave_served: dict[str, int] = {}
    wave_owned: dict[str, int] = {}
    for w in waves:
        wf = wave_facts(w)
        for e, k in (wf["served_by_engine"] or {}).items():
            wave_served[e] = wave_served.get(e, 0) + k
        for e, k in (wf["moving_by_engine"] or {}).items():
            wave_owned[e] = wave_owned.get(e, 0) + k

    rows: list[dict[str, Any]] = []
    for r in ranking:
        eng = r.get("target")
        if not eng:
            continue
        workload = r.get("workload_percent")
        role = engine_role(eng, recommended, schema_designs, workload)
        design = schema_designs.get(eng) or {}
        design_tables = design.get("tables")
        objs = len(design_tables) if isinstance(design_tables, list) else None
        cached = cache_layer_counts(report, eng) if role == CACHE_LAYER else None
        source_tables: int | None = None
        shared_tables: int | None = None
        migrates = 0
        if role == MIGRATION_TARGET:
            n = src_tables.get(eng)
            has_design = design.get("status") == "completed"
            owned: int | None = None
            if not n and not has_design:
                wave_n = wave_served.get(eng)
                n = wave_n if isinstance(wave_n, int) and wave_n > 0 else n
                owned = wave_owned.get(eng)
            migrates = (
                owned if isinstance(owned, int) else (n if isinstance(n, int) else (objs or 0))
            )
            source_tables = n if isinstance(n, int) else None
            served = design_source_tables(design_tables)
            if source_tables is not None and served > source_tables:
                shared_tables = served
        rows.append(
            {
                "engine": str(eng),
                "role": role,
                # The cache layer owns no query: no workload share, its cached reads
                # and their share of calls instead (#296)
                "workload_percent": (
                    None
                    if cached
                    else (
                        float(workload)
                        if isinstance(workload, (int, float)) and not isinstance(workload, bool)
                        else None
                    )
                ),
                "cached_queries": cached[0] if cached else None,
                "cached_call_share_percent": cached[1] if cached else None,
                "engine_source_tables": source_tables,
                "shared_source_tables": shared_tables,
                "target_objects": objs,
                "tables_migrate": int(migrates or 0),
                "monthly_cost_usd": costs.get(eng),
                # Synthesis's per-engine rationale, built from the routed workload
                # (#152); the ranking entry carries it for engines `databases` omits
                "rationale": rationale_text(
                    next((d.get("rationale") for d in dbs if d.get("service") == eng), None)
                    or r.get("rationale")
                ),
            }
        )
    rows.sort(
        key=lambda x: x["workload_percent"] if x["workload_percent"] is not None else -1,
        reverse=True,
    )
    return rows


# ---------------------------------------------------------------------------
# Table basis (#530, #533)
# ---------------------------------------------------------------------------

# "Analyzed 311 source tables and 1654 query patterns across 5 target
# database(s)." -- ``build_summary``'s own fixed, deterministic opening
# sentence. Read only for a report old enough to carry neither ``facts`` nor
# ``source_table_count``.
ANALYZED_SOURCE_TABLES = re.compile(r"\bAnalyzed (\d+) source tables?\b")


def _tables_analyzed(report: dict[str, Any], waves: list[dict[str, Any]]) -> int:
    count = report.get("source_table_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    m = ANALYZED_SOURCE_TABLES.search(str(report.get("summary_deterministic") or ""))
    if m:
        return int(m.group(1))
    tables: set[str] = set()
    for w in waves:
        tables.update(str(t) for t in (w.get("tables") or []) if t)
    return len(tables)


def table_basis(
    report: dict[str, Any], engines: list[dict[str, Any]], waves: list[dict[str, Any]]
) -> dict[str, Any]:
    """The figure behind the deck's "N tables mapped to a named target" (#530, #533).

    ``design_coverage`` says how much of it a completed schema design backs:

    - ``complete``: every migration target has a completed design, so
      ``table_mappings`` is the precise per-table count (each with its own
      fit score) and ``basis_table_count`` is its length;
    - ``partial``: only ``designed_engines`` have one (#533). ``table_mappings``
      covers those engines only, so its length is *not* the count: the
      "Tables migrate" tile also adds the wave fallback of every
      ``pending_engines`` entry. ``basis_table_count`` is then the tables analyzed, as
      with no design at all, and ``mapped_tables`` how many of them carry a
      per-table fit score;
    - ``none``: no design ran; ``basis_table_count`` is the tables analyzed
      (``source_table_count``, else the summary's own figure, else the
      union of the waves' tables).
    """
    mappings = [m for m in (report.get("table_mappings") or []) if isinstance(m, dict)]
    roles = {e["engine"]: e["role"] for e in engines}
    # The migration map's own split ("21 migrate, 1 to the cache layer", #258):
    # a table mapped to the cache layer needs no data migration.
    by_role: dict[str, int] = {}
    for m in mappings:
        role = roles.get(str(m.get("recommended_database") or ""), OTHER_ROLE)
        if role not in (MIGRATION_TARGET, CACHE_LAYER, RETAINED, SEARCH_READ_MODEL):
            role = OTHER_ROLE
        by_role[role] = by_role.get(role, 0) + 1
    schema_designs = report.get("schema_designs") or {}
    targets = [e["engine"] for e in engines if e["role"] == MIGRATION_TARGET]
    designed = [e for e in targets if (schema_designs.get(e) or {}).get("status") == "completed"]
    pending = [e for e in targets if e not in designed]
    if mappings and not pending:
        return {
            "basis_table_count": len(mappings),
            "mapped_tables": len(mappings),
            "design_coverage": "complete",
            "designed_engines": designed,
            "pending_engines": [],
            "mapped_tables_by_role": by_role,
        }
    return {
        "basis_table_count": _tables_analyzed(report, waves),
        "mapped_tables": len(mappings),
        "design_coverage": "partial" if mappings else "none",
        "designed_engines": designed if mappings else [],
        "pending_engines": pending if mappings else targets,
        "mapped_tables_by_role": by_role,
    }


# ---------------------------------------------------------------------------
# Risks
# ---------------------------------------------------------------------------


def risk_engine_and_body(desc: Any) -> tuple[str, str]:
    """Split a risk description into its ``[engine]`` prefix and the remaining text.

    Risks carry no explicit engine field; the engine is encoded as a leading
    ``[documentdb]`` / ``[elasticache]`` tag in the description. Returns
    ``(engine, body)`` with ``engine`` defaulting to ``"(general)"``.
    """
    s = str(desc or "").strip()
    engine = "(general)"
    if s.startswith("["):
        j = s.find("]")
        if j != -1:
            engine = s[1:j].strip() or "(general)"
            s = s[j + 1 :].strip()
    return engine, s


def risk_has_content(desc: Any) -> bool:
    """False for a risk description with no real text after its ``[engine]``
    prefix (or only the historical ``unknown:`` placeholder, #210)."""
    _, body = risk_engine_and_body(desc)
    if body.lower().startswith("unknown:"):
        body = body[len("unknown:") :].strip()
    return bool(body)


def open_risks(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The one open-risk list every deliverable renders and counts from (#201)."""
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("risks") or [])
        if isinstance(r, dict) and risk_has_content(r.get("description"))
    ]


def resolved_risk_list(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Analysis risks the effective assignment resolved (``risk_assessment.resolved_risks``)."""
    risk = report.get("risk_assessment") or {}
    return [
        r
        for r in (risk.get("resolved_risks") or [])
        if isinstance(r, dict) and risk_has_content(r.get("description"))
    ]


def risk_facts(report: dict[str, Any]) -> dict[str, Any]:
    """Open/resolved risk counts, by severity, by type and by engine."""
    risks = open_risks(report)
    by_type: dict[str, int] = {}
    crit_high_by_engine: dict[str, int] = {}
    crit_high_types: set[str] = set()
    for r in risks:
        t = str(r.get("risk_type") or "other").replace("_", " ").lower()
        by_type[t] = by_type.get(t, 0) + 1
        # #270: CRITICAL is at least as severe as HIGH, so every "needs
        # escalating" count includes it.
        if severity_of(r) in ("CRITICAL", "HIGH"):
            crit_high_types.add(str(r.get("risk_type") or ""))
            engine, _ = risk_engine_and_body(r.get("description"))
            if engine != "(general)":
                crit_high_by_engine[engine] = crit_high_by_engine.get(engine, 0) + 1
    by_severity = count_by_severity(risks)
    return {
        "open_risk_count": len(risks),
        "open_risks_by_severity": {
            k: by_severity[k] for k in (*SEVERITY_ORDER, "UNRATED") if by_severity.get(k)
        },
        "critical_high_risk_count": sum(by_severity.get(k, 0) for k in ("CRITICAL", "HIGH")),
        "critical_high_risks_by_engine": dict(sorted(crit_high_by_engine.items())),
        "critical_high_risk_types": sorted(crit_high_types),
        # "All the same root cause" is claimed only when the CRITICAL/HIGH
        # risks share exactly one risk_type.
        "critical_high_root_cause": (
            next(iter(crit_high_types)) or None if len(crit_high_types) == 1 else None
        ),
        "open_risks_by_type": dict(sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0]))),
        "resolved_risk_count": len(resolved_risk_list(report)),
    }


# ---------------------------------------------------------------------------
# Labels and reasons (#584, #546)
# ---------------------------------------------------------------------------

# Reason and trade-off text names engines by display name ("Aurora MySQL") or,
# in reports written before #572, by raw id ("aurora_mysql"); both map back to
# the id. Text is read here, once, for the reality check's own consolidation
# lines and for an older report's override reasons -- never by a renderer.
ENGINE_BY_NAME = {
    **{v: k for k, v in ENGINE_DISPLAY_NAMES.items()},
    **{k: k for k in ENGINE_DISPLAY_NAMES},
}
_ENGINE_NAME_ALT = "|".join(re.escape(n) for n in sorted(ENGINE_BY_NAME, key=len, reverse=True))
# A "Consolidated N queries from <engine> -> <absorber>: ... no in-scope
# <engine> query remains ..." trade-off line (reality_check_patterns.
# _build_recommendations / reality_check_records.reconcile_consolidations): the
# "no in-scope ... remains" clause is reconcile_consolidations' own marker for a
# full elimination.
FULLY_ELIMINATED_RE = re.compile(rf"no in-scope ({_ENGINE_NAME_ALT}) query remains")
CONSOLIDATED_FROM_TO_RE = re.compile(
    rf"^Consolidated \d+ quer(?:y|ies) from ({_ENGINE_NAME_ALT}) → ({_ENGINE_NAME_ALT})"
)
_SIGNAL_BY_NAME = {**{v: k for k, v in SIGNAL_NOUNS.items()}}
# "signal override: <signal> → <engine>", the signal and the engine either raw
# ids (before #584) or readable names.
_SIGNAL_OVERRIDE_RE = re.compile(
    rf"signal override:\s*([^→|]+?)\s*(?:→|->)\s*({_ENGINE_NAME_ALT})(?![\w])"
)


def engine_id_from_name(name: str) -> str:
    """``Aurora MySQL`` or ``aurora_mysql`` -> ``aurora_mysql``."""
    return ENGINE_BY_NAME.get(name, name)


def _signal_id(name: str) -> str:
    name = name.strip()
    return _SIGNAL_BY_NAME.get(name, name.replace(" ", "_"))


def signal_overrides_from_reasons(report: dict[str, Any]) -> dict[str, list[str]]:
    """engine -> the triage signals that overrode queries onto it, read from the
    ranking's reason summaries: only for a report written before
    ``facts.engines[].signal_overrides`` existed. Synthesis passes the
    assignment's own ``signal_override`` fields instead (#584)."""
    out: dict[str, set[str]] = {}
    for r in report.get("ranking") or []:
        if not isinstance(r, dict):
            continue
        reasons = r.get("assignment_reason_summary") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        for reason in reasons:
            for m in _SIGNAL_OVERRIDE_RE.finditer(str(reason)):
                engine = engine_id_from_name(m.group(2))
                out.setdefault(engine, set()).add(_signal_id(m.group(1)))
    return {e: sorted(v) for e, v in out.items()}


def final_engines(engines: list[dict[str, Any]]) -> set[str]:
    """Engines in the final architecture: every engine the effective assignment
    gives work (owners) plus the cache layer. An ``Evaluated`` engine won
    nothing."""
    return {e["engine"] for e in engines if e["role"] != EVALUATED}


def _trade_off_engines(desc: str) -> tuple[str | None, str | None, bool]:
    """(from engine, to engine, fully eliminated) a reality-check line names."""
    m = CONSOLIDATED_FROM_TO_RE.match(desc)
    elim = FULLY_ELIMINATED_RE.search(desc)
    src = engine_id_from_name(m.group(1)) if m else None
    dst = engine_id_from_name(m.group(2)) if m else None
    if elim and not src:
        src = engine_id_from_name(elim.group(1))
    return src, dst, bool(elim)


def reality_check_trade_off_facts(report: dict[str, Any], final: set[str]) -> list[dict[str, Any]]:
    """Every reality-check trade-off, with the engines it names and whether a
    later assignment edit reversed it (#546).

    The reality check writes its consolidation lines once, against the
    assignment it settled. A customer edit can then move queries back onto
    an engine it removed; the line still says that engine "leaves the
    architecture" and what removing it saves. ``reversed`` is true for a
    consolidation line whose source engine is back in the final architecture:
    no deliverable states it as a current decision.
    """
    out: list[dict[str, Any]] = []
    for i, t in enumerate(report.get("trade_offs") or []):
        if not isinstance(t, dict):
            continue
        desc = str(t.get("description") or "")
        src, dst, _ = _trade_off_engines(desc)
        # A report written before trade-offs carried an engine still has the
        # reality check's consolidation lines: recognised by their own shape.
        if t.get("engine") != "reality-check" and not (t.get("engine") is None and src):
            continue
        out.append(
            {
                "index": i,
                "from_engine": src,
                "to_engine": dst,
                "reversed": bool(src and src in final),
            }
        )
    return out


_KEPT_RE = re.compile(rf"^({_ENGINE_NAME_ALT}) kept: ")
_PATTERN_PREFIX = "Recommended pattern: "


def reality_check_decision_facts(
    report: dict[str, Any],
    trade_offs: list[dict[str, Any]],
    removed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The reality-check trade-offs that still apply, as every deliverable shows them (#595).

    The reality check's own lines (consolidations, the sanity sweep's
    kept-engine verdicts, #593, and recommended patterns) used to reach only
    the engineering report: the decision report, the deck and the UI's
    trade-offs tab read trade-offs per schema-design engine, and
    "reality-check" is not one. Each entry here carries its ``kind``, the
    ``engine`` it is about, a short ``title`` and the ``detail``, split once
    from the line's own text; a renderer only formats them.
    ``with_removed_engine`` marks a consolidation of an engine
    ``removed_engines`` already lists with this same line as its reason.
    A reversed line (#546) is not here at all.
    """
    rows = report.get("trade_offs") or []
    removed_set = {e["engine"] for e in removed}
    out: list[dict[str, Any]] = []
    for t in trade_offs:
        if t["reversed"]:
            continue
        entry = rows[t["index"]]
        desc = str(entry.get("description") or "").strip()
        consolidated = CONSOLIDATED_FROM_TO_RE.match(desc)
        kept = _KEPT_RE.match(desc)
        if consolidated:
            kind, engine = "consolidation", t["from_engine"]
            title, detail = consolidated.group(0), desc[consolidated.end() :]
        elif kept:
            kind, engine = "kept", engine_id_from_name(kept.group(1))
            title, detail = f"{kept.group(1)} kept", desc[kept.end() :]
        elif desc.startswith(_PATTERN_PREFIX):
            kind, engine = "pattern", None
            name, _, rest = desc[len(_PATTERN_PREFIX) :].partition(". ")
            title, detail = _PATTERN_PREFIX + name.rstrip("."), rest
        else:
            kind, engine, title, detail = "other", t["from_engine"], desc, ""
        out.append(
            {
                "index": t["index"],
                "kind": kind,
                "engine": engine,
                "title": title.strip(),
                "detail": detail.strip(" :"),
                "query_ids": [str(q) for q in entry.get("query_ids") or []],
                "with_removed_engine": kind == "consolidation" and engine in removed_set,
            }
        )
    return out


def removed_engine_facts(
    report: dict[str, Any], final: set[str], trade_offs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Engines the reality check removed, each with where its queries went and why.

    #547: an engine counts as removed when it owned queries before the reality
    check (``before_distribution``), a full consolidation moved it out, or the
    TCO lists its cost as already removed -- and it is not in the final
    architecture. #546: the final architecture is the effective assignment's
    (the ranking), not the reality check's own ``after_distribution``, so an
    engine a customer edit moved queries back onto is never listed. A trade-off
    line carrying the "no in-scope X query remains" marker also counts, for
    reports written before the distributions were recorded. A partial
    consolidation leaves its engine in the architecture, so it is never listed.
    """
    rc = report.get("reality_check") or {}
    after = rc.get("after_distribution") or {}
    cache = (report.get("cache_overlay") or {}).get("engine")
    rc_lines = [
        (str((report.get("trade_offs") or [])[t["index"]].get("description") or ""), t)
        for t in trade_offs
    ]
    candidates: list[str] = []
    marked: set[str] = set()
    for desc, _t in rc_lines:
        src, _, elim = _trade_off_engines(desc)
        if src:
            candidates.append(src)
        if elim and src:
            marked.add(src)
    consolidations = [c for c in rc.get("consolidations") or [] if isinstance(c, dict)]
    candidates += [
        str(c.get("from_engine") or "") for c in consolidations if c.get("action") == "full"
    ]
    candidates += sorted(e for e, n in (rc.get("before_distribution") or {}).items() if n)
    eliminated = [
        str(row.get("database") or "")
        for row in (report.get("tco_analysis") or {}).get("eliminated_engine_costs") or []
        if isinstance(row, dict)
    ]
    candidates += eliminated
    marked |= set(eliminated)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in candidates:
        if not e or e in seen or e == cache or e in final or after.get(e):
            continue
        if not after and e not in marked:
            # No distributions recorded (an older report): only the marker or
            # an eliminated cost says an engine left; a bare line does not.
            continue
        seen.add(e)
        reasons: list[str] = []
        moved_to: list[str] = []
        for desc, t in rc_lines:
            if t["from_engine"] != e:
                continue
            if desc not in reasons:
                reasons.append(desc)
            if t["to_engine"] and t["to_engine"] not in moved_to:
                moved_to.append(t["to_engine"])
        for c in consolidations:
            if c.get("from_engine") == e:
                to = str(c.get("to_engine") or "")
                if to and to not in moved_to:
                    moved_to.append(to)
                if not reasons and c.get("reason"):
                    reasons.append(str(c["reason"]))
        out.append({"engine": e, "moved_to": moved_to, "reasons": reasons})
    return out


# ---------------------------------------------------------------------------
# The whole facts block
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Per-query decision traces (#339)
# ---------------------------------------------------------------------------

# The rules that pin a query by a listed rule rather than by its score:
# full-text search. Deterministic, so counted apart from a customer's pick,
# its group propagation and a design review's flag (recognised by actor).
_PINNING_RULES = frozenset({"override.text_search"})

# At most this many listed queries carry their full trace in report.json and
# the engineering report's appendix (each is about 3.4 KB and 9 lines); the
# rest say only why they are listed (#339 review).
TRACE_FULL_CAP = 200


def trace_kinds(trace: Mapping[str, Any]) -> dict[str, bool]:
    """Why a query's decision trace is listed (``QueryTraceListing``).

    ``moved``: a later step changed its engine (even if a still later one
    moved it back); ``tied``: it was tied at some step, settled or not;
    ``open_tie``: the tie is still unresolved at the end; ``overridden``: a
    customer or a design review (actor ``model_flag``) placed it; ``pinned``:
    a listed rule (full-text search) placed it. ``trace`` is a
    ``QueryTraceResponse`` as JSON. Synthesis selects the queries
    ``query_traces`` lists with this, and the facts layer counts them with
    it, so the two never disagree.
    """
    first = trace.get("first_assignment")
    later = [m for m in trace.get("moves") or [] if isinstance(m, dict)]
    steps = ([first] if isinstance(first, dict) else []) + later
    open_tie = bool(trace.get("tied_engines"))
    return {
        "moved": any(
            m.get("from_engine") and m.get("from_engine") != m.get("to_engine") for m in later
        ),
        "tied": open_tie or any(s.get("tied_engines") for s in steps),
        "open_tie": open_tie,
        "overridden": any(s.get("actor") not in (None, "rule") for s in steps),
        "pinned": any(s.get("reason_code") in _PINNING_RULES for s in steps),
    }


def is_listed(kinds: Mapping[str, bool]) -> bool:
    """Whether a query with these ``trace_kinds`` is listed."""
    return any(kinds.get(k) for k in ("moved", "tied", "overridden", "pinned"))


def trace_order(row: Mapping[str, Any]) -> tuple[bool, bool, bool, bool, str]:
    """Most notable first: overridden, an open tie, pinned, moved, then by query id."""
    return (
        not row.get("overridden"),
        not row.get("open_tie"),
        not row.get("pinned"),
        not row.get("moved"),
        str(row.get("query_id")),
    )


_KIND_KEYS = ("moved", "tied", "open_tie", "overridden", "pinned")


def query_trace_facts(report: dict[str, Any]) -> dict[str, Any] | None:
    """The figures of ``report["query_traces"]`` (``QueryTraceFacts``), or None without it."""
    digest = report.get("query_traces")
    if not isinstance(digest, dict):
        return None
    full = [t for t in digest.get("queries") or [] if isinstance(t, dict) and t.get("query_id")]
    rows: list[dict[str, Any]] = [
        {"query_id": str(t["query_id"]), **trace_kinds(t), "in_full": True} for t in full
    ]
    rows += [
        {
            "query_id": str(m["query_id"]),
            **{k: bool(m.get(k)) for k in _KIND_KEYS},
            "in_full": False,
        }
        for m in digest.get("more_listed") or []
        if isinstance(m, dict) and m.get("query_id")
    ]
    rows.sort(key=trace_order)
    total = digest.get("query_count")
    total = total if isinstance(total, int) and not isinstance(total, bool) else len(rows)
    in_full = sum(1 for row in rows if row["in_full"])
    return {
        "has_ledger": bool(digest.get("has_ledger")),
        "traced_query_count": total,
        "listed_query_count": len(rows),
        "full_trace_cap": TRACE_FULL_CAP,
        "full_trace_count": in_full,
        "not_in_full_count": len(rows) - in_full,
        "moved_query_count": sum(1 for row in rows if row["moved"]),
        "ever_tied_query_count": sum(1 for row in rows if row["tied"]),
        "open_tie_query_count": sum(1 for row in rows if row["open_tie"]),
        "overridden_query_count": sum(1 for row in rows if row["overridden"]),
        "pinned_query_count": sum(1 for row in rows if row["pinned"]),
        "unlisted_query_count": max(total - len(rows), 0),
        "listed_queries": rows,
        "unlisted_rules": [
            {
                "rule": str(r.get("rule")),
                "label": str(r.get("label") or r.get("rule")),
                "rule_query_count": int(r.get("query_count") or 0),
            }
            for r in digest.get("unlisted_rules") or []
            if isinstance(r, dict)
        ],
    }


def _unrounded(x: float) -> float:
    """A sum of percentages, kept at full precision: each renderer rounds it
    once, to the places it prints (``99.45`` prints "99%", never a second
    rounding to "100%"). Only float noise (``17.460499999999996``) is cut."""
    return round(x, 9)


def projected_monthly_cost(report: dict[str, Any]) -> float:
    """The headline projected monthly cost (#358).

    ``tco_analysis.projected_monthly_cost`` when synthesis wrote it -- one
    source of truth, never a sum recomputed from ``cost_breakdown`` (which can
    still list an eliminated engine). A report written before that field
    existed sums the kept engines' rows: the reality check's owners
    (``after_distribution``) plus the cache layer, the set the interactive
    report summed before the facts layer existed.
    """
    reported = (report.get("tco_analysis") or {}).get("projected_monthly_cost")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported)
    kept = set((report.get("reality_check") or {}).get("after_distribution") or {})
    cache = (report.get("cache_overlay") or {}).get("engine")
    if cache:
        kept.add(cache)
    return round(sum(c for e, c in engine_costs(report).items() if e in kept), 2)


def compute_report_facts(
    report: dict[str, Any],
    waves: list[dict[str, Any]] | None = None,
    view_ids: Iterable[str] | None = None,
    signal_overrides: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """Every customer-facing figure, computed once (``SynthesisOutputContract.facts``).

    ``waves`` defaults to ``report["migration_waves"]``; a renderer passes the
    resolved (legacy-fallback) waves for a report written before that field.
    ``view_ids``, the collected view ids, lets wave 1 state its tables and
    views separately (#578); without it (an older report) the view count is
    what wave 1 carries beyond ``source_table_count``. ``signal_overrides``
    (engine -> the triage signals the assignment's ``signal_override`` fields
    name) is what synthesis passes; an older report reads them from the
    ranking's reason text instead (#584).
    """
    if waves is None:
        waves = [w for w in (report.get("migration_waves") or []) if isinstance(w, dict)]
    views = set(view_ids) if view_ids is not None else None
    engines = engine_facts(report, waves)
    overrides = (
        {e: sorted(set(v)) for e, v in signal_overrides.items()}
        if signal_overrides is not None
        else signal_overrides_from_reasons(report)
    )
    for e in engines:
        e["signal_overrides"] = list(overrides.get(e["engine"], []))
    final = final_engines(engines)
    rc_trade_offs = reality_check_trade_off_facts(report, final)
    n_trade_offs = sum(1 for t in report.get("trade_offs") or [] if isinstance(t, dict))
    removed = removed_engine_facts(report, final, rc_trade_offs)
    wave_rows = [wave_facts(w, views) for w in waves]
    tables_analyzed = report.get("source_table_count")
    if not isinstance(tables_analyzed, int) or isinstance(tables_analyzed, bool):
        tables_analyzed = None
    if views is None and tables_analyzed is not None:
        for row, w in zip(wave_rows, waves, strict=True):
            if w.get("homogeneity") is not None:
                row["views"] = max(row["served_tables"] - tables_analyzed, 0)
                row["tables_only"] = row["served_tables"] - row["views"]
    view_count = next((row["views"] for row in wave_rows if row["views"] is not None), None)
    migration_total = sum(e["tables_migrate"] for e in engines if e["role"] == MIGRATION_TARGET)
    wave_total = sum(row["moving_tables"] or 0 for row in wave_rows if row["migrating"])
    tables_migrate = migration_total or wave_total
    costs = [e["monthly_cost_usd"] for e in engines if e["monthly_cost_usd"] is not None]
    tied = [t for t in (report.get("tied_queries") or []) if isinstance(t, dict)]
    risks = risk_facts(report)
    by_engine = risks["critical_high_risks_by_engine"]
    for row in wave_rows:
        row["critical_high_risks"] = sum(by_engine.get(e, 0) for e in row["engines"])
    # The engines that need no data migration (the retained relational core and
    # the cache layer): the deck's "no-migration step" card and summary footer.
    no_move = [e for e in engines if e["role"] in NO_MIGRATION_ROLES]
    no_migration = {
        "engines": [e["engine"] for e in no_move],
        "workload_percent": _unrounded(
            sum(e["workload_percent"] or 0.0 for e in no_move if e["cached_queries"] is None)
        ),
        "cached_call_share_percent": _unrounded(
            sum(e["cached_call_share_percent"] or 0.0 for e in no_move)
        ),
        "critical_high_risks": sum(by_engine.get(e["engine"], 0) for e in no_move),
    }
    return {
        "projected_monthly_cost_usd": projected_monthly_cost(report),
        "source_table_count": tables_analyzed,
        "source_view_count": view_count,
        "engine_count": len(engines),
        "migration_wave_count": len(wave_rows),
        "engines": engines,
        "tables_migrate": tables_migrate,
        "workload_total_percent": _unrounded(sum(e["workload_percent"] or 0.0 for e in engines)),
        "engine_cost_total_usd": round(sum(costs), 2),
        "table_basis": table_basis(report, engines, waves),
        "waves": wave_rows,
        "no_migration": no_migration,
        "removed_engines": removed,
        "reality_check_decisions": reality_check_decision_facts(report, rc_trade_offs, removed),
        "reality_check_trade_offs": rc_trade_offs,
        "trade_off_count": n_trade_offs - sum(1 for t in rc_trade_offs if t["reversed"]),
        **risks,
        "tied_query_count": len(tied),
        # #629: queries the post-schema router moved after the customer approved.
        "post_schema_reroute_count": sum(
            1 for r in report.get("post_schema_reroutes") or [] if isinstance(r, dict)
        ),
        # #339: the per-query decision traces' figures.
        "query_trace": query_trace_facts(report),
    }
