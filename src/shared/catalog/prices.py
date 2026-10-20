"""The one dated price table every cost estimate reads (#1196).

``src/catalog/prices.toml`` holds us-east-1 on-demand list prices, written by
``scripts/refresh_prices.py`` from the public AWS Price List bulk files (no credentials
needed). Each section is one engine configuration (``aurora_mysql.io_optimized``,
``documentdb.standard``, ``elasticache.valkey``, ...), and its keys are
``EnginePricing`` field names. An engine file names the configuration it is priced on
(``[pricing] list_prices``), and the engine catalog merges that section into the
engine's ``[pricing]``, so the routing cost dimension, the analysis estimates and the
load test all read the same figures through ``catalog.pricing(engine)``.

``retrieved_on`` is the day the script read the files; ``published`` is each offer
file's own publication date. Synthesis states the retrieval date in every deliverable
(``facts.price_list``, ADR-032), and lint warns when the table is old
(``scripts/refresh_prices.py --check-age``).
"""

from __future__ import annotations

import datetime as dt
import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PRICES_FILE = Path(__file__).resolve().parents[2] / "catalog" / "prices.toml"
SOURCE = "AWS Price List"
# The one month every estimate bills (#1196): 365 x 24 / 12, the AWS Pricing Calculator's.
HOURS_PER_MONTH = 730
SECONDS_PER_MONTH = HOURS_PER_MONTH * 3600
PRICING_MODEL = "on-demand"


class PriceTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    region: str = Field(min_length=1)
    retrieved_on: dt.date
    published: dict[str, dt.date] = Field(min_length=1)
    # engine -> configuration -> EnginePricing fields (validated where they are merged).
    sections: dict[str, dict[str, dict[str, Any]]]

    def section(self, path: str) -> dict[str, Any]:
        """The ``engine.configuration`` section, or a KeyError naming it."""
        engine, _, config = path.partition(".")
        try:
            return dict(self.sections[engine][config])
        except KeyError:
            raise KeyError(f"no [{path}] section in {PRICES_FILE.name}") from None


def load_price_table(path: Path = PRICES_FILE) -> PriceTable:
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    meta = {k: data.pop(k) for k in ("region", "retrieved_on", "published") if k in data}
    return PriceTable.model_validate({**meta, "sections": data})


@cache
def price_table() -> PriceTable:
    """The loaded table (read once per process)."""
    return load_price_table()


def price_basis() -> dict[str, str]:
    """What the deliverables state about the prices behind every figure (``facts.price_list``)."""
    table = price_table()
    return {
        "source": SOURCE,
        "region": table.region,
        "pricing_model": PRICING_MODEL,
        "retrieved_on": table.retrieved_on.isoformat(),
    }


def estimates_price_basis(bases: Mapping[str, Any]) -> dict[str, Any] | None:
    """``facts.price_list``: the prices behind the TCO's figures, from the estimates.

    ``bases`` maps each engine the TCO prices to the ``price_list`` its analysis estimate
    recorded (None for an estimate written before it did, #1196). ``retrieved_on`` is
    the one date when every dated estimate agrees, else None, and ``engine_dates`` gives
    each dated engine's own, so a job re-synthesized after a refresh states the dates its
    figures were priced on, never today's table. None when no estimate has a date.
    """
    dated = {
        engine: basis
        for engine, basis in sorted(bases.items())
        if isinstance(basis, Mapping) and basis.get("retrieved_on")
    }
    if not dated:
        return None
    first = next(iter(dated.values()))
    dates = {engine: str(basis["retrieved_on"]) for engine, basis in dated.items()}
    return {
        "source": str(first.get("source") or SOURCE),
        "region": str(first.get("region") or price_table().region),
        "pricing_model": str(first.get("pricing_model") or PRICING_MODEL),
        "retrieved_on": next(iter(set(dates.values()))) if len(set(dates.values())) == 1 else None,
        "engine_dates": dates,
        "undated_engines": sorted(set(bases) - set(dated)),
    }
