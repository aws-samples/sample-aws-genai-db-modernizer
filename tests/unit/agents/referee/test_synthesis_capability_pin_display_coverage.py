"""Every hard capability ``capability_registry`` can emit has a display string
(#393 review): ``_CAPABILITY_PIN_DISPLAY`` had no entry for ``inverted_index`` or
``scan_engine``, so a query pinned to Aurora for either leaked the raw capability id
into the resolved-risk text and the Aurora "Why" cell instead of reading as prose.
"""

from __future__ import annotations

from src.agents.referee.capability_registry import (
    CAPABILITY_DETECTORS,
    SIGNAL_TO_CAPABILITY,
    detect_required_capabilities,
)
from src.agents.referee.synthesis_report import _CAPABILITY_PIN_DISPLAY, _capability_pin_reason

# Capabilities ``detect_required_capabilities`` can actually return, exercised the same
# way it is driven in production: via a triage signal, via a regex-detected pattern in
# the query text, or via the dedicated aggregation/computed-join/utility-statement
# detectors it also calls.
_EMITTED_BY_SIGNAL = set(SIGNAL_TO_CAPABILITY.values())
_EMITTED_BY_REGEX = {cap for cap, patterns in CAPABILITY_DETECTORS.items() if patterns}


class TestCapabilityPinDisplayCoverage:
    def test_every_signal_mapped_capability_has_a_display_string(self) -> None:
        for cap in _EMITTED_BY_SIGNAL:
            assert cap in _CAPABILITY_PIN_DISPLAY, f"no display string for {cap!r}"

    def test_every_regex_detected_capability_has_a_display_string(self) -> None:
        for cap in _EMITTED_BY_REGEX:
            assert cap in _CAPABILITY_PIN_DISPLAY, f"no display string for {cap!r}"

    def test_text_search_capability_reads_as_prose(self) -> None:
        required = detect_required_capabilities("SELECT * FROM posts WHERE title LIKE '%foo%'", [])
        assert "inverted_index" in required
        assert _CAPABILITY_PIN_DISPLAY["inverted_index"] != "inverted_index"

    def test_window_function_capability_reads_as_prose(self) -> None:
        required = detect_required_capabilities(
            "SELECT id, ROW_NUMBER() OVER (ORDER BY id) FROM posts", []
        )
        assert "scan_engine" in required
        assert _CAPABILITY_PIN_DISPLAY["scan_engine"] != "scan_engine"

    def test_computed_join_capability_reads_as_prose(self) -> None:
        required = detect_required_capabilities(
            "SELECT * FROM groups g JOIN users u ON lower(g.name) = u.username_lower", []
        )
        assert "computed_join" in required
        assert _CAPABILITY_PIN_DISPLAY["computed_join"] != "computed_join"

    def test_capability_pin_reason_never_leaks_a_raw_id(self) -> None:
        """``_capability_pin_reason`` only ever returns display strings, never a raw
        capability id that slipped past ``_CAPABILITY_PIN_DISPLAY`` unmapped."""
        reason = (
            "co-dependency group → aurora_mysql | [capability] dynamodb lacks required "
            "capability: inverted_index, scan_engine"
        )
        text = _capability_pin_reason([reason])
        assert text is not None
        assert "inverted_index" not in text
        assert "scan_engine" not in text
        assert "full-text or pattern search" in text
        assert "window or recursive queries" in text


class TestCapabilityPinReasonMergesAllFragments:
    """#393 review: ``_capability_pin_reason`` used to keep only the FIRST
    "[capability] ... lacks required capability: ..." fragment in a reason string
    (``re.search``). Different excluded engines can lack different, non-identical
    subsets of what a query requires -- an engine that already has aggregation but
    not joins lists only "complex_joins", while one with neither lists both -- so
    reading only the first fragment under-reported the full set of reasons."""

    def test_capabilities_from_every_fragment_are_merged(self) -> None:
        reason = (
            "co-dependency group → aurora_mysql | "
            "[capability] opensearch lacks required capability: complex_joins; "
            "[capability] dynamodb lacks required capability: aggregation, complex_joins"
        )
        text = _capability_pin_reason([reason])
        assert text == "aggregation and multi-table joins"

    def test_no_duplicate_capability_names_when_fragments_repeat(self) -> None:
        reason = (
            "[capability] dynamodb lacks required capability: complex_joins; "
            "[capability] documentdb lacks required capability: complex_joins; "
            "[capability] opensearch lacks required capability: complex_joins"
        )
        text = _capability_pin_reason([reason])
        assert text == "multi-table joins"
