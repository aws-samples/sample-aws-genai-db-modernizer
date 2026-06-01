"""
DDL Parser Tool

Fetches DDL scripts from S3 and parses them into schema structures
matching the CollectorOutput contract.
"""

import logging
import re

logger = logging.getLogger(__name__)


def fetch_ddl_from_s3(bucket: str, key: str, region: str = "us-east-1") -> str:
    """Download DDL content from S3. Raises on failure (critical)."""
    import boto3

    s3 = boto3.client("s3", region_name=region)
    resp = s3.get_object(Bucket=bucket, Key=key)
    return str(resp["Body"].read().decode("utf-8"))  # type: ignore[no-any-return]


def parse_ddl(ddl_text: str, database_name: str = "unknown") -> list[dict]:
    """
    Parse CREATE TABLE statements from DDL text.
    Returns list of table dicts compatible with the schema builder.

    Handles:
    - CREATE TABLE with columns, types, constraints
    - PRIMARY KEY (inline and table-level)
    - FOREIGN KEY constraints
    - INDEX / UNIQUE INDEX
    - AUTO_INCREMENT
    """
    tables = []
    # Split on CREATE TABLE, case-insensitive
    create_stmts = re.split(r"(?i)(?=CREATE\s+TABLE)", ddl_text)

    for stmt in create_stmts:
        stmt = stmt.strip()
        if not stmt:
            continue

        table = _parse_create_table(stmt, database_name)
        if table:
            tables.append(table)

    return tables


def _parse_create_table(stmt: str, database_name: str) -> dict | None:
    """Parse a single CREATE TABLE statement."""
    # Extract table name
    m = re.match(
        r"(?i)CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?(?:\.`?(\w+)`?)?\s*\(",
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

        # PRIMARY KEY (table-level)
        pk_match = re.match(r"(?i)PRIMARY\s+KEY\s*\(([^)]+)\)", part)
        if pk_match:
            primary_key = [c.strip().strip("`") for c in pk_match.group(1).split(",")]
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

        # FOREIGN KEY
        fk_match = re.match(
            r"(?i)(?:CONSTRAINT\s+`?(\w+)`?\s+)?FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+`?(\w+)`?\s*\(([^)]+)\)"
            r"(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))?"
            r"(?:\s+ON\s+UPDATE\s+(CASCADE|SET\s+NULL|NO\s+ACTION|RESTRICT))?",
            part,
        )
        if fk_match:
            fk_cols = [c.strip().strip("`") for c in fk_match.group(2).split(",")]
            ref_cols = [c.strip().strip("`") for c in fk_match.group(4).split(",")]
            foreign_keys.append(
                {
                    "constraint_name": fk_match.group(1) or f"fk_{table_name}_{'_'.join(fk_cols)}",
                    "columns": fk_cols,
                    "referenced_table": fk_match.group(3),
                    "referenced_columns": ref_cols,
                    "on_delete": fk_match.group(5),
                    "on_update": fk_match.group(6),
                }
            )
            continue

        # INDEX / UNIQUE INDEX / KEY
        idx_match = re.match(r"(?i)(UNIQUE\s+)?(?:INDEX|KEY)\s+`?(\w+)`?\s*\(([^)]+)\)", part)
        if idx_match:
            idx_cols = [c.strip().strip("`") for c in idx_match.group(3).split(",")]
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
    m = re.match(
        r"(?i)`?(\w+)`?\s+(\w+(?:\([^)]*\))?(?:\s+unsigned)?)",
        part,
    )
    if not m:
        return None

    col_name = m.group(1)
    col_type_full = m.group(2).strip()

    # Skip if it looks like a constraint keyword
    if col_name.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "INDEX", "KEY", "CONSTRAINT", "CHECK"):
        return None

    # Extract base type
    base_type_match = re.match(r"(\w+)", col_type_full)
    base_type = base_type_match.group(1).lower() if base_type_match else col_type_full.lower()

    # Extract max_length
    len_match = re.search(r"\((\d+)", col_type_full)
    max_length = int(len_match.group(1)) if len_match else None

    nullable = "NOT NULL" not in part.upper()
    auto_inc = "AUTO_INCREMENT" in part.upper()
    is_primary = "PRIMARY KEY" in part.upper()

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
