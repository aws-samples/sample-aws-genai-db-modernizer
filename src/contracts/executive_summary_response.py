"""Executive Summary Response Contract

The one shape a model's synthesis executive summary takes, in both LLM modes
(#628):

- headless (Claude Code) writes it to ``synthesis/[v<N>/]llm_response.json``;
- the Bedrock path asks for it as Strands structured output (and reads a
  plain-text answer as the summary text).

Both modes validate it with :func:`parse_executive_summary_response` and apply
it through ``synthesis_handler.apply_synthesis_llm_output``, which runs the same
post-checks (grounding, internal names, wave order, risk counts) either way.

``extra="forbid"``: a response with any other key is rejected whole.

Version History:
- 1.0 (2026-10-14, #628): Initial version.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# A summary shorter than this is not a summary (the old Bedrock-only gate was
# "more than 20 characters"); both modes now apply it through the contract.
MIN_SUMMARY_CHARS = 21


class ExecutiveSummaryResponse(BaseModel):
    """A model's synthesis executive summary (both LLM modes)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    executive_summary: str = Field(
        ...,
        min_length=MIN_SUMMARY_CHARS,
        description="The CTO-facing executive summary, plain prose (untrusted text)",
    )


def parse_executive_summary_response(raw: object) -> ExecutiveSummaryResponse:
    """Validate a raw answer (a dict, or an already-built response) against the contract.

    Raises ``pydantic.ValidationError`` when the answer is not the contract's shape.
    """
    if isinstance(raw, ExecutiveSummaryResponse):
        return raw
    return ExecutiveSummaryResponse.model_validate(raw)
