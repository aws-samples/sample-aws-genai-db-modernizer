# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DDL Parser Tool

Fetches DDL scripts from S3 and parses them into schema structures
matching the CollectorOutput contract.

Dialect support is additive — the parser accepts both backtick-quoted
(MySQL/MariaDB) and bracket-quoted (T-SQL/SQL Server) identifiers, and
recognizes both ``AUTO_INCREMENT`` and ``IDENTITY(seed, increment)`` for
auto-increment columns. The ``dialect`` parameter signals intent at the
call site and allows future dialect-specific behavior; today it does not
gate any behavior — the parser is dialect-tolerant by default.
"""

import logging
import re

logger = logging.getLogger(__name__)


# Identifier quote characters across supported dialects.
# - Backtick: MySQL/MariaDB
# - Square bracket: SQL Server (T-SQL)
# - Double quote: PostgreSQL/Oracle (when QUOTED_IDENTIFIER is on)
_IDENTIFIER_QUOTES = '`[]"'


def _unquote(name: str) -> str:
    """Strip surrounding identifier quotes and whitespace."""
    return name.strip().strip(_IDENTIFIER_QUOTES).strip()


# A parenthesised key-part list that may hold one level of nested parentheses, the
# MySQL prefix length: ``(slug(10), body(50))``.
_KEY_LIST = r"\(((?:[^()]|\([^()]*\))+)\)"
_PREFIXED_PART = re.compile(r"^(.*?)\s*\(\s*(\d+)\s*\)\s*(?:ASC|DESC)?\s*$", re.IGNORECASE)


def _key_parts(text: str) -> tuple[list[str], list[int | None] | None]:
    """The columns of a key-part list and their MySQL prefix lengths (#1623).

    ``slug(10), body(50)`` is ``(["slug", "body"], [10, 50])``. The second value is None
    when no part has a prefix, so an index without any reads as before.
    """
    columns: list[str] = []
    prefixes: list[int | None] = []
    for part in _split_top_level(text):
        m = _PREFIXED_PART.match(part)
        if m and int(m.group(2)) > 0:
            columns.append(_unquote(m.group(1)))
            prefixes.append(int(m.group(2)))
        else:
            columns.append(_unquote(part))
            prefixes.append(None)
    return columns, (prefixes if any(n is not None for n in prefixes) else None)


def fetch_ddl_from_s3(bucket: str, key: str, region: str = "us-east-1") -> str:
    """Download DDL content from S3. Raises on failure (critical)."""
    import boto3

    s3 = boto3.client("s3", region_name=region)
    resp = s3.get_object(Bucket=bucket, Key=key)
    return str(resp["Body"].read().decode("utf-8"))  # type: ignore[no-any-return]


def parse_ddl(ddl_text: str, database_name: str = "unknown", dialect: str = "mysql") -> list[dict]:
    """
    Parse CREATE TABLE statements from DDL text.
    Returns list of table dicts compatible with the schema builder.

    Handles:
    - CREATE TABLE with columns, types, constraints
    - PRIMARY KEY (inline and table-level)
    - FOREIGN KEY constraints
    - INDEX / UNIQUE INDEX (MySQL inline syntax)
    - AUTO_INCREMENT (MySQL) and IDENTITY(seed, increment) (T-SQL)
    - varchar(N), nvarchar(MAX), varbinary(MAX) length variants
    - Backtick (MySQL) and bracket (T-SQL) quoted identifiers

    Args:
        ddl_text: Raw DDL text
        database_name: Database name for table_id formatting
        dialect: Source dialect — "mysql", "postgresql", "sqlserver", "oracle".
                 Currently informational; the parser is dialect-tolerant by
                 default. May be used for dialect-specific behavior in future.
    """
    tables = []
    # Split on CREATE TABLE, case-insensitive
    create_stmts = re.split(r"(?i)(?=CREATE\s+TABLE)", ddl_text)

    for stmt in create_stmts:
        stmt = stmt.strip()
        if not stmt:
            continue

        table = _parse_create_table(stmt, database_name, dialect)
        if table:
            tables.append(table)

    return tables


def _parse_create_table(stmt: str, database_name: str, dialect: str = "mysql") -> dict | None:
    """Parse a single CREATE TABLE statement. ``dialect`` is currently informational."""
    # Extract table name. Accept backtick, bracket, or double-quote quoting,
    # for either bare or schema-qualified names.
    m = re.match(
        r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"[`\"\[]?(\w+)[`\"\]]?(?:\.[`\"\[]?(\w+)[`\"\]]?)?\s*\(",
        stmt,
    )
    if not m:
        return None

    # Handle schema.table or just table
    if m.group(2):
        table_name = m.group(2)
    else:
        table_name = m.group(1)

    # Extract body between outer parens
    body = _extract_body(stmt)
    if not body:
        return None

    columns: list[dict] = []
    indexes = []
    foreign_keys: list[dict] = []
    primary_key: list[str] = []

    # Split body by top-level commas (not inside parens)
    parts = _split_top_level(body)

    ordinal = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue

        # PRIMARY KEY (table-level) — supports both
        # ``PRIMARY KEY (cols)`` and ``CONSTRAINT name PRIMARY KEY (cols)``.
        pk_match = re.match(
            r"(?i)(?:CONSTRAINT\s+[`\"\[]?\w+[`\"\]]?\s+)?PRIMARY\s+KEY\s*(?:CLUSTERED|NONCLUSTERED)?\s*"
            + _KEY_LIST,
            part,
        )
        if pk_match:
            primary_key, pk_prefixes = _key_parts(pk_match.group(1))
            indexes.append(
                {
                    "index_name": "PRIMARY",
                    "columns": primary_key,
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                    **({"prefix_lengths": pk_prefixes} if pk_prefixes else {}),
                }
            )
            continue

        # FOREIGN KEY — supports both backtick and bracket quoted identifiers
        fk_match = re.match(
            r"(?i)(?:CONSTRAINT\s+[`\"\[]?(\w+)[`\"\]]?\s+)?"
            r"FOREIGN\s+KEY\s*\(([^)]+)\)\s*"
            r"REFERENCES\s+[`\"\[]?(\w+)[`\"\]]?(?:\.[`\"\[]?(\w+)[`\"\]]?)?\s*\(([^)]+)\)"
            r"(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))?"
            r"(?:\s+ON\s+UPDATE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))?",
            part,
        )
        if fk_match:
            fk_cols = [_unquote(c) for c in fk_match.group(2).split(",")]
            ref_cols = [_unquote(c) for c in fk_match.group(5).split(",")]
            # When the REFERENCES clause is schema-qualified, group(4) holds the table
            # and group(3) holds the schema; otherwise group(3) is the table.
            ref_table = fk_match.group(4) or fk_match.group(3)
            foreign_keys.append(
                {
                    "constraint_name": fk_match.group(1) or f"fk_{table_name}_{'_'.join(fk_cols)}",
                    "columns": fk_cols,
                    "referenced_table": ref_table,
                    "referenced_columns": ref_cols,
                    "on_delete": fk_match.group(6),
                    "on_update": fk_match.group(7),
                }
            )
            continue

        # INDEX / UNIQUE INDEX / KEY (MySQL inline syntax — T-SQL uses
        # CREATE INDEX as separate statements outside CREATE TABLE).
        idx_match = re.match(r"(?i)(UNIQUE\s+)?(?:INDEX|KEY)\s+[`\"\[]?(\w+)[`\"\]]?\s*\(", part)
        if idx_match:
            indexes.append(
                {
                    "index_name": idx_match.group(2),
                    **_index_keys(part[idx_match.end() - 1 :]),
                    "is_unique": bool(idx_match.group(1)),
                    "is_primary": False,
                    "index_type": "btree",
                }
            )
            continue

        # CONSTRAINT name UNIQUE (cols) — T-SQL inline unique constraint
        unique_match = re.match(
            r"(?i)CONSTRAINT\s+[`\"\[]?(\w+)[`\"\]]?\s+UNIQUE\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(",
            part,
        )
        if unique_match:
            indexes.append(
                {
                    "index_name": unique_match.group(1),
                    **_index_keys(part[unique_match.end() - 1 :]),
                    "is_unique": True,
                    "is_primary": False,
                    "index_type": "btree",
                }
            )
            continue

        # Column definition
        col = _parse_column(part, ordinal + 1)
        if col:
            ordinal += 1
            if col.get("is_primary"):
                primary_key.append(col["column_name"])
            columns.append(col)

    if not columns:
        return None

    return {
        "table_name": table_name,
        "table_id": f"{database_name}.{table_name}",
        # #1636: DDL holds no row counts, so none is known (not an empty table).
        # #1636: DDL holds no row counts: unknown, not an empty table.
        "row_count": None,
        "data_size_mb": None,
        "index_size_mb": None,
        "columns": columns,
        "indexes": indexes,
        "foreign_keys": foreign_keys,
        "primary_key": primary_key,
    }


def _parse_column(part: str, ordinal: int) -> dict | None:
    """Parse a column definition line."""
    # Match identifier (any quote style) followed by type (with optional length).
    # Length token can be a digit run, a digit pair like "(p,s)", or the
    # T-SQL keyword MAX (varchar(MAX), varbinary(MAX), nvarchar(MAX)).
    m = re.match(
        r"(?i)[`\"\[]?(\w+)[`\"\]]?\s+(\w+(?:\([^)]*\))?(?:\s+unsigned)?)",
        part,
    )
    if not m:
        return None

    col_name = m.group(1)
    col_type_full = m.group(2).strip()

    # Skip if it looks like a constraint keyword
    if col_name.upper() in (
        "PRIMARY",
        "FOREIGN",
        "UNIQUE",
        "INDEX",
        "KEY",
        "CONSTRAINT",
        "CHECK",
    ):
        return None

    # Extract base type
    base_type_match = re.match(r"(\w+)", col_type_full)
    base_type = base_type_match.group(1).lower() if base_type_match else col_type_full.lower()

    # Extract max_length. Recognize digit values and the T-SQL MAX keyword
    # (varchar(MAX) → unbounded → None).
    len_match = re.search(r"\((\d+|MAX)", col_type_full, re.IGNORECASE)
    max_length: int | None = None
    if len_match:
        token = len_match.group(1).upper()
        if token != "MAX":  # nosec B105 — "MAX" is the T-SQL length keyword, not a password
            try:
                max_length = int(token)
            except ValueError:
                max_length = None

    nullable = "NOT NULL" not in part.upper()

    # Auto-increment detection across dialects:
    # - MySQL/MariaDB: AUTO_INCREMENT
    # - SQL Server (T-SQL): IDENTITY(seed, increment) or just IDENTITY
    # - Oracle 12c+: GENERATED [ALWAYS|BY DEFAULT [ON NULL]] AS IDENTITY
    upper_part = part.upper()
    auto_inc = (
        "AUTO_INCREMENT" in upper_part
        or re.search(r"\bIDENTITY\b", upper_part) is not None
        or re.search(r"\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+", upper_part) is not None
    )
    is_primary = "PRIMARY KEY" in upper_part

    # Default value
    default_val = None
    def_match = re.search(r"(?i)DEFAULT\s+('(?:[^']*)'|[^\s,]+)", part)
    if def_match:
        default_val = def_match.group(1).strip("'")

    return {
        "column_name": col_name,
        "ordinal_position": ordinal,
        "data_type": base_type,
        "column_type": col_type_full,
        "max_length": max_length,
        "is_nullable": "YES" if nullable else "NO",
        "column_default": default_val,
        "column_key": "PRI" if is_primary else "",
        "extra": "auto_increment" if auto_inc else "",
        "is_primary": is_primary,
    }


# A key part that names a column: the identifier, an optional MySQL prefix length
# and an optional sort order. Anything else (MySQL's ``(lower(name))``) is an expression.
_COLUMN_KEY = re.compile(
    r"[`\"\[]?(\w+)[`\"\]]?\s*(?:\(\s*(\d+)\s*\))?(?:\s+(?:ASC|DESC))?", re.IGNORECASE
)


def _index_keys(key_list: str) -> dict:
    """``columns`` and the #295 expression fields for an inline index's key list.

    ``key_list`` starts at the list's opening parenthesis. The list is split on its
    top-level commas, so a functional key part like ``(lower(name))`` is one key, and
    it never joins ``columns``: an index keyed on the rest would be narrower than the
    source's, and a narrower UNIQUE index rejects rows the source accepts.
    """
    columns: list[str] = []
    prefixes: list[int | None] = []
    expressions: list[str] = []
    for key in _split_top_level(_extract_body(key_list) or ""):
        key = key.strip()
        if not key:
            continue
        plain = _COLUMN_KEY.fullmatch(key)
        if plain:
            columns.append(plain.group(1))
            # A MySQL key prefix length (#1623), aligned with ``columns``.
            prefixes.append(int(plain.group(2)) if plain.group(2) and int(plain.group(2)) else None)
        else:
            expressions.append(key)
    return {
        "columns": columns,
        "has_expressions": bool(expressions),
        "index_expressions": ", ".join(expressions) or None,
        **({"prefix_lengths": prefixes} if any(n is not None for n in prefixes) else {}),
    }


def _extract_body(stmt: str) -> str | None:
    """Extract content between the outermost parentheses of CREATE TABLE."""
    depth = 0
    start = None
    for i, ch in enumerate(stmt):
        if ch == "(":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                return stmt[start:i]
    return None


def _split_top_level(body: str) -> list[str]:
    """Split by commas that are not inside parentheses."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


# ---------------------------------------------------------------------------
# Procedures, functions and triggers in a DDL upload (#1652): the zero-load path
# ---------------------------------------------------------------------------

_BATCH_SPLITTERS = {
    # T-SQL: GO on its own line ends a batch.
    "sqlserver": re.compile(r"^\s*GO\s*;?\s*$", re.I | re.M),
    # Oracle SQL*Plus: a lone slash ends a PL/SQL block.
    "oracle": re.compile(r"^\s*/\s*$", re.M),
}
_NAME = r"(?:`[^`]+`|\[[^\]]+\]|\"[^\"]+\"|[\w$#]+)"
_QUALIFIED = rf"(?:{_NAME}\s*\.\s*)?{_NAME}"
_ROUTINE_HEAD = re.compile(
    r"\bCREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(?:DEFINER\s*=\s*\S+\s+)?"
    r"(?:(?:NON)?EDITIONABLE\s+)?(?:CONSTRAINT\s+)?"
    rf"(PROCEDURE|PROC|FUNCTION|TRIGGER)\s+({_QUALIFIED})",
    re.I,
)
_EVENT_LIST = r"(?:INSERT|UPDATE|DELETE)(?:\s+OF\s+[\w\s,\"`]+?)?"
_TRIGGER_TAIL = re.compile(
    rf"(BEFORE|AFTER|INSTEAD\s+OF|FOR)\s+({_EVENT_LIST}(?:\s*(?:,|\bOR\b)\s*{_EVENT_LIST})*)"
    rf"\s+ON\s+({_QUALIFIED})",
    re.I,
)
# T-SQL: CREATE TRIGGER x ON t AFTER INSERT, UPDATE
_TRIGGER_ON_FIRST = re.compile(
    rf"\s+ON\s+({_QUALIFIED})\s+(?:WITH\s+[\w\s,]+?\s+)?(AFTER|INSTEAD\s+OF|FOR)\s+"
    r"((?:INSERT|UPDATE|DELETE)(?:\s*,\s*(?:INSERT|UPDATE|DELETE))*)",
    re.I,
)
_DDL_STATS_STATUS = {"status": "offline", "reason": "read from DDL: bodies, no stats"}


# #2071: PostgreSQL trigger enable modes in a DDL upload. pg_dump writes a trigger's
# CREATE TRIGGER, then ``ALTER TABLE <table> DISABLE | ENABLE [REPLICA | ALWAYS] TRIGGER
# <name>`` when its mode is not the default O. Names are compared as PostgreSQL folds them
# (unquoted lower-cased, quoted exact).
_PG_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_PG_QUALIFIED = rf"(?:{_PG_IDENT}\s*\.\s*)?{_PG_IDENT}"
_LEADING_COMMENTS = re.compile(r"\A(?:\s*--[^\n]*(?:\n|\Z))*\s*")
_PG_ALTER_TABLE = re.compile(
    rf"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?({_PG_QUALIFIED})\s*\*?\s+(.*)\Z",
    re.I | re.S,
)
_PG_ALTER_TRIGGER = re.compile(rf"ALTER\s+TRIGGER\s+({_PG_IDENT})\s+ON\s+({_PG_QUALIFIED})", re.I)
_PG_TRIGGER_ACTION = re.compile(
    rf"\s*(ENABLE(?:\s+(REPLICA|ALWAYS))?|DISABLE)\s+TRIGGER\s+({_PG_IDENT})\s*\Z", re.I
)
_PG_MODE_LETTER = {"": "O", "REPLICA": "R", "ALWAYS": "A"}
_PG_TRIGGER_WORD = re.compile(r"\bTRIGGER\b", re.I)
_OR_REPLACE = re.compile(r"CREATE\s+OR\s+REPLACE\b", re.I)


def _pg_ident(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1].replace('""', '"')
    return raw.lower()


def _pg_table_key(raw: str) -> tuple[str | None, str]:
    parts = [_pg_ident(part) for part in re.findall(_PG_IDENT, raw)]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (None, parts[-1])


class _PgTriggerModes:
    """Each PostgreSQL trigger's mode as the file's statements set it, in file order:
    O at its CREATE, then each ALTER on it. A mode the file does not settle is None: an
    ALTER it cannot read, a table named with and without its schema (the search path
    decides), a rename, or a CREATE OR REPLACE of a trigger it already holds."""

    def __init__(self) -> None:
        self.keys: list[tuple[tuple[str | None, str], str]] = []
        self.modes: list[str | None] = []
        self.unknown: set[int] = set()

    def create(self, table: str, name: str, replace: bool) -> None:
        key = (_pg_table_key(table), _pg_ident(re.findall(_PG_IDENT, name)[-1]))
        if replace and key in self.keys:  # OR REPLACE may keep the old mode
            self.unknown.update(i for i, k in enumerate(self.keys) if k == key)
            self.unknown.add(len(self.keys))
        self.keys.append(key)
        self.modes.append("O")

    def _on(self, table: tuple[str | None, str]) -> tuple[list[int], list[int]]:
        """The triggers on ``table``, and those that may be on it (same table name, one of
        the two names qualified and the other not)."""
        same, maybe = [], []
        for i, ((schema, name), _) in enumerate(self.keys):
            if name != table[1]:
                continue
            if schema == table[0]:
                same.append(i)
            elif schema is None or table[0] is None:
                maybe.append(i)
        return same, maybe

    def statement(self, block: str) -> bool:
        """Apply an ALTER TABLE / ALTER TRIGGER statement; False when ``block`` is not one
        that touches a trigger's mode or name."""
        text = _LEADING_COMMENTS.sub("", block)
        renamed = _PG_ALTER_TRIGGER.match(text)
        if renamed:
            same, maybe = self._on(_pg_table_key(renamed.group(2)))
            name = _pg_ident(renamed.group(1))
            self.unknown.update(i for i in same + maybe if self.keys[i][1] == name)
            return True
        alter = _PG_ALTER_TABLE.match(text)
        if not alter or not _PG_TRIGGER_WORD.search(alter.group(2)):
            return False
        same, maybe = self._on(_pg_table_key(alter.group(1)))
        self.unknown.update(maybe)
        for action in alter.group(2).split(","):
            if not _PG_TRIGGER_WORD.search(action):
                continue  # another sub-command (ADD COLUMN ...): no trigger's mode
            m = _PG_TRIGGER_ACTION.match(action)
            if not m:  # a trigger action it cannot read: every trigger on the table
                self.unknown.update(same)
                continue
            enable = m.group(1).upper() != "DISABLE"
            letter = _PG_MODE_LETTER[(m.group(2) or "").upper()] if enable else "D"
            target = m.group(3)
            wildcard = not target.startswith('"') and target.upper() in ("ALL", "USER")
            for i in same:
                if wildcard or self.keys[i][1] == _pg_ident(target):
                    self.modes[i] = letter
        return True

    def result(self) -> list[str | None]:
        return [None if i in self.unknown else m for i, m in enumerate(self.modes)]


def _routine_name(name: str) -> tuple[str | None, str]:
    parts = [_unquote(p) for p in re.split(r"\s*\.\s*", name.strip())]
    return (parts[-2], parts[-1]) if len(parts) >= 2 else (None, parts[0])


def _routine_blocks(ddl_text: str, dialect: str) -> list[str]:
    """Statements that may hold a routine: batch-split for T-SQL and Oracle, the
    DELIMITER regions for MySQL, and dollar-quote-aware ``;`` splitting otherwise."""
    if dialect in _BATCH_SPLITTERS:
        return [b for b in _BATCH_SPLITTERS[dialect].split(ddl_text) if b.strip()]
    if dialect in ("mysql", "mariadb"):
        blocks: list[str] = []
        delimiter = ";"
        buffer = ""
        for line in ddl_text.splitlines(keepends=True):
            m = re.match(r"\s*DELIMITER\s+(\S+)\s*$", line, re.I)
            if m:
                if buffer.strip():
                    blocks.append(buffer)
                buffer = ""
                delimiter = m.group(1)
                continue
            buffer += line
            if buffer.rstrip().endswith(delimiter):
                blocks.append(buffer.rstrip()[: -len(delimiter)])
                buffer = ""
        return [b for b in [*blocks, buffer] if b.strip()]
    # PostgreSQL: split on ';' outside $tag$ ... $tag$ bodies.
    out: list[str] = []
    parts: list[str] = []
    tag: str | None = None
    for token in re.split(r"(\$[A-Za-z_]\w*\$|\$\$|;)", ddl_text):
        if not token:
            continue
        if re.fullmatch(r"\$(?:[A-Za-z_]\w*)?\$", token):
            if tag is None:
                tag = token
            elif tag == token:
                tag = None
        if token == ";" and tag is None:  # nosec B105 -- a statement end, not a password
            out.append("".join(parts))
            parts = []
            continue
        parts.append(token)
    out.append("".join(parts))
    return [b for b in out if b.strip()]


def parse_ddl_routines(ddl_text: str, dialect: str) -> dict:
    """The procedures, functions and triggers in a DDL upload (#1652), as raw rows with
    the collection scripts' keys (``routine_name``, ``schema_name``, ``routine_type``,
    ``definition``; ``trigger_name``, ``table_name``, ``timing``, ``events``,
    ``for_each``, ``definition``). Bodies only: a DDL file holds no execution counts, so
    ``routine_stats_status`` is ``offline``. A block this cannot read is left out."""
    procedures: list[dict] = []
    triggers: list[dict] = []
    pg_modes = _PgTriggerModes() if dialect == "postgresql" else None
    for block in _routine_blocks(ddl_text or "", dialect):
        if pg_modes is not None and pg_modes.statement(block):
            continue
        head = _ROUTINE_HEAD.search(block)
        if not head:
            continue
        kind = head.group(1).upper()
        schema, name = _routine_name(head.group(2))
        definition = block[head.start() :].strip()
        if kind != "TRIGGER":
            row = {
                "routine_name": name,
                "routine_type": "FUNCTION" if kind == "FUNCTION" else "PROCEDURE",
                "definition": definition,
            }
            if schema:
                row["schema_name"] = schema
            procedures.append(row)
            continue
        rest = block[head.end() :]
        on_first = _TRIGGER_ON_FIRST.match(rest)
        tail = None if on_first else _TRIGGER_TAIL.search(rest)
        if on_first:
            table, timing, events = on_first.group(1), on_first.group(2), on_first.group(3)
        elif tail:
            timing, events, table = tail.group(1), tail.group(2), tail.group(3)
        else:
            continue
        timing = "AFTER" if timing.upper() == "FOR" else re.sub(r"\s+", " ", timing.upper())
        table_schema, table_name = _routine_name(table)
        schema = schema or table_schema  # a PostgreSQL trigger takes its table's schema
        event_names = re.findall(r"\b(INSERT|UPDATE|DELETE)\b", events, re.I)
        trigger: dict[str, str | None] = {
            "trigger_name": name,
            "table_name": table_name,
            "timing": timing,
            "events": " OR ".join(e.upper() for e in event_names),
            "event_type": event_names[0].upper() if event_names else "",
            "for_each": (
                "ROW"
                if re.search(r"FOR\s+EACH\s+ROW", rest, re.I)
                else (
                    "STATEMENT" if dialect == "sqlserver" or "STATEMENT" in rest.upper() else None
                )
            ),
            "definition": definition,
        }
        if schema:
            trigger["schema_name"] = schema
        if pg_modes is not None:
            pg_modes.create(table, head.group(2), replace=bool(_OR_REPLACE.match(definition)))
        triggers.append(trigger)
    if pg_modes is not None:  # #2071: the PostgreSQL trigger's mode, None when unsettled
        for trigger, mode in zip(triggers, pg_modes.result(), strict=True):
            trigger["enabled_mode"] = mode
    return {
        "procedures": procedures,
        "triggers": triggers,
        "routine_stats_status": dict(_DDL_STATS_STATUS),
    }
