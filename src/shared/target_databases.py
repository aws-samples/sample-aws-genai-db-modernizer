"""The one engine-scope setting, ``target_databases`` (#1328, decision 1 on #1181).

A customer may limit the engines a job considers. The setting has one name and one
validation everywhere it is given: the Assessments API's ``target_databases``,
``scripts/run_assessment.py --target-databases`` (and ``/modernize``, which passes
it through) and the orchestrator's ``start_job`` config. Routing reads the result
as triage's ``engine_scope`` (ADR-031 Decision 2 Amendment 14).

- Ids are engine catalog ids, compared lower-cased with surrounding blanks removed;
  an id given twice counts once.
- Only active engines are accepted (the engines triage can analyse). A legacy id
  (the generic ``aurora``) names the ids that replace it; a future placeholder
  (``neptune``, ``keyspaces``) is refused as not available yet.
- An empty list is refused: a scope names at least one engine. Leaving the setting
  out (``None``) means no scope, every engine, as before.

Pure functions over the catalog: ``src.shared`` imports neither ``src.agents`` nor
``src.report`` (ADR-032 Decision 3).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from src.shared import catalog
from src.shared.engine_names import display_engine

SETTING = "target_databases"


class TargetDatabasesError(ValueError):
    """A ``target_databases`` value that is empty or names an engine routing cannot use."""


def _active() -> frozenset[str]:
    return catalog.engine_ids(status=catalog.ACTIVE)


def normalize_target_databases(
    values: Iterable[object] | None, *, setting: str = SETTING
) -> list[str] | None:
    """The scope as sorted active engine ids, or ``None`` when the setting is left out.

    ``setting`` names the input in the message (``target_databases``,
    ``--target-databases``). Raises :class:`TargetDatabasesError` for an empty list,
    a value that is not a string, or an id that is legacy, a future placeholder or
    not an engine at all, naming the ids it accepts.
    """
    if values is None:
        return None
    if isinstance(values, str):
        raise TargetDatabasesError(f"{setting} must be a list of engine ids, not one string")
    ids: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TargetDatabasesError(f"{setting} lists {value!r}, which is not an engine id")
        engine = value.strip().lower()
        if engine and engine not in ids:
            ids.append(engine)
    active = _active()
    choose = ", ".join(sorted(active))
    if not ids:
        raise TargetDatabasesError(
            f"{setting} is empty: name at least one engine ({choose}), or leave it out "
            "to consider every engine"
        )
    for legacy in sorted(set(ids) & catalog.engine_ids(status="legacy")):
        successors = sorted(e for e in active if e.startswith(f"{legacy}_"))
        raise TargetDatabasesError(
            f"{setting}: the engine id '{legacy}' is legacy and not accepted"
            + (f"; use {' or '.join(successors)}" if successors else f"; choose from {choose}")
        )
    future = sorted(set(ids) & catalog.engine_ids(status="future"))
    if future:
        names = ", ".join(f"{display_engine(e)} ({e})" for e in future)
        raise TargetDatabasesError(
            f"{setting}: {names} is not available yet (a planned engine no analysis "
            f"covers); choose from {choose}"
        )
    unknown = [e for e in ids if e not in active]
    if unknown:
        raise TargetDatabasesError(
            f"{setting}: unknown engine id(s) {', '.join(unknown)}; choose from {choose}"
        )
    return sorted(ids)


def parse_target_databases_option(text: str | None) -> list[str] | None:
    """``--target-databases dynamodb,aurora_postgresql`` as a scope (``None`` when not given)."""
    if text is None:
        return None
    return normalize_target_databases(text.split(","), setting="--target-databases")


def engine_scope_statement(
    engines: Iterable[str], escape: Callable[[str], str] | None = None
) -> str:
    """ "Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)".

    ``engines`` is ``facts.engine_scope``, already in display order. The one wording
    every deliverable states for a job the customer scoped; the UI formats the same.
    ``escape`` formats each engine name for the deliverable (a loaded ``report.json``
    may carry any string there, and ``display_engine`` passes an unknown id through).
    """
    fmt = escape or (lambda name: name)
    names = ", ".join(fmt(display_engine(e)) for e in engines)
    return f"Engines in scope: {names} (customer's choice)"


def no_engine_in_scope_statement(count: int, total: int) -> str:
    """ "42 of 1,696 queries have no engine in the customer's engine scope; a new assessment
    whose scope adds an engine can serve them".

    ``count`` and ``total`` are the facts' ``no_engine_in_scope_query_count`` and
    ``query_total_count`` (the gate's from ``gate_block_facts``): the one wording the gate
    block, the final summary and the deliverables index state (#1574).
    """
    one = count == 1
    return (
        f"{count:,} of {total:,} {'query' if total == 1 else 'queries'} "
        f"{'has' if one else 'have'} no engine in the customer's engine scope; a new "
        f"assessment whose scope adds an engine can serve {'it' if one else 'them'}"
    )


def engine_scope_display_order(engines: Iterable[str]) -> list[str]:
    """The scope's ids in the order the statement names them: by display name."""
    return sorted(set(engines), key=lambda e: (display_engine(e), e))
