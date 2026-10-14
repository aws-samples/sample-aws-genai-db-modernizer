"""Shadow mode for the sqlglot facade (#557).

The regex answer drives everything; the facade runs alongside it and every
disagreement is recorded, never applied. These tests pin that: the parsed
collection is identical with or without the shadow (even when the facade is
made to disagree, or to fail), disagreements are categorised, and the
per-job artifact is written.
"""

from __future__ import annotations

import json

import pytest

import src.tools.sql.facade as facade
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.facade import TableRef, TableReferences
from src.tools.sql.shadow import SHADOW_ARTIFACT, TableShadow, write_shadow_artifact
from tests.unit.tools.sql.table_refs_snapshot import load_sample


def _collection(queries: list[str], engine: str = "postgresql") -> dict:
    return {
        "metadata": {"database_name": "shop", "engine": engine},
        "tables": [{"table_name": n} for n in ("orders", "users", "posts", "topics")],
        "queries": [
            {"query_text": q, "digest": f"q{i}", "execution_count": 1}
            for i, q in enumerate(queries)
        ],
    }


QUERIES = [
    "SELECT * FROM orders o JOIN users u ON u.id = o.user_id",
    "SELECT * FROM posts p WHERE p.x IS DISTINCT FROM topics.id",
    "DELETE FROM orders o USING users u WHERE o.user_id = u.id",
    "SELECT * FROM posts, topics WHERE posts.topic_id = topics.id",
    "WITH u AS ( -- users allowed to join chat\n SELECT id FROM users) SELECT * FROM u",
    "SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) FROM orders",
    "REFRESH MATERIALIZED VIEW CONCURRENTLY orders",
]


def _dump(parsed: dict) -> str:
    return json.dumps(parsed, sort_keys=True, default=str)


class TestOutputUnchanged:
    def test_parsed_collection_is_identical_with_and_without_shadow(self):
        data = _collection(QUERIES)
        plain = _dump(parse_offline_collection(json.loads(json.dumps(data))))
        shadow = TableShadow()
        shadowed = _dump(parse_offline_collection(json.loads(json.dumps(data)), shadow=shadow))
        assert shadowed == plain
        assert shadow.disagree > 0  # the shadow did see disagreements

    @pytest.mark.parametrize("sample", ["wordpress", "discourse", "trailgear"])
    def test_samples_are_byte_identical_with_and_without_shadow(self, sample):
        plain = _dump(parse_offline_collection(load_sample(sample)))
        shadow = TableShadow()
        assert _dump(parse_offline_collection(load_sample(sample), shadow=shadow)) == plain
        assert shadow.queries == len(json.loads(plain)["queries"])

    def test_a_disagreeing_facade_changes_nothing(self, monkeypatch):
        def bogus(sql, engine):
            return TableReferences(
                tables=(TableRef(name="phantom"),),
                cte_names=(),
                parsed_by="sqlglot",
                fallback_reason=None,
                dialect="postgres",
                truncated=False,
            )

        data = _collection(QUERIES)
        plain = _dump(parse_offline_collection(json.loads(json.dumps(data))))
        monkeypatch.setattr(facade, "table_references", bogus)
        shadow = TableShadow()
        assert _dump(parse_offline_collection(json.loads(json.dumps(data)), shadow=shadow)) == plain
        assert shadow.agree == 0
        assert all(d["facade"] == ["phantom"] for d in shadow.disagreements)

    def test_a_failing_facade_changes_nothing(self, monkeypatch):
        def boom(sql, engine):
            raise RuntimeError("facade bug")

        data = _collection(QUERIES)
        plain = _dump(parse_offline_collection(json.loads(json.dumps(data))))
        monkeypatch.setattr(facade, "table_references", boom)
        shadow = TableShadow()
        assert _dump(parse_offline_collection(json.loads(json.dumps(data)), shadow=shadow)) == plain
        assert shadow.queries == 0


class TestDisagreements:
    @pytest.fixture
    def shadow(self) -> TableShadow:
        shadow = TableShadow()
        parse_offline_collection(_collection(QUERIES), shadow=shadow)
        return shadow

    def _row(self, shadow: TableShadow, qid: str) -> dict:
        return next(d for d in shadow.disagreements if d["query_id"] == qid)

    def test_agreeing_queries_are_counted_not_listed(self, shadow):
        assert "q0" not in {d["query_id"] for d in shadow.disagreements}

    @pytest.mark.parametrize(
        "qid,current_only,facade_only,category",
        [
            ("q1", ["topics.id"], [], "is_distinct_from"),
            ("q2", [], ["shop.users"], "delete_using"),
            ("q3", [], ["shop.topics"], "comma_join"),
            ("q4", ["chat"], [], "comment_text"),
            ("q5", ["CURRENT_TIMESTAMP"], [], "from_in_function"),
        ],
    )
    def test_each_disagreement_is_categorised(
        self, shadow, qid, current_only, facade_only, category
    ):
        row = self._row(shadow, qid)
        assert row["current_only"] == current_only
        assert row["facade_only"] == facade_only
        assert row["categories"] == [category]

    def test_pseudo_table_category(self):
        shadow = TableShadow()
        parse_offline_collection(
            _collection(["SELECT ?, ... FROM DUAL WHERE (SELECT ? FROM DUAL) IS NULL"], "mysql"),
            shadow=shadow,
        )
        assert shadow.disagreements[0]["categories"] == ["pseudo_table"]

    def test_fallback_is_recorded_and_agrees(self, shadow):
        assert "q6" not in {d["query_id"] for d in shadow.disagreements}
        assert shadow.fallbacks == [{"query_id": "q6", "reason": "command", "truncated": False}]

    def test_summary(self, shadow):
        summary = shadow.summary()
        assert summary == {
            "queries": 7,
            "agree": 2,
            "disagree": 5,
            "agreement_rate": round(2 / 7, 4),
            "sqlglot_parsed": 6,
            "sqlglot_agree": 1,
            "sqlglot_agreement_rate": round(1 / 6, 4),
            "order_differs": 0,
            "parsed_by": {"regex": 1, "sqlglot": 6},
            "fallback_reasons": {"command": 1},
            "disagreement_categories": {
                "comma_join": 1,
                "comment_text": 1,
                "delete_using": 1,
                "from_in_function": 1,
                "is_distinct_from": 1,
            },
        }

    def test_artifact_payload(self, shadow):
        payload = shadow.to_dict()
        assert payload["mode"] == "shadow"
        assert payload["drives_output"] == "regex"
        assert payload["source_engine"] == "postgresql"
        assert payload["dialect"] == "postgres"
        assert payload["sqlglot_version"].startswith("30.")
        assert payload["summary"] == shadow.summary()


class _Store:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.written: dict[str, dict] = {}

    def write_json(self, path: str, data: dict) -> None:
        if self.fail:
            raise OSError("disk full")
        self.written[path] = data


def test_write_shadow_artifact():
    shadow = TableShadow()
    parse_offline_collection(_collection(QUERIES), shadow=shadow)
    store = _Store()
    path = write_shadow_artifact(store, "shop", "job1", shadow)
    assert path == f"shop/job1/{SHADOW_ARTIFACT}"
    assert store.written[path]["summary"]["disagree"] == 5


def test_write_shadow_artifact_never_raises():
    assert write_shadow_artifact(_Store(fail=True), "shop", "job1", TableShadow()) is None
