#!/usr/bin/env python3
"""A runnable tour of the modernizer used as a Python library, through ``assess()``.

Run it from the repository root (after ``uv sync``):

    uv run python docs/examples/library/demo_assess.py
    uv run python docs/examples/library/demo_assess.py --sample discourse
    uv run python docs/examples/library/demo_assess.py --target-databases dynamodb,elasticache
    uv run python docs/examples/library/demo_assess.py --llm-mode bedrock --run-through synthesis

The default, ``--llm-mode none``, calls no model: it is free, offline and takes seconds.
``--llm-mode bedrock`` calls Amazon Bedrock with your AWS credentials and costs money
(see README.md). Every figure printed below is read from the result's stable surface
(``docs/contracts/assess.md``), except where a line says it reads an internal artifact.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import textwrap
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pydantic import ValidationError  # noqa: E402

from src.assessment import (  # noqa: E402
    AssessmentOptions,
    AssessmentRequest,
    AssessmentResult,
    CollectionLoadError,
    assess,
    load_collection,
)
from src.assessment.runner import PROGRESS_LOGGER  # noqa: E402

SAMPLES = ("wordpress", "discourse", "trailgear")
#: The deepest ``run_through`` each mode runs by default.
DEFAULT_RUN_THROUGH = {"none": "reality_check", "bedrock": "synthesis"}
MODEL_PHASES = ("schema_design", "synthesis")
WIDTH = 100


def short(text: str | None, width: int = WIDTH - 6) -> str:
    """One customer-facing sentence, shortened to fit a line (the demo only shows it)."""
    return textwrap.shorten(text or "", width=width, placeholder=" ...")


def qid(query_id: str) -> str:
    return query_id[:12]


def count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}es" if noun.endswith("x") else f"{n} {noun}s"


def section(number: int, title: str) -> None:
    print(f"\n{number}. {title}")


def line(text: str = "") -> None:
    print(f"   {text}")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sample", choices=SAMPLES, default="wordpress")
    parser.add_argument(
        "--llm-mode",
        choices=("none", "bedrock"),
        default="none",
        help="none (default): no model, free and offline; bedrock: Amazon Bedrock, costs money",
    )
    parser.add_argument(
        "--target-databases",
        default=None,
        help="Engine scope, comma-separated catalog ids (for example dynamodb,elasticache)",
    )
    parser.add_argument(
        "--run-through",
        choices=("analysis", "reality_check", "schema_design", "synthesis"),
        default=None,
        help="How far to run (default: reality_check with none, synthesis with bedrock)",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Also print the phases' progress lines (the src.assessment.progress logger) to stderr",
    )
    args = parser.parse_args(argv)
    args.run_through = args.run_through or DEFAULT_RUN_THROUGH[args.llm_mode]
    if args.llm_mode == "none" and args.run_through in MODEL_PHASES:
        parser.error(f"--run-through {args.run_through} needs a model: add --llm-mode bedrock")
    args.engines = (
        [e for e in args.target_databases.split(",") if e.strip()]
        if args.target_databases is not None
        else None
    )
    try:  # the library's own check of the engine scope, before anything runs
        AssessmentOptions(target_engines=args.engines)
    except ValidationError as error:
        parser.error(str(error.errors()[0]["msg"]).removeprefix("Value error, "))
    return args


# --------------------------------------------------------------------------- sections


def show_loading(sample: str) -> dict[str, Any]:
    path = REPO / "docs" / "examples" / sample / f"{sample}.zip"
    section(1, "Load the collection: load_collection()")
    collection = load_collection(path)
    line(
        f"{path.relative_to(REPO)}: {len(collection['tables'])} tables, "
        f"{len(collection['queries'])} statements"
    )
    try:
        load_collection("collection_output\nno JSON here")
    except CollectionLoadError as error:
        line(f"Text that is not a collection raises CollectionLoadError: {short(str(error), 50)}")
    return collection


def show_analysis(sample: str, collection: dict[str, Any], engines: list[str] | None) -> None:
    section(2, 'Scores only: run_through="analysis" (never a model)')
    request = AssessmentRequest(
        database_name=sample,
        collection=collection,
        options=AssessmentOptions(run_through="analysis", target_engines=engines),
    )
    first, again = assess(request), assess(request)
    placed = sum(1 for s in first.statements if s.engine is not None)
    line(
        f"label {first.label}: {first.summary.statement_count} statements scored, "
        f"{placed} placed"
    )
    # The first statement scored on the most engines.
    statement = max(first.statements, key=lambda s: len(s.fit))
    fit = ", ".join(f"{engine} {score}" for engine, score in sorted(statement.fit.items()))
    line(f"statement {qid(statement.query_id)} ({statement.query_type}): fit {fit}")
    same = first.model_dump_json() == again.model_dump_json()
    line(f"A repeat run gives a byte-identical result: {'yes' if same else 'NO'}")


def show_placement(result: AssessmentResult) -> None:
    summary = result.summary
    section(3, 'Placement: run_through="reality_check" (and every later phase)')
    per_engine = ", ".join(f"{e} {n}" for e, n in summary.statements_per_engine.items())
    line(
        f"label {result.label}, assignment v{summary.assignment_version}, "
        f"{summary.statement_count} statements: {per_engine or 'none placed'}"
    )
    if summary.relational_engine is not None:
        line(f"relational engine: {summary.relational_engine.engine}")
    line("engines:")
    for engine in summary.engines:
        into = f" into {engine.consolidated_into}" if engine.consolidated_into else ""
        line(f"  {engine.engine:<18} {engine.role:<13} {engine.statement_count:>4}{into}")
    for move in summary.consolidations:
        line(f"consolidated: {move.from_engine} -> {move.to_engine} ({move.query_count})")
    kinds = Counter(group.kind for group in result.groups)
    line(f"groups that move together: {', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}")
    owned = [t for t in summary.tables if t.owner]
    line(f"tables with an owner: {len(owned)}")
    for table in owned[:2]:
        extra = "".join(
            [
                f", read by {', '.join(table.also_read_by)}" if table.also_read_by else "",
                f", cached by {table.cached_by}" if table.cached_by else "",
            ]
        )
        line(f"  {table.table_id}: {table.owner} ({table.owner_basis}){extra}")


def show_reasons(result: AssessmentResult) -> None:
    section(4, "Why each statement is where it is: reason_code, matched_patterns, reason_text")
    codes = Counter(s.reason_code for s in result.statements if s.reason_code)
    line("reason codes: " + ", ".join(f"{c} {n}" for c, n in codes.most_common()))
    shown: set[str] = set()
    for statement in result.statements:
        if statement.engine is None or statement.reason_code in shown or len(shown) == 3:
            continue
        shown.add(str(statement.reason_code))
        patterns = ", ".join(p.name for p in statement.matched_patterns) or "no pattern"
        line(
            short(
                f"  {qid(statement.query_id)} {statement.engine} [{statement.reason_code}] "
                f"{patterns}: {statement.reason_text}"
            )
        )
    if result.options.deterministic is False:
        flagged = codes.get("capability_flag", 0)
        line(f"moved by the model's consolidation review (capability_flag): {flagged}")


def show_scope(result: AssessmentResult) -> None:
    section(5, "Engine scope: options.target_engines")
    scope = result.options.target_engines
    line(f"scope: {', '.join(scope) if scope else 'none (every engine the workload warrants)'}")
    line(f"unservable statements: {result.summary.unservable_count}")
    for statement in result.statements:
        if statement.reason_code == "unservable":
            line(f"  e.g. {qid(statement.query_id)}: {short(statement.unservable_reason, 80)}")
            break


def show_designs(result: AssessmentResult, run_through: str) -> None:
    section(6, 'Schema design and the stable design block: run_through="schema_design"')
    if run_through not in MODEL_PHASES:
        try:
            AssessmentOptions(run_through="schema_design")
        except ValidationError as error:
            message = str(error.errors()[0]["msg"]).removeprefix("Value error, ")
            line("it needs a model: with deterministic=True assess() refuses it up front:")
            line(f"  {short(message, 90)}")
        line("run with --llm-mode bedrock --run-through schema_design (or synthesis) to see it")
        return
    summary = result.summary
    line(f"label {result.label}, designed engines: {', '.join(summary.designed_engines) or 'none'}")
    if summary.design_note:
        line(f"note: {short(summary.design_note)}")
    for design in result.designs or []:
        status = "valid" if design.validation_passed else "FAILED its checks"
        if design.engine == "dynamodb":
            gsis = sum(len(t.gsis) for t in design.tables)
            detail = f"{count(len(design.tables), 'table')}, {count(gsis, 'GSI')}"
            if design.tables:
                table = design.tables[0]
                sort = f", SK {table.sort_key.name}" if table.sort_key else ""
                detail += f"; e.g. {table.name}: PK {table.partition_key.name}{sort}"
        elif design.engine in ("aurora_mysql", "aurora_postgresql"):
            indexes = sum(len(t.indexes) for t in design.tables)
            detail = f"{count(len(design.tables), 'table')}, {count(indexes, 'index')}"
        elif design.engine == "documentdb":
            detail = count(len(design.collections), "collection")
        elif design.engine == "opensearch":
            detail = (
                f"{count(len(design.indexes), 'index')}, "
                f"{count(len(design.data_streams), 'data stream')}"
            )
            if design.indexes:
                index = design.indexes[0]
                detail += f"; e.g. {index.name}: {count(len(index.fields), 'field')}"
        elif design.engine == "elasticache":
            detail = count(len(design.keys), "key")
            if design.keys:
                detail += f"; e.g. {design.keys[0].pattern}"
        else:  # a new engine is a MINOR change: read the block with a default branch
            detail = "a design"
        line(f"  {design.engine:<18} {status}: {short(detail, 70)}")


def show_post_schema(result: AssessmentResult, run_through: str) -> None:
    section(7, "Post-schema routing: reroutes and terminal statements")
    if run_through not in MODEL_PHASES:
        line("runs after schema design (needs a model); nothing to show at this stage")
        return
    moved = [s for s in result.statements if s.reason_code == "post_schema_reroute"]
    line(f"moved after a design flagged them (post_schema_reroute): {len(moved)}")
    for statement in moved[:1]:
        line(f"  e.g. {qid(statement.query_id)} now on {statement.engine}")
    line(f"left terminal (post_schema_terminal_count): {result.summary.post_schema_terminal_count}")
    for statement in result.statements:
        marker = statement.post_schema_terminal
        if marker is not None:
            line(
                f"  e.g. {qid(statement.query_id)} stays on {marker.engine} "
                f"({marker.flag_reason}, {marker.cause}): {short(marker.reason, 40)}"
            )
            break


def show_report(result: AssessmentResult, run_through: str) -> None:
    section(8, 'The report: run_through="synthesis"')
    if run_through != "synthesis":
        line("the report (and its cost figures) comes from synthesis, which needs a model")
        return
    # artifacts.synthesis_report is an internal contract: no compatibility promise.
    report = (result.artifacts.model_dump() if result.artifacts else {}).get("synthesis_report")
    facts = (report or {}).get("facts") or {}
    total = facts.get("engine_cost_total_usd")
    line("(read from artifacts.synthesis_report, an internal contract)")
    if total is not None:
        line(f"monthly cost of the kept engines: ${total:,.2f}")
    line(f"cost label: {facts.get('cost_scope_label')}")
    summary = (report or {}).get("summary")
    if isinstance(summary, str) and summary.strip():
        line(f"executive summary: {short(summary, 75)}")


def show_usage(result: AssessmentResult, llm_mode: str) -> None:
    section(9, "Model usage and its cost: artifacts.model_usage + scripts/usage_cost.py")
    # The run's token counts by phase and model: an internal record, no compatibility promise.
    usage = (result.artifacts.model_dump() if result.artifacts else {}).get("model_usage")
    if not usage:
        line(f"no model call, so no usage record (--llm-mode {llm_mode})")
        return
    from scripts.usage_cost import MissingPriceError, compute, load_prices

    totals = usage["totals"]
    line(
        f"{totals['calls']} model calls, {totals['inputTokens']:,} input and "
        f"{totals['outputTokens']:,} output tokens"
    )
    try:
        cost = compute(usage, load_prices())
    except MissingPriceError as error:
        line(f"no cost: {error.args[0]}")
        return
    for phase, models in cost["by_phase"].items():
        line(f"  {phase}: ${models['total']:.2f}")
    line(f"total: ${cost['total_usd']:.2f}")


def show_json(result: AssessmentResult) -> None:
    from jsonschema import Draft202012Validator

    section(10, "The result as JSON, checked against the published schema")
    schema_path = REPO / "docs" / "contracts" / "schemas" / "assess_result.v1.schema.json"
    # The stable surface only: the opt-in artifacts block has no compatibility promise.
    payload = result.model_copy(update={"artifacts": None}).model_dump(mode="json")
    errors = list(Draft202012Validator(json.loads(schema_path.read_text())).iter_errors(payload))
    line(
        f"stable surface {len(json.dumps(payload)):,} bytes, contract {result.contract_version}: "
        f"{'valid' if not errors else f'{len(errors)} schema errors'} against "
        f"{schema_path.relative_to(REPO)}"
    )


# --------------------------------------------------------------------------- main


def show_progress() -> tuple[logging.Handler, int]:
    """assess() writes nothing to stdout; its phases' progress lines go to a logger.
    Returns the handler added and the logger's previous level, to restore both."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("   | %(message)s"))
    progress = logging.getLogger(PROGRESS_LOGGER)
    previous = progress.level
    progress.addHandler(handler)
    progress.setLevel(logging.INFO)
    return handler, previous


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    print(
        f"assess() library demo: {args.sample}, --llm-mode {args.llm_mode}, "
        f"run through {args.run_through}, scope {args.target_databases or 'every engine'}"
    )
    shown = show_progress() if args.progress else None
    try:
        run(args)
    finally:
        if shown is not None:
            handler, previous = shown
            progress = logging.getLogger(PROGRESS_LOGGER)
            progress.removeHandler(handler)
            progress.setLevel(previous)
    print(f"\ndone in {time.monotonic() - started:.1f}s")
    return 0


def run(args: argparse.Namespace) -> None:
    deterministic = args.llm_mode == "none"
    collection = show_loading(args.sample)
    show_analysis(args.sample, collection, args.engines)
    if not deterministic:
        print(
            f"\nRunning assess() through {args.run_through} with a model: this takes minutes "
            "(18 and 34 in two runs through synthesis on wordpress); --progress shows each phase.",
            flush=True,
        )
    result = assess(
        AssessmentRequest(
            database_name=args.sample,
            collection=collection,
            options=AssessmentOptions(
                run_through=args.run_through,
                target_engines=args.engines,
                deterministic=deterministic,
                # The report and the usage record are in the artifacts block.
                include_artifacts=not deterministic,
            ),
        )
    )
    print(
        f"\nlibrary {result.library_version}, contract {result.contract_version}, "
        f"ruleset {result.ruleset_hash[:19]}"
    )
    if args.run_through != "analysis":
        show_placement(result)
        show_reasons(result)
    show_scope(result)
    show_designs(result, args.run_through)
    show_post_schema(result, args.run_through)
    show_report(result, args.run_through)
    show_usage(result, args.llm_mode)
    show_json(result)


if __name__ == "__main__":
    sys.exit(main())
