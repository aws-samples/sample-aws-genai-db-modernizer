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
   named and no share claimed) when no Aurora engine was picked at all.
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
``assignment_resolver.derive_table_assignments``'s own ``engines`` list,
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
(``src.report.renderers._architecture_engines``) never credits the whole
union to both engines at once.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.assignment_resolver import retained_engine_for
from src.agents.referee.aurora_choice import AURORA_ENGINES, describe_features
from src.agents.referee.table_resolution import (
    PSEUDO_TABLES,
    TableNameResolver,
    is_engine_system_object,
)
from src.agents.referee.triage import HETEROGENEOUS_SOURCE_ENGINES, SOURCE_ENGINE_TO_AURORA
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


def _served_tables(engine: str, table_assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tables ``engine`` serves, as primary or secondary (the multi-engine rule, AGENTS.md).

    This is every owner wave's table scope (#562): ``table_assignments``'
    own ``engines`` list already names every engine with an assigned query
    referencing the table (``assignment_resolver.derive_table_assignments``),
    so this is query-scoped, not a re-derivation from ``primary_engine``
    alone. A table-level plurality winner (``primary_engine``) is a summary
    label (ADR-031 Decision 1), not the set of tables the engine's own
    queries actually need present -- a table where this engine lost the
    plurality to another engine, but still has at least one assigned query
    of its own touching it, belongs here too.

    ``engines`` is read with no ``primary_engine``-only fallback (#568
    review, Suggestion 1): a row with no ``engines`` list at all has no
    recorded query evidence for any engine, so it never counts as served --
    counting it for ``primary_engine`` alone would be exactly the
    table-level claim with no query behind it this function exists to
    avoid. The real pipeline (``derive_table_assignments``) always sets
    ``engines`` (at minimum ``[primary_engine]``) for every row it builds,
    so this only ever affects a hand-built or legacy row with no ``engines``
    field, never real pipeline output.
    """
    return [t for t in table_assignments if engine in (t.get("engines") or [])]


def _table_owners_for_served(
    served: list[dict[str, Any]], engine: str, retained_engine: str | None
) -> list[dict[str, Any]] | None:
    """Which of ``served``'s tables ``engine`` does not durably own (#562):
    each paired with its real system of record and ``"CDC"`` -- the dual-read
    gate text's own word for this -- as the sync mechanism. ``None`` when
    every served table is this engine's own (the common case for an owner
    wave). A table absent from this list is this wave's own; an owner wave
    states ownership by that absence, the same convention the
    search/analytics wave already used (:func:`_search_wave`) before #562
    generalized it to every owner wave.
    """
    owners = sorted(
        (
            {"table": str(t["table_id"]), "owner": owner, "sync": "CDC"}
            for t in served
            if t.get("table_id")
            for owner in [_durable_owner(t, retained_engine)]
            if owner != engine
        ),
        key=lambda o: str(o["table"]),
    )
    return owners or None


def _dedupe_table_owners(entries: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Dedupe identical (table, owner, sync) facts (#568 review, Suggestion 4).

    The merged DynamoDB/DocumentDB wave concatenates each engine's own
    ``table_owners``; a table neither engine durably owns (a third engine,
    typically the retained Aurora engine, owns it, and both DynamoDB and
    DocumentDB merely serve it) would otherwise appear twice with an
    identical-looking entry and no way to tell them apart. The fact itself
    -- this table, kept in sync with this owner, this way -- is the same
    fact regardless of which engine's own list it came from, so it is
    recorded once.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for o in entries:
        key = (str(o["table"]), str(o["owner"]), str(o.get("sync")))
        if key in seen:
            continue
        seen.add(key)
        out.append(o)
    out.sort(key=lambda o: str(o["table"]))
    return out or None


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

    ``rows`` is DynamoDB's *served* tables (#562, ``_served_tables``) --
    every table at least one DynamoDB-assigned query touches, primary or
    secondary -- not only the tables where DynamoDB won the table-level
    ``primary_engine`` plurality. The query is the atomic unit (ADR-031
    Decision 1): a table DynamoDB serves only as a secondary engine still
    needs to be grouped and counted here, the same as one it owns outright.

    Two phases, both deterministic. First, a co-dependency group's *member*
    queries decide its table membership: a co-dependency group (queries
    sharing a significant JOIN) that has a DynamoDB-assigned member query
    touching one of DynamoDB's tables becomes one group (``kind:
    "co_dependency"``), named by its tables. Every DynamoDB table left over
    forms one final group of independent tables (``kind: "independent"``).
    Second, every DynamoDB-assigned query that touches a served table — not
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
    engine_tables: set[str],
    synced_tables: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    engine: str,
    engine_wave_number: dict[str, int],
    current_wave_no: int,
) -> str:
    """Gate addendum for both directions of cross-wave co-dependency, plus the
    count of ``engine``-assigned queries whose table groups don't cover them.

    Runs for every owner wave now (#562: DynamoDB, DocumentDB and any other
    direct migration target), not DynamoDB's alone -- the dual read it names
    is a real operational fact regardless of which engine the wave moves.

    ``engine_tables`` is the OWNED subset of that wave's tables -- the rows
    where ``engine`` is the durable owner (``_durable_owner``), never the
    full served set :func:`_served_tables` returns (#568 review, Critical
    1): the served set already includes every table this wave keeps in
    sync from elsewhere (``table_owners``), so passing it here would fold
    both directions into one sentence that no longer says which way the
    sync runs -- every forward hit would also be a reverse hit, and
    ``tables & engine_tables`` would always be true, silencing the reverse
    clause on real data entirely.

    ``synced_tables`` is this wave's own ``table_owners`` -- every table
    ``engine`` serves but does not durably own, built once, exhaustively, by
    :func:`_table_owners_for_served` (#568 review round 2, Important 2).
    The reverse clause is now built from this list directly, not
    re-derived from which ``engine``-assigned queries happen to touch no
    owned table: that re-derivation skipped a kept-in-sync table outright
    whenever a query read it *together with* an owned table (``tables &
    engine_tables`` was true, so the whole query, and every table it
    touched, was treated as "already counted" -- on real discourse data,
    ``discourse.child_themes`` and ``discourse.post_actions`` never
    appeared in any gate because every query that read them also read an
    owned table). Building from ``table_owners`` instead means every
    kept-in-sync table is named regardless of what else its queries read.

    Forward: a table ``engine`` durably owns that another wave's queries
    still read. Reverse: every table ``engine`` keeps in sync from another
    wave's owner, with how many of ``engine``'s own queries actually read
    it -- a separate count from the gap below, not a condition for being
    named (#225 — the previous version only covered the forward direction).
    Also counts ``engine``-assigned queries that touch neither an owned nor
    a kept-in-sync table (fully unresolved), so a reader can reconcile the
    table groups' total against the wave's ``query_count`` instead of being
    left with an unexplained gap.

    #321 review (finding 1): the forward clause used to say "until wave N"
    unconditionally, which reads backwards once a wave can precede this one
    -- Aurora is wave 1, so a table a wave-1 (Aurora) query still reads was
    rendered as "stay dual-read ... until wave 1", as if the dual read ends
    when wave 1 (already in the past) finishes. "Until wave N" is only true
    when the other engine's own wave has not run yet
    (``wave_no > current_wave_no``): only then does *its* queries moving
    resolve the dual read. When the other engine's wave already ran (Aurora,
    always wave 1) or has no wave at all, that engine's queries never move
    again, so the dual read is permanent and the clause says so instead.
    """
    # Forward: this wave's own tables, still read by a query assigned elsewhere.
    forward: dict[str, set[str]] = {}
    for qa in query_assignments:
        other = qa.get("assigned_engine")
        if not other or other == engine:
            continue
        hit = set(qa.get("source_tables") or []) & engine_tables
        if hit:
            forward.setdefault(other, set()).update(hit)

    # Reverse: every kept-in-sync table, exhaustively, from table_owners --
    # never re-derived from which queries happen to touch no owned table
    # (#568 review round 2).
    owner_by_table: dict[str, str] = {str(o["table"]): str(o["owner"]) for o in synced_tables}
    synced_ids = set(owner_by_table)
    reverse_tables: dict[str, set[str]] = {}
    for table, owner in owner_by_table.items():
        reverse_tables.setdefault(owner, set()).add(table)
    reverse_queries: dict[str, set[str]] = {}
    no_owned_table = 0
    for qa in query_assignments:
        if qa.get("assigned_engine") != engine:
            continue
        tables = set(qa.get("source_tables") or [])
        hit = tables & synced_ids
        if hit:
            for table in hit:
                reverse_queries.setdefault(owner_by_table[table], set()).add(
                    str(qa.get("query_id"))
                )
            continue  # named by table_owners above; not a gap
        if tables & engine_tables:
            continue  # an owned table, already counted in a table group
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
            others = []
            for other in sorted(future, key=lambda e: (engine_wave_number[e], e)):
                tables = future[other]
                all_tables |= tables
                others.append(other)
                parts.append(
                    f"{display_engine(other)} queries until wave {engine_wave_number[other]}"
                )
            n = len(all_tables)
            # #568 review round 2, Suggestion 1: name which way the sync runs
            # explicitly -- ``engine`` owns these tables, so the OTHER
            # engine's copy is the one synced, from ``engine``, not the
            # reverse.
            other_names = ", ".join(display_engine(o) for o in others)
            clauses.append(
                f"{n} {_plural(n, 'table')} ({', '.join(sorted(all_tables))}) stay dual-read by "
                + ", ".join(parts)
                + f"; {display_engine(engine)} owns them, so keep the {other_names} copy in "
                f"sync from {display_engine(engine)} via CDC until then."
            )
        if permanent:
            all_tables = set()
            parts = []
            others = []
            for other in sorted(permanent, key=lambda e: (engine_wave_number.get(e) or 999, e)):
                tables = permanent[other]
                all_tables |= tables
                others.append(other)
                parts.append(
                    f"{display_engine(other)} queries, which remain on {display_engine(other)}"
                )
            n = len(all_tables)
            other_names = ", ".join(display_engine(o) for o in others)
            clauses.append(
                f"{n} {_plural(n, 'table')} ({', '.join(sorted(all_tables))}) stay dual-read by "
                + ", ".join(parts)
                + f"; {display_engine(engine)} owns them, so keep the {other_names} copy in "
                f"sync from {display_engine(engine)} via CDC."
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
                f"owns ({wave_text}) and keeps owning; keep a {display_engine(engine)} copy of "
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
    mapped_tables: set[str] | None = None,
    cross_engine: bool = False,
    aurora_engine_choice: dict[str, Any] | None = None,
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
    PostgreSQL -> Aurora PostgreSQL); ``"cross_engine"`` (#381, ``cross_engine=True``,
    the caller's own signal: the source has no Aurora dialect of its own, but the
    assignment resolver still picked one of the two competing Aurora engines, so
    ``retained_engine`` names it) when the source has no Aurora dialect but a choice
    was made anyway -- the title and rationale state this is a schema-and-dialect
    conversion, not a 1:1 carry-over, and the gate calls out validating the
    translation, but the engine is still named and still given a share, same as the
    homogeneous case; and ``"heterogeneous"`` otherwise, in which case ``engines`` is
    empty (this assessment did not pick an Aurora target at all, so none is named),
    the title says what the source moves *off* instead of *to*, and the rationale
    drops the share and "later waves move off Aurora" sentences entirely -- there is
    no Aurora here for either to be true of (#324 review).

    ``aurora_engine_choice`` (#381) is the resolver's own decision trace
    (``Assignment.aurora_engine_choice``, as a dict: ``totals``, ``margin``,
    ``deciding_features``, ``reason``) for a ``cross_engine`` wave's rationale, naming
    both engines' totals and whatever tipped the choice. ``None`` (homogeneous, no
    Aurora at all, or a legacy assignment with no such field) drops that sentence.

    The rationale states only what the collector actually reports for
    homogeneity: engine and version. The collector never reports feature or
    extension compatibility, so the wording says that plainly instead of
    implying a check that was never run (#324 review finding 8).

    ``cutover_query_count`` (homogeneous only) is every in-scope query across
    every wave with a query-pattern share (i.e. every wave but the cache,
    which is a share of calls): the whole workload runs on Aurora the moment
    this wave finishes, before any later wave has moved its own share away.
    Set by the caller (``build_migration_waves``) once every wave exists.

    ``mapped_tables`` (#380 review) is the caller's set of tables some
    engine's schema design actually mapped (``table_mappings``' own
    ``source_table`` set, already resolved against the collector's real
    schema). It never narrows ``tables`` -- wave 1 still carries every
    collected base table and view, because that is what "the whole source
    database moves 1:1" actually means, and every other deliverable
    (``renderers.py``, ``pptx_report.py``) keeps this wave's ``table_count``
    as the whole collected schema. It is used only to add one explanatory
    sentence to the rationale, reconciling this wave's full count against the
    migration map's smaller "N tables mapped to a target engine" total (the
    #380 complaint was the two counts disagreeing with no explanation, not
    that wave 1 counted too much): "``M`` of these are mapped to a target
    engine in a later wave; the other ``K`` stay on ``<engine>`` unchanged."
    Omitted (``None``) when the caller has nothing mapped at all (no schema
    design ran for any engine, e.g. ``--llm-mode none``) -- there is nothing
    to reconcile against yet.
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

    # #380 review: wave 1 carries the whole collected schema -- every base
    # table and view the collector saw, minus only engine-internal noise
    # (a sequence, a stats-extension object: ``is_engine_system_object``),
    # never narrowed to what some later wave happens to map. A real
    # application table with no schema-design mapping (a framework table
    # like ``ar_internal_metadata``, or simply one no query touches) is
    # still part of "the whole database" and stays.
    tables = sorted(known) if known is not None else _table_ids(table_assignments)

    if retained_engine and not cross_engine:
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
        if mapped_tables is not None:
            # #380 review: reconciles this wave's full table-and-view count
            # against the migration map's smaller "mapped" total, so the two
            # numbers disagreeing (322 vs 280 in the field) reads as explained,
            # not contradictory.
            mapped_count = len(set(tables) & mapped_tables)
            unmapped_count = len(tables) - mapped_count
            rationale += (
                f" {mapped_count} of these tables and views are mapped to a target engine "
                f"in a later wave; the other {unmapped_count} stay on {target_name} "
                "unchanged."
            )
        gate = (
            "Schema and data parity validated against the source database before cutover; "
            "decommission the legacy source once replication lag is zero."
        )
        title = f"Move to {target_name}"
        engines: list[str] = [retained_engine]
        homogeneity = "homogeneous"
    elif retained_engine and cross_engine:
        # #381: the source has no Aurora dialect of its own (SQL Server, Oracle, DB2),
        # but the assignment resolver still picked one of the two competing Aurora
        # engines by scoring every query against both -- named and given a share here,
        # the same as the homogeneous case, but stated plainly as a dialect-and-schema
        # conversion rather than a 1:1 carry-over, so this wave never reads as "no data
        # model changes".
        target_name = display_engine(retained_engine)
        owner_entry = by_engine.get(retained_engine) or {}
        n = int(owner_entry.get("assigned_queries") or 0)
        share = round(float(owner_entry.get("workload_percent") or 0.0), 1)
        decision = ""
        if aurora_engine_choice:
            totals = aurora_engine_choice.get("totals") or {}
            if totals:
                decision = (
                    " Aurora MySQL scored "
                    f"{float(totals.get('aurora_mysql', 0.0)):.1f} and Aurora PostgreSQL "
                    f"{float(totals.get('aurora_postgresql', 0.0)):.1f} in total adjusted "
                    "query fit"
                )
            features = aurora_engine_choice.get("deciding_features") or []
            if features:
                decision += f"; tipped by {describe_features(features)}"
            if decision:
                decision += "."
        rationale = (
            f"The source database ({source_desc}) has no Aurora engine of its own dialect: "
            f"Aurora MySQL and Aurora PostgreSQL both competed on the collected workload, "
            # #381 review: never "earned the move" -- that reads as a decisive win even
            # when the actual decision was a tie-break or an unneeded default (an exact
            # tie, or a close call with no deciding feature). "Selected" is accurate
            # whichever of those the trace in ``decision`` (if present) turns out to be.
            f"and the assignment resolver selected {target_name} to serve it.{decision} "
            "This is a cross-engine move -- SQL dialect and schema conversion, not a 1:1 "
            "carry-over. "
            f"{n} {_query_noun(n)} ({share:.1f}%) remain on {target_name} once later waves "
            f"have moved their share; later waves move subsets of these {len(tables)} "
            f"tables and views out of {target_name}."
        )
        if mapped_tables is not None:
            mapped_count = len(set(tables) & mapped_tables)
            unmapped_count = len(tables) - mapped_count
            rationale += (
                f" {mapped_count} of these tables and views are mapped to a target engine "
                f"in a later wave; the other {unmapped_count} stay on {target_name} "
                "unchanged."
            )
        gate = (
            f"Cross-engine move: validate the schema and query translation to {target_name} "
            "(SQL dialect and data model conversion, not a 1:1 carry-over) before cutover."
        )
        title = f"Move off {source_name} to {target_name} (cross-engine)"
        engines = [retained_engine]
        homogeneity = "cross_engine"
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
    rows = _served_tables(engine, table_assignments)
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
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
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
    rows = _served_tables(engine, table_assignments)
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
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
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
    rows = _served_tables(engine, table_assignments)
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
        "table_owners": _table_owners_for_served(rows, engine, retained_engine),
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

    ``_dynamo_label``/``_dynamo_gate``/``_dynamo_owned``/``_dynamo_synced``/
    ``_doc_label``/``_doc_gate``/``_doc_owned``/``_doc_synced`` are transient
    bookkeeping ``build_migration_waves`` uses to splice each engine's own
    cross-wave dual-read note (#562: now computed for both, not DynamoDB's
    alone) into its own gate text specifically, keyed to its own OWNED
    table set for the forward clause (#568 review, Critical 1 -- never the
    served set) and its own ``table_owners`` entries for the reverse clause
    (#568 review round 2, Important 2 -- the deduped, combined public
    ``table_owners`` no longer says which engine each entry is for), rather
    than blending them or attaching both to the end of one engine's gate;
    they are stripped before the waves are returned, never part of the
    public shape.
    """
    dynamo_name = display_engine(dynamo["engines"][0])
    doc_name = display_engine(doc["engines"][0])
    dynamo_engine = dynamo["engines"][0]
    doc_engine = doc["engines"][0]
    tables = sorted(set(dynamo["tables"]) | set(doc["tables"]))
    moves_from = sorted(set(dynamo.get("moves_from") or []) | set(doc.get("moves_from") or []))
    dynamo_owners = dynamo.get("table_owners") or []
    doc_owners = doc.get("table_owners") or []
    # #568 review, Suggestion 4: a table neither engine durably owns (both
    # merely serve it) would otherwise appear twice, identically -- dedupe
    # the fact itself rather than keep two copies with no way to tell them
    # apart.
    table_owners = _dedupe_table_owners([*dynamo_owners, *doc_owners])
    dynamo_owned = sorted(set(dynamo["tables"]) - {o["table"] for o in dynamo_owners})
    doc_owned = sorted(set(doc["tables"]) - {o["table"] for o in doc_owners})
    return {
        "title": f"Move key-value and document-shaped data to {dynamo_name} and {doc_name}",
        "engines": [dynamo_engine, doc_engine],
        "moves_from": moves_from,
        "serves_from": [],
        "tables": tables,
        "table_count": len(tables),
        "table_groups": dynamo.get("table_groups"),
        "table_owners": table_owners,
        # #568 review, Important 2: table_count above is the union of both
        # engines' served tables and cannot be split back into each
        # engine's own share -- a consumer that credited it to every engine
        # (the pre-#568-review behaviour) double counted. These two give
        # the real per-engine served and owned counts instead.
        "table_counts_by_engine": {
            dynamo_engine: dynamo["table_count"],
            doc_engine: doc["table_count"],
        },
        "owned_counts_by_engine": {
            dynamo_engine: len(dynamo_owned),
            doc_engine: len(doc_owned),
        },
        "query_count": dynamo["query_count"] + doc["query_count"],
        "workload_share_percent": round(
            dynamo["workload_share_percent"] + doc["workload_share_percent"], 1
        ),
        "share_basis": "queries",
        "rationale": f"{dynamo['rationale']} {doc['rationale']}",
        "gate": f"{dynamo_name}: {dynamo['gate']} {doc_name}: {doc['gate']}",
        "_dynamo_label": dynamo_name,
        "_dynamo_gate": dynamo["gate"],
        "_dynamo_owned": dynamo_owned,
        "_dynamo_synced": dynamo_owners,
        "_doc_label": doc_name,
        "_doc_gate": doc["gate"],
        "_doc_owned": doc_owned,
        "_doc_synced": doc_owners,
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
    mapped_tables: set[str] | list[str] | None = None,
    aurora_engine_choice: dict[str, Any] | None = None,
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
    :func:`_aurora_wave` for the explanatory sentence it adds to wave 1's
    rationale (it never narrows wave 1's own table list).

    ``aurora_engine_choice`` (#381) is ``Assignment.aurora_engine_choice`` (as a
    dict), when the caller has it, used only for wave 1's cross-engine rationale --
    see :func:`_aurora_wave`.
    """
    if not ranking:
        return None
    by_engine = {str(r["target"]): r for r in ranking if r.get("target")}
    source_engine = (source_engine or "").lower()
    retained_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine)
    # #381: a source with no Aurora dialect of its own (SQL Server, Oracle, DB2) still
    # gets a retained engine once the assignment resolver has picked one of the two
    # competing Aurora engines -- by then only the winner ever owns a query, so it is
    # the one (if either) actually present in ``by_engine``. ``cross_engine`` flags
    # wave 1 as a dialect-and-schema conversion rather than a 1:1 carry-over, so it
    # never claims "no data model changes" for one of these sources.
    cross_engine = False
    if retained_engine is None and source_engine in HETEROGENEOUS_SOURCE_ENGINES:
        # #583/#586 review: ``retained_engine_for`` is the one shared helper for
        # "which Aurora engine is retained" (``aurora_engine_choice`` first, then
        # ``query_assignments``' own ``assigned_engine``, then the shared
        # ADR-031 Decision 2/12 tie-break for a genuine, unresolved tie) --
        # ``synthesis_report._source_compatible_aurora_engine`` calls it too, so
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

    known: set[str] | None = (
        None
        if known_tables is None
        else {str(t) for t in known_tables if not is_engine_system_object(str(t))}
    )
    mapped: set[str] | None = None if mapped_tables is None else {str(t) for t in mapped_tables}
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
        by_engine,
        retained_engine,
        source_engine,
        known,
        table_assignments,
        source_version,
        mapped_tables=mapped,
        cross_engine=cross_engine,
        aurora_engine_choice=aurora_engine_choice,
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
            dynamo_note = _table_group_coverage_note(
                set(w["_dynamo_owned"]),
                w.get("_dynamo_synced") or [],
                query_assignments,
                engines_here[0],
                engine_wave_number,
                w["wave"],
            )
            doc_note = _table_group_coverage_note(
                set(w["_doc_owned"]),
                w.get("_doc_synced") or [],
                query_assignments,
                engines_here[1],
                engine_wave_number,
                w["wave"],
            )
            if dynamo_note:
                w["_dynamo_gate"] = w["_dynamo_gate"] + dynamo_note
            if doc_note:
                w["_doc_gate"] = w["_doc_gate"] + doc_note
            if dynamo_note or doc_note:
                w["gate"] = (
                    f"{w['_dynamo_label']}: {w['_dynamo_gate']} {w['_doc_label']}: {w['_doc_gate']}"
                )
        else:
            # #568 review, Critical 1: the OWNED subset only (``tables``
            # minus whatever ``table_owners`` already names as kept in
            # sync from elsewhere) -- the served set would silence the
            # reverse clause on real data (every served-but-not-owned
            # table is already "in" it).
            synced = w.get("table_owners") or []
            owned = set(w["tables"]) - {o["table"] for o in synced}
            note = _table_group_coverage_note(
                owned,
                synced,
                query_assignments,
                engines_here[0],
                engine_wave_number,
                w["wave"],
            )
            if note:
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
    if waves[0].get("homogeneity") in ("homogeneous", "cross_engine"):
        waves[0]["cutover_query_count"] = sum(
            w.get("query_count") or 0 for w in waves if w.get("share_basis") != "calls"
        )

    for w in waves:
        w.pop("_dynamo_label", None)
        w.pop("_dynamo_gate", None)
        w.pop("_dynamo_owned", None)
        w.pop("_dynamo_synced", None)
        w.pop("_doc_label", None)
        w.pop("_doc_gate", None)
        w.pop("_doc_owned", None)
        w.pop("_doc_synced", None)

    return waves
