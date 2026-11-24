# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each ADR invariant as one SQL query over a loaded job (#2143).

A check is two queries over the tables :func:`src.tools.invariants.load.load_job`
builds: ``checked`` counts what the invariant applies to, and ``violations`` returns one
row (a one-line description) per break. A check with nothing to apply to is ``na``
(a job with no design checks no design), one with a violation is ``fail``. The queries
are oracles: they read what the pipeline saved and decide nothing, so a rule lives in
Python once (ADR-031's one scorer, the move ledger, the splitter) and is checked here
against every run's artifacts.

Every check reads every saved version: each assignment version, each design and split
of that version, and each synthesis report against the assignment version it renders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import duckdb

#: How many violation lines a result keeps (the count is always the full one).
MAX_EXAMPLES = 5


@dataclass(frozen=True)
class Check:
    """One invariant: where it is stated, what it applies to, what breaks it and, where
    the ADR allows a legacy exception, what is only a warning."""

    name: str
    rule: str
    checked: str
    violations: str
    warnings: str | None = None


# One engine per group, as ADR-031 Decision 4 states it and
# ``assignment_validator._unit_split_errors`` applies it: only in-scope members count; a
# co-dependency group's or routine unit's split onto engines the customer picked
# (``customer_override``) stands (Amendment 21), a table's writes never split that way;
# and a split a version saved before whole units (contract 1.26,
# ``saved_before_units``) already had, still on engines the members had in the version
# it came from, is a warning. Filled in by ``_one_engine`` from this module's own names.
_ONE_ENGINE_CHECKED = """
    SELECT count(*) FROM (
        SELECT DISTINCT g.version, g.{key}
        FROM {table} g JOIN query_assignments q USING (version, query_id) WHERE q.in_scope)
"""
_ONE_ENGINE_SPLITS = """
    WITH RECURSIVE lineage(version, ancestor) AS (
        SELECT version, version FROM versions
        UNION
        SELECT l.version, v.previous_version
        FROM lineage l JOIN versions v ON v.version = l.ancestor
        WHERE v.previous_version IS NOT NULL),
    members AS (
        SELECT g.version, g.{key} AS group_id, q.query_id, q.engine, q.customer_override
        FROM {table} g JOIN query_assignments q USING (version, query_id)
        WHERE q.in_scope AND q.engine IS NOT NULL),
    splits AS (
        SELECT version, group_id, list_sort(list(DISTINCT engine)) AS engines,
               list_sort(list(DISTINCT engine) FILTER (WHERE customer_override)) AS picked
        FROM members GROUP BY version, group_id HAVING count(DISTINCT engine) > 1),
    old_splits AS (
        SELECT DISTINCT m.version, m.group_id
        FROM members m
        JOIN lineage l ON l.version = m.version
        JOIN versions lv ON lv.version = l.ancestor AND lv.units_legacy
        JOIN query_assignments o ON o.version = l.ancestor AND o.query_id = m.query_id
        GROUP BY m.version, m.group_id, l.ancestor
        HAVING count(DISTINCT o.engine) > 1),
    had AS (
        SELECT m.version, m.group_id, list(DISTINCT p.engine) AS engines
        FROM members m JOIN versions v ON v.version = m.version
        JOIN query_assignments p ON p.version = v.previous_version AND p.query_id = m.query_id
        GROUP BY m.version, m.group_id),
    judged AS (
        SELECT s.version, s.group_id, s.engines,
               {customer} AS customer_split,
               (o.version IS NOT NULL AND (
                   list_has_all(coalesce(h.engines, []), s.engines)
                   OR (v.previous_version IS NULL AND v.units_legacy))) AS carried
        FROM splits s JOIN versions v USING (version)
        LEFT JOIN old_splits o USING (version, group_id)
        LEFT JOIN had h USING (version, group_id))
    SELECT 'v' || version || ': {label} ' || group_id || ' is on '
           || array_to_string(engines, ', ') || {suffix}
    FROM judged WHERE NOT customer_split AND {which}
    ORDER BY version, group_id
"""
_CUSTOMER_SPLIT = "(len(coalesce(s.picked, [])) > 1 AND list_has_all(s.picked, s.engines))"
_CARRIED = "', already split in a version saved before whole units (contract 1.26)'"


def _one_engine(name: str, table: str, key: str, label: str, *, customer: bool) -> Check:
    """Every in-scope member of each ``table`` group (keyed by ``key``) on one engine, in
    every version (a member with no engine, unservable, is on none); ``customer``: a
    split onto engines the customer picked passes this check (the customer gate's
    feasibility review refuses a JOIN split onto an engine that cannot serve it, #2519)."""
    names = {"table": table, "key": key, "label": label}
    common = {**names, "customer": _CUSTOMER_SPLIT if customer else "false"}
    return Check(
        name=name,
        rule=f"each {label} is placed on one engine, in every assignment version",
        checked=_ONE_ENGINE_CHECKED.format(**names),  # nosec B608 -- this module's own names
        violations=_ONE_ENGINE_SPLITS.format(  # nosec B608 -- this module's own names
            **common, suffix="''", which="NOT carried"
        ),
        warnings=_ONE_ENGINE_SPLITS.format(  # nosec B608 -- this module's own names
            **common, suffix=_CARRIED, which="carried"
        ),
    )


CHECKS: tuple[Check, ...] = (
    _one_engine(
        "co_dependency_group_one_engine",
        "co_dependency",
        "group_no",
        "co-dependency group",
        customer=True,
    ),
    _one_engine(
        "write_group_one_engine",
        "write_groups",
        "table_name",
        "write group of table",
        customer=False,
    ),
    _one_engine(
        "routine_unit_one_engine", "routine_units", "routine_id", "routine unit", customer=True
    ),
    Check(
        name="one_writer_per_table",
        rule=(
            "a table has one writer (ADR-031 Amendment 9): its writes and locking reads, "
            "as saved and as the collected statements write it, sit on one engine"
        ),
        checked="""
            SELECT count(*) FROM (
                SELECT version, table_name FROM write_groups
                UNION SELECT q.version, s.table_name
                FROM statement_writes s JOIN query_assignments q USING (query_id)
                UNION SELECT version, table_id FROM table_roles
                WHERE origin = 'assignment' AND role = 'write_engine')
        """,
        violations="""
            WITH writes AS (
                SELECT version, table_name, query_id FROM write_groups
                UNION SELECT q.version, s.table_name, s.query_id
                FROM statement_writes s JOIN query_assignments q USING (query_id))
            SELECT 'v' || w.version || ': table ' || w.table_name || ' is written on '
                   || array_to_string(list_sort(list(DISTINCT q.engine)), ', ')
            FROM writes w JOIN query_assignments q USING (version, query_id)
            WHERE q.engine IS NOT NULL
            GROUP BY w.version, w.table_name
            HAVING count(DISTINCT q.engine) > 1
            UNION ALL
            SELECT 'v' || version || ': table_assignments gives ' || table_id
                   || ' the write engines '
                   || array_to_string(list_sort(list(DISTINCT engine)), ', ')
            FROM table_roles WHERE origin = 'assignment' AND role = 'write_engine'
            GROUP BY version, table_id HAVING count(DISTINCT engine) > 1
            ORDER BY 1
        """,
    ),
    Check(
        name="cache_never_owns",
        rule=(
            "a cache never owns a query: no query is assigned or tied to a cache engine, "
            "and no table's owner, primary engine, writer or source of truth is one"
        ),
        checked="""
            SELECT (SELECT count(*) FROM query_assignments) + (SELECT count(*) FROM table_roles)
        """,
        violations="""
            SELECT 'v' || version || ': query ' || query_id || ' is '
                   || CASE WHEN engine IN (SELECT engine FROM cache_engines)
                           THEN 'owned by ' || engine ELSE 'tied to a cache (' || tied || ')' END
            FROM query_assignments
            WHERE engine IN (SELECT engine FROM cache_engines)
               OR list_has_any(string_split(tied, ','), (SELECT list(engine) FROM cache_engines))
            UNION ALL
            SELECT 'v' || version || ': ' || origin || ' gives table ' || table_id
                   || ' the ' || role || ' ' || engine
            FROM table_roles WHERE engine IN (SELECT engine FROM cache_engines)
            ORDER BY 1
        """,
    ),
    Check(
        name="no_unrecorded_unit_cut",
        rule=(
            "a placement unit (co-dependency group, write group, routine unit, merged) sits "
            "in one design part unless the groups manifest's unit_cuts records the cut "
            "with exactly the parts holding it (ADR-027 Decision B)"
        ),
        checked="""
            SELECT count(*) FROM (
                SELECT p.engine, u.version, u.unit_id
                FROM placement_units u JOIN split_parts p USING (version, query_id)
                GROUP BY ALL HAVING count(DISTINCT u.query_id) >= 2)
        """,
        violations="""
            WITH held AS (
                SELECT p.engine, u.version, u.unit_id,
                       count(DISTINCT u.query_id) AS statements,
                       list_sort(list(DISTINCT p.part)) AS parts
                FROM placement_units u JOIN split_parts p USING (version, query_id)
                GROUP BY ALL HAVING count(DISTINCT u.query_id) >= 2),
            cuts AS (
                SELECT engine, version, unit_id, list_sort(list(DISTINCT part)) AS parts
                FROM unit_cuts GROUP BY ALL)
            SELECT 'schema-' || h.engine || '/v' || h.version || ': ' || h.unit_id || ' ('
                   || h.statements || ' statements) is in parts '
                   || array_to_string(h.parts, ', ')
                   || CASE WHEN c.parts IS NULL THEN ' and unit_cuts does not record the cut'
                           ELSE ', unit_cuts records ' || array_to_string(c.parts, ', ') END
            FROM held h LEFT JOIN cuts c USING (engine, version, unit_id)
            WHERE len(h.parts) > 1 AND (c.parts IS NULL OR c.parts <> h.parts)
            UNION ALL
            SELECT 'schema-' || engine || '/v' || version || ': no assignment v' || version
                   || ' to check the split against'
            FROM splits WHERE version NOT IN (SELECT version FROM versions)
            ORDER BY 1
        """,
    ),
    Check(
        name="no_write_outside_owner_part",
        rule=(
            "nothing is owned in one design part and written in another: a table's writes "
            "sit in the part that owns it, unless the write is in a recorded cut whose "
            "parts include the owner's (ADR-027 Decision B, #2153)"
        ),
        checked="""
            SELECT count(*)
            FROM split_parts p JOIN statement_writes w USING (query_id)
            JOIN split_owners o ON o.engine = p.engine AND o.version = p.version
                               AND o.table_name = w.table_name
        """,
        violations="""
            WITH writes AS (
                SELECT p.engine, p.version, p.part, p.query_id, w.table_name,
                       o.part AS owner_part
                FROM split_parts p JOIN statement_writes w USING (query_id)
                JOIN split_owners o ON o.engine = p.engine AND o.version = p.version
                                   AND o.table_name = w.table_name),
            excused AS (
                SELECT c.engine, c.version, s.query_id, c.part
                FROM unit_cuts c JOIN unit_cut_statements s USING (engine, version, unit_id))
            SELECT 'schema-' || engine || '/v' || version || ': ' || query_id || ' writes '
                   || table_name || ' in part ' || part || ', owned by part ' || owner_part
            FROM writes w
            WHERE owner_part <> part AND NOT EXISTS (
                SELECT 1 FROM excused e
                WHERE e.engine = w.engine AND e.version = w.version
                  AND e.query_id = w.query_id AND e.part = w.owner_part)
            ORDER BY 1
        """,
    ),
    Check(
        name="ledger_explains_every_engine",
        rule=(
            "the move ledger explains every engine change (ADR-031 Amendment 3): each "
            "version's engines and ties replay from the version it was derived from plus "
            "its own moves; only a file saved before the ledger (no contract version, or "
            "one below 1.15) may record none"
        ),
        checked="""
            SELECT count(*) FROM query_assignments JOIN versions USING (version)
            WHERE has_moves OR NOT ledger_legacy
        """,
        violations="""
            WITH state AS (
                SELECT version, query_id, engine, tied FROM query_assignments
                UNION ALL SELECT version, query_id, 'unservable', '' FROM unservable),
            last_move AS (
                SELECT version, query_id, arg_max(to_engine, seq) AS engine,
                       arg_max(tied, seq) AS tied
                FROM moves GROUP BY ALL)
            SELECT 'v' || e.version || ': query ' || e.query_id || ' is on ' || e.engine
                   || CASE WHEN e.tied <> '' THEN ' (tied ' || e.tied || ')' ELSE '' END
                   || ', its moves replay to '
                   || coalesce(coalesce(m.engine, s.engine), 'nothing')
                   || CASE WHEN coalesce(m.tied, s.tied, '') <> ''
                           THEN ' (tied ' || coalesce(m.tied, s.tied) || ')' ELSE '' END
            FROM state e JOIN versions v USING (version)
            LEFT JOIN last_move m USING (version, query_id)
            LEFT JOIN state s ON s.version = v.previous_version AND s.query_id = e.query_id
            WHERE (v.has_moves OR NOT v.ledger_legacy)
              AND (coalesce(m.engine, s.engine) IS DISTINCT FROM e.engine
                   OR coalesce(m.tied, s.tied) IS DISTINCT FROM e.tied)
            UNION ALL
            -- Every assignment saved with a contract version (1.15+) records its moves:
            -- only a file from before the ledger may lack them (the review of #2370).
            SELECT 'v' || version || ': contract ' || contract || ' records no moves'
            FROM versions WHERE NOT has_moves AND NOT ledger_legacy
            ORDER BY 1
        """,
    ),
    Check(
        name="facts_recount",
        rule=(
            "report.json's facts agree with what they count (ADR-032): each figure recounted "
            "from the assignment version the report renders, or from the report's own lists; "
            "a figure missing from a report that has facts fails"
        ),
        checked="SELECT count(*) FROM facts_recount",
        violations="""
            SELECT 'synthesis v' || version || ': facts.' || figure || ' is '
                   || CASE WHEN saved IS NULL THEN 'missing'
                           ELSE saved || ', recounted ' || recount END
            FROM facts_recount
            WHERE saved IS NULL OR abs(saved - recount) > tolerance
            ORDER BY version, figure
        """,
    ),
    Check(
        name="design_names_only_own_statements",
        rule=(
            "each design names only its own engine's statements: every statement an access "
            "pattern serves is in scope for that engine (owned, or fronted by the cache) "
            "in the assignment version the design is for"
        ),
        checked="SELECT count(*) FROM design_names",
        violations="""
            SELECT 'schema-' || d.engine || '/v' || d.version || ': names query ' || d.query_id
                   || CASE WHEN q.query_id IS NULL THEN ', which assignment v' || d.version
                                || ' does not list'
                           WHEN NOT q.in_scope THEN ', which is out of scope'
                           ELSE ', assigned to ' || coalesce(q.engine, 'no engine') END
            FROM design_names d
            LEFT JOIN query_assignments q ON q.version = d.version AND q.query_id = d.query_id
            WHERE NOT coalesce(q.in_scope AND (q.engine = d.engine OR q.cache_engine = d.engine),
                               false)
            ORDER BY d.engine, d.version, d.query_id
        """,
    ),
    Check(
        name="design_covers_its_statements",
        rule=(
            "each designed engine's statements are all covered: every statement its design "
            "input lists (in scope for it, not a routine's scratch statement, touching a "
            "source table, not excluded) is named by an access pattern (ADR-027, #2153)"
        ),
        checked="SELECT count(*) FROM design_required",
        violations="""
            SELECT 'schema-' || r.engine || '/v' || r.version || ': query ' || r.query_id
                   || ' is assigned to ' || r.engine || ' and no access pattern names it'
            FROM design_required r
            WHERE NOT EXISTS (
                SELECT 1 FROM design_names n
                WHERE n.engine = r.engine AND n.version = r.version AND n.query_id = r.query_id)
            ORDER BY r.engine, r.version, r.query_id
        """,
    ),
)

# Views the checks share: the figures recounted for ``facts_recount`` and the statements
# each design must name for ``design_covers_its_statements``.
VIEWS: tuple[str, ...] = (
    """
    CREATE VIEW owned AS
    SELECT version, query_id, engine, tied FROM query_assignments
    WHERE in_scope AND engine IS NOT NULL AND engine NOT IN (SELECT engine FROM cache_engines)
    """,
    """
    CREATE VIEW design_required AS
    SELECT d.engine, d.version, q.query_id
    FROM designs d JOIN coverage_engines USING (engine)
    JOIN query_assignments q
      ON q.version = d.version AND q.in_scope AND (q.engine = d.engine OR q.cache_engine = d.engine)
    JOIN statements s ON s.query_id = q.query_id
    WHERE NOT s.routine_follower AND s.has_source_table
      AND NOT EXISTS (
          SELECT 1 FROM design_excluded x
          WHERE x.engine = d.engine AND x.version = d.version AND x.query_id = q.query_id)
    """,
    """
    CREATE VIEW facts_recount AS
    WITH r AS (
        SELECT version, doc, doc -> 'facts' AS f FROM reports
        WHERE json_type(doc -> 'facts') = 'OBJECT'),
    n_owned AS (SELECT version, count(*) AS n FROM owned GROUP BY version),
    n_tied AS (SELECT version, count(*) AS n FROM owned WHERE tied <> '' GROUP BY version),
    n_engines AS (
        SELECT version, count(DISTINCT engine) AS n FROM (
            SELECT version, engine FROM owned
            UNION SELECT version, cache_engine FROM query_assignments
            WHERE in_scope AND cache_engine IS NOT NULL) GROUP BY version),
    engines AS (
        SELECT version, count(*) AS n, sum(coalesce(monthly_cost_usd, 0)) AS cost,
               sum(coalesce(workload_percent, 0)) AS workload
        FROM report_engines GROUP BY version),
    moving AS (
        SELECT e.version, count(o.query_id) AS n
        FROM report_engines e LEFT JOIN owned o ON o.version = e.version AND o.engine = e.engine
        WHERE e.role IN (SELECT role FROM moving_roles) GROUP BY e.version),
    risks AS (
        SELECT version, sum(risk_count) AS n,
               sum(risk_count) FILTER (WHERE severity IN ('CRITICAL', 'HIGH')) AS critical_high
        FROM report_open_risks GROUP BY version),
    figures AS (
        SELECT r.version, 'query_total_count' AS figure,
               (f ->> 'query_total_count')::DOUBLE AS saved,
               coalesce(o.n, 0) + coalesce(json_array_length(doc -> 'scope_unservable'), 0)
                   AS recount, 0.0 AS tolerance
        FROM r LEFT JOIN n_owned o USING (version)
        UNION ALL
        SELECT r.version, 'in_scope_query_count', (f ->> 'in_scope_query_count')::DOUBLE,
               coalesce(o.n, 0), 0.0
        FROM r LEFT JOIN n_owned o USING (version)
        UNION ALL
        SELECT version, 'no_engine_in_scope_query_count',
               (f ->> 'no_engine_in_scope_query_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'scope_unservable'), 0), 0.0
        FROM r
        UNION ALL
        SELECT r.version, 'engine_count', (f ->> 'engine_count')::DOUBLE, coalesce(e.n, 0), 0.0
        FROM r LEFT JOIN engines e USING (version)
        UNION ALL
        SELECT r.version, 'engine_count (engines the assignment uses)',
               (f ->> 'engine_count')::DOUBLE, coalesce(n.n, 0), 0.0
        FROM r LEFT JOIN n_engines n USING (version)
        UNION ALL
        SELECT version, 'migration_wave_count', (f ->> 'migration_wave_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'migration_waves'), 0), 0.0
        FROM r
        UNION ALL
        SELECT r.version, 'engine_cost_total_usd', (f ->> 'engine_cost_total_usd')::DOUBLE,
               round(coalesce(e.cost, 0), 2), 0.011
        FROM r LEFT JOIN engines e USING (version)
        UNION ALL
        SELECT r.version, 'workload_total_percent', (f ->> 'workload_total_percent')::DOUBLE,
               coalesce(e.workload, 0), 0.05
        FROM r LEFT JOIN engines e USING (version)
        UNION ALL
        SELECT r.version, 'moving_query_count', (f ->> 'moving_query_count')::DOUBLE,
               coalesce(m.n, 0), 0.0
        FROM r LEFT JOIN moving m USING (version)
        UNION ALL
        SELECT r.version, 'tied_query_count', (f ->> 'tied_query_count')::DOUBLE,
               coalesce(t.n, 0), 0.0
        FROM r LEFT JOIN n_tied t USING (version)
        UNION ALL
        SELECT version, 'tied_query_count (report.tied_queries)',
               (f ->> 'tied_query_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'tied_queries'), 0), 0.0
        FROM r
        UNION ALL
        SELECT r.version, 'open_risk_count', (f ->> 'open_risk_count')::DOUBLE,
               coalesce(k.n, 0), 0.0
        FROM r LEFT JOIN risks k USING (version)
        UNION ALL
        SELECT r.version, 'critical_high_risk_count',
               (f ->> 'critical_high_risk_count')::DOUBLE, coalesce(k.critical_high, 0), 0.0
        FROM r LEFT JOIN risks k USING (version)
        UNION ALL
        SELECT version, 'pending_choice_count', (f ->> 'pending_choice_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'pending_engine_choices'), 0), 0.0
        FROM r
        UNION ALL
        SELECT version, 'auto_decided_choice_count',
               (f ->> 'auto_decided_choice_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'auto_decided_choices'), 0), 0.0
        FROM r
        UNION ALL
        SELECT version, 'post_schema_reroute_count',
               (f ->> 'post_schema_reroute_count')::DOUBLE,
               coalesce(json_array_length(doc -> 'post_schema_reroutes'), 0), 0.0
        FROM r
        UNION ALL
        SELECT version, 'post_schema_reroute_count (source of truth + design review)',
               (f ->> 'post_schema_reroute_count')::DOUBLE,
               coalesce((f ->> 'source_of_truth_reroute_count')::DOUBLE, 0)
                   + coalesce((f ->> 'design_review_reroute_count')::DOUBLE, 0), 0.0
        FROM r
        UNION ALL
        SELECT k.version, 'ranking.' || k.engine || '.assigned_queries_in_scope',
               k.in_scope_queries, coalesce(count(o.query_id), 0), 0.0
        FROM report_ranking k
        LEFT JOIN owned o ON o.version = k.version AND o.engine = k.engine
        GROUP BY k.version, k.engine, k.in_scope_queries)
    SELECT * FROM figures
    """,
)


def run_checks(
    con: duckdb.DuckDBPyConnection, checks: tuple[Check, ...] = CHECKS
) -> list[dict[str, Any]]:
    """Each check's result: ``name``, ``status`` (``pass``, ``fail`` or ``na``), how many
    things it ``checked``, how many ``failed`` and were only ``warned`` about (a legacy
    exception the ADR allows), and the first :data:`MAX_EXAMPLES` lines of each."""
    for view in VIEWS:
        con.execute(view)
    results: list[dict[str, Any]] = []
    for check in checks:
        row = con.execute(check.checked).fetchone()
        checked = int(row[0]) if row and row[0] is not None else 0
        violations = [str(r[0]) for r in con.execute(check.violations).fetchall()]
        warnings = (
            [str(r[0]) for r in con.execute(check.warnings).fetchall()] if check.warnings else []
        )
        status = "fail" if violations else ("pass" if checked else "na")
        results.append(
            {
                "name": check.name,
                "status": status,
                "checked": checked,
                "failed": len(violations),
                "warned": len(warnings),
                "examples": violations[:MAX_EXAMPLES],
                "warnings": warnings[:MAX_EXAMPLES],
            }
        )
    return results
