# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


"""MySQL CREATE statements rebuilt from information_schema (#1652)."""

from __future__ import annotations

from src.agents.collector.mysql_routine_create import (
    definer_clause,
    event_create,
    event_schedule,
    quote_ident,
    routine_create,
    string_literal,
    trigger_create,
    trigger_follows,
)

PROCEDURE = {
    "routine_name": "place_order", "routine_type": "PROCEDURE", "schema_name": "shop",
    "specific_name": "place_order", "definer": "app@%", "security_type": "DEFINER",
    "is_deterministic": "NO", "sql_data_access": "MODIFIES SQL DATA", "routine_comment": "it's",
    "external_language": "SQL", "sql_mode": "STRICT_TRANS_TABLES",
    "definition": "BEGIN\n  INSERT INTO orders (id) VALUES (p_id);\nEND",
}  # fmt: skip
PARAMETERS = [
    {"ordinal": 2, "mode": "OUT", "parameter_name": "p_total", "dtd_identifier": "decimal(10,2)"},
    {"ordinal": 1, "mode": "IN", "parameter_name": "p_id", "dtd_identifier": "int"},
    {"ordinal": 3, "mode": "INOUT", "parameter_name": "p_note", "dtd_identifier": "varchar(40)",
     "character_set_name": "latin1", "collation_name": "latin1_swedish_ci"},
]  # fmt: skip
TRIGGERS = [
    {"trigger_name": "tg_b", "schema_name": "shop", "table_name": "orders", "timing": "BEFORE",
     "event_type": "INSERT", "action_order": 2, "definer": "app@%", "definition": "SET NEW.amount = 0"},
    {"trigger_name": "tg_a", "schema_name": "shop", "table_name": "orders", "timing": "BEFORE",
     "event_type": "INSERT", "action_order": 1, "definer": "app@%", "definition": "SET NEW.status = 'new'"},
    {"trigger_name": "tg_x", "schema_name": "shop", "table_name": "orders", "timing": "BEFORE",
     "event_type": "UPDATE", "action_order": 1, "definer": "app@%", "definition": "SET NEW.id = NEW.id"},
]  # fmt: skip
EVENT = {
    "event_name": "purge", "schema_name": "shop", "definer": "app@%", "time_zone": "SYSTEM",
    "event_type": "RECURRING", "execute_at": None, "interval_value": "1", "interval_field": "DAY",
    "starts": "2030-01-01 00:00:00", "ends": None, "status": "ENABLED",
    "on_completion": "NOT PRESERVE", "event_comment": "", "sql_mode": "",
    "definition": "DELETE FROM orders WHERE status = 'void'",
}  # fmt: skip


def test_identifiers_and_definers_are_quoted():
    assert quote_ident("odd`name") == "`odd``name`"
    assert definer_clause("app@%") == "DEFINER=`app`@`%` "
    assert definer_clause("we@b@10.0.0.1") == "DEFINER=`we@b`@`10.0.0.1` "
    assert definer_clause("") == ""


def test_strings_follow_the_sql_mode_they_are_read_under():
    assert string_literal("it's a \\ test", "STRICT_TRANS_TABLES") == "'it''s a \\\\ test'"
    assert string_literal("C:\\dir", "NO_BACKSLASH_ESCAPES,STRICT_TRANS_TABLES") == "'C:\\dir'"


def test_a_procedure_rebuilds_with_every_parameter_and_characteristic():
    assert routine_create(PROCEDURE, PARAMETERS) == (
        "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN `p_id` int, OUT `p_total` "
        "decimal(10,2), INOUT `p_note` varchar(40) CHARSET latin1 COLLATE latin1_swedish_ci)\n"
        "    MODIFIES SQL DATA\n"
        "    COMMENT 'it''s'\n"
        "BEGIN\n  INSERT INTO orders (id) VALUES (p_id);\nEND"
    )


def test_a_function_rebuilds_with_its_return_type_and_no_parameter_modes():
    function = {**PROCEDURE, "routine_name": "label", "routine_type": "FUNCTION",
                "security_type": "INVOKER", "is_deterministic": "YES",
                "sql_data_access": "NO SQL", "routine_comment": "", "definition": "RETURN 'x'"}  # fmt: skip
    params = [
        {"ordinal": 0, "mode": None, "parameter_name": None, "dtd_identifier": "varchar(20)",
         "character_set_name": "utf8mb4", "collation_name": "utf8mb4_0900_ai_ci"},
        {"ordinal": 1, "mode": None, "parameter_name": "p_code", "dtd_identifier": "int"},
    ]  # fmt: skip
    assert routine_create(function, params) == (
        "CREATE DEFINER=`app`@`%` FUNCTION `label`(`p_code` int) RETURNS varchar(20) "
        "CHARSET utf8mb4 COLLATE utf8mb4_0900_ai_ci\n"
        "    NO SQL\n    DETERMINISTIC\n    SQL SECURITY INVOKER\nRETURN 'x'"
    )


def test_a_type_that_names_its_charset_is_not_given_a_second():
    params = [{"ordinal": 1, "mode": "IN", "parameter_name": "p",
               "dtd_identifier": "varchar(5) CHARSET latin1", "character_set_name": "latin1"}]  # fmt: skip
    assert "varchar(5) CHARSET latin1)" in routine_create(PROCEDURE, params)


def test_no_create_without_a_body_a_return_type_or_sql():
    assert routine_create({**PROCEDURE, "definition": None}, PARAMETERS) is None
    assert routine_create({**PROCEDURE, "routine_type": "FUNCTION"}, PARAMETERS) is None
    assert routine_create({**PROCEDURE, "external_language": "JAVASCRIPT"}, PARAMETERS) is None


def test_a_comment_under_no_backslash_escapes_keeps_its_backslash():
    out = routine_create(
        {**PROCEDURE, "routine_comment": "C:\\dir", "sql_mode": "NO_BACKSLASH_ESCAPES"}, []
    )
    assert "    COMMENT 'C:\\dir'\n" in out


def test_triggers_follow_their_predecessor_in_action_order():
    follows = trigger_follows(TRIGGERS)
    assert follows == {("shop", "orders", "tg_b"): "tg_a"}
    assert trigger_create(TRIGGERS[0], follows[("shop", "orders", "tg_b")]) == (
        "CREATE DEFINER=`app`@`%` TRIGGER `tg_b` BEFORE INSERT ON `orders` FOR EACH ROW "
        "FOLLOWS `tg_a` SET NEW.amount = 0"
    )
    assert trigger_create(TRIGGERS[1]) == (
        "CREATE DEFINER=`app`@`%` TRIGGER `tg_a` BEFORE INSERT ON `orders` FOR EACH ROW "
        "SET NEW.status = 'new'"
    )


def test_no_trigger_create_without_a_body_or_a_dml_event():
    assert trigger_create({**TRIGGERS[1], "definition": None}) is None
    assert trigger_create({**TRIGGERS[1], "event_type": "TRUNCATE"}) is None


def test_a_recurring_event_rebuilds():
    assert event_create(EVENT) == (
        "CREATE DEFINER=`app`@`%` EVENT `purge` ON SCHEDULE EVERY 1 DAY "
        "STARTS '2030-01-01 00:00:00' ON COMPLETION NOT PRESERVE ENABLE "
        "DO DELETE FROM orders WHERE status = 'void'"
    )


def test_composite_intervals_one_time_events_and_states():
    assert event_schedule(
        {**EVENT, "interval_value": "1:30", "interval_field": "HOUR_MINUTE",
         "ends": "2031-01-01 00:00:00"}
    ) == "EVERY '1:30' HOUR_MINUTE STARTS '2030-01-01 00:00:00' ENDS '2031-01-01 00:00:00'"  # fmt: skip
    assert (
        event_schedule({**EVENT, "event_type": "ONE TIME", "execute_at": "2030-06-01 12:00:00"})
        == "AT '2030-06-01 12:00:00'"
    )
    disabled = event_create(
        {**EVENT, "status": "DISABLED", "on_completion": "PRESERVE", "event_comment": "nightly"}
    )
    assert " ON COMPLETION PRESERVE DISABLE COMMENT 'nightly' DO " in disabled
    assert " DISABLE ON REPLICA DO " in event_create({**EVENT, "status": "REPLICA_SIDE_DISABLED"})
    assert event_create({**EVENT, "event_type": "ONE TIME", "execute_at": None}) is None


def test_a_sql_mode_read_as_a_set_of_names_is_read_the_same():
    """A client that reads information_schema's SET column as a set (#1652)."""
    from src.agents.collector.mysql_routine_create import sql_mode_text

    modes = {"NO_BACKSLASH_ESCAPES", "STRICT_TRANS_TABLES"}
    assert string_literal("C:\\dir", modes) == "'C:\\dir'"
    assert sql_mode_text(modes) == "NO_BACKSLASH_ESCAPES,STRICT_TRANS_TABLES"
    assert sql_mode_text("") == "" and sql_mode_text(None) is None
