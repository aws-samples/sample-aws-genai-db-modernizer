# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Check a model's routine repair on the rebuilt exact text, then prove it (#1941).

The repair loop (``src.agents.routine_repair.loop``) gives each candidate that passed the
redacted screen (``repair_screen``) to :meth:`RepairVerifier.verify`. Everything here
runs on the exact text, never on the redacted candidate (the review notes on #2369):

1. **Rebuild** (:func:`~src.tools.routines.repair_rebuild.rebuild_exact`): the routine's
   exact ``CREATE`` from the job's routine sources, its literals put back token by token
   into the candidate. A routine whose literals cannot be put back by position is not
   repaired at all (:meth:`RepairVerifier.blocker`, asked before the model is).
2. **Screen again**: the repair rules (``repair_screen.refusal_of``, the proof's own
   ``create_refusal`` first) comparing the rebuilt text with the exact original.
3. **Check again**: every routine check of the routine-migration phase
   (``migration.run_routine_migration_deterministic``) on the job's routine sources with
   the rebuilt texts in place of the originals. A rule the rebuilt text fails, or a
   dependency it names that the collection lacks, refuses it.
4. **Prove**: the side-by-side comparison (``comparison.compare_routines``) of the
   original exact text with the rebuilt one, on the same synthetic rows and calls. Only
   this catches a placeholder the candidate moved or swapped: every one reads ``'?'``.
   It runs only on a proof target: ``local``, two disposable engines of the source's own
   engine and major (``disposable_engine``, no network listener), one for each side; or
   ``test-cluster`` (#2384), the original on a disposable engine of the source's major and
   the rebuilt text on the job's throwaway Aurora test cluster, through the RDS Data API
   (``target_factory``, which the caller opens on an approved, deployed cluster).
   With ``none``, or no engine of that major here, a candidate is *pending proof*.

Step 1 and the test cluster's proof rebuild a ``model_repaired`` routine the same way
(:meth:`RepairVerifier.rebuild`, :func:`rebuild_repairs`, #2384): rebuilt, screened again
and checked again in the caller's process, never read from a saved state.

Each verdict (:class:`RepairCheck`) holds a stage, an outcome, a fixed code and words
that hold no routine text and no literal: the loop feeds them to the model's next
attempt and writes them into the artifact. The rebuilt text is held in memory for the
call only: no artifact, log, printed line or model input gets it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from src.contracts.routine_migration import RoutineMigration, RoutineMigrationContract
from src.contracts.routine_sources import RoutineSource
from src.tools.routines.repair_rebuild import REBUILD_WORDS, RebuildRefused, rebuild_exact
from src.tools.routines.repair_rebuild import rebuild_refusal as _rebuild_refusal

__all__ = [
    "PROOF_CHOICES",
    "ProofChoice",
    "RepairCheck",
    "RepairItem",
    "RepairVerifier",
    "verifier_for_job",
]

ProofChoice = Literal["none", "local", "test-cluster"]
#: Every proof a run may name, the one list the script, the loop and the verifier read
#: (#2384): ``auto`` resolves to ``local`` or ``none`` before the verifier runs, never to
#: ``test-cluster``, which only an attended ``/test-routines`` asks for.
PROOF_CHOICES: tuple[str, ...] = ("auto", "local", "none", "test-cluster")
#: The proofs the verifier runs: every choice but ``auto``.
PROOF_TARGETS: tuple[str, ...] = tuple(c for c in PROOF_CHOICES if c != "auto")
Stage = Literal["rebuild", "rescreen", "recheck", "proof"]
#: ``failed``: the candidate is refused and another attempt may follow; ``pending``: it
#: passed every check but no proof ran (or could compare), so it is not carried;
#: ``proved``: the proof found no difference, so the repair is carried.
CheckOutcome = Literal["failed", "pending", "proved"]
#: A comparison reason that is the candidate's own failure (the carried side would not
#: load or be created, or left its role), not a proof that could not run.
_CANDIDATE_FAULTS = frozenset({"carried_does_not_load", "outside_namespace", "role_changed"})
NO_PROOF_TARGET = "no proof target was given (--proof none), so the side-by-side proof did not run"
NO_TEST_CLUSTER = (
    "no test cluster was opened for the proof (--proof test-cluster), so the side-by-side "
    "proof did not run"
)
#: The reason codes of the routine-migration phase that are not the rebuilt text's.
_NOT_THE_TEXTS = frozenset({"depends_on_needs_attention"})
_WORDS = 300
#: The body states with no body to rebuild on (the repair seam's own, #1933).
_NO_BODY = frozenset({"opted_out", "not_readable", "not_supported"})


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _WORDS else text[: _WORDS - 1] + "…"


@dataclass(frozen=True)
class RepairCheck:
    """What the checks and the proof made of one candidate: never routine text."""

    stage: Stage
    outcome: CheckOutcome
    code: str | None = None
    error: str | None = None
    proof_source: str | None = None
    proof_calls: int = 0


@dataclass(frozen=True)
class RepairItem:
    """One candidate to verify: its routine and the two redacted texts the screen saw."""

    kind: str
    routine_id: str
    seen: str = field(repr=False)
    candidate: str = field(repr=False)

    @property
    def ref(self) -> tuple[str, str]:
        return (self.kind, self.routine_id)


def _major(engine: str, version: str | None) -> str | None:
    found = re.search(r"(\d+)\.(\d+)", version or "")
    if not found:
        return None
    return found.group(1) if engine == "postgresql" else f"{found.group(1)}.{found.group(2)}"


class RepairVerifier:
    """The checks and the proof of the repair loop for one routine-migration artifact.

    ``sources`` gives the job's routine sources each time it is called (the store's
    reader, one chunk at a time); only the exact texts of the routines the loop repairs,
    and of the carried routines they name, are kept, in memory. ``proof`` is the proof
    target (:data:`PROOF_CHOICES`); ``engine_factory(engine, major)`` opens a side of it
    (the disposable engine by default, a test's fake otherwise) and ``engine_available``
    says whether that major is here."""

    def __init__(
        self,
        migration: RoutineMigrationContract,
        collector: Mapping[str, Any],
        sources: Callable[[], Iterable[RoutineSource]],
        *,
        refs: Iterable[tuple[str, str]],
        proof: str = "none",
        engine_factory: Callable[[str, str], Any] | None = None,
        engine_available: Callable[[str, str], bool] | None = None,
        target_factory: Callable[[], Any] | None = None,
        target_words: str | None = None,
        carried_text: Callable[[RoutineMigration, RoutineSource], str | None] | None = None,
    ) -> None:
        if proof not in PROOF_TARGETS:
            raise ValueError(f"unknown proof target {proof!r}")
        self.migration = migration
        self.collector = collector
        self.engine = migration.source_engine
        self.proof = proof
        self._sources = sources
        self._rows: dict[tuple[str, str], RoutineMigration] = {
            (r.kind, r.routine_id): r for r in migration.routines
        }
        self._factory = engine_factory
        self._available = engine_available
        # --proof test-cluster (#2384): the cluster's side, what it is in words, and the text
        # each routine is created with there (the rewrites for the cluster's user).
        self._target = target_factory
        self._target_words = target_words or "the job's Aurora test cluster"
        self._carried = carried_text
        wanted = {ref for ref in refs if ref in self._rows}
        for ref in list(wanted):
            wanted |= self._named(self._rows[ref])
        keys = {self._key(self._rows[ref]): ref for ref in wanted}
        self._exact: dict[tuple[str, str], RoutineSource] = {}
        for source in sources():
            found = keys.get(source.key())
            if found is not None and source.exact:
                self._exact[found] = source

    # ------------------------------------------------------------------ lookups

    @staticmethod
    def _key(row: RoutineMigration) -> tuple[str, str, str]:
        return (row.kind, row.schema_name or "", row.specific_name or "")

    def _named(self, row: RoutineMigration) -> set[tuple[str, str]]:
        """The routines ``row`` names, as artifact refs, transitively."""
        out: set[tuple[str, str]] = set()
        todo = [row]
        while todo:
            current = todo.pop()
            for dep in current.dependencies:
                for ref in dep.routines:
                    key = (ref.kind, ref.routine_id)
                    if key not in out and key in self._rows:
                        out.add(key)
                        todo.append(self._rows[key])
        return out

    def has_exact(self, kind: str, routine_id: str) -> bool:
        return (kind, routine_id) in self._exact

    def blocker(self, kind: str, routine_id: str, seen: str) -> tuple[str, str] | None:
        """Why no repair of the routine can be rebuilt onto its exact text (``(code,
        words)``), whatever the model answers; None when one can. The loop asks this
        before it sends the routine, so such a routine never reaches a model."""
        source = self._exact.get((kind, routine_id))
        if source is None:
            return "no_exact_text", "the job's routine sources hold no exact text for it"
        code = _rebuild_refusal(source.create_statement, seen, self.engine)
        return None if code is None else (code, REBUILD_WORDS[code])

    # ------------------------------------------------------------------ verify

    def verify(self, items: Sequence[RepairItem]) -> dict[tuple[str, str], RepairCheck]:
        """Each candidate's verdict (module docstring), keyed ``(kind, routine_id)``."""
        rebuilt, out = self.rebuild(items)
        out.update(self._prove(rebuilt))
        return out

    def rebuild(
        self, items: Sequence[RepairItem]
    ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], RepairCheck]]:
        """Steps 1 to 3 of the module docstring: each candidate's rebuilt exact text, held
        in memory only, when it was rebuilt, screened again and checked again; else its
        failed check. No proof runs here."""
        out: dict[tuple[str, str], RepairCheck] = {}
        rebuilt: dict[tuple[str, str], str] = {}
        for item in items:
            source = self._exact.get(item.ref)
            if source is None:
                out[item.ref] = RepairCheck("rebuild", "failed", "no_exact_text",
                                            "the job's routine sources hold no exact text for it")  # fmt: skip
                continue
            try:
                text = rebuild_exact(
                    source.create_statement, item.seen, item.candidate, self.engine
                )
            except RebuildRefused as exc:
                out[item.ref] = RepairCheck("rebuild", "failed", exc.code, str(exc))
                continue
            refused = self._screen(item, source, text)
            if refused is not None:
                out[item.ref] = refused
                continue
            rebuilt[item.ref] = text
        for ref, check in self._recheck(rebuilt).items():
            out[ref] = check
            rebuilt.pop(ref, None)
        return rebuilt, out

    def _screen(self, item: RepairItem, source: RoutineSource, text: str) -> RepairCheck | None:
        from src.shared.catalog.routine_checks import routine_check_rules
        from src.tools.routines.repair_screen import RepairedRoutine, hinted_calls, refusal_of

        row = self._rows[item.ref]
        hints = {rule.id: rule.fix_hint for rule in routine_check_rules()}
        routine = RepairedRoutine(
            kind=row.kind,
            schema_name=row.schema_name,
            name=row.name,
            create_text=source.create_statement,
            sql_mode=source.session.sql_mode,
            allowed_calls=hinted_calls(hints.get(r, "") for r in row.reasons),
        )
        refused = refusal_of(routine, text, self.engine)
        if refused is None:
            return None
        code, words = refused
        return RepairCheck("rescreen", "failed", code, f"on the rebuilt exact text, {words}")

    def _recheck(
        self, rebuilt: Mapping[tuple[str, str], str]
    ) -> dict[tuple[str, str], RepairCheck]:
        """Every routine check on the rebuilt texts, run as the routine-migration phase
        runs them, the job's other routines as they are."""
        from src.tools.routines.migration import run_routine_migration_deterministic

        if not rebuilt:
            return {}
        swap = {self._key(self._rows[ref]): text for ref, text in rebuilt.items()}

        def swapped() -> Iterable[RoutineSource]:
            for source in self._sources():
                text = swap.get(source.key())
                yield (
                    source if text is None else source.model_copy(update={"create_statement": text})
                )

        try:
            checked = run_routine_migration_deterministic(
                self.collector, None, swapped(), job_id=self.migration.job_id,
                database_name=self.migration.database_name,
                assignment_version=self.migration.assignment_version,
            )  # fmt: skip
        except Exception as exc:  # noqa: BLE001 -- its message may quote the text
            return {
                ref: RepairCheck("recheck", "failed", "check_failed",
                                 f"the checks could not read the rebuilt text ({type(exc).__name__})")  # fmt: skip
                for ref in rebuilt
            }
        rows: dict[tuple[str, str], RoutineMigration] = {
            (r.kind, r.routine_id): r for r in (checked.routines if checked else [])
        }
        rules = {r.rule_id: r for r in (checked.rules if checked else [])}
        out: dict[tuple[str, str], RepairCheck] = {}
        for ref in rebuilt:
            row = rows.get(ref)
            if row is None:
                out[ref] = RepairCheck("recheck", "failed", "check_failed",
                                       "the checks no longer find the routine in the rebuilt text")  # fmt: skip
                continue
            failed = [r for r in row.reasons if r not in _NOT_THE_TEXTS]
            if not failed:
                continue
            words = "; ".join(f"{r}: {rules[r].reason}" if r in rules else r for r in failed)
            out[ref] = RepairCheck("recheck", "failed", "check_failed",
                                   _short(f"the rebuilt exact text fails {words}"))  # fmt: skip
        return out

    # ------------------------------------------------------------------ prove

    def _side(self, major: str) -> Any:
        if self._factory is not None:
            return self._factory(self.engine, major)
        from src.tools.routines.proof_targets import disposable_target

        return disposable_target(self.engine, major)

    def _here(self, major: str) -> bool:
        if self._available is not None:
            return self._available(self.engine, major)
        from src.tools.database import disposable_engine as de

        return de.available(self.engine, major)

    def _prove(self, rebuilt: Mapping[tuple[str, str], str]) -> dict[tuple[str, str], RepairCheck]:
        if not rebuilt:
            return {}
        if self.proof == "none":
            return {ref: RepairCheck("proof", "pending", "no_proof_target", NO_PROOF_TARGET)
                    for ref in rebuilt}  # fmt: skip
        cluster = self.proof == "test-cluster"
        if cluster and self._target is None:
            return {ref: RepairCheck("proof", "pending", "no_test_cluster", NO_TEST_CLUSTER)
                    for ref in rebuilt}  # fmt: skip
        major = _major(self.engine, self.migration.source_version)
        if major is None or not self._here(major):
            why = (
                f"no disposable {self.engine} {major} engine is here to run the side-by-side "
                "proof on"
                if major
                else "the collection names no source version to run the proof on"
            )
            return {ref: RepairCheck("proof", "pending", "source_engine_unavailable", why)
                    for ref in rebuilt}  # fmt: skip
        from src.tools.database import disposable_engine as de
        from src.tools.routines.comparison import ComparedRoutine, compare_routines
        from src.tools.routines.proof_run import proof_input

        tables = (self.collector.get("database_schema") or {}).get("tables") or []
        dialect: Any = self.engine
        order: dict[tuple[str, str], int] = {
            (r.kind, r.routine_id): r.deploy_order for r in self.migration.routines
        }
        compared: dict[str, ComparedRoutine] = {}
        wanted: dict[str, tuple[str, str]] = {}

        def add(ref: tuple[str, str], carried: str | None = None) -> bool:
            row = self._rows[ref]
            if row.routine_id in compared and carried is None:
                return True
            source = self._exact.get(ref)
            if source is None:
                return False
            deps = sorted(self._named(row), key=lambda k: order.get(k, 0))
            if not all(add(d) for d in deps):
                return False
            inp = proof_input(row, self.engine, self.collector,
                              [d.name for d in row.dependencies if d.kind == "table"])  # fmt: skip
            text: str | None = carried or source.create_statement
            if self._carried is not None:
                # On the test cluster: the text as step 1 would create it for its user.
                text = self._carried(row, source.model_copy(update={"create_statement": text}))
                if text is None:
                    return False
            compared[row.routine_id] = ComparedRoutine(
                inp,
                (de.SourceRoutine(inp.name, row.kind, source.create_statement),),  # type: ignore[arg-type]
                (de.SourceRoutine(inp.name, row.kind, text),),  # type: ignore[arg-type]
                tuple(self._rows[d].routine_id for d in deps),
            )
            return True

        out: dict[tuple[str, str], RepairCheck] = {}
        for ref, text in sorted(rebuilt.items(), key=lambda kv: order.get(kv[0], 0)):
            if not add(ref, text):
                out[ref] = RepairCheck("proof", "pending", "dependency_not_compared",
                                       "a routine it names has no exact text to load on both sides")  # fmt: skip
                continue
            wanted[ref[1]] = ref
        if not wanted:
            return out
        side = f"a disposable {self.engine} {major} engine"
        if cluster:
            side = f"{side} and {self._target_words}"
        routines = sorted(
            compared.values(), key=lambda r: order.get((r.proof.kind, r.routine_id), 0)
        )
        try:
            target: Any = self._target if cluster else self._side(major)
            report = compare_routines(routines, tables, source=self._side(major),
                                      target=target, dialect=dialect)  # fmt: skip
        except Exception as exc:  # noqa: BLE001 -- the proof did not run: nothing is carried
            for ref in wanted.values():
                out[ref] = RepairCheck("proof", "pending", "engine_failed",
                                       f"the side-by-side proof failed to run ({type(exc).__name__})",
                                       proof_source=side)  # fmt: skip
            return out
        results = report.by_id()
        for rid, ref in wanted.items():
            found = results.get(rid)
            if found is None:
                out[ref] = RepairCheck("proof", "pending", "engine_failed",
                                       "the side-by-side proof gave it no result", proof_source=side)  # fmt: skip
                continue
            out[ref] = _verdict(found, side)
        return out


def _verdict(found: Any, side: str) -> RepairCheck:
    """A comparison's result as a :class:`RepairCheck`: its reason and, for a difference,
    each kind of difference and the calls it showed on (never a value)."""
    if found.outcome == "no_differences_found":
        return RepairCheck("proof", "proved", None, _short(found.reason), proof_source=side,
                           proof_calls=found.calls_compared)  # fmt: skip
    if found.outcome == "different" or found.reason_code in _CANDIDATE_FAULTS:
        aspects = sorted({d.aspect for d in found.differences})
        calls = sorted({d.call for d in found.differences if d.call})
        words = f"the side-by-side proof found {found.differences_total} difference(s)"
        if aspects:
            words += f" ({', '.join(aspects)})"
        if calls:
            words += f" on the call(s) {', '.join(calls[:5])}"
        words += f": {found.reason}"
        return RepairCheck("proof", "failed", found.reason_code or "different", _short(words),
                           proof_source=side, proof_calls=found.calls_compared)  # fmt: skip
    return RepairCheck("proof", "pending", found.reason_code or "not_compared",
                       _short(f"the side-by-side proof could not compare it: {found.reason}"),
                       proof_source=side, proof_calls=found.calls_compared)  # fmt: skip


def verifier_for_job(
    store: Any,
    database_name: str,
    job_id: str,
    migration: RoutineMigrationContract,
    *,
    refs: Iterable[tuple[str, str]],
    proof: str = "none",
    **kwargs: Any,
) -> RepairVerifier:
    """A :class:`RepairVerifier` on the job's collection and routine sources (read one
    chunk at a time, each time the checks run again)."""
    from src.agents.collector.routine_sources import read_routine_sources

    collector = store.read_json(f"{database_name}/{job_id}/collector/output.json")
    return RepairVerifier(
        migration,
        collector,
        lambda: read_routine_sources(store, database_name, job_id),
        refs=refs,
        proof=proof,
        **kwargs,
    )


def seen_texts(
    store: Any, database_name: str, job_id: str, migration: RoutineMigrationContract
) -> dict[tuple[str, str], str]:
    """The redacted ``CREATE`` the repair loop gave the model for each routine (the
    collection read for models, #1625), keyed ``(kind, routine_id)``: what a candidate is
    rebuilt against. A routine whose body was not collected has none."""
    from src.agents.collector.routine_sources import collection_includes_bodies
    from src.agents.routine_repair.source_text import redacted_create
    from src.storage.collector_for_models import read_collector_for_models
    from src.tools.routines.migration import collection_routines

    collector = read_collector_for_models(store, database_name, job_id, missing_ok=True)
    if not collection_includes_bodies(collector):
        return {}  # the customer opted out of bodies: nothing to rebuild on (#1933)
    rows = collection_routines(collector)
    out: dict[tuple[str, str], str] = {}
    for row in migration.routines:
        found = rows.get((row.kind, row.routine_id))
        if found is None:
            continue
        status = found.get("body_status")
        if str(getattr(status, "value", status) or "") in _NO_BODY:
            continue
        text = redacted_create(found, migration.source_engine, row.kind)
        if text is not None:
            out[(row.kind, row.routine_id)] = text
    return out


def rebuild_repairs(
    store: Any,
    database_name: str,
    job_id: str,
    migration: RoutineMigrationContract,
    candidates: Mapping[tuple[str, str], str],
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], RepairCheck]]:
    """The exact text of each model-repaired routine of ``candidates`` (its redacted
    candidate, ``routine_repair.json``'s ``repaired_create``), rebuilt from the routines
    file, screened again and checked again in this process (#2384, the #2387 review: a
    saved ``model_repaired`` is never trusted on its own). Returns the rebuilt texts, held in
    memory only, and the failed check of each routine that did not pass."""
    if not candidates:
        return {}, {}
    seen = seen_texts(store, database_name, job_id, migration)
    verifier = verifier_for_job(store, database_name, job_id, migration, refs=list(candidates))
    items, missing = [], {}
    for ref, candidate in candidates.items():
        if ref not in seen:
            missing[ref] = RepairCheck("rebuild", "failed", "no_redacted_text",
                                       "the collection holds no text of it to rebuild the repair on")  # fmt: skip
            continue
        items.append(RepairItem(ref[0], ref[1], seen[ref], candidate))
    rebuilt, failed = verifier.rebuild(items)
    return rebuilt, {**missing, **failed}
