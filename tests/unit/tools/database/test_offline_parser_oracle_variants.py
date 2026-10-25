"""Oracle literal variants merge into one query shape in the offline parser (#412).

``V$SQL.SQL_ID`` hashes the exact SQL text, so a collection made before the script
grouped by ``FORCE_MATCHING_SIGNATURE`` holds one row per literal value, each with its
own ``digest``. The parser merges them by their literal-masked shape, for Oracle only.
"""

from __future__ import annotations

import pytest

from src.tools.database.offline_parser import _merge_query_variants, _transform_queries


def _row(digest: str, text: str, **over: object) -> dict:
    row = {
        "digest": digest,
        "query_text": text,
        "execution_count": 10,
        "total_time_ms": 100.0,
        "avg_time_ms": 10.0,
        "min_time_ms": 0,
        "max_time_ms": 0,
        "total_rows_sent": 10,
        "total_rows_examined": 20,
        "total_rows_affected": 0,
        "lock_time_ms": 1.0,
        "first_seen": "2026-01-10 00:00:00",
        "last_seen": "2026-01-11 00:00:00",
    }
    row.update(over)
    return row


def _merged(rows: list[dict], engine: str = "oracle") -> list[dict]:
    return _merge_query_variants(rows, engine)


class TestOracleVariantsMerge:
    def test_each_literal_kind_merges_to_one_row(self) -> None:
        variants = [
            "SELECT * FROM orders WHERE id = 1",
            "SELECT * FROM orders WHERE id = 2",
            "SELECT * FROM orders WHERE id = 3.5",
        ]
        rows = [_row(f"sql{i}", t) for i, t in enumerate(variants)]
        out = _merged(rows)
        assert len(out) == 1
        assert out[0]["execution_count"] == 30

    @pytest.mark.parametrize(
        "variants",
        [
            ["status = 'A'", "status = 'it''s'", "status = q'[B]'", "status = q'!C!'"],
            ["d > DATE '2024-01-01'", "d > DATE '2025-02-02'"],
            ["id IN (1, 2)", "id IN (3)", "id IN (4, 5, 6, 7)"],
        ],
    )
    def test_strings_dates_and_in_lists_merge(self, variants: list[str]) -> None:
        rows = [_row(f"sql{i}", f"SELECT * FROM t WHERE {v}") for i, v in enumerate(variants)]
        assert len(_merged(rows)) == 1

    def test_counters_are_summed_and_timing_combined(self) -> None:
        rows = [
            _row("a", "SELECT * FROM t WHERE id = 1", execution_count=10, total_time_ms=100.0),
            _row("b", "SELECT * FROM t WHERE id = 2", execution_count=30, total_time_ms=900.0),
        ]
        (m,) = _merged(rows)
        assert m["execution_count"] == 40
        assert m["total_time_ms"] == pytest.approx(1000.0)
        assert m["avg_time_ms"] == pytest.approx(25.0)
        assert m["total_rows_sent"] == 20
        assert m["lock_time_ms"] == pytest.approx(2.0)
        # The costliest variant is the sample text and names the merged row.
        assert m["query_text"] == "SELECT * FROM t WHERE id = 2"
        assert m["digest"] == "b"

    @pytest.mark.passes_on_main
    def test_a_row_that_never_varied_keeps_its_digest(self) -> None:
        out = _merged([_row("only", "SELECT * FROM t WHERE id = 1")])
        assert [r["digest"] for r in out] == ["only"]

    def test_force_matching_digests_merge_by_shape_too(self) -> None:
        # Two signatures that differ only in IN-list length still describe one shape.
        rows = [
            _row("fms_1", "SELECT * FROM t WHERE id IN (1, 2)"),
            _row("fms_2", "SELECT * FROM t WHERE id IN (1, 2, 3)"),
        ]
        assert len(_merged(rows)) == 1

    @pytest.mark.passes_on_main
    def test_rows_sharing_a_digest_still_merge(self) -> None:
        rows = [
            _row("fms_1", "SELECT * FROM t WHERE id = 1"),
            _row("fms_1", "select * from t where id = 9"),
        ]
        assert len(_merged(rows)) == 1

    def test_order_is_first_appearance(self) -> None:
        rows = [
            _row("b1", "SELECT * FROM b WHERE id = 1"),
            _row("a1", "SELECT * FROM a WHERE id = 1"),
            _row("b2", "SELECT * FROM b WHERE id = 2"),
        ]
        out = _merged(rows)
        assert [r["query_text"].split()[3] for r in out] == ["b", "a"]


@pytest.mark.passes_on_main
class TestStructuralVariantsStayApart:
    @pytest.mark.parametrize(
        "pair",
        [
            ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE b = 1"),
            ("SELECT * FROM t WHERE a = 1", "SELECT * FROM t WHERE a > 1"),
            ("SELECT * FROM t WHERE a = 1", "SELECT * FROM u WHERE a = 1"),
            ("SELECT a, b FROM t ORDER BY 1", "SELECT a, b FROM t ORDER BY 2"),
            ("SELECT * FROM t WHERE a = :1", "SELECT * FROM t WHERE a = 1"),
            ("INSERT INTO t VALUES (1, 2)", "INSERT INTO t VALUES (1, 2, 3)"),
        ],
    )
    def test_structure_differs_so_no_merge(self, pair: tuple[str, str]) -> None:
        rows = [_row("x", pair[0]), _row("y", pair[1])]
        assert len(_merged(rows)) == 2


@pytest.mark.passes_on_main
class TestOtherDialectsAreUnchanged:
    @pytest.mark.parametrize("engine", ["mysql", "postgresql", "sqlserver", "mariadb", ""])
    def test_distinct_digests_are_not_merged_by_text(self, engine: str) -> None:
        rows = [
            _row("d1", "SELECT * FROM t WHERE id = 1"),
            _row("d2", "SELECT * FROM t WHERE id = 2"),
        ]
        assert [r["digest"] for r in _merged(rows, engine)] == ["d1", "d2"]


def test_transform_queries_counts_one_query_per_shape() -> None:
    rows = [_row(f"s{i}", f"SELECT * FROM orders WHERE id = {i}") for i in range(5)]
    patterns = _transform_queries(rows, "db", {"orders"}, source_engine="oracle")
    assert len(patterns) == 1
    assert patterns[0]["execution_count"] == 50


def _long_statement(literal: str) -> str:
    """A statement over 2000 characters, cut at 2000 as the collection script cuts it."""
    filler = " AND ".join(f"col_{i:04d} IS NOT NULL" for i in range(200))
    return f"SELECT * FROM orders WHERE name = '{literal}' AND {filler}"[:2000]


class TestTruncatedOracleVariants:
    """Variants cut at the script's 2000-character cap merge on the text both keep (#1518)."""

    def test_variants_cut_at_different_points_merge(self) -> None:
        rows = [
            _row("a", _long_statement("AB")),
            _row("b", _long_statement("ABCDEFGHIJKLMNOPQRSTUVWXYZ")),
        ]
        assert all(len(r["query_text"]) == 2000 for r in rows)
        out = _merged(rows)
        assert len(out) == 1
        assert out[0]["execution_count"] == 20

    @pytest.mark.passes_on_main
    def test_truncated_variants_with_different_structure_stay_apart(self) -> None:
        other = _long_statement("AB").replace("FROM orders", "FROM invoices")
        out = _merged([_row("a", _long_statement("AB")), _row("b", other)])
        assert len(out) == 2

    @pytest.mark.passes_on_main
    def test_short_statements_are_never_prefix_merged(self) -> None:
        rows = [_row("a", "SELECT * FROM t WHERE id = 1"), _row("b", "SELECT * FROM t")]
        assert len(_merged(rows)) == 2

    def test_cap_matches_the_collection_script(self) -> None:
        from pathlib import Path

        from src.tools.database.offline_parser import ORACLE_TEXT_CAP

        script = Path("scripts/collect-oracle.sql").read_text()
        assert f"SQL_FULLTEXT, {ORACLE_TEXT_CAP}, 1" in script


_IN_HEAD = "SELECT * FROM orders WHERE " + " AND ".join(
    f"col_{i:04d} IS NOT NULL" for i in range(8)
)
_FILLER = " AND ".join(f"col_{i:04d} IS NOT NULL" for i in range(200))


def _in_statement(items: str, tail: str = "") -> str:
    """A statement over 2000 characters with a whole ``IN`` list, cut at 2000."""
    return f"{_IN_HEAD} AND code IN ({items}){tail} AND {_FILLER}"[:2000]


def _cut_inside_list(fragment: str) -> str:
    """The statement as an old collection holds it when the cap lands inside the list:
    exactly 2000 characters, ending with ``fragment`` (spaces pad, so the shape is unchanged)."""
    tail = f"code IN ({fragment}"
    pad = " " * (2000 - len(_IN_HEAD) - len(" AND ") - len(tail))
    text = f"{_IN_HEAD} AND {pad}{tail}"
    assert len(text) == 2000
    return text


class TestCutInsideInList:
    """A cut inside an ``IN (...)`` list still matches its sibling (#1541)."""

    @pytest.mark.parametrize(
        "cut_items",
        [
            "1, 2, 3, 4",
            "1, 2, 3, 45",
            "'AB', 'CD', 'E",
            "q'[AB]', q'[C,D)]', q'[E",
            "q'!AB!', q'!(C!', 'it''",
            "(1, 2), (3, (4",
            "1, 2, ",
        ],
    )
    def test_cut_list_merges_with_whole_sibling(self, cut_items: str) -> None:
        cut = _cut_inside_list(cut_items)
        whole = _in_statement("7, 8, 9", " AND status = 'A'")
        out = _merged([_row("a", cut), _row("b", whole)])
        assert len(out) == 1
        assert out[0]["execution_count"] == 20

    @pytest.mark.passes_on_main
    def test_cut_list_merges_with_longer_list_cut_elsewhere(self) -> None:
        a = _in_statement(", ".join(str(n) for n in range(400)))
        b = _in_statement(", ".join(str(n) for n in range(900, 1300)))
        assert len(a) == len(b) == 2000
        assert len(_merged([_row("a", a), _row("b", b)])) == 1

    @pytest.mark.passes_on_main
    @pytest.mark.parametrize(
        "replacement",
        [("FROM orders", "FROM invoices"), ("col_0003 IS NOT NULL", "col_0003 IS NULL")],
    )
    def test_structure_before_the_list_still_keeps_variants_apart(
        self, replacement: tuple[str, str]
    ) -> None:
        a = _in_statement(", ".join(str(n) for n in range(400)))
        b = _in_statement(", ".join(str(n) for n in range(900, 1300))).replace(*replacement)
        assert len(_merged([_row("a", a), _row("b", b)])) == 2

    @pytest.mark.passes_on_main
    def test_different_operator_at_the_list_keeps_variants_apart(self) -> None:
        a = _in_statement(", ".join(str(n) for n in range(400)))
        b = _in_statement(", ".join(str(n) for n in range(900, 1300))).replace(
            "code IN (", "code = ("
        )
        assert len(_merged([_row("a", a), _row("b", b)])) == 2


class TestOpenInListStart:
    @pytest.mark.parametrize(
        ("shape", "expected"),
        [
            ("A IN (?, ?", 5),
            ("A NOT IN (?, (?, ?), (?", 9),
            ("A IN (?, ...) AND B IN (?", 23),
            ("A IN (?, ...) AND B = ?", None),
            ('A IN ("x(y", ?', 5),
            ("A IN (/* ( */ ?", 5),
            ("F(?, G(?", None),
        ],
    )
    def test_start(self, shape: str, expected: int | None) -> None:
        from src.tools.sql.normalize import open_in_list_start

        assert open_in_list_start(shape, "oracle") == expected
