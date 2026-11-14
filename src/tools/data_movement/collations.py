# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The character set and collation step 1 carries into Aurora MySQL (#2276).

The source's database collation goes into ``step-1.ddl.sql``'s ``CREATE DATABASE`` and
into every ``CREATE DATABASE`` the routine scripts write, through :func:`database_options`
alone, so the tables, the routines and their parameters land on the same collation.
Carrying it into one script only fails a call that compares a parameter with a column
(``ERROR 1267``, an illegal mix of collations; PR #2272, round 2). A table or column whose
collation differs from its database's (its table's) on the source keeps its own.

A collation name never goes from the collection into DDL: :func:`carry` takes it only when
it is in :func:`aurora_collations` for the target's line, and the options re-check it. A
collation that is not there (utf8mb3, a name that is not a collation of Aurora MySQL, one
newer than the line) is not carried, with a reason and what to do: the database then keeps
today's server defaults (:data:`SERVER_DEFAULTS`), a table its database's collation, a
column its table's. A collection that records no database collation carries nothing.
"""

from __future__ import annotations

import re

from src.contracts.transformation_contract import CharacterSet

#: What step-1.ddl.sql and the routine scripts say when they set no character set or
#: collation on a database (#2148).
SERVER_DEFAULTS = (
    "It sets no character set or collation: the cluster's server defaults apply. Check them "
    "against the source's before you run it."
)
#: The warning finding of a table or column whose collation step 1 does not carry.
NOT_CARRIED = "collation_not_carried"

_PLAIN = re.compile(r"^[A-Za-z0-9_]{1,64}$")

# The utf8mb4, latin1 and ascii collations of MySQL 8.0.23, the oldest Aurora MySQL
# version 3 release (information_schema.COLLATIONS); utf8mb3 is left out on purpose.
_UTF8MB4_80 = (
    "0900_ai_ci 0900_as_ci 0900_as_cs 0900_bin bin croatian_ci cs_0900_ai_ci cs_0900_as_cs "
    "czech_ci danish_ci da_0900_ai_ci da_0900_as_cs de_pb_0900_ai_ci de_pb_0900_as_cs "
    "eo_0900_ai_ci eo_0900_as_cs esperanto_ci estonian_ci es_0900_ai_ci es_0900_as_cs "
    "es_trad_0900_ai_ci es_trad_0900_as_cs et_0900_ai_ci et_0900_as_cs general_ci german2_ci "
    "hr_0900_ai_ci hr_0900_as_cs hungarian_ci hu_0900_ai_ci hu_0900_as_cs icelandic_ci "
    "is_0900_ai_ci is_0900_as_cs ja_0900_as_cs ja_0900_as_cs_ks latvian_ci la_0900_ai_ci "
    "la_0900_as_cs lithuanian_ci lt_0900_ai_ci lt_0900_as_cs lv_0900_ai_ci lv_0900_as_cs "
    "persian_ci pl_0900_ai_ci pl_0900_as_cs polish_ci romanian_ci roman_ci ro_0900_ai_ci "
    "ro_0900_as_cs ru_0900_ai_ci ru_0900_as_cs sinhala_ci sk_0900_ai_ci sk_0900_as_cs "
    "slovak_ci slovenian_ci sl_0900_ai_ci sl_0900_as_cs spanish2_ci spanish_ci sv_0900_ai_ci "
    "sv_0900_as_cs swedish_ci tr_0900_ai_ci tr_0900_as_cs turkish_ci unicode_520_ci "
    "unicode_ci vietnamese_ci vi_0900_ai_ci vi_0900_as_cs zh_0900_as_cs"
)
_LATIN1 = "bin danish_ci general_ci general_cs german1_ci german2_ci spanish_ci swedish_ci"
_ASCII = "bin general_ci"
# Added in MySQL 8.0.30: on every release of the newer lines, not on every version 3 one.
_UTF8MB4_8030 = (
    "bg_0900_ai_ci bg_0900_as_cs bs_0900_ai_ci bs_0900_as_cs gl_0900_ai_ci gl_0900_as_cs "
    "mn_cyrl_0900_ai_ci mn_cyrl_0900_as_cs nb_0900_ai_ci nb_0900_as_cs nn_0900_ai_ci "
    "nn_0900_as_cs sr_latn_0900_ai_ci sr_latn_0900_as_cs"
)


def _names(charset: str, suffixes: str) -> frozenset[str]:
    return frozenset(f"{charset}_{s}" for s in suffixes.split())


_AURORA_3 = _names("utf8mb4", _UTF8MB4_80) | _names("latin1", _LATIN1) | _names("ascii", _ASCII)
_NEWER = _AURORA_3 | _names("utf8mb4", _UTF8MB4_8030)


def _oldest() -> str:
    """The oldest Aurora MySQL line (``aurora_versions.toml``): version 3, MySQL 8.0."""
    from src.shared.catalog.gaps import aurora_version_lines

    return aurora_version_lines()["aurora_mysql"].lines[0]


def aurora_collations(line: str | None) -> frozenset[str]:
    """The collations step 1 carries into the Aurora MySQL ``line`` (the plan's
    ``target_engine_line``): those every release of it offers. None, or a line the catalog
    does not list, reads as the oldest."""
    from src.shared.catalog.gaps import aurora_version_lines

    lines = aurora_version_lines()["aurora_mysql"].lines
    return _NEWER if line in lines and line != lines[0] else _AURORA_3


def character_set_of(collation: str) -> str:
    """The character set a carried collation belongs to (its name's first part)."""
    return collation.split("_", 1)[0]


def _where(line: str | None) -> str:
    return f"Aurora MySQL release for MySQL {line or _oldest()}"


#: Where to choose another collation for each kind of object, by hand.
_EDIT = {
    "the database": "the CREATE DATABASE statements in step-1.ddl.sql, routines-deploy.sql and "
    "triggers-enable.sql together",
    "the table": "that table's CREATE TABLE in step-1.ddl.sql",
    "the column": "that column's definition in step-1.ddl.sql",
}


def closest(collation: str, line: str | None) -> str | None:
    """The collation Aurora MySQL offers that is closest to a utf8mb4 one it does not:
    MariaDB's ``_nopad`` variants without it, its ``uca1400`` (UCA 14.0) ones as the
    ``0900`` (UCA 9.0) ones; None when there is none."""
    name = collation.lower().replace("_nopad", "").replace("uca1400", "0900")
    return name if name != collation.lower() and name in aurora_collations(line) else None


def carry(
    collation: str | None, character_set: str | None, line: str | None, what: str, fallback: str
) -> tuple[CharacterSet | None, str | None]:
    """What step 1 does with a recorded collation: (the character set and collation to
    write, None) when ``line`` offers it, (None, why not and what to do) when it does not,
    (None, None) when nothing is recorded. ``what`` names the object ("the database",
    "the table", "the column") and ``fallback`` what it gets instead."""
    if not collation:
        return None, None
    them = "them" if what == "the database" else "it"
    edit = f"To use another collation, edit {_EDIT[what]} before you run {them}."
    instead = f"so {what} gets {fallback} instead"
    if not _PLAIN.fullmatch(collation):
        return (
            None,
            f"The collection records a collation name that is not a plain identifier for {what}, {instead}. {edit}",
        )
    lowered = collation.lower()
    if lowered.startswith(("utf8_", "utf8mb3_")) or (character_set or "").lower() in (
        "utf8",
        "utf8mb3",
    ):
        return None, (f"{what.capitalize()} is on {collation} (utf8mb3, MySQL's deprecated 3-byte utf8), "
                      f"which step 1 does not create new objects in, {instead}, which compares text "
                      f"differently from {collation}. {edit}")  # fmt: skip
    if lowered not in aurora_collations(line):
        if lowered in _NEWER:
            return None, (f"{what.capitalize()} is on {collation}, which is not on every "
                          f"{_where(line)}, {instead}. {edit}")  # fmt: skip
        if character_set_of(lowered) == "utf8mb4":
            near = closest(lowered, line)
            pick = (f"{near} is the closest one it offers" if near
                    else "choose a utf8mb4 collation it offers, such as utf8mb4_0900_ai_ci")  # fmt: skip
            return None, (f"Aurora MySQL offers no collation named {collation}; {pick}. Step 1 does "
                          f"not choose for you, {instead}. {edit}")  # fmt: skip
        return None, (f"{what.capitalize()} is on {collation}, which is not a utf8mb4, latin1 or ascii "
                      f"collation step 1 carries to Aurora MySQL, {instead}. {edit}")  # fmt: skip
    charset = character_set_of(lowered)
    if character_set and character_set.lower() != charset:
        recorded = character_set if _PLAIN.fullmatch(character_set) else "another character set"
        return None, (f"The collection records {what} as {recorded} with {collation}, "
                      f"which is a {charset} collation, {instead}. {edit}")  # fmt: skip
    return CharacterSet(character_set=charset, collation=lowered), None


def _checked(value: CharacterSet, line: str | None) -> CharacterSet:
    """A carried value as it may be written: one of :func:`aurora_collations` for the
    target's ``line``, never text from elsewhere (a contract edited by hand)."""
    if (
        value.collation not in aurora_collations(line)
        or character_set_of(value.collation) != value.character_set
    ):
        raise ValueError(
            f"{value.collation!r} is not a collation step 1 carries to the {_where(line)}"
        )
    return value


def database_options(value: CharacterSet | None, line: str | None = None) -> str:
    """What follows the name in a ``CREATE DATABASE`` (and an ``ALTER DATABASE``): empty
    with nothing carried. The one place a database's clause is written and checked."""
    if value is None:
        return ""
    v = _checked(value, line)
    return f" CHARACTER SET {v.character_set} COLLATE {v.collation}"


def table_options(value: CharacterSet | None, line: str | None = None) -> str:
    """What follows a ``CREATE TABLE``'s closing parenthesis: empty with nothing carried."""
    if value is None:
        return ""
    v = _checked(value, line)
    return f" DEFAULT CHARSET={v.character_set} COLLATE={v.collation}"


def column_options(value: CharacterSet | None, line: str | None = None) -> str:
    """What follows a column's type: empty with nothing carried."""
    if value is None:
        return ""
    v = _checked(value, line)
    return f" CHARACTER SET {v.character_set} COLLATE {v.collation}"


#: The Aurora MySQL column types that take a character set and collation.
TEXT_TYPES = frozenset(
    {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set"}
)


def is_text_type(aurora_type: str) -> bool:
    return re.split(r"[\s(]", aurora_type.strip(), maxsplit=1)[0].lower() in TEXT_TYPES


#: What step-1.ddl.sql and the routine scripts say of a database that already exists (review
#: of #2282): CREATE DATABASE IF NOT EXISTS leaves its collation as it is.
EXISTING = (
    "A database that already exists keeps its own collation: CREATE DATABASE IF NOT EXISTS "
    "does not change it. If it holds nothing else yet, run the ALTER DATABASE above its "
    "CREATE DATABASE first."
)


def database_sentence(
    value: CharacterSet | None, line: str | None = None, note: str | None = None
) -> str:
    """The one sentence step-1.ddl.sql and the routine scripts give of a database's
    character set and collation (and, when the source's is not carried, why)."""
    if value is None:
        return SERVER_DEFAULTS + (f" {note}" if note else "")
    options = database_options(value, line).strip()
    return (f"It creates the database with {options}, the source's, when it does not exist yet, so "
            "the tables, the routines and their parameters compare text as the source does; a table "
            f"or column that differs on the source keeps its own. {EXISTING}")  # fmt: skip


def alter_comment(
    quoted_name: str, value: CharacterSet | None, line: str | None = None
) -> list[str]:
    """The commented ``ALTER DATABASE`` for an existing database (:data:`EXISTING`), placed
    above its ``CREATE DATABASE``; none with nothing carried. ``quoted_name``: the name as
    the statement quotes it."""
    if value is None:
        return []
    return [f"-- ALTER DATABASE {quoted_name}{database_options(value, line)};"]
