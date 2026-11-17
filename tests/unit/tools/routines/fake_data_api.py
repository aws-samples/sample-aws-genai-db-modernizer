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

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

#: The test cluster's master user in every fake answer.
MASTER = "proofadmin"


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
    #: Whether the call named a database at all (the runner's sign-in check names none).
    named_database: bool = True


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
                 drop_fails: tuple[str, ...] = (), role_reset_on: tuple[str, ...] = (),
                 session_reset_on: tuple[str, ...] = (),
                 secrets: FakeSecrets | None = None,
                 sign_in_fails: bool = False, reports_user: str | None = None,
                 reports_session: str | None = None,
                 list_ignores_prefix: bool = False) -> None:  # fmt: skip
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
        #: A statement holding one of these markers leaves the runner role (current_user
        #: goes back to the master user) or changes session_user, in its transaction.
        self.role_reset_on = role_reset_on
        self.session_reset_on = session_reset_on
        #: Each user's ``(plugin, authentication string)``, from its ``CREATE USER``; each
        #: login role's ``("scram-sha-256", verifier)``, from its ``CREATE ROLE ... PASSWORD``.
        self.auth: dict[str, tuple[str, str]] = {}
        #: The roles created with LOGIN, and the role each scratch database granted CONNECT.
        self.logins: set[str] = set()
        self.granted: dict[str, str] = {}
        #: Linked, a statement sent with one of these secrets signs in as the secret's user,
        #: as the real Data API does: the user (MySQL) or login role (PostgreSQL) must exist
        #: and the secret's password must match its authentication string or SCRAM verifier,
        #: or the statement is refused (1045; 28P01). ``sign_in_fails`` refuses every such
        #: sign-in (a server that cannot check the plugin, say).
        self.secrets_store = secrets
        self.sign_in_fails = sign_in_fails
        #: What ``SELECT CURRENT_USER()`` answers instead of the signed-in user, when set.
        self.reports_user = reports_user
        #: What ``session_user`` answers instead of the signed-in role, when set.
        self.reports_session = reports_session
        #: A runner listing that returns every user or role, whatever its prefix filter.
        self.list_ignores_prefix = list_ignores_prefix
        #: Each login role's VALID UNTIL, from its ``CREATE ROLE``.
        self.valid_until: dict[str, Any] = {}
        self._tx_role: dict[str, str] = {}
        self._tx_session: dict[str, str] = {}
        self._strict_tx: set[str] = set()
        self._tx = 0

    # ---- what the target calls
    def begin_transaction(self, **kw: Any) -> dict[str, Any]:
        self._sign_in(kw.get("secretArn", ""))
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
        self._sign_in(kw.get("secretArn", ""))
        self._maybe_fail(kw["sql"])
        return {"updateResults": [{}] * len(kw.get("parameterSets", []))}

    def execute_statement(self, **kw: Any) -> dict[str, Any]:
        sql, db, tx = kw["sql"], kw.get("database", ""), kw.get("transactionId")
        self.sent.append(
            Sent(
                "execute",
                sql,
                db,
                kw.get("parameters", []),
                tx,
                kw.get("secretArn", ""),
                "database" in kw,
            )
        )
        self._sign_in(kw.get("secretArn", ""))
        self._track_role(sql, tx)  # a failing statement can leave the role too
        self._maybe_fail(sql)
        if "plpgsql.extra_errors" in sql and tx:
            self._strict_tx.add(tx)
        for marker, state in self.extra_errors_fail.items():
            if tx in self._strict_tx and marker in sql:
                raise db_error(state)
        return self._answer(sql, db, tx, kw.get("parameters", []), kw.get("secretArn", ""))

    # ---- helpers
    def _signed_in_as(self, secret_arn: str) -> str | None:
        """The user a linked secret signs in as, or None for the master secret."""
        if self.secrets_store is None or secret_arn not in self.secrets_store.secrets:
            return None
        return str(json.loads(self.secrets_store.secrets[secret_arn]["SecretString"])["username"])

    def _sign_in(self, secret_arn: str) -> None:
        user = self._signed_in_as(secret_arn)
        if user is None:
            return
        stored = self.secrets_store.secrets[secret_arn]  # type: ignore[union-attr]
        password = str(json.loads(stored["SecretString"])["password"])
        plugin, auth = self.auth.get(user, ("", ""))
        if self.engine == "postgresql":
            from src.tools.routines.postgresql_auth import matches as scram_matches

            expired = user in self.valid_until and self.valid_until[user] < dt.datetime.now(dt.UTC)
            if (self.sign_in_fails or user not in self.roles or user not in self.logins or expired
                    or plugin != "scram-sha-256" or not scram_matches(password, auth)):  # fmt: skip
                raise db_error("28P01", f'FATAL: password authentication failed for user "{user}"')
            return
        from src.tools.routines.mysql_auth import matches

        if (self.sign_in_fails or user not in self.users or plugin != "caching_sha2_password"
                or not matches(password, auth)):  # fmt: skip
            raise db_error("28000", f"Database error code: 1045. Message: Access denied for "
                           f"user '{user}'@'%' (using password: YES)")  # fmt: skip

    def _track_role(self, sql: str, tx: str | None) -> None:
        if tx is None:
            return
        set_role = re.match(r'SET LOCAL ROLE "(\w+)"', sql)
        if set_role:
            self._tx_role[tx] = set_role.group(1)
        if any(m in sql for m in self.role_reset_on):
            self._tx_role[tx] = MASTER
        if any(m in sql for m in self.session_reset_on):
            self._tx_session[tx] = "someone_else"

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

    def _answer(self, sql: str, db: str, tx: str | None, params: list[Any],
                kw_secret: str = "") -> dict[str, Any]:  # fmt: skip

        def rows(*values: Any, types: tuple[str, ...] = ()) -> dict[str, Any]:
            records = [[self._field(v) for v in row] for row in values]
            width = len(values[0]) if values else len(types)
            meta = [{"name": f"c{i}", "label": f"c{i}", "typeName": (types[i] if i < len(types) else "text")}
                    for i in range(width)]  # fmt: skip
            return {"records": records, "columnMetadata": meta}

        if sql.startswith("SELECT current_user::text, session_user::text"):
            login = self.reports_user or self._signed_in_as(kw_secret) or MASTER
            current = self._tx_role.get(tx or "", login)
            session = self._tx_session.get(tx or "", self.reports_session or login)
            return rows([current, session], types=("text", "text"))
        if sql.startswith(("CREATE ROLE", "ALTER ROLE")) and re.search(r"(?<!:):[A-Za-z_]", sql):
            # Conservative: a ``:name`` in the text is taken as a parameter mark, as the Data
            # API takes one, so no test can pass on a verifier whose colons are left as they are.
            raise ClientError({"Error": {"Code": "BadRequestException",
                                         "Message": "missing parameter"}}, "ExecuteStatement")  # fmt: skip
        if sql.startswith("CREATE USER") and ":p" in sql:
            raise db_error("42000", "Database error code: 1064. Message: You have an error in "
                           "your SQL syntax; check the manual near '?' at line 1")  # fmt: skip
        if "FROM mysql.user WHERE" in sql:
            prefix = "" if self.list_ignores_prefix else params[-1]["value"]["stringValue"]
            return rows(*[[u] for u in sorted(self.users) if u.startswith(prefix)])
        if any(name in sql for name in self.drop_fails) and sql.startswith("DROP"):
            raise db_error("55006", "ERROR: database is being accessed by other users")
        if sql.startswith("SELECT CURRENT_USER()"):
            who = self.reports_user or f"{self._signed_in_as(kw_secret) or MASTER}@%"
            return rows([who], types=("VARCHAR",))
        user = re.match(r"(CREATE|DROP) USER (?:IF EXISTS )?'(\w+)'@'%'", sql)
        if user:
            (self.users.add if user.group(1) == "CREATE" else self.users.discard)(user.group(2))
            how = re.search(r"IDENTIFIED WITH (\w+) AS '([^']*)'$", sql)
            if user.group(1) == "CREATE" and how:
                self.auth[user.group(2)] = (how.group(1), how.group(2))
            elif user.group(1) == "DROP":
                self.auth.pop(user.group(2), None)
            return {}
        if "@@GLOBAL.event_scheduler" in sql:
            return rows([self.scheduler], types=("VARCHAR",))
        if "FROM mysql.user" in sql:
            return rows(*[[f"{u}@%"] for u in sorted(self.users | {MASTER})])
        if "FROM pg_roles ORDER BY" in sql:
            return rows(*[[r] for r in sorted(self.roles | {"proofadmin"})])
        if "FROM pg_database ORDER BY" in sql or "SCHEMATA ORDER BY" in sql:
            return rows(*[[d] for d in sorted(self.databases | {"routine_proof"})])
        if "information_schema.EVENTS ORDER BY" in sql:
            return rows()
        if "FROM pg_roles WHERE" in sql:
            prefix = "" if self.list_ignores_prefix else params[-1]["value"]["stringValue"]
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
            name = role.group(2)
            if role.group(1) == "DROP" and any(self.granted.get(d) == name for d in self.databases):
                raise db_error("2BP01", f'ERROR: role "{name}" cannot be dropped because some '
                               "objects depend on it")  # fmt: skip
            (self.roles.add if role.group(1) == "CREATE" else self.roles.discard)(name)
            password = re.search(r" PASSWORD E'([^']*)'$", sql)
            if role.group(1) == "CREATE":
                if re.search(r"\bLOGIN\b", sql) and "NOLOGIN" not in sql:
                    self.logins.add(name)
                if password:
                    self.auth[name] = ("scram-sha-256", password.group(1).replace("\\x3a", ":"))
                until = re.search(r" VALID UNTIL '([^']*)'", sql)
                if until:
                    self.valid_until[name] = dt.datetime.strptime(
                        until.group(1), "%Y-%m-%d %H:%M:%S+00").replace(tzinfo=dt.UTC)  # fmt: skip
            else:
                self.logins.discard(name)
                self.auth.pop(name, None)
            return {}
        granted = re.match(r'GRANT CONNECT, TEMPORARY ON DATABASE "(\w+)" TO "(\w+)"$', sql)
        if granted:
            self.granted[granted.group(1)] = granted.group(2)
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
        if "typname::text, pg_catalog.format_type" in sql:
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
    recovery window (``delete_fails``: every delete is refused). It has no call that reads
    a secret's value."""

    def __init__(self, *, delete_fails: bool = False) -> None:
        self.secrets: dict[str, dict[str, Any]] = {}
        self.deleted: list[tuple[str, bool]] = []
        self.delete_fails = delete_fails
        #: Secrets scheduled for deletion: ``list_secrets`` shows them only with
        #: ``IncludePlannedDeletion``, as Secrets Manager does.
        self.planned: set[str] = set()
        self.listed: list[dict[str, Any]] = []

    def list_secrets(self, **kw: Any) -> dict[str, Any]:
        """One page per call, filtered by name prefix as Secrets Manager's ``name`` filter is."""
        prefixes = [v for f in kw.get("Filters", []) if f["Key"] == "name" for v in f["Values"]]
        found = [{"ARN": arn, "Name": v["Name"], "Tags": list(v.get("Tags") or [])}
                 for arn, v in self.secrets.items()
                 if any(v["Name"].startswith(p) for p in prefixes)
                 and (kw.get("IncludePlannedDeletion") or arn not in self.planned)]  # fmt: skip
        self.listed.append(kw)
        return {"SecretList": found}

    def create_secret(self, **kw: Any) -> dict[str, Any]:
        arn = f"arn:aws:secretsmanager:us-east-1:111122223333:secret:{kw['Name']}"  # pragma: allowlist secret
        self.secrets[arn] = kw
        return {"ARN": arn, "Name": kw["Name"]}

    def delete_secret(self, **kw: Any) -> dict[str, Any]:
        if self.delete_fails:
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": ""}},
                              "DeleteSecret")  # fmt: skip
        self.deleted.append((kw["SecretId"], bool(kw.get("ForceDeleteWithoutRecovery"))))
        self.secrets.pop(kw["SecretId"], None)
        return {}


def runner_for(client: FakeDataApi, *, cluster_arn: str, secret_arn: str, job_id: str = "job1",
               admin_database: str | None = "routine_proof") -> Any:  # fmt: skip
    """A runner made the way the proof makes one (:func:`create_runner`), its secret linked
    to ``client``, so every statement sent with it signs in only when its password matches."""
    from src.tools.routines import data_api_target as dat

    if client.secrets_store is None:
        client.secrets_store = FakeSecrets()
    return dat.create_runner(client, client.secrets_store, cluster_arn=cluster_arn,
                             secret_arn=secret_arn, job_id=job_id, admin_database=admin_database,
                             tags=[], engine=client.engine)  # fmt: skip
