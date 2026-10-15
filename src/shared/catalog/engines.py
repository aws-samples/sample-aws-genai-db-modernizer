"""The engine half of the rule catalog (ADR-031 Decision 8, #492).

One ``src/catalog/engines/<id>.toml`` per engine carries the engine-wide facts every
consumer must agree on: its display name, its colours, whether it is a real routing
target today (``status``), its architectural role and whether it can be a system of
record. This module loads those files into strict Pydantic models (an unknown, missing
or mistyped field fails at import time, so a broken catalog never reaches a run) and
derives the engine sets the rest of the code reads.

This first slice changes no behaviour: ``src.shared.engine_names``,
``src.shared.cache_engines``, ``src.shared.migration_wave_engines``,
``src.shared.report_facts``, the decision report and the deck build their existing
constants, under their existing names, from here. The UI reads the same facts through
``src/ui/src/utils/engineCatalog.generated.js``, written by
``scripts/render_catalog.py`` (``--check`` runs in lint).

Fields:

- ``id``: the engine id the pipeline emits; must equal the file name.
- ``display_name``: the AWS-branded name every deliverable shows.
- ``display_order``: position in the display-name table (unique).
- ``ui_picker_order``: position in the assignment gate's engine picker (unique). Required,
  so a new engine always appears in the picker.
- ``status``: ``active`` (a routing target), ``future`` (a named placeholder that is
  never a routing candidate, ADR-031 Decision 9) or ``legacy`` (an id only older
  reports carry, kept so they still render; routing never emits it).
- ``role``: what the engine is for in the target architecture.
- ``system_of_record``: whether the engine can durably own a table. A cache fronts hot
  reads and a search read model indexes data synced from an owner; neither owns.
- ``colors.ui``: the live UI's hex colour (Sankey nodes, assignment gate).
- ``colors.ui_badge``: the live UI's badge colour name.
- ``colors.report_badge``: the decision report's badge hex, darkened for WCAG AA
  contrast under white text (optional: no badge colour means the report's grey).
- ``colors.deck``: the executive deck's accent hex (optional: only the engines the deck
  colours).
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog" / "engines"

EngineStatus = Literal["active", "future", "legacy"]
EngineRole = Literal[
    "relational", "key_value", "document", "search", "cache", "graph", "wide_column"
]
# The colour names the UI's badge component accepts.
BadgeColor = Literal["blue", "grey", "green", "red"]
_HEX = r"^#[0-9A-Fa-f]{6}$"


class EngineColors(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ui: str = Field(pattern=_HEX)
    ui_badge: BadgeColor
    report_badge: str | None = Field(default=None, pattern=_HEX)
    deck: str | None = Field(default=None, pattern=_HEX)


class EngineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    display_order: int = Field(ge=1)
    ui_picker_order: int = Field(ge=1)
    status: EngineStatus
    role: EngineRole
    system_of_record: bool
    colors: EngineColors

    @model_validator(mode="after")
    def _roles_that_never_own(self) -> EngineEntry:
        if self.role in ("cache", "search") and self.system_of_record:
            raise ValueError(f"{self.id}: a {self.role} engine is never a system of record")
        return self


class CatalogError(ValueError):
    """The engine catalog on disk is inconsistent (raised at import time)."""


def load_engine_catalog(directory: Path = CATALOG_DIR) -> tuple[EngineEntry, ...]:
    """Every engine file in ``directory``, validated, in ``display_order``.

    A deleted engine file is not detected here: the only lists to check it against are
    the hand-kept ones the catalog replaces. ``tests/unit/shared/test_engine_catalog.py``
    (a release gate) fails instead, naming the contract enums, triage sets and runners
    that still name the engine.
    """
    entries: list[EngineEntry] = []
    for path in sorted(directory.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
            entry = EngineEntry.model_validate(data)
        except (tomllib.TOMLDecodeError, ValueError) as exc:
            raise CatalogError(f"{path.name}: {exc}") from exc
        if entry.id != path.stem:
            raise CatalogError(f"{path.name}: id {entry.id!r} does not match the file name")
        entries.append(entry)
    if not entries:
        raise CatalogError(f"no engine files under {directory}")
    for field in ("display_order", "ui_picker_order"):
        orders = [getattr(e, field) for e in entries]
        if len(set(orders)) != len(orders):
            raise CatalogError(f"{field} values repeat: {sorted(orders)}")
    return tuple(sorted(entries, key=lambda e: e.display_order))


@cache
def engines() -> tuple[EngineEntry, ...]:
    """The loaded catalog (read once per process)."""
    return load_engine_catalog()


def engine_ids(
    *,
    status: EngineStatus | Iterable[EngineStatus] | None = None,
    role: EngineRole | Iterable[EngineRole] | None = None,
    system_of_record: bool | None = None,
) -> frozenset[str]:
    """Ids of the engines matching every given filter (``None`` means any)."""
    statuses = {status} if isinstance(status, str) else set(status) if status else None
    roles = {role} if isinstance(role, str) else set(role) if role else None
    return frozenset(
        e.id
        for e in engines()
        if (statuses is None or e.status in statuses)
        and (roles is None or e.role in roles)
        and (system_of_record is None or e.system_of_record is system_of_record)
    )


# Engines routing can target today: every routing-facing role set is restricted to these.
ACTIVE: tuple[EngineStatus, ...] = ("active",)
# Engines a report may name as a real component: active ones plus legacy ids.
REPORTABLE: tuple[EngineStatus, ...] = ("active", "legacy")
