"""Conditional writes come from a structured field, not prose (issue #810).

The DynamoDB merge never replaces a key that a group's conditional write
depends on: the condition would then check another item, and a uniqueness
guarantee (``attribute_not_exists(email)``) would be silently gone. It used to
find those writes by matching words in the pattern's text, so a condition
described in prose ("rejected when the email is already taken") was missed.
An access pattern now states its condition in ``condition_expression``. A set
field is taken at its word; when it is null or missing (a draft written before
the field existed) the text is still matched, since that can only keep more
keys, never drop a guarantee. No model is called anywhere here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.agents.schema_design.dynamodb_key_reconcile import conditional_write
from src.agents.schema_design.group_merger import merge_failures, merge_group_drafts
from src.contracts.dynamodb_model_output import AccessPattern, DynamoDBModelOutputContract
from tests.unit.agents.schema_design.test_dynamodb_key_reconcile import (
    _ap,
    _attr,
    _draft,
    _key,
    _keys,
    _single,
)

USERS = "shop.users"
USER_UNIQUE = {"users": [frozenset({"user_id"}), frozenset({"email"})]}
PROSE = "PK=email; rejected when the email is already taken"


def _users_drafts(put: dict) -> list[dict]:
    """Group 0 keys users by user_id (2 patterns), group 1 by email (one PutItem)."""
    attrs = [_attr("user_id", USERS), _attr("email", USERS, type_="S")]
    g0 = _draft(
        [_single("users", USERS, "user_id", attrs=attrs)],
        [
            _ap("DDB-AP-1", "users", "GetItem", "PK=user_id", source=USERS),
            _ap("DDB-AP-2", "users", "UpdateItem", "PK=user_id", source=USERS),
        ],
    )
    email_table = _single("users", USERS, "email", attrs=attrs)
    email_table["partition_key"] = _key("email", "S")
    return [g0, _draft([email_table], [put])]


def _put(key_condition: str, **fields) -> dict:
    ap = _ap("DDB-AP-1", "users", "PutItem", key_condition, source=USERS)
    ap.update(fields)
    return ap


def _pattern(merged: dict, operation: str) -> dict:
    matches: list[dict] = [a for a in merged["access_patterns"] if a["operation"] == operation]
    [ap] = matches
    return ap


class TestTheStructuredField:
    def test_a_condition_described_in_prose_keeps_its_key(self):
        """The #810 repro: the field says what the prose only describes."""
        drafts = _users_drafts(_put(PROSE, condition_expression="attribute_not_exists(email)"))

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=USER_UNIQUE)

        [table] = merged["table_definitions"]
        assert _keys(table) == ("email", None)  # user_id would lose the uniqueness check
        assert merge_failures(merged) == []
        put = _pattern(merged, "PutItem")
        assert put["condition_expression"] == "attribute_not_exists(email)"
        assert put["gsi_name"] is None

    def test_conditional_writes_on_two_different_keys_stay_a_conflict(self):
        drafts = _users_drafts(_put(PROSE, condition_expression="attribute_not_exists(email)"))
        drafts[0]["access_patterns"][1].update(
            operation="PutItem",
            description="create the user; rejected when the id is taken",
            condition_expression="attribute_not_exists(user_id)",
        )

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=USER_UNIQUE)

        [failure] = merge_failures(merged)
        assert "table 'users'" in failure

    def test_a_null_field_still_reads_the_text(self):
        """Null is no proof of an unconditional write: the text can still keep the key."""
        drafts = _users_drafts(
            _put("PK=email, condition attribute_not_exists(email)", condition_expression=None)
        )

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=USER_UNIQUE)

        [table] = merged["table_definitions"]
        assert _keys(table) == ("email", None)  # user_id would lose the uniqueness check
        assert merge_failures(merged) == []

    def test_a_null_field_with_plain_text_is_unconditional(self):
        drafts = _users_drafts(_put("PK=email", condition_expression=None))

        merged = merge_group_drafts(drafts, "dynamodb", unique_keys=USER_UNIQUE)

        assert _keys(merged["table_definitions"][0]) == ("user_id", None)  # 2 patterns
        assert merge_failures(merged) == []

    def test_a_draft_without_the_field_falls_back_to_the_text(self):
        """Drafts written before the field existed are still read by their text."""
        put = _put("PK=email, condition attribute_not_exists(email)")
        assert "condition_expression" not in put

        merged = merge_group_drafts(_users_drafts(put), "dynamodb", unique_keys=USER_UNIQUE)

        assert _keys(merged["table_definitions"][0]) == ("email", None)

    def test_prose_alone_in_an_old_draft_is_still_missed(self):
        """The known limit of the fallback, pinned so a change to it is deliberate."""
        merged = merge_group_drafts(_users_drafts(_put(PROSE)), "dynamodb", unique_keys=USER_UNIQUE)

        assert _keys(merged["table_definitions"][0]) == ("user_id", None)


class TestConditionalWrite:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"condition_expression": "attribute_not_exists(email)"}, True),
            ({"condition_expression": "version = :expected"}, True),
            ({"condition_expression": None}, False),
            ({"condition_expression": "  "}, False),
            (
                {
                    "condition_expression": None,
                    "key_condition": "PK=email attribute_not_exists(email)",
                },
                True,
            ),
            ({"condition_expression": "attribute_not_exists(email)", "key_condition": PROSE}, True),
            ({"key_condition": "PK=email attribute_not_exists(email)"}, True),
            ({"key_condition": PROSE}, False),
        ],
    )
    def test_writes(self, fields, expected):
        fields = dict(fields)
        ap = _put(fields.pop("key_condition", "PK=email"), **fields)
        assert conditional_write(ap) is expected

    def test_a_read_is_never_a_conditional_write(self):
        ap = _ap("DDB-AP-1", "users", "GetItem", "PK=email", source=USERS)
        ap["condition_expression"] = "attribute_exists(email)"
        assert conditional_write(ap) is False


class TestContract:
    def test_the_field_is_optional_and_defaults_to_none(self):
        ap = AccessPattern.model_validate(_put("PK=email"))
        assert ap.condition_expression is None

    def test_a_conditional_write_validates(self):
        ap = AccessPattern.model_validate(
            _put(PROSE, condition_expression="attribute_not_exists(email)")
        )
        assert ap.condition_expression == "attribute_not_exists(email)"

    def test_the_field_must_be_text(self):
        with pytest.raises(ValidationError):
            AccessPattern.model_validate(_put("PK=email", condition_expression=["x"]))

    def test_the_contract_version_moved(self):
        version = DynamoDBModelOutputContract.model_fields["contract_version"].default
        assert tuple(map(int, version.split("."))) >= (1, 1)
