"""Migration waves (#225, reordered relational-first by #321): the incremental
roadmap, written once by synthesis.

The target architecture synthesis recommends can be fully decomposed, but a
customer needs an incremental path to get there, and the waves are how every
deliverable presents it — as one suggested path, not the only one (every
deliverable also offers the direct, single-step option: adopt the fully
decomposed target architecture shown elsewhere in the same report). The order
is one documented, deterministic rule — a model may explain a wave, it never
decides the sequence:

1. **Move to Aurora.** The whole source database moves to the source-compatible
   Aurora engine first (MySQL/MariaDB -> Aurora MySQL, PostgreSQL -> Aurora
   PostgreSQL), schema carried over 1:1 (every collected table and view, #321:
   this is the lowest-risk, closest-match step, so it goes first instead of
   last). The wave states how homogeneous the move is from what the collector
   actually reported — engine and version only; the collector never reports
   feature or extension compatibility, so none is claimed: ``homogeneous``
   when the source maps to a compatible Aurora engine, ``cross_engine`` (#381:
   titled "Move off <source> to <engine> (cross-engine)", the chosen engine
   named and given its share, the gate flagging schema and query translation
   as a risk to validate) when the source has no Aurora dialect of its own
   (SQL Server, Oracle, DB2) but the assignment resolver still picked one of
   the two competing Aurora engines, and ``heterogeneous`` (titled "Move off
   <source> (heterogeneous)", flagged as a risk in the gate, no Aurora engine
   named and no share claimed) when no Aurora engine was picked at all. When the
   customer's engine scope is why (#1369: it has no Aurora engine, or its Aurora
   engine serves no query, including one with no analysis), the wave is titled "No
   move to Aurora: ..." instead: the source is not converted to Aurora in this plan,
   no Aurora pick is asked for, and the gate names the engines the plan moves to
   (``homogeneity`` stays ``heterogeneous``, the marker of a wave 1 with no Aurora
   engine).
   Skipped when there is no source engine to report at all, or the source is
   already an Aurora engine. For a homogeneous or cross-engine move, ``query_count`` and
   ``workload_share_percent`` are the share still assigned to Aurora once
   every later wave has moved its own share away — the same figures the
   pre-#321 "retained" wave reported as its own, separate, final wave;
   ``cutover_query_count`` is every in-scope query, since the whole workload
   runs on Aurora the moment this wave finishes, before any later wave moves
   its share away. Every later wave's ``table_count`` is a subset of this
   wave's tables, moving on from Aurora, never an addition to them.
2. **Cache.** ElastiCache cache-aside in front of Aurora (``fronts`` the
   retained Aurora engine, not the legacy source database, #321 — Aurora has
   already absorbed the schema in wave 1), fronting the hot reads from the
   cache overlay (:mod:`.cache_overlay`). No data migration (``moves_from`` is
   always empty), fully reversible. Skipped when no cache overlay is
   recommended. ElastiCache owns no query and is never a system of record.
3. **Key-value and point lookups -> DynamoDB, and/or document-shaped data ->
   DocumentDB.** One wave for both when both are routed (#321 review: the
   maintainer's own plan is "cache + DynamoDB or cache + DocumentDB", not a
   separate wave each), each engine's own rationale and gate text kept
   distinct inside the shared wave rather than blended into one. DynamoDB's
   share is table group by table group, respecting co-dependency groups
   (queries sharing a significant JOIN, ``assignment.co_dependency_groups``):
   a group that touches a DynamoDB table moves as one group, so co-dependent
   tables never split across groups where that is possible. Every
   DynamoDB-assigned query that touches an owned table is counted in exactly
   one group (the first whose tables it touches); a query touching none of
   DynamoDB's own tables is not in any group. A table this wave's queries
   still share with another wave, in either direction (another engine's
   query still reading a table DynamoDB owns, or a DynamoDB-assigned query
   still reading a table another wave owns), says so in DynamoDB's own gate
   text, naming the dual-read requirement: "until wave N" only when N is a
   later wave than this one, and permanent phrasing ("which remain on
   <engine>") when the other engine's own wave already ran (Aurora's queries,
   wave 1, never move again). ``moves_from`` is the retained Aurora engine
   (#321 — the data is already there after wave 1), not the legacy source
   database.
4. **Any other direct migration target** this rule does not otherwise name
   (forward compatible with an engine added later), ordered by workload share.
5. **Search / analytics read models -> OpenSearch.** OpenSearch never owns a
   write or the sole copy of a table: every table it serves keeps a durable
   owner, synced by zero-ETL, OpenSearch Ingestion or CDC, and its recovery
   path is always to re-index — never a data migration. A table whose owner
   is itself OpenSearch (an upstream data error, #317) is reassigned to the
   next real owner in its ``engines`` list, or the retained engine as a last
   resort. When a query's indexed tables cannot be resolved from the SQL at
   all, the wave says so explicitly and falls back to the retained/source
   engine as the owner of record rather than silently showing no owner.

A wave with nothing to move (no table, no query, no cached read) is skipped;
the remaining waves are numbered consecutively from 1.

Every wave but the Aurora move, the cache and the search/analytics read model
moves data away from the retained Aurora engine once wave 1 has reached it,
never from the legacy source database it has already left (``moves_from`` is
the retained engine, ``SOURCE_ENGINE_TO_AURORA[source_engine]``, when one
exists; the legacy source engine itself only when it does not — a
heterogeneous source, or wave 1 skipped outright for lack of any source
engine to report). The Aurora wave itself, and the cache wave whenever there
is no retained engine to front instead (a heterogeneous source — wave 1 still
runs, it just names no Aurora engine — or no source engine at all), name the
legacy source engine (``source_engine``, e.g. ``"mysql"``), rendered as "the
source MySQL database" (:func:`src.shared.engine_names.display_source_database`).

A wave is scoped to tables and views the collector actually saw
(``known_tables``, synthesis's own set): a parser artifact (a CTE alias, a
keyword, a system catalog name) never reaches a wave or a deliverable.

An owner wave's ``tables`` (#562) is every table its own engine *serves* --
primary or secondary (``_served_tables``, built from
``table_assignments.derive_table_assignments``'s own ``engines`` list,
which already names every engine with an assigned query referencing a
table) -- never only the tables where it is the table-level ``primary_engine``
plurality winner (``_owned_tables``). ADR-031 Decision 1: the query is the
unit of assignment, and a table's ``primary_engine`` is a summary label, not
ownership of the work a wave moves. A served table this wave's engine does
not durably own is named in ``table_owners`` with its real system of record
and ``"CDC"`` as the sync mechanism (the dual-read gate text already uses
this word); a table absent from ``table_owners`` is this wave's own, the
same convention the search/analytics wave already used before #562. The
dual-read gate text itself (``_table_group_coverage_note``) runs for every
owner wave now, not DynamoDB's alone, built from the OWNED subset of the
wave's tables specifically (never the full served set), so the forward
clause ("stays dual-read by <other engine>'s queries") and the reverse
clause ("<other engine> owns and keeps owning; keep a <this engine> copy
in sync via CDC") each still say the one direction they are true of
(#568 review). Table-level ``primary_engine`` stays the right tool for what
it was always right for: naming the system of record in sync/dual-read
prose, and per-table schema placement -- never for scoping which tables, or
how many queries, a wave counts as its own.

For the merged DynamoDB/DocumentDB wave only, ``table_count`` is the union
of both engines' served tables and cannot be split back into each engine's
own share; ``table_counts_by_engine`` and ``owned_counts_by_engine`` (#568
review) give each engine's own served and owned counts, so a consumer
(``src.report.architecture._architecture_engines``) never credits the whole
union to both engines at once.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.agents.referee.assignment_resolver import retained_engine_for
from src.agents.referee.aurora_choice import AURORA_ENGINES
from src.agents.referee.engine_scope import SCOPE_BASIS
from src.agents.referee.migration_wave_builders import (
    _aurora_wave,
    _cache_wave,
    _document_wave,
    _kv_wave,
    _merge_kv_and_document_waves,
    _other_target_wave,
    _search_wave,
    wave_one_map_sentence,
)
from src.agents.referee.migration_wave_tables import (
    _cache_overlap_note,
    _durable_owner,
    _table_group_coverage_parts,
    add_gate_items,
    add_gate_text,
    gate_parts_text,
)
from src.agents.referee.table_resolution import (
    PSEUDO_TABLES,
    SourceScope,
    TableNameResolver,
    is_engine_system_object,
)
from src.agents.referee.triage import HETEROGENEOUS_SOURCE_ENGINES, SOURCE_ENGINE_TO_AURORA
from src.shared.migration_wave_engines import (
    CACHE_ENGINES,
    DOCUMENT_ENGINES,
    KV_ENGINES,
    NAMED_ENGINES,
    RELATIONAL_ENGINES,
    SEARCH_ENGINES,
)
from src.shared.report_facts import wave_one_moved_by_later_waves

# ``_durable_owner`` is imported from here by callers; it lives in migration_wave_tables.
__all__ = ["_durable_owner", "build_migration_waves", "wave_table_owners"]


def _extend_gate_parts(parts: list[dict[str, Any]], more: list[dict[str, Any]]) -> None:
    """Append ``more`` to ``parts``, merging text runs the way the gate string joins them."""
    for part in more:
        add_gate_text(parts, part["text"])
        if part["items"]:
            add_gate_items(parts, part["items"])


def _retained_engine(
    by_engine: dict[str, Any],
    source_engine: str,
    query_assignments: list[dict[str, Any]],
    aurora_engine_choice: dict[str, Any] | None,
) -> tuple[str | None, bool]:
    """(wave 1's retained Aurora engine or None, whether wave 1 converts the source
    to it): the engine every wave's table owner falls back to (``_durable_owner``)."""
    retained_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine)
    # #381: a source with no Aurora dialect of its own (SQL Server, Oracle, DB2) still
    # gets a retained engine once the assignment resolver has picked one of the two
    # competing Aurora engines -- by then only the winner ever owns a query, so it is
    # the one (if either) actually present in ``by_engine``. ``cross_engine`` flags
    # wave 1 as a dialect-and-schema conversion rather than a 1:1 carry-over, so it
    # never claims "no data model changes" for one of these sources.
    cross_engine = False
    scope_choice = (aurora_engine_choice or {}).get("engine")
    if (aurora_engine_choice or {}).get("basis") == SCOPE_BASIS and scope_choice in AURORA_ENGINES:
        # #1327 (ADR-031 Decision 2 Amendment 14): the customer's engine scope left
        # out the source-compatible engine, so wave 1 converts the source to the
        # Aurora engine it chose: a cross-engine move, as for a heterogeneous source.
        # Only when some query ends on it: a conversion to an engine that serves
        # nothing is no step of the plan, so wave 1 then names no target, as for a
        # heterogeneous source whose chosen engine serves nothing (never the
        # source-compatible engine the scope left out).
        serves = int((by_engine.get(str(scope_choice)) or {}).get("assigned_queries") or 0) > 0
        retained_engine = str(scope_choice) if serves else None
        cross_engine = serves
    elif retained_engine is None and source_engine in HETEROGENEOUS_SOURCE_ENGINES:
        # #583/#586 review: ``retained_engine_for`` is the one shared helper for
        # "which Aurora engine is retained" (``aurora_engine_choice`` first, then
        # ``query_assignments``' own ``assigned_engine``, then the shared
        # ADR-031 Decision 2/12 tie-break for a genuine, unresolved tie) --
        # ``synthesis_report._relational_aurora_engine`` calls it too, so
        # wave 1 and the architecture/cache-layer narrative cannot name different
        # engines for the same assignment. A fake ``collector_output`` carries
        # just enough for ``retained_engine_for`` to see this is a heterogeneous
        # source with no Aurora dialect of its own -- ``source_engine`` already
        # confirmed that above.
        fake_collector = {"metadata": {"source_database": {"engine": source_engine}}}
        candidate = retained_engine_for(fake_collector, query_assignments, aurora_engine_choice)
        if candidate in AURORA_ENGINES and candidate in by_engine:
            retained_engine = candidate
            cross_engine = True
        elif candidate in AURORA_ENGINES:
            # ``candidate`` names a real engine (from the choice or from
            # ``query_assignments``) with no ``by_engine``/``ranking`` entry --
            # nothing to build wave 1's query count and share from. Re-derive
            # from ``query_assignments`` alone (dropping the ranking-less
            # choice), through the same shared helper, rather than giving up.
            fallback = retained_engine_for(fake_collector, query_assignments, None)
            if fallback in AURORA_ENGINES and fallback in by_engine:
                retained_engine = fallback
                cross_engine = True

    return retained_engine, cross_engine


def _canonical_table_assignments(
    table_assignments: list[dict[str, Any]],
    known_tables: set[str] | list[str] | None,
    source: SourceScope | None,
) -> tuple[set[str] | None, list[dict[str, Any]]]:
    """(the known tables and views, the ``table_assignments`` rows that resolve to one,
    under the canonical id): what every wave is built from (#225)."""
    known: set[str] | None = (
        None
        if known_tables is None
        else {str(t) for t in known_tables if not is_engine_system_object(str(t), source)}
    )
    resolver = None if known is None else TableNameResolver.from_known_ids(known)

    def _canonical_in_scope(table_id: Any) -> Any | None:
        if not table_id or table_id in PSEUDO_TABLES:
            return None
        if resolver is None:
            return table_id
        return resolver.resolve(str(table_id))

    canonical_assignments = []
    for t in table_assignments:
        cid = _canonical_in_scope(t.get("table_id"))
        if cid is None:
            continue
        canonical_assignments.append({**t, "table_id": cid} if cid != t.get("table_id") else t)
    return known, canonical_assignments


def wave_table_owners(
    *,
    ranking: list[dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    source_engine: str,
    known_tables: set[str] | list[str] | None = None,
    aurora_engine_choice: dict[str, Any] | None = None,
    source: SourceScope | None = None,
    engine_scope: Iterable[str] | None = None,
) -> dict[str, str]:
    """``{table_id: owner}``: each table's owner as the wave plan states it (#212), the
    same ``_durable_owner`` call over the same rows and retained engine
    :func:`build_migration_waves` uses, so the Migration map's Owner column, the wave
    plan's owner lines and its kept-in-sync counts name one engine per table. Empty
    with no ranking, as the waves are None."""
    if not ranking:
        return {}
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    retained, _ = _retained_engine(
        by_engine, (source_engine or "").lower(), query_assignments, aurora_engine_choice
    )
    if engine_scope is not None and retained not in set(engine_scope):
        retained = None  # #1329: as build_migration_waves, no engine the scope left out
    _, rows = _canonical_table_assignments(table_assignments, known_tables, source)
    return {str(t["table_id"]): _durable_owner(t, retained) for t in rows if t.get("table_id")}


def build_migration_waves(
    *,
    ranking: list[dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    cache_overlay: dict[str, Any] | None,
    source_engine: str,
    known_tables: set[str] | list[str] | None = None,
    source_version: Any = None,
    mapped_tables: set[str] | list[str] | None = None,
    aurora_engine_choice: dict[str, Any] | None = None,
    source: SourceScope | None = None,
    engine_scope: Iterable[str] | None = None,
) -> list[dict[str, Any]] | None:
    """The incremental migration roadmap for this report, or ``None`` with no assignment.

    Pure function of the synthesis data already loaded for ``ranking``,
    ``table_mappings``/``cache_overlay`` and the assignment artifact
    (``table_assignments``, ``query_assignments``, ``co_dependency_groups``):
    no model call, so the same inputs always produce the same waves. See the
    module docstring for the sequencing rule.

    ``known_tables``, when given, is the caller's set of tables and views the
    collector actually saw (#225): a ``table_assignments`` row whose
    ``table_id`` does not resolve to one of them (a parser artifact — a CTE
    alias, a keyword, a system catalog name; or a spelling mismatch between
    the parser and the collector's own naming, #316) never reaches a wave. A
    row that resolves under a different spelling than its own ``table_id``
    (e.g. a bare name the collector recorded schema-qualified) is carried
    under the canonical id, so it is not double-counted against
    ``known_tables``. Wave 1 (Aurora, #321) carries every table in
    ``known_tables`` — the whole collected schema moves there first, minus
    only an engine-internal object (:func:`is_engine_system_object`: a
    sequence, a stats-extension table/view a raw introspection can pick up
    alongside real application tables) — so wave table counts add up to the
    collected schema, less that engine noise, without a separate
    unreferenced-table pass. A real application table with no later wave's
    mapping (a framework table, or one no query touches) stays; #380 review:
    narrowing wave 1 to "what some schema design mapped" was tried and wrong
    -- it silently dropped real tables from "the whole database moves to
    Aurora". ``None`` skips the filtering (back-compat for a caller that
    cannot supply the collected schema): wave 1 then falls back to every
    table named anywhere in ``table_assignments``.

    ``source_version`` is the collector's own
    ``metadata.source_database.version`` (free-form string), when the caller
    has it, used only to state what was checked for wave 1's homogeneity
    note — never to infer a feature or extension gap the collector did not
    report (#321).

    ``mapped_tables`` (#380) is the caller's set of tables some engine's
    schema design actually mapped (``table_mappings``' own ``source_table``
    set, after it has resolved out model-written noise) -- see
    :func:`wave_one_map_sentence` for the explanatory sentence it adds to
    wave 1's rationale once every wave exists (it never narrows wave 1's own
    table list).

    ``source`` (#835) is the collected database's name and engine: a database named
    ``sys`` or ``mysql`` is not read as that system schema in ``known_tables``.

    ``aurora_engine_choice`` (#381) is ``Assignment.aurora_engine_choice`` (as a
    dict), when the caller has it, used only for wave 1's cross-engine rationale --
    see :func:`_aurora_wave`.

    ``engine_scope`` is the customer's engine scope (``triage.json``'s, #1327), or
    ``None`` with no scope. A scope that leaves out every Aurora engine has no
    relational target, so wave 1 names none (#1329): never the source-compatible
    engine the scope left out.
    """
    if not ranking:
        return None
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    source_engine = (source_engine or "").lower()
    retained_engine, cross_engine = _retained_engine(
        by_engine, source_engine, query_assignments, aurora_engine_choice
    )
    no_target: str | None = None  # #1369: why the scope leaves wave 1 no Aurora engine
    choice = aurora_engine_choice or {}
    if (
        choice.get("basis") == SCOPE_BASIS
        and choice.get("engine") in AURORA_ENGINES
        and retained_engine is None
    ):
        # The scope's chosen engine serves no query: wave 1 names no target.
        no_target = "scope_choice_serves_nothing"
    if engine_scope is not None and retained_engine not in set(engine_scope):
        # #1329 (ADR-031 Decision 2 Amendment 15): no Aurora engine in the scope, so
        # wave 1 converts to no engine the customer left out.
        retained_engine = None
        cross_engine = False
        scope_aurora = set(engine_scope) & set(AURORA_ENGINES)
        if not scope_aurora:
            no_target = "no_relational_in_scope"
        elif (
            no_target is None
            and source_engine in SOURCE_ENGINE_TO_AURORA
            and not any(
                int((by_engine.get(e) or {}).get("assigned_queries") or 0) for e in scope_aurora
            )
        ):
            # #1369: the scope names an Aurora engine but no analysis ran for it, so no
            # choice was recorded and no query ends on it: the same case as a recorded
            # choice that serves nothing, never a heterogeneous source to pick for.
            no_target = "scope_choice_serves_nothing"

    known, table_assignments = _canonical_table_assignments(table_assignments, known_tables, source)
    mapped: set[str] | None = None if mapped_tables is None else {str(t) for t in mapped_tables}
    waves: list[dict[str, Any]] = []

    aurora = _aurora_wave(
        by_engine,
        retained_engine,
        source_engine,
        known,
        table_assignments,
        source_version,
        cross_engine=cross_engine,
        aurora_engine_choice=aurora_engine_choice,
        no_target=no_target,
    )
    if aurora:
        waves.append(aurora)

    cache = _cache_wave(cache_overlay, retained_engine or source_engine)
    if cache:
        waves.append(cache)

    # #321 review (finding 10): DynamoDB and DocumentDB share one wave when
    # both are routed -- the maintainer's plan is "cache + DynamoDB or cache
    # + DocumentDB", not a separate wave for each.
    dynamo_wave = None
    for engine in sorted(KV_ENGINES & set(by_engine)):
        dynamo_wave = _kv_wave(
            engine,
            by_engine,
            table_assignments,
            query_assignments,
            co_dependency_groups,
            retained_engine,
            source_engine,
        )

    doc_wave = None
    for engine in sorted(DOCUMENT_ENGINES & set(by_engine)):
        doc_wave = _document_wave(
            engine, by_engine, table_assignments, retained_engine, source_engine
        )

    if dynamo_wave and doc_wave:
        waves.append(_merge_kv_and_document_waves(dynamo_wave, doc_wave))
    elif dynamo_wave:
        waves.append(dynamo_wave)
    elif doc_wave:
        waves.append(doc_wave)

    others = sorted(
        (e for e in by_engine if e not in NAMED_ENGINES),
        key=lambda e: (-float(by_engine[e].get("workload_percent") or 0.0), e),
    )
    for engine in others:
        wave = _other_target_wave(
            engine, by_engine, table_assignments, retained_engine, source_engine
        )
        if wave:
            waves.append(wave)

    for engine in sorted(SEARCH_ENGINES & set(by_engine)):
        wave = _search_wave(engine, by_engine, table_assignments, retained_engine, source_engine)
        if wave:
            waves.append(wave)

    if not waves:
        return None

    for i, wave in enumerate(waves, start=1):
        wave["wave"] = i

    engine_wave_number: dict[str, int] = {}
    for w in waves:
        for e in w["engines"]:
            engine_wave_number.setdefault(e, w["wave"])

    # #562: the dual-read coverage note runs for every owner wave -- key-value,
    # document and any other direct migration target -- not DynamoDB's alone.
    # Wave 1 (Aurora), the cache and the search/analytics wave are excluded:
    # wave 1 is the whole schema (nothing else to reconcile against yet), the
    # cache owns no table, and search's own ``table_owners``/``serves_from``
    # already state its sync facts in full.
    _coverage_note_excluded = RELATIONAL_ENGINES | CACHE_ENGINES | SEARCH_ENGINES
    for w in waves:
        engines_here = w.get("engines") or []
        if not engines_here or engines_here[0] in _coverage_note_excluded:
            continue
        if "_dynamo_owned" in w:
            # A wave merged with DocumentDB (finding 10): each engine's own
            # OWNED table set (``_dynamo_owned``/``_doc_owned``, #568 review
            # Critical 1 -- never the served set) and own ``table_owners``
            # entries (``_dynamo_synced``/``_doc_synced``, #568 review round
            # 2 -- never the deduped, combined public list, which no longer
            # says which engine each entry is for) each get their own note,
            # spliced into their own gate segment -- the merged ``tables``
            # union would wrongly credit one engine's dual reads to the
            # other's gate text, and the served set would silence the
            # reverse clause entirely (every served-but-not-owned table is
            # already "in" the served set).
            dynamo_parts = _table_group_coverage_parts(
                set(w["_dynamo_owned"]),
                w.get("_dynamo_synced") or [],
                query_assignments,
                engines_here[0],
                engine_wave_number,
                w["wave"],
            )
            doc_parts = _table_group_coverage_parts(
                set(w["_doc_owned"]),
                w.get("_doc_synced") or [],
                query_assignments,
                engines_here[1],
                engine_wave_number,
                w["wave"],
            )
            if dynamo_parts or doc_parts:
                # #1028: the gate as parts (each table list its own items), and
                # the gate string joined from them, so the two never disagree.
                parts: list[dict[str, Any]] = []
                add_gate_text(parts, f"{w['_dynamo_label']}: {w['_dynamo_gate']}")
                _extend_gate_parts(parts, dynamo_parts)
                add_gate_text(parts, f" {w['_doc_label']}: {w['_doc_gate']}")
                _extend_gate_parts(parts, doc_parts)
                w["gate_parts"] = parts
                w["gate"] = gate_parts_text(parts)
        else:
            # #568 review, Critical 1: the OWNED subset only (``tables``
            # minus whatever ``table_owners`` already names as kept in
            # sync from elsewhere) -- the served set would silence the
            # reverse clause on real data (every served-but-not-owned
            # table is already "in" it).
            synced = w.get("table_owners") or []
            owned = set(w["tables"]) - {o["table"] for o in synced}
            note_parts = _table_group_coverage_parts(
                owned,
                synced,
                query_assignments,
                engines_here[0],
                engine_wave_number,
                w["wave"],
            )
            if note_parts:
                parts = [{"text": w["gate"], "items": []}]
                _extend_gate_parts(parts, note_parts)
                w["gate_parts"] = parts
                w["gate"] = gate_parts_text(parts)

    if cache_overlay:
        cache_w = next(
            (w for w in waves if w["engines"] and w["engines"][0] in CACHE_ENGINES), None
        )
        if cache_w:
            note = _cache_overlap_note(cache_overlay, engine_wave_number)
            if note:
                cache_w["rationale"] = cache_w["rationale"] + note

    # #321 review (finding 2): wave 1's own query_count/workload_share_percent
    # are the end state (what's still on Aurora once every later wave has
    # moved its share away) -- cutover_query_count is the whole workload,
    # every wave with a query-pattern share (every wave but the cache, which
    # is a share of calls), since 100% of it runs on Aurora the moment wave 1
    # finishes.
    if waves[0].get("homogeneity") in ("homogeneous", "cross_engine"):
        waves[0]["cutover_query_count"] = sum(
            w.get("query_count") or 0 for w in waves if w.get("share_basis") != "calls"
        )
        # #380 review, #1284: reconcile wave 1's whole-schema count with the
        # migration map's, stating apart what a later wave actually moves (the
        # facts layer's figure, so the two never disagree).
        moved = wave_one_moved_by_later_waves(waves)
        if mapped is not None and moved is not None:
            waves[0]["rationale"] += wave_one_map_sentence(waves[0], mapped, moved)

    for w in waves:
        # #1028: every wave carries its gate as parts; a gate with no table
        # list is one text part.
        if "gate_parts" not in w:
            w["gate_parts"] = [{"text": w["gate"], "items": []}]
        w.pop("_dynamo_label", None)
        w.pop("_dynamo_gate", None)
        w.pop("_dynamo_owned", None)
        w.pop("_dynamo_synced", None)
        w.pop("_doc_label", None)
        w.pop("_doc_gate", None)
        w.pop("_doc_owned", None)
        w.pop("_doc_synced", None)

    return waves
