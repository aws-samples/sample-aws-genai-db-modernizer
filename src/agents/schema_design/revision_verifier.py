"""Pure-function schema revision verifier.

Provides a set of composable check functions that validate a proposed schema
revision against coverage, consistency, conflict, and cost criteria.  Each
check returns a list[VerificationIssue].  The top-level verify_revision
function orchestrates all checks and produces a VerificationResult.

No classes, no I/O, no side effects — all functions are deterministic given
the same inputs.
"""

from __future__ import annotations

from src.contracts.schema_revision_models import VerificationIssue, VerificationResult

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _collect_covered_query_ids(schema_output: dict) -> set[str]:
    """Return the union of all query IDs referenced across access_patterns,
    index_designs, and collection_designs in *schema_output*.
    """
    covered: set[str] = set()

    for ap in schema_output.get("access_patterns", []):
        covered.update(ap.get("query_ids", []))

    for idx in schema_output.get("index_designs", []):
        covered.update(idx.get("query_ids", []))

    for col in schema_output.get("collection_designs", []):
        covered.update(col.get("query_ids", []))

    return covered


def _collect_pattern_ids(output: dict) -> set[str]:
    """Return all pattern_ids found across access_patterns, index_designs,
    and collection_designs in *output*.
    """
    ids: set[str] = set()

    for ap in output.get("access_patterns", []):
        if pid := ap.get("pattern_id"):
            ids.add(pid)

    for idx in output.get("index_designs", []):
        if pid := idx.get("pattern_id"):
            ids.add(pid)

    for col in output.get("collection_designs", []):
        if pid := col.get("pattern_id"):
            ids.add(pid)

    return ids


# ---------------------------------------------------------------------------
# Public check functions
# ---------------------------------------------------------------------------


def check_coverage(
    schema_output: dict,
    in_scope_query_ids: list[str],
    engine: str,
) -> list[VerificationIssue]:
    """Verify that every in-scope query ID is covered by at least one pattern.

    Coverage is satisfied when a query_id appears in any of:
    - access_patterns[].query_ids
    - index_designs[].query_ids
    - collection_designs[].query_ids

    Returns one VerificationIssue per uncovered query_id.
    """
    if not in_scope_query_ids:
        return []

    covered = _collect_covered_query_ids(schema_output)
    issues: list[VerificationIssue] = []

    for qid in in_scope_query_ids:
        if qid not in covered:
            issues.append(
                VerificationIssue(
                    category="coverage",
                    severity="error",
                    message=(
                        f"Query '{qid}' is in-scope for engine '{engine}' "
                        "but is not covered by any access pattern, index design, "
                        "or collection design."
                    ),
                    affected_patterns=[],
                    affected_tables=[],
                    cost_delta=None,
                    suggested_resolutions=[
                        f"Add an access pattern that covers query '{qid}'.",
                        (  # nosemgrep: string-concat-in-list
                            f"Verify that the schema agent processed query '{qid}' "
                            "and did not inadvertently drop it."
                        ),
                        (  # nosemgrep: string-concat-in-list
                            "If the query is genuinely unsupported by this engine, "
                            "mark it as out-of-scope in the schema output."
                        ),
                    ],
                )
            )

    return issues


def check_consistency(schema_output: dict, engine: str) -> list[VerificationIssue]:
    """Verify internal consistency of the schema output for the given engine.

    DynamoDB checks:
    - No duplicate table_definitions[].table_name
    - Every table_definition has a non-empty partition_key

    OpenSearch checks:
    - No duplicate index_designs[].index_name

    DocumentDB checks:
    - No duplicate collection_designs[].collection_name

    Returns one VerificationIssue per violation found.
    """
    issues: list[VerificationIssue] = []

    if engine == "dynamodb":
        seen_tables: dict[str, int] = {}
        for td in schema_output.get("table_definitions", []):
            name = td.get("table_name", "")
            seen_tables[name] = seen_tables.get(name, 0) + 1

        for name, count in seen_tables.items():
            if count > 1:
                issues.append(
                    VerificationIssue(
                        category="consistency",
                        severity="error",
                        message=(
                            f"DynamoDB table name '{name}' appears {count} times "
                            "in table_definitions. Table names must be unique."
                        ),
                        affected_patterns=[],
                        affected_tables=[name],
                        cost_delta=None,
                        suggested_resolutions=[
                            f"Remove or rename the duplicate table definition for '{name}'.",
                        ],
                    )
                )

        for td in schema_output.get("table_definitions", []):
            name = td.get("table_name", "<unknown>")
            pk = td.get("partition_key")
            if not pk:
                issues.append(
                    VerificationIssue(
                        category="consistency",
                        severity="error",
                        message=(
                            f"DynamoDB table '{name}' is missing a partition_key. "
                            "Every DynamoDB table must define a partition key."
                        ),
                        affected_patterns=[],
                        affected_tables=[name],
                        cost_delta=None,
                        suggested_resolutions=[
                            f"Add a partition_key definition to table '{name}'.",
                        ],
                    )
                )

    elif engine == "opensearch":
        seen_indices: dict[str, int] = {}
        for idx in schema_output.get("index_designs", []):
            name = idx.get("index_name", "")
            seen_indices[name] = seen_indices.get(name, 0) + 1

        for name, count in seen_indices.items():
            if count > 1:
                issues.append(
                    VerificationIssue(
                        category="consistency",
                        severity="error",
                        message=(
                            f"OpenSearch index name '{name}' appears {count} times "
                            "in index_designs. Index names must be unique."
                        ),
                        affected_patterns=[],
                        affected_tables=[name],
                        cost_delta=None,
                        suggested_resolutions=[
                            f"Remove or rename the duplicate index design for '{name}'.",
                        ],
                    )
                )

    elif engine == "documentdb":
        seen_collections: dict[str, int] = {}
        for col in schema_output.get("collection_designs", []):
            name = col.get("collection_name", "")
            seen_collections[name] = seen_collections.get(name, 0) + 1

        for name, count in seen_collections.items():
            if count > 1:
                issues.append(
                    VerificationIssue(
                        category="consistency",
                        severity="error",
                        message=(
                            f"DocumentDB collection name '{name}' appears {count} times "
                            "in collection_designs. Collection names must be unique."
                        ),
                        affected_patterns=[],
                        affected_tables=[name],
                        cost_delta=None,
                        suggested_resolutions=[
                            f"Remove or rename the duplicate collection design for '{name}'.",
                        ],
                    )
                )

    return issues


def check_conflicts(
    reassignments: list[dict],
    target_outputs: dict[str, dict],
) -> list[VerificationIssue]:
    """Verify that every reassigned pattern is present in its target engine output.

    Each reassignment is a dict with keys:
    - pattern_id: str
    - target_engine: str

    A pattern is considered present in a target output if its pattern_id appears
    in any of: access_patterns[].pattern_id, index_designs[].pattern_id, or
    collection_designs[].pattern_id of the target engine's schema output.

    Returns one VerificationIssue per missing reassignment.
    """
    if not reassignments:
        return []

    issues: list[VerificationIssue] = []

    for reassignment in reassignments:
        pattern_id = reassignment.get("pattern_id", "")
        target_engine = reassignment.get("target_engine", "")

        target_output = target_outputs.get(target_engine)
        if target_output is None:
            issues.append(
                VerificationIssue(
                    category="conflict",
                    severity="warning",
                    message=(
                        f"Pattern '{pattern_id}' was reassigned to engine '{target_engine}', "
                        f"but no schema output exists for '{target_engine}' yet. "
                        "The target engine will need a redesign pass to incorporate this pattern."
                    ),
                    affected_patterns=[pattern_id],
                    affected_tables=[],
                    cost_delta=None,
                    suggested_resolutions=[
                        (  # nosemgrep: string-concat-in-list
                            f"Run schema design for '{target_engine}' to incorporate the "
                            "reassigned pattern."
                        ),
                        f"Check that '{target_engine}' is a valid engine identifier.",
                    ],
                )
            )
            continue

        present_ids = _collect_pattern_ids(target_output)
        if pattern_id not in present_ids:
            issues.append(
                VerificationIssue(
                    category="conflict",
                    severity="warning",
                    message=(
                        f"Pattern '{pattern_id}' was reassigned to engine '{target_engine}', "
                        f"but it is not yet present in '{target_engine}' schema output. "
                        "A redesign pass on the target engine is needed."
                    ),
                    affected_patterns=[pattern_id],
                    affected_tables=[],
                    cost_delta=None,
                    suggested_resolutions=[
                        (  # nosemgrep: string-concat-in-list
                            f"Re-run the schema design step for '{target_engine}' to incorporate "
                            f"the reassigned pattern '{pattern_id}'."
                        ),
                        f"Verify that the pattern ID '{pattern_id}' is correct.",
                    ],
                )
            )

    return issues


def check_cost_delta(
    previous_cost: float | None,
    current_cost: float | None,
    threshold: float = 0.20,
) -> list[VerificationIssue]:
    """Warn when the estimated monthly cost increases by more than *threshold* (default 20%).

    Returns a warning VerificationIssue with cost_delta set to the absolute
    cost increase in USD when the increase strictly exceeds the threshold.
    Returns an empty list for decreases, no change, or when either cost is None.
    """
    if previous_cost is None or current_cost is None:
        return []

    if previous_cost <= 0:
        # Cannot compute a meaningful percentage; skip to avoid division errors.
        return []

    delta = current_cost - previous_cost
    if delta <= 0:
        return []

    pct_increase = delta / previous_cost
    if pct_increase <= threshold:
        return []

    pct_display = round(pct_increase * 100, 1)
    threshold_display = round(threshold * 100, 0)
    return [
        VerificationIssue(
            category="cost",
            severity="warning",
            message=(  # nosemgrep: string-concat-in-list
                f"Estimated monthly cost increased by {pct_display}% "
                f"(${previous_cost:,.2f} → ${current_cost:,.2f}), "
                f"exceeding the {threshold_display:.0f}% threshold."
            ),
            affected_patterns=[],
            affected_tables=[],
            cost_delta=delta,
            suggested_resolutions=[
                "Review the new schema design for over-provisioned capacity or unnecessary indexes.",
                "Consider whether the cost increase is justified by the access pattern improvements.",
            ],
        )
    ]


def verify_revision(
    schema_output: dict,
    in_scope_query_ids: list[str],
    engine: str,
    reassignments: list[dict],
    target_outputs: dict[str, dict],
    previous_cost: float | None,
    current_cost: float | None,
    cost_threshold: float = 0.20,
) -> VerificationResult:
    """Run all verification checks and return a VerificationResult.

    Orchestrates:
    1. check_coverage — every in-scope query must be covered
    2. check_consistency — no duplicates or missing required fields
    3. check_conflicts — reassigned patterns must appear in target outputs
    4. check_cost_delta — cost increases beyond threshold produce a warning

    Issues with severity="error" go into hard_errors and set passed=False.
    Issues with severity="warning" go into warnings and do not affect passed.
    """
    all_issues: list[VerificationIssue] = []
    all_issues.extend(check_coverage(schema_output, in_scope_query_ids, engine))
    all_issues.extend(check_consistency(schema_output, engine))
    all_issues.extend(check_conflicts(reassignments, target_outputs))
    all_issues.extend(check_cost_delta(previous_cost, current_cost, threshold=cost_threshold))

    hard_errors = [i for i in all_issues if i.severity == "error"]
    warnings = [i for i in all_issues if i.severity == "warning"]

    return VerificationResult(
        passed=len(hard_errors) == 0,
        hard_errors=hard_errors,
        warnings=warnings,
    )
