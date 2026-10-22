"""The one engine-scope setting, ``target_databases`` (#1328, decision 1 on #1181).

One validation for every entry point: catalog ids, compared lower-cased and trimmed,
each counted once; active engines only, with a legacy id and a planned engine each
refused with its own reason; an empty list refused; ``None`` (left out) is no scope.
"""

from __future__ import annotations

import pytest

from src.shared import catalog
from src.shared.target_databases import (
    TargetDatabasesError,
    engine_scope_display_order,
    engine_scope_statement,
    normalize_target_databases,
    parse_target_databases_option,
)

ACTIVE = sorted(catalog.engine_ids(status=catalog.ACTIVE))


def test_left_out_is_no_scope() -> None:
    assert normalize_target_databases(None) is None
    assert parse_target_databases_option(None) is None


def test_case_blanks_and_duplicates_are_normalized_and_sorted() -> None:
    got = normalize_target_databases(
        [" DynamoDB", "aurora_postgresql", "dynamodb", "AURORA_POSTGRESQL "]
    )
    assert got == ["aurora_postgresql", "dynamodb"]


def test_every_active_engine_is_accepted_and_nothing_else_is() -> None:
    assert normalize_target_databases(ACTIVE) == ACTIVE
    assert set(ACTIVE) == {
        "aurora_mysql",
        "aurora_postgresql",
        "documentdb",
        "dynamodb",
        "elasticache",
        "opensearch",
    }


@pytest.mark.parametrize("value", [[], [""], ["  ", ""]])
def test_an_empty_scope_is_refused(value: list[str]) -> None:
    with pytest.raises(TargetDatabasesError, match="is empty: name at least one engine"):
        normalize_target_databases(value)


def test_the_legacy_generic_aurora_id_names_its_successors() -> None:
    with pytest.raises(TargetDatabasesError) as err:
        normalize_target_databases(["dynamodb", "Aurora"])
    assert "'aurora' is legacy and not accepted; use aurora_mysql or aurora_postgresql" in str(
        err.value
    )


@pytest.mark.parametrize("engine", sorted(catalog.engine_ids(status="future")))
def test_a_planned_engine_is_refused_as_not_available_yet(engine: str) -> None:
    with pytest.raises(TargetDatabasesError, match=r"is not available yet \(a planned engine"):
        normalize_target_databases(["dynamodb", engine])


def test_an_unknown_id_is_refused_naming_the_accepted_ones() -> None:
    with pytest.raises(TargetDatabasesError) as err:
        normalize_target_databases(["dynamodb", "mysql", "redis"])
    message = str(err.value)
    assert "unknown engine id(s) mysql, redis" in message
    assert "choose from " + ", ".join(ACTIVE) in message


def test_a_string_or_a_non_string_entry_is_refused() -> None:
    with pytest.raises(TargetDatabasesError, match="must be a list"):
        normalize_target_databases("dynamodb")
    with pytest.raises(TargetDatabasesError, match="not an engine id"):
        normalize_target_databases(["dynamodb", 3])  # type: ignore[list-item]


def test_the_message_names_the_input_it_was_given_as() -> None:
    with pytest.raises(TargetDatabasesError, match="^--target-databases: unknown"):
        parse_target_databases_option("dynamodb,nope")


def test_the_cli_option_splits_on_commas() -> None:
    assert parse_target_databases_option("DynamoDB, aurora_postgresql,") == [
        "aurora_postgresql",
        "dynamodb",
    ]
    with pytest.raises(TargetDatabasesError, match="is empty"):
        parse_target_databases_option(" , ")


def test_a_refusal_is_a_value_error() -> None:
    # argparse's parser.error and pydantic's 422 both take a ValueError.
    assert issubclass(TargetDatabasesError, ValueError)


def test_the_statement_every_deliverable_states() -> None:
    order = engine_scope_display_order(["dynamodb", "aurora_postgresql", "dynamodb"])
    assert order == ["aurora_postgresql", "dynamodb"]
    assert (
        engine_scope_statement(order)
        == "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)"
    )
    # Ordered by display name, not by id: ElastiCache before OpenSearch, DocumentDB first.
    assert engine_scope_display_order(["opensearch", "elasticache", "documentdb"]) == [
        "documentdb",
        "elasticache",
        "opensearch",
    ]
