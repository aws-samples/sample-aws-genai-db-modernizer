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
    primary_key = []

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
            r"(?i)(?:CONSTRAINT\s+[`\"\[]?\w+[`\"\]]?\s+)?PRIMARY\s+KEY\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]+)\)",
            part,
        )
        if pk_match:
            primary_key = [_unquote(c) for c in pk_match.group(1).split(",")]
            indexes.append(
                {
                    "index_name": "PRIMARY",
                    "columns": primary_key,
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
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
        idx_match = re.match(
            r"(?i)(UNIQUE\s+)?(?:INDEX|KEY)\s+[`\"\[]?(\w+)[`\"\]]?\s*\(([^)]+)\)", part
        )
        if idx_match:
            idx_cols = [_unquote(c) for c in idx_match.group(3).split(",")]
            indexes.append(
                {
                    "index_name": idx_match.group(2),
                    "columns": idx_cols,
                    "is_unique": bool(idx_match.group(1)),
                    "is_primary": False,
                    "index_type": "btree",
                }
            )
            continue

        # CONSTRAINT name UNIQUE (cols) — T-SQL inline unique constraint
        unique_match = re.match(
            r"(?i)CONSTRAINT\s+[`\"\[]?(\w+)[`\"\]]?\s+UNIQUE\s*(?:CLUSTERED|NONCLUSTERED)?\s*\(([^)]+)\)",
            part,
        )
        if unique_match:
            uq_cols = [_unquote(c) for c in unique_match.group(2).split(",")]
            indexes.append(
                {
                    "index_name": unique_match.group(1),
                    "columns": uq_cols,
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
        "row_count": 0,
        "data_size_mb": 0,
        "index_size_mb": 0,
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
    upper_part = part.upper()
    auto_inc = "AUTO_INCREMENT" in upper_part or re.search(r"\bIDENTITY\b", upper_part) is not None
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
