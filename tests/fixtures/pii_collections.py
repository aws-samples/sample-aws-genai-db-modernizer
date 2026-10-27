"""Raw collections seeded with known personal values in their literals (#1625).

SQL Server, Oracle and Db2 keep the text of a real execution, so an application that
sends literals leaves real values in it. Each collection here borrows the synthetic
SQL Server sample's schema (trailgear) and holds statements, a view, a procedure and a
trigger whose literals are an e-mail address, a name, a card-like number, a date and a
hex blob, in the dialect's own forms. :data:`PII_VALUES` are the values: none of them
may appear in anything the tool writes or returns.
"""

from __future__ import annotations

import copy
import json
import zipfile
from functools import cache
from pathlib import Path
from typing import Any

from src.tools.database.collection_text import extract_json_object

REPO = Path(__file__).resolve().parents[2]

EMAIL = "ottoline.brackenbury@example.com"
FIRST_NAME = "Ottoline"
LAST_NAME = "Brackenbury"
CARD = "4111111111111111"
BIRTH_DATE = "1984-03-07"
HEX_BLOB = "DEADBEEFCAFE"

#: Every seeded value, as it is looked for in an output (case-insensitively).
PII_VALUES = (EMAIL, FIRST_NAME, LAST_NAME, CARD, BIRTH_DATE, HEX_BLOB)

ENGINES = ("sqlserver", "oracle", "db2")

_METADATA = {
    "sqlserver": {"engine": "sqlserver"},
    "oracle": {"engine": "oracle", "version": "Oracle Database 19c Enterprise Edition"},
    "db2": {"engine": "db2", "version": "DB2 v11.5.8.0"},
}

# (digest, text) per engine. A digest of None is a row its collection script wrote
# with none (a Db2 collection has no digest at all): its query_id is the SHA-256 of
# the text as collected. Two rows sharing a digest are literal variants of one shape.
_STATEMENTS: dict[str, list[tuple[str | None, str]]] = {
    "sqlserver": [
        (
            "0x7A11000000000001",
            f"SELECT [CustomerID], [Email] FROM [Customer] WHERE [Email] = '{EMAIL}'",
        ),
        (
            "0x7A11000000000002",
            f"SELECT [CustomerID] FROM [Customer] WHERE [FirstName] = N'{FIRST_NAME}' "
            f"AND [LastName] = N'{LAST_NAME}'",
        ),
        (
            "0x7A11000000000002",
            "SELECT [CustomerID] FROM [Customer] WHERE [FirstName] = N'Anna' AND [LastName] = N'Lee'",
        ),
        ("0x7A11000000000003", f"SELECT [PaymentID] FROM [Payment] WHERE [CardNumber] = {CARD}"),
        (
            "0x7A11000000000004",
            f"SELECT [CustomerID] FROM [Customer] WHERE [CreatedDate] >= '{BIRTH_DATE}'",
        ),
        (
            "0x7A11000000000005",
            f"SELECT [SessionID] FROM [UserSession] WHERE [SessionToken] = 0x{HEX_BLOB}",
        ),
        (
            "0x7A11000000000006",
            f"/* user: {EMAIL} */ SELECT TOP (1) [CustomerID] FROM [Customer] "
            f"WHERE [Email] LIKE '%{EMAIL}%'",
        ),
        (
            "0x7A11000000000007",
            # One line, so the comment runs into the statement (the collection script
            # replaces newlines with spaces).
            f"-- customer lookup SELECT [CustomerID] FROM [Customer] "
            f"WHERE [LastName] = '{LAST_NAME}' AND [Phone] = '{CARD}'",
        ),
        (
            None,
            f"UPDATE [Customer] SET [LastName] = '{FIRST_NAME} {LAST_NAME}' "
            f"WHERE [Email] = '{EMAIL}'",
        ),
    ],
    "oracle": [
        ("fms_7100000000001", f"SELECT customer_id, email FROM customer WHERE email = '{EMAIL}'"),
        (
            "fms_7100000000002",
            f"SELECT customer_id FROM customer WHERE first_name = q'[{FIRST_NAME}]' "
            f"AND last_name = '{LAST_NAME}'",
        ),
        ("fms_7100000000003", f"SELECT payment_id FROM payment WHERE card_number = {CARD}"),
        (
            "fms_7100000000004",
            f"SELECT customer_id FROM customer WHERE created_date >= DATE '{BIRTH_DATE}' "
            f"AND modified_date < TO_DATE('{BIRTH_DATE}', 'YYYY-MM-DD')",
        ),
        (
            "fms_7100000000005",
            f"SELECT session_id FROM usersession WHERE session_token = HEXTORAW('{HEX_BLOB}')",
        ),
        (
            "fms_7100000000006",
            f"/* user: {EMAIL} */ SELECT customer_id FROM customer "
            f"WHERE email LIKE '%{EMAIL}%' FETCH FIRST 1 ROWS ONLY",
        ),
        (
            None,
            f"UPDATE customer SET last_name = '{FIRST_NAME} {LAST_NAME}' WHERE email = '{EMAIL}'",
        ),
    ],
    "db2": [
        (None, f"SELECT CUSTOMERID, EMAIL FROM CUSTOMER WHERE EMAIL = '{EMAIL}'"),
        (
            None,
            f"SELECT CUSTOMERID FROM CUSTOMER WHERE FIRSTNAME = '{FIRST_NAME}' "
            f"AND LASTNAME = G'{LAST_NAME}'",
        ),
        (None, f"SELECT PAYMENTID FROM PAYMENT WHERE CARDNUMBER = {CARD}"),
        (
            None,
            f"SELECT CUSTOMERID FROM CUSTOMER WHERE CREATEDDATE >= DATE('{BIRTH_DATE}') "
            "FETCH FIRST 10 ROWS ONLY",
        ),
        (None, f"SELECT SESSIONID FROM USERSESSION WHERE SESSIONTOKEN = X'{HEX_BLOB}'"),
        (
            None,
            f"/* user: {EMAIL} */ SELECT CUSTOMERID FROM CUSTOMER WHERE EMAIL LIKE '%{EMAIL}%'",
        ),
        (
            None,
            f"UPDATE CUSTOMER SET LASTNAME = '{FIRST_NAME} {LAST_NAME}' WHERE EMAIL = '{EMAIL}'",
        ),
    ],
}


def _definitions(engine: str) -> dict[str, list[dict[str, Any]]]:
    quote = "N'" if engine == "sqlserver" else "'"
    return {
        "views": [
            {
                "view_name": "vw_Vip",
                "schema_name": "dbo",
                "definition": (
                    f"CREATE VIEW vw_Vip AS SELECT CustomerID FROM Customer "
                    f"WHERE Email = '{EMAIL}' OR LastName = {quote}{LAST_NAME}'"
                ),
            }
        ],
        "procedures": [
            {
                "routine_name": "usp_Flag",
                "schema_name": "dbo",
                "routine_type": "PROCEDURE",
                "language": "SQL",
                "definition": (
                    f"CREATE PROCEDURE usp_Flag AS UPDATE Customer SET Phone = '{CARD}' "
                    f"WHERE FirstName = '{FIRST_NAME}' AND CreatedDate < '{BIRTH_DATE}'"
                ),
            }
        ],
        "triggers": [
            {
                "trigger_name": "trg_Audit",
                "table_name": "Customer",
                "schema_name": "dbo",
                "timing": "AFTER",
                "event_type": "UPDATE",
                "definition": (
                    f"CREATE TRIGGER trg_Audit ON Customer AFTER UPDATE AS "
                    f"INSERT INTO EventLog (Payload) VALUES ('{EMAIL} {HEX_BLOB}')"
                ),
            }
        ],
    }


@cache
def _trailgear() -> str:
    with zipfile.ZipFile(REPO / "docs" / "examples" / "trailgear" / "trailgear.zip") as z:
        return extract_json_object(z.read("trailgear-collection.json").decode("utf-8"))


def pii_collection(engine: str) -> dict[str, Any]:
    """A fresh raw collection for ``engine`` (one of :data:`ENGINES`) with the seeded
    values in its query texts and definitions. The SQL Server one keeps the sample's
    own statements too."""
    data: dict[str, Any] = json.loads(_trailgear())
    data["metadata"] = {**data["metadata"], **_METADATA[engine]}
    rows = []
    for n, (digest, text) in enumerate(_STATEMENTS[engine]):
        row: dict[str, Any] = {
            "query_text": text,
            "execution_count": 1000 + n,
            "total_time_ms": 50.0 + n,
            "avg_time_ms": 0.05,
        }
        if digest is not None:
            row["digest"] = digest
        rows.append(row)
    data["queries"] = (data["queries"] if engine == "sqlserver" else []) + rows
    data.update(_definitions(engine))
    return copy.deepcopy(data)


def pii_texts(engine: str) -> list[str]:
    """The seeded statements of ``engine``'s collection, as collected."""
    return [text for _digest, text in _STATEMENTS[engine]]


def keep_literals(monkeypatch: Any) -> None:
    """Turn the redaction off for one test (``monkeypatch``): every text is kept as
    collected, as the code did before #1625."""
    import src.tools.sql.redact as redact

    monkeypatch.setattr(redact, "_redacted", lambda text, engine, mode: text)


def leaked(text: str) -> list[str]:
    """The seeded values ``text`` holds (case-insensitively); empty when none."""
    lowered = text.lower()
    return [value for value in PII_VALUES if value.lower() in lowered]
