# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement on a view is scored on the view's base tables (#1875).

A small synthetic SQL Server collection (``shop``): a simple view, a view joining two
tables, a view on that view, a pair of views that read each other, a view with no
definition and one whose definition does not parse, and statements that read and
write through them. The same code path serves MySQL and PostgreSQL collections, and a
collector output a library caller hands in (``rederive_query_facts``).
"""

from __future__ import annotations

import copy

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.referee.pending_choices import build_pending_choices
from src.agents.referee.query_pattern import QueryPatternModel
from src.agents.referee.query_scoring import compute_query_confidence
from src.agents.referee.write_groups import build_write_groups, inserted_tables, written_tables
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.offline_parser import parse_offline_collection, rederive_query_facts
from src.tools.sql.view_lineage import NO_DEFINITION, apply_view_lineage

TABLES = {
    "Users": ("UserId", "Username"),
    "UserPortals": ("UserId", "PortalId", "IsDeleted"),
    "Orders": ("OrderId", "UserId", "Total"),
    "Products": ("ProductId", "Name"),
}

PRIMARY_KEYS = {
    "Users": ("UserId",),
    "UserPortals": ("UserId", "PortalId"),
    "Orders": ("OrderId",),
    "Products": ("ProductId",),
}

VIEWS = {
    # A simple view on one table.
    "vw_Orders": "CREATE VIEW [dbo].[vw_Orders] AS SELECT o.OrderId, o.UserId, o.Total "
    "FROM [dbo].[Orders] AS o",
    # A view joining two tables.
    "vw_Users": "CREATE VIEW dbo.[vw_Users] AS SELECT U.UserId, U.Username, UP.PortalId, "
    "UP.IsDeleted FROM dbo.[Users] AS U LEFT JOIN dbo.[UserPortals] AS UP "
    "ON U.UserId = UP.UserId",
    # A view on a view.
    "vw_ActiveUsers": "CREATE VIEW dbo.vw_ActiveUsers AS SELECT UserId, Username "
    "FROM dbo.vw_Users WHERE IsDeleted = 0",
    # Two views that read each other.
    "vw_A": "CREATE VIEW dbo.vw_A AS SELECT x FROM dbo.vw_B",
    "vw_B": "CREATE VIEW dbo.vw_B AS SELECT x FROM dbo.vw_A",
    # A view the collection holds no definition for (sys.sql_modules gave none).
    "vw_Missing": None,
    # A view whose definition the facade cannot parse and that names no table.
    "vw_Broken": "CREATE VIEW dbo.vw_Broken AS SELECT ((( ",
}

QUERIES = {
    "q_orders": "SELECT * FROM dbo.vw_Orders WHERE OrderId = @id",
    "q_users": "SELECT * FROM dbo.vw_Users WHERE UserId = @id AND PortalId = @p",
    "q_direct": "SELECT * FROM dbo.Users AS U LEFT JOIN dbo.UserPortals AS UP "
    "ON U.UserId = UP.UserId WHERE U.UserId = @id AND UP.PortalId = @p",
    "q_active": "SELECT * FROM dbo.vw_ActiveUsers WHERE UserId = @id",
    "q_cycle": "SELECT * FROM dbo.vw_A",
    "q_missing": "SELECT * FROM dbo.vw_Missing WHERE id = @id",
    "q_broken": "SELECT * FROM dbo.vw_Broken",
    # Writes through views.
    "w_insert": "INSERT INTO dbo.vw_Orders (OrderId, UserId) SELECT p.ProductId, @u "
    "FROM dbo.Products AS p WHERE p.ProductId = @id",
    "w_portal": "UPDATE dbo.vw_Users SET IsDeleted = 1 WHERE UserId = @id",
    "w_mixed": "UPDATE dbo.vw_Users SET IsDeleted = 1, Username = @n WHERE UserId = @id",
    "w_nested": "UPDATE dbo.vw_ActiveUsers SET Username = @n WHERE UserId = @id",
}


def _collection(engine: str = "sqlserver", views: dict | None = None, queries=None) -> dict:
    return {
        "metadata": {"database_name": "shop", "engine": engine},
        "tables": [{"table_name": n, "schema_name": "dbo", "row_count": 1000} for n in TABLES],
        "columns": [
            {"table_name": n, "column_name": c, "data_type": "int", "ordinal_position": i}
            for n, cols in TABLES.items()
            for i, c in enumerate(cols, start=1)
        ],
        "primary_keys": [
            {"table_name": n, "column_name": c, "ordinal_position": i}
            for n, cols in PRIMARY_KEYS.items()
            for i, c in enumerate(cols, start=1)
        ],
        "views": [
            {"view_name": name, "schema_name": "dbo", "definition": text}
            for name, text in (VIEWS if views is None else views).items()
        ],
        "queries": [
            {"query_text": text, "digest": qid, "execution_count": 10}
            for qid, text in (QUERIES if queries is None else queries).items()
        ],
    }


def _output(engine: str = "sqlserver", **kwargs) -> dict:
    parsed = parse_offline_collection(_collection(engine, **kwargs))
    return build_offline_collector_output(parsed, job_id="j", database_name="shop", s3_key="k")


@pytest.fixture(scope="module")
def output() -> dict:
    return _output()


def _query(output: dict, qid: str) -> dict:
    return next(q for q in output["queries"]["query_patterns"] if q["query_id"] == qid)


def _view(output: dict, name: str) -> dict:
    return next(v for v in output["database_schema"]["views"] if v["view_name"] == name)


def test_a_simple_view_reads_its_one_table(output: dict) -> None:
    query = _query(output, "q_orders")
    assert query["tables_accessed"] == ["shop.Orders"]
    assert query["views_read"] == [
        {"view_id": "vw_Orders", "base_tables": ["shop.Orders"], "written_tables": None}
    ]
    assert _view(output, "vw_Orders")["lineage_status"] == "resolved"
    assert _view(output, "vw_Orders")["referenced_tables"] == ["shop.Orders"]


def test_a_view_joining_two_tables_reads_both_with_its_join(output: dict) -> None:
    on_view, direct = _query(output, "q_users"), _query(output, "q_direct")
    assert on_view["tables_accessed"] == ["shop.Users", "shop.UserPortals"]
    assert on_view["tables_accessed"] == direct["tables_accessed"]
    assert (on_view["has_joins"], on_view["join_count"]) == (True, 1)
    assert (on_view["has_joins"], on_view["join_count"]) == (
        direct["has_joins"],
        direct["join_count"],
    )


def test_a_view_on_a_view_resolves_transitively(output: dict) -> None:
    view = _view(output, "vw_ActiveUsers")
    assert view["lineage_status"] == "resolved"
    assert view["referenced_tables"] == ["shop.Users", "shop.UserPortals"]
    assert view["referenced_views"] == ["vw_Users"]
    query = _query(output, "q_active")
    assert query["tables_accessed"] == ["shop.Users", "shop.UserPortals"]
    assert query["join_count"] == 1


def test_a_cycle_of_views_is_unresolved_and_says_so(output: dict) -> None:
    for name in ("vw_A", "vw_B"):
        view = _view(output, name)
        assert view["lineage_status"] == "unresolved"
        assert view["referenced_tables"] == []
        assert "cycle of views" in view["lineage_reason"]
        assert "vw_A" in view["lineage_reason"] and "vw_B" in view["lineage_reason"]
    query = _query(output, "q_cycle")
    assert query["tables_accessed"] == ["dbo.vw_A"]  # no base table: the view stays named
    assert query["views_read"] == [{"view_id": "vw_A", "base_tables": [], "written_tables": None}]


def test_a_view_with_no_definition_is_unresolved_with_its_reason(output: dict) -> None:
    view = _view(output, "vw_Missing")
    assert (view["lineage_status"], view["lineage_reason"]) == ("unresolved", NO_DEFINITION)
    assert _query(output, "q_missing")["tables_accessed"] == ["dbo.vw_Missing"]


def test_a_definition_that_does_not_parse_is_reported_as_such(output: dict) -> None:
    view = _view(output, "vw_Broken")
    assert view["lineage_status"] == "unresolved"
    assert "could not be parsed (parse_error)" in view["lineage_reason"]
    assert _query(output, "q_broken")["tables_accessed"] == ["dbo.vw_Broken"]


def test_a_view_reading_an_unresolved_view_is_partial() -> None:
    views = {
        "vw_Missing": None,
        "vw_Mixed": "CREATE VIEW dbo.vw_Mixed AS SELECT o.OrderId FROM dbo.Orders o "
        "JOIN dbo.vw_Missing m ON m.id = o.OrderId",
    }
    out = _output(views=views, queries={"q": "SELECT * FROM dbo.vw_Mixed"})
    view = _view(out, "vw_Mixed")
    assert view["lineage_status"] == "partial"
    assert view["referenced_tables"] == ["shop.Orders"]
    assert (
        view["lineage_reason"] == f"it reads view vw_Missing, which is unresolved: {NO_DEFINITION}"
    )
    # The unresolved view it reads stays named next to the table that is known.
    assert _query(out, "q")["tables_accessed"] == ["shop.Orders", "vw_Missing"]


def test_a_write_through_a_view_writes_its_base_table_not_what_it_reads(output: dict) -> None:
    insert = _query(output, "w_insert")
    assert insert["tables_accessed"] == ["shop.Orders", "shop.Products"]
    assert insert["views_read"][0]["written_tables"] == ["shop.Orders"]
    assert written_tables(insert, "sqlserver") == ["shop.Orders"]
    assert inserted_tables(insert, "sqlserver") == ["shop.Orders"]


def test_a_write_through_a_join_view_writes_the_table_its_columns_come_from(
    output: dict,
) -> None:
    assert written_tables(_query(output, "w_portal"), "sqlserver") == ["shop.UserPortals"]
    assert written_tables(_query(output, "w_nested"), "sqlserver") == ["shop.Users"]
    # Columns of two tables: the write cannot be placed on one, so it keeps both together.
    assert written_tables(_query(output, "w_mixed"), "sqlserver") == [
        "shop.Users",
        "shop.UserPortals",
    ]


def test_one_writer_per_table_holds_for_writes_through_views(output: dict) -> None:
    groups = build_write_groups(output["queries"]["query_patterns"], "sqlserver")
    assert groups["shop.Orders"] == ["w_insert"]
    assert "shop.Products" not in groups
    assert groups["shop.UserPortals"] == ["w_portal", "w_mixed"]
    assert groups["shop.Users"] == ["w_mixed", "w_nested"]


def _analysis(table_ids: list[str]) -> dict:
    breakdown = {
        "pattern_match_score": 50,
        "complexity_score": 70,
        "performance_score": 60,
        "cost_score": 70,
    }
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": 65, "score_breakdown": dict(breakdown)}
            for t in table_ids
        ],
    }


@pytest.mark.parametrize("engine", ["aurora_postgresql", "dynamodb"])
def test_the_scorer_rates_a_view_query_on_the_base_tables_a_direct_join_reads(
    output: dict, engine: str
) -> None:
    """The one scorer's table dimensions for a query on a view are those of the query
    that joins the base tables directly; before #1875 the view had no rated table, so
    every engine scored it 0."""
    analysis = _analysis(["shop.Users", "shop.UserPortals", "shop.Orders", "shop.Products"])
    model = QueryPatternModel.from_pipeline(output, {engine: analysis})
    rows = {}
    for qid in ("q_users", "q_direct"):
        trace: dict = {}
        compute_query_confidence(
            qid,
            _query(output, qid),
            engine,
            analysis,
            pattern_model=model,
            cost_score=70,
            trace=trace,
        )
        rows[qid] = trace["tables"]
    assert [r["table_id"] for r in rows["q_users"]] == ["shop.Users", "shop.UserPortals"]
    assert rows["q_users"] == rows["q_direct"]
    assert all(r["score"] > 0 for r in rows["q_users"])


def test_a_tie_on_an_unresolved_view_says_why(output: dict) -> None:
    qa = {
        "query_id": "q_missing",
        "assigned_engine": "dynamodb",
        "source_tables": ["dbo.vw_Missing"],
        "tied_engines": ["aurora_postgresql", "dynamodb"],
        "in_scope": True,
    }
    (choice,) = build_pending_choices([qa], output, None)
    assert "vw_Missing is a view whose base tables are unknown" in choice.why_tied
    assert NO_DEFINITION in choice.why_tied
    assert "every engine scores it the same" in choice.why_tied


def test_a_tie_on_a_resolved_view_has_no_view_note(output: dict) -> None:
    qa = {
        "query_id": "q_orders",
        "assigned_engine": "dynamodb",
        "source_tables": ["shop.Orders"],
        "tied_engines": ["aurora_postgresql", "dynamodb"],
        "in_scope": True,
    }
    (choice,) = build_pending_choices([qa], output, None)
    assert "base tables are unknown" not in choice.why_tied


def test_a_query_with_no_view_is_unchanged() -> None:
    plain = {"q_direct": QUERIES["q_direct"]}
    with_views = _query(_output(queries=plain), "q_direct")
    without = _query(_output(views={}, queries=plain), "q_direct")
    assert with_views == without
    assert with_views["views_read"] is None


def test_applying_twice_changes_nothing(output: dict) -> None:
    again = apply_view_lineage(copy.deepcopy(output))
    assert again == output


def test_it_applies_to_a_contract_model_too(output: dict) -> None:
    """The live collectors resolve on the model they build."""
    raw = copy.deepcopy(output)
    for q in raw["queries"]["query_patterns"]:
        q["views_read"] = None
    raw["queries"]["query_patterns"] = [
        {**q, "tables_accessed": ["dbo.vw_Users"], "join_count": 0, "has_joins": False}
        for q in raw["queries"]["query_patterns"]
        if q["query_id"] == "q_users"
    ]
    model = CollectorOutputContract.model_validate(raw)
    apply_view_lineage(model, "sqlserver")
    (pattern,) = model.queries.query_patterns
    assert pattern.tables_accessed == ["shop.Users", "shop.UserPortals"]
    assert pattern.join_count == 1
    assert pattern.views_read is not None and pattern.views_read[0].view_id == "vw_Users"


def test_a_library_callers_collector_output_is_resolved_too(output: dict) -> None:
    """``rederive_query_facts`` reads the facts again and resolves the views again,
    whatever the caller wrote."""
    given = copy.deepcopy(output)
    for q in given["queries"]["query_patterns"]:
        q["views_read"] = None
        q["tables_accessed"] = ["unknown"]
    for v in given["database_schema"]["views"]:
        v["referenced_tables"] = ["made.up"]
        v["lineage_status"] = None
    out = rederive_query_facts(given, "sqlserver")
    assert _query(out, "q_users")["tables_accessed"] == ["shop.Users", "shop.UserPortals"]
    assert _query(out, "q_users")["join_count"] == 1
    assert _view(out, "vw_Users")["referenced_tables"] == ["shop.Users", "shop.UserPortals"]


@pytest.mark.parametrize(
    "engine,definition",
    [
        # MySQL's information_schema.views.view_definition: the body only, backquoted.
        (
            "mysql",
            "select `u`.`UserId` AS `UserId`,`up`.`PortalId` AS `PortalId` from "
            "(`shop`.`Users` `u` left join `shop`.`UserPortals` `up` "
            "on((`u`.`UserId` = `up`.`UserId`)))",
        ),
        # PostgreSQL's pg_get_viewdef: the body only.
        (
            "postgresql",
            " SELECT u.userid, up.portalid FROM (users u LEFT JOIN userportals up "
            "ON ((u.userid = up.userid)));",
        ),
    ],
)
def test_mysql_and_postgresql_views_resolve_on_the_same_path(engine: str, definition: str) -> None:
    views = {"vw_users": definition, "vw_none": None}
    queries = {"q": "SELECT * FROM vw_users WHERE UserId = 1", "n": "SELECT * FROM vw_none"}
    out = _output(engine, views=views, queries=queries)
    # #1903: a PostgreSQL table is named after its schema, a MySQL one after the database.
    qualifier = "dbo" if engine == "postgresql" else "shop"
    assert {t.lower() for t in _query(out, "q")["tables_accessed"]} == {
        f"{qualifier}.users",
        f"{qualifier}.userportals",
    }
    assert _query(out, "q")["join_count"] == 1
    assert _view(out, "vw_none")["lineage_reason"] == NO_DEFINITION


def test_a_read_on_views_gets_the_access_class_of_the_direct_join(output: dict) -> None:
    model = QueryPatternModel.from_pipeline(output, {})
    direct = model.classification(_query(output, "q_direct"))
    assert _query(output, "q_users")["view_resolved_text"]
    assert model.classification(_query(output, "q_users")) == direct
    # The view's own WHERE (IsDeleted = 0) is part of the merged read too.
    assert "isdeleted = 0" in _query(output, "q_active")["view_resolved_text"].lower()


def test_a_read_of_an_aggregating_view_classifies_as_its_body_in_a_derived_table() -> None:
    """Review #1883 round 1, example 2: a view that does not merge is kept as its body, a
    derived table, so the statement carries its aggregation and is classified as the
    same body written as a derived table."""
    views = {
        "vw_Totals": "CREATE VIEW dbo.vw_Totals AS SELECT o.UserId, SUM(o.Total) AS Total "
        "FROM dbo.Orders o GROUP BY o.UserId",
    }
    queries = {
        "v": "SELECT * FROM dbo.vw_Totals WHERE UserId = @u",
        "d": "SELECT * FROM (SELECT o.UserId, SUM(o.Total) AS Total FROM dbo.Orders o "
        "GROUP BY o.UserId) AS vw_Totals WHERE UserId = @u",
    }
    out = _output(views=views, queries=queries)
    on_view, derived = _query(out, "v"), _query(out, "d")
    assert on_view["tables_accessed"] == derived["tables_accessed"] == ["shop.Orders"]
    assert (on_view["view_text_form"], on_view["view_text_fallback"]) == ("derived", None)
    assert on_view["has_aggregations"] is derived["has_aggregations"] is True
    model = QueryPatternModel.from_pipeline(out, {})
    assert model.classification(on_view) == model.classification(derived)
    assert model.classification(on_view).name != "ad_hoc_filter"


def test_a_write_through_a_view_classifies_as_the_write_on_its_base_table() -> None:
    """Review #1883 round 1, example 1: an UPDATE through a column-preserving view is
    written onto its base table and gets the base-table statement's class."""
    queries = {
        "v": "UPDATE dbo.vw_Orders SET Total = @t WHERE OrderId = @id",
        "b": "UPDATE dbo.Orders SET Total = @t WHERE OrderId = @id",
    }
    out = _output(queries=queries)
    on_view, base = _query(out, "v"), _query(out, "b")
    assert on_view["view_text_form"] == "base_write"
    model = QueryPatternModel.from_pipeline(out, {})
    assert model.classification(on_view).name == model.classification(base).name == "key_lookup"
    assert written_tables(on_view, "sqlserver") == ["shop.Orders"]


def test_a_write_through_a_nested_view_lands_on_the_base_table(output: dict) -> None:
    query = _query(output, "w_nested")  # UPDATE vw_ActiveUsers SET Username ... (Users)
    assert query["view_text_form"] == "base_write"
    assert "users" in query["view_resolved_text"].lower()
    assert "vw_" not in query["view_resolved_text"].lower()


def test_a_write_that_cannot_be_mapped_says_why_and_reads_as_written(output: dict) -> None:
    """``SET IsDeleted`` (UserPortals) ``WHERE UserId`` (Users) through vw_Users spans two
    tables: no base-table text, and the fallback is stated in the fact and the reason."""
    query = _query(output, "w_portal")
    assert query["view_resolved_text"] is None
    assert (
        query["view_text_fallback"] == "its columns come from more than one table of view vw_users"
    )
    result = QueryPatternModel.from_pipeline(output, {}).classification(query)
    assert result.reason.endswith(f"(read as written: {query['view_text_fallback']})")


def test_a_statement_on_an_unresolved_view_says_why_it_reads_as_written(output: dict) -> None:
    assert _query(output, "q_missing")["view_text_fallback"] == (
        f"it reads view vw_Missing, which is unresolved: {NO_DEFINITION}"
    )


def test_a_common_table_expression_named_like_a_view_is_not_the_view() -> None:
    """Review suggestion 4: a CTE that shares a view's name stays the CTE."""
    sql = (
        "WITH vw_Users AS (SELECT ProductId AS UserId FROM dbo.Products) "
        "SELECT * FROM vw_Users u JOIN dbo.vw_Orders o ON o.UserId = u.UserId"
    )
    out = _output(queries={"q": sql})
    text = _query(out, "q")["view_resolved_text"].lower()
    assert "products" in text and "orders" in text
    assert "userportals" not in text


def test_mysql_placeholders_stay_placeholders_in_the_resolved_text() -> None:
    """Review suggestion 5: the MySQL family's ``?`` is not written back as ``NULL``."""
    views = {"vw_users": "select `u`.`UserId` AS `UserId` from `shop`.`Users` `u`"}
    out = _output(
        "mysql", views=views, queries={"q": "SELECT * FROM vw_users WHERE UserId = ? LIMIT ?"}
    )
    text = _query(out, "q")["view_resolved_text"]
    assert "NULL" not in text.upper() and text.count("?") == 2
    # A text with a NULL of its own cannot tell the two apart: read as written, and said.
    both = "SELECT * FROM vw_users WHERE UserId = ? OR UserId IS NULL"
    q = _query(_output("mysql", views=views, queries={"q": both}), "q")
    assert q["view_resolved_text"] is None and "placeholders" in q["view_text_fallback"]


def test_an_unqualified_self_name_in_a_view_is_the_table_it_reads() -> None:
    """Review suggestion 6: ``reporting.users AS SELECT ... FROM users`` reads the table."""
    data = _collection("postgresql", views={}, queries={"q": "SELECT * FROM reporting.users"})
    data["views"] = [
        {
            "view_name": "users",
            "schema_name": "reporting",
            "definition": "SELECT u.UserId FROM users u",
        }
    ]
    data["tables"].append({"table_name": "users", "schema_name": "public", "row_count": 10})
    data["columns"].append(
        {"table_name": "users", "column_name": "UserId", "data_type": "int", "ordinal_position": 1}
    )
    parsed = parse_offline_collection(data)
    out = build_offline_collector_output(parsed, job_id="j", database_name="shop", s3_key="k")
    (view,) = out["database_schema"]["views"]
    assert view["lineage_status"] == "resolved"
    assert view["referenced_tables"] == ["public.users"]


def test_the_resolved_text_is_redacted_like_a_query_text() -> None:
    """Review suggestion 8: a literal in ``view_resolved_text`` never survives redaction."""
    from src.tools.sql.redact import redact_values

    out = redact_values(
        {"view_resolved_text": "SELECT * FROM orders WHERE note = 'secret'"}, "sqlserver"
    )
    assert "secret" not in out["view_resolved_text"]


@pytest.mark.parametrize("module", ["sqlserver", "postgres", "oracle"])
def test_the_live_collectors_resolve_views_too(output: dict, module: str) -> None:
    """A live collection builds its own output; it resolves its views as an offline one."""
    from unittest.mock import MagicMock, patch

    from src.agents.collector import oracle_collector, postgres_collector, sqlserver_collector
    from src.contracts.collector_input import CollectionMode, CollectorInput

    raw = copy.deepcopy(output)
    raw["queries"]["query_patterns"] = [
        {**q, "tables_accessed": ["dbo.vw_Users"], "views_read": None, "join_count": 0}
        for q in raw["queries"]["query_patterns"]
        if q["query_id"] == "q_users"
    ]
    collector = {
        "sqlserver": sqlserver_collector,
        "postgres": postgres_collector,
        "oracle": oracle_collector,
    }[module]
    store = MagicMock()
    store.exists.return_value = False
    inp = MagicMock(spec=CollectorInput)
    inp.mode = CollectionMode.live
    inp.job_id = "live"
    # PostgreSQL resolves inside its live stages' source settings (#1482), the others
    # after them.
    stage = "_collect_live_stages" if module == "postgres" else "_collect_live"
    with (
        patch.object(collector, "_init_checkpoint_store", return_value=store),
        patch.object(collector, stage, return_value=CollectorOutputContract.model_validate(raw)),
    ):
        result = collector.collect(inp)
    (pattern,) = result.queries.query_patterns
    assert pattern.tables_accessed == ["shop.Users", "shop.UserPortals"]


def test_live_postgresql_resolves_views_with_the_source_settings(output: dict) -> None:
    """Review suggestion 7: the views and statements are read with the settings the
    metadata stage made active, not the defaults after the collection ended."""
    from unittest.mock import MagicMock, patch

    from src.agents.collector import postgres_collector
    from src.shared.sql_spans import active_span_settings, span_settings, use_span_settings

    source = span_settings("postgresql", {"standard_conforming_strings": "off"})
    seen = []

    def stages(inp, ckpt):
        use_span_settings(source)  # what the metadata stage does
        return CollectorOutputContract.model_validate(copy.deepcopy(output))

    def spy(out, engine):
        seen.append(active_span_settings())
        return out

    with (
        patch.object(postgres_collector, "_collect_live_stages", side_effect=stages),
        patch.object(postgres_collector, "apply_view_lineage", side_effect=spy),
    ):
        postgres_collector._collect_live(MagicMock(), MagicMock())
    assert seen == [source]
    assert active_span_settings() != source


def _group(output: dict, qids: list[str], tables: list[str]) -> dict:
    from src.agents.schema_design.group_input import build_group_input

    return build_group_input(
        job_id="j",
        database_name="shop",
        engine="dynamodb",
        group_index=0,
        group_name="g",
        primary_tables=tables,
        group_queries=[_query(output, q) for q in qids],
        group_tables=[t for t in output["database_schema"]["tables"] if t["table_id"] in tables],
        collector_output=output,
        analysis_output={},
        total_queries=len(qids),
    )


def test_a_design_group_sees_the_views_its_queries_read(output: dict) -> None:
    """The designer meets a view with its definition and lineage, so a migration note
    says a view has no source tables only when the collection holds none (#1875)."""
    views = _group(output, ["q_users", "q_missing"], ["shop.Users", "shop.UserPortals"])[
        "collector_output"
    ]["database_schema"]["views"]
    by_id = {v["view_id"]: v for v in views}
    assert set(by_id) == {"vw_Users", "vw_Missing"}
    assert by_id["vw_Users"]["referenced_tables"] == ["shop.Users", "shop.UserPortals"]
    assert by_id["vw_Users"]["definition"].startswith("CREATE VIEW")
    assert (by_id["vw_Missing"]["lineage_status"], by_id["vw_Missing"]["lineage_reason"]) == (
        "unresolved",
        NO_DEFINITION,
    )


def test_a_design_group_with_no_view_query_lists_no_views(output: dict) -> None:
    schema = _group(output, ["q_direct"], ["shop.Users", "shop.UserPortals"])["collector_output"][
        "database_schema"
    ]
    assert "views" not in schema


def test_lookups_through_a_view_are_workload_key_evidence() -> None:
    """Two lookups of Orders by UserId through vw_Orders make UserId a workload key of
    Orders, as the same two lookups on the table do (``_workload_keys``)."""
    from src.agents.referee.access_pattern import SchemaKeys

    lookups = {
        "a": "SELECT OrderId FROM dbo.{} WHERE UserId = @u",
        "b": "SELECT Total FROM dbo.{} WHERE UserId = @u ORDER BY OrderId",
    }
    keys = {}
    for target in ("vw_Orders", "Orders"):
        out = _output(queries={q: t.format(target) for q, t in lookups.items()})
        keys[target] = SchemaKeys.from_collector(out).tables["shop.Orders"].workload_keys
    assert keys["vw_Orders"] == keys["Orders"] == frozenset({"userid"})


def test_query_access_and_redesign_read_a_view_query_as_its_direct_join(output: dict) -> None:
    from src.agents.referee.access_pattern import SchemaKeys, query_access

    queries = {
        "v": "SELECT * FROM dbo.vw_Orders WHERE UserId = @u",
        "d": "SELECT o.OrderId, o.UserId, o.Total FROM dbo.Orders AS o WHERE o.UserId = @u",
    }
    out = _output(queries=queries)
    keys = SchemaKeys.from_collector(out)
    on_view, direct = _query(out, "v"), _query(out, "d")
    assert query_access(on_view, keys, "sqlserver") == query_access(direct, keys, "sqlserver")
    model = QueryPatternModel.from_pipeline(out, {})
    redesign = model.redesign(direct)
    assert redesign is not None
    assert model.redesign(on_view) == redesign


@pytest.mark.parametrize(
    "body",
    [
        "SELECT o.UserId, SUM(o.Total) AS Total FROM dbo.Orders o GROUP BY o.UserId",
        "SELECT DISTINCT o.UserId FROM dbo.Orders o",
        "SELECT TOP 10 o.UserId FROM dbo.Orders o ORDER BY o.Total",
        "SELECT o.UserId FROM dbo.Orders o UNION SELECT p.ProductId FROM dbo.Products p",
    ],
    ids=["aggregation", "distinct", "top", "union"],
)
def test_views_that_do_not_merge(body: str) -> None:
    """They are kept as their body, a derived table, whose signals the statement keeps."""
    from src.tools.sql.facade import resolve_view_text

    columns = {"orders": ("orderid", "userid", "total"), "products": ("productid", "name")}
    views = {"v": f"CREATE VIEW dbo.v AS {body}"}
    found = resolve_view_text("SELECT * FROM dbo.v WHERE UserId = @u", "sqlserver", views, columns)
    assert found.form == "derived"
    assert found.text is not None and "(SELECT" in found.text.upper()


def test_a_view_that_names_no_collected_table_is_unresolved() -> None:
    views = {"vw_Remote": "CREATE VIEW dbo.vw_Remote AS SELECT r.id FROM OtherDb.dbo.Remote r"}
    out = _output(views=views, queries={"q": "SELECT * FROM dbo.vw_Remote"})
    view = _view(out, "vw_Remote")
    assert view["lineage_status"] == "unresolved"
    assert view["lineage_reason"] == (
        "it reads OtherDb.dbo.Remote, which the collection has no table for"
    )
    assert _query(out, "q")["tables_accessed"] == ["dbo.vw_Remote"]


def test_view_resolution_is_part_of_the_ruleset_hash() -> None:
    """A result's ``ruleset_hash`` covers the view resolution, so the 1.3 value changes
    for collections with views are reported as a new ruleset (docs/contracts/assess.md)."""
    from src.assessment import ruleset

    covered = {p.relative_to(ruleset._SRC).as_posix() for p in ruleset.ruleset_files()}
    assert {"tools/sql/view_lineage.py", "tools/sql/facade.py"} <= covered


def test_triage_and_the_capability_gates_read_the_views_body() -> None:
    """A read of an aggregating view carries the aggregation signal and capability its
    body needs, as the derived-table statement does."""
    from src.agents.referee.triage import detect_query_capabilities, triage

    views = {
        "vw_Totals": "CREATE VIEW dbo.vw_Totals AS SELECT o.UserId, SUM(o.Total) AS Total "
        "FROM dbo.Orders o GROUP BY o.UserId",
    }
    out = _output(views=views, queries={"v": "SELECT * FROM dbo.vw_Totals WHERE UserId = @u"})
    result = triage(out)
    signals = {s.signal for s in result.signals if "v" in s.query_ids}
    assert "aggregations" in signals
    assert "aggregation" in detect_query_capabilities(out, result.signals).get("v", [])


def test_a_write_through_a_filtered_view_keeps_the_views_filter() -> None:
    """The view's own filter on the base table is the write's too: an UPDATE through it
    changes only the rows the view shows."""
    from src.tools.sql.facade import resolve_view_text

    views = {
        "vw_big": "CREATE VIEW dbo.vw_Big AS SELECT o.OrderId, o.Total FROM dbo.Orders o "
        "WHERE o.Total > 100"
    }
    found = resolve_view_text(
        "UPDATE dbo.vw_Big SET Total = @t WHERE OrderId = @id",
        "sqlserver",
        views,
        {"orders": ("orderid", "userid", "total")},
    )
    assert found.form == "base_write"
    text = found.text.lower()
    assert "update orders" in text and "orderid = @id" in text and "total > 100" in text


def test_a_capability_the_views_body_needs_is_the_statements() -> None:
    """A join on a computed key inside the view is a capability the statement needs."""
    from src.agents.referee.triage import detect_query_capabilities, triage

    views = {
        "vw_Named": "CREATE VIEW dbo.vw_Named AS SELECT u.UserId, o.Total FROM dbo.Users u "
        "JOIN dbo.Orders o ON LOWER(u.Username) = o.Total",
    }
    out = _output(views=views, queries={"v": "SELECT * FROM dbo.vw_Named WHERE UserId = @u"})
    caps = detect_query_capabilities(out, triage(out).signals).get("v", [])
    assert "computed_join" in caps, caps


def test_the_single_table_access_shape_reads_the_views_body() -> None:
    """A filter on an aggregating one-table view is not single-table CRUD: its body
    aggregates, as the derived-table statement's does."""
    from src.agents.referee.query_shape import RELATIONAL, single_table_access_shape

    views = {
        "vw_Totals": "CREATE VIEW dbo.vw_Totals AS SELECT o.UserId, SUM(o.Total) AS Total "
        "FROM dbo.Orders o GROUP BY o.UserId",
    }
    out = _output(views=views, queries={"v": "SELECT * FROM dbo.vw_Totals WHERE UserId = @u"})
    assert single_table_access_shape(_query(out, "v")) == RELATIONAL


def test_a_three_table_view_hits_the_hard_exclusion_its_direct_join_does() -> None:
    """Review #1883 round 2: the hard exclusions read the resolved text too, so a read
    of a view joining three tables is excluded from ElastiCache as a hard exclusion
    (``ec-no-complex-join``), with the same gate as the same direct join."""
    from src.agents.referee.query_scoring import (
        GATE_KIND_HARD_EXCLUSION,
        ScoringContext,
        _structural_gate,
    )

    views = {
        "vw_Three": "CREATE VIEW dbo.vw_Three AS SELECT o.OrderId, u.Username, up.PortalId "
        "FROM dbo.Orders o JOIN dbo.Users u ON u.UserId = o.UserId "
        "JOIN dbo.UserPortals up ON up.UserId = u.UserId",
    }
    queries = {
        "v": "SELECT * FROM dbo.vw_Three WHERE OrderId = @id",
        "d": "SELECT o.OrderId, u.Username, up.PortalId FROM dbo.Orders o "
        "JOIN dbo.Users u ON u.UserId = o.UserId JOIN dbo.UserPortals up "
        "ON up.UserId = u.UserId WHERE o.OrderId = @id",
    }
    out = _output(views=views, queries=queries)
    ctx = ScoringContext.from_pipeline({}, {"query_capabilities": {}}, out)
    gates = {q: _structural_gate(_query(out, q), "elasticache", ctx) for q in queries}
    assert gates["v"] is not None and gates["d"] is not None
    assert gates["v"].gate_kind == gates["d"].gate_kind == GATE_KIND_HARD_EXCLUSION
    assert gates["v"].gate_reason == gates["d"].gate_reason


def test_the_cache_overlay_does_not_offer_a_cache_for_an_aggregating_view() -> None:
    """Review #1883 round 2: the cache overlay reads the view's body, so a read of an
    aggregating view is no point lookup."""
    from src.agents.referee.cache_overlay import cache_pattern

    views = {
        "vw_Totals": "CREATE VIEW dbo.vw_Totals AS SELECT o.UserId, SUM(o.Total) AS Total "
        "FROM dbo.Orders o GROUP BY o.UserId",
    }
    out = _output(views=views, queries={"v": "SELECT * FROM dbo.vw_Totals WHERE UserId = @u"})
    assert cache_pattern(_query(out, "v"), "sqlserver") is None


def test_the_search_floor_reads_a_full_text_view_as_deep() -> None:
    """Review #1883 round 2: a read of a view whose body is a full-text search has the
    search depth the search floor counts."""
    from src.agents.referee.reality_check_floors import _query_has_search_depth
    from src.tools.sql.view_lineage import access_text

    views = {
        "vw_search": "SELECT p.id FROM posts p WHERE to_tsvector('english', p.raw) "
        "@@ plainto_tsquery('english', 'x')",
    }
    data = _collection(
        "postgresql", views=views, queries={"q": "SELECT id FROM vw_search WHERE id = $1"}
    )
    data["tables"].append({"table_name": "posts", "schema_name": "public", "row_count": 10})
    data["columns"] += [
        {"table_name": "posts", "column_name": c, "data_type": "text", "ordinal_position": i}
        for i, c in enumerate(("id", "raw"), start=1)
    ]
    out = build_offline_collector_output(
        parse_offline_collection(data), job_id="j", database_name="shop", s3_key="k"
    )
    query = _query(out, "q")
    assert not _query_has_search_depth(query["query_text"], "postgresql")
    assert _query_has_search_depth(access_text(query), "postgresql")


_ORDER_COLUMNS = {"orders": ("orderid", "userid", "total", "status")}


def test_a_write_through_a_cte_bodied_view_is_never_written_onto_the_cte() -> None:
    """Review #1883 round 2, Important 5: ``vw_cte``'s body is a CTE, so the write is not
    rewritten onto ``x``; it reads as written, and says why."""
    from src.tools.sql.facade import resolve_view_text

    views = {
        "vw_cte": "CREATE VIEW vw_cte AS WITH x AS (SELECT OrderId, Total FROM dbo.Orders) "
        "SELECT OrderId, Total FROM x"
    }
    found = resolve_view_text(
        "UPDATE vw_cte SET Total = @t WHERE OrderId = @id", "sqlserver", views, _ORDER_COLUMNS
    )
    assert found.text is None
    assert found.fallback == (
        "view vw_cte reads a common table expression, which the rewrite does not map onto "
        "a base table"
    )
    out = _output(views=views, queries={"w": "UPDATE vw_cte SET Total = @t WHERE OrderId = @id"})
    query = _query(out, "w")
    assert query["view_resolved_text"] is None
    assert written_tables(query, "sqlserver") == ["shop.Orders"]
    reason = QueryPatternModel.from_pipeline(out, {}).classification(query).reason
    assert " x " not in f" {reason} " and "read as written" in reason


def test_a_null_in_a_mysql_views_body_stays_null() -> None:
    """Review #1883 round 2, Important 5 (MySQL NULL): only the statement's own ``?``
    placeholders are put back; a soft-delete view's ``IS NULL`` stays ``NULL``."""
    from src.tools.sql.facade import resolve_view_text

    views = {"vw_live": "select `u`.`id` AS `id` from `users` `u` where `u`.`deleted_at` is null"}
    found = resolve_view_text(
        "SELECT * FROM vw_live WHERE id = ?",
        "mysql",
        views,
        {"users": ("id", "deleted_at")},
    )
    assert found.text is not None
    assert "IS NULL" in found.text.upper() and found.text.count("?") == 1


def test_a_view_with_a_column_list_is_mapped_through_it() -> None:
    from src.tools.sql.facade import resolve_view_text

    views = {"vwc": "CREATE VIEW vwc (Id, Amt) AS SELECT o.OrderId, o.Total FROM dbo.Orders o"}
    write = resolve_view_text(
        "UPDATE vwc SET Amt = @a WHERE Id = @id", "sqlserver", views, _ORDER_COLUMNS
    )
    assert write.form == "base_write"
    assert "set total = @a where orderid = @id" in write.text.lower()
    read = resolve_view_text(
        "SELECT Amt FROM vwc WHERE Id = @id", "sqlserver", views, _ORDER_COLUMNS
    )
    assert read.form == "merged" and "orderid = @id" in read.text.lower()


def test_a_delete_with_no_where_through_a_one_table_view_keeps_the_views_filter() -> None:
    from src.tools.sql.facade import resolve_view_text

    views = {
        "vw_open": "CREATE VIEW vw_open AS SELECT o.OrderId, o.Status FROM dbo.Orders o "
        "WHERE o.Status = 'open'"
    }
    found = resolve_view_text("DELETE FROM vw_open", "sqlserver", views, _ORDER_COLUMNS)
    assert found.form == "base_write"
    assert found.text.lower() == "delete from orders where status = 'open'"


def test_a_planted_literal_in_the_resolved_text_is_redacted_on_every_read(output: dict) -> None:
    """Review #1883 round 2, Important 4: an uploaded or stored collector output whose
    ``view_resolved_text`` holds a literal is redacted on read (``read_contract_payload``,
    what ``run_assessment.py --file`` and ``assess()`` read through) and by the contract
    itself, as its ``query_text`` is."""
    from src.shared.contract_versioning import read_contract_payload

    planted = copy.deepcopy(output)
    pattern = next(q for q in planted["queries"]["query_patterns"] if q["query_id"] == "q_users")
    pattern["query_text"] = "SELECT * FROM dbo.vw_Users WHERE Email = 'jane@example.com'"
    pattern["view_resolved_text"] = "SELECT * FROM users AS u WHERE u.email = 'jane@example.com'"
    read = read_contract_payload(planted, CollectorOutputContract, artifact="upload")
    row = next(q for q in read["queries"]["query_patterns"] if q["query_id"] == "q_users")
    assert "jane@example.com" not in row["query_text"]
    assert "jane@example.com" not in row["view_resolved_text"]
    model = CollectorOutputContract.model_validate(planted)
    (found,) = [q for q in model.queries.query_patterns if q.query_id == "q_users"]
    assert "jane@example.com" not in (found.view_resolved_text or "")
    assert "jane@example.com" not in found.query_text


def test_a_statement_that_fills_a_temp_table_from_a_view_is_routed_on_the_base_tables() -> None:
    """With #1876: ``INSERT INTO #page (UserId) SELECT UserId FROM dbo.vw_Users`` writes
    scratch rows but reads application data through the view. Its tables are the view's
    base tables, so it is not procedure-internal and is routed on them; the same text on
    the temp table alone stays procedure-internal."""
    from src.agents.referee.table_resolution import SourceScope
    from src.agents.referee.utility_statements import procedure_internal_reason
    from src.tools.sql.view_lineage import access_text

    queries = {
        "v": "INSERT INTO #page (UserId) SELECT UserId FROM dbo.vw_Users WHERE PortalId = @p",
        "t": "SELECT COUNT(*) FROM #page",
    }
    out = _output(queries=queries)
    source = SourceScope.from_collector(out)
    on_view, temp = _query(out, "v"), _query(out, "t")
    assert on_view["tables_accessed"] == ["shop.Users", "shop.UserPortals"]
    assert (
        procedure_internal_reason(access_text(on_view), on_view["tables_accessed"], source) is None
    )
    assert procedure_internal_reason(access_text(temp), temp["tables_accessed"], source) is not None
