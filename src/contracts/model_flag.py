"""Model Flag Contract

What a model may say about a routing decision (ADR-031 Decision 11, as amended
2026-10-14): "these queries cannot be served on this engine, for this reason".
A flag is evidence, never a decision. It names the engine the queries are on
(the one the model judged), never a destination: the deterministic scorer
treats the flagged engine as unable to serve those queries and re-routes them
to the best remaining engine (``src.agents.referee.model_flags``).

Both LLM modes validate against this one model: the headless
``llm_responses/reality_check.json`` ``consolidation_corrections`` entries and
the Bedrock consolidation validator's structured output (``ModelFlagBatch``).
``extra="forbid"`` rejects any additional key, so a response that tries to name
a target (``target_engine``, ``to_engine``, ``original_engine``) fails
validation instead of being read.

Every consumer validates entries one at a time (:func:`parse_model_flags`): an
invalid entry is dropped and counted, and the valid flags beside it are kept, in
both modes.

Version History:
- 1.0 (2026-10-14, #647, #667): Initial version.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, WithJsonSchema

logger = logging.getLogger(__name__)

# The six modeled target engines (``src.shared.engine_names.ENGINE_DISPLAY_NAMES``
# minus the ``aurora``/``neptune``/``keyspaces`` placeholders). Contracts do not
# import ``src.shared``, so the ids are listed here;
# ``tests/unit/agents/referee/test_models_flag_never_decide.py`` checks the two
# lists agree.
FlaggableEngine = Literal[
    "aurora_mysql",
    "aurora_postgresql",
    "documentdb",
    "dynamodb",
    "elasticache",
    "opensearch",
]

# A flag's explanation is a short note, not an essay (the old free-text reason
# was asked for in at most 15 words).
MAX_EXPLANATION_CHARS = 300


class FlagReason(str, Enum):
    """Why the model judged the flagged engine unable to serve the queries."""

    FULL_SCAN = "full_scan"
    CLIENT_SIDE_JOIN = "client_side_join"
    MISSING_FEATURE = "missing_feature"
    OTHER = "other"


class ModelFlag(BaseModel):
    """One model flag: these queries cannot be served on ``flagged_engine``."""

    model_config = ConfigDict(extra="forbid")

    query_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Query IDs the flag covers, copied exactly from the input",
    )
    flagged_engine: FlaggableEngine = Field(
        ...,
        description=(
            "The engine the queries are on now (the one judged unable to serve them). "
            "Never a destination: where the queries go is the scorer's choice"
        ),
    )
    reason_category: FlagReason = Field(..., description="The kind of capability gap")
    explanation: str = Field(
        ...,
        min_length=1,
        max_length=MAX_EXPLANATION_CHARS,
        description="A short note on the gap, in the model's words (untrusted text)",
    )


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """``schema`` with its ``#/$defs/...`` references inlined (no ``$defs`` left).

    The flag schema is embedded inside another model's schema below, where a
    nested ``$defs`` block would not resolve.
    """
    defs = schema.get("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(defs[ref.removeprefix("#/$defs/")])
            return {k: resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)  # type: ignore[no-any-return]


# One flag entry inside another model's structured output: the model sees the
# ModelFlag schema, the consumer receives a plain object and validates it on its
# own with parse_model_flags (an invalid entry is dropped and counted; the rest
# of the output is kept).
RoutingFlagEntry = Annotated[
    dict[str, Any], WithJsonSchema(_inline_refs(ModelFlag.model_json_schema()))
]


class ModelFlagBatch(BaseModel):
    """The Bedrock validator's structured output.

    Each entry advertises the :class:`ModelFlag` schema to the model, but is
    received as a plain object and validated on its own by
    :func:`parse_model_flags`, so one invalid entry is dropped and counted
    instead of failing the whole batch (the same handling the headless
    response gets).
    """

    flags: list[RoutingFlagEntry] = Field(
        default_factory=list, description="Flags; empty when every query is fine"
    )


def parse_model_flags(raw: Iterable[object]) -> tuple[list[ModelFlag], list[str]]:
    """Validate each raw entry against :class:`ModelFlag`, one at a time.

    Returns the valid flags and one short note per rejected entry (its index and
    the first validation error's location), for the log. A rejected entry is
    never partly read: an entry that names a destination (``target_engine``,
    ``original_engine``, ...) fails ``extra="forbid"`` and is dropped whole.
    """
    flags: list[ModelFlag] = []
    rejected: list[str] = []
    for index, entry in enumerate(raw):
        if isinstance(entry, ModelFlag):
            flags.append(entry)
            continue
        try:
            flags.append(ModelFlag.model_validate(entry))
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "entry"
            rejected.append(f"flag {index}: {where}: {first.get('type', 'invalid')}")
    for note in rejected:
        logger.warning("Rejected a model flag that does not match the contract (%s)", note)
    return flags, rejected
