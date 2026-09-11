"""Raise and read Human-In-The-Loop (HITL) tasks on the AWS Transform platform.

The assignment-review gate (ADR-028) needs the customer to review, and optionally
edit, the query-to-engine routing. The original transport published a markdown
document to the Artifacts panel and asked the customer to paste an edited table
back into chat. That does not scale: the routing table can be thousands of rows,
which cannot be pasted back, and free-text "move query X" instructions were never
wired to the override path.

This module is the platform-native replacement: a HITL task rendered as an
editable ``TableComponent``. The customer edits the ``new engine`` / ``in scope``
cells inline in the WebApp and submits; the submission comes back as a structured
``HITL_FROM_USER`` artifact we read and apply. It is the same lifecycle Helix and
the DB-Assessment agent use, over the same ``get_agentic_api_client()`` seam
:mod:`artifacts` already uploads through.

Two functions, mirroring the two halves of the gate:

* :func:`raise_assignment_table` — upload the table as a ``HITL_FROM_AGENT`` JSON
  artifact, create a **BLOCKING** ``TableComponent`` HITL task, and start it.
  Returns the task id. The orchestrator records that id and ends its turn
  (raise-and-resume); the platform re-invokes the orchestrator when the customer
  submits.
* :func:`read_assignment_submission` — given a task id, fetch the task, and if the
  customer has submitted, return the edited rows. Tolerates the submission being
  inlined on the task or referenced by a downloadable artifact id.

Like :mod:`artifacts`, **nothing here raises out to the caller**. Outside the ATX
runtime (local runs, tests) or on any SDK/client failure these return ``None`` /
``("unavailable", None)`` so the gate can fall back to the chat transport instead
of breaking. The upload sequence deliberately mirrors ``artifacts.publish`` —
notably ``upload_from_presigned_url(resp, content, storedInAtxBucket)`` takes the
managed-bucket flag as its third argument in this SDK, unlike the two-argument
form other agents' SDKs expose. A HITL *request* artifact stays ``INTERNAL`` (it
feeds the HITL UI, not the Artifacts panel), so there is no ``copy_artifact``
step, which is the one that makes ``CUSTOMER_OUTPUT`` deliverables external.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# HITL task statuses that mean the customer is done with the task (submitted, or
# the task was closed after a submission). Anything else means still waiting.
_TERMINAL_STATUSES = frozenset({"SUBMITTED", "COMPLETED", "CLOSED", "CLOSED_PENDING_NEXT_TASK"})


def _resolve_client_and_context() -> tuple[Any, dict[str, Any]]:
    """Resolve the Agentic API client and requestContext from the runtime env.

    Raises if the SDK or agent-context env vars are absent (i.e. outside the ATX
    runtime); callers catch and degrade. Mirrors the seam in :mod:`artifacts`.
    """
    from agent_builder_sdk.agentic_framework.client_factory import (  # noqa: PLC0415
        get_agentic_api_client,
    )
    from agent_builder_sdk.env_var import get_agent_context_from_env  # noqa: PLC0415

    client = get_agentic_api_client()
    request_context = dict(get_agent_context_from_env().to_dict())
    return client, request_context


def _upload_hitl_request(client: Any, request_context: dict[str, Any], payload: dict) -> str:
    """Upload ``payload`` as a HITL_FROM_AGENT JSON artifact; return its id.

    Uses the same three-step upload as ``artifacts.publish`` but leaves the
    artifact INTERNAL (a HITL request feeds the task UI, not the Artifacts panel,
    so it is not copied to EXTERNAL visibility).
    """
    from agent_builder_sdk.agentic_framework.common import (  # noqa: PLC0415
        calculate_digest,
        upload_from_presigned_url,
    )

    content = json.dumps(payload).encode("utf-8")
    resp = client.create_artifact_upload_url(
        contentDigest={"sha256": calculate_digest(content)},
        visibility="INTERNAL",
        artifactReference={"artifactType": {"categoryType": "HITL_FROM_AGENT", "fileType": "JSON"}},
        requestContext=request_context,
    )
    artifact_id = resp["artifactId"]
    metadata = client.get_artifact_metadata(artifactId=artifact_id, requestContext=request_context)
    upload_from_presigned_url(resp, content, metadata["artifact"].get("storedInAtxBucket", True))
    client.complete_artifact_upload(artifactId=artifact_id, requestContext=request_context)
    return str(artifact_id)


def raise_assignment_table(
    *,
    column_definitions: list[dict[str, Any]],
    items: list[dict[str, Any]],
    header: str,
    title: str,
    description: str,
    submit_button: str = "Approve routing",
    step_id: str = "",
    tag: str = "",
) -> str | None:
    """Raise a BLOCKING editable-table HITL task; return its id, or None.

    Uploads ``{properties: {columnDefinitions, items, header, submitButton}}`` as
    the HITL request artifact, creates a ``TableComponent`` HITL task with
    ``blockingType="BLOCKING"`` (so the WebApp parks the step until the customer
    submits), and starts it. ``column_definitions`` mark which cells are editable
    via each column's ``editConfig``.

    Returns the HITL task id on success. Returns ``None`` outside the ATX runtime
    or on any failure — the caller then falls back to the chat transport. Never
    raises.
    """
    try:
        client, request_context = _resolve_client_and_context()
    except Exception as exc:  # noqa: BLE001 - expected outside the ATX runtime
        logger.warning("HITL unavailable (no runtime client): %s", exc)
        return None

    try:
        artifact_id = _upload_hitl_request(
            client,
            request_context,
            {
                "properties": {
                    "columnDefinitions": column_definitions,
                    "items": items,
                    "header": header,
                    "submitButton": submit_button,
                }
            },
        )

        create_kwargs: dict[str, Any] = {
            "uxComponentId": "TableComponent",
            "title": title,
            "description": description,
            "severity": "STANDARD",
            "hitlTaskType": "NORMAL",
            "blockingType": "BLOCKING",
            "hitlRequestArtifact": {"artifactId": artifact_id},
            "idempotencyToken": str(uuid.uuid4()),
            "requestContext": request_context,
        }
        if step_id:
            create_kwargs["stepId"] = step_id
        if tag:
            create_kwargs["tag"] = tag

        hitl_response = client.create_hitl_task(**create_kwargs)
        hitl_task_id = str(hitl_response["hitlTaskId"])

        client.start_hitl_task(
            hitlTaskId=hitl_task_id,
            idempotencyToken=str(uuid.uuid4()),
            firstInChain=True,
            requestContext=request_context,
        )
        logger.info(
            "Raised BLOCKING TableComponent HITL task %s (%d rows)", hitl_task_id, len(items)
        )
        return hitl_task_id
    except Exception as exc:  # noqa: BLE001 - degrade to chat fallback
        logger.warning("Failed to raise HITL table task: %s", exc, exc_info=True)
        return None


def _download_artifact_json(client: Any, request_context: dict[str, Any], artifact_id: str) -> Any:
    """Download a JSON artifact by id and return the parsed body.

    Uses the SDK's ``create_artifact_download_url`` + ``download_from_presigned_url``
    (the same seam :mod:`artifacts` uploads through, and the SDK's own
    ``ArtifactStore.download_artifact`` uses), writing to a temp file and parsing
    it. Returns the parsed JSON, or ``None`` if it cannot be fetched/parsed.
    """
    from agent_builder_sdk.agentic_framework.common import (  # noqa: PLC0415
        download_from_presigned_url,
    )

    try:
        dl = client.create_artifact_download_url(
            artifactId=artifact_id, requestContext=request_context
        )
        stored_in_atx = (dl.get("artifact") or {}).get("storedInAtxBucket", True)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tmp:
            download_from_presigned_url(dl, tmp.name, is_managed_bucket=stored_in_atx)
            with open(tmp.name, encoding="utf-8") as fh:
                raw = fh.read()
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not download HITL submission artifact %s: %s", artifact_id, exc)
        return None


def _extract_items(payload: Any) -> list[dict[str, Any]] | None:
    """Pull the edited rows out of a HITL submission payload, tolerantly.

    A submitted ``TableComponent`` may hand back the rows in a few shapes:
    the bare list, ``{"items": [...]}``, or ``{"properties": {"items": [...]}}``.
    A JSON string is decoded first. Returns the list of row dicts, or ``None``.
    """
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    rows: Any
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("items")
        if rows is None:
            rows = (payload.get("properties") or {}).get("items")
    else:
        return None
    if not isinstance(rows, list):
        return None
    return [r for r in rows if isinstance(r, dict)]


def read_assignment_submission(hitl_task_id: str) -> tuple[str, list[dict[str, Any]] | None]:
    """Fetch a HITL task and return ``(status, edited_items | None)``.

    ``status`` is a coarse label the caller can branch on:

    * ``"submitted"`` — the customer submitted; ``edited_items`` is the row list
      (possibly empty if the submission carried no rows).
    * ``"awaiting_submission"`` — the task exists but has no human response yet.
    * ``"unavailable"`` — outside the ATX runtime, or the task/response could not
      be read; ``edited_items`` is ``None``.

    The submission is read from ``hitlTask.humanArtifact``: inline ``content`` is
    preferred (no download), otherwise it is fetched by ``artifactId``. Both the
    nested ``{"hitlTask": {...}}`` and a flat top-level shape are tolerated. Never
    raises.
    """
    try:
        client, request_context = _resolve_client_and_context()
    except Exception as exc:  # noqa: BLE001 - outside the ATX runtime
        logger.warning("HITL read unavailable (no runtime client): %s", exc)
        return "unavailable", None

    try:
        resp: Any = client.get_hitl_task(hitlTaskId=hitl_task_id, requestContext=request_context)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_hitl_task failed for %s: %s", hitl_task_id, exc)
        return "unavailable", None

    task = resp.get("hitlTask") or resp
    human = task.get("humanArtifact") or resp.get("humanArtifact") or {}
    status = str(task.get("hitlTaskStatus") or task.get("status") or "").upper()

    # A submission is signalled by the presence of a humanArtifact. A closed task
    # keeps its humanArtifact, so terminal status alone is not "just submitted";
    # the caller's .meta COMPLETED flag guards against re-applying a processed one.
    if not human and status not in _TERMINAL_STATUSES:
        return "awaiting_submission", None
    if not human:
        return "awaiting_submission", None

    inline = human.get("content")
    items = _extract_items(inline)
    if items is None:
        artifact_id = human.get("artifactId")
        if artifact_id:
            items = _extract_items(_download_artifact_json(client, request_context, artifact_id))

    if items is None:
        logger.warning(
            "HITL task %s has a humanArtifact but no readable items (status=%s)",
            hitl_task_id,
            status,
        )
        return "unavailable", None
    return "submitted", items
