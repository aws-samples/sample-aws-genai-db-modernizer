"""scripts/search_artifacts.py: the allowlisted, repo-contained grep substitute (#275).

Headless Claude Code sessions may have no Grep tool and CI denies Bash `grep`,
so pipeline subagents search artifacts with this script. It must never read
outside the repository or open credential-like files, and its output must
stay small enough to be returned inline.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path

import pytest

from scripts import search_artifacts as sa

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    art = root / "artifacts" / "wordpress" / "job1"
    art.mkdir(parents=True)
    (art / "input_group_0.json").write_text(
        '{\n  "queries": [\n    {"query_id": "Q1",\n     "tables": ["wp_posts"]},\n'
        '    {"query_id": "Q2",\n     "tables": ["wp_users"]}\n  ]\n}\n'
    )
    (art / "schema_draft_group_0.json").write_text('{"tables": ["wp_posts"]}\n')
    (root / "src").mkdir()
    (root / "src" / "contracts.py").write_text("class TradeOff:\n    a: int\n    b: int\n")
    monkeypatch.chdir(root)
    return root


def _run(repo: Path, *args: str, **kwargs: object) -> sa.SearchResult:
    return sa.search(*args, repo_root=repo, **kwargs)  # type: ignore[arg-type]


# --- basic behaviour -------------------------------------------------------


def test_prints_repo_relative_path_line_text(repo: Path) -> None:
    result = _run(repo, '"query_id"', "artifacts")
    assert result.lines == [
        'artifacts/wordpress/job1/input_group_0.json:3:    {"query_id": "Q1",',
        'artifacts/wordpress/job1/input_group_0.json:5:    {"query_id": "Q2",',
    ]
    assert result.matches == 2 and result.files_matched == 1


def test_absolute_path_inside_repo_is_accepted(repo: Path) -> None:
    result = _run(repo, "TradeOff", str(repo / "src" / "contracts.py"))
    assert result.lines == ["src/contracts.py:1:class TradeOff:"]


def test_context_lines_use_dash_separator(repo: Path) -> None:
    result = _run(repo, "class TradeOff", "src", context=1)
    assert result.lines == ["src/contracts.py:1:class TradeOff:", "src/contracts.py-2-    a: int"]


def test_context_blocks_are_separated(repo: Path) -> None:
    lines = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    (repo / "artifacts" / "big.txt").write_text(lines)
    result = _run(repo, r"^line(3|15)$", "artifacts/big.txt", context=1)
    assert result.lines == [
        "artifacts/big.txt-2-line2",
        "artifacts/big.txt:3:line3",
        "artifacts/big.txt-4-line4",
        "--",
        "artifacts/big.txt-14-line14",
        "artifacts/big.txt:15:line15",
        "artifacts/big.txt-16-line16",
    ]


def test_files_only_and_glob(repo: Path) -> None:
    result = _run(repo, "wp_posts", "artifacts", files_only=True)
    assert result.lines == [
        "artifacts/wordpress/job1/input_group_0.json",
        "artifacts/wordpress/job1/schema_draft_group_0.json",
    ]
    globbed = _run(repo, "wp_posts", "artifacts", files_only=True, glob="schema_draft_group_*.json")
    assert globbed.lines == ["artifacts/wordpress/job1/schema_draft_group_0.json"]
    star = _run(repo, "wp_posts", ".", files_only=True, glob="**/schema_draft_group_*.json")
    assert star.lines == globbed.lines


def test_ignore_case(repo: Path) -> None:
    assert _run(repo, "tradeoff", "src").lines == []
    assert _run(repo, "tradeoff", "src", ignore_case=True).matches == 1


def test_max_matches_truncates_with_reason(repo: Path) -> None:
    result = _run(repo, "query_id", "artifacts", max_matches=1)
    assert result.matches == 1 and len(result.lines) == 1
    assert result.truncated_reason and "--max-matches" in result.truncated_reason


def test_output_is_capped(repo: Path) -> None:
    (repo / "artifacts" / "many.txt").write_text("hit " * 10 + "\n" * 1 + ("hit\n" * 5000))
    result = _run(repo, "hit", "artifacts/many.txt", max_matches=10_000, max_output_chars=2000)
    assert sum(len(line) + 1 for line in result.lines) <= 2000
    assert result.truncated_reason and "capped" in result.truncated_reason


def test_long_lines_are_clipped(repo: Path) -> None:
    (repo / "artifacts" / "wide.json").write_text("x" * 5000 + "needle\n")
    (line,) = _run(repo, "needle", "artifacts/wide.json").lines
    assert len(line) < sa.MAX_LINE_CHARS + 100
    assert "chars]" in line


def test_binary_files_are_skipped(repo: Path) -> None:
    (repo / "artifacts" / "blob.bin").write_bytes(b"needle\0\x01\x02")
    assert _run(repo, "needle", "artifacts").files_matched == 0


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"context": -1}, "--context"),
        ({"context": sa.MAX_CONTEXT + 1}, "--context"),
        ({"max_matches": 0}, "--max-matches"),
    ],
)
def test_rejects_bad_limits(repo: Path, kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(sa.SearchError, match=message):
        _run(repo, "x", "artifacts", **kwargs)


def test_rejects_invalid_regex(repo: Path) -> None:
    with pytest.raises(sa.SearchError, match="invalid regular expression"):
        _run(repo, "(", "artifacts")


def test_missing_path_is_an_error(repo: Path) -> None:
    with pytest.raises(sa.SearchError, match="does not exist"):
        _run(repo, "x", "artifacts/nope")


# --- containment -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/etc", "/etc/passwd", "..", "../..", "artifacts/../../"])
def test_rejects_paths_outside_repo(repo: Path, path: str) -> None:
    with pytest.raises(sa.SearchError, match="outside the repository root"):
        _run(repo, "root", path)


def test_rejects_symlinked_search_path_escaping_repo(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n")
    (repo / "artifacts" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(sa.SearchError, match="outside the repository root"):
        _run(repo, "needle", "artifacts/link")


def test_walk_skips_file_symlinks_escaping_repo(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "loot.txt"
    outside.write_text("needle\n")
    (repo / "artifacts" / "loot.txt").symlink_to(outside)
    (repo / "artifacts" / "ok.txt").write_text("needle\n")
    result = _run(repo, "needle", "artifacts", files_only=True)
    assert result.lines == ["artifacts/ok.txt"]


def test_walk_does_not_follow_directory_symlinks_out_of_repo(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (outside / "x.txt").write_text("needle\n")
    (repo / "artifacts" / "dirlink").symlink_to(outside, target_is_directory=True)
    assert _run(repo, "needle", "artifacts").files_matched == 0


@pytest.mark.parametrize(
    "name", [".env", ".env.local", "prod.env", "server.pem", "id_rsa", ".npmrc", "credentials"]
)
def test_credential_like_files_are_never_read(repo: Path, name: str) -> None:
    (repo / name).write_text("AWS_SECRET=needle\n")
    with pytest.raises(sa.SearchError, match="credential or secret"):
        _run(repo, "needle", name)
    assert _run(repo, "needle", ".").files_matched == 0


@pytest.mark.parametrize("skip", [".git", ".venv", "node_modules", ".local-ui"])
def test_skipped_directories_are_not_read(repo: Path, skip: str) -> None:
    (repo / skip).mkdir()
    (repo / skip / "f.txt").write_text("needle\n")
    assert _run(repo, "needle", ".").files_matched == 0
    with pytest.raises(sa.SearchError, match="never reads"):
        _run(repo, "needle", f"{skip}/f.txt")


# --- CLI -------------------------------------------------------------------


def _cli(*args: str, sandbox: bool = False) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
    if sandbox:
        env["MODERNIZER_CI_SANDBOX"] = "1"
    return subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, "scripts/search_artifacts.py", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_finds_lines_in_the_repo_source_tree() -> None:
    proc = _cli("^def sandbox_violation", "scripts/_sandbox.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.startswith("scripts/_sandbox.py:")


def test_cli_no_match_exits_1() -> None:
    proc = _cli("zzz_no_such_text_zzz_" + "x" * 3, "scripts/_sandbox.py")
    assert proc.returncode == 1
    assert "no matches" in proc.stdout


@pytest.mark.parametrize("sandbox", [False, True])
@pytest.mark.parametrize("path", ["/etc/passwd", "../", "/root"])
def test_cli_refuses_escape_with_json_error(path: str, sandbox: bool) -> None:
    proc = _cli("root", path, sandbox=sandbox)
    assert proc.returncode == 2
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["status"] == "error"
    assert "outside the repository root" in out["message"]
