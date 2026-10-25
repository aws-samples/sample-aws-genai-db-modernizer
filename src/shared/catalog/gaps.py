"""The relational-move gap rules (#471): what a source uses that the target Aurora lacks.

One ``src/catalog/gaps/<rule_id>.toml`` per rule says, in one place, which source engines
and target Aurora engines it applies to, what it looks for in the collection, how much it
matters for the relational move (``impact``) and why, citing the public AWS documentation
it rests on. ``src/catalog/aurora_versions.toml`` lists, per Aurora engine, the major
version lines a homogeneous source is compared against. This module loads both into
strict Pydantic models (an unknown, missing or mistyped field fails at load time); the
detector (:mod:`src.tools.analysis.relational_gaps`) only reads them, so no rule lives
in code.

Rule fields:

- ``id``: equals the file name.
- ``feature``: the short name of what the rule finds (one line).
- ``source_engines``: the collector source engines it applies to.
- ``targets``: the Aurora engines it applies to (``aurora_mysql``, ``aurora_postgresql``).
- ``impact``: one of :data:`GAP_IMPACTS`, most severe first.
- ``reason``: one plain sentence a customer can act on.
- ``docs``: the public AWS documentation the rule rests on, by name.
- ``[match]``: what the rule looks for; any one matcher that hits is a gap (see
  :class:`GapMatch`).

Impacts:

- ``blocks_move``: the target cannot run it; the move cannot finish until it is
  replaced outside the database or removed.
- ``needs_rewrite``: the schema, a query or a routine must change before cutover.
- ``behaves_differently``: it runs on the target, but results or behaviour can differ;
  test it.
- ``informational``: worth knowing for the move; no change is expected.
"""

from __future__ import annotations

import re
import tomllib
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.collector_output import IndexType
from src.shared.catalog.engines import CatalogError

__all__ = [
    "AURORA_TARGETS",
    "AURORA_VERSIONS_FILE",
    "GAPS_DIR",
    "GAP_IMPACTS",
    "GAP_IMPACT_LABELS",
    "SOURCE_ENGINES",
    "AuroraVersionLines",
    "GapImpact",
    "GapMatch",
    "GapRule",
    "INDEX_TYPES",
    "aurora_version_lines",
    "gap_rules",
    "load_aurora_versions",
    "load_gap_rules",
    "parse_version_line",
]

_CATALOG = Path(__file__).resolve().parents[2] / "catalog"
GAPS_DIR = _CATALOG / "gaps"
AURORA_VERSIONS_FILE = _CATALOG / "aurora_versions.toml"

GapImpact = Literal["blocks_move", "needs_rewrite", "behaves_differently", "informational"]
# Most severe first: the order gaps are listed in.
GAP_IMPACTS: tuple[GapImpact, ...] = (
    "blocks_move",
    "needs_rewrite",
    "behaves_differently",
    "informational",
)
# Each impact as a deliverable names it in a gap list, lower case ("TOP row limits
# (needs a rewrite)", #1120); the rule-catalog page capitalizes it. The results page's
# ``analysis-results.waves.gap-impact.*`` strings are the same words
# (tests/unit/shared/test_gap_catalog.py keeps them in step).
GAP_IMPACT_LABELS: dict[GapImpact, str] = {
    "blocks_move": "blocks the move",
    "needs_rewrite": "needs a rewrite",
    "behaves_differently": "behaves differently",
    "informational": "informational",
}
# The collector's source engines (src.contracts.collector_output.DatabaseEngine).
SOURCE_ENGINES = frozenset({"mysql", "mariadb", "postgresql", "sqlserver", "oracle", "db2"})
AURORA_TARGETS = frozenset({"aurora_mysql", "aurora_postgresql"})

_ONE_LINE = r"^[^\n]+$"
# The collector's index types (src.contracts.collector_output.IndexType).
INDEX_TYPES = frozenset(t.value for t in IndexType)

VersionCheck = Literal["older_than_target", "newer_than_target", "unknown"]


def parse_version_line(engine: str, version: str | None) -> tuple[int, ...] | None:
    """The major version line of ``version`` for ``engine``, or None when it has none.

    The line is what a major upgrade changes: ``(major, minor)`` for MySQL and MariaDB
    (``8.0.45`` is line 8.0), ``(major,)`` for PostgreSQL 10 and later (``16.10`` is
    line 16) and ``(major, minor)`` before 10 (``9.6.24`` is line 9.6). The first
    dotted number in the text counts, so ``PostgreSQL 16.10 on x86_64 ...`` and
    ``5.7.44-log`` both read.
    """
    match = re.search(r"(\d+)(?:\.(\d+))?", version or "")
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else None
    if engine == "postgresql" and major >= 10:
        return (major,)
    return (major, minor) if minor is not None else None


class GapMatch(BaseModel):
    """What a rule looks for. Text matchers read every collected query and every view,
    procedure and trigger definition, in the source engine's dialect, outside string
    literals, quoted identifiers and comments (:func:`src.tools.sql.facade.sql_tokens`);
    names compare case-insensitively."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Function names called (the name directly followed by "("), dotted or not.
    calls: tuple[str, ...] = ()
    # Single words anywhere in the text (a keyword, a hint, a pseudo-column).
    words: tuple[str, ...] = ()
    # A ``words`` hit does not count when the next token is one of these: T-SQL's
    # MERGE statement, not its MERGE JOIN / MERGE UNION hints.
    unless_followed_by: tuple[str, ...] = ()
    # Nor when the token before it is one of these: PostgreSQL's ~ between two
    # operands, not bitwise NOT after an operator or a keyword (SELECT ~ a).
    unless_preceded_by: tuple[str, ...] = ()
    # Token sequences, space-separated: "CONNECT BY", "( + )", ". NEXTVAL". The
    # token ``{value}`` stands for one value: a ``?`` placeholder, a number or an
    # @variable ("LIMIT {value} ," is MySQL's LIMIT offset, count).
    syntax: tuple[str, ...] = ()
    # Token sequences that count only where they end a statement (before ";" or the
    # end of the text): Db2's "WITH UR" isolation clause, not a CTE named ur.
    statement_endings: tuple[str, ...] = ()
    # Token sequences that count only where they begin a statement (at the start of
    # the text or after ";"): MySQL's SHOW statements, not a column named show.
    statement_starts: tuple[str, ...] = ()
    # Words that begin with one of these sigils and then a letter or "_": "@" is a
    # MySQL user variable (@total, not @@autocommit), "@@" a system variable.
    word_prefixes: tuple[str, ...] = ()
    # Quote characters: the text holds a quoted span (a string literal or a quoted
    # identifier, as the source dialect reads it) that opens with one of them. A
    # PostgreSQL "name" is an identifier; a MySQL "text" is a string.
    quoted_with: tuple[str, ...] = ()
    # An extension's own functions (``extension_calls``): a call counts when it is
    # unqualified (or quoted) or qualified with one of ``extension_schemas``, and the
    # collection defines no routine of that name in another schema (a user function).
    # The collection's own routines of that name in an extension schema (or with no
    # schema), and a ``CREATE EXTENSION <extension>`` statement, are evidence too.
    extension: str | None = None
    extension_calls: tuple[str, ...] = ()
    extension_schemas: tuple[str, ...] = ()
    # Column data types, without length or precision ("nvarchar", "long raw").
    data_types: tuple[str, ...] = ()
    # Column data types as shell-style globs ("* unsigned"; "_*" and "*[[]]" are
    # PostgreSQL array types, _int4 and integer[]).
    data_type_globs: tuple[str, ...] = ()
    # Column defaults as shell-style globs, compared as text ("0000-00-00*").
    default_values: tuple[str, ...] = ()
    # Columns the collector marks auto-incrementing (AUTO_INCREMENT, serial, identity).
    auto_increment: bool = False
    # Table and column names with an upper-case letter (``ID``, ``comment_post_ID``).
    uppercase_names: bool = False
    # Index types (Index.index_type: "gin", "gist", "fulltext", ...).
    index_types: tuple[str, ...] = ()
    # Indexes with a predicate (Index.predicate: a partial index).
    partial_indexes: bool = False
    # Tables, views, routines and triggers in a schema other than these (lower case).
    schemas_other_than: tuple[str, ...] = ()
    # MySQL-family table storage engines (Table.storage_engine).
    storage_engines: tuple[str, ...] = ()
    # Table collations, as shell-style globs ("utf8mb3_*").
    collations: tuple[str, ...] = ()
    # Procedure and function languages (Procedure.language).
    routine_languages: tuple[str, ...] = ()
    # Every procedure, function and trigger the source has.
    routines: bool = False
    # The source's major version line against the target's lines (homogeneous only).
    version: VersionCheck | None = None
    # The rule applies to every source of its engines (no evidence to find).
    always: bool = False

    @model_validator(mode="after")
    def _one_matcher_at_least(self) -> GapMatch:
        lists = (
            self.calls,
            self.words,
            self.syntax,
            self.statement_endings,
            self.statement_starts,
            self.word_prefixes,
            self.quoted_with,
            self.extension_calls,
            self.data_types,
            self.data_type_globs,
            self.default_values,
            self.index_types,
            self.schemas_other_than,
            self.storage_engines,
            self.collations,
            self.routine_languages,
        )
        flags = (self.routines, self.auto_increment, self.uppercase_names, self.partial_indexes)
        if not (any(lists) or any(flags) or self.version or self.always):
            raise ValueError("[match] needs at least one matcher")
        if (self.always or self.version) and (any(lists) or any(flags)):
            raise ValueError("an 'always' or 'version' rule takes no other matcher")
        if self.always and self.version:
            raise ValueError("a rule is 'always' or 'version', not both")
        if bool(self.extension) != bool(self.extension_calls):
            raise ValueError("extension and extension_calls go together")
        if self.extension_schemas and not self.extension:
            raise ValueError("extension_schemas needs an extension")
        if (self.unless_followed_by or self.unless_preceded_by) and not self.words:
            raise ValueError("unless_followed_by and unless_preceded_by qualify words")
        for name in self.syntax:
            if len(name.split()) < 2:
                raise ValueError(f"syntax {name!r} is one token: list it under words")
            if all(part in ("{value}", ",", "(", ")") for part in name.split()):
                raise ValueError(f"syntax {name!r} has no word to anchor it")
        for field in ("data_type_globs", "default_values", "schemas_other_than", "index_types"):
            if any(not entry.strip() or entry != entry.strip() for entry in getattr(self, field)):
                raise ValueError(f"{field} holds an empty or padded entry")
        if unknown := sorted(set(self.index_types) - INDEX_TYPES):
            raise ValueError(
                f"unknown index types {unknown} (Index.index_type: {sorted(INDEX_TYPES)})"
            )
        for name in (*self.syntax, *self.statement_starts):
            if any(("{" in part or "}" in part) and part != "{value}" for part in name.split()):
                raise ValueError(f"{name!r}: the only placeholder is {{value}}")
        for name in self.statement_starts:
            if "{value}" in name.split():
                raise ValueError(f"statement start {name!r} takes no placeholder")
        for prefix in self.word_prefixes:
            if not prefix or any(ch.isalnum() or ch == "_" or ch.isspace() for ch in prefix):
                raise ValueError(f"word prefix {prefix!r} is not a sigil")
        for quote in self.quoted_with:
            if len(quote) != 1 or quote.isalnum() or quote.isspace():
                raise ValueError(f"quoted_with {quote!r} is not one quote character")
        return self


class GapRule(BaseModel):
    """One gap rule (one ``src/catalog/gaps/<id>.toml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    feature: str = Field(min_length=1, pattern=_ONE_LINE)
    source_engines: tuple[str, ...] = Field(min_length=1)
    targets: tuple[str, ...] = Field(min_length=1)
    impact: GapImpact
    reason: str = Field(min_length=1, pattern=_ONE_LINE)
    docs: str = Field(min_length=1, pattern=_ONE_LINE)
    match: GapMatch

    @model_validator(mode="after")
    def _known_engines(self) -> GapRule:
        if unknown := sorted(set(self.source_engines) - SOURCE_ENGINES):
            raise ValueError(f"unknown source engines {unknown}")
        if unknown := sorted(set(self.targets) - AURORA_TARGETS):
            raise ValueError(f"unknown targets {unknown}")
        if len(set(self.source_engines)) != len(self.source_engines):
            raise ValueError("a source engine is listed twice")
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("a target is listed twice")
        return self


class AuroraVersionLines(BaseModel):
    """One Aurora engine's major version lines (``[engines.<id>]``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The source engines that move to this Aurora engine without a dialect change.
    source_engines: tuple[str, ...] = Field(min_length=1)
    # The major lines with standard support, as "8.0" or "16".
    lines: tuple[str, ...] = Field(min_length=1)
    docs: str = Field(min_length=1, pattern=_ONE_LINE)


class AuroraVersions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engines: dict[str, AuroraVersionLines]

    @model_validator(mode="after")
    def _valid(self) -> AuroraVersions:
        if unknown := sorted(set(self.engines) - AURORA_TARGETS):
            raise ValueError(f"unknown Aurora engines {unknown}")
        for engine, entry in self.engines.items():
            if unknown := sorted(set(entry.source_engines) - SOURCE_ENGINES):
                raise ValueError(f"{engine}: unknown source engines {unknown}")
            for line in entry.lines:
                for source in entry.source_engines:
                    if parse_version_line(source, line) is None:
                        raise ValueError(f"{engine}: {line!r} is not a {source} version line")
        return self


def load_aurora_versions(path: Path = AURORA_VERSIONS_FILE) -> dict[str, AuroraVersionLines]:
    """Aurora engine -> its version lines, from ``path``."""
    try:
        with path.open("rb") as fh:
            return AuroraVersions.model_validate(tomllib.load(fh)).engines
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc


def load_gap_rules(
    directory: Path = GAPS_DIR, versions: dict[str, AuroraVersionLines] | None = None
) -> tuple[GapRule, ...]:
    """Every gap rule file in ``directory``, validated, sorted by id.

    A ``version`` rule must pair each of its source engines with each of its targets
    homogeneously (``versions``: the source engines each target lists).
    """
    versions = load_aurora_versions() if versions is None else versions
    rules: list[GapRule] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                rule = GapRule.model_validate(tomllib.load(fh))
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if rule.id != path.stem:
            raise CatalogError(f"{path.name}: id {rule.id!r} does not match the file name")
        if rule.match.version in ("older_than_target", "newer_than_target"):
            for target in rule.targets:
                entry = versions.get(target)
                stray = sorted(
                    set(rule.source_engines) - set(entry.source_engines if entry else ())
                )
                if stray:
                    raise CatalogError(
                        f"{path.name}: a version rule needs a homogeneous pair; {target} "
                        f"lists no version lines for {stray}"
                    )
        rules.append(rule)
    if not rules:
        raise CatalogError(f"no gap rule files under {directory}")
    return tuple(rules)


@cache
def aurora_version_lines() -> dict[str, AuroraVersionLines]:
    """The loaded Aurora version lines (read once per process)."""
    return load_aurora_versions()


@cache
def gap_rules() -> tuple[GapRule, ...]:
    """The loaded gap rules (read once per process), sorted by id."""
    return load_gap_rules(versions=aurora_version_lines())
