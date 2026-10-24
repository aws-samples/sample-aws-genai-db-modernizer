"""The Strands Bedrock model every role builds (see ``src/shared/models.py``).

Strands answers a structured-output turn that ended without the output tool by
retrying with a forced ``tool_choice`` and dropping ``thinking`` from the
request. That retry is not valid on every family, so this subclass adjusts it
per :class:`~src.shared.models.ModelProfile`:

* ``accepted``: unchanged (today's 4.x behaviour);
* ``needs_thinking_disabled``: the forced turn sends ``thinking: {type: "disabled"}``;
* ``rejected``: the forced choice goes out as ``auto``, with Strands' own
  "use the tool" instruction still in the conversation.

With prompt caching on (``RoleSpec.prompt_cache``, #626) only the stable prefix
is cached: the tool definitions and the system prompt. Strands' automatic cache
point on the last user turn is not placed (see :meth:`_inject_cache_point`).

Every call's token usage (input, output, cache read, cache write) is logged and
added up in :attr:`RoleBedrockModel.usage`, so a schema-design run can record
what it cost in its design trace.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from strands.models.bedrock import BedrockModel
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolChoice, ToolChoiceAuto, ToolChoiceAutoDict, ToolSpec

from src.shared.bedrock_guardrail import (
    GuardrailIntervened,
    blocked_by_guardrail,
    guard_messages,
)
from src.shared.model_usage import record_usage
from src.shared.models import ModelProfile

logger = logging.getLogger(__name__)

_FORCED_KEYS = ("any", "tool")

#: Private ``BedrockModel`` hooks this subclass overrides. Strands has no public
#: switch for them, so a release that renames one would silently bring back the
#: behaviour the override removes (e.g. a paid cache write on every user turn).
#: Fail at import instead (#626).
_OVERRIDDEN_HOOKS = (
    "_inject_cache_point",
    "_get_additional_request_fields",
    "_format_bedrock_messages",
)
_missing = [name for name in _OVERRIDDEN_HOOKS if not callable(getattr(BedrockModel, name, None))]
if _missing:
    raise ImportError(
        "strands BedrockModel no longer has "
        + ", ".join(_missing)
        + "; RoleBedrockModel overrides them. Pin strands-agents to a release that has "
        "them (see src/atx_orchestrator/requirements.txt) or update RoleBedrockModel."
    )

#: Bedrock Converse usage fields, as Strands reports them.
USAGE_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")


def is_forced(tool_choice: ToolChoice | None) -> bool:
    """True for ``any`` or a named ``tool`` choice (the forced kinds)."""
    return tool_choice is not None and any(key in tool_choice for key in _FORCED_KEYS)


class RoleBedrockModel(BedrockModel):
    """A ``BedrockModel`` whose forced ``tool_choice`` follows the model family."""

    def __init__(self, *, profile: ModelProfile, phase: str = "model", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.profile = profile
        #: The phase named in a :class:`GuardrailIntervened` error (the role's name).
        self.phase = phase
        #: Token totals over every call this model made, plus the call count.
        self.usage: dict[str, int] = {"calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)}

    def _record_usage(self, usage: Mapping[str, Any]) -> None:
        self.usage["calls"] += 1
        for field in USAGE_FIELDS:
            self.usage[field] += int(usage.get(field) or 0)
        record_usage(str(self.config.get("model_id")), usage)
        logger.info(
            "model=<%s> | usage input=%s output=%s cache_read=%s cache_write=%s",
            self.config.get("model_id"),
            *(usage.get(field, 0) for field in USAGE_FIELDS),
        )

    def safe_tool_choice(self, tool_choice: ToolChoice | None) -> ToolChoice | None:
        """``auto`` in place of a forced choice when the family rejects it."""
        if self.profile.forced_tool_choice == "rejected" and is_forced(tool_choice):
            logger.info("Forced tool_choice not supported by %s; sending auto", self.profile.family)
            auto: ToolChoiceAutoDict = {"auto": ToolChoiceAuto()}
            return auto
        return tool_choice

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        *,
        tool_choice: ToolChoice | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamEvent, None]:
        stop_reason: str | None = None
        guardrail_trace: Mapping[str, Any] | None = None
        async for event in super().stream(
            messages,
            tool_specs,
            system_prompt,
            tool_choice=self.safe_tool_choice(tool_choice),
            **kwargs,
        ):
            if isinstance(event, dict):
                metadata: Mapping[str, Any] = event.get("metadata") or {}  # type: ignore[assignment]
                if metadata.get("usage"):
                    self._record_usage(metadata["usage"])
                trace: Mapping[str, Any] = metadata.get("trace") or {}
                if trace.get("guardrail"):
                    guardrail_trace = trace["guardrail"]
                if "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
            yield event
        # Only a guardrail's block ends here; a masking-only intervention
        # (e.g. PII set to anonymize) is a normal reply.
        if blocked_by_guardrail(stop_reason, guardrail_trace):
            raise GuardrailIntervened(self.phase)

    def _format_bedrock_messages(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """Strands' messages, with customer-data blocks marked as guard content when a
        guardrail is attached (see :func:`~src.shared.bedrock_guardrail.split_guard_content`)."""
        messages: list[dict[str, Any]] = super()._format_bedrock_messages(*args, **kwargs)
        if self.config.get("guardrail_id"):
            return guard_messages(messages)
        return messages

    def _inject_cache_point(
        self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any
    ) -> None:
        """Cache the stable prefix only: no cache point on the conversation.

        Strands' ``cache_config`` also marks the last user turn. A schema-design
        prompt or a PE review input is unique per call, so a write there costs
        the cache-write premium on content no later request reads. Any cache
        point a caller placed in the messages is left as it is.
        """
        return None

    def _get_additional_request_fields(self, tool_choice: ToolChoice | None) -> dict[str, Any]:
        fields: dict[str, Any] = super()._get_additional_request_fields(tool_choice)
        if self.profile.forced_tool_choice == "needs_thinking_disabled" and is_forced(tool_choice):
            extra = dict(fields.get("additionalModelRequestFields", {}))
            extra["thinking"] = {"type": "disabled"}
            return {"additionalModelRequestFields": extra}
        return fields
