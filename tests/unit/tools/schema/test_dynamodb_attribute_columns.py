"""A DynamoDB design's source columns are checked against the collector (#1431)."""

from __future__ import annotations

from typing import Any

from src.tools.schema.dynamodb_attribute_columns import check_attribute_columns

COLLECTOR = {
    "database_schema": {
        "tables": [
            {
                "table_id": "shop.users",
                "table_name": "users",
                "columns": [{"column_name": "ID"}, {"column_name": "email"}],
            },
            {
                "table_id": "shop.orders",
                "table_name": "orders",
                "columns": [{"column_name": "order_id"}, {"column_name": "total"}],
            },
        ]
    }
}


def attr(name: str, table: str, column: Any, **extra: Any) -> dict:
    return {"name": name, "type": "S", "source_table": table, "source_column": column, **extra}


def design(attributes: list[dict], **table: Any) -> dict:
    return {
        "table_definitions": [
            {
                "table_name": "Users",
                "source_tables": ["shop.users"],
                "attributes": attributes,
                **table,
            }
        ]
    }


def test_correct_attributes_pass() -> None:
    d = design([attr("pk", "shop.users", "ID"), attr("email", "users", "EMAIL")])
    assert check_attribute_columns(d, COLLECTOR) == []


def test_missing_column_is_named() -> None:
    d = design([attr("user_id", "shop.users", "user_id")])
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "Users.user_id" in message
    assert "'user_id' is not a column of 'shop.users'" in message


def test_column_of_another_table_points_at_source_table() -> None:
    d = design([attr("total", "shop.users", "total")], source_tables=["shop.users", "shop.orders"])
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "is a column of 'shop.orders'" in message


def test_unknown_source_table_is_reported() -> None:
    d = design([attr("x", "shop.nothing", "ID")])
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "'shop.nothing' is not a table of the collector" in message


def test_derived_attributes_are_allowed() -> None:
    d = design(
        [
            attr("calc", "shop.users", "", calculation={"type": "aggregate"}),
            attr("joined", "shop.users", "no_such_fk", join={"type": "foreign_key"}),
        ]
    )
    assert check_attribute_columns(d, COLLECTOR) == []


def test_attribute_with_no_source_is_a_warning_not_a_failure() -> None:
    from src.tools.schema.dynamodb_attribute_columns import check_columns

    check = check_columns(design([attr("orphan", "shop.users", "")]), COLLECTOR)
    assert check.failures == []
    [warning] = check.warnings
    assert "names no source column" in warning and "constant" in warning


def test_multi_column_source_checks_each_column() -> None:
    d = design([attr("k", "shop.users", ["ID", "gone"])])
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "'gone'" in message


def test_entity_attributes_and_key_templates_are_checked() -> None:
    d = {
        "table_definitions": [
            {
                "table_name": "Shop",
                "source_tables": ["shop.users", "shop.orders"],
                "entities": [
                    {
                        "entity_type": "ORDER",
                        "source_table": "shop.orders",
                        "pk_template": "USER#{ID}",
                        "sk_template": "ORDER#{order_id}",
                        "attributes": [attr("t", "shop.orders", "total")],
                    }
                ],
            }
        ]
    }
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "Shop[ORDER].pk_template" in message and "'ID'" in message


def test_no_collector_tables_skips_the_check() -> None:
    d = design([attr("x", "shop.users", "nope")])
    assert check_attribute_columns(d, {}) == []


def test_long_names_are_truncated() -> None:
    d = design([attr("x", "shop.users", "c" * 500)])
    [message] = check_attribute_columns(d, COLLECTOR)
    assert len(message) < 400


def test_designer_is_asked_again_with_the_messages_and_failures_keep_them() -> None:
    from pydantic import BaseModel

    from src.tools.schema.base_schema_agent import enforce_design_numbers

    class Out(BaseModel):
        validation_passed: bool = True
        validation_failures: list[str] = []

    prompts: list[str] = []

    def invoke(prompt: str) -> Out:
        prompts.append(prompt)
        return Out()

    message = (
        "source columns: Users.user_id: source column 'user_id' is not a column of 'shop.users'"
    )
    result = enforce_design_numbers(invoke, Out(), lambda _out: [message], "[t]")
    assert len(prompts) == 2 and "'user_id' is not a column" in prompts[0]
    assert result.validation_passed is False
    assert result.validation_failures == [message]


def collection(entity_attrs: list[dict], pk: str, sk: str, source: str = "shop.users") -> dict:
    return {
        "table_definitions": [
            {
                "table_name": "Shop",
                "source_tables": ["shop.users", "shop.orders"],
                "entities": [
                    {
                        "entity_type": "E",
                        "source_table": source,
                        "pk_template": pk,
                        "sk_template": sk,
                        "attributes": entity_attrs,
                    }
                ],
            }
        ]
    }


def test_token_naming_a_renamed_attribute_passes() -> None:
    d = collection([attr("user_id", "shop.users", "ID")], "USER#{user_id}", "PROFILE")
    assert check_attribute_columns(d, COLLECTOR) == []


def test_tokens_naming_several_renamed_attributes_pass() -> None:
    attrs = [attr("user_id", "shop.users", "ID"), attr("order", "shop.users", "email")]
    d = collection(attrs, "USER#{user_id}", "ORDER#{order}")
    assert check_attribute_columns(d, COLLECTOR) == []


def test_token_naming_a_calculated_attribute_passes() -> None:
    attrs = [attr("bucket", "shop.users", "", calculation={"type": "case"})]
    d = collection(attrs, "B#{bucket}", "PROFILE")
    assert check_attribute_columns(d, COLLECTOR) == []


def test_token_matching_no_attribute_is_checked_as_a_column() -> None:
    d = collection([attr("x", "shop.users", "ID")], "USER#{ID}", "N#{nothing}")
    [message] = check_attribute_columns(d, COLLECTOR)
    assert "sk_template" in message and "'nothing'" in message


def test_bedrock_runner_checks_columns_on_the_first_design_only(tmp_path) -> None:
    from types import SimpleNamespace
    from unittest.mock import patch

    from src.tools.schema import dynamodb_schema_agent as agent

    bad = design([attr("user_id", "shop.users", "user_id")])
    final = SimpleNamespace(
        validation_passed=True,
        validation_failures=["source columns: stale"],
        model_dump=lambda mode="json": bad,
    )
    captured: dict = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, *_a, **_k):
            check = captured["output_check_fn"]
            first = check(SimpleNamespace(model_dump=lambda mode="json": bad))
            second = check(SimpleNamespace(model_dump=lambda mode="json": bad))
            captured["rounds"] = (first, second)
            return final, {}

    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    with (
        patch.object(agent, "_build_model", return_value=object()),
        patch.object(agent, "Agent", lambda **_: object()),
        patch.object(agent, "_project_input", lambda _p: {"design_metrics": None}),
        patch.object(agent, "_collector_for_columns", lambda _p: COLLECTOR),
        patch.object(agent, "SchemaDesignRunner", FakeRunner),
    ):
        output, _ = agent.run_dynamodb_schema_agent(
            collector_path=str(path), analysis_path=str(path)
        )
    first, second = captured["rounds"]
    assert any("'user_id' is not a column" in m for m in first)
    assert second == []  # PE revisions do not pay for more correction turns
    # The final design is still checked once, without a model call.
    assert output.validation_passed is False
    assert len(output.validation_failures) == 1 and "stale" not in output.validation_failures[0]


def test_both_checks_share_one_token_resolver() -> None:
    """The plan and the schema check read a key template's tokens through the same
    resolver (#1532): a token is a column only when no entity attribute carries its name."""
    from src.agents.schema_design.dynamodb_key_reconcile import unresolved_tokens

    entity = {"attributes": [{"name": "post_id", "source_column": "ID"}, {"name": "Author"}]}
    assert unresolved_tokens("POST#{post_id}#{author}#{state}", entity) == ["state"]
    assert unresolved_tokens("POST#{POST_ID}", entity) == []
    assert unresolved_tokens("PLAIN", entity) == []
    assert unresolved_tokens("{x}", {}) == ["x"]
