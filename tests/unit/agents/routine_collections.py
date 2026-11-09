# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Raw PostgreSQL and MySQL collections with routines, for the routine migration tests
(#1652). Each holds SEED in a string literal of a body, so a test can tell where bodies
went."""

from __future__ import annotations

from typing import Any

SEED = "jane@example.com"

PG_FUNCTION_INT = (
    "CREATE OR REPLACE FUNCTION public.set_status(p_id integer)\n"
    " RETURNS void\n LANGUAGE plpgsql\n SECURITY DEFINER\n"
    "AS $function$\nBEGIN\n"
    f"  UPDATE orders SET status = 'shipped', contact = '{SEED}' WHERE id = p_id;\n"
    "END;\n$function$\n"
)
PG_FUNCTION_TEXT = PG_FUNCTION_INT.replace("p_id integer", "p_id text")
PG_AUDIT = (
    "CREATE OR REPLACE FUNCTION public.audit_orders()\n RETURNS trigger\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  RETURN NEW;\nEND;\n$function$\n"
)
PG_TRIGGER = (
    "CREATE TRIGGER tg_orders AFTER INSERT OR UPDATE ON public.orders "
    "FOR EACH ROW EXECUTE FUNCTION audit_orders()"
)
MYSQL_BODY = (
    f"BEGIN\n  UPDATE orders SET status = 'shipped', contact = '{SEED}' WHERE id = p_id;\nEND"
)


def _pg_routine(
    name: str, specific: str, identity: str, definition: str | None, status: str,
    *, security_definer: bool = False, depends_on: list[dict] | None = None,
) -> dict[str, Any]:  # fmt: skip
    return {
        "routine_name": name, "schema_name": "public", "specific_name": specific,
        "identity_arguments": identity, "routine_type": "FUNCTION", "return_type": "void",
        "language": "PLPGSQL", "owner": "app_owner", "security_definer": security_definer,
        "depends_on": depends_on or [], "body_chars": 60,
        "body_status": status, "definition": definition,
    }  # fmt: skip


def pg_collection(*, include_bodies: bool = True) -> dict[str, Any]:
    status = "collected" if include_bodies else "opted_out"

    def body(text: str) -> str | None:
        return text if include_bodies else None

    return {
        "metadata": {
            "database_name": "shop",
            "version": "PostgreSQL 16.2 on x86_64-pc-linux-gnu",
            "routine_capture": {"include_bodies": include_bodies, "privileges_not_seen": []},
        },
        "tables": [{"table_name": "orders", "schema_name": "public", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int4",
                     "is_nullable": "NO", "ordinal_position": 1}],  # fmt: skip
        "queries": [{"digest": "1", "query_text": "SELECT id FROM orders WHERE id = $1",
                     "execution_count": 20, "total_time_ms": 4}],  # fmt: skip
        "procedures": [
            _pg_routine("set_status", "set_status_16390", "p_id integer", body(PG_FUNCTION_INT),
                        status, security_definer=True,
                        depends_on=[{"kind": "table", "name": "public.orders"}]),
            _pg_routine("set_status", "set_status_16391", "p_id text", body(PG_FUNCTION_TEXT), status),
            _pg_routine("audit_orders", "audit_orders_16392", "", body(PG_AUDIT), status),
        ],  # fmt: skip
        "triggers": [{
            "trigger_name": "tg_orders", "schema_name": "public", "table_name": "orders",
            "timing": "AFTER", "event_type": "INSERT", "events": "INSERT OR UPDATE",
            "for_each": "ROW", "is_enabled": "O", "function_signature": "public.audit_orders()",
            "body_chars": len(PG_TRIGGER), "body_status": status, "definition": body(PG_TRIGGER),
        }],  # fmt: skip
    }


def _mysql_routine(
    name: str, kind: str, definition: str | None, status: str, **extra: Any
) -> dict[str, Any]:
    return {
        "routine_name": name, "schema_name": "shop", "specific_name": name, "routine_type": kind,
        "return_type": "int" if kind == "FUNCTION" else "", "definer": "app@%",
        "security_type": "DEFINER", "is_deterministic": "NO",
        "sql_data_access": "MODIFIES SQL DATA", "routine_comment": "", "external_language": "SQL",
        "sql_mode": "STRICT_TRANS_TABLES", "character_set_client": "utf8mb4",
        "collation_connection": "utf8mb4_0900_ai_ci", "database_collation": "utf8mb4_0900_ai_ci",
        "body_chars": len(definition) if definition else None, "body_status": status,
        "definition": definition, **extra,
    }  # fmt: skip


def _mysql_trigger(name: str, order: int, definition: str | None, status: str) -> dict[str, Any]:
    return {
        "trigger_name": name, "schema_name": "shop", "table_name": "orders", "timing": "BEFORE",
        "event_type": "INSERT", "for_each": "ROW", "definer": "app@%", "action_order": order,
        "sql_mode": "STRICT_TRANS_TABLES", "character_set_client": "utf8mb4",
        "collation_connection": "utf8mb4_0900_ai_ci", "database_collation": "utf8mb4_0900_ai_ci",
        "body_chars": 20, "body_status": status, "definition": definition,
    }  # fmt: skip


def mysql_collection(*, include_bodies: bool = True) -> dict[str, Any]:
    status = "collected" if include_bodies else "opted_out"

    def body(text: str) -> str | None:
        return text if include_bodies else None

    return {
        "metadata": {
            "database_name": "shop", "version": "8.0.36",
            "settings": {"sql_mode": "STRICT_TRANS_TABLES"},
            "routine_capture": {"include_bodies": include_bodies, "routines_counted": 7,
                                "parameters_counted": 3,
                                "privileges_not_seen": ["EVENT"]},
        },  # fmt: skip
        "tables": [{"table_name": "orders", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],  # fmt: skip
        "queries": [{"digest": "d1", "query_text": "SELECT id FROM orders WHERE id = ?",
                     "execution_count": 20, "total_time_ms": 4}],  # fmt: skip
        "procedures": [
            _mysql_routine("place_order", "PROCEDURE", body(MYSQL_BODY), status,
                           routine_comment=f"call {SEED}"),
            _mysql_routine("place_order", "FUNCTION", body("RETURN 1"), status,
                           is_deterministic="YES", sql_data_access="NO SQL"),
            _mysql_routine("secret_proc", "PROCEDURE", None,
                           "not_readable" if include_bodies else "opted_out"),
            _mysql_routine("js_label", "FUNCTION", body("return 'x';"), status,
                           external_language="JAVASCRIPT"),
        ],  # fmt: skip
        "routine_parameters": [
            {"routine_name": "place_order", "schema_name": "shop", "routine_type": "PROCEDURE",
             "ordinal": 1, "mode": "IN", "parameter_name": "p_id", "data_type": "int",
             "dtd_identifier": "int"},
            {"routine_name": "place_order", "schema_name": "shop", "routine_type": "FUNCTION",
             "ordinal": 0, "mode": None, "parameter_name": None, "data_type": "int",
             "dtd_identifier": "int"},
            {"routine_name": "js_label", "schema_name": "shop", "routine_type": "FUNCTION",
             "ordinal": 0, "mode": None, "parameter_name": None, "data_type": "varchar",
             "dtd_identifier": "varchar(10)", "character_set_name": "utf8mb4",
             "collation_name": "utf8mb4_0900_ai_ci"},
        ],  # fmt: skip
        "triggers": [
            _mysql_trigger("tg_a", 1, body("SET NEW.status = 'new'"), status),
            _mysql_trigger("tg_b", 2, body("SET NEW.id = NEW.id"), status),
        ],
        "events": [{
            "event_name": "purge", "schema_name": "shop", "definer": "app@%",
            "time_zone": "SYSTEM", "event_type": "RECURRING", "execute_at": None,
            "interval_value": "1", "interval_field": "DAY", "starts": "2030-01-01 00:00:00",
            "ends": None, "status": "ENABLED", "on_completion": "NOT PRESERVE",
            "event_comment": "", "sql_mode": "STRICT_TRANS_TABLES",
            "character_set_client": "utf8mb4", "collation_connection": "utf8mb4_0900_ai_ci",
            "database_collation": "utf8mb4_0900_ai_ci", "body_chars": 40,
            "body_status": status,
            "definition": body(f"DELETE FROM orders WHERE contact = '{SEED}'"),
        }],  # fmt: skip
    }


def old_mysql_collection() -> dict[str, Any]:
    """A collection written before #1652: no body status, capture, parameters or events."""
    raw = mysql_collection()
    raw["metadata"].pop("routine_capture")
    raw.pop("routine_parameters")
    raw.pop("events")
    raw["procedures"] = [{"routine_name": "place_order", "routine_type": "PROCEDURE",
                          "definition": MYSQL_BODY, "sql_mode": "STRICT_TRANS_TABLES"}]  # fmt: skip
    raw["triggers"] = []
    return raw


# #2023: a trigger that fires on TRUNCATE alone, and two event triggers.
PG_TRUNCATE_TRIGGER = (
    "CREATE TRIGGER tg_orders_truncate BEFORE TRUNCATE ON public.orders "
    f"FOR EACH STATEMENT EXECUTE FUNCTION audit_orders('{SEED}')"
)
PG_EVENT_TRIGGER = (
    'CREATE EVENT TRIGGER "Guard DDL" ON ddl_command_start '
    "WHEN TAG IN ('DROP TABLE', 'ALTER TABLE') EXECUTE FUNCTION public.guard_ddl()"
)
PG_EVENT_TRIGGER_NO_TAGS = (
    "CREATE EVENT TRIGGER log_ddl ON ddl_command_end EXECUTE FUNCTION public.guard_ddl()"
)


def pg_event_trigger_row(
    name: str, quoted: str, event: str, tags: list[str], status: str, enabled: str = "O"
) -> dict[str, Any]:
    """One row of the script's ``event_triggers`` section (and the live read's)."""
    return {
        "trigger_name": name, "quoted_name": quoted, "event": event, "tags": tags,
        "is_enabled": enabled, "owner": "postgres", "function_signature": "public.guard_ddl()",
        "quoted_function": "public.guard_ddl", "body_status": status,
    }  # fmt: skip


def pg_collection_with_non_dml_triggers(*, include_bodies: bool = True) -> dict[str, Any]:
    """:func:`pg_collection` plus a TRUNCATE-only trigger and two event triggers (#2023)."""
    raw = pg_collection(include_bodies=include_bodies)
    status = "collected" if include_bodies else "opted_out"
    raw["triggers"].append({
        "trigger_name": "tg_orders_truncate", "schema_name": "public", "table_name": "orders",
        "timing": "BEFORE", "event_type": "TRUNCATE", "for_each": "STATEMENT",
        "is_enabled": "O", "function_signature": "public.audit_orders()",
        "body_chars": len(PG_TRUNCATE_TRIGGER), "body_status": status,
        "definition": PG_TRUNCATE_TRIGGER if include_bodies else None,
    })  # fmt: skip
    raw["event_triggers"] = [
        pg_event_trigger_row("Guard DDL", '"Guard DDL"', "ddl_command_start",
                             ["DROP TABLE", "ALTER TABLE"], status),
        pg_event_trigger_row("log_ddl", "log_ddl", "ddl_command_end", [], status, enabled="D"),
    ]  # fmt: skip
    return raw
