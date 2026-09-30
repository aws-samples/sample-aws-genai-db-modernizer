"""Tests for the code-object inventory carried into the Aurora agent input.

Both Aurora contracts declare an ``AppLayerNote`` whose ``feature`` is documented
as "e.g. 'trigger', 'sequence', 'stored_procedure'" and whose ``source_object``
names the object. All five collectors gather views, procedures and triggers, but
the projection iterated only ``database_schema.tables``, so every one was dropped
and the agent was asked to name objects it had never been shown.

The inventory closes that gap. It deliberately carries identity only -- the tests
below pin the exclusion of ``definition``, because converting procedural code is
out of scope and shipping bodies would put source code in the agent input for no
reachable purpose.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.contracts.schema_design_input import AgentCodeObject, _project_code_objects


class _Obj:
    """Minimal stand-in so these tests pin projection behaviour, not contract shape."""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _schema(
    views: list[Any] | None = None,
    procedures: list[Any] | None = None,
    triggers: list[Any] | None = None,
) -> Any:
    return _Obj(views=views, procedures=procedures, triggers=triggers)


def _view(name: str = "v_active", **kw: Any) -> Any:
    return _Obj(
        view_name=name,
        schema_name=kw.get("schema_name", "app"),
        referenced_tables=kw.get("referenced_tables", ["app.orders"]),
        definition=kw.get("definition", "SELECT * FROM app.orders WHERE active"),
    )


def _procedure(name: str = "sp_settle", kind: str = "procedure", **kw: Any) -> Any:
    return _Obj(
        procedure_name=name,
        schema_name=kw.get("schema_name", "app"),
        procedure_type=_Obj(value=kind),
        referenced_tables=kw.get("referenced_tables", ["app.orders"]),
        definition=kw.get("definition", "BEGIN UPDATE app.orders SET x = 1; END;"),
    )


def _trigger(name: str = "trg_audit", **kw: Any) -> Any:
    return _Obj(
        trigger_name=name,
        schema_name=kw.get("schema_name", "app"),
        table_id=kw.get("table_id", "app.orders"),
        definition=kw.get("definition", "BEGIN INSERT INTO audit VALUES (:new.id); END;"),
    )


# ---------------------------------------------------------------------------
# Identity is carried
# ---------------------------------------------------------------------------


def test_all_three_object_kinds_are_inventoried() -> None:
    got = _project_code_objects(
        _schema(views=[_view()], procedures=[_procedure()], triggers=[_trigger()])
    )

    assert got is not None
    assert [o.object_type for o in got] == ["view", "procedure", "trigger"]
    assert [o.object_name for o in got] == ["v_active", "sp_settle", "trg_audit"]


def test_function_is_distinguished_from_procedure() -> None:
    """The handoff differs: a function is usually inlined, a procedure rewritten."""
    got = _project_code_objects(
        _schema(
            procedures=[
                _procedure("sp_settle", "procedure"),
                _procedure("fn_tax", "function"),
            ]
        )
    )

    assert got is not None
    assert {o.object_name: o.object_type for o in got} == {
        "sp_settle": "procedure",
        "fn_tax": "function",
    }


def test_referenced_tables_survive_for_views_and_procedures() -> None:
    """These are what a recommendation is based on, absent the body."""
    got = _project_code_objects(
        _schema(
            views=[_view(referenced_tables=["app.orders", "app.customers"])],
            procedures=[_procedure(referenced_tables=["app.ledger"])],
        )
    )

    assert got is not None
    assert got[0].referenced_tables == ["app.orders", "app.customers"]
    assert got[1].referenced_tables == ["app.ledger"]


def test_trigger_records_the_table_it_fires_on() -> None:
    got = _project_code_objects(_schema(triggers=[_trigger(table_id="app.invoices")]))

    assert got is not None
    assert got[0].attached_table == "app.invoices"
    assert got[0].referenced_tables is None


# ---------------------------------------------------------------------------
# Bodies are not
# ---------------------------------------------------------------------------


def test_no_definition_reaches_the_inventory() -> None:
    """Pinned deliberately: converting procedural code is out of scope.

    A body in the agent input would be source code shipped for no reachable
    purpose, and would inflate a prompt that is already compacted elsewhere.
    """
    got = _project_code_objects(
        _schema(views=[_view()], procedures=[_procedure()], triggers=[_trigger()])
    )

    assert got is not None
    assert "definition" not in AgentCodeObject.model_fields
    for obj in got:
        dumped = obj.model_dump(mode="json")
        assert "definition" not in dumped
        assert not any("BEGIN" in str(v) or "SELECT" in str(v) for v in dumped.values())


# ---------------------------------------------------------------------------
# Absence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema",
    [
        _schema(),
        _schema(views=[], procedures=[], triggers=[]),
    ],
    ids=["all_none", "all_empty"],
)
def test_no_code_objects_yields_none_not_empty_list(schema: Any) -> None:
    """None and [] must stay distinguishable in the serialized input.

    "The source reported no procedural objects" and "we did not look" lead to
    different notes, so the projection does not collapse one into the other.
    """
    assert _project_code_objects(schema) is None


def test_partial_sources_do_not_fail() -> None:
    """MySQL reports triggers while a bare source may report none of the three."""
    got = _project_code_objects(_schema(triggers=[_trigger()]))

    assert got is not None
    assert len(got) == 1
    assert got[0].object_type == "trigger"


def test_missing_procedure_type_defaults_to_procedure() -> None:
    """Defensive: the field is required on the contract but arrives from a CLI parse."""
    proc = _procedure()
    proc.procedure_type = None

    got = _project_code_objects(_schema(procedures=[proc]))

    assert got is not None
    assert got[0].object_type == "procedure"
