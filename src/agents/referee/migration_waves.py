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
   when the source maps to a compatible Aurora engine, ``heterogeneous``
   (titled "Move off <source> (heterogeneous)", flagged as a risk in the
   gate, no Aurora engine named and no share claimed) when it does not.
   Skipped when there is no source engine to report at all, or the source is
   already an Aurora engine. For a homogeneous move, ``query_count`` and
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
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.table_resolution import PSEUDO_TABLES, TableNameResolver
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.shared.migration_wave_engines import (
    CACHE_ENGINES,
    DOCUMENT_ENGINES,
    KV_ENGINES,
    NAMED_ENGINES,
    NON_OWNER_ENGINES,
    RELATIONAL_ENGINES,
    SEARCH_ENGINES,
    cache_front_description,
)

# A source engine value that is already an Aurora engine: wave 1 has nothing
# to move (#321). The collector contract has no such source value today (its
# own comment: "Aurora is a target database, not a source") -- this guards a
# hand-built or future caller rather than a case the real pipeline hits.
_ALREADY_AURORA = RELATIONAL_ENGINES | {"aurora"}


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else f"{noun}s"


def _query_noun(n: int) -> str:
    return _plural(n, "query").replace("querys", "queries")


def _table_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(t["table_id"]) for t in rows if t.get("table_id")})


def _short_version(version: Any) -> str | None:
    """A brief version label from the collector's own ``version`` string, or
    ``None`` -- never an invented one (#321).

    The collector's ``metadata.source_database.version`` is sometimes short
    (``"8.0.45"``) and sometimes a full server banner (``"PostgreSQL 16.10 on
    x86_64-pc-linux-gnu, compiled by gcc ..."``); only the text up to the
    first " on " or comma is ever shown, so the banner's build details don't
    clutter a one-line wave rationale. This only trims the collector's own
    string -- it never adds or guesses a number the collector didn't report.
    """
    if not version:
        return None
    v = str(version).strip()
    for sep in (" on ", ","):
        if sep in v:
            v = v.split(sep, 1)[0].strip()
    return v or None


def _durable_owner(table: dict[str, Any], retained_engine: str | None) -> str:
    """The engine that durably owns ``table`` — never a cache or read-model engine.

    ``primary_engine`` is used unless it is a non-owner engine
    (``NON_OWNER_ENGINES``: a cache, which fronts hot reads but owns nothing,
    or a read-model/search engine, which indexes data synced from a real
    owner), in which case the first owner engine in ``engines`` takes over,
    and failing that the source-compatible retained engine (#225, #317):
    every table a non-owner engine serves must resolve to a real owner,
    never the non-owner engine itself.

    Follow-up considered after #317 fixed this at the source
    (``assignment_resolver.derive_table_assignments`` now never picks a
    read-model or cache engine as ``primary_engine``): reducing this function
    to just ``return str(table.get("primary_engine") or retained_engine or
    "unresolved")`` would be simpler, but ``table_assignments`` here is a
    plain ``dict`` this builder does not require to come from that resolver
    (a stored artifact predating #317, a hand-built fixture, a future
    caller), and the explicit regression tests for exactly this shape
    (``test_migration_waves.py::TestSearchReadModelWave``) exist for that
    reason. Keeping the non-owner skip here is defense in depth, not a
    live bug; simplifying it is a follow-up for whoever next touches this
    function, not required by #317.
    """
    primary = table.get("primary_engine")
    engines = table.get("engines") or ([primary] if primary else [])
    if primary and primary not in NON_OWNER_ENGINES:
        return str(primary)
    for engine in engines:
        if engine and engine not in NON_OWNER_ENGINES:
            return str(engine)
    return retained_engine or "unresolved"


def _owned_tables(
    engine: str, table_assignments: list[dict[str, Any]], retained_engine: str | None
) -> list[dict[str, Any]]:
    """Tables ``engine`` durably owns (the corrected ``primary_engine``, #225)."""
    return [t for t in table_assignments if _durable_owner(t, retained_engine) == engine]


def _served_tables(engine: str, table_assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables ``engine`` serves, as primary or secondary (the multi-engine rule, AGENTS.md)."""
    return [
        t for t in table_assignments if engine in (t.get("engines") or [t.get("primary_engine")])
    ]


def _sync_pattern(owner_engine: str) -> str:
    """Deterministic sync pattern OpenSearch uses to stay current with ``owner_engine``."""
    if owner_engine in RELATIONAL_ENGINES:
        return "zero-ETL"
    if owner_engine in KV_ENGINES:
        return "OpenSearch Ingestion"
    return "CDC"


def _dynamodb_table_groups(
    engine: str,
    rows: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    query_assignments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """DynamoDB's tables, table group by table group, respecting co-dependency groups.

    Two phases, both deterministic. First, a co-dependency group's *member*
    queries decide its table membership: a co-dependency group (queries
    sharing a significant JOIN) that has a DynamoDB-assigned member query
    touching one of DynamoDB's tables becomes one group (``kind:
    "co_dependency"``), named by its tables. Every DynamoDB table left over
    forms one final group of independent tables (``kind: "independent"``).
    Second, every DynamoDB-assigned query that touches an owned table — not
    just a group's member queries — is counted in exactly one group: the
    first one (in this order) whose tables it touches (#225). A query
    touching none of DynamoDB's own tables is counted in no group; the
    caller surfaces that count separately (``_table_group_coverage_note``),
    so ``sum(group query_count) + that count == the wave's query_count``.
    Groups are visited in the order ``co_dependency_groups`` lists them;
    tables within a group are sorted.
    """
    dynamo_tables = {str(t["table_id"]) for t in rows if t.get("table_id")}
    if not dynamo_tables:
        return []
    by_qid = {str(qa.get("query_id")): qa for qa in query_assignments}
    dynamo_qids = {
        qid
        for qid, qa in by_qid.items()
        if qa.get("assigned_engine") == engine
        and set(qa.get("source_tables") or []) & dynamo_tables
    }

    # Phase 1: a co-dependency group's table membership, from its own member
    # queries only -- the group's "shape" does not depend on which other,
    # non-member queries happen to touch the same tables.
    bucket_tables: list[set[str]] = []
    grouped_tables: set[str] = set()
    for group in co_dependency_groups:
        qids = set(group) & dynamo_qids
        if not qids:
            continue
        tables: set[str] = set()
        for qid in qids:
            tables |= set(by_qid[qid].get("source_tables") or []) & dynamo_tables
        if not tables:
            continue
        bucket_tables.append(tables)
        grouped_tables |= tables

    kinds = ["co_dependency"] * len(bucket_tables)
    remaining = dynamo_tables - grouped_tables
    if remaining:
        bucket_tables.append(remaining)
        kinds.append("independent")

    # Phase 2: every DynamoDB-assigned query that touches an owned table goes
    # to the first bucket (in this order) whose tables it touches -- not just
    # a co-dependency group's own members (#225: this was the counting bug).
    bucket_qids: list[set[str]] = [set() for _ in bucket_tables]
    for qid in dynamo_qids:
        q_tables = set(by_qid[qid].get("source_tables") or []) & dynamo_tables
        for i, tables in enumerate(bucket_tables):
            if q_tables & tables:
                bucket_qids[i].add(qid)
                break

    return [
        {"tables": sorted(tables), "query_count": len(qids), "kind": kind}
        for tables, qids, kind in zip(bucket_tables, bucket_qids, kinds, strict=True)
    ]


def _table_group_coverage_note(
    dynamo_tables: set[str],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    dynamo_engine: str,
    retained_engine: str | None,
    engine_wave_number: dict[str, int],
    current_wave_no: int,
) -> str:
    """Gate addendum for both directions of cross-wave co-dependency, plus the
    count of DynamoDB-assigned queries whose table groups don't cover them.

    Forward: a table DynamoDB owns that another wave's queries still read.
    Reverse: a table another wave owns that a DynamoDB-assigned query still
    reads (#225 — the previous version only covered the forward direction).
    Also counts DynamoDB-assigned queries that touch none of DynamoDB's own
    tables, so a reader can reconcile the table groups' total against the
    wave's ``query_count`` instead of being left with an unexplained gap.

    #321 review (finding 1): the forward clause used to say "until wave N"
    unconditionally, which reads backwards once a wave can precede this one
    -- Aurora is wave 1, so a DynamoDB-owned table a wave-1 (Aurora) query
    still reads was rendered as "stay dual-read ... until wave 1", as if the
    dual read ends when wave 1 (already in the past) finishes. "Until wave
    N" is only true when the other engine's own wave has not run yet
    (``wave_no > current_wave_no``): only then does *its* queries moving
    resolve the dual read. When the other engine's wave already ran (Aurora,
    always wave 1) or has no wave at all, that engine's queries never move
    again, so the dual read is permanent and the clause says so instead.
    """
    by_table = {str(t["table_id"]): t for t in table_assignments if t.get("table_id")}

    # Forward: DynamoDB's own tables, still read by a non-DynamoDB query.
    forward: dict[str, set[str]] = {}
    for qa in query_assignments:
        other = qa.get("assigned_engine")
        if not other or other == dynamo_engine:
            continue
        hit = set(qa.get("source_tables") or []) & dynamo_tables
        if hit:
            forward.setdefault(other, set()).update(hit)

    # Reverse: a DynamoDB-assigned query that reads a table some other engine
    # durably owns, and the count of queries resolving to no owner at all.
    reverse_tables: dict[str, set[str]] = {}
    reverse_queries: dict[str, set[str]] = {}
    no_owned_table = 0
    for qa in query_assignments:
        if qa.get("assigned_engine") != dynamo_engine:
            continue
        tables = set(qa.get("source_tables") or [])
        if tables & dynamo_tables:
            continue  # already counted in a table group
        other_tables = [by_table[t] for t in tables if t in by_table]
        if not other_tables:
            no_owned_table += 1
            continue
        matched = False
        for t_row in other_tables:
            owner = _durable_owner(t_row, retained_engine)
            if owner and owner != dynamo_engine:
                reverse_tables.setdefault(owner, set()).add(str(t_row["table_id"]))
                reverse_queries.setdefault(owner, set()).add(str(qa.get("query_id")))
                matched = True
        if not matched:
            no_owned_table += 1

    clauses: list[str] = []
    if forward:
        # #324 review (finding 1): group by whether the other engine's own
        # wave is still ahead of this one (temporary -- resolves once it
        # runs) or already behind/absent (permanent -- it never moves again).
        future: dict[str, set[str]] = {}
        permanent: dict[str, set[str]] = {}
        for other, tables in forward.items():
            wave_no = engine_wave_number.get(other)
            if wave_no and wave_no > current_wave_no:
                future[other] = tables
            else:
                permanent[other] = tables
        if future:
            all_tables = set()
            parts = []
            for other in sorted(future, key=lambda e: (engine_wave_number[e], e)):
                tables = future[other]
                all_tables |= tables
                parts.append(
                    f"{display_engine(other)} queries until wave {engine_wave_number[other]}"
                )
            n = len(all_tables)
            clauses.append(
                f"{n} {_plural(n, 'table')} ({', '.join(sorted(all_tables))}) stay dual-read by "
                + ", ".join(parts)
                + "; keep them in sync via CDC until then."
            )
        if permanent:
            all_tables = set()
            parts = []
            for other in sorted(permanent, key=lambda e: (engine_wave_number.get(e) or 999, e)):
                tables = permanent[other]
                all_tables |= tables
                parts.append(
                    f"{display_engine(other)} queries, which remain on {display_engine(other)}"
                )
            n = len(all_tables)
            clauses.append(
                f"{n} {_plural(n, 'table')} ({', '.join(sorted(all_tables))}) stay dual-read by "
                + ", ".join(parts)
                + "; keep a copy in sync via CDC."
            )
    if reverse_tables:
        for other in sorted(reverse_tables, key=lambda e: (engine_wave_number.get(e) or 999, e)):
            owned_table_list = sorted(reverse_tables[other])
            n_t = len(owned_table_list)
            n_q = len(reverse_queries.get(other, set()))
            wave_no = engine_wave_number.get(other)
            wave_text = f"wave {wave_no}" if wave_no else "a later wave"
            clauses.append(
                f"{n_q} {_query_noun(n_q)} read {n_t} "
                f"{_plural(n_t, 'table')} ({', '.join(owned_table_list)}) {display_engine(other)} "
                f"owns ({wave_text}) and keeps owning; keep a {display_engine(dynamo_engine)} copy of "
                f"them in sync via CDC."
            )
    if no_owned_table:
        clauses.append(
            f"{no_owned_table} {_query_noun(no_owned_table)} "
            "could not be resolved to a table in either wave and are not counted in any "
            "table group."
        )
    return (" " + " ".join(clauses)) if clauses else ""


def _cache_overlap_note(cache_overlay: dict[str, Any], engine_wave_number: dict[str, int]) -> str:
    """Says which wave(s) the cache's owner engines move in (#225).

    The cache's share is of calls, the owner shares are of query patterns; a
    reader seeing both needs to know they measure different things and that
    the cached reads already belong to a later wave's queries.
    """
    owners = cache_overlay.get("owners") or {}
    clauses = []
    for engine in sorted(owners, key=lambda e: (-owners[e], e)):
        wave_no = engine_wave_number.get(engine)
        if wave_no:
            clauses.append(f"wave {wave_no} ({display_engine(engine)})")
    if not clauses:
        return ""
    return (
        " These reads belong to queries that move in "
        + " and ".join(clauses)
        + "; this share overlaps the owner shares, which alone sum to 100%."
    )


# A source that is the same engine family in substance, worded without
# implying it is literally the same product (#324 review): MariaDB is a
# fork, so its wave 1 rationale calls it "MySQL-compatible" rather than
# claiming the stronger "same engine family" MySQL/PostgreSQL get.
_MYSQL_COMPATIBLE_SOURCES = {"mariadb"}


def _aurora_wave(
    by_engine: dict[str, dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
    known: set[str] | None,
    table_assignments: list[dict[str, Any]],
    source_version: Any,
) -> dict[str, Any] | None:
    """Wave 1 (#321): the whole source database moves to Aurora first.

    ``None`` when there is no source engine to report at all (nothing to say,
    #321: never invent a wave from no data) or the source is already an
    Aurora engine (nothing to move). Otherwise always present — unlike the
    pre-#321 "retained" wave, this wave does not depend on any engine having
    a share of the workload: it is the schema-carryover step itself, not a
    leftover accounting of what nothing else claimed.

    ``homogeneity`` is ``"homogeneous"`` when the source maps to a
    source-compatible Aurora engine (MySQL/MariaDB -> Aurora MySQL,
    PostgreSQL -> Aurora PostgreSQL) and ``"heterogeneous"`` otherwise, in
    which case ``engines`` is empty (this assessment did not pick an Aurora
    target, so none is named), the title says what the source moves *off*
    instead of *to*, and the rationale drops the share and "later waves move
    off Aurora" sentences entirely -- there is no Aurora here for either to
    be true of (#324 review).

    The rationale states only what the collector actually reports for
    homogeneity: engine and version. The collector never reports feature or
    extension compatibility, so the wording says that plainly instead of
    implying a check that was never run (#324 review finding 8).

    ``cutover_query_count`` (homogeneous only) is every in-scope query across
    every wave with a query-pattern share (i.e. every wave but the cache,
    which is a share of calls): the whole workload runs on Aurora the moment
    this wave finishes, before any later wave has moved its own share away.
    Set by the caller (``build_migration_waves``) once every wave exists.
    """
    source_engine = (source_engine or "").strip().lower()
    if not source_engine or source_engine in _ALREADY_AURORA:
        return None

    version = _short_version(source_version)
    source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(source_engine, display_engine(source_engine))
    # The collector's version string sometimes already leads with the engine
    # name (a full server banner, e.g. "PostgreSQL 16.10"): drop the
    # duplicate rather than show "PostgreSQL PostgreSQL 16.10".
    if version and version.lower().startswith(source_name.lower()):
        version_number = version[len(source_name) :].strip()
        source_desc = version if version_number else source_name
    else:
        source_desc = source_name + (f" {version}" if version else "")

    tables = sorted(known) if known is not None else _table_ids(table_assignments)

    if retained_engine:
        target_name = display_engine(retained_engine)
        family = (
            "MySQL-compatible"
            if source_engine in _MYSQL_COMPATIBLE_SOURCES
            else "same engine family"
        )
        owner_entry = by_engine.get(retained_engine) or {}
        n = int(owner_entry.get("assigned_queries") or 0)
        share = round(float(owner_entry.get("workload_percent") or 0.0), 1)
        rationale = (
            f"The whole source database ({source_desc}) moves 1:1 to {target_name}: {family}. "
            "The collector reports engine and version only; feature compatibility is not "
            f"assessed here. {n} {_query_noun(n)} ({share:.1f}%) remain on {target_name} once "
            f"later waves have moved their share; later waves move subsets of these "
            f"{len(tables)} tables and views out of {target_name}."
        )
        gate = (
            "Schema and data parity validated against the source database before cutover; "
            "decommission the legacy source once replication lag is zero."
        )
        title = f"Move to {target_name}"
        engines: list[str] = [retained_engine]
        homogeneity = "homogeneous"
    else:
        rationale = (
            f"The source database ({source_desc}) has no source-compatible Aurora engine in "
            "this assessment: a heterogeneous move (schema and query translation, not a 1:1 "
            "carry-over)."
        )
        gate = (
            "Risk: no Aurora-compatible target engine was identified for this source database; "
            "pick and validate one (schema and query translation, not a 1:1 carry-over) before "
            "this wave starts."
        )
        title = f"Move off {source_name} (heterogeneous)"
        engines = []
        n = 0
        share = 0.0
        homogeneity = "heterogeneous"

    return {
        "title": title,
        "engines": engines,
        "moves_from": [source_engine],
        "serves_from": [],
        "fronts": None,
        "tables": tables,
        "table_count": len(tables),
        "table_groups": None,
        "homogeneity": homogeneity,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": rationale,
        "gate": gate,
    }


def _cache_wave(cache_overlay: dict[str, Any] | None, front_engine: str) -> dict[str, Any] | None:
    """Wave 2 (#321, was wave 1): the cache now fronts Aurora, not the legacy
    source database -- wave 1 has already carried the schema over by the time
    this wave runs. ``front_engine`` is ``retained_engine`` normally, falling
    back to the raw source engine when there is no retained engine to front
    instead -- a heterogeneous source (wave 1 still ran, it just names no
    Aurora engine) or no source engine at all (wave 1 did not run): the
    cache still needs *something* to front either way.
    """
    if not cache_overlay or cache_overlay.get("engine") not in CACHE_ENGINES:
        return None
    n = int(cache_overlay.get("query_count") or 0)
    if not n:
        return None
    engine = cache_overlay["engine"]
    share = round(float(cache_overlay.get("call_share_percent") or 0.0), 1)
    read = _plural(n, "read")
    fronts_aurora = front_engine in RELATIONAL_ENGINES
    if fronts_aurora:
        # Review of #375: say both stages when the cache's final owner (the
        # engine the cached queries actually end up on, after every wave)
        # differs from the relational engine wave 2 fronts it with here --
        # the same shared helper every other deliverable's cache-fronting
        # sentence calls, so none of them can disagree about it.
        front = cache_front_description(front_engine, cache_overlay.get("owners"))
        authority = display_engine(front_engine)
    else:
        source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(front_engine)
        front = (
            f"the current source database ({source_name})"
            if source_name
            else "the current source database"
        )
        authority = "the source database"
    return {
        "title": f"Cache hot reads with {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": [],
        "fronts": front_engine or None,
        "tables": [],
        "table_count": 0,
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "calls",
        "rationale": (
            f"{n} hot {read} ({share:.1f}% of calls), cache-aside in front of {front}: no "
            "data migration, fully reversible. It takes the read pressure off before the "
            "data migrations that follow."
        ),
        "gate": f"Cache hit rate and invalidation verified against {authority}.",
    }


def _kv_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    co_dependency_groups: list[list[str]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    groups = _dynamodb_table_groups(engine, rows, co_dependency_groups, query_assignments)
    n_groups = len(groups) or len(rows)
    query = _query_noun(n)
    group = _plural(n_groups, "group")
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move key-value and point-lookup queries to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": groups or None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} key-value and point-lookup {query} ({share:.1f}% of query patterns) across "
            f"{n_groups} table {group}, respecting co-dependent tables where possible: the "
            f"pattern {display_engine(engine)} fits best, and the smallest-blast-radius data "
            "migration available once the cache wave has absorbed the read pressure."
        ),
        "gate": "Dual-write/backfill validated and query parity confirmed per table group.",
    }


def _other_target_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move the remaining workload to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} {query} ({share:.1f}% of query patterns) to {display_engine(engine)}, the "
            "migration target the earlier waves do not already cover."
        ),
        "gate": "Query parity confirmed before the next wave.",
    }


def _document_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _owned_tables(engine, table_assignments, retained_engine)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    moves_from_engine = retained_engine or source_engine
    return {
        "title": f"Move document-shaped data to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [moves_from_engine] if moves_from_engine else [],
        "serves_from": [],
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} document-shaped {query} ({share:.1f}% of query patterns) to "
            f"{display_engine(engine)}, kept as an owner engine for its nested, variable-shape "
            "tables."
        ),
        "gate": "Document shape conformance and query coverage validated before the next wave.",
    }


def _search_wave(
    engine: str,
    by_engine: dict[str, dict[str, Any]],
    table_assignments: list[dict[str, Any]],
    retained_engine: str | None,
    source_engine: str,
) -> dict[str, Any] | None:
    rows = _served_tables(engine, table_assignments)
    n = int(by_engine[engine].get("assigned_queries") or 0)
    if not n and not rows:
        return None
    share = round(float(by_engine[engine].get("workload_percent") or 0.0), 1)
    query = _query_noun(n)
    fallback_owner = retained_engine or (source_engine or None)

    if not rows:
        # #225: queries exist, but the SQL gave no resolvable source
        # table (every ``source_tables`` entry was pseudo/unknown or dropped as
        # parser noise). Say so explicitly instead of silently showing no owner.
        owner_label = display_engine(fallback_owner) if fallback_owner else "the source database"
        return {
            "title": f"Sync search and analytics read models to {display_engine(engine)}",
            "engines": [engine],
            "moves_from": [],
            "serves_from": [fallback_owner] if fallback_owner else [],
            "tables": [],
            "table_count": 0,
            "table_groups": None,
            "table_owners": [],
            "query_count": n,
            "workload_share_percent": share,
            "share_basis": "queries",
            "rationale": (
                f"{n} search/analytics {query} ({share:.1f}% of query patterns) build a read "
                f"model in {display_engine(engine)}. The indexed tables could not be resolved "
                "from the SQL, so until the OpenSearch index/mapping definitions are audited "
                f"directly, {owner_label} is treated as the owner of record."
            ),
            "gate": (
                "Resolve the indexed tables from the OpenSearch index/mapping definitions (not "
                "just the SQL) and confirm each has a durable owner before the next wave."
            ),
        }

    table_owners = [
        {"table": table_id, "owner": owner, "sync": _sync_pattern(owner)}
        for table_id, owner in sorted(
            (str(t["table_id"]), _durable_owner(t, retained_engine))
            for t in rows
            if t.get("table_id")
        )
    ]
    owners = sorted({o["owner"] for o in table_owners})
    owner_names = ", ".join(display_engine(o) for o in owners) or "the engines that own its tables"
    return {
        "title": f"Sync search and analytics read models to {display_engine(engine)}",
        "engines": [engine],
        "moves_from": [],
        "serves_from": owners,
        "tables": _table_ids(rows),
        "table_count": len(rows),
        "table_groups": None,
        "table_owners": table_owners,
        "query_count": n,
        "workload_share_percent": share,
        "share_basis": "queries",
        "rationale": (
            f"{n} search/analytics {query} ({share:.1f}% of query patterns) build a read model "
            f"in {display_engine(engine)}, kept in sync (zero-ETL, OpenSearch Ingestion or CDC "
            f"per table) from {owner_names}, which keep durable ownership: every table it "
            f"serves still has an owner. {display_engine(engine)} never becomes the system of "
            "record, and recovery is always by re-indexing, never a data migration."
        ),
        "gate": (
            "Sync lag and re-index time inside SLA, and every served table still has a "
            "durable owner, before the next wave."
        ),
    }


def _merge_kv_and_document_waves(dynamo: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    """One wave for DynamoDB and DocumentDB when both are routed (#321 review
    finding 10): the maintainer's own plan is "cache + DynamoDB or cache +
    DocumentDB", not a separate wave for each. Each engine's rationale and
    gate text stay distinct, labeled, inside the shared wave, rather than
    blended into one sentence that would blur which gate blocks which
    engine.

    ``_dynamo_label``/``_dynamo_gate``/``_doc_label``/``_doc_gate`` are
    transient bookkeeping ``build_migration_waves`` uses to splice the
    cross-wave dual-read note (if any) into DynamoDB's own gate text
    specifically, not the end of DocumentDB's; they are stripped before the
    waves are returned, never part of the public shape.
    """
    dynamo_name = display_engine(dynamo["engines"][0])
    doc_name = display_engine(doc["engines"][0])
    tables = sorted(set(dynamo["tables"]) | set(doc["tables"]))
    moves_from = sorted(set(dynamo.get("moves_from") or []) | set(doc.get("moves_from") or []))
    return {
        "title": f"Move key-value and document-shaped data to {dynamo_name} and {doc_name}",
        "engines": [*dynamo["engines"], *doc["engines"]],
        "moves_from": moves_from,
        "serves_from": [],
        "tables": tables,
        "table_count": len(tables),
        "table_groups": dynamo.get("table_groups"),
        "query_count": dynamo["query_count"] + doc["query_count"],
        "workload_share_percent": round(
            dynamo["workload_share_percent"] + doc["workload_share_percent"], 1
        ),
        "share_basis": "queries",
        "rationale": f"{dynamo['rationale']} {doc['rationale']}",
        "gate": f"{dynamo_name}: {dynamo['gate']} {doc_name}: {doc['gate']}",
        "_dynamo_label": dynamo_name,
        "_dynamo_gate": dynamo["gate"],
        "_dynamo_tables": dynamo["tables"],
        "_doc_label": doc_name,
        "_doc_gate": doc["gate"],
    }


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
    ``known_tables`` — the whole collected schema moves there first — so wave
    table counts add up to the collected schema without a separate
    unreferenced-table pass. ``None`` skips the filtering (back-compat for a
    caller that cannot supply the collected schema): wave 1 then falls back to
    every table named anywhere in ``table_assignments``.

    ``source_version`` is the collector's own
    ``metadata.source_database.version`` (free-form string), when the caller
    has it, used only to state what was checked for wave 1's homogeneity
    note — never to infer a feature or extension gap the collector did not
    report (#321).
    """
    if not ranking:
        return None
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    source_engine = (source_engine or "").lower()
    retained_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine)

    known: set[str] | None = None if known_tables is None else {str(t) for t in known_tables}
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
    table_assignments = canonical_assignments

    waves: list[dict[str, Any]] = []

    aurora = _aurora_wave(
        by_engine, retained_engine, source_engine, known, table_assignments, source_version
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

    for w in waves:
        if w["engines"] and w["engines"][0] in KV_ENGINES and w.get("table_groups"):
            # A wave merged with DocumentDB (finding 10) carries DynamoDB's
            # own table set separately (``_dynamo_tables``): the coverage
            # note is about DynamoDB's tables specifically, not DocumentDB's
            # too, which the merged ``tables`` union would wrongly include.
            dynamo_tables = set(w.get("_dynamo_tables", w["tables"]))
            note = _table_group_coverage_note(
                dynamo_tables,
                table_assignments,
                query_assignments,
                w["engines"][0],
                retained_engine,
                engine_wave_number,
                w["wave"],
            )
            if note:
                # #321 review (finding 10): a wave merged with DocumentDB
                # keeps the note attached to DynamoDB's own gate text, not
                # tacked onto the end after DocumentDB's.
                if "_dynamo_gate" in w:
                    w["_dynamo_gate"] = w["_dynamo_gate"] + note
                    w["gate"] = (
                        f"{w['_dynamo_label']}: {w['_dynamo_gate']} {w['_doc_label']}: {w['_doc_gate']}"
                    )
                else:
                    w["gate"] = w["gate"] + note

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
    if waves[0].get("homogeneity") == "homogeneous":
        waves[0]["cutover_query_count"] = sum(
            w.get("query_count") or 0 for w in waves if w.get("share_basis") != "calls"
        )

    for w in waves:
        w.pop("_dynamo_label", None)
        w.pop("_dynamo_gate", None)
        w.pop("_dynamo_tables", None)
        w.pop("_doc_label", None)
        w.pop("_doc_gate", None)

    return waves
