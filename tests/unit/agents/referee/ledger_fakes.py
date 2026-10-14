"""Test helper: a stand-in reality check's engine changes, recorded as Moves (#616).

A test that hand-builds a reality-check result (or fakes ``run_reality_check``)
changes engines without running the real passes. The handler refuses to write a
revision that does not replay from its input plus its Moves, so such a result
carries the ledger this helper builds: one ``settle`` Move per query whose
engine or tie differs between ``before`` and ``after``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from src.agents.referee.move_ledger import MoveLedger, apply, ledger_for
from src.contracts.move_ledger import MovePass, ReasonCode


def recorded_ledger(before: Iterable[Mapping], after: Iterable[Mapping]) -> MoveLedger:
    """A ledger starting from ``before`` with a Move for every change ``after`` shows."""
    before = list(before)
    ledger = ledger_for(before)
    tied_before = {qa["query_id"]: sorted(qa.get("tied_engines") or []) for qa in before}
    for qa in after:
        qid = qa["query_id"]
        tied = sorted(qa.get("tied_engines") or [])
        if ledger.engine_of(qid) == qa["assigned_engine"] and tied_before.get(qid, []) == tied:
            continue
        apply(
            ledger,
            qid,
            qa["assigned_engine"],
            pass_=MovePass.SETTLE,
            reason_code=ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE,
            tied_engines=tied,
        )
    return ledger
