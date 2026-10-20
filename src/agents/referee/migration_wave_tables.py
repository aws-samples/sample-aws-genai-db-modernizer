"""Migration waves: the tables each wave serves, their owners and the dual-read notes."""

from __future__ import annotations

from typing import Any

from src.contracts.synthesis_output import join_gate_parts
from src.shared.engine_names import display_engine
from src.shared.migration_wave_engines import (
    KV_ENGINES,
    NON_OWNER_ENGINES,
    RELATIONAL_ENGINES,
)


def _plural(n: int, noun: str) -> str:
    return noun if n == 1 else f"{noun}s"


def _query_noun(n: int) -> str:
    return _plural(n, "query").replace("querys", "queries")


# A wave gate's structured parts (#1028): each part is a run of text followed
# by an optional item list (table names). The results page collapses each list
# to an inline "(…)"; joined back, the parts are exactly the wave's ``gate``.
GatePart = dict[str, Any]


def gate_parts_text(parts: list[GatePart]) -> str:
    """The gate string the parts stand for: each part's text, then
    " (a, b, c)" when it has items (the contract's ``join_gate_parts``)."""
    return join_gate_parts(parts)


def add_gate_text(parts: list[GatePart], text: str) -> None:
    """Append ``text``, merged into the last part when that part has no list."""
    if parts and not parts[-1]["items"]:
        parts[-1]["text"] += text
    else:
        parts.append({"text": text, "items": []})


def add_gate_items(parts: list[GatePart], items: list[str]) -> None:
    """Close the last text run with an item list."""
    if parts and not parts[-1]["items"]:
        parts[-1]["items"] = list(items)
    else:
        parts.append({"text": "", "items": list(items)})


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
    (``table_assignments.derive_table_assignments`` now never picks a
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

    A written table's owner is its ``source_of_truth`` (#992, ADR-031 Decision
    15): the one engine all its writes land on, which the other engines that
    serve it replicate from. The plurality ``primary_engine`` is only a summary
    (Decision 1), so it never names the owner of a written table: a table
    written on DynamoDB is owned there even when more of its reads run on
    Aurora.
    """
    source_of_truth = table.get("source_of_truth")
    if source_of_truth and source_of_truth not in NON_OWNER_ENGINES:
        return str(source_of_truth)
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
    referencing the table (``table_assignments.derive_table_assignments``),
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
            # A table this engine writes is never a copy kept in sync from
            # another owner (#992): it is not one-way.
            and engine not in (t.get("write_engines") or [])
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
    """The gate addendum as one string (``_table_group_coverage_parts`` joined)."""
    return gate_parts_text(
        _table_group_coverage_parts(
            engine_tables,
            synced_tables,
            query_assignments,
            engine,
            engine_wave_number,
            current_wave_no,
        )
    )


def _table_group_coverage_parts(
    engine_tables: set[str],
    synced_tables: list[dict[str, Any]],
    query_assignments: list[dict[str, Any]],
    engine: str,
    engine_wave_number: dict[str, int],
    current_wave_no: int,
) -> list[GatePart]:
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

    clauses: list[list[GatePart]] = []
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
                [
                    {"text": f"{n} {_plural(n, 'table')}", "items": sorted(all_tables)},
                    {
                        "text": " stay dual-read by "
                        + ", ".join(parts)
                        + f"; {display_engine(engine)} owns them, so keep the {other_names} "
                        f"copy in sync from {display_engine(engine)} via CDC until then.",
                        "items": [],
                    },
                ]
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
                [
                    {"text": f"{n} {_plural(n, 'table')}", "items": sorted(all_tables)},
                    {
                        "text": " stay dual-read by "
                        + ", ".join(parts)
                        + f"; {display_engine(engine)} owns them, so keep the {other_names} "
                        f"copy in sync from {display_engine(engine)} via CDC.",
                        "items": [],
                    },
                ]
            )
    if reverse_tables:
        owners = sorted(reverse_tables, key=lambda e: (engine_wave_number.get(e) or 999, e))
        for i, other in enumerate(owners):
            owned_table_list = sorted(reverse_tables[other])
            n_t = len(owned_table_list)
            n_q = len(reverse_queries.get(other, set()))
            wave_no = engine_wave_number.get(other)
            wave_text = f"wave {wave_no}" if wave_no else "a later wave"
            # #1170: with several owners, the sync is stated once, after the last.
            last = i == len(owners) - 1
            tail = (
                f"; keep a {display_engine(engine)} copy of "
                f"{'all of them' if len(owners) > 1 else 'them'} in sync via CDC."
                if last
                else ", and"
            )
            clauses.append(
                [
                    {
                        "text": f"{n_q} {_query_noun(n_q)} read {n_t} {_plural(n_t, 'table')}",
                        "items": owned_table_list,
                    },
                    {
                        "text": f" {display_engine(other)} owns ({wave_text}) and keeps owning"
                        + tail,
                        "items": [],
                    },
                ]
            )
    if no_owned_table:
        clauses.append(
            [
                {
                    "text": f"{no_owned_table} {_query_noun(no_owned_table)} "
                    "could not be resolved to a table in either wave and are not counted in "
                    "any table group.",
                    "items": [],
                }
            ]
        )
    out: list[GatePart] = []
    for clause in clauses:
        add_gate_text(out, " ")
        for part in clause:
            add_gate_text(out, part["text"])
            if part["items"]:
                add_gate_items(out, part["items"])
    return out


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
