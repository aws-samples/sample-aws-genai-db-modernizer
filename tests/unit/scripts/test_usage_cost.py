"""``scripts/usage_cost.py``: dollars from a run's model usage record (#1494)."""

from __future__ import annotations

import json

import pytest

from scripts import usage_cost
from src.shared.models import ROLE_SPECS

PRICES = {"m": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 3.0}}
ROW = {
    "calls": 3,
    "inputTokens": 1_000_000,
    "outputTokens": 500_000,
    "cacheReadInputTokens": 2_000_000,
    "cacheWriteInputTokens": 100_000,
}


def test_cost_of_a_row() -> None:
    assert usage_cost.cost_of(ROW, "m", PRICES) == pytest.approx(2 + 5 + 0.4 + 0.3)


def test_compute_totals_by_phase_and_model() -> None:
    usage = {"by_phase": {"schema_design": {"m": ROW}, "synthesis": {"m": ROW}}}
    result = usage_cost.compute(usage, PRICES)
    assert result["by_phase"]["synthesis"]["total"] == pytest.approx(7.7)
    assert result["by_model"]["m"] == pytest.approx(15.4)
    assert result["total_usd"] == pytest.approx(15.4)


def test_a_missing_price_raises() -> None:
    with pytest.raises(usage_cost.MissingPriceError, match="no price for model 'other'"):
        usage_cost.compute({"by_phase": {"p": {"other": ROW}}}, PRICES)


def test_main_prints_dollars_and_fails_on_a_missing_price(tmp_path, capsys) -> None:
    ok = tmp_path / "ok.json"
    ok.write_text(
        json.dumps({"by_phase": {"schema_design": {"us.anthropic.claude-opus-5-5": ROW}}})
    )
    assert usage_cost.main([str(ok)]) == 0
    out = capsys.readouterr().out
    assert "schema_design: $" in out and "total: $" in out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"by_phase": {"p": {"unpriced-model": ROW}}}))
    assert usage_cost.main([str(bad)]) == 1
    assert "unpriced-model" in capsys.readouterr().err


def test_the_price_table_prices_every_default_model_and_is_sourced() -> None:
    import tomllib

    prices = usage_cost.load_prices()
    for spec in ROLE_SPECS.values():
        assert spec.default_model in prices, spec.default_model
    for row in prices.values():
        assert set(row) == {"input", "output", "cache_read", "cache_write"}
    raw = tomllib.loads(usage_cost.PRICES_FILE.read_text())
    assert raw["source"].startswith("https://") and raw["retrieved_on"]
