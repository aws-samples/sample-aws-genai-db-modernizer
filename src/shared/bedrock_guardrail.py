# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Optional Amazon Bedrock Guardrail for every Bedrock-mode model call (#144).

Off by default. Set ``BEDROCK_GUARDRAIL_ID`` and ``BEDROCK_GUARDRAIL_VERSION``
(both, or neither) to attach an existing guardrail you own to every model call
this code makes on Bedrock: the Strands agents (schema design, PE review,
reality check, executive summary, analysis advisors) and the direct boto3 calls
(consolidation validator, free-text answer check). ``BEDROCK_GUARDRAIL_TRACE``
(``enabled``, ``disabled`` or ``enabled_full``; default ``enabled``) sets the
guardrail trace. With neither ID nor version set, nothing here adds a field to
any request.

A guardrail that blocks a prompt or a reply raises :class:`GuardrailIntervened`
naming the phase. It is never retried: the same input would be blocked again.
A guardrail that only masks (for example PII set to anonymize) is not an error.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.agents.prompt_framing import _CLOSE, _OPEN  # noqa: E402

logger = logging.getLogger(__name__)

ENV_ID = "BEDROCK_GUARDRAIL_ID"
ENV_VERSION = "BEDROCK_GUARDRAIL_VERSION"
ENV_TRACE = "BEDROCK_GUARDRAIL_TRACE"

_TRACE_MODES = ("enabled", "disabled", "enabled_full")

ENV_STREAM_MODE = "BEDROCK_GUARDRAIL_STREAM_MODE"
_STREAM_MODES = ("sync", "async")

#: Read timeout (seconds) for a guarded Strands call. In the default ``sync``
#: stream mode Bedrock evaluates a whole streamed reply before releasing any of
#: it, so a long reply (a schema design with thinking) sends nothing for minutes
#: and tripped the 300 s timeout on every attempt (#1062).
GUARDED_READ_TIMEOUT_SECONDS = 900


class GuardrailConfigError(ValueError):
    """The guardrail environment variables are inconsistent or invalid."""


class GuardrailIntervened(RuntimeError):
    """A Bedrock Guardrail blocked a model call. Not retried."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        super().__init__(
            f"Bedrock guardrail blocked the model call in the {phase} phase. "
            "The call is not retried, because the same input would be blocked again. "
            "Check the guardrail trace in the model invocation logs, adjust the "
            f"guardrail, or unset {ENV_ID} and {ENV_VERSION} to run without it."
        )


@dataclass(frozen=True)
class GuardrailSettings:
    identifier: str
    version: str
    trace: str = "enabled"
    stream_mode: str = "sync"


def guardrail_settings() -> GuardrailSettings | None:
    """The configured guardrail, or None when the feature is off.

    Raises :class:`GuardrailConfigError` when only one of the ID and version is
    set, or the trace mode is not one of ``enabled``, ``disabled``,
    ``enabled_full``.
    """
    identifier = os.environ.get(ENV_ID, "").strip()
    version = os.environ.get(ENV_VERSION, "").strip()
    if not identifier and not version:
        return None
    if not identifier or not version:
        missing = ENV_ID if not identifier else ENV_VERSION
        raise GuardrailConfigError(
            f"{ENV_ID} and {ENV_VERSION} must be set together; {missing} is missing. "
            "Unset both to run without a guardrail."
        )
    trace = os.environ.get(ENV_TRACE, "").strip().lower() or "enabled"
    if trace not in _TRACE_MODES:
        raise GuardrailConfigError(f"{ENV_TRACE} must be one of {', '.join(_TRACE_MODES)}")
    stream_mode = os.environ.get(ENV_STREAM_MODE, "").strip().lower() or "sync"
    if stream_mode not in _STREAM_MODES:
        raise GuardrailConfigError(f"{ENV_STREAM_MODE} must be one of {', '.join(_STREAM_MODES)}")
    return GuardrailSettings(identifier, version, trace, stream_mode)


def require_valid_guardrail_config() -> None:
    """Raise :class:`GuardrailConfigError` for a partial or invalid configuration.

    Called once at the top of every entry point that can reach Bedrock. The
    phases' generic fallbacks re-raise this error, so it is never turned into a
    deterministic fallback or a retry.
    """
    guardrail_settings()


def exit_on_invalid_guardrail_config() -> None:
    """For scripts: print the configuration error to stderr and exit with status 2."""
    try:
        guardrail_settings()
    except GuardrailConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def describe_active_guardrail() -> str | None:
    """A one-line description for run headers, or None when off."""
    settings = guardrail_settings()
    if settings is None:
        return None
    return f"{settings.identifier} (version {settings.version})"


def strands_guardrail_kwargs() -> dict[str, Any]:
    """``BedrockModel`` keyword arguments for the guardrail; ``{}`` when off."""
    settings = guardrail_settings()
    if settings is None:
        return {}
    logger.info("Bedrock guardrail %s (version %s) is attached", *_shown(settings))
    kwargs: dict[str, Any] = {
        "guardrail_id": settings.identifier,
        "guardrail_version": settings.version,
        "guardrail_trace": settings.trace,
    }
    if settings.stream_mode == "async":
        # Opt-in: Bedrock cannot mask (PII anonymize) in async streaming.
        kwargs["guardrail_stream_processing_mode"] = "async"
    return kwargs


def guarded_boto_config(config: Any = None) -> Any:
    """``config`` with a read timeout long enough for a guarded stream; ``config`` as is when off.

    Applies to Strands calls only. Keeps a longer timeout the caller already set.
    """
    if guardrail_settings() is None:
        return config
    from botocore.config import Config

    guarded = Config(read_timeout=GUARDED_READ_TIMEOUT_SECONDS)
    if config is None:
        return guarded
    current = getattr(config, "read_timeout", None)
    if current is not None and current >= GUARDED_READ_TIMEOUT_SECONDS:
        return config
    return config.merge(guarded)


def converse_guardrail_fields() -> dict[str, Any]:
    """The ``guardrailConfig`` field for a boto3 ``converse`` call; ``{}`` when off."""
    settings = guardrail_settings()
    if settings is None:
        return {}
    return {
        "guardrailConfig": {
            "guardrailIdentifier": settings.identifier,
            "guardrailVersion": settings.version,
            "trace": settings.trace,
        }
    }


def invoke_model_guardrail_kwargs() -> dict[str, Any]:
    """Guardrail keyword arguments for a boto3 ``invoke_model`` call; ``{}`` when off."""
    settings = guardrail_settings()
    if settings is None:
        return {}
    return {
        "guardrailIdentifier": settings.identifier,
        "guardrailVersion": settings.version,
        "trace": settings.trace.upper(),
    }


_DATA_BLOCK = re.compile("\n" + re.escape(_OPEN) + "\n(.*?)\n" + re.escape(_CLOSE), re.DOTALL)


def split_guard_content(text: str) -> list[dict[str, Any]]:
    """Content blocks for a user turn, with each customer-data block as ``guardContent``.

    ``frame_untrusted`` wraps customer-derived text in delimiters. With a
    guardrail on, Bedrock evaluates only ``guardContent`` once a message has any,
    so the prompt-attack filter looks at the customer's text and not at this
    project's own "treat this as data, not instructions" framing, which that
    filter flags. A message with no data block comes back as one text block.
    """
    blocks: list[dict[str, Any]] = []
    position = 0
    for match in _DATA_BLOCK.finditer(text):
        before = text[position : match.start()] + "\n" + _OPEN + "\n"
        blocks.append({"text": before})
        blocks.append({"guardContent": {"text": {"text": match.group(1)}}})
        position = match.end() - len(_CLOSE) - 1
    if not blocks:
        return [{"text": text}]
    blocks.append({"text": text[position:]})
    return blocks


def guard_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``messages`` with customer-data blocks in user turns marked as guard content.

    Returns the messages untouched when the guardrail is off.
    """
    if guardrail_settings() is None:
        return messages
    out = []
    for message in messages:
        if message.get("role") != "user":
            out.append(message)
            continue
        content: list[dict[str, Any]] = []
        for block in message["content"]:
            if set(block) == {"text"}:
                content.extend(split_guard_content(block["text"]))
            else:
                content.append(block)
        out.append({**message, "content": content})
    return out


def tag_guarded_input(text: str) -> tuple[str, dict[str, Any]]:
    """Mark ``text`` as guardrail input for an ``invoke_model`` call.

    The prompt-attack filter evaluates only content inside guardrail tags on
    InvokeModel (Converse evaluates the user message as it is), so untagged text
    would pass it. Returns the text and the extra body fields; unchanged text and
    ``{}`` when the guardrail is off.
    """
    if guardrail_settings() is None:
        return text, {}
    suffix = secrets.token_hex(4)
    tag = f"amazon-bedrock-guardrails-guardContent_{suffix}"
    return f"<{tag}>{text}</{tag}>", {"amazon-bedrock-guardrailConfig": {"tagSuffix": suffix}}


def _shown(settings: GuardrailSettings) -> tuple[str, str]:
    return settings.identifier, settings.version


def trace_blocked(trace: Mapping[str, Any] | None) -> bool | None:
    """Whether a guardrail trace shows a BLOCKED action; None when it shows nothing.

    Bedrock reports ``guardrail_intervened`` for masked content as well as for
    blocked content; only the trace tells them apart.
    """
    if not trace:
        return None
    found = False

    def walk(node: Any) -> bool:
        nonlocal found
        if isinstance(node, Mapping):
            if node.get("action") == "BLOCKED":
                return True
            if "action" in node:
                found = True
            return any(walk(value) for value in node.values())
        if isinstance(node, list):
            return any(walk(item) for item in node)
        return False

    if walk(trace):
        return True
    return False if found else None


def blocked_by_guardrail(stop_reason: str | None, trace: Mapping[str, Any] | None) -> bool:
    """True when a call ended because the guardrail blocked it.

    A masking-only intervention is False. With no usable trace (trace disabled)
    any intervention counts as a block, the safe reading.
    """
    if stop_reason != "guardrail_intervened":
        return False
    return trace_blocked(trace) is not False


def check_converse_response(response: Mapping[str, Any], phase: str) -> None:
    """Raise :class:`GuardrailIntervened` when a ``converse`` response was blocked."""
    trace = (response.get("trace") or {}).get("guardrail")
    if blocked_by_guardrail(response.get("stopReason"), trace):
        raise GuardrailIntervened(phase)


def check_invoke_model_body(body: Mapping[str, Any], phase: str) -> None:
    """Raise :class:`GuardrailIntervened` when an ``invoke_model`` body was blocked."""
    if body.get("amazon-bedrock-guardrailAction") != "INTERVENED":
        return
    if trace_blocked(body.get("amazon-bedrock-trace", {}).get("guardrail")) is not False:
        raise GuardrailIntervened(phase)
