"""The schema-design PE review for headless runs (``--llm-mode external``, #1059).

Bedrock mode reviews every design inside ``SchemaDesignRunner``: a PE reviewer
returns a ``PEReviewResult`` and its ``routing_flags`` are recorded in the
engine's design trace, where the post-schema router reads them
(``routing_flags_from_trace``). A headless run (Claude Code) gets the same
review as an external step, in the LLM seam shape:

1. :func:`prepare_pe_review_llm_input` writes one review request per design
   (per group for DynamoDB, which is designed in groups): the design, the
   queries it serves, the engine's PE-review skill and the answer contract (the
   engine's own ``PEReviewResult``, the one the Bedrock reviewer returns);
2. a review subagent follows the skill and writes the answer;
3. :func:`apply_pe_review_llm_output` validates the answer against that
   contract and records it in the design trace in the Bedrock trace shape:
   ``routing_flags`` at the top of ``design_trace.json``, or in
   ``design_trace_group_<G>.json``, which ``--merge`` consolidates under
   ``groups``.

A flag is evidence, never a destination: the post-schema router gates the
flagged engine and the shared scorer picks among the engines the assignment
keeps (ADR-031 Decision 11 Amendment 2). Nothing here routes.

One review per design, with no revision round: the review's change requests
and notes are recorded in the trace only. Engines whose review contract has no
``routing_flags`` (Aurora, ElastiCache) are skipped, because a headless review
of them could not change anything the pipeline reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from src.agents.prompt_framing import with_untrusted_notice
from src.agents.schema_design.design_trace import DesignTraceShapeError, write_design_trace
from src.contracts.model_flag import parse_model_flags
from src.storage.artifact_store import ArtifactStore

#: The trace key a recorded external review sits under.
REVIEW_KEY = "pe_review"


def _grouped(engine: str) -> bool:
    """DynamoDB is designed (and so reviewed) per group."""
    return engine == "dynamodb"


def _has_contract(engine: str) -> bool:
    try:
        review_contract(engine)
    except ValueError:
        return False
    return True


def review_contract(engine: str) -> type[BaseModel]:
    """The engine's ``PEReviewResult``: the answer the Bedrock reviewer returns."""
    # Imported on use: the schema agents pull in the Bedrock agent framework.
    if engine == "dynamodb":
        from src.contracts.dynamodb_pe_review import PEReviewResult as DynamoDBReview

        return DynamoDBReview
    if engine == "documentdb":
        from src.tools.schema.documentdb_schema_agent import PEReviewResult as DocumentDBReview

        return DocumentDBReview
    if engine == "opensearch":
        from src.tools.schema.opensearch_schema_agent import PEReviewResult as OpenSearchReview

        return OpenSearchReview
    if engine == "elasticache":
        from src.tools.schema.elasticache_schema_agent import PEReviewResult as CacheReview

        return CacheReview
    if engine == "aurora_postgresql":
        from src.tools.schema.aurora_postgresql_schema_agent import PEReviewResult as PgReview

        return PgReview
    if engine == "aurora_mysql":
        from src.tools.schema.aurora_mysql_schema_agent import PEReviewResult as MySQLReview

        return MySQLReview
    raise ValueError(f"no PE review contract for engine {engine!r}")


def can_flag(engine: str) -> bool:
    """Whether the engine's review contract carries ``routing_flags``."""
    return "routing_flags" in review_contract(engine).model_fields


def review_skill(engine: str) -> str:
    """The engine's PE-review skill, the one the Bedrock reviewer is prompted with."""
    return f"src/skills/{engine}-pe-review.md"


def _suffix(group: int | None) -> str:
    return "" if group is None else f"_group_{group}"


def request_key(db: str, job_id: str, engine: str, group: int | None) -> str:
    return f"{db}/{job_id}/llm_requests/pe_review_{engine}{_suffix(group)}.json"


def response_key(db: str, job_id: str, engine: str, group: int | None) -> str:
    return f"{db}/{job_id}/llm_responses/pe_review_{engine}{_suffix(group)}.json"


def trace_key(db: str, job_id: str, engine: str, version: int, group: int | None) -> str:
    base = f"{db}/{job_id}/schema-{engine}/v{version}"
    if group is None:
        return f"{base}/design_trace.json"
    return f"{base}/design_trace_group_{group}.json"


def _read(store: ArtifactStore, key: str) -> Any:
    try:
        return store.read_json(key)
    except (OSError, ValueError):
        return None


def _local_path(store: ArtifactStore, key: str) -> str:
    base = getattr(store, "base_dir", None)
    return str(Path(base) / key) if base else key


def is_recorded(
    store: ArtifactStore, db: str, job_id: str, engine: str, version: int, group: int | None
) -> bool:
    """Whether the design's trace already holds a review (its own, or one carried forward)."""
    trace = _read(store, trace_key(db, job_id, engine, version, group))
    return isinstance(trace, dict) and isinstance(trace.get(REVIEW_KEY), dict)


def _assigned_queries(
    store: ArtifactStore, db: str, job_id: str, version: int, engine: str
) -> list[str]:
    from src.storage.assignment_versioning import assignment_artifact_path

    assignment = _read(store, assignment_artifact_path(db, job_id, version)) or {}
    return sorted(
        qa["query_id"]
        for qa in assignment.get("query_assignments", [])
        if qa.get("assigned_engine") == engine and qa.get("in_scope", True)
    )


def _group_queries(group_input: dict) -> list[dict]:
    collector = group_input.get("collector_output") or {}
    return list((collector.get("queries") or {}).get("query_patterns") or [])


def _design_targets(
    store: ArtifactStore, db: str, job_id: str, version: int, engine: str
) -> tuple[list[dict], str | None]:
    """The designs to review for ``engine`` on ``version``, or a reason there are none.

    DynamoDB: one per group draft (the group's queries from its input).
    Every other engine: its ``schema_output.json`` (a skipped placeholder has
    nothing to review).
    """
    base = f"{db}/{job_id}/schema-{engine}/v{version}"
    if _grouped(engine):
        manifest = _read(store, f"{base}/groups_manifest.json")
        if not isinstance(manifest, dict):
            return [], f"not split on v{version}"
        targets = []
        for group in manifest.get("groups") or []:
            idx = int(group["group_index"])
            draft = _read(store, f"{base}/schema_draft_group_{idx}.json")
            if not isinstance(draft, dict):
                continue  # --status / --merge report a missing or invalid draft
            group_input = _read(store, f"{base}/input_group_{idx}.json") or {}
            queries = _group_queries(group_input)
            targets.append(
                {
                    "group": idx,
                    "design_key": f"{base}/schema_draft_group_{idx}.json",
                    "design": draft,
                    "query_ids": sorted(q["query_id"] for q in queries if q.get("query_id")),
                    "table_count": len(
                        (group_input.get("collector_output") or {}).get("tables") or []
                    )
                    or len({t for q in queries for t in q.get("tables_accessed") or []}),
                }
            )
        if not targets:
            return [], f"no readable group draft on v{version}"
        return targets, None
    output = _read(store, f"{base}/schema_output.json")
    if not isinstance(output, dict):
        return [], f"not designed on v{version}"
    if output.get("status") == "skipped":
        return [], "its design was skipped (nothing to review)"
    query_ids = _assigned_queries(store, db, job_id, version, engine)
    return [
        {
            "group": None,
            "design_key": f"{base}/schema_output.json",
            "design": output,
            "query_ids": query_ids,
            "table_count": None,
        }
    ], None


def _designed_engines(store: ArtifactStore, db: str, job_id: str, version: int) -> list[str]:
    """Engines with a ``schema-<engine>/v<version>/`` directory, in a stable order."""
    prefix = f"{db}/{job_id}/"
    engines: set[str] = set()
    for key in store.list_prefix(prefix):
        parts = str(key).replace(prefix, "").split("/")
        if len(parts) >= 3 and parts[0].startswith("schema-") and parts[1] == f"v{version}":
            engines.add(parts[0][len("schema-") :])
    return sorted(e for e in engines if _has_contract(e))


def _review_design(engine: str, design: dict) -> dict:
    """What the reviewer reads: the Bedrock reviewer's own view of the design."""
    if _grouped(engine):
        from src.tools.schema.dynamodb_schema_agent import _build_pe_input

        return _build_pe_input(design)
    return design


def _finalize_command(db: str, job_id: str, engine: str, group: int | None) -> str:
    group_arg = "" if group is None else f" --group {group}"
    return (
        f"uv run python scripts/run_pe_review.py --job-id {job_id} --db {db} "
        f"--engine {engine}{group_arg} --finalize"
    )


def prepare_pe_review_llm_input(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    engines: list[str] | None = None,
) -> dict:
    """Write a review request for every design on ``version`` that has none recorded.

    ``engines`` limits it to those engines (default: every engine designed on
    ``version``). Returns ``{"assignment_version", "reviews", "recorded",
    "skipped"}``: ``reviews`` lists one entry per request written (``engine``,
    ``group``, ``request``, ``response``, ``skill_prompt``, ``finalize``);
    ``recorded`` the designs already reviewed (a review carried forward with an
    unchanged design is not asked again, as on the Bedrock path); ``skipped``
    the engines with nothing to review and why.
    """
    from src.contracts.model_flag import ModelFlag

    reviews: list[dict] = []
    recorded: list[dict] = []
    skipped: list[dict] = []
    for engine in engines if engines is not None else _designed_engines(store, db, job_id, version):
        if not _has_contract(engine):
            skipped.append({"engine": engine, "reason": "unknown engine"})
            continue
        if not can_flag(engine):
            skipped.append(
                {
                    "engine": engine,
                    "reason": "its review contract has no routing_flags, so a review "
                    "could not change the assignment",
                }
            )
            continue
        targets, reason = _design_targets(store, db, job_id, version, engine)
        if reason:
            skipped.append({"engine": engine, "reason": reason})
            continue
        for target in targets:
            group = target["group"]
            if is_recorded(store, db, job_id, engine, version, group):
                recorded.append({"engine": engine, "group": group})
                continue
            design = target["design"]
            request = {
                "task": "pe_review",
                "engine": engine,
                "group": group,
                "assignment_version": version,
                "skill_prompt": review_skill(engine),
                "response_file": _local_path(store, response_key(db, job_id, engine, group)),
                "design_file": _local_path(store, target["design_key"]),
                # The queries this design serves: a flag names only these.
                "query_ids": target["query_ids"],
                "source_summary": {
                    "query_count": len(target["query_ids"]),
                    "table_count": (
                        target["table_count"]
                        if target["table_count"] is not None
                        else len(design.get("table_definitions") or design.get("collections") or [])
                    ),
                    "access_pattern_count": len(design.get("access_patterns") or []),
                },
                "flag_contract": ModelFlag.model_json_schema(),
                "output_schema": review_contract(engine).model_json_schema(),
                "design": _review_design(engine, design),
            }
            key = request_key(db, job_id, engine, group)
            # Indented, so the reviewer can page it with Read offset/limit. The
            # file says its content is data, not instructions (#1317).
            store.write_bytes(key, _dumps(with_untrusted_notice(request)).encode("utf-8"))
            reviews.append(
                {
                    "engine": engine,
                    "group": group,
                    "request": _local_path(store, key),
                    "response": request["response_file"],
                    "skill_prompt": request["skill_prompt"],
                    "finalize": _finalize_command(db, job_id, engine, group),
                }
            )
    return {
        "assignment_version": version,
        "reviews": reviews,
        "recorded": recorded,
        "skipped": skipped,
    }


def missing_reviews(
    store: ArtifactStore, db: str, job_id: str, version: int, engines: list[str]
) -> list[str]:
    """The designs of ``engines`` on ``version`` whose review is not recorded yet.

    Only engines whose review can flag; an engine with no design yet is left to
    the caller's missing-design check. A DynamoDB group is ``"dynamodb group <G>"``.
    """
    missing: list[str] = []
    for engine in engines:
        if not _has_contract(engine) or not can_flag(engine):
            continue
        targets, reason = _design_targets(store, db, job_id, version, engine)
        if reason:
            continue
        for target in targets:
            group = target["group"]
            if not is_recorded(store, db, job_id, engine, version, group):
                missing.append(engine if group is None else f"{engine} group {group}")
    return missing


def _dumps(data: dict) -> str:
    import json

    return json.dumps(data, indent=1, default=str) + "\n"


def _contract_errors(exc: ValidationError) -> list[str]:
    """``<location>: <message>`` per error, never the answer's own text (#143)."""
    errors = []
    for error in exc.errors():
        where = ".".join(str(p) for p in error.get("loc", ())) or "response"
        if error.get("type") == "extra_forbidden":
            where = where.rsplit(".", 1)[0] + ".<unknown key>" if "." in where else "<unknown key>"
        errors.append(f"{where}: {error.get('msg', error.get('type', 'invalid'))}")
    return errors


def apply_pe_review_llm_output(
    store: ArtifactStore,
    db: str,
    job_id: str,
    version: int,
    engine: str,
    group: int | None = None,
    failed: bool = False,
) -> dict:
    """Validate the review answer and record it in the design trace.

    ``failed``: the review subagent gave up; record that the design was
    reviewed without a usable answer (no flags), as the Bedrock runner records
    a review that errors (``pe_error``), so the reroute does not wait for it.

    Returns ``{"status": "complete", ...}`` with ``routing_flags`` (how many
    were recorded) and ``warnings`` (flags the router will ignore: off the
    flag contract, naming another engine, or naming a query the design does
    not serve); ``{"status": "validation_failed", "errors": [...]}`` when the
    answer is off its contract (nothing recorded); ``{"status": "error",
    "message": ...}`` when there is no answer or no request.
    """
    if not _has_contract(engine):
        return {"status": "error", "message": f"unknown engine {engine!r}"}
    if (_grouped(engine)) != (group is not None):
        need = "needs --group" if _grouped(engine) else "has no groups"
        return {"status": "error", "message": f"{engine} {need}"}
    req = _read(store, request_key(db, job_id, engine, group))
    if not isinstance(req, dict) or req.get("assignment_version") != version:
        return {
            "status": "error",
            "message": f"no review request for {engine}{_suffix(group)} on v{version}; "
            "run scripts/run_pe_review.py first",
        }
    if failed:
        return _record_failed_review(store, db, job_id, version, engine, group)
    answer = _read(store, response_key(db, job_id, engine, group))
    if not isinstance(answer, dict):
        return {
            "status": "error",
            "message": f"no readable review answer at {req['response_file']}",
        }
    try:
        review = review_contract(engine).model_validate(answer)
    except ValidationError as exc:
        return {"status": "validation_failed", "errors": _contract_errors(exc)}

    raw_flags = [dict(f) for f in getattr(review, "routing_flags", None) or []]
    flags, rejected = parse_model_flags(raw_flags)
    in_design = set(req.get("query_ids") or [])
    warnings = [f"{note}: off the flag contract; the router ignores it" for note in rejected]
    for index, flag in enumerate(flags):
        if flag.flagged_engine != engine:
            warnings.append(
                f"flag {index}: flagged_engine is not {engine}; a reviewer flags only its own engine"
            )
        outside = [q for q in flag.query_ids if q not in in_design]
        if outside:
            warnings.append(
                f"flag {index}: {len(outside)} query id(s) this design does not serve; "
                "the router ignores them"
            )

    dumped = review.model_dump(mode="json")
    verdict = str(dumped.get("verdict", ""))
    key = trace_key(db, job_id, engine, version, group)
    trace = _read(store, key)
    trace = dict(trace) if isinstance(trace, dict) else {}
    entries = [e for e in trace.get("trace") or [] if e.get("type") != "pe_review"]
    entries.append(
        {
            "type": "pe_review",
            "iteration": 0,
            "verdict": verdict,
            "change_requests": len(dumped.get("change_requests") or []),
            "summary": (dumped.get("summary") or "")[:200],
        }
    )
    trace["trace"] = entries
    # As the Bedrock runner records them: the raw entries, each checked by the
    # router on its own (parse_model_flags), so one bad entry never drops the rest.
    # Left out when there are none, as the Bedrock runner leaves it out.
    if raw_flags:
        trace["routing_flags"] = raw_flags
    else:
        trace.pop("routing_flags", None)
    trace[REVIEW_KEY] = {"source": "external", **dumped}
    try:
        write_design_trace(store, key, trace, lenient=True)
    except DesignTraceShapeError as exc:
        # The trace's routing flags are not ones the router can read; nothing is written,
        # so the step is retried (a fresh answer, or the trace corrected).
        return {"status": "validation_failed", "errors": exc.errors}

    status: dict = {
        "status": "complete",
        "engine": engine,
        "assignment_version": version,
        "verdict": verdict,
        "routing_flags": len(raw_flags),
        "trace": _local_path(store, key),
    }
    if group is not None:
        status["group"] = group
        status["next"] = "the DynamoDB --merge records the group's review in design_trace.json"
    if warnings:
        status["warnings"] = warnings
    return status


def _record_failed_review(
    store: ArtifactStore, db: str, job_id: str, version: int, engine: str, group: int | None
) -> dict:
    """Record a review that produced no usable answer: no flags, design unchanged."""
    message = "the review subagent returned no valid answer; the design is accepted as is"
    key = trace_key(db, job_id, engine, version, group)
    trace = _read(store, key)
    trace = dict(trace) if isinstance(trace, dict) else {}
    entries = [
        e for e in trace.get("trace") or [] if e.get("type") not in ("pe_review", "pe_error")
    ]
    entries.append({"type": "pe_error", "iteration": 0, "error": message})
    trace["trace"] = entries
    trace.pop("routing_flags", None)
    trace[REVIEW_KEY] = {"source": "external", "error": message}
    try:
        write_design_trace(store, key, trace, lenient=True)
    except DesignTraceShapeError as exc:
        return {"status": "validation_failed", "errors": exc.errors}
    status: dict = {
        "status": "complete",
        "engine": engine,
        "assignment_version": version,
        "review_failed": True,
        "routing_flags": 0,
        "trace": _local_path(store, key),
    }
    if group is not None:
        status["group"] = group
    return status
