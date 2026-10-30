# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Strands Bedrock model every role builds (see ``src/shared/models.py``).

Strands answers a structured-output turn that ended without the output tool by
retrying with a forced ``tool_choice`` and dropping ``thinking`` from the
request. That retry is not valid on every family, so this subclass adjusts it
per :class:`~src.shared.models.ModelProfile`:

* ``accepted``: unchanged (today's 4.x behaviour);
* ``needs_thinking_disabled``: the forced turn sends ``thinking: {type: "disabled"}``;
* ``rejected``: the forced choice goes out as ``auto``, with Strands' own
  "use the tool" instruction still in the conversation.

With prompt caching on (``RoleSpec.prompt_cache``, #626) the stable prefix is
cached: the tool definitions and the system prompt. Strands' automatic cache
point on the last user turn is placed only for a multi-turn agent, whose next
turn resends the whole conversation: a schema designer builds its agent on
:meth:`RoleBedrockModel.with_conversation_cache` (#1675). A one-shot call such
as the PE reviewer sends its input once, so it gets no conversation cache point
(see :meth:`_inject_cache_point`).

Every call's token usage (input, output, cache read, cache write) is logged and
added up in :attr:`RoleBedrockModel.usage`, so a schema-design run can record
what it cost in its design trace. A request that asks again for a structured
output (the model answered without the output tool, or the tool's input failed
validation) is logged at INFO with the reason and kept in
:attr:`RoleBedrockModel.structured_output_reasks` (#1675).
"""

from __future__ import annotations

import copy
import functools
import logging
import re
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from strands.models.bedrock import BedrockModel
from strands.tools.structured_output import DEFAULT_STRUCTURED_OUTPUT_PROMPT
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
#: One INFO line per structured-output re-ask (#1675), with no model or customer
#: text: ``scripts/run_assessment.py`` routes it to the run log.
REASK_LOGGER = logging.getLogger(f"{__name__}.reask")

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


#: The tool result Strands' structured-output tool returns when its input fails
#: the output model (``strands.tools.structured_output.structured_output_tool``).
_VALIDATION_FAILED = re.compile(
    r"^Validation failed for (?P<tool>\S+?)\. Please fix the following errors:\n(?P<errors>.*)",
    re.DOTALL,
)
_ERROR_FIELD = re.compile(r"^- Field '(?P<path>[^']*)'")


def _schema_branches(node: Any, defs: Mapping[str, Any], depth: int = 0) -> list[Mapping[str, Any]]:
    """``node`` with ``$ref`` followed and ``anyOf``/``oneOf``/``allOf`` flattened."""
    if not isinstance(node, Mapping) or depth > 20:
        return []
    ref = node.get("$ref")
    if isinstance(ref, str):
        return _schema_branches(defs.get(ref.rsplit("/", 1)[-1]), defs, depth + 1)
    branches = [node]
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in node.get(key) or []:
            branches.extend(_schema_branches(sub, defs, depth + 1))
    return branches


def declared_path(path: str, schema: Mapping[str, Any] | None) -> str:
    """``path`` (a pydantic error location, ``a -> 3 -> b``) with only declared names kept.

    Each segment is checked against the output model's JSON schema: a property the
    schema declares, a list index, or a union member's tag or model name is kept; a dict key (a ``dict[str, ...]`` field such as
    DynamoDB's ``select_columns``, keyed by source-derived names) becomes ``<key>``;
    anything else becomes ``<?>``. So no model or customer text reaches INFO.
    """
    root: Mapping[str, Any] = schema or {}
    defs: Mapping[str, Any] = root.get("$defs") or root.get("definitions") or {}
    nodes = _schema_branches(root, defs) if schema else []
    out: list[str] = []
    for segment in path.split(" -> "):
        kept: str | None = None
        following: list[Mapping[str, Any]] = []
        for node in nodes:
            properties = node.get("properties")
            if isinstance(properties, Mapping) and segment in properties:
                kept = segment
                following += _schema_branches(properties[segment], defs)
        if kept is None and segment.isdigit():
            for node in nodes:
                if node.get("type") == "array" or "items" in node:
                    kept = segment
                    following += _schema_branches(node.get("items"), defs)
        if kept is None:  # pydantic names a union member by its tag or its model name
            for node in nodes:
                tags = (node.get("discriminator") or {}).get("mapping") or {}
                if segment in tags:
                    kept = segment
                    following += _schema_branches({"$ref": tags[segment]}, defs)
        if kept is None and segment in defs:
            kept, following = segment, _schema_branches(defs[segment], defs)
        if kept is None:
            for node in nodes:
                extra = node.get("additionalProperties")
                if isinstance(extra, Mapping):
                    kept = "<key>"
                    following += _schema_branches(extra, defs)
        out.append(kept if kept is not None else (segment if segment.isdigit() else "<?>"))
        nodes = following
    return " -> ".join(out)


def _subclasses(cls: type) -> list[type]:
    found: list[type] = []
    for sub in cls.__subclasses__():
        found += [sub, *_subclasses(sub)]
    return found


@functools.lru_cache(maxsize=64)
def _model_schema(name: str, properties: frozenset[str]) -> Mapping[str, Any] | None:
    """The full JSON schema of the output model ``name`` whose fields are ``properties``.

    Strands' tool spec drops ``$defs`` that nested models refer to, so the path is
    walked over the model's own schema; the property set tells same-named models apart.
    """
    from pydantic import BaseModel
    from pydantic.errors import PydanticUserError

    for cls in _subclasses(BaseModel):
        if cls.__name__ != name:
            continue
        try:
            schema: dict[str, Any] = cls.model_json_schema()  # type: ignore[attr-defined]
        except PydanticUserError:  # a model with no JSON schema is just not a match
            continue
        if frozenset(schema.get("properties") or {}) == properties:
            return schema
    return None


def _output_schema(tool_specs: list[ToolSpec] | None, name: str) -> Mapping[str, Any] | None:
    """The output tool's schema: its model's full schema when found, else the tool spec's."""
    for spec in tool_specs or []:
        if spec.get("name") == name:
            schema = (spec.get("inputSchema") or {}).get("json")
            if not isinstance(schema, Mapping):
                return None
            full = _model_schema(name, frozenset(schema.get("properties") or {}))
            return full if full is not None else schema
    return None


def structured_output_reask(
    messages: Messages,
    tool_choice: ToolChoice | None,
    tool_specs: list[ToolSpec] | None = None,
) -> tuple[str, str] | None:
    """Why this request asks again for a structured output, or None when it does not.

    Strands asks again in two cases, both visible in the request it sends:

    * the previous answer ended without calling the output tool: the last user turn
      is Strands' "format the previous response" prompt, with a forced tool choice;
    * the output tool's input failed the output model: the last user turn carries
      the tool's error result, listing each failing field.

    Returns ``(reason, detail)``: ``reason`` names only the case and the failing
    field paths, reduced to the field names the output tool's schema (in
    ``tool_specs``) declares and list indexes (:func:`declared_path`); ``detail``
    adds the validation messages, which can quote model or customer text (DEBUG
    only, #143).
    """
    if not messages or messages[-1].get("role") != "user":
        return None
    content = messages[-1].get("content") or []
    texts = [block.get("text") for block in content if "text" in block]
    if is_forced(tool_choice) and texts == [DEFAULT_STRUCTURED_OUTPUT_PROMPT]:
        reason = "the answer ended without calling the output tool"
        return reason, reason
    for block in content:
        result: Mapping[str, Any] = block.get("toolResult") or {}
        if result.get("status") != "error":
            continue
        for item in result.get("content") or []:
            match = _VALIDATION_FAILED.match(str(item.get("text") or ""))
            if not match:
                continue
            lines = [line for line in match["errors"].splitlines() if line.startswith("- ")]
            schema = _output_schema(tool_specs, match["tool"])
            paths = [
                declared_path(m["path"], schema)
                for line in lines
                if (m := _ERROR_FIELD.match(line))
            ]
            tool = match["tool"] if schema is not None else "the output tool"
            reason = f"{tool} input failed validation, {len(lines)} error(s) at: " + ", ".join(
                paths or ["(no field)"]
            )
            return reason, match["errors"]
    return None


class RoleBedrockModel(BedrockModel):
    """A ``BedrockModel`` whose forced ``tool_choice`` follows the model family."""

    def __init__(
        self,
        *,
        profile: ModelProfile,
        phase: str = "model",
        cache_conversation: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.profile = profile
        #: The phase named in a :class:`GuardrailIntervened` error (the role's name).
        self.phase = phase
        #: Place Strands' cache point on the latest user turn (multi-turn agents
        #: only; see :meth:`with_conversation_cache`). Needs the role's ``cache_config``.
        self.cache_conversation = cache_conversation
        #: Token totals over every call this model made, plus the call count.
        self.usage: dict[str, int] = {"calls": 0, **dict.fromkeys(USAGE_FIELDS, 0)}
        #: One reason per request that asked again for a structured output.
        self.structured_output_reasks: list[str] = []

    def with_conversation_cache(self) -> RoleBedrockModel:
        """This model for a multi-turn agent: its conversation is cached too.

        Each turn of a schema designer (tool calls, a structured-output re-ask, a
        numbers correction, a PE revision on the same agent) resends every earlier
        turn, so a cache point on the latest user turn lets the next turn read the
        conversation at the cache-read rate instead of paying full input for it
        again (#1675). The returned model shares this one's client, settings, token
        totals and re-ask record, so the design trace still counts every call; this
        model is unchanged for one-shot calls (the PE reviewer).
        """
        if self.cache_conversation:
            return self
        sibling = copy.copy(self)
        sibling.cache_conversation = True
        return sibling

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
        reask = structured_output_reask(messages, tool_choice, tool_specs)
        if reask is not None:
            self.structured_output_reasks.append(reask[0])
            REASK_LOGGER.info(
                "model=<%s> | phase=<%s> | structured output asked again: %s",
                self.config.get("model_id"),
                self.phase,
                reask[0],
            )
            logger.debug("structured output re-ask detail: %s", reask[1])
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
        """Strands' cache point on the last user turn, for a multi-turn agent only.

        Strands' ``cache_config`` marks the last user turn on every request. For a
        conversation (``cache_conversation``) that is the point the next turn reads
        back: Strands keeps exactly one point in the messages, so with the tools and
        system points a request carries three, within Bedrock's four. A one-shot
        input (a PE review) is unique per call, so a write there would cost the
        cache-write premium on content no later request reads: no point is placed,
        and any cache point a caller placed in the messages is left as it is.
        """
        if self.cache_conversation:
            super()._inject_cache_point(messages, *args, **kwargs)
        return None

    def _get_additional_request_fields(self, tool_choice: ToolChoice | None) -> dict[str, Any]:
        fields: dict[str, Any] = super()._get_additional_request_fields(tool_choice)
        if self.profile.forced_tool_choice == "needs_thinking_disabled" and is_forced(tool_choice):
            extra = dict(fields.get("additionalModelRequestFields", {}))
            extra["thinking"] = {"type": "disabled"}
            return {"additionalModelRequestFields": extra}
        return fields
