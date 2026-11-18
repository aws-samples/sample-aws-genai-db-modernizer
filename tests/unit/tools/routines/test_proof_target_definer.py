# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How a disposable MySQL side creates an original whose ``DEFINER`` names another account
(#2426): through ``load_as_definer`` with the account its own clause names, with a note
when the routine runs as its definer. The engine is faked, so CI runs these."""

from __future__ import annotations

import pytest

from src.tools.database import disposable_engine as de
from src.tools.routines.proof_targets import (
    DEFINER_NOTE,
    CreateRefused,
    DefinerRefused,
    DisposableEngineTarget,
    named_definer,
    runs_as_definer,
)


def _target(monkeypatch, engine="mysql", made=True, refuse=None, own_engine=True):
    calls: list[tuple] = []

    def load(handle, ddl, rows, routines):
        calls.append(("load", routines[0].create_sql))

    def load_as_definer(handle, routine, user, host=None):
        calls.append(("definer", routine.create_sql, user, host))
        if refuse:
            raise de.DefinerRefused(refuse)
        return made

    monkeypatch.setattr(de, "load", load)
    monkeypatch.setattr(de, "load_as_definer", load_as_definer)
    monkeypatch.setattr(de, "makes_definers", lambda handle: own_engine)
    target = object.__new__(DisposableEngineTarget)
    target._handle = None
    target.engine = engine
    target.namespace = de.NAMESPACE[engine]
    target._schemas = ()
    target.routine_notes = {}
    return target, calls


def _proc(definer="DEFINER=`wp_user`@`localhost` ", security=""):
    return de.SourceRoutine(
        "refresh", "procedure",
        f"CREATE {definer}PROCEDURE refresh(IN p INT) {security}"
        "MODIFIES SQL DATA BEGIN UPDATE posts SET n = p WHERE id = p; END",
    )  # fmt: skip


def test_a_definer_the_engine_lacks_is_loaded_as_that_account_with_a_note(monkeypatch):
    target, calls = _target(monkeypatch)
    routine = _proc()
    assert target.create(routine) is None
    assert calls == [("definer", routine.create_sql, "wp_user", "localhost")]
    assert target.routine_notes == {routine.create_sql: DEFINER_NOTE}
    assert "privileges" in DEFINER_NOTE and "outside that database" in DEFINER_NOTE


@pytest.mark.parametrize(
    ("clause", "account"),
    [("DEFINER=`wp_user`@`localhost` ", ("wp_user", "localhost")),
     ("DEFINER='app'@'10.0.0.%' ", ("app", "10.0.0.%")),
     ("DEFINER=admin@'%' ", ("admin", "%")),
     ("DEFINER=`admin` ", ("admin", None))],
)  # fmt: skip
def test_the_account_comes_from_the_texts_own_clause(clause, account):
    assert named_definer(_proc(clause)) == account


@pytest.mark.parametrize("clause", ["DEFINER=CURRENT_USER ", "DEFINER=CURRENT_USER() ", ""])
def test_a_current_user_definer_or_none_loads_as_the_runner(monkeypatch, clause):
    target, calls = _target(monkeypatch)
    routine = _proc(clause)
    assert target.create(routine) is None
    assert calls == [("load", routine.create_sql)]
    assert target.routine_notes == {}


def test_sql_security_invoker_is_loaded_as_its_definer_with_no_note(monkeypatch):
    target, calls = _target(monkeypatch)
    routine = _proc(security="SQL SECURITY INVOKER ")
    assert target.create(routine) is None
    assert calls[0][0] == "definer"
    assert target.routine_notes == {}
    assert runs_as_definer(_proc(security="SQL SECURITY DEFINER ")) is True
    assert runs_as_definer(routine) is False


def test_a_trigger_runs_as_its_definer():
    trigger = de.SourceRoutine("bump", "trigger", "CREATE DEFINER=`admin`@`%` TRIGGER bump "
                               "BEFORE UPDATE ON t FOR EACH ROW SET NEW.n = 1")  # fmt: skip
    assert runs_as_definer(trigger) is True
    assert named_definer(trigger) == ("admin", "%")


def test_the_runners_own_account_gets_no_note(monkeypatch):
    target, _ = _target(monkeypatch, made=False)
    assert target.create(_proc("DEFINER=`routine_runner`@`localhost` ")) is None
    assert target.routine_notes == {}


def test_an_unsafe_definer_is_refused_before_anything_loads(monkeypatch):
    target, _ = _target(monkeypatch, refuse="its DEFINER names an account the server keeps")
    with pytest.raises(DefinerRefused, match="keeps") as caught:
        target.create(_proc("DEFINER=`root`@`localhost` "))
    assert isinstance(caught.value, CreateRefused)
    assert caught.value.reason_code == "definer_refused"
    assert target.routine_notes == {}


def test_postgresql_never_loads_as_a_definer(monkeypatch):
    target, calls = _target(monkeypatch, engine="postgresql")
    text = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$"
    assert target.create(de.SourceRoutine("f", "function", text)) is None
    assert calls == [("load", text)]


def test_a_target_that_only_borrows_the_handle_never_makes_a_definer(monkeypatch):
    """Review of #2433: the Aurora test cluster's target inherits ``create``; on a handle
    that is not this module's disposable MySQL engine every routine loads as it did."""
    target, calls = _target(monkeypatch, own_engine=False)
    routine = _proc()
    assert target.create(routine) is None
    assert calls == [("load", routine.create_sql)]
    assert target.routine_notes == {}
