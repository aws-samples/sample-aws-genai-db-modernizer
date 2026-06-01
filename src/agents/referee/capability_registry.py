"""Capability Registry — Engine capabilities and serviceability detection.

Maps each engine's fundamental capabilities (what it CAN do at an
architectural level) and provides detection logic for query requirements.

Used by the reality check serviceability gate to prevent query absorption
when the target engine fundamentally cannot serve the access pattern.

This is distinct from the existing ENGINE_CAPABILITIES in reality_check.py
which tracks workload-pattern fit scoring. This registry tracks hard
architectural constraints: things an engine structurally cannot do.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Hard capability matrix — architectural constraints, not fit preferences.
#
# A capability here means the engine CAN serve this pattern at all,
# even if poorly. Absence means "structurally impossible."
# ---------------------------------------------------------------------------

ENGINE_CAPABILITIES: dict[str, set[str]] = {
    "dynamodb": {"strong_consistency"},
    "documentdb": {"multi_doc_acid", "strong_consistency"},
    "opensearch": {"inverted_index", "scan_engine"},
    "aurora_postgresql": {"multi_doc_acid", "strong_consistency", "scan_engine", "inverted_index"},
    "aurora_mysql": {"multi_doc_acid", "strong_consistency", "scan_engine"},
}

# ---------------------------------------------------------------------------
# Capability detectors — regex patterns that indicate a query requires
# a specific hard capability.
# ---------------------------------------------------------------------------

CAPABILITY_DETECTORS: dict[str, list[str]] = {
    "inverted_index": [
        r"LIKE\s+['\"]%",
        r"MATCH\s*\(.+?\)\s*AGAINST\s*\(",
        r"to_tsvector|to_tsquery|@@",
    ],
    "scan_engine": [
        r"\b(ROW_NUMBER|RANK|DENSE_RANK|NTILE|LAG|LEAD)\s*\(\s*\)\s*OVER\s*\(",
        r"WITH\s+RECURSIVE\b",
    ],
    "multi_doc_acid": [],  # Structural detection, not regex-based
}

# Compiled regex cache (built on first use)
_COMPILED_DETECTORS: dict[str, list[re.Pattern]] | None = None


def _get_compiled_detectors() -> dict[str, list[re.Pattern]]:
    global _COMPILED_DETECTORS
    if _COMPILED_DETECTORS is None:
        _COMPILED_DETECTORS = {
            cap: [re.compile(pat, re.IGNORECASE) for pat in patterns]
            for cap, patterns in CAPABILITY_DETECTORS.items()
        }
    return _COMPILED_DETECTORS


# ---------------------------------------------------------------------------
# Lightweight managed-service alternatives for small orphan sets
# ---------------------------------------------------------------------------

LIGHTWEIGHT_ALTERNATIVES: dict[str, dict[str, str]] = {
    "scan_engine": {
        "service": "Amazon Athena + S3",
        "use_when": "Infrequent analytics (< daily), reporting, ad-hoc exploration",
        "pattern": "DynamoDB export to S3 -> Athena queries on schedule or on-demand",
        "cost_profile": "Pay-per-query ($5/TB scanned), zero idle cost",
        "limitations": "Not real-time, seconds-to-minutes latency, read-only",
    },
    "inverted_index": {
        "service": "OpenSearch Serverless",
        "use_when": "Low-volume full-text search (< 5 queries, low RPS)",
        "pattern": "DynamoDB Streams -> OpenSearch Serverless collection -> search API",
        "cost_profile": "Serverless pricing, scales to zero when idle",
        "limitations": "Higher per-request cost at scale vs provisioned OpenSearch",
    },
    "multi_doc_acid": {
        "service": "Application-layer saga pattern",
        "use_when": "Infrequent cross-entity transactions, eventual consistency acceptable",
        "pattern": "Step Functions orchestrated writes with compensating transactions",
        "cost_profile": "No additional database, Step Functions execution cost only",
        "limitations": "Eventual consistency, requires idempotent operations",
    },
}

# ---------------------------------------------------------------------------
# Triage signal → hard capability mapping
#
# When a triage signal implies a hard architectural requirement, map it here.
# Not all signals imply hard capabilities — most are just workload preferences.
# ---------------------------------------------------------------------------

SIGNAL_TO_CAPABILITY: dict[str, str] = {
    "text_search": "inverted_index",
}


def detect_required_capabilities(query_text: str, signals: list[str]) -> list[str]:
    """Detect hard capability requirements from query text and triage signals.

    Returns a list of capability names that the query structurally requires.
    An empty list means any engine can potentially serve this query.
    """
    required: set[str] = set()

    # 1. Signal-based detection (fast path)
    for sig in signals:
        cap = SIGNAL_TO_CAPABILITY.get(sig)
        if cap:
            required.add(cap)

    # 2. Regex-based detection from SQL text
    if query_text:
        detectors = _get_compiled_detectors()
        for cap, patterns in detectors.items():
            for pattern in patterns:
                if pattern.search(query_text):
                    required.add(cap)
                    break

    return sorted(required)


def can_engine_serve_capability(engine: str, required: list[str]) -> bool:
    """Check if an engine can serve ALL required capabilities.

    Returns True if:
    - The query has no required capabilities (any engine can serve it), OR
    - The engine has ALL required capabilities in its capability set.
    """
    if not required:
        return True

    engine_caps = ENGINE_CAPABILITIES.get(engine, set())
    return all(cap in engine_caps for cap in required)


def suggest_lightweight_alternative(capability: str) -> dict[str, str] | None:
    """Get a lightweight managed-service alternative for a capability.

    Returns None if no alternative exists for the given capability.
    """
    return LIGHTWEIGHT_ALTERNATIVES.get(capability)
