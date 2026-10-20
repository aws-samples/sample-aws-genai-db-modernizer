"""The scheduled price refresh opens one reviewed pull request, with no credentials (#1196)."""

import re
from pathlib import Path
from typing import Any

import yaml

from scripts import refresh_prices as rp
from scripts.check_commit_message import check_message

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-prices.yml"
# Two weeks to review and merge a refresh pull request.
REVIEW_DAYS = 14


def _load() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data


def _triggers() -> dict[str, Any]:
    wf: dict[Any, Any] = _load()
    # PyYAML reads the bare key `on` as the boolean True.
    on: dict[str, Any] = wf.get("on", wf.get(True))
    return on


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _load()["jobs"]["refresh"]["steps"]
    return steps


def _script() -> str:
    return "\n".join(s.get("run", "") for s in _steps())


def test_it_runs_monthly_and_on_demand_never_on_a_pull_request() -> None:
    on = _triggers()
    assert set(on) == {"schedule", "workflow_dispatch"}
    [cron] = [c["cron"] for c in on["schedule"]]
    minute, hour, day, month, weekday = cron.split()
    # One run a month, on a fixed day: the longest gap between two runs is 31 days.
    assert day.isdigit() and 1 <= int(day) <= 28
    assert (month, weekday) == ("*", "*")


def test_the_staleness_warning_fits_the_schedule() -> None:
    # The lint warning must not fire while a refresh is on schedule: the longest gap
    # between two monthly runs plus the time to merge the pull request it opens.
    assert rp.MAX_AGE_DAYS == 31 + REVIEW_DAYS


def test_every_action_is_pinned_by_sha() -> None:
    uses = [s["uses"] for s in _steps() if "uses" in s]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_it_needs_no_credentials_beyond_the_jobs_own_token() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"GITHUB_TOKEN"}
    assert "aws-actions/" not in text and "AWS_" not in text
    assert _load()["permissions"] == {"contents": "read"}
    assert _load()["jobs"]["refresh"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }


def test_it_refreshes_with_the_script() -> None:
    assert "uv run python scripts/refresh_prices.py" in _script()


def test_one_fixed_branch_and_an_existing_pull_request_is_updated() -> None:
    assert _load()["env"]["BRANCH"] == "chore/price-table-refresh"
    script = _script()
    assert 'gh pr list --head "$BRANCH"' in script
    # The open pull request is edited, a new one is created only when none is open.
    assert script.index('gh pr edit "$open_pr"') < script.index("gh pr create")
    assert 'if [ -n "$open_pr" ]; then\n  gh pr edit' in script
    assert "gh pr merge" not in script


def test_an_unchanged_table_proposes_nothing() -> None:
    script = _script()
    unchanged = script.index('if git diff --quiet -- "$table"; then')
    assert script.index("exit 0", unchanged) < script.index("git commit")


def test_its_commit_message_passes_the_commit_message_check() -> None:
    script = _script()
    body = script.split("git commit -q -F - <<EOF\n", 1)[1].split("\nEOF\n", 1)[0]
    message = body.replace("$retrieved", "2026-11-01") + "\n"
    assert check_message(message) == []
