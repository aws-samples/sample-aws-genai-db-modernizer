"""Keep the reality-check executive summary consistent with the final records (#236).

The summary is written before the steps that can still change the outcome: the LLM
corrections, the re-run Aurora absorption, the sanity sweep, the customer-override
restore and the record reconciliation. In external mode the LLM writes it from the
deterministic preview, before its own corrections are applied. So it can name an
engine as part of the architecture that the final records consolidate away, or call
an engine eliminated that keeps queries.

:func:`finalize_executive_summary` runs once the records are final. It checks the
summary with :func:`check_summary_against_records`: every engine the summary names
as kept must keep queries, and every engine it names as eliminated must have none.
On a contradiction the customer sees :func:`build_final_summary`, a short narrative
built from the final records, and the LLM text is kept for audit, as synthesis does
for its own summary (#205).

The check is a heuristic over clauses, not a parser. It errs toward rejecting: a
rejected summary is replaced by a correct deterministic one, while an accepted wrong
one reaches the customer.
"""

from __future__ import annotations

import re

from src.shared.engine_names import display_engine

# Engine names as a summary writes them, longest first so "Aurora MySQL" wins over
# "Aurora". The bare "Aurora" means any Aurora engine.
_AURORA_FAMILY = "aurora*"
_ENGINE_ALIASES: list[tuple[str, str]] = [
    (r"aurora\s+postgre(?:sql|s)", "aurora_postgresql"),
    (r"aurora\s+mysql", "aurora_mysql"),
    (r"aurora", _AURORA_FAMILY),
    (r"dynamodb", "dynamodb"),
    (r"documentdb", "documentdb"),
    (r"opensearch", "opensearch"),
    (r"elasticache|redis|valkey|memcached", "elasticache"),
    (r"neptune", "neptune"),
    (r"keyspaces", "keyspaces"),
]
_ENGINE_RE = re.compile(
    "|".join(f"(?P<e{i}>\\b(?:{pattern})\\b)" for i, (pattern, _) in enumerate(_ENGINE_ALIASES)),
    re.IGNORECASE,
)

# Clause boundaries: punctuation and conjunctions that start a new statement.
_CLAUSE_SPLIT = re.compile(
    r"[.;:!?,()]|\b(?:while|whereas|but|so|because|which|although)\b", re.IGNORECASE
)

# An engine after one of these, in the same clause, is described as eliminated
# ("removing the DocumentDB cluster", "DynamoDB replaces DocumentDB").
_FORWARD_CUES = re.compile(
    r"\b(?:remov(?:e|es|ed|ing)|eliminat\w*|drop(?:s|ped|ping)?|retir(?:e|es|ed|ing)"
    r"|decommission\w*|replac(?:e|es|ed|ing)|absorb(?:s|ing)?|instead\s+of|rather\s+than"
    r"|without|no\s+(?:separate|dedicated|need\s+for|longer\s+need\w*)|consolidation\s+of)\b",
    re.IGNORECASE,
)
# An engine before one of these is described as eliminated ("DocumentDB is retired",
# "OpenSearch folds into Aurora", "a dedicated OpenSearch domain would not justify").
_BACKWARD_CUES = re.compile(
    r"\b(?:(?:is|are|was|were|be|been|being|gets?|got)\s+(?:\w+ly\s+)?"
    r"(?:removed|eliminated|dropped|retired|decommissioned|replaced|absorbed|consolidated"
    r"|folded|merged|not\s+needed|no\s+longer\s+needed|unnecessary|not\s+justified)"
    r"|(?:consolidat|fold|merg|absorb)\w*\s+(?:in)?to|(?:consolidat|fold|merg)\w*\s+onto"
    r"|would\s+(?:not|have)|no\s+longer|not\s+needed|unnecessary)\b",
    re.IGNORECASE,
)
# Statements that an engine carries work; they end the reach of an elimination cue.
_KEEP_CUES = re.compile(
    r"\b(?:stay(?:s|ed)?|remain(?:s|ed|ing)?|keep(?:s|ing)?|kept|retain\w*"
    r"|serv(?:e|es|ed|ing)|handl\w*|cover\w*|takes?|carr(?:y|ies)|owns?|hosts?|primary)\b",
    re.IGNORECASE,
)
# A clause about part of an engine's workload does not eliminate the engine.
_PARTIAL = re.compile(
    r"\b(?:partial\w*|partly|some\s+of|part\s+of|most\s+of|reduced|fewer)\b", re.IGNORECASE
)
# "the 6 DocumentDB queries consolidate into DynamoDB": queries move, the engine is
# not named as kept or eliminated.
_QUERY_LEVEL_AFTER = re.compile(r"^(?:'s)?\s+(?:[\w-]+\s+){0,2}?quer(?:y|ies)\b", re.IGNORECASE)
_SOURCE_BEFORE = re.compile(r"\b(?:from|off)\s+(?:(?:the|amazon|a|an)\s+)*$", re.IGNORECASE)
# The destination of a move: kept, whatever cue the clause carries.
_TARGET_BEFORE = re.compile(
    r"\b(?:into|onto|to|by|on|in|with|via)\s+(?:(?:the|a|an|amazon|single|one)\s+)*$",
    re.IGNORECASE,
)


def _engine_key(match: re.Match) -> str:
    for i, (_, key) in enumerate(_ENGINE_ALIASES):
        if match.group(f"e{i}"):
            return key
    raise AssertionError("unreachable: engine regex matched no alias")


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSE_SPLIT.split(text) if c and c.strip()]


def _keep_between(clause: str, start: int, end: int) -> bool:
    return bool(_KEEP_CUES.search(clause, start, end)) if start < end else False


def _claims(summary: str) -> tuple[set[str], set[str]]:
    """Engines the summary names as kept and as eliminated."""
    kept: set[str] = set()
    eliminated: set[str] = set()
    for clause in _clauses(summary):
        forward = list(_FORWARD_CUES.finditer(clause))
        backward = list(_BACKWARD_CUES.finditer(clause))
        partial = bool(_PARTIAL.search(clause))
        for m in _ENGINE_RE.finditer(clause):
            engine = _engine_key(m)
            before, after = clause[: m.start()], clause[m.end() :]
            if _QUERY_LEVEL_AFTER.match(after) or _SOURCE_BEFORE.search(before):
                continue
            if _TARGET_BEFORE.search(before):
                kept.add(engine)
                continue
            gone = not partial and (
                any(
                    f.end() <= m.start() and not _keep_between(clause, f.end(), m.start())
                    for f in forward
                )
                or any(
                    b.start() >= m.end() and not _keep_between(clause, m.end(), b.start())
                    for b in backward
                )
            )
            (eliminated if gone else kept).add(engine)
    return kept, eliminated


def final_outcome(
    before_distribution: dict[str, int],
    after_distribution: dict[str, int],
    consolidations: list[dict],
) -> tuple[dict[str, int], set[str]]:
    """Engines that keep queries (with their counts) and engines consolidated away."""
    fully_moved = {c["from_engine"] for c in consolidations if c.get("action", "full") == "full"}
    kept = {e: n for e, n in after_distribution.items() if n > 0 and e not in fully_moved}
    eliminated = (set(before_distribution) | fully_moved) - set(kept)
    return kept, eliminated


def _family(engine: str, engines) -> list[str]:
    if engine == _AURORA_FAMILY:
        return [e for e in engines if e.startswith("aurora")]
    return [engine] if engine in engines else []


def _queries(n: int) -> str:
    return f"{n} {'query' if n == 1 else 'queries'}"


def _move(n: int) -> str:
    return "moves" if n == 1 else "move"


def _moved_count(engine: str, consolidations: list[dict]) -> int:
    return sum(c.get("query_count", 0) for c in consolidations if c["from_engine"] == engine)


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _moves_from(engine: str, consolidations: list[dict]) -> list[str]:
    targets: dict[str, int] = {}
    for c in consolidations:
        if c["from_engine"] == engine:
            targets[c["to_engine"]] = targets.get(c["to_engine"], 0) + c.get("query_count", 0)
    return [display_engine(t) for t in sorted(targets, key=lambda t: (-targets[t], t))]


def check_summary_against_records(
    summary: str,
    before_distribution: dict[str, int],
    after_distribution: dict[str, int],
    consolidations: list[dict],
) -> list[str]:
    """Contradictions between ``summary`` and the final records; empty when consistent.

    An engine named as kept must keep queries; an engine named as eliminated must
    have none. An engine named both ways passes when the records eliminate it (the
    summary says where its queries went, then that it is gone), and fails when they
    keep it.
    """
    kept, eliminated = final_outcome(before_distribution, after_distribution, consolidations)
    said_kept, said_gone = _claims(summary or "")
    findings: list[str] = []
    for engine in sorted(said_gone):
        for real in _family(engine, kept) or []:
            findings.append(
                f"The summary describes {display_engine(real)} as eliminated, but the final "
                f"assignment keeps {_queries(kept[real])} on it."
            )
    for engine in sorted(said_kept - said_gone):
        if engine == _AURORA_FAMILY:
            if _family(engine, kept) or not _family(engine, eliminated):
                continue
            names = _family(engine, eliminated)
        elif engine in kept:
            continue
        else:
            names = [engine]
        for real in names:
            targets = _moves_from(real, consolidations)
            where = f" (its queries move to {_join(targets)})" if targets else ""
            findings.append(
                f"The summary describes {display_engine(real)} as part of the final "
                f"architecture, but the final assignment leaves it no queries{where}."
            )
    return findings


def build_final_summary(
    before_distribution: dict[str, int],
    after_distribution: dict[str, int],
    consolidations: list[dict],
) -> str:
    """A short, customer-readable summary of the final records.

    Names engines by display name and states query counts only: no costs, no
    confidence figures.
    """
    kept, eliminated = final_outcome(before_distribution, after_distribution, consolidations)
    total = sum(after_distribution.values())
    evaluated = [display_engine(e) for e in before_distribution]
    ranked = sorted(kept, key=lambda e: (-kept[e], e))
    sentences = [
        f"The assessment evaluated {_queries(total)} across {len(evaluated)} "
        f"{'engine' if len(evaluated) == 1 else 'engines'} ({_join(evaluated)})."
    ]
    if len(ranked) == 1:
        sentences.append(f"The final architecture runs all of them on {display_engine(ranked[0])}.")
    elif ranked:
        listed = [f"{display_engine(e)} ({_queries(kept[e])})" for e in ranked]
        sentences.append(f"The final architecture keeps {_join(listed)}.")
    for engine in sorted(eliminated, key=lambda e: (-before_distribution.get(e, 0), e)):
        moved = _moved_count(engine, consolidations)
        targets = _moves_from(engine, consolidations)
        if not moved or not targets:
            continue
        sentences.append(
            f"{display_engine(engine)} is consolidated away: its {_queries(moved)} "
            f"{_move(moved)} to {_join(targets)}."
        )
    for engine in ranked:
        moved = _moved_count(engine, consolidations)
        targets = _moves_from(engine, consolidations)
        if not moved or not targets:
            continue
        sentences.append(
            f"{display_engine(engine)} is reduced in scope: {moved} of its queries "
            f"{_move(moved)} to {_join(targets)}."
        )
    if not consolidations:
        sentences.append("No engine is consolidated: each engine keeps the queries it serves.")
    return " ".join(sentences)


def finalize_executive_summary(result: dict) -> None:
    """Make ``result["executive_summary"]`` describe the final records.

    Call once the consolidation records and ``after_distribution`` are final. Sets
    ``executive_summary_llm`` (the summary as received, for audit),
    ``executive_summary_validation_warnings`` and ``executive_summary_source``:
    ``llm`` when the summary is consistent with the records, ``deterministic_fallback``
    when it contradicts them and was replaced by :func:`build_final_summary`, None
    when there is no summary. Mutates ``result``.
    """
    # Re-running keeps the original LLM text, not an earlier fallback.
    llm_summary = result.get("executive_summary_llm") or result.get("executive_summary")
    result["executive_summary_llm"] = llm_summary
    if not llm_summary:
        result["executive_summary"] = None
        result["executive_summary_source"] = None
        result["executive_summary_validation_warnings"] = []
        return
    before = result["before_distribution"]
    after = result["after_distribution"]
    consolidations = result["consolidations"]
    findings = check_summary_against_records(llm_summary, before, after, consolidations)
    result["executive_summary_validation_warnings"] = findings
    if findings:
        for finding in findings:
            print(f"[reality-check] WARNING: {finding}")
        print("[reality-check] Executive summary replaced by one built from the final records")
        result["executive_summary"] = build_final_summary(before, after, consolidations)
        result["executive_summary_source"] = "deterministic_fallback"
    else:
        result["executive_summary"] = llm_summary
        result["executive_summary_source"] = "llm"
