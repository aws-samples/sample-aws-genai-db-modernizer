"""Tests for DynamoDB key_condition string parser."""

from src.agents.load_test.dynamodb.key_condition_parser import parse_key_condition


class TestParseKeyCondition:
    def test_pk_only(self):
        result = parse_key_condition("PK=option_name")
        assert result.sk_operator is None
        assert result.sk_literal is None

    def test_pk_and_sk_equals(self):
        result = parse_key_condition("PK=post_id AND SK=meta_key#meta_id")
        assert result.sk_operator == "equals"
        assert result.sk_literal == "meta_key#meta_id"

    def test_pk_and_sk_begins_with(self):
        result = parse_key_condition("PK=post_id AND SK begins_with 'meta_key#'")
        assert result.sk_operator == "begins_with"
        assert result.sk_literal == "meta_key#"

    def test_pk_and_sk_begins_with_double_quotes(self):
        result = parse_key_condition('PK=user_id AND SK begins_with "ORDER#"')
        assert result.sk_operator == "begins_with"
        assert result.sk_literal == "ORDER#"

    def test_pk_literal_constant(self):
        result = parse_key_condition("PK=GLOBAL")
        assert result.sk_operator is None
        assert result.sk_literal is None

    def test_pk_and_sk_between(self):
        result = parse_key_condition("PK=user_id AND SK between 'ORDER#2020' and 'ORDER#2025'")
        assert result.sk_operator == "between"
        assert result.sk_literal == "ORDER#2020"

    def test_gsi_pk_and_sk_begins_with(self):
        result = parse_key_condition("GSI1PK=author_id AND GSI1SK begins_with 'POST#'")
        assert result.sk_operator == "begins_with"
        assert result.sk_literal == "POST#"

    def test_n_a_returns_none(self):
        result = parse_key_condition("N/A")
        assert result is None
