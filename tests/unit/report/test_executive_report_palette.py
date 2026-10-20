"""#1246: the executive report draws each engine in the rule catalog's one colour
(``colors.ui``), the colour every UI view uses, instead of a third palette of its own
(Aurora pink, DynamoDB yellow, ElastiCache purple, from the retired ``colors.deck``).

Some catalog colours are too dark to read as text on the report's dark cards, so the
report uses them for marks only: the share bar, each wave card's accent rule and a rule
left of each engine's table row. Engine names are written in white.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from reportlab.lib.colors import HexColor
from reportlab.platypus import Paragraph, Table

from src.report import executive_report as er
from src.shared import catalog

FIXTURES = Path(__file__).parent / "fixtures"
UI_CATALOG = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "ui"
    / "src"
    / "utils"
    / "engineCatalog.generated.js"
)


def _ui_engine_hex() -> dict[str, str]:
    """``ENGINE_HEX`` as the UI's generated module states it."""
    text = UI_CATALOG.read_text(encoding="utf-8")
    block = re.search(r"export const ENGINE_HEX = \{(.*?)\};", text, re.S)
    assert block is not None
    return dict(re.findall(r"(\w+): '(#[0-9a-fA-F]{6})'", block.group(1)))


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURES / "wordpress_no_schema_design_report.json").read_text()
    )
    return loaded


def test_every_catalog_engine_has_its_ui_colour_in_the_report() -> None:
    for entry in catalog.engines():
        assert er._engine_color(entry.id).hexval() == HexColor(entry.colors.ui).hexval(), entry.id


def test_the_report_palette_is_the_ui_palette() -> None:
    ui = _ui_engine_hex()
    assert set(ui) == {e.id for e in catalog.engines()}
    for engine, hex_colour in ui.items():
        assert er._engine_color(engine).hexval() == HexColor(hex_colour).hexval(), engine


def test_the_retired_deck_colours_are_gone() -> None:
    # The PDF's old accents, which no UI view used.
    retired = {"#FF5C85", "#41B3FF", "#FBD332", "#AD5CFF", "#00E500", "#FF693C"}
    used = {er._engine_color(e.id).hexval().upper().replace("0X", "#") for e in catalog.engines()}
    assert used.isdisjoint(retired)


@pytest.mark.passes_on_main  # a negative case: what must not change
def test_an_unknown_engine_reads_in_the_reports_grey() -> None:
    assert er._engine_color("not_an_engine") == er.MUTED


def _tables(flow: list) -> list[Table]:
    return [f for f in flow if isinstance(f, Table)]


def _row_rules(table: Table) -> dict[int, str]:
    return {
        cmd[1][1]: cmd[4].hexval()
        for cmd in table._linecmds  # reportlab keeps the line commands here after setStyle
        if cmd[0] == "LINEBEFORE" and cmd[1][0] == 0
    }


@pytest.mark.parametrize("section", ["_recommendation", "_numbers", "_why"])
def test_each_engine_row_carries_its_colour_beside_a_white_name(
    report: dict[str, Any], section: str
) -> None:
    flow = getattr(er, section)(report, er._styles(), er.table_names(report))
    engines = er._architecture_engines(report)
    assert engines
    (table,) = [t for t in _tables(flow) if t._cellvalues[0][0].text == "Engine"]
    rules = _row_rules(table)
    for row, engine in enumerate(engines, start=1):
        assert rules[row] == er._engine_color(engine["engine"]).hexval(), (section, engine)
        name = table._cellvalues[row][0]
        assert isinstance(name, Paragraph)
        assert "<font color" not in name.text, (section, engine)
    # The header and the total row carry no engine rule.
    assert 0 not in rules
    assert max(rules) == len(engines)


def test_wave_cards_keep_the_engine_colour_as_their_rule_not_their_text(
    report: dict[str, Any],
) -> None:
    waves = er._resolve_waves(report)
    assert waves
    flow = er._waves(report, er._styles(), er.table_names(report))
    panels = [f for f in flow if isinstance(f, er._Panel)]
    assert len(panels) == len(waves)
    for panel, wave in zip(panels, waves, strict=True):
        engines = [str(e) for e in wave.get("engines") or []]
        expected = er._engine_color(engines[0]) if engines else er.MUTED
        assert panel.accent == expected, wave.get("wave")
        kicker = panel.content[0]
        assert "<font color" not in kicker.text, wave.get("wave")


@pytest.mark.passes_on_main  # a negative case: what must not change
def test_a_table_without_row_accents_draws_no_row_rule() -> None:
    table = er._table(er._styles(), ("A", "B"), [["x", "y"]], (1, 1))
    assert _row_rules(table) == {}


def test_share_bar_labels_read_on_every_engine_colour() -> None:
    for entry in catalog.engines():
        fill = er._engine_color(entry.id)
        ink = er._label_ink(fill)
        assert ink in (er.WHITE, er.INK)
        assert er.contrast_ratio(fill, ink) == max(
            er.contrast_ratio(fill, er.WHITE), er.contrast_ratio(fill, er.INK)
        ), entry.id
    # Aurora's tan is too dark for the card ink the bar used to write every label in.
    assert er._label_ink(er._engine_color("aurora_mysql")) == er.WHITE


def _delta_e(a: str, b: str) -> float:
    """CIE76 colour difference in CIELAB between two #rrggbb colours."""

    def lab(hex_colour: str) -> tuple[float, float, float]:
        def lin(i: int) -> float:
            c = int(hex_colour[i : i + 2], 16) / 255
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = lin(1), lin(3), lin(5)

        def f(v: float) -> float:
            return v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116

        x = f((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
        y = f(0.2126 * r + 0.7152 * g + 0.0722 * b)
        z = f((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
        return 116 * y - 16, 500 * (x - y), 200 * (y - z)

    la, lb = lab(a), lab(b)
    return float(sum((p - q) ** 2 for p, q in zip(la, lb, strict=True)) ** 0.5)


def test_neighbouring_share_bar_segments_are_clearly_different_colours() -> None:
    """Any two engines can sit side by side in the share bar (or in the row rules), the
    two Aurora engines included: every pair differs by at least 20 (CIE76)."""
    entries = list(catalog.engines())
    assert {"aurora_mysql", "aurora_postgresql"} <= {e.id for e in entries}
    for i, a in enumerate(entries):
        for b in entries[i + 1 :]:
            assert _delta_e(a.colors.ui, b.colors.ui) >= 20, (a.id, b.id)


class _Canvas:
    """Records the fills a flowable draws."""

    def __init__(self) -> None:
        self.fill: Any = None
        self.rects: list[tuple[Any, float, float]] = []

    def setFillColor(self, color: Any) -> None:  # noqa: N802 -- reportlab's canvas API
        self.fill = color

    def rect(self, x: float, y: float, w: float, h: float, **_: Any) -> None:
        self.rects.append((self.fill, x, w))

    def setFont(self, *_: Any) -> None:  # noqa: N802
        pass

    def drawString(self, *_: Any) -> None:  # noqa: N802
        pass


def test_share_bar_splits_neighbouring_segments_with_a_gap() -> None:
    mysql, postgres = er._engine_color("aurora_mysql"), er._engine_color("aurora_postgresql")
    bar = er._Bar(
        [(mysql, 60.0, "Aurora MySQL"), (postgres, 39.0, "Aurora PostgreSQL"), (mysql, 1.0, "x")]
    )
    bar.wrap(800, 100)
    canvas = _Canvas()
    bar.canv = canvas  # type: ignore[assignment]
    bar.draw()
    gaps = [(x, w) for fill, x, w in canvas.rects if fill == er.BG]
    starts = [x for fill, x, _ in canvas.rects if fill in (mysql, postgres)]
    # One gap at the start of every segment but the first, the narrow one included.
    assert [x for x, _ in gaps] == starts[1:]
    assert all(w == er._Bar.SEPARATOR for _, w in gaps)


@pytest.mark.passes_on_main  # a negative case: a single segment needs no gap
def test_a_one_segment_share_bar_has_no_gap() -> None:
    bar = er._Bar([(er._engine_color("dynamodb"), 100.0, "DynamoDB")])
    bar.wrap(800, 100)
    canvas = _Canvas()
    bar.canv = canvas  # type: ignore[assignment]
    bar.draw()
    assert [fill for fill, _, _ in canvas.rects] == [er._engine_color("dynamodb")]
