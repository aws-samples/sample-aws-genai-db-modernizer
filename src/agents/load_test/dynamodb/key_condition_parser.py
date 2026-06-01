"""Deterministic parser for DynamoDB key_condition strings from schema design."""
import re
from dataclasses import dataclass


@dataclass
class ParsedKeyCondition:
    """Parsed result of a key_condition string.

    The parser extracts SK operator and literal only.
    PK attribute name/type always comes from TableDefinition.partition_key.
    """

    sk_operator: str | None  # "equals", "begins_with", "between", "greater", "less", None
    sk_literal: str | None  # Literal value/prefix from the condition string


def parse_key_condition(raw: str) -> ParsedKeyCondition | None:
    """Parse a key_condition string into structured data.

    Input formats (produced by schema design agent):
      - "PK=option_name" -> no SK condition
      - "PK=post_id AND SK=meta_key#meta_id" -> SK equals
      - "PK=post_id AND SK begins_with 'meta_key#'" -> SK begins_with
      - "PK=user_id AND SK between 'A' and 'Z'" -> SK between (uses first value)
      - "GSI1PK=author_id AND GSI1SK begins_with 'POST#'" -> GSI SK begins_with
      - "N/A" -> returns None (out of scope)

    Returns None for "N/A" patterns.
    """
    if raw.strip().upper() == "N/A":
        return None

    # Split on AND that separates key conditions (PK ... AND SK ...).
    # We only want the first AND — the "and" inside "between X and Y" must not be consumed.
    # Strategy: split on AND only when it appears between two identifier-like tokens
    # (e.g., "SK=..." or "GSI1SK begins_with ..."), which means it follows a word char
    # and precedes a word char that looks like a key name.
    parts = re.split(
        r"\s+AND\s+(?=\w+\s*(?:=|begins_with|between|>|<))", raw, flags=re.IGNORECASE, maxsplit=1
    )

    if len(parts) < 2:
        # PK-only condition
        return ParsedKeyCondition(sk_operator=None, sk_literal=None)

    # Second part is the SK condition
    sk_part = parts[1].strip()

    # Check for begins_with
    begins_match = re.search(r"begins_with\s+['\"]([^'\"]+)['\"]", sk_part, re.IGNORECASE)
    if begins_match:
        return ParsedKeyCondition(sk_operator="begins_with", sk_literal=begins_match.group(1))

    # Check for between
    between_match = re.search(
        r"between\s+['\"]([^'\"]+)['\"]\s+and\s+['\"]([^'\"]+)['\"]", sk_part, re.IGNORECASE
    )
    if between_match:
        return ParsedKeyCondition(sk_operator="between", sk_literal=between_match.group(1))

    # Check for equality (SK=value or GSI1SK=value)
    eq_match = re.search(r"(?:GSI\d+)?SK\s*=\s*(.+)$", sk_part, re.IGNORECASE)
    if eq_match:
        value = eq_match.group(1).strip().strip("'\"")
        return ParsedKeyCondition(sk_operator="equals", sk_literal=value)

    # Fallback: unknown SK format, treat as no SK condition
    return ParsedKeyCondition(sk_operator=None, sk_literal=None)
