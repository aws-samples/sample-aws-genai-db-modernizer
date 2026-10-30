# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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
- 1.1 (2026-10-17, #1006): ``contract_version`` is a MAJOR.MINOR string, not
  ``Literal["1.0"]`` (ADR-008 Decision 1). An answer saying ``1.0`` still validates.
  The pattern checks the form only: this is a model's answer, which may echo any
  version, so no reader checks its MAJOR (model I/O, ADR-008 "Reading Older
  Artifacts").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# A summary shorter than this is not a summary (the old Bedrock-only gate was
# "more than 20 characters"); both modes now apply it through the contract.
MIN_SUMMARY_CHARS = 21


class ExecutiveSummaryResponse(BaseModel):
    """A model's synthesis executive summary (both LLM modes)."""

    model_config = ConfigDict(extra="forbid")

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )
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
