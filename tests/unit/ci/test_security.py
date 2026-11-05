# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/security.py: the one security scan's file selection, pinned rules and gitleaks binary (#1844)."""

from __future__ import annotations

import argparse
import hashlib
import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from ci import security
from ci.security import (
    SecurityError,
    bandit_targets,
    changed_files,
    checkout_problem,
    existing_files,
    fetch_source,
    gitleaks_archive,
    gitleaks_platform,
    merged_rules,
    missing_rule_files,
    rule_id,
    semgrep_targets,
    unpack_gitleaks,
)

REPO = Path(__file__).resolve().parents[3]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "core.hooksPath", "/dev/null")
    return root


def _commit(root: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)


# ---------------------------------------------------------------- rule ids


def test_rule_id_is_the_registry_form() -> None:
    assert (
        rule_id("", "python/lang/security/audit/formatted-sql-query.yaml", "formatted-sql-query")
        == "python.lang.security.audit.formatted-sql-query.formatted-sql-query"
    )


def test_rule_id_with_a_source_prefix_and_a_yml_suffix() -> None:
    assert (
        rule_id("trailofbits", "python/tarfile-extractall-traversal.yml", "tarfile-x")
        == "trailofbits.python.tarfile-extractall-traversal.tarfile-x"
    )


def test_rule_id_lowercases_the_path_but_not_the_rules_own_id() -> None:
    assert (
        rule_id(
            "", "javascript/lang/security/detect-pseudoRandomBytes.yaml", "detect-pseudoRandomBytes"
        )
        == "javascript.lang.security.detect-pseudorandombytes.detect-pseudoRandomBytes"
    )


def _rules_repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    root = _repo(tmp_path)
    _commit(root, files, "rules")
    return root, _git(root, "rev-parse", "HEAD")


def test_merged_rules_gives_every_rule_its_registry_id(tmp_path: Path) -> None:
    root, commit = _rules_repo(
        tmp_path, {"python/a.yaml": "rules:\n- id: one\n  pattern: x\n- id: two\n  pattern: y\n"}
    )
    source = {"name": "s", "commit": commit, "id_prefix": "", "rules": ["python/a.yaml"]}
    merged = merged_rules([source], [root])
    assert [r["id"] for r in merged["rules"]] == ["python.a.one", "python.a.two"]
    assert merged["rules"][1]["pattern"] == "y"


def test_merged_rules_refuses_a_listed_file_the_commit_lacks(tmp_path: Path) -> None:
    root, commit = _rules_repo(tmp_path, {"python/a.yaml": "rules: []\n"})
    source = {"name": "s", "commit": commit, "id_prefix": "", "rules": ["python/gone.yaml"]}
    with pytest.raises(SecurityError, match="python/gone.yaml"):
        merged_rules([source], [root])


def test_rule_files_are_matched_with_exact_case_from_the_git_tree(tmp_path: Path) -> None:
    # On a case-insensitive filesystem (macOS) the lowercase path opens the file; the tree
    # comparison refuses it there too, as Linux would.
    root, _ = _rules_repo(tmp_path, {"html/UseSRIForCDNs.yaml": "rules: []\n"})
    assert missing_rule_files(root, ["html/UseSRIForCDNs.yaml"]) == []
    assert missing_rule_files(root, ["html/usesriforcdns.yaml"]) == ["html/usesriforcdns.yaml"]


# ---------------------------------------------------------------- which files


def test_semgrep_targets_apply_the_repositorys_semgrepignore() -> None:
    ignore = (REPO / ".semgrepignore").read_text(encoding="utf-8")
    files = [
        "contrib/x/tool.py",
        ".secrets.baseline",
        "src/ui/package.json",
        "src/a.py",
        "contributing/a.py",
        "src/ui/src/package.json",
    ]
    assert semgrep_targets(files, ignore) == [
        "src/a.py",
        "contributing/a.py",
        "src/ui/src/package.json",
    ]


def test_semgrep_targets_with_an_empty_ignore_keep_everything() -> None:
    assert semgrep_targets(["contrib/a.py", "b.md"], "") == ["contrib/a.py", "b.md"]


def test_bandit_targets_are_the_python_files() -> None:
    assert bandit_targets(["a.py", "b.pyi", "c.sh", "d.py.txt", "e/f.py"]) == ["a.py", "e/f.py"]


def test_existing_files_drop_deleted_paths_symlinks_and_directories(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "d").mkdir()
    (tmp_path / "link.py").symlink_to(tmp_path / "a.py")
    got = existing_files(["a.py", "gone.py", "d", "link.py", "a.py"], root=tmp_path)
    assert got == ["a.py"]


def test_existing_files_drop_paths_outside_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x", encoding="utf-8")
    outside = tmp_path / "COMMIT_EDITMSG"
    outside.write_text("feat: x", encoding="utf-8")
    got = existing_files([str(outside), "../COMMIT_EDITMSG", str(root / "src" / "a.py")], root=root)
    assert got == ["src/a.py"]


def test_changed_files_cover_the_branch_uncommitted_and_untracked_not_deleted(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _commit(root, {"base.py": "1", "old.py": "1", "doomed.py": "1"}, "base")
    _git(root, "branch", "base-ref")
    _commit(root, {"committed.py": "1", "old.py": "2"}, "branch")
    (root / "doomed.py").unlink()
    (root / "base.py").write_text("2", encoding="utf-8")
    (root / "untracked.py").write_text("1", encoding="utf-8")
    assert changed_files("base-ref", root=root) == [
        "base.py",
        "committed.py",
        "old.py",
        "untracked.py",
    ]


def test_changed_files_with_nothing_changed_is_empty(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _commit(root, {"a.py": "1"}, "base")
    assert changed_files("HEAD", root=root) == []


def test_select_files_given_files_win_over_quick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "changed_files", lambda base: pytest.fail("not called"))
    args = argparse.Namespace(files=["ci/security.py", "missing.py"], quick=True, base="x")
    assert security.select_files(args)[1] == ["ci/security.py"]


def test_select_files_quick_asks_for_the_changed_files(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def changed(base: str) -> list[str]:
        seen.append(base)
        return ["ci/lib.sh"]

    monkeypatch.setattr(security, "changed_files", changed)
    args = argparse.Namespace(files=[], quick=True, base="upstream/main")
    assert security.select_files(args)[1] == ["ci/lib.sh"]
    assert seen == ["upstream/main"]


def test_select_files_default_is_every_tracked_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/lib.sh", "ci/test.sh"])
    args = argparse.Namespace(files=[], quick=False, base="origin/main")
    assert security.select_files(args) == ("every tracked file", ["ci/lib.sh", "ci/test.sh"])


# ---------------------------------------------------------------- the verdict


def _runners(monkeypatch: pytest.MonkeyPatch, codes: dict, ran: list[str]) -> None:
    def runner(name: str):
        def run(config: dict, files: list[str]) -> int:
            ran.append(name)
            outcome = codes.get(name, 0)
            if isinstance(outcome, Exception):
                raise outcome
            return int(outcome)

        return run

    monkeypatch.setattr(security, "RUNNERS", {n: runner(n) for n in security.SCANNERS})
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/lib.sh"])


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ({}, 0),
        ({"semgrep": 1}, 1),
        ({"gitleaks": 3}, 1),
        ({"bandit": 1}, 1),
        # A scanner that exits with anything but its findings code could not run.
        ({"semgrep": 2}, 2),
        ({"gitleaks": 1}, 2),
        ({"semgrep": 1, "bandit": 2}, 2),
        ({"gitleaks": SecurityError("no network")}, 2),
    ],
)
def test_main_exit_code(monkeypatch: pytest.MonkeyPatch, codes: dict, expected: int) -> None:
    ran: list[str] = []
    _runners(monkeypatch, codes, ran)
    assert security.main([]) == expected
    assert ran == list(security.SCANNERS), "every scanner runs even after one fails"


def test_main_says_which_scanner_could_not_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _runners(monkeypatch, {"semgrep": 2}, [])
    assert security.main([]) == 2
    assert "security: semgrep could not run: it exited 2" in capsys.readouterr().err


def test_a_missing_uv_exits_2_with_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/security.py"])
    assert security.main(["--only", "semgrep"]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [
        "security: semgrep could not run: uv is not on PATH (https://docs.astral.sh/uv/)"
    ]


def test_a_scanner_that_cannot_start_exits_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(security, "semgrep_executable", lambda c, **k: tmp_path / "no-semgrep")
    monkeypatch.setattr(security, "build_rules", lambda c, out: out.write_text("rules: []\n"))
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/security.py"])
    assert security.main(["--only", "semgrep"]) == 2
    assert "semgrep could not run: could not start" in capsys.readouterr().err
    assert not list(REPO.glob(".security-semgrep-rules.*.yaml")), "the rules file is removed"


def test_main_only_runs_the_named_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []

    def runner(name: str):
        def run(config: dict, files: list[str]) -> int:
            ran.append(name)
            return 0

        return run

    monkeypatch.setattr(security, "RUNNERS", {n: runner(n) for n in security.SCANNERS})
    monkeypatch.setattr(security, "tracked_files", lambda: [])
    assert security.main(["--only", "bandit"]) == 0
    assert ran == ["bandit"]


def test_a_scanner_with_no_files_passes_without_fetching(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security, "semgrep_executable", lambda c, **k: pytest.fail("fetched"))
    monkeypatch.setattr(security, "build_rules", lambda c, out: pytest.fail("fetched"))
    monkeypatch.setattr(security, "gitleaks_archive", lambda c: pytest.fail("fetched"))
    config = security.load_config()
    assert security.run_semgrep(config, ["contrib/x.py"]) == 0
    assert security.run_gitleaks(config, []) == 0
    assert security.run_bandit(config, ["a.md"]) == 0


def test_bandit_noise_filter_keeps_findings() -> None:
    noise = "[manager]\tWARNING\tTest in comment: argv is not a test name or id, ignoring"
    assert security.BANDIT_NOISE.search(noise)
    assert not security.BANDIT_NOISE.search(
        ">> Issue: [B602:subprocess_popen_with_shell_equals_true]"
    )
    stale = "[tester]\tWARNING\tnosec encountered (B608), but no failed test on file ./a.py:3"
    assert security.BANDIT_NOISE.search(stale)
    assert not security.BANDIT_NOISE.search("[tester]\tWARNING\tsomething else (B603)")


# ---------------------------------------------------------------- fetching the pins


def _upstream(tmp_path: Path) -> tuple[Path, str]:
    upstream = _repo(tmp_path)
    _commit(upstream, {"python/a.yaml": "rules: []\n"}, "one")
    first = _git(upstream, "rev-parse", "HEAD")
    _commit(upstream, {"python/a.yaml": "rules: [changed]\n"}, "two")
    return upstream, first


def test_fetch_source_checks_out_the_commit(tmp_path: Path) -> None:
    upstream, first = _upstream(tmp_path)
    got = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    assert (got / "python" / "a.yaml").read_text(encoding="utf-8") == "rules: []\n"
    assert checkout_problem(got, first) is None


def test_a_verified_checkout_is_reused_without_a_fetch(tmp_path: Path) -> None:
    upstream, first = _upstream(tmp_path)
    got = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    # Checked (HEAD and a clean tree) and reused: the remote is not contacted again.
    assert fetch_source((tmp_path / "nowhere").as_uri(), first, tmp_path / "cache") == got


@pytest.mark.parametrize(
    "tamper",
    [
        lambda root: (root / "python" / "a.yaml").write_text("rules: [evil]\n"),
        lambda root: (root / "python" / "extra.yaml").write_text("rules: []\n"),
        lambda root: _git(
            root,
            "-c",
            "user.email=t@x",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "moved",
        ),
    ],
    ids=["edited-rule", "extra-file", "moved-head"],
)
def test_a_tampered_checkout_is_fetched_again(tmp_path: Path, tamper) -> None:
    upstream, first = _upstream(tmp_path)
    got = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    tamper(got)
    assert checkout_problem(got, first) is not None
    again = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    assert checkout_problem(again, first) is None
    assert (again / "python" / "a.yaml").read_text(encoding="utf-8") == "rules: []\n"


def test_a_tampered_checkout_with_no_remote_fails(tmp_path: Path) -> None:
    upstream, first = _upstream(tmp_path)
    got = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    (got / "python" / "a.yaml").write_text("rules: [evil]\n")
    with pytest.raises(SecurityError, match="could not fetch"):
        fetch_source((tmp_path / "nowhere").as_uri(), first, tmp_path / "cache")


def test_the_merged_rules_are_rebuilt_on_every_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, first = _upstream(tmp_path)
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path / "cache"))
    config = {
        "semgrep": {
            "source": [
                {
                    "name": "s",
                    "url": upstream.as_uri(),
                    "commit": first,
                    "id_prefix": "",
                    "rules": ["python/a.yaml"],
                }
            ],
            "rules_sha256": _pin(upstream, first),
        }
    }
    out = tmp_path / "rules.yaml"
    out.write_text("rules:\n- id: no-op\n  pattern: nothing\n")  # a tampered leftover
    security.build_rules(config, out)
    assert out.read_text(encoding="utf-8") == "rules: []\n"


def _pin(upstream: Path, commit: str, rules: tuple[str, ...] = ("python/a.yaml",)) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        root = fetch_source(upstream.as_uri(), commit, Path(tmp))
        return security.rules_digest([{"name": "s", "rules": list(rules)}], [root])


def _pinned_config(upstream: Path, commit: str, pin: str | None = None) -> dict:
    return {
        "semgrep": {
            "source": [
                {
                    "name": "s",
                    "url": upstream.as_uri(),
                    "commit": commit,
                    "id_prefix": "",
                    "rules": ["python/a.yaml"],
                }
            ],
            "rules_sha256": pin or _pin(upstream, commit),
        }
    }


def _skip_worktree_edit(root: Path) -> None:
    _git(root, "update-index", "--skip-worktree", "python/a.yaml")
    (root / "python" / "a.yaml").write_text("rules: [evil]\n")


def _replace_commit(root: Path) -> None:
    # A replacement commit whose tree holds the edited rule; HEAD still names the pinned id.
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input="rules: [evil]\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "mktree"],
        cwd=root,
        input=f"100644 blob {blob}\ta.yaml\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    top = subprocess.run(
        ["git", "mktree"],
        cwd=root,
        input=f"040000 tree {tree}\tpython\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    head = _git(root, "rev-parse", "HEAD")
    fake = _git(root, "-c", "user.email=t@x", "-c", "user.name=t", "commit-tree", top, "-m", "x")
    _git(root, "replace", head, fake)
    _git(root, "reset", "-q", "--hard", head)


@pytest.mark.parametrize(
    "tamper", [_skip_worktree_edit, _replace_commit], ids=["skip-worktree", "git-replace"]
)
def test_a_tamper_git_status_cannot_see_is_caught_by_the_rules_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper
) -> None:
    upstream, first = _upstream(tmp_path)
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path / "cache"))
    config = _pinned_config(upstream, first)
    (root,) = security.checked_checkouts(config)
    tamper(root)
    assert (root / "python" / "a.yaml").read_text() == "rules: [evil]\n"
    assert checkout_problem(root, first) is None, "git status and HEAD do not see it"
    (again,) = security.checked_checkouts(config)  # the pin does: fetched again
    assert (again / "python" / "a.yaml").read_text(encoding="utf-8") == "rules: []\n"


def test_a_tampered_checkout_that_refetches_wrong_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, first = _upstream(tmp_path)
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path / "cache"))
    fetches: list[bool] = []
    real = security.fetch_source

    def counting(url: str, commit: str, dest: Path, refetch: bool = False) -> Path:
        fetches.append(refetch)
        return real(url, commit, dest, refetch)

    monkeypatch.setattr(security, "fetch_source", counting)
    with pytest.raises(SecurityError, match="--print-rules-sha256"):
        security.checked_checkouts(_pinned_config(upstream, first, pin="0" * 64))
    assert fetches == [False, True], "one refetch, then it gives up"


def test_the_pin_helper_prints_the_hash_of_a_fresh_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream, first = _upstream(tmp_path)
    config = _pinned_config(upstream, first)
    monkeypatch.setattr(security, "load_config", lambda: config)
    assert security.main(["--print-rules-sha256"]) == 0
    assert capsys.readouterr().out.strip() == config["semgrep"]["rules_sha256"]


def test_the_repository_pins_a_rules_hash() -> None:
    import re as _re

    assert _re.fullmatch(r"[0-9a-f]{64}", security.load_config()["semgrep"]["rules_sha256"])


@pytest.mark.parametrize(
    ("rc", "report", "expected"),
    [
        (0, None, 0),
        (1, '{"results": [{"check_id": "x"}]}', 1),
        (2, None, 2),
    ],
)
def test_classify_keeps_findings_and_other_codes(
    tmp_path: Path, rc: int, report: str | None, expected: int
) -> None:
    path = tmp_path / "r.json"
    if report is not None:
        path.write_text(report)
    assert security.classify("semgrep", rc, path) == expected


@pytest.mark.parametrize("report", [None, "not json", '{"results": []}', '{"errors": []}'])
def test_exit_1_with_no_results_is_a_crash(tmp_path: Path, report: str | None) -> None:
    path = tmp_path / "r.json"
    if report is not None:
        path.write_text(report)
    with pytest.raises(SecurityError, match="a crash"):
        security.classify("semgrep", 1, path)


def test_a_bandit_crash_is_could_not_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def crash(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "No module named bandit\n")

    monkeypatch.setattr(security.subprocess, "run", crash)
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/security.py"])
    assert security.main(["--only", "bandit"]) == 2
    assert "bandit could not run: bandit exited 1 with no results" in capsys.readouterr().err


def test_the_cache_git_environment_drops_only_the_location_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_SSL_CAINFO", "GIT_ASKPASS"):
        monkeypatch.setenv(name, "/x")
    env = security._cache_env()
    assert not {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"} & set(env)
    assert env["GIT_SSL_CAINFO"] == "/x" and env["GIT_ASKPASS"] == "/x"


def test_fetch_source_reports_an_unknown_commit(tmp_path: Path) -> None:
    upstream = _repo(tmp_path)
    _commit(upstream, {"a": "1"}, "one")
    with pytest.raises(SecurityError, match="could not fetch"):
        fetch_source(upstream.as_uri(), "0" * 40, tmp_path / "cache")
    assert not (tmp_path / "cache" / ("0" * 40)).exists()


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "arm64", "darwin_arm64"),
        ("Darwin", "x86_64", "darwin_x64"),
        ("Linux", "x86_64", "linux_x64"),
        ("Linux", "aarch64", "linux_arm64"),
    ],
)
def test_gitleaks_platform(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, expected: str
) -> None:
    monkeypatch.setattr(security.platform, "system", lambda: system)
    monkeypatch.setattr(security.platform, "machine", lambda: machine)
    assert gitleaks_platform() == expected


def _tarball(content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("gitleaks")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _gitleaks_config(digest: str) -> dict:
    return {"gitleaks": {"version": "9.9.9", "sha256": {"linux_x64": digest}}}


@pytest.fixture
def linux_x64(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(security, "gitleaks_platform", lambda: "linux_x64")
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path / "cache"))
    return tmp_path / "cache" / "gitleaks" / "gitleaks_9.9.9_linux_x64.tar.gz"


def _serve(monkeypatch: pytest.MonkeyPatch, data: bytes | None) -> list[str]:
    calls: list[str] = []

    def urlopen(url: str, timeout: int):
        calls.append(url)
        if data is None:
            raise OSError("offline")
        return _Response(data)

    monkeypatch.setattr(security.urllib.request, "urlopen", urlopen)
    return calls


def test_gitleaks_archive_is_downloaded_checked_and_cached(
    monkeypatch: pytest.MonkeyPatch, linux_x64: Path, tmp_path: Path
) -> None:
    data = _tarball(b"#!/bin/sh\n")
    calls = _serve(monkeypatch, data)
    config = _gitleaks_config(hashlib.sha256(data).hexdigest())
    assert gitleaks_archive(config) == data
    assert linux_x64.read_bytes() == data
    assert gitleaks_archive(config) == data
    assert len(calls) == 1, "a cached archive that matches the pin is not downloaded again"
    binary = unpack_gitleaks(data, tmp_path)
    assert binary.read_bytes() == b"#!/bin/sh\n"
    assert binary.stat().st_mode & 0o111


def test_a_swapped_cached_archive_is_downloaded_again(
    monkeypatch: pytest.MonkeyPatch, linux_x64: Path
) -> None:
    data = _tarball(b"#!/bin/sh\n")
    calls = _serve(monkeypatch, data)
    config = _gitleaks_config(hashlib.sha256(data).hexdigest())
    gitleaks_archive(config)
    linux_x64.write_bytes(_tarball(b"#!/bin/sh\necho; exit 0\n"))  # swapped in the cache
    assert gitleaks_archive(config) == data
    assert linux_x64.read_bytes() == data
    assert len(calls) == 2


def test_a_swapped_cached_archive_with_no_network_fails(
    monkeypatch: pytest.MonkeyPatch, linux_x64: Path
) -> None:
    data = _tarball(b"#!/bin/sh\n")
    linux_x64.parent.mkdir(parents=True)
    linux_x64.write_bytes(_tarball(b"#!/bin/sh\necho; exit 0\n"))
    _serve(monkeypatch, None)
    with pytest.raises(SecurityError, match="could not download"):
        gitleaks_archive(_gitleaks_config(hashlib.sha256(data).hexdigest()))
    assert not linux_x64.exists()


def test_gitleaks_archive_refuses_a_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch, linux_x64: Path
) -> None:
    _serve(monkeypatch, _tarball(b"tampered"))
    with pytest.raises(SecurityError, match="SHA-256"):
        gitleaks_archive(_gitleaks_config("0" * 64))
    assert not linux_x64.exists()


def test_gitleaks_archive_refuses_a_platform_with_no_pin(
    monkeypatch: pytest.MonkeyPatch, linux_x64: Path
) -> None:
    monkeypatch.setattr(security, "gitleaks_platform", lambda: "plan9_mips")
    with pytest.raises(SecurityError, match="plan9_mips"):
        gitleaks_archive(_gitleaks_config("0" * 64))


def test_the_semgrep_environment_is_synced_to_the_hashed_lock_on_every_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path))
    calls: list[tuple[str, ...]] = []

    def uv(*args: str) -> None:
        calls.append(args)
        if args[0] == "venv":
            python = Path(args[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.write_text("")

    monkeypatch.setattr(security, "_uv", uv)
    first = security.semgrep_executable({})
    second = security.semgrep_executable({})
    assert first == second and first.name == "semgrep"
    assert [c[0] for c in calls] == ["venv", "pip", "pip"], "one venv, a sync on each run"
    for sync in calls[1:]:
        assert {"--require-hashes", "--no-build", "--reinstall"} <= set(sync)
        assert sync[-1] == str(security.REQUIREMENTS)


def test_the_hook_syncs_the_semgrep_environment_without_reinstalling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SECURITY_CACHE_DIR", str(tmp_path))
    (tmp_path / "semgrep-env").mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(security, "_uv", lambda *a: calls.append(a))
    security.semgrep_executable({}, reinstall=False)
    sync = calls[-1]
    assert "--reinstall" not in sync and {"--require-hashes", "--no-build"} <= set(sync)


def test_run_semgrep_reinstalls_unless_told_to_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []

    def executable(config: dict, reinstall: bool = True) -> Path:
        seen.append(reinstall)
        return tmp_path / "semgrep"

    monkeypatch.setattr(security, "semgrep_executable", executable)
    monkeypatch.setattr(security, "build_rules", lambda c, out: out.touch())
    monkeypatch.setattr(security, "_run", lambda cmd, cwd: 0)
    monkeypatch.setattr(security, "tracked_files", lambda: ["ci/security.py"])
    seen_by_flag = []
    for argv in (["--only", "semgrep"], ["--only", "semgrep", "--skip-env-reinstall"]):
        seen.clear()
        assert security.main(argv) == 0
        seen_by_flag.append(seen[0])
    assert seen_by_flag == [True, False]


def test_a_failed_sync_is_a_could_not_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail(cmd, **kwargs):
        raise subprocess.CalledProcessError(2, cmd, stderr="error: hash mismatch for x\n")

    monkeypatch.setattr(security.subprocess, "run", fail)
    with pytest.raises(SecurityError, match="hash mismatch"):
        security._uv("pip", "sync", "req.txt")


def test_run_semgrep_builds_the_rules_on_every_run_and_removes_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built: list[Path] = []
    monkeypatch.setattr(security, "semgrep_executable", lambda c, **k: tmp_path / "semgrep")

    def build(config: dict, out: Path) -> None:
        built.append(out)
        out.touch()

    monkeypatch.setattr(security, "build_rules", build)
    monkeypatch.setattr(security, "_run", lambda cmd, cwd: 0)
    config = security.load_config()
    assert security.run_semgrep(config, ["ci/security.py"]) == 0
    assert security.run_semgrep(config, ["ci/security.py"]) == 0
    assert len(built) == 2
    assert not any(p.exists() for p in built)


def test_cache_checkouts_ignore_the_hook_git_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A pre-commit hook runs with GIT_INDEX_FILE (and more) set for the repository being
    # committed to; the fetch into the cache must not follow it.
    upstream, first = _upstream(tmp_path)
    monkeypatch.setenv("GIT_INDEX_FILE", str(REPO / ".git" / "index"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))
    got = fetch_source(upstream.as_uri(), first, tmp_path / "cache")
    assert checkout_problem(got, first) is None


def test_semgrep_runs_with_no_per_rule_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(security, "semgrep_executable", lambda c, **k: tmp_path / "semgrep")
    monkeypatch.setattr(security, "build_rules", lambda c, out: out.touch())

    def run(cmd: list[str], cwd: Path) -> int:
        seen.append(list(cmd))
        return 0

    monkeypatch.setattr(security, "_run", run)
    security.run_semgrep(security.load_config(), ["ci/security.py"])
    cmd = seen[0]
    assert cmd[cmd.index("--timeout") + 1] == "0"
    assert "--metrics=off" in cmd and "--error" in cmd


def test_rules_digest_covers_each_files_name_and_bytes(tmp_path: Path) -> None:
    (tmp_path / "python").mkdir()
    rule = tmp_path / "python" / "a.yaml"
    rule.write_bytes(b"rules: []")
    source = [{"name": "s", "rules": ["python/a.yaml"]}]
    # sha256(b"s\0python/a.yaml\09\0" + b"rules: []"): the documented layout, fixed here.
    assert security.rules_digest(source, [tmp_path]) == EXPECTED_DIGEST
    rule.write_bytes(b"rules: [x]")
    assert security.rules_digest(source, [tmp_path]) != EXPECTED_DIGEST


EXPECTED_DIGEST = (
    "c5f3b4549d9bbb40c47c267234e231f10fe6bb03d47f40684c0c0eb669f338f2"  # pragma: allowlist secret
)
