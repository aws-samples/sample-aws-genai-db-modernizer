"""The model entry points an ``assess()`` run with ``deterministic=False`` reaches, and
what tests put in their place (tests never call a model).

- the reality check's review: accept every consolidation, no model summary;
- schema design (``run_schema_design_auto``, which ``run_schema_design_core`` calls):
  on the wordpress sample, the designed wordpress job's designs
  (``tests/e2e/designed_run.py``); on any other database, a skipped placeholder, as a
  designer writes when it has nothing to design;
- synthesis's executive summary: no model answer, so the deterministic one stays.

A phase that adds a model call adds its entry point to :data:`MODEL_ENTRY_POINTS`. The
Bedrock guard in ``tests/conftest.py`` still refuses any call this list misses.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

REALITY_CHECK = "src.agents.referee.reality_check_handler"
SYNTHESIS = "src.agents.referee.synthesis_handler"
DESIGNER = "src.agents.schema_design.handler.run_schema_design_auto"


def write_fake_design(
    job_id: str, database_name: str, target_type: str, store: Any, assignment_version: int
) -> None:
    from src.agents.schema_design.output_contract import write_schema_output
    from src.contracts.skipped_placeholders import SkippedSchemaOutput
    from tests.e2e.designed_run import (
        write_aurora_mysql_design,
        write_dynamodb_design,
        write_elasticache_design,
    )

    writers: dict[str, Callable[..., Any]] = {
        "dynamodb": write_dynamodb_design,
        "elasticache": write_elasticache_design,
        "aurora_mysql": write_aurora_mysql_design,
    }
    if database_name == "wordpress" and target_type in writers:
        writers[target_type](store, database_name, job_id, assignment_version)
        return
    placeholder = SkippedSchemaOutput(
        target_type=target_type,
        reason="No design in this test",
        assignment_version=assignment_version,
    )
    key = f"{database_name}/{job_id}/schema-{target_type}/v{assignment_version}/schema_output.json"
    write_schema_output(store, key, placeholder.model_dump(mode="json"), target_type)


def fake_designer(**kw: Any) -> Any:
    # ``run_schema_design_core`` passes every argument by name.
    write_fake_design(
        kw["job_id"], kw["database_name"], kw["target_type"], kw["store"], kw["assignment_version"]
    )


#: ``(target, value)``: a callable value is the patch's side effect, any other value
#: its return value.
MODEL_ENTRY_POINTS: tuple[tuple[str, Any], ...] = (
    (f"{REALITY_CHECK}.require_valid_guardrail_config", None),
    (f"{REALITY_CHECK}.validate_consolidations", []),
    (f"{REALITY_CHECK}._generate_executive_summary", None),
    (DESIGNER, fake_designer),
    (f"{SYNTHESIS}.require_valid_guardrail_config", None),
    (f"{SYNTHESIS}.generate_executive_summary", None),
)


@contextmanager
def models_patched() -> Iterator[dict[str, MagicMock]]:
    """Every entry point of :data:`MODEL_ENTRY_POINTS` patched; the mocks, by target."""
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        for target, value in MODEL_ENTRY_POINTS:
            mock = (
                MagicMock(side_effect=value) if callable(value) else MagicMock(return_value=value)
            )
            mocks[target] = stack.enter_context(patch(target, mock))
        yield mocks
