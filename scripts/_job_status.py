"""CLI glue for ``scripts/run_*.py`` around the shared job-status primitives
in ``src.shared.job_status`` (#517).

The actual marker/review logic lives in ``src.shared.job_status`` so
``LocalOrchestrator`` (``src/orchestrator/local_orchestrator.py``) can clear
a script-driven failure marker too, without a ``src/`` module importing
from ``scripts/`` (#517 round-1 review, I3). This module re-exports those
functions for every script's convenience, plus ``run_main``: the one
catch-all every ``scripts/run_*.py`` entry point's ``if __name__ ==
"__main__":`` guard used to repeat (#517 round-1 review).
"""

from __future__ import annotations

from collections.abc import Callable

from src.shared.job_status import (  # noqa: F401
    ANY_ENGINE,
    clear_failure_marker,
    mark_assignment_reviewed,
    record_unhandled_failure,
    write_failure_marker,
)

# (store, db_name, job_id, phase, engine, require_existing_job) once known,
# else None. require_existing_job is almost always True (#517 round-1
# review I2) -- run_collect.py's own phase is the one exception, since the
# job it is creating has no collector/output.json yet by definition.
JobCtx = Callable[[], "tuple | None"]


def run_main(main_fn: Callable[[], None], job_ctx: JobCtx) -> None:
    """Run ``main_fn()``; if anything other than ``SystemExit`` escapes
    (``SystemExit`` means ``_error()`` already handled and recorded it, if
    there was anything to record), record it against the job ``job_ctx()``
    describes via ``src.shared.job_status.record_unhandled_failure``.

    ``job_ctx`` is a zero-arg callable returning
    ``(store, db_name, job_id, phase, engine, require_existing_job)`` or
    ``None`` when job/db aren't known yet (e.g. collect itself failed
    before creating anything) -- called lazily, at the moment of the
    exception, so it reflects whatever context the script had set up by
    then.
    """
    try:
        main_fn()
    except SystemExit:
        raise
    except BaseException as exc:
        record_unhandled_failure(job_ctx(), exc)
        raise
