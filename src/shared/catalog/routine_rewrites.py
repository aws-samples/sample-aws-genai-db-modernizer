# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine rewrite rules (#1937): the only changes the pipeline makes to a routine on
its own, each to its header only, never its body. Each rule's ``reason`` says what it
changes, the privileges a routine runs with or the path it resolves names in included.

One ``src/catalog/routine_rewrites/<rule_id>.toml`` per rule says which source and target
engines and routine kinds it applies to, why the change is safe, and the public
documentation it rests on, like the routine check rules
(:mod:`src.shared.catalog.routine_checks`). The rewriters
(:mod:`src.tools.routines.rewrites`, :mod:`src.tools.routines.dependencies`) only read
them: each rewrite they apply is recorded with its rule's id and reason.

Rule fields:

- ``id``: equals the file name.
- ``feature``: the short name of the change (one line).
- ``source_engines``, ``targets``, ``kinds``: what it applies to, as a check rule's.
- ``reason``: what the change is, what it fixes, and what it changes beyond the text
  (one sentence).
- ``docs``: the public documentation the rule rests on, by name.
- ``[match]``: ``rewrite`` (:data:`ROUTINE_REWRITES`), and ``resolves``: the routine
  check rules whose failure the rewrite fixes, by id (a check it fixes passes once the
  rewrite is applied; every id must be a check rule of the catalog).
"""

from __future__ import annotations

import tomllib
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.gaps import AURORA_TARGETS, SOURCE_ENGINES
from src.shared.catalog.routine_checks import RoutineCheckRule, RoutineKind, routine_check_rules

__all__ = [
    "ROUTINE_REWRITES",
    "ROUTINE_REWRITES_DIR",
    "RoutineRewriteKind",
    "RoutineRewriteMatch",
    "RoutineRewriteRule",
    "load_routine_rewrite_rules",
    "routine_rewrite_rules",
]

ROUTINE_REWRITES_DIR = Path(__file__).resolve().parents[2] / "catalog" / "routine_rewrites"

# The rewrites the rewriters implement:
# - ``definer`` (MySQL): the ``DEFINER`` clause names the target user;
# - ``owner`` (PostgreSQL): a function or procedure is owned by the target user;
# - ``search_path`` (PostgreSQL): a SECURITY DEFINER function or procedure pins the
#   source's effective search_path, to keep its name resolution;
# - ``dependency_order`` (every engine): routines and triggers are created after the
#   routines they name.
RoutineRewriteKind = Literal["definer", "owner", "search_path", "dependency_order"]
ROUTINE_REWRITES: tuple[RoutineRewriteKind, ...] = (
    "definer",
    "owner",
    "search_path",
    "dependency_order",
)
_REWRITE_ENGINES: dict[RoutineRewriteKind, frozenset[str]] = {
    "definer": frozenset({"mysql", "mariadb"}),
    "owner": frozenset({"postgresql"}),
    "search_path": frozenset({"postgresql"}),
}
# PostgreSQL's CREATE TRIGGER has no owner and no SET clause.
_FUNCTION_ONLY: frozenset[RoutineRewriteKind] = frozenset({"owner", "search_path"})
_ONE_LINE = r"^[^\n]+$"


class RoutineRewriteMatch(BaseModel):
    """What a rule rewrites, and the check rules it resolves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rewrite: RoutineRewriteKind
    resolves: tuple[str, ...] = ()


class RoutineRewriteRule(BaseModel):
    """One routine rewrite rule (one ``src/catalog/routine_rewrites/<id>.toml``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    feature: str = Field(min_length=1, pattern=_ONE_LINE)
    source_engines: tuple[str, ...] = Field(min_length=1)
    targets: tuple[str, ...] = Field(min_length=1)
    kinds: tuple[RoutineKind, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, pattern=_ONE_LINE)
    docs: str = Field(min_length=1, pattern=_ONE_LINE)
    match: RoutineRewriteMatch

    @model_validator(mode="after")
    def _known_values(self) -> RoutineRewriteRule:
        if unknown := sorted(set(self.source_engines) - SOURCE_ENGINES):
            raise ValueError(f"unknown source engines {unknown}")
        if unknown := sorted(set(self.targets) - AURORA_TARGETS):
            raise ValueError(f"unknown targets {unknown}")
        for field in ("source_engines", "targets", "kinds"):
            values = getattr(self, field)
            if len(set(values)) != len(values):
                raise ValueError(f"{field} lists a value twice")
        rewrite = self.match.rewrite
        engines = _REWRITE_ENGINES.get(rewrite)
        if engines is not None and (stray := sorted(set(self.source_engines) - engines)):
            raise ValueError(f"a {rewrite} rewrite applies only to {sorted(engines)}, not {stray}")
        if rewrite in _FUNCTION_ONLY and (
            stray := sorted(set(self.kinds) - {"function", "procedure"})
        ):
            raise ValueError(
                f"a {rewrite} rewrite applies only to functions and procedures, not {stray}"
            )
        if len(set(self.match.resolves)) != len(self.match.resolves):
            raise ValueError("resolves lists a rule twice")
        return self


def load_routine_rewrite_rules(
    directory: Path = ROUTINE_REWRITES_DIR,
    check_rules: tuple[RoutineCheckRule, ...] | None = None,
) -> tuple[RoutineRewriteRule, ...]:
    """Every routine rewrite rule file in ``directory``, validated, sorted by id. Each id a
    rule ``resolves`` must be a check rule of ``check_rules`` (the catalog's) for one of
    its source engines."""
    checks = {r.id: r for r in (routine_check_rules() if check_rules is None else check_rules)}
    rules: list[RoutineRewriteRule] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                rule = RoutineRewriteRule.model_validate(tomllib.load(fh))
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if rule.id != path.stem:
            raise CatalogError(f"{path.name}: id {rule.id!r} does not match the file name")
        for check_id in rule.match.resolves:
            check = checks.get(check_id)
            if check is None or not set(check.source_engines) & set(rule.source_engines):
                raise CatalogError(
                    f"{path.name}: resolves {check_id!r}, which is no check rule for "
                    f"{list(rule.source_engines)}"
                )
        rules.append(rule)
    if not rules:
        raise CatalogError(f"no routine rewrite rule files under {directory}")
    return tuple(rules)


@cache
def routine_rewrite_rules() -> tuple[RoutineRewriteRule, ...]:
    """The loaded routine rewrite rules (read once per process), sorted by id."""
    return load_routine_rewrite_rules()
