"""Publish phase outputs to the AWS Transform Artifacts panel.

The pipeline's system of record is S3, written through ``ArtifactStore``
(``S3ArtifactStore`` is a raw ``put_object``). That makes an object durable but
gives the platform nothing to show: no artifact record, no download link, no
entry in the Artifacts panel. Registering a platform artifact is a separate call,
and until 2026-08-21 nothing in ``src/atx_orchestrator/`` made it — which is why
a successful synthesis run produced a report the customer could not reach.

This is a port of the shape already working in
``docdb-mig-exp-atx/src/atx_orchestrator/sizing.py`` (lines 159-183). Two
properties of that implementation are deliberate and preserved here:

**The S3 copy is written first and independently.** Callers persist through the
store, then publish. If publishing fails the data is already safe.

**Publishing never raises.** A failure to register an artifact must not fail a
phase whose real work succeeded. That mistake was made once already, in the
synthesis guard, where an exception after the report was durable turned a
successful run into a reported failure and left the agent telling the customer no
report existed. Here a failure logs a warning and returns an empty mapping.

Not yet implemented: worklogs and ``plan_step_id``. An artifact carries a
download link in the Artifacts panel; the *worklog* is a second index that makes
it appear in the step narrative. The reference implementation does not do this
either, so it is unproven and left as follow-up.

Rendering of the deliverables themselves lives in ``src/report/``.
"""

from __future__ import annotations

import logging
from typing import Literal, cast

logger = logging.getLogger(__name__)

# The SDK types these as Literals, so they are the authoritative enums — more
# reliable than copying whatever the reference implementation happened to pass.
# Surfaced by mypy on 2026-08-21; note TXT, not TEXT.
CategoryType = Literal[
    "AGENT_INPUT",
    "AGENT_OUTPUT",
    "CUSTOMER_INPUT",
    "CUSTOMER_OUTPUT",
    "HITL_FROM_AGENT",
    "HITL_FROM_USER",
    "INTERNAL",
    "PLAN_STEP_OUTPUT",
    "PLAN_STEP_SUMMARY",
    "STATE",
]
FileType = Literal["CSV", "HTML", "JSON", "MARKDOWN", "OTHER", "PDF", "PPTX", "TXT", "XLSX", "ZIP"]


# Download filename extension per file type, so a published artifact downloads
# as "<name>.<ext>" instead of an opaque UUID. Keys are the FileType literals.
_FILE_TYPE_EXT: dict[str, str] = {
    "CSV": "csv",
    "HTML": "html",
    "JSON": "json",
    "MARKDOWN": "md",
    "PDF": "pdf",
    "PPTX": "pptx",
    "TXT": "txt",
    "XLSX": "xlsx",
    "ZIP": "zip",
}


def _default_path(label: str, file_type: str) -> str:
    """Derive a friendly download filename from the label and file type.

    Slugs the label to a filesystem-safe stem and appends the extension for the
    file type. Used when a caller does not supply an explicit ``path``. The
    result is what the customer's browser saves the download as, so it must be
    human-readable rather than the artifact UUID.
    """
    stem = "".join(c if (c.isalnum() or c in "-_") else "-" for c in label.strip().lower())
    stem = "-".join(filter(None, stem.split("-"))) or "artifact"
    ext = _FILE_TYPE_EXT.get(str(file_type).upper(), "dat")
    return f"{stem}.{ext}"


PublishItem = (
    tuple[bytes, FileType, str, CategoryType] | tuple[bytes, FileType, str, CategoryType, str]
)


def publish(items: list[PublishItem]) -> dict[str, str]:
    """Register content with the platform so it appears in the Artifacts panel.

    Uploads each item as an **EXTERNAL**-visibility artifact via the client-direct
    sequence (``create_artifact_upload_url`` -> ``upload_from_presigned_url`` ->
    ``complete_artifact_upload``). This is deliberate and important:

    * ``ArtifactStore.upload_artifact`` hardcodes ``visibility="INTERNAL"``, which
      the frontend cannot serve for download and which cross-account viewers
      (e.g. a customer in a different account than where the job ran) cannot see.
      The publishing account sees INTERNAL artifacts fine, which masks the bug.
      ``CUSTOMER_OUTPUT`` deliverables must be EXTERNAL to reach the customer.
    * The SDK method also never sets ``fileMetadata.path``, so the download is
      named with the artifact UUID. We set it so the file saves as its friendly
      name. Reference: AWSTransformHelixAgentSkills schema_deployment_tools.py.

    Args:
        items: ``(content, file_type, label, category_type[, path])`` tuples.
            ``label`` is what the customer sees in the Artifacts panel; ``path``
            (optional) is the download filename and defaults to a slug of the
            label plus the file-type extension.

    Returns:
        ``{label: artifact_id}`` for whatever uploaded. Empty when running outside
        the ATX runtime, or when publishing failed. **Never raises** — the caller's
        S3 copy is the system of record and its phase must not fail over this.

    Each item is uploaded independently so one rejection does not lose the rest.
    """
    published: dict[str, str] = {}
    try:
        import uuid

        from agent_builder_sdk.agentic_framework.client_factory import get_agentic_api_client
        from agent_builder_sdk.agentic_framework.common import (
            calculate_digest,
            upload_from_presigned_url,
        )
        from agent_builder_sdk.env_var import get_agent_context_from_env
        from agent_builder_types import type_defs as abt

        ctx = get_agent_context_from_env()
        client = get_agentic_api_client()
        # ctx.to_dict() is typed dict[str, object]; the generated client expects the
        # RequestContextTypeDef TypedDict. The runtime shape is identical (this is
        # exactly what the SDK's own _create_request_context returns), so cast rather
        # than rebuild it. Without the cast the newer mypy flags all four SDK calls.
        request_context = cast("abt.RequestContextTypeDef", dict(ctx.to_dict()))

        for item in items:
            content, file_type, label, category = item[0], item[1], item[2], item[3]
            path = item[4] if len(item) > 4 else _default_path(label, file_type)
            try:
                # Upload as INTERNAL first. Setting visibility="EXTERNAL" here does
                # NOT actually make the artifact externally visible — the platform
                # keeps it INTERNAL (observed: a create with visibility=EXTERNAL
                # still stored INTERNAL). The switch that makes an artifact visible
                # cross-account / frontend-downloadable is a separate copy_artifact
                # call after the upload completes. See AWSTransformSQLTransformer
                # artifact_service.upload_artifact (copy_artifact "make public").
                resp = client.create_artifact_upload_url(
                    contentDigest={"sha256": calculate_digest(content)},
                    visibility="INTERNAL",
                    artifactReference={
                        "artifactType": {"categoryType": category, "fileType": file_type}
                    },
                    fileMetadata={"path": path},
                    label=label,
                    requestContext=request_context,
                )
                artifact_id = resp["artifactId"]

                # The presigned PUT targets either the ATX-managed bucket or the
                # customer's own bucket; the metadata's storedInAtxBucket flag
                # tells upload_from_presigned_url which error contract applies.
                metadata = client.get_artifact_metadata(
                    artifactId=artifact_id, requestContext=request_context
                )
                upload_from_presigned_url(
                    resp, content, metadata["artifact"].get("storedInAtxBucket", True)
                )
                client.complete_artifact_upload(
                    artifactId=artifact_id, requestContext=request_context
                )

                # Make the completed artifact externally visible. This is the
                # actual visibility switch (INTERNAL -> EXTERNAL) for a
                # CUSTOMER_OUTPUT deliverable so cross-account viewers can see and
                # download it. idempotencyToken guards against a retried publish.
                client.copy_artifact(
                    artifactId=artifact_id,
                    idempotencyToken=str(uuid.uuid4()),
                    requestContext=request_context,
                )

                published[label] = artifact_id
                logger.info(
                    "Published artifact: label=%r type=%s category=%s visibility=EXTERNAL "
                    "path=%r bytes=%d id=%s",
                    label,
                    file_type,
                    category,
                    path,
                    len(content),
                    artifact_id,
                )
            except Exception as exc:  # noqa: BLE001
                # One artifact failing must not lose the others.
                logger.warning(
                    "Artifact upload failed for %r (type=%s category=%s): %s",
                    label,
                    file_type,
                    category,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        # Expected outside the ATX runtime (no agent context env vars), e.g. local
        # runs and tests. Also catches SDK or client construction failure.
        logger.warning("Artifact publishing unavailable, S3 copies still written: %s", exc)
    return published
