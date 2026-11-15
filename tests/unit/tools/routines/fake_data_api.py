# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A fake RDS Data API client for the routine proof's tests (#1945): nothing is sent.

It records every call (operation, SQL, database, parameters, transaction) and answers the
statements the Data API target sends with canned results: the scratch databases it was asked
to create (and drop), the tables a setup created, a sentinel row only the call's own
transaction sees, each routine call's configured result, and a ``DatabaseErrorException``
for any statement holding one of ``fail_on``'s markers (an ``InternalFailure``, which is no
SQL error, for one of ``internal_on``'s)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError


def db_error(sqlstate: str = "42601", message: str = "ERROR: syntax error") -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "DatabaseErrorException",
                "Message": f"{message}; SQLState: {sqlstate}",
            }
        },
        "ExecuteStatement",
    )


@dataclass
class Sent:
    op: str
    sql: str = ""
    database: str = ""
    parameters: list[Any] = field(default_factory=list)
    transaction: str | None = None
    secret: str = ""


class FakeDataApi:
    """See the module docstring. ``extra_errors_fail`` maps a marker to the SQLSTATE a
    statement holding it raises only in a transaction that set ``plpgsql.extra_errors``.
    ``calls`` maps a routine's quoted name, as a call names it
    (``"public"."f"``), to the records it returns (one integer column); ``fail_on`` maps a
    marker found in a statement to the SQLSTATE of the error it raises; ``strict`` is the
    plpgsql_check findings ``[(level, sqlstate)]`` per checked routine id."""

    def __init__(self, engine: str = "postgresql", *, calls: dict[str, list[int]] | None = None,
                 fail_on: dict[str, str] | None = None, plpgsql_check: bool = False,
                 strict: dict[str, list[tuple[str, str]]] | None = None,
                 extra_errors_fail: dict[str, str] | None = None,
                 internal_on: tuple[str, ...] = (), scheduler: str = "OFF",
                 drop_fails: tuple[str, ...] = ()) -> None:  # fmt: skip
        self.engine = engine
        self.sent: list[Sent] = []
        self.databases: set[str] = set()
        self.roles: set[str] = set()
        self.tables: dict[str, set[str]] = {}
        self.calls = calls or {}
        self.fail_on = fail_on or {}
        self.plpgsql_check = plpgsql_check
        self.strict = strict or {}
        self.extra_errors_fail = extra_errors_fail or {}
        self.internal_on = internal_on
        self.scheduler = scheduler
        self.drop_fails = drop_fails
        self.users: set[str] = set()
        self._strict_tx: set[str] = set()
        self._tx = 0

    # ---- what the target calls
    def begin_transaction(self, **kw: Any) -> dict[str, Any]:
        self._tx += 1
        self.sent.append(Sent("begin", database=kw.get("database", "")))
        return {"transactionId": f"tx{self._tx}"}

    def rollback_transaction(self, **kw: Any) -> dict[str, Any]:
        self.sent.append(Sent("rollback", transaction=kw.get("transactionId")))
        return {}

    def commit_transaction(self, **kw: Any) -> dict[str, Any]:
        self.sent.append(Sent("commit", transaction=kw.get("transactionId")))
        return {}

    def batch_execute_statement(self, **kw: Any) -> dict[str, Any]:
        self.sent.append(Sent("batch", kw["sql"], kw.get("database", ""),
                              kw.get("parameterSets", []), kw.get("transactionId"),
                              kw.get("secretArn", "")))  # fmt: skip
        self._maybe_fail(kw["sql"])
        return {"updateResults": [{}] * len(kw.get("parameterSets", []))}

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        sql, db, tx = kw["sql"], kw.get("database", ""), kw.get("transactionId")
        self.sent.append(
            Sent("execute", sql, db, kw.get("parameters", []), tx, kw.get("secretArn", ""))
        )
        self._maybe_fail(sql)
        if "plpgsql.extra_errors" in sql and tx:
            self._strict_tx.add(tx)
        for marker, state in self.extra_errors_fail.items():
            if tx in self._strict_tx and marker in sql:
                raise db_error(state)
        return self._answer(sql, db, tx, kw.get("parameters", []))

    # ---- helpers
    def _maybe_fail(self, sql: str) -> None:
        if any(marker in sql for marker in self.internal_on):
            raise ClientError(
                {"Error": {"Code": "InternalFailure", "Message": ""}}, "ExecuteStatement"
            )
        for marker, state in self.fail_on.items():
            if marker in sql:
                raise db_error(state)

    def statements(self, database: str | None = None) -> list[str]:
        return [s.sql for s in self.sent if s.op in ("execute", "batch")
                and (database is None or s.database == database)]  # fmt: skip

    def _answer(self, sql: str, db: str, tx: str | None, params: list[Any]) -> dict[str, Any]:
        def rows(*values: Any, types: tuple[str, ...] = ()) -> dict[str, Any]:
            records = [[self._field(v) for v in row] for row in values]
            width = len(values[0]) if values else len(types)
            meta = [{"name": f"c{i}", "label": f"c{i}", "typeName": (types[i] if i < len(types) else "text")}
                    for i in range(width)]  # fmt: skip
            return {"records": records, "columnMetadata": meta}

        if any(name in sql for name in self.drop_fails) and sql.startswith("DROP"):
            raise db_error("55006", "ERROR: database is being accessed by other users")
        user = re.match(r"(CREATE|DROP) USER (?:IF EXISTS )?'(\w+)'@'%'", sql)
        if user:
            (self.users.add if user.group(1) == "CREATE" else self.users.discard)(user.group(2))
            return {}
        if "@@GLOBAL.event_scheduler" in sql:
            return rows([self.scheduler], types=("VARCHAR",))
        if "FROM mysql.user" in sql:
            return rows(*[[f"{u}@%"] for u in sorted(self.users | {"proofadmin"})])
        if "FROM pg_roles ORDER BY" in sql:
            return rows(*[[r] for r in sorted(self.roles | {"proofadmin"})])
        if "FROM pg_database ORDER BY" in sql or "SCHEMATA ORDER BY" in sql:
            return rows(*[[d] for d in sorted(self.databases | {"routine_proof"})])
        if "information_schema.EVENTS ORDER BY" in sql:
            return rows()
        if "FROM pg_roles WHERE" in sql:
            prefix = params[-1]["value"]["stringValue"]
            return rows(*[[r] for r in sorted(self.roles) if r.startswith(prefix)])
        created = re.match(r'CREATE DATABASE [`"](\w+)[`"]', sql)
        if created:
            self.databases.add(created.group(1))
            return {}
        dropped = re.match(r'DROP DATABASE IF EXISTS [`"](\w+)[`"]', sql)
        if dropped:
            self.databases.discard(dropped.group(1))
            return {}
        role = re.match(r'(CREATE|DROP) ROLE (?:IF EXISTS )?"(\w+)"', sql)
        if role:
            (self.roles.add if role.group(1) == "CREATE" else self.roles.discard)(role.group(2))
            return {}
        table = re.match(r'CREATE TABLE (?:IF NOT EXISTS )?[`"]?(\w+)[`"]?', sql)
        if table:
            self.tables.setdefault(db, set()).add(table.group(1))
            return {}
        if "FROM pg_database" in sql or "information_schema.SCHEMATA" in sql:
            prefix = params[-1]["value"]["stringValue"]
            return rows(*[[d] for d in sorted(self.databases) if d.startswith(prefix)])
        if "pg_available_extensions" in sql:
            return rows([1 if self.plpgsql_check else 0], types=("int8",))
        if "FROM pg_proc p JOIN pg_namespace" in sql:
            return rows(*[[i + 1, rid, None] for i, rid in enumerate(self.strict)],
                        types=("int8", "text", "int8"))  # fmt: skip
        if "plpgsql_check_function_tb" in sql:
            oid = params[0]["value"]["longValue"]
            rid = list(self.strict)[oid - 1]
            return rows(*[[lvl, st] for lvl, st in self.strict[rid]], types=("text", "text"))
        if "typname::text, format_type" in sql:
            return rows(
                ["int4", "integer"], ["int8", "bigint"], ["text", "text"], types=("text", "text")
            )
        if "FROM pg_tables" in sql or "TABLE_TYPE = 'BASE TABLE'" in sql:
            return rows(*[[t] for t in sorted(self.tables.get(db, set()))])
        if "pg_sequences" in sql or "AUTO_INCREMENT FROM" in sql or "pg_get_serial_sequence" in sql:
            return rows(types=("text", "int8", "int8"))
        if "t.typcategory::text" in sql:
            return rows(["id", "int4", "N"], types=("text", "text", "text"))
        if "COLUMN_NAME, DATA_TYPE FROM" in sql:
            return rows(["id", "int"], types=("VARCHAR", "VARCHAR"))
        if sql.startswith(("SELECT q.", "SELECT to_jsonb(q.", "SELECT CAST(q.", "SELECT HEX(q.")):
            return rows(["1"], types=("text",))
        if "format_type(atttypid" in sql or "COLUMN_TYPE FROM" in sql:
            return rows(["id", "integer"], types=("text", "text"))
        if "count(*) FROM modernizer_sentinel" in sql:
            return rows([1 if tx else 0], types=("int8",))
        if "pg_catalog.now()" in sql or "NOW(6)" in sql:
            return rows(["2024-06-01 12:00:00.123456"], types=("timestamptz",))
        if "pg_settings" in sql or "performance_schema" in sql:
            return rows(types=("text", "text"))
        if sql.startswith("SELECT * FROM ") and "(" not in sql:
            return rows([1], types=("int4",))
        for name, values in self.calls.items():
            if name in sql:
                return rows(*[[v] for v in values], types=("int4",))
        return {}

    @staticmethod
    def _field(value: Any) -> dict[str, Any]:
        if value is None:
            return {"isNull": True}
        if isinstance(value, bool):
            return {"booleanValue": value}
        if isinstance(value, int):
            return {"longValue": value}
        return {"stringValue": str(value)}


class FakeSecrets:
    """A fake Secrets Manager client: the secrets created, and the ones deleted with no
    recovery window."""

    def __init__(self) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.deleted: list[tuple[str, bool]] = []

    def create_secret(self, **kw: Any) -> dict[str, Any]:
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{kw['Name']}"  # pragma: allowlist secret
        self.secrets[arn] = kw
        return {"ARN": arn, "Name": kw["Name"]}

    def delete_secret(self, **kw: Any) -> dict[str, Any]:
        self.deleted.append((kw["SecretId"], bool(kw.get("ForceDeleteWithoutRecovery"))))
        self.secrets.pop(kw["SecretId"], None)
        return {}
