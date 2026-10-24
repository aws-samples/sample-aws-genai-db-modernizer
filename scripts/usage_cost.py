#!/usr/bin/env python3
"""Turn a run's model usage record into dollars (#1494).

    uv run python scripts/usage_cost.py <artifacts/<db>/<job>/_logs/model_usage.json>

Reads the record ``assess()`` writes, prices every model id with
``src/catalog/model_prices.toml`` (public Bedrock list prices, dated there) and prints
the cost per phase and model and in total. A model id with no price raises
``MissingPriceError``: a guessed price would be a wrong figure.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

PRICES_FILE = Path(__file__).resolve().parent.parent / "src" / "catalog" / "model_prices.toml"
#: usage counter -> price key (USD per 1M tokens)
RATES = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadInputTokens": "cache_read",
    "cacheWriteInputTokens": "cache_write",
}


class MissingPriceError(KeyError):
    """A model id in the usage record has no entry in the price table."""


def load_prices(path: Path = PRICES_FILE) -> dict[str, dict[str, float]]:
    """Prices by Bedrock model ID, as a usage record names it.

    The table is keyed by model name and inference profile prefix (``[opus-5.5.us]``); each
    ID is spelled out from ``MODEL_IDS`` (src/shared/models.py), the one place IDs live.
    """
    from src.shared import models  # the IDs only; this script calls no model

    with path.open("rb") as fh:
        data = tomllib.load(fh)
    prices: dict[str, dict[str, float]] = {}
    for name, model_id in models.MODEL_IDS.items():
        _, _, base = model_id.partition(".")
        for profile, row in (data.get(name) or {}).items():
            prices[f"{profile}.{base}"] = row
    return prices


def cost_of(row: dict[str, Any], model_id: str, prices: dict[str, dict[str, float]]) -> float:
    """Dollars for one usage row of ``model_id``."""
    if model_id not in prices:
        raise MissingPriceError(
            f"no price for model {model_id!r} in {PRICES_FILE.name}; add it from the public "
            "Bedrock price list before computing a cost"
        )
    price = prices[model_id]
    return sum(
        int(row.get(counter) or 0) * price[key] / 1_000_000 for counter, key in RATES.items()
    )


def compute(usage: dict[str, Any], prices: dict[str, dict[str, float]]) -> dict[str, Any]:
    by_phase: dict[str, dict[str, float]] = {}
    by_model: dict[str, float] = {}
    for phase, models in usage["by_phase"].items():
        for model_id, row in models.items():
            dollars = cost_of(row, model_id, prices)
            by_phase.setdefault(phase, {})[model_id] = dollars
            by_model[model_id] = by_model.get(model_id, 0.0) + dollars
    return {
        "by_phase": {p: {**m, "total": sum(m.values())} for p, m in by_phase.items()},
        "by_model": by_model,
        "total_usd": sum(by_model.values()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("usage_json", type=Path)
    args = parser.parse_args(argv)
    usage = json.loads(args.usage_json.read_text())
    try:
        result = compute(usage, load_prices())
    except MissingPriceError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 1
    for phase, models in result["by_phase"].items():
        print(f"{phase}: ${models['total']:.2f}")
        for model_id, dollars in models.items():
            if model_id != "total":
                print(f"  {model_id}: ${dollars:.2f}")
    print(f"total: ${result['total_usd']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
