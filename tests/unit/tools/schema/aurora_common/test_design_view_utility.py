"""Aurora schema design and routing share one utility-statement definition (#778).

Schema design used its own skip list (``design_view._NON_DESIGN_STATEMENT``) that
disagreed with routing's ``is_utility_statement`` both ways, and did not strip
leading comments. It now asks ``is_utility_statement``, so a statement routing
treats as administration never reaches schema design, and the reverse. The one
exception, ``REFRESH MATERIALIZED VIEW`` as application access (#733), lives in
``utility_statements`` only. On top of that, design has one design-only filter
(#831 review): a query must name a table the design view holds, or there is
nothing to design for.
"""

import pytest

from src.agents.referee.utility_statements import is_utility_statement
from src.contracts.schema_design_input import AgentQueryPattern
from src.tools.schema.aurora_common.design_view import _designable

TABLES = ["shop.users"]
# The design view's own tables, as ``_key`` folds them.
HELD = {"users", "users_mv", "pg_stat_statements"}


def _query(text: str, tables: list[str] | None = None) -> AgentQueryPattern:
    return AgentQueryPattern(
        query_id="q",
        query_text=text,
        frequency_per_hour=1.0,
        tables_accessed=TABLES if tables is None else tables,
    )


def _designed(text: str, tables: list[str] | None = None, held: set[str] = HELD) -> bool:
    return _designable(_query(text, tables), held)


# Every statement the two old lists disagreed on (#778), the comment-led forms,
# and a sample of each family either list held.
STATEMENTS = [
    "EXPLAIN SELECT * FROM users WHERE id = $1",
    "CREATE TABLE users_copy AS SELECT * FROM users",
    "UPDATE STATISTICS dbo.users",
    "REFRESH MATERIALIZED VIEW users_mv",
    "REFRESH MATERIALIZED VIEW CONCURRENTLY users_mv",
    "LISTEN user_events",
    "UNLISTEN *",
    "NOTIFY user_events, 'signup'",
    "/* tx */ BEGIN",
    "-- session\nSET statement_timeout = 0",
    "# mysql\nSHOW TABLES",
    "/*!40101 SET NAMES utf8 */",
    "/* nightly */ VACUUM ANALYZE users",
    "/* nightly */ REFRESH MATERIALIZED VIEW users_mv",
    "BEGIN",
    "COMMIT",
    "START TRANSACTION",
    "SAVEPOINT s1",
    "RELEASE SAVEPOINT s1",
    "END",
    "SET search_path = public",
    "SHOW search_path",
    "RESET ALL",
    "DISCARD ALL",
    "DEALLOCATE ALL",
    "VACUUM users",
    "ANALYZE users",
    "CHECKPOINT",
    "LOCK TABLE users IN ACCESS EXCLUSIVE MODE",
    "GRANT SELECT ON users TO app",
    "SELECT * FROM users WHERE id = $1",
    "UPDATE users SET name = $1 WHERE id = $2",
    "WITH u AS (SELECT * FROM users) SELECT count(*) FROM u",
    "/* api */ SELECT * FROM users WHERE id = $1",
]


@pytest.mark.parametrize("text", STATEMENTS)
def test_design_skips_exactly_what_routing_calls_utility(text):
    assert _designed(text) is not is_utility_statement(text, TABLES)


@pytest.mark.parametrize(
    "text",
    [
        "EXPLAIN SELECT * FROM users WHERE id = $1",
        "CREATE TABLE users_copy AS SELECT * FROM users",
        "UPDATE STATISTICS dbo.users",
        "LISTEN user_events",
        "UNLISTEN *",
        "NOTIFY user_events, 'signup'",
        # leading comments are stripped first
        "/* tx */ BEGIN",
        "-- session\nSET statement_timeout = 0",
        "# mysql\nSHOW TABLES",
        "/*!40101 SET NAMES utf8 */",
    ],
)
def test_administration_never_reaches_schema_design(text):
    assert _designed(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "REFRESH MATERIALIZED VIEW users_mv",
        "REFRESH MATERIALIZED VIEW CONCURRENTLY users_mv",
        "/* nightly */ REFRESH MATERIALIZED VIEW users_mv",
    ],
)
def test_refresh_is_application_access_for_both(text):
    """#733: a REFRESH recomputes a view from application tables."""
    assert is_utility_statement(text, TABLES) is False
    assert _designed(text) is True


@pytest.mark.parametrize(
    "tables",
    [
        # #831 review: engine objects, whatever schema they sit in
        ["public.pg_stat_statements"],
        ["discourse.public.pg_stat_statements"],
        ["public.pg_stat_activity"],
        ["db.sys.objects"],
        ["pg_index"],
        ["pg_catalog.pg_class"],
        ["information_schema.columns"],
        ["mysql.user"],
        ["performance_schema.events_statements_summary_by_digest"],
        ["sys.tables"],
    ],
)
def test_system_catalog_reads_are_utility_for_both(tables):
    text = f"SELECT * FROM {tables[0]}"
    assert is_utility_statement(text, tables) is True
    assert _designed(text, tables) is False


@pytest.mark.parametrize("tables", [["unknown"], [], ["shop.audit_log"], ["unknown", "other"]])
def test_a_query_on_no_table_the_view_holds_has_nothing_to_design(tables):
    """Design-only (#831 review): not utility, but nothing to design for."""
    assert is_utility_statement("SELECT * FROM audit_log", tables) is False
    assert _designed("SELECT * FROM audit_log", tables) is False


def test_one_held_table_is_enough():
    assert _designed("SELECT * FROM users JOIN audit_log USING (id)", ["shop.users", "audit_log"])


@pytest.mark.parametrize(
    "tables",
    [
        ["gamification_leaderboard_cache_1_daily"],
        ["discourse.gamification_leaderboard_cache_1_daily"],
    ],
)
def test_a_refresh_of_a_view_the_design_does_not_hold_is_left_out(tables):
    """#831 review: the 6 discourse refreshes name views the design view has no
    entry for, so they must not take hot-query slots. Routing and anti-05 still
    count them as application access (#733)."""
    text = f"REFRESH MATERIALIZED VIEW CONCURRENTLY {tables[0]}"
    assert is_utility_statement(text, tables) is False
    assert _designed(text, tables) is False


@pytest.mark.parametrize(
    "tables", [["shop.mysql_logs"], ["app.sys_config"], ["shop.pg_style_settings"]]
)
def test_an_application_table_with_a_system_like_name_is_designed(tables):
    text = f"SELECT * FROM {tables[0]} WHERE id = $1"
    assert is_utility_statement(text, tables) is False
    assert _designed(text, tables, {"mysql_logs", "sys_config", "pg_style_settings"}) is True


def test_pg_notify_is_notify():
    """#831 review: ``SELECT pg_notify(...)`` is the function form of NOTIFY."""
    assert is_utility_statement("SELECT pg_notify('user_events', 'signup')", ["unknown"]) is True
    assert is_utility_statement("NOTIFY user_events, 'signup'", ["unknown"]) is True
    # alongside a real table it is part of the application statement
    text = "SELECT pg_notify('c', id::text) FROM users WHERE id = $1"
    assert is_utility_statement(text, TABLES) is False
