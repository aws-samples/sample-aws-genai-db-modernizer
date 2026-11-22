# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An RDS Data API client backed by a local disposable MySQL engine, for the routine proof's
MySQL harness tests (#2560): nothing is sent to AWS.

It answers the calls :mod:`src.tools.routines.data_api_target` makes
(``execute_statement``, ``batch_execute_statement``, ``begin_transaction``,
``commit_transaction``, ``rollback_transaction``) the way the Data API does: each
transaction holds one connection, a statement outside one runs on a connection of its own,
and a SQL error comes back as a ``DatabaseErrorException`` whose message carries ``Error
code: <n>`` and ``SQLState: <code>``. The master secret signs in as the engine's ``root``;
a secret made in ``secrets`` signs in as the user and password it holds.

It is not a full model of the test cluster: its master user is ``root`` (on Aurora the
master user has fewer privileges), each statement outside a transaction gets a fresh
connection (the Data API reuses pooled sessions, so whether a session's ``TEMPORARY`` tables
or settings outlive a transaction there is not modelled), and a ``CALL`` answers with its
first result set only.
"""

from __future__ import annotations

import datetime
import json
import re
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

_MARK = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")
#: The connector's field type names as the Data API's ``typeName``.
_TYPE_NAMES = {
    "DECIMAL": "DECIMAL", "NEWDECIMAL": "DECIMAL", "TINY": "TINYINT", "SHORT": "SMALLINT",
    "LONG": "INT", "FLOAT": "FLOAT", "DOUBLE": "DOUBLE", "TIMESTAMP": "TIMESTAMP",
    "LONGLONG": "BIGINT", "INT24": "MEDIUMINT", "DATE": "DATE", "TIME": "TIME",
    "DATETIME": "DATETIME", "YEAR": "YEAR", "VARCHAR": "VARCHAR", "VAR_STRING": "VARCHAR",
    "STRING": "CHAR", "BIT": "BIT", "JSON": "JSON", "BLOB": "TEXT", "TINY_BLOB": "TEXT",
    "MEDIUM_BLOB": "TEXT", "LONG_BLOB": "TEXT",
}  # fmt: skip


def _error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "ExecuteStatement")


class LocalMySqlDataApi:
    """See the module docstring. ``sent`` records ``(user, transaction, sql)`` per statement."""

    def __init__(self, handle: Any, secrets: Any) -> None:
        self._server = handle._backend._server
        self._mc = handle._backend._mc
        self._secrets = secrets
        self._open: dict[str, Any] = {}
        self._count = 0
        self.sent: list[tuple[str, str | None, str]] = []

    # ---- connections
    def login(self, secret_arn: str) -> tuple[str, str]:
        found = self._secrets.secrets.get(secret_arn)
        if not found:
            return "root", self._server.password
        value = json.loads(found["SecretString"])
        return str(value["username"]), str(value["password"])

    def connect(self, secret_arn: str, database: str | None) -> Any:
        user, password = self.login(secret_arn)
        return self._mc.connect(**self._server.connect_args(user, password, database or None))

    # ---- the Data API's calls
    def begin_transaction(self, **kw: Any) -> dict[str, Any]:
        conn = self.connect(kw["secretArn"], kw.get("database"))
        conn.autocommit = False
        conn.start_transaction()
        self._count += 1
        tx = f"local-tx-{self._count}"
        self._open[tx] = conn
        return {"transactionId": tx}

    def _end(self, kw: dict[str, Any], commit: bool) -> dict[str, Any]:
        conn = self._open.pop(kw["transactionId"], None)
        if conn is None:
            raise _error("NotFoundException")
        try:
            conn.commit() if commit else conn.rollback()
        finally:
            conn.close()
        return {}

    def commit_transaction(self, **kw: Any) -> dict[str, Any]:
        return self._end(kw, True)

    def rollback_transaction(self, **kw: Any) -> dict[str, Any]:
        return self._end(kw, False)

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
        if tx is not None and tx not in self._open:
            raise _error("BadRequestException", "Transaction is not found")
        conn = (
            self._open[tx] if tx is not None else self.connect(kw["secretArn"], kw.get("database"))
        )
        self.sent.append((self.login(kw["secretArn"])[0], tx, kw["sql"]))
        out = []
        try:
            for params in sets:
                text, values = _typed(kw["sql"], params)
                cur = conn.cursor()
                try:
                    cur.execute(text, values or None)
                    out.append(self._response(cur))
                    while cur.nextset():
                        if cur.description:
                            cur.fetchall()
                except self._mc.Error as exc:
                    raise _error("DatabaseErrorException",
                                 f"Error code: {exc.errno}; SQLState: {exc.sqlstate}") from None  # fmt: skip
                finally:
                    cur.close()
        finally:
            if tx is None:
                conn.close()
        return out

    def _response(self, cur: Any) -> dict[str, Any]:
        from mysql.connector import FieldType

        if not cur.description:
            return {"numberOfRecordsUpdated": max(cur.rowcount, 0)}
        meta = [{"name": d[0], "label": d[0],
                 "typeName": _TYPE_NAMES.get(str(FieldType.get_info(d[1]) or ""), "VARCHAR")}
                for d in cur.description]  # fmt: skip
        return {"columnMetadata": meta, "records": [[_field(v) for v in r] for r in cur.fetchall()]}


def _typed(sql: str, params: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    """``sql`` with each ``:name`` mark as a connector mark, and the values in order."""
    by_name = {p["name"]: p for p in params}
    values: list[Any] = []

    def mark(match: re.Match[str]) -> str:
        value = by_name[match.group(1)]["value"]
        values.append(None if value.get("isNull") else
                      next(v for k, v in value.items() if k.endswith("Value")))  # fmt: skip
        return "%s"

    if not params:  # the connector reads ``%`` marks only when it is given values
        return sql, values
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
    if isinstance(value, (bytes, bytearray)):
        try:
            return {"stringValue": bytes(value).decode("utf-8")}
        except UnicodeDecodeError:
            return {"blobValue": bytes(value)}
    if isinstance(value, (Decimal, datetime.date, datetime.time, datetime.timedelta)):
        return {"stringValue": str(value)}
    return {"stringValue": str(value)}
