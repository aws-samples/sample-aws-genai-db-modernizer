# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An RDS Data API client backed by a local test PostgreSQL (``tests/pg_server.py``), for
the routine proof's tests (#2337): nothing is sent to AWS.

It answers the calls :mod:`src.tools.routines.data_api_target` makes
(``execute_statement``, ``batch_execute_statement``, ``begin_transaction``,
``commit_transaction``, ``rollback_transaction``) the way the Data API does: each
transaction holds one connection, a statement outside one runs on a connection of its own,
a bound string is typed ``varchar``, an integer ``int8`` and a float ``float8``, and a SQL
error comes back as a ``DatabaseErrorException`` whose message ends ``; SQLState: <code>``.
The master secret signs in as the server's superuser; a secret made in ``secrets`` signs in
as the user it names (the server trusts its Unix socket, so the password is not checked).

It is not a full model of the test cluster: its master user is the server's superuser (on
Aurora it is a member of ``rds_superuser``, not a superuser), each statement outside a
transaction gets a fresh connection (the Data API reuses pooled sessions), and the local
server has no ``plpgsql_check``, so the strict check's extension and query run only against
the fake client (``fake_data_api``).
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

import psycopg2
import psycopg2.extras
from botocore.exceptions import ClientError

_MARK = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
_HINTS = {"DECIMAL": "numeric", "JSON": "json"}


def _error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ExecuteStatement")


class LocalDataApi:
    """See the module docstring. ``sent`` records ``(user, transaction, sql)`` per statement."""

    def __init__(self, server: Any, secrets: Any) -> None:
        self._server = server
        self._secrets = secrets
        self._open: dict[str, Any] = {}
        self._count = 0
        self.sent: list[tuple[str, str | None, str]] = []
        self._types: dict[int, str] = {}

    # ---- connections
    def _user(self, secret_arn: str) -> str:
        found = self._secrets.secrets.get(secret_arn)
        return str(json.loads(found["SecretString"])["username"]) if found else "postgres"

    def _connect(self, database: str | None, user: str) -> Any:
        conn = psycopg2.connect(host=str(self._server.workdir), dbname=database or "postgres",
                                user=user)  # fmt: skip
        psycopg2.extras.register_default_json(conn, loads=lambda raw: raw)
        psycopg2.extras.register_default_jsonb(conn, loads=lambda raw: raw)
        return conn

    def _type_name(self, oid: int) -> str:
        if not self._types:
            conn = self._connect("postgres", "postgres")
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT oid::int8, typname::text FROM pg_catalog.pg_type")
                    self._types = {int(o): str(n) for o, n in cur.fetchall()}
            finally:
                conn.close()
        return self._types.get(oid, "")

    # ---- the Data API's calls
    def begin_transaction(self, **kw: Any) -> dict[str, Any]:
        conn = self._connect(kw.get("database"), self._user(kw["secretArn"]))
        self._count += 1
        tx = f"local-tx-{self._count}"
        self._open[tx] = conn
        return {"transactionId": tx}

    def commit_transaction(self, **kw: Any) -> dict[str, Any]:
        conn = self._open.pop(kw["transactionId"], None)
        if conn is None:
            raise _error("NotFoundException")
        try:
            conn.commit()
        finally:
            conn.close()
        return {}

    def rollback_transaction(self, **kw: Any) -> dict[str, Any]:
        conn = self._open.pop(kw["transactionId"], None)
        if conn is None:
            raise _error("NotFoundException")
        try:
            conn.rollback()
        finally:
            conn.close()
        return {}

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        return self._run(kw, [kw.get("parameters") or []])[0]

    def batch_execute_statement(self, **kw: Any) -> dict[str, Any]:
        self._run(kw, kw.get("parameterSets") or [[]])
        return {"updateResults": [{}] * len(kw.get("parameterSets") or [])}

    def close_all(self) -> None:
        for conn in self._open.values():
            conn.close()
        self._open.clear()

    # ---- helpers
    def _run(self, kw: dict[str, Any], sets: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
        tx = kw.get("transactionId")
        user = self._user(kw["secretArn"])
        if tx is not None and tx not in self._open:
            raise _error("BadRequestException", "Transaction is not found")
        conn = self._open[tx] if tx is not None else self._connect(kw.get("database"), user)
        if tx is None:
            conn.autocommit = True
        self.sent.append((user, tx, kw["sql"]))
        out = []
        try:
            for params in sets:
                text, values = _typed(kw["sql"], params)
                with conn.cursor() as cur:
                    try:
                        cur.execute(text, values)
                    except psycopg2.Error as exc:
                        message = (exc.pgerror or str(exc)).strip()
                        raise _error("DatabaseErrorException",
                                     f"{message}; SQLState: {exc.pgcode}") from None  # fmt: skip
                    out.append(self._response(cur))
        finally:
            if tx is None:
                conn.close()
        return out

    def _response(self, cur: Any) -> dict[str, Any]:
        if cur.description is None:
            return {"numberOfRecordsUpdated": max(cur.rowcount, 0)}
        meta = [{"name": d.name, "label": d.name, "typeName": self._type_name(d.type_code)}
                for d in cur.description]  # fmt: skip
        return {"columnMetadata": meta, "records": [[_field(v) for v in r] for r in cur.fetchall()]}


def _typed(sql: str, params: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """``sql`` with each ``:name`` mark as a psycopg2 mark cast to the type the Data API
    gives that parameter, and the values."""
    by_name = {p["name"]: p for p in params}
    values: dict[str, Any] = {}

    def mark(match: re.Match[str]) -> str:
        p = by_name[match.group(1)]
        value = p["value"]
        if value.get("isNull"):
            return "NULL"
        values[p["name"]] = next(v for k, v in value.items() if k.endswith("Value"))
        cast = _HINTS.get(str(p.get("typeHint", "")), "")
        if not cast:
            cast = {"stringValue": "varchar", "longValue": "int8", "doubleValue": "float8",
                    "booleanValue": "bool", "blobValue": "bytea"}[next(iter(value))]  # fmt: skip
        return f"%({p['name']})s::{cast}"

    return _MARK.sub(mark, sql.replace("%", "%%")), values


def _field(value: Any) -> dict[str, Any]:
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (bytes, memoryview)):
        return {"blobValue": bytes(value)}
    if isinstance(value, Decimal):
        return {"stringValue": str(value)}
    if isinstance(value, (list, dict)):
        return {"stringValue": json.dumps(value)}
    return {"stringValue": str(value)}
