"""The single eliminated-engine rule (#235), in one place.

Before this module existed, the reality-check handler and the UI counted ANY
remaining query (in or out of scope) to decide whether an engine was
eliminated, while synthesis and the consolidation records counted only
in-scope queries. An engine left with only out-of-scope queries was therefore
shown as active by the handler and the UI, but eliminated everywhere else.
"""

from __future__ import annotations

from src.shared.engine_elimination import eliminated_engines, in_scope_distribution


def _qa(engine: str, *, in_scope: bool = True) -> dict:
    return {"query_id": f"{engine}-{in_scope}", "assigned_engine": engine, "in_scope": in_scope}


class TestInScopeDistribution:
    def test_counts_in_scope_queries_only(self) -> None:
        qas = [
            {"query_id": "1", "assigned_engine": "dynamodb", "in_scope": True},
            {"query_id": "2", "assigned_engine": "dynamodb", "in_scope": True},
            {"query_id": "3", "assigned_engine": "dynamodb", "in_scope": False},
            {"query_id": "4", "assigned_engine": "documentdb", "in_scope": False},
        ]
        assert in_scope_distribution(qas) == {"dynamodb": 2}

    def test_missing_in_scope_key_defaults_to_in_scope(self) -> None:
        """The field was added after assignments without it already existed."""
        qas = [{"query_id": "1", "assigned_engine": "dynamodb"}]
        assert in_scope_distribution(qas) == {"dynamodb": 1}

    def test_empty_input_gives_empty_distribution(self) -> None:
        assert in_scope_distribution([]) == {}


class TestEliminatedEngines:
    def test_no_change_eliminates_nothing(self) -> None:
        before = [_qa("dynamodb"), _qa("documentdb")]
        assert eliminated_engines(before, before) == set()

    def test_an_engine_fully_moved_away_is_eliminated(self) -> None:
        before = [_qa("dynamodb"), _qa("documentdb")]
        after = [_qa("dynamodb"), _qa("dynamodb")]
        assert eliminated_engines(before, after) == {"documentdb"}

    def test_an_engine_left_with_only_out_of_scope_queries_is_eliminated(self) -> None:
        """The #235 bug: an engine still owns a query after reality check, but it
        is out of scope -- the rule still calls this engine eliminated, which the
        handler's and the UI's old ANY-query rule got wrong."""
        before = [_qa("dynamodb"), _qa("documentdb")]
        after = [_qa("dynamodb"), _qa("dynamodb"), _qa("documentdb", in_scope=False)]
        assert eliminated_engines(before, after) == {"documentdb"}
        # The engine still has a query -- just not an in-scope one.
        assert in_scope_distribution(after).get("documentdb") is None

    def test_an_engine_that_keeps_an_in_scope_query_is_not_eliminated(self) -> None:
        before = [_qa("dynamodb"), _qa("documentdb")]
        after = [_qa("dynamodb"), _qa("documentdb")]
        assert eliminated_engines(before, after) == set()

    def test_an_engine_never_given_an_in_scope_query_is_never_reported(self) -> None:
        """An engine that only ever had out-of-scope queries was never part of
        the active architecture, whatever happens to it afterwards."""
        before = [_qa("dynamodb"), _qa("documentdb", in_scope=False)]
        after = [_qa("dynamodb")]
        assert eliminated_engines(before, after) == set()
