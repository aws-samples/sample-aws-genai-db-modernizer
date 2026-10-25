"""Table ids follow the collected database, never the run's name for it (#1572).

A run is named by ``--db``, by the collection file name (``/modernize``) or by
``assess()``'s ``database_name``. The parser qualifies each statement's
``tables_accessed`` with the database the collection holds, so the tables must be
keyed the same way: keyed by the run's name instead, no statement matched a
table and every one went to Aurora on the tie rule. A name that is ambiguous (it
names another database the collection's statements belong to, or the collection
states no database and its statements belong to several others) is refused.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.offline_parser import parse_offline_collection
from tests.unit.assessment.conftest import SAMPLES, sample_collection


def _collector(raw: dict[str, Any], name: str, *, tell_parser: bool = True) -> dict[str, Any]:
    parsed = (
        parse_offline_collection(raw, database_name=name)
        if tell_parser
        else parse_offline_collection(raw)
    )
    return build_offline_collector_output(parsed, job_id="job", database_name=name, s3_key="k")


def _ids(out: dict[str, Any]) -> set[str]:
    return {t["table_id"] for t in out["database_schema"]["tables"]}


def _resolved(out: dict[str, Any]) -> set[str]:
    """Every table reference that names a table of this collection's schema."""
    names = {t["table_name"] for t in out["database_schema"]["tables"]}
    return {
        ref
        for q in out["queries"]["query_patterns"]
        for ref in q["tables_accessed"]
        if ref.rsplit(".", 1)[-1] in names
    }


def _without_metadata_name(raw: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(raw)
    raw["metadata"].pop("database_name")
    return raw


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", SAMPLES)
def test_a_matching_name_keys_tables_by_it(name: str) -> None:
    out = _collector(sample_collection(name), name, tell_parser=False)
    assert all(i.startswith(f"{name}.") for i in _ids(out))
    assert _resolved(out) and _resolved(out) <= _ids(out)


@pytest.mark.parametrize("name", SAMPLES)
def test_a_different_run_name_builds_the_same_collector_output(name: str) -> None:
    """The shared builder as every caller used it before #1572 (no name for the parser)."""
    matching = _collector(sample_collection(name), name, tell_parser=False)
    for tell_parser in (False, True):
        other = _collector(sample_collection(name), "shop", tell_parser=tell_parser)
        assert other["database_schema"] == matching["database_schema"]
        assert other["queries"] == matching["queries"]
        # One name for the ids and the source database (#835 reads the latter).
        assert other["metadata"]["source_database"] == matching["metadata"]["source_database"]
        assert other["metadata"]["source_database"]["database_name"] == name


@pytest.mark.parametrize("system_name", ["sys", "mysql"])
def test_a_database_named_like_a_system_schema_keeps_its_tables(system_name: str) -> None:
    """A database named ``sys`` (or ``mysql``) run under another name: its tables
    are application tables (#835), as when the run is named ``sys``."""
    from src.agents.referee.table_resolution import SourceScope, is_engine_system_object

    raw = copy.deepcopy(sample_collection("wordpress"))
    raw["metadata"]["database_name"] = system_name
    for run_name in (system_name, "shop"):
        out = _collector(raw, run_name)
        scope = SourceScope.from_collector(out)
        system = [i for i in _ids(out) if is_engine_system_object(i, scope)]
        assert _ids(out) and not system, run_name


def test_a_name_differing_only_in_case_keys_tables_by_the_collection() -> None:
    out = _collector(sample_collection("wordpress"), "WordPress")
    assert all(i.startswith("wordpress.") for i in _ids(out))
    assert _resolved(out) <= _ids(out)


def test_a_collection_with_no_database_name_is_keyed_by_the_run_name() -> None:
    raw = _without_metadata_name(sample_collection("wordpress"))
    out = _collector(raw, "shop")
    assert all(i.startswith("shop.") for i in _ids(out))
    assert _resolved(out) and _resolved(out) <= _ids(out)


def _attributed(raw: dict[str, Any], databases: list[str]) -> dict[str, Any]:
    """``raw`` with its statements attributed, in turn, to ``databases`` (as SQL
    Server's server-wide query statistics are, #382)."""
    raw = copy.deepcopy(raw)
    for i, row in enumerate(raw["queries"]):
        row["database_name"] = databases[i % len(databases)]
    return raw


def test_a_run_named_after_a_sibling_database_keeps_the_stated_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Server-wide statements (SQL Server) name a sibling database: the stated name
    wins, a warning is logged, and the result is the matching name's."""
    raw = _attributed(sample_collection("trailgear"), ["trailgear", "reporting"])
    matching = _collector(raw, "trailgear")
    with caplog.at_level("WARNING", logger="src.tools.database.offline_parser"):
        other = _collector(raw, "reporting")
    assert other["database_schema"] == matching["database_schema"]
    assert other["queries"] == matching["queries"]
    assert any("'reporting'" in r.getMessage() for r in caplog.records)


def test_a_name_the_statements_do_not_mention_keeps_the_collected_database() -> None:
    raw = _attributed(sample_collection("trailgear"), ["trailgear", "reporting"])
    matching = _collector(raw, "trailgear")
    other = _collector(raw, "shop")
    assert other["database_schema"] == matching["database_schema"]
    assert other["queries"] == matching["queries"]
    # The other database's statements are dropped either way.
    assert len(matching["queries"]["query_patterns"]) < len(raw["queries"])


def test_no_database_name_and_statements_of_several_databases_is_refused() -> None:
    from src.tools.database.offline_parser import DatabaseNameMismatchError

    raw = _attributed(_without_metadata_name(sample_collection("trailgear")), ["sales", "hr"])
    with pytest.raises(DatabaseNameMismatchError) as exc:
        parse_offline_collection(raw, database_name="shop")
    message = str(exc.value)
    assert "'hr'" in message and "'sales'" in message and "'shop'" in message


def test_no_database_name_and_a_run_named_after_one_of_its_databases() -> None:
    raw = _attributed(_without_metadata_name(sample_collection("trailgear")), ["sales", "hr"])
    out = _collector(raw, "sales")
    assert all(i.startswith("sales.") for i in _ids(out))
    assert _resolved(out) and _resolved(out) <= _ids(out)


def test_no_database_name_and_statements_of_one_database_are_keyed_by_it() -> None:
    raw = _attributed(_without_metadata_name(sample_collection("trailgear")), ["sales"])
    out = _collector(raw, "shop")
    assert all(i.startswith("sales.") for i in _ids(out))
    assert len(out["queries"]["query_patterns"]) == len(
        parse_offline_collection(raw, database_name="sales")["queries"]
    )
    assert _resolved(out) and _resolved(out) <= _ids(out)


def test_a_parse_saved_before_1572_keys_tables_by_its_stated_name() -> None:
    """The collector handler checkpoints the parse: one saved before the parser
    recorded ``collected_database_name`` keeps the handler's old rule (the
    collection's own name, else the run's)."""
    from src.agents.collector.offline_ingest import table_database_name

    parsed = parse_offline_collection(sample_collection("wordpress"), database_name="shop")
    assert table_database_name(parsed, "shop") == "wordpress"
    old = {k: v for k, v in parsed.items() if k != "collected_database_name"}
    assert table_database_name(old, "shop") == "wordpress"
    old["metadata"] = {k: v for k, v in old["metadata"].items() if k != "database_name"}
    assert table_database_name(old, "shop") == "shop"
    unnamed = parse_offline_collection(_without_metadata_name(sample_collection("wordpress")))
    assert unnamed["collected_database_name"] is None
    assert table_database_name(unnamed, "shop") == "shop"


class _Checkpoints:
    """A checkpoint store that runs every stage but the AWS metrics (no AWS)."""

    def load_or_run(self, name: str, fn: Any) -> Any:
        return {} if name == "aws_metrics" else fn()


@pytest.mark.parametrize(
    "stated",
    # The agent already keyed tables by a stated name before #1572.
    [pytest.param(True, marks=pytest.mark.passes_on_main), False],
)
def test_the_collector_agent_keys_tables_like_the_statements(stated: bool) -> None:
    """``_collect_offline`` (the deployed collector's offline mode): with or without a
    stated name, every statement's table references are the tables' own ids."""
    from src.agents.collector import mysql_collector
    from src.contracts.collector_input import CollectorInput

    inp = CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": "mysql",
            "cluster_endpoint": "offline",
            "port": 3306,
            "database_name": "shop",
            "mode": "offline",
            "offline_config": {"s3_bucket": "b", "s3_key": "k"},
        }
    )
    raw = sample_collection("wordpress")
    if not stated:
        raw = _without_metadata_name(raw)
    out = mysql_collector._collect_offline(inp, _Checkpoints(), raw=raw).model_dump(mode="json")
    prefix = "wordpress." if stated else "shop."
    assert all(i.startswith(prefix) for i in _ids(out))
    assert _resolved(out) and _resolved(out) <= _ids(out)
