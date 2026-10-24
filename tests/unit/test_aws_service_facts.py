"""Guard against stale AWS service facts in prompts, skills, commands and code (#627).

Each entry pairs a pattern that used to appear in a prompt, skill or catalog
with the current fact and the AWS documentation page it comes from. A match
means a stale claim came back, in a file a model reads (or one that renders
into a deliverable).
"""

from __future__ import annotations

import os
import re
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Where model-facing and customer-facing text lives. Test fixtures under
# tests/ are recorded outputs, not sources, so they are not scanned.
SCANNED_ROOTS = ("src", ".claude/commands", "docs", "scripts")
SCANNED_SUFFIXES = {".py", ".md", ".j2", ".json", ".js"}
SKIPPED_PARTS = {"node_modules", "build", "__pycache__", ".venv", "site"}

STALE_FACTS: list[tuple[str, str, str]] = [
    (
        r"transactions?[^.\n]{0,40}\b25[ -]items?\b|\b25[ -]item limit",
        "DynamoDB transactions take up to 100 actions (4 MB aggregate)",
        "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/transaction-apis.html",
    ),
    (
        r"TTL[^.\n]{0,60}48.hour",
        "DynamoDB TTL deletes expired items typically within a few days",
        "https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/howitworks-ttl.html",
    ),
    (
        # Wildcard ($**) indexes really are unsupported on every version.
        # "not supported on elastic clusters" is true, so a gap mentioning
        # "elastic" does not count.
        r"\$(graphLookup|facet|setWindowFields|stdDev\w*)(?:(?!elastic)[^.\n]){0,60}not supported(?!\s+on\s+elastic)",
        "DocumentDB 8.0 supports $graphLookup, $facet (8.0.2+) and $setWindowFields (8.0.2+)",
        "https://docs.aws.amazon.com/documentdb/latest/developerguide/mongo-apis.html",
    ),
    (
        r"\b(lacks|no|not support\w*)\b[^.\n]{0,20}correlated subqueries",
        "DocumentDB 8.0.2+ supports correlated $lookup with let/pipeline",
        "https://docs.aws.amazon.com/documentdb/latest/developerguide/lookup.html",
    ),
    (
        r"loss of multi-document ACID",
        "DocumentDB 4.0+ runs multi-document transactions",
        "https://docs.aws.amazon.com/documentdb/latest/developerguide/transactions.html",
    ),
    (
        r"beyond a few TB",
        "Aurora volumes reach 128 or 256 TiB; DocumentDB 128 or 256 TiB, elastic clusters 4 PiB",
        "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_Limits.html",
    ),
    (
        r"no inverted index",
        "DocumentDB text indexes (5.0+) are inverted indexes",
        "https://docs.aws.amazon.com/documentdb/latest/developerguide/text-search.html",
    ),
    (
        r"relevance[- ]tuned|no relevance tuning",
        "DocumentDB text search supports field weights and sorting by score",
        "https://docs.aws.amazon.com/documentdb/latest/developerguide/text-search.html",
    ),
    (
        # #1483: a GSI's key values need not be unique and an application-side
        # check races, so neither enforces a DynamoDB unique attribute.
        r"uniqueness[^.\n]{0,40}at the application layer|single unique identifier \+ GSI",
        "DynamoDB enforces a non-key unique attribute with a sentinel item in the same "
        "TransactWriteItems, conditioned on attribute_not_exists",
        "https://aws.amazon.com/blogs/database/simulating-amazon-dynamodb-unique-constraints-using-transactions/",
    ),
    (
        r"relational \(MySQL or PostgreSQL\)",
        "Sources also include MariaDB, SQL Server and Oracle",
        "src/agents/collector/handler.py",
    ),
]


@cache
def _scanned_files() -> tuple[Path, ...]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT / root):
            # Prune while walking: src/ui/node_modules alone is tens of
            # thousands of files in a built worktree.
            dirnames[:] = [d for d in dirnames if d not in SKIPPED_PARTS]
            for filename in filenames:
                path = Path(dirpath) / filename
                if path.suffix in SCANNED_SUFFIXES:
                    files.append(path)
    return tuple(sorted(files))


# Adjacent Python string literals ("... " "...") split one sentence across
# lines; joining them lets a pattern match a claim the catalog wraps.
_LITERAL_BREAK = re.compile(r"""(["'])\s*\n\s*(?:#[^\n]*\n\s*)*\1""")


@cache
def _normalized_lines(path: Path) -> tuple[str, ...]:
    """The file's lines, plus each paragraph with literals joined and whitespace collapsed."""
    text = path.read_text(errors="ignore")
    joined = _LITERAL_BREAK.sub("", text) if path.suffix == ".py" else text
    paragraphs = [" ".join(block.split()) for block in re.split(r"\n\s*\n", joined)]
    return tuple(text.splitlines()) + tuple(paragraphs)


def test_scan_covers_the_files_that_carried_stale_facts():
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _scanned_files()}
    for expected in (
        ".claude/commands/reality-check.md",
        "src/agents/referee/consolidation_validator.py",
        "src/skills/dynamodb-data-modeling.md",
        "src/skills/documentdb-data-modeling.md",
        "src/skills/documentdb-pe-review.md",
        "src/tools/analysis/documentdb_pattern_catalog.py",
    ):
        assert expected in scanned


@pytest.mark.parametrize(("pattern", "current_fact", "source"), STALE_FACTS)
def test_stale_fact_does_not_reappear(pattern: str, current_fact: str, source: str):
    regex = re.compile(pattern, re.IGNORECASE)
    hits = sorted(
        {
            f"{path.relative_to(REPO_ROOT)}: {line.strip()[:160]}"
            for path in _scanned_files()
            for line in _normalized_lines(path)
            if regex.search(line)
        }
    )
    assert not hits, f"Stale fact (now: {current_fact}; see {source}):\n" + "\n".join(hits)


def test_dynamodb_transaction_limit_is_stated_once_for_both_modes():
    """The Bedrock validator reads the capability text from the skill that
    /reality-check reads, so the limit is stated once (#628)."""
    from src.agents.referee.consolidation_validator import ENGINE_CONTEXT

    skill = (REPO_ROOT / "src/skills/consolidation-review.md").read_text()
    command = (REPO_ROOT / ".claude/commands/reality-check.md").read_text()
    assert "100 actions or 4 MB" in ENGINE_CONTEXT["dynamodb"]
    assert "100 actions or 4 MB" in skill
    assert "src/skills/consolidation-review.md" in command


def test_a_claim_split_across_string_literals_is_still_caught(tmp_path):
    source = tmp_path / "catalog.py"
    source.write_text(
        'description = (\n    "Self-referential FKs. $graphLookup is NOT "\n    "supported in DocumentDB."\n)\n'
    )
    pattern = re.compile(STALE_FACTS[2][0], re.IGNORECASE)
    assert not any(pattern.search(line) for line in source.read_text().splitlines())
    assert any(pattern.search(line) for line in _normalized_lines(source))


def test_a_true_elastic_cluster_caveat_is_not_flagged():
    pattern = re.compile(STALE_FACTS[2][0], re.IGNORECASE)
    true_claim = (
        "`$graphLookup` works on instance-based clusters but is not supported on elastic clusters"
    )
    assert not pattern.search(true_claim)
    assert pattern.search("`$graphLookup` is not supported in any DocumentDB version")
