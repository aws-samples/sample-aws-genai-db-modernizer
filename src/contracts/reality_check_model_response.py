"""Reality Check Model Response Contract

The one shape a model's answer to the reality check's consolidation review
takes, in both LLM modes (#628):

- headless (Claude Code) writes it to ``llm_responses/reality_check.json``;
- the Bedrock consolidation validator asks for it as Strands structured output
  (and parses the same shape from a plain-text answer).

Both modes validate the answer with :func:`parse_reality_check_response` and
then apply it through ``reality_check_handler.apply_reality_check_llm_output``,
so the same answer reaches the same result whichever mode produced it.

``consolidation_corrections`` holds model flags
(:class:`~src.contracts.model_flag.ModelFlag`): each entry advertises the flag
schema, but is received as a plain object and validated on its own
(``parse_model_flags``), so one bad entry is dropped and counted while the valid
flags beside it are kept. ``executive_summary`` is the reality check's own
CTO-facing narrative; the headless response carries it, the Bedrock validator's
per-consolidation answer does not (the Bedrock summary is a separate call).

``extra="forbid"``: a response with any other top-level key is rejected whole.

Version History:
- 1.0 (2026-10-14, #628): Initial version. Replaces ``ModelFlagBatch``
  (``{"flags": [...]}``), the Bedrock-only wrapper.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .executive_summary_response import MIN_SUMMARY_CHARS
from .model_flag import RoutingFlagEntry


class RealityCheckModelResponse(BaseModel):
    """A model's answer to the consolidation review (both LLM modes)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    consolidation_corrections: list[RoutingFlagEntry] = Field(
        default_factory=list,
        description="Model flags (ModelFlag); empty when every moved query fits its target",
    )
    executive_summary: str | None = Field(
        default=None,
        min_length=MIN_SUMMARY_CHARS,
        description="The reality check's CTO-facing summary (untrusted text); None when not given",
    )


def parse_reality_check_response(raw: object) -> RealityCheckModelResponse:
    """Validate a raw answer (a dict, or an already-built response) against the contract.

    Raises ``pydantic.ValidationError`` when the answer is not the contract's
    shape; the flag entries inside it are validated later, one at a time.
    """
    if isinstance(raw, RealityCheckModelResponse):
        return raw
    return RealityCheckModelResponse.model_validate(raw)
