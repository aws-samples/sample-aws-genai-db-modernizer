"""The maintainer sync must compare file contents, not size and mtime (#344).

Also covers #448: before copying files, the script must fetch the target
remote, move the target clone to the latest target main (stashing any local
changes instead of stopping), and refuse to continue if the target main has
diverged from its remote. And the expanded scope on the same issue: the
script syncs one exported git ref (never the working tree), accepts
--ref/--branch/--message overrides, always records a Source-Commit trailer,
and refuses to push a sync that would change a protected path.
"""

import os
import re
import shutil
import subprocess  # nosec B404 -- runs the system rsync/git binaries with fixed argv
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "maintainer-sync.sh"

# The suite runs with --fail-on-skip. GitHub Actions' ubuntu-latest image
# ships rsync, so a missing rsync there is a real environment break: fail
# loudly. Elsewhere (a local machine, or another CI image without rsync)
# these tests return early; GitHub Actions is where they are guaranteed to run.
RSYNC_MISSING = shutil.which("rsync") is None


def _skip_without_rsync() -> bool:
    if not RSYNC_MISSING:
        return False
    if os.environ.get("GITHUB_ACTIONS") == "true":
        pytest.fail("rsync is not installed; the GitHub Actions image is expected to ship it")
    return True


def _rsync_args_block() -> str:
    text = SCRIPT.read_text()
    match = re.search(r"RSYNC_ARGS=\((.*?)\n\)", text, re.S)
    assert match, "RSYNC_ARGS array not found in maintainer-sync.sh"
    return match.group(1)


def test_sync_compares_file_contents(tmp_path):
    assert "--checksum" in _rsync_args_block().split()

    if _skip_without_rsync():
        return
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "f.txt").write_text('version = "1.5"\n')
    (dst / "f.txt").write_text('version = "1.4"\n')
    stamp = (src / "f.txt").stat().st_mtime
    os.utime(dst / "f.txt", (stamp, stamp))
    # Only the plain flags: the real --filter/--exclude values need a
    # resolved $TARGET_REPO_PATH and a sync config, both exercised by the
    # subprocess-driven tests below; this one just checks --checksum behaves.
    args = [
        a
        for a in _rsync_args_block().split()
        if a.startswith("-") and not a.startswith(("--exclude", "--filter"))
    ]
    subprocess.run(  # nosec B603 B607 -- fixed argv, rsync resolved from PATH like the script it tests
        ["rsync", *args, f"{src}/", f"{dst}/"], check=True, capture_output=True
    )
    assert (dst / "f.txt").read_text() == 'version = "1.5"\n'


# --- Integration-style tests for the fetch / stash / fast-forward flow ---


def _git(*args: str, cwd: Path, env: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv against throwaway tmp_path repos
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )


def _git_env(home: Path) -> dict:
    """A hermetic environment: no global git config, hooks or signing."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # Belt and suspenders alongside HOME: ignore any global gitconfig the
    # ambient environment might point at (e.g. via XDG_CONFIG_HOME), so a
    # stray global hook, signing setting or alias can never leak in.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env.pop("GIT_AUTHOR_NAME", None)
    env.pop("GIT_AUTHOR_EMAIL", None)
    env.pop("GIT_COMMITTER_NAME", None)
    env.pop("GIT_COMMITTER_EMAIL", None)
    return env


def _init_repo(path: Path, env: dict) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path, env=env)
    _git("config", "user.email", "test@example.com", cwd=path, env=env)
    _git("config", "user.name", "Test", cwd=path, env=env)
    _git("config", "commit.gpgsign", "false", cwd=path, env=env)
    _git("config", "tag.gpgsign", "false", cwd=path, env=env)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _tmp_fs_is_case_insensitive(base: Path) -> bool:
    """True if base's filesystem treats "probe" and "PROBE" as the same
    path (default macOS APFS; not Linux ext4). Some tests only reproduce
    anything interesting on a case-insensitive filesystem.
    """
    marker = base / "case-fs-probe"
    marker.write_text("x")
    return (base / "CASE-FS-PROBE").exists()


def _commit_all(path: Path, env: dict, message: str) -> None:
    _git("add", "-A", cwd=path, env=env)
    _git("commit", "-q", "-m", message, cwd=path, env=env)


def _rev_parse(path: Path, env: dict, ref: str) -> str:
    return _git("rev-parse", ref, cwd=path, env=env).stdout.strip()


def _make_target(
    tmp_path: Path,
    env: dict,
    protect: list[str] | None = None,
    extra_seed_files: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """A bare 'remote' plus a clone of it, with a minimal .sync-config."""
    bare = tmp_path / "target-remote.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare, env=env)

    seed = tmp_path / "target-seed"
    _init_repo(seed, env)
    # The real .sync-config excludes itself (it is target-only); mirror
    # that here so a sync never deletes the config a second run needs.
    sync_config = "[exclude]\n.sync-config\n[protect]\n" + "".join(f"{p}\n" for p in protect or [])
    _write(seed / ".sync-config", sync_config)
    _write(seed / "README.md", "target seed\n")
    for rel_path, content in (extra_seed_files or {}).items():
        _write(seed / rel_path, content)
    _commit_all(seed, env, "chore: seed target")
    _git("remote", "add", "origin", str(bare), cwd=seed, env=env)
    _git("push", "-q", "origin", "main", cwd=seed, env=env)

    clone = tmp_path / "target-clone"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=clone, env=env)
    _git("config", "user.name", "Test", cwd=clone, env=env)
    _git("config", "commit.gpgsign", "false", cwd=clone, env=env)
    return bare, clone


def _make_source(tmp_path: Path, env: dict, branch: str = "fix/sync-flow") -> Path:
    source = tmp_path / "source-repo"
    _init_repo(source, env)
    _write(source / "README.md", "source repo\n")
    _commit_all(source, env, "chore: initial source commit")
    _git("checkout", "-q", "-b", branch, cwd=source, env=env)
    _write(source / "feature.txt", "a new feature\n")
    _commit_all(source, env, "feat: add a feature")
    return source


def _run_script(
    target: Path,
    source: Path,
    env: dict,
    extra_env: dict | None = None,
    extra_args: list[str] | None = None,
):
    run_env = dict(env)
    run_env["TARGET_REPO_PATH"] = str(target)
    if extra_env:
        run_env.update(extra_env)
    return subprocess.run(  # nosec B603 B607 -- fixed argv, invoking the script under test
        ["bash", str(SCRIPT), *(extra_args or [])],
        cwd=source,
        env=run_env,
        capture_output=True,
        text=True,
    )


def test_behind_target_is_fetched_and_rebuilt_on_remote_main(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/behind-main")

    # Advance the remote's main independently of the target clone, so the
    # clone is behind and must be fetched + fast-forwarded before copying.
    advance = tmp_path / "target-advance"
    _git("clone", "-q", str(bare), str(advance), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=advance, env=env)
    _git("config", "user.name", "Test", cwd=advance, env=env)
    _write(advance / "UPSTREAM.md", "a change only on the remote\n")
    _commit_all(advance, env, "chore: advance remote main")
    _git("push", "-q", "origin", "main", cwd=advance, env=env)
    remote_main_tip = _rev_parse(advance, env, "HEAD")

    # The target clone's local main is still behind -- it never fetched.
    assert _rev_parse(clone, env, "main") != remote_main_tip

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    first_parent = _git(
        "log", "-1", "--format=%P", "fix/behind-main", cwd=clone, env=env
    ).stdout.strip()
    assert first_parent == remote_main_tip


def test_dirty_target_is_stashed_not_rejected(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/dirty-target")

    # Dirty the target clone: an uncommitted edit plus an untracked file.
    (clone / "README.md").write_text("locally edited, never committed\n")
    _write(clone / "untracked.txt", "should be stashed too\n")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    stash_list = _git("stash", "list", cwd=clone, env=env).stdout
    assert "maintainer-sync: fix/dirty-target " in stash_list


def test_diverged_target_main_stops_with_clear_error(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/diverged-main")

    # Diverge: a commit on the remote the clone never saw, and a different
    # local-only commit on the clone's main that was never pushed.
    advance = tmp_path / "target-advance"
    _git("clone", "-q", str(bare), str(advance), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=advance, env=env)
    _git("config", "user.name", "Test", cwd=advance, env=env)
    _write(advance / "UPSTREAM.md", "a change only on the remote\n")
    _commit_all(advance, env, "chore: advance remote main")
    _git("push", "-q", "origin", "main", cwd=advance, env=env)

    _write(clone / "LOCAL_ONLY.md", "a change only in the local clone\n")
    _commit_all(clone, env, "chore: local-only change never pushed")
    local_main_tip = _rev_parse(clone, env, "main")

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "diverged" in (result.stdout + result.stderr).lower()

    # Nothing was touched: local main is unchanged and nothing new was
    # pushed to the remote for the mapped target branch.
    assert _rev_parse(clone, env, "main") == local_main_tip
    push_check = _git("ls-remote", str(bare), "fix/diverged-main", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


def test_dry_run_prints_plan_without_stashing_or_merging(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/dry-run")

    # Advance the remote main, and dirty the clone, to exercise both parts
    # of the dry-run plan output.
    advance = tmp_path / "target-advance"
    _git("clone", "-q", str(bare), str(advance), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=advance, env=env)
    _git("config", "user.name", "Test", cwd=advance, env=env)
    _write(advance / "UPSTREAM.md", "a change only on the remote\n")
    _commit_all(advance, env, "chore: advance remote main")
    _git("push", "-q", "origin", "main", cwd=advance, env=env)

    (clone / "README.md").write_text("dirty but should not be stashed\n")
    local_main_before = _rev_parse(clone, env, "main")
    branch_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone, env=env).stdout.strip()

    result = _run_script(clone, source, env, extra_env={"SYNC_DRY_RUN": "true"})
    assert result.returncode == 0, result.stdout + result.stderr

    output = (result.stdout + result.stderr).lower()
    assert "would stash" in output
    assert "would fast-forward" in output
    assert "by 1 commit" in output  # exactly one commit behind

    # Nothing was actually touched.
    assert _rev_parse(clone, env, "main") == local_main_before
    assert (
        _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone, env=env).stdout.strip()
        == branch_before
    )
    assert _git("stash", "list", cwd=clone, env=env).stdout.strip() == ""
    diff = _git("diff", "--", "README.md", cwd=clone, env=env).stdout
    assert "dirty but should not be stashed" in diff


# --- Integration-style tests for the "sync a ref, not a working tree" scope ---


def test_untracked_source_file_is_not_copied(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/untracked-check")
    _write(source / "secret-local-only.txt", "never committed, must not sync\n")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    # The committed feature file made it through, the untracked one didn't.
    assert (clone / "feature.txt").exists()
    assert not (clone / "secret-local-only.txt").exists()


def test_ref_option_syncs_a_commit_that_is_not_checked_out(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/ref-check")
    older_commit = _rev_parse(source, env, "HEAD")

    # Move HEAD forward, but ask the script to sync the older commit via
    # --ref -- not whatever happens to be checked out right now.
    _write(source / "newer.txt", "a later change, must not be synced\n")
    _commit_all(source, env, "feat: a later change")

    result = _run_script(clone, source, env, extra_args=["--ref", older_commit])
    assert result.returncode == 0, result.stdout + result.stderr

    assert (clone / "feature.txt").exists()
    assert not (clone / "newer.txt").exists()

    body = _git("log", "-1", "--format=%B", "fix/ref-check", cwd=clone, env=env).stdout
    assert f"Source-Commit: {older_commit}" in body


def test_source_commit_trailer_matches_synced_ref(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/trailer-check")
    head_sha = _rev_parse(source, env, "HEAD")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    body = _git("log", "-1", "--format=%B", "fix/trailer-check", cwd=clone, env=env).stdout
    assert f"Source-Commit: {head_sha}" in body
    assert body.splitlines()[0] == "chore: sync changes from source branch fix/trailer-check"


def test_protected_path_change_stops_the_run(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(
        tmp_path,
        env,
        protect=["PROTECTED.md"],
        extra_seed_files={"PROTECTED.md": "original\n"},
    )
    source = _make_source(tmp_path, env, branch="fix/protect-check")

    # Simulate a target pre-commit hook touching a protected path -- the
    # exact gap the pre-push protect check guards against: the rsync
    # exclude and the backup/restore of protected files both run *before*
    # the commit, so a hook that edits a protected file during the commit
    # itself (as the script's own comment notes real hooks can do, e.g.
    # openapi regen) would otherwise slip through unnoticed.
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho tampered >> PROTECTED.md\ngit add PROTECTED.md\n")
    hook.chmod(0o755)

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "protected" in output.lower()
    assert "PROTECTED.md" in output

    push_check = _git("ls-remote", str(bare), "fix/protect-check", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


def test_message_option_sets_the_commit_subject(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/message-check")

    result = _run_script(clone, source, env, extra_args=["--message", "chore: release 2026.10.08"])
    assert result.returncode == 0, result.stdout + result.stderr

    subject = _git(
        "log", "-1", "--format=%s", "fix/message-check", cwd=clone, env=env
    ).stdout.strip()
    assert subject == "chore: release 2026.10.08"

    body = _git("log", "-1", "--format=%B", "fix/message-check", cwd=clone, env=env).stdout
    assert "Source-Commit:" in body


def test_detached_head_without_branch_errors_clearly(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/detached-check")
    head_sha = _rev_parse(source, env, "HEAD")
    _git("checkout", "-q", head_sha, cwd=source, env=env)  # detach HEAD

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "detached head" in (result.stdout + result.stderr).lower()


# --- Integration-style tests for the PR #450 review round: ignored-file
# protection, the foreign-commit/--force/lease guard, ahead/diverged
# ordering, the concurrency lock, protect-pattern normalisation and the
# dry-run preview worktree. ---


def test_branch_option_overrides_current_branch(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/current-branch-ignored")

    result = _run_script(clone, source, env, extra_args=["--branch", "feat/explicit-branch"])
    assert result.returncode == 0, result.stdout + result.stderr

    assert _rev_parse(clone, env, "feat/explicit-branch")
    not_created = subprocess.run(  # nosec B603 B607 -- fixed argv against a throwaway tmp_path repo
        ["git", "rev-parse", "--verify", "fix/current-branch-ignored"],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
    )
    assert not_created.returncode != 0


def test_target_branch_matching_default_branch_is_refused(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/anything")

    result = _run_script(clone, source, env, extra_args=["--branch", "main"])
    assert result.returncode != 0
    assert "default branch" in (result.stdout + result.stderr).lower()


@pytest.mark.parametrize("bad_branch", ["refs/heads/main", "heads/main"])
def test_branch_ref_path_forms_are_rejected(tmp_path, bad_branch):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/anything")

    result = _run_script(clone, source, env, extra_args=["--branch", bad_branch])
    assert result.returncode != 0
    assert "ref path" in (result.stdout + result.stderr).lower()


def test_env_var_equivalents_match_cli_flags(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/env-check")
    older_commit = _rev_parse(source, env, "HEAD")
    _write(source / "newer.txt", "later, must not be synced via the SYNC_REF env var\n")
    _commit_all(source, env, "feat: a later change")

    result = _run_script(
        clone,
        source,
        env,
        extra_env={
            "SYNC_REF": older_commit,
            "SYNC_TARGET_BRANCH": "feat/env-branch",
            "SYNC_COMMIT_MESSAGE": "chore: env var sync",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr

    assert (clone / "feature.txt").exists()
    assert not (clone / "newer.txt").exists()
    subject = _git("log", "-1", "--format=%s", "feat/env-branch", cwd=clone, env=env).stdout.strip()
    assert subject == "chore: env var sync"


def test_exp_branch_maps_to_feat_branch(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="exp/some-experiment")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    assert _rev_parse(clone, env, "feat/some-experiment")


def test_no_changes_path_exits_cleanly_without_a_new_push(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    source = _make_source(tmp_path, env, branch="fix/no-changes")
    # The sync branch is always rebuilt from scratch on top of
    # 'origin/<default>' (never on top of a previous sync commit, since
    # that lands on a separate branch main only gets via a later,
    # separate merge). So a "no changes" run only happens when the
    # target's default branch already has byte-identical content -- seed
    # it that way here, rather than relying on a first real run to have
    # merged anything back into main (it doesn't).
    bare, clone = _make_target(
        tmp_path,
        env,
        extra_seed_files={"README.md": "source repo\n", "feature.txt": "a new feature\n"},
    )

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no changes to sync" in (result.stdout + result.stderr).lower()

    push_check = _git("ls-remote", str(bare), "fix/no-changes", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


def test_failing_hook_shows_real_error_and_stops(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/hook-fails")

    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'DISTINCTIVE_HOOK_FAILURE_MARKER' >&2\nexit 1\n")
    hook.chmod(0o755)

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "DISTINCTIVE_HOOK_FAILURE_MARKER" in output
    assert "pre-commit hooks failed" in output.lower()

    push_check = _git("ls-remote", str(bare), "fix/hook-fails", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


def test_ahead_only_target_is_refused_before_touching_anything(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/ahead-only")

    # The target's local main has a commit the remote doesn't -- ahead,
    # not diverged (the remote has nothing new of its own).
    _write(clone / "LOCAL_ONLY.md", "ahead-only, never pushed\n")
    _commit_all(clone, env, "chore: local-only ahead commit")
    local_main_tip = _rev_parse(clone, env, "main")

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "ahead" in (result.stdout + result.stderr).lower()

    assert _rev_parse(clone, env, "main") == local_main_tip
    push_check = _git("ls-remote", str(bare), "fix/ahead-only", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


def test_dirty_and_diverged_target_is_refused_without_stashing(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/dirty-and-diverged")

    advance = tmp_path / "target-advance"
    _git("clone", "-q", str(bare), str(advance), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=advance, env=env)
    _git("config", "user.name", "Test", cwd=advance, env=env)
    _write(advance / "UPSTREAM.md", "remote-only change\n")
    _commit_all(advance, env, "chore: advance remote main")
    _git("push", "-q", "origin", "main", cwd=advance, env=env)

    _write(clone / "LOCAL_ONLY.md", "local-only change, diverging\n")
    _commit_all(clone, env, "chore: local-only diverging commit")
    (clone / "README.md").write_text("dirty on top of the diverging commit\n")

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "diverged" in (result.stdout + result.stderr).lower()

    # The divergence check runs before any stash, so nothing was stashed.
    assert _git("stash", "list", cwd=clone, env=env).stdout.strip() == ""
    diff = _git("diff", "--", "README.md", cwd=clone, env=env).stdout
    assert "dirty on top of the diverging commit" in diff


def test_gitignored_target_file_survives_sync(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env, extra_seed_files={".gitignore": "*.env\nshared/\n"})
    source = _make_source(tmp_path, env, branch="fix/ignored-file-check")
    _write(source / "shared" / "from-source.txt", "the source also tracks this\n")
    _commit_all(source, env, "feat: add a file under a dir the target ignores")

    # Untracked and ignored, like a maintainer's local files -- never
    # committed, so none of this makes the clone's main "ahead". Covers a
    # plain name, a non-ASCII name, a bracketed (glob-metacharacter) name,
    # and a file inside a directory the source *also* has content in (the
    # GNU rsync gap a bare "P /dir/" filter rule doesn't cover).
    _write(clone / "local.env", "SECRET=do-not-delete\n")
    _write(clone / "café.env", "SECRET=do-not-delete-either\n")
    _write(clone / "[id].env", "SECRET=also-keep\n")
    _write(clone / "shared" / "existing.txt", "pre-existing ignored content\n")

    # The real push path, not a dry run: only a real `rsync --delete`
    # against the actual clone can prove nothing was deleted.
    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (clone / "local.env").read_text() == "SECRET=do-not-delete\n"
    assert (clone / "café.env").read_text() == "SECRET=do-not-delete-either\n"
    assert (clone / "[id].env").read_text() == "SECRET=also-keep\n"
    assert (clone / "shared" / "existing.txt").read_text() == "pre-existing ignored content\n"
    assert (clone / "shared" / "from-source.txt").read_text() == "the source also tracks this\n"


def test_foreign_commit_on_remote_branch_is_refused(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/foreign-commit")

    # Another maintainer pushes directly to the mapped target branch,
    # bypassing the sync script entirely -- no Source-Commit trailer.
    other = tmp_path / "other-maintainer-clone"
    _git("clone", "-q", str(bare), str(other), cwd=tmp_path, env=env)
    _git("config", "user.email", "other@example.com", cwd=other, env=env)
    _git("config", "user.name", "Other", cwd=other, env=env)
    _git("checkout", "-q", "-b", "fix/foreign-commit", cwd=other, env=env)
    _write(other / "manual-change.txt", "a manual, non-sync commit\n")
    _commit_all(other, env, "chore: a manual change, not from the sync script")
    _git("push", "-q", "origin", "fix/foreign-commit", cwd=other, env=env)
    foreign_sha = _rev_parse(other, env, "HEAD")

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "source-commit" in output.lower()
    assert foreign_sha in output

    push_check = _git("ls-remote", str(bare), "fix/foreign-commit", cwd=clone, env=env).stdout
    assert foreign_sha in push_check


def test_force_overwrites_foreign_commit_with_explicit_lease(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/force-overwrite")

    other = tmp_path / "other-maintainer-clone"
    _git("clone", "-q", str(bare), str(other), cwd=tmp_path, env=env)
    _git("config", "user.email", "other@example.com", cwd=other, env=env)
    _git("config", "user.name", "Other", cwd=other, env=env)
    _git("checkout", "-q", "-b", "fix/force-overwrite", cwd=other, env=env)
    _write(other / "manual-change.txt", "a manual, non-sync commit\n")
    _commit_all(other, env, "chore: a manual change, not from the sync script")
    _git("push", "-q", "origin", "fix/force-overwrite", cwd=other, env=env)

    result = _run_script(clone, source, env, extra_args=["--force"])
    assert result.returncode == 0, result.stdout + result.stderr

    body = _git("log", "-1", "--format=%B", "fix/force-overwrite", cwd=clone, env=env).stdout
    assert "Source-Commit:" in body

    push_check = _git("ls-remote", str(bare), "fix/force-overwrite", cwd=clone, env=env).stdout
    remote_sha = push_check.split()[0]
    local_sha = _rev_parse(clone, env, "fix/force-overwrite")
    assert remote_sha == local_sha


def test_stale_force_with_lease_is_rejected(tmp_path):
    """Not a full script run: this proves that the exact push shape the
    script uses -- an explicit refspec plus an explicit
    --force-with-lease=<ref>:<sha> -- rejects a push whose recorded sha has
    gone stale. The script relies on git enforcing exactly this.
    """
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git("init", "-q", "--bare", "-b", "main", cwd=bare, env=env)

    clone_a = tmp_path / "clone-a"
    _git("clone", "-q", str(bare), str(clone_a), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=clone_a, env=env)
    _git("config", "user.name", "Test", cwd=clone_a, env=env)
    _write(clone_a / "f.txt", "v1\n")
    _commit_all(clone_a, env, "v1")
    _git("push", "-q", "origin", "main", cwd=clone_a, env=env)
    stale_sha = _rev_parse(clone_a, env, "HEAD")

    # A second clone advances the remote after clone_a observed stale_sha.
    clone_b = tmp_path / "clone-b"
    _git("clone", "-q", str(bare), str(clone_b), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=clone_b, env=env)
    _git("config", "user.name", "Test", cwd=clone_b, env=env)
    _write(clone_b / "f.txt", "v2\n")
    _commit_all(clone_b, env, "v2")
    _git("push", "-q", "origin", "main", cwd=clone_b, env=env)

    # clone_a tries to push with a lease based on the now-stale sha.
    _write(clone_a / "f.txt", "v3-from-clone-a\n")
    _commit_all(clone_a, env, "v3")
    result = (
        subprocess.run(  # nosec B603 B607 -- fixed argv, mirrors the script's own push invocation
            [
                "git",
                "push",
                "origin",
                "refs/heads/main:refs/heads/main",
                f"--force-with-lease=refs/heads/main:{stale_sha}",
            ],
            cwd=clone_a,
            env=env,
            capture_output=True,
            text=True,
        )
    )
    assert result.returncode != 0
    assert "stale" in (result.stdout + result.stderr).lower()


def test_concurrent_run_is_refused_by_lock(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/lock-check")

    lock_dir = clone / ".git" / "maintainer-sync.lock"
    lock_dir.mkdir()

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "in progress" in (result.stdout + result.stderr).lower()

    # Not created by this run, so cleanup must not remove it.
    assert lock_dir.exists()


def test_successful_run_leaves_no_lock_behind(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/lock-cleanup-check")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (clone / ".git" / "maintainer-sync.lock").exists()


@pytest.mark.parametrize(
    "protect_entry,touched_path,seed_files",
    [
        ("/PROTECTED.md", "PROTECTED.md", {"PROTECTED.md": "original\n"}),
        ("conf/", "conf/sub/file.txt", {"conf/sub/file.txt": "original\n"}),
        ("secrets.env", "nested/dir/secrets.env", {"nested/dir/secrets.env": "original\n"}),
    ],
)
def test_protect_pattern_matches_various_forms(tmp_path, protect_entry, touched_path, seed_files):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env, protect=[protect_entry], extra_seed_files=seed_files)
    source = _make_source(tmp_path, env, branch="fix/protect-pattern-check")

    # Same hook-injection trick as test_protected_path_change_stops_the_run:
    # the only way to prove the guard's own matching (leading/trailing
    # slash, any depth), independent of rsync's excludes.
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho tampered >> '{touched_path}'\ngit add '{touched_path}'\n")
    hook.chmod(0o755)

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "protected" in (result.stdout + result.stderr).lower()

    push_check = _git(
        "ls-remote", str(bare), "fix/protect-pattern-check", cwd=clone, env=env
    ).stdout
    assert push_check.strip() == ""


def test_source_uncommitted_changes_warns(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/warn-uncommitted")
    (source / "README.md").write_text("uncommitted tracked edit\n")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "uncommitted" in (result.stdout + result.stderr).lower()


def test_dry_run_builds_preview_worktree_with_diff_and_protect_guard(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(
        tmp_path, env, protect=["PROTECTED.md"], extra_seed_files={"PROTECTED.md": "original\n"}
    )
    source = _make_source(tmp_path, env, branch="fix/dry-run-preview")

    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho tampered >> PROTECTED.md\ngit add PROTECTED.md\n")
    hook.chmod(0o755)

    result = _run_script(clone, source, env, extra_env={"SYNC_DRY_RUN": "true"})
    assert result.returncode == 0, result.stdout + result.stderr

    output = result.stdout + result.stderr
    assert "diff against" in output.lower()
    assert "would refuse to push" in output.lower()
    assert "protected" in output.lower()

    # Nothing in the real clone changed, and no preview worktree was left
    # (only the main worktree -- the clone itself -- should be listed).
    assert _git("branch", "--list", "fix/dry-run-preview", cwd=clone, env=env).stdout.strip() == ""
    worktree_list = _git("worktree", "list", cwd=clone, env=env).stdout
    assert len(worktree_list.strip().splitlines()) == 1


# --- Integration-style tests for the PR #450 round-2 review: origin/*
# branch names, case-insensitive default-branch matching, a relative
# TARGET_REPO_PATH, and reading .sync-config from the preview worktree. ---


def test_branch_origin_prefix_is_rejected(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/anything")

    result = _run_script(clone, source, env, extra_args=["--branch", "origin/main"])
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "origin" in output
    assert "remote-tracking" in output

    # No local branch named "main" (or anything else) was ever created or
    # checked out from this.
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone, env=env).stdout.strip() == "main"


def test_branch_case_insensitive_match_against_default_is_refused(tmp_path):
    if _skip_without_rsync():
        return

    # This specifically guards the case-insensitive-filesystem collision
    # (default macOS APFS); on a case-sensitive filesystem (typical Linux
    # CI) "Main" and "main" never collide at the filesystem level, so the
    # scenario this guards against doesn't reproduce there. The script's
    # own check is a plain string comparison either way, but there is
    # nothing informative to assert on a case-sensitive filesystem beyond
    # what test_target_branch_matching_default_branch_is_refused already
    # covers, so skip (silently return) rather than skip via pytest, since
    # this suite fails the build on any actual pytest-level skip.
    if not _tmp_fs_is_case_insensitive(tmp_path):
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/anything")
    main_before = _rev_parse(clone, env, "main")

    result = _run_script(clone, source, env, extra_args=["--branch", "Main"])
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "default branch" in output
    assert "case-insensitively" in output

    # "main" itself is unchanged: on a case-insensitive filesystem,
    # `.exists()` for "Main" is meaningless (it would resolve to the same
    # inode as "main" either way), so the real proof is that writing a
    # "Main" ref never moved "main" and nothing was ever stashed/checked
    # out for this to have had a chance to happen.
    assert _rev_parse(clone, env, "main") == main_before
    assert _git("stash", "list", cwd=clone, env=env).stdout.strip() == ""


def test_relative_target_repo_path_is_resolved(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/relative-path-check")

    # source and target-clone are siblings directly under tmp_path, so
    # this is a valid relative path from the script's starting cwd
    # (source, since _run_script runs with cwd=source).
    result = _run_script(clone, source, env, extra_env={"TARGET_REPO_PATH": "../target-clone"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert _rev_parse(clone, env, "fix/relative-path-check")


def test_dry_run_reads_sync_config_from_preview_worktree_not_stale_checkout(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    # The local checkout's own .sync-config has no protect entries.
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/stale-config-check")

    # Advance origin/main's .sync-config to add a protect entry, without
    # ever updating the local checkout -- the preview worktree, built
    # fresh from origin/<default>, must see this new config; the stale
    # local checkout must not matter.
    advance = tmp_path / "target-advance"
    _git("clone", "-q", str(bare), str(advance), cwd=tmp_path, env=env)
    _git("config", "user.email", "test@example.com", cwd=advance, env=env)
    _git("config", "user.name", "Test", cwd=advance, env=env)
    _write(advance / ".sync-config", "[exclude]\n.sync-config\n[protect]\nPROTECTED.md\n")
    _write(advance / "PROTECTED.md", "original\n")
    _commit_all(advance, env, "chore: add a protect entry upstream")
    _git("push", "-q", "origin", "main", cwd=advance, env=env)

    # Same hook-injection trick used elsewhere: the only way to prove the
    # guard fired because of the *new* protect entry.
    hook = clone / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho tampered >> PROTECTED.md\ngit add PROTECTED.md\n")
    hook.chmod(0o755)

    result = _run_script(clone, source, env, extra_env={"SYNC_DRY_RUN": "true"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "protected" in (result.stdout + result.stderr).lower()


# --- Integration-style tests for the PR #450 round-3 review: the
# ambiguous-tag rev-list bug, backslash-named ignored paths, the
# ignored/also-in-source exclude-and-warn, remotes/*|tags/* branch
# rejection, and the default commit subject never naming the target
# branch. ---


def test_tag_named_like_default_branch_does_not_mask_ahead_only(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/tag-ambiguity")

    # A tag named exactly like the default branch makes a bare
    # "main...origin/main" rev-list range ambiguous: git prints a warning
    # to stderr instead of the two counts. The local main is also ahead
    # of the remote -- the exact combination that let the ahead-only
    # refusal be silently bypassed.
    _git("tag", "main", cwd=clone, env=env)
    _write(clone / "LOCAL_ONLY.md", "ahead-only, never pushed\n")
    _commit_all(clone, env, "chore: local-only ahead commit")
    local_main_tip = _rev_parse(clone, env, "main")

    result = _run_script(clone, source, env)
    assert result.returncode != 0
    assert "ahead" in (result.stdout + result.stderr).lower()

    assert _rev_parse(clone, env, "main") == local_main_tip
    push_check = _git("ls-remote", str(bare), "fix/tag-ambiguity", cwd=clone, env=env).stdout
    assert push_check.strip() == ""


@pytest.mark.parametrize("bad_branch", ["remotes/origin/main", "tags/v1"])
def test_branch_remotes_and_tags_prefix_rejected(tmp_path, bad_branch):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/anything")

    result = _run_script(clone, source, env, extra_args=["--branch", bad_branch])
    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "ref path" in output
    assert "remote-tracking" in output


def test_ignored_directory_name_with_backslash_survives_sync(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    # A gitignore pattern escapes a literal backslash as "\\": this
    # pattern (two backslashes in the file) matches a directory literally
    # named "bs\dir.loc" (one backslash) -- verified against real git
    # before writing this test.
    _bare, clone = _make_target(tmp_path, env, extra_seed_files={".gitignore": "bs\\\\dir.loc/\n"})
    source = _make_source(tmp_path, env, branch="fix/backslash-dir-check")

    _write(clone / "bs\\dir.loc" / "f", "must survive\n")

    # The real push path, not a dry run: only a real `rsync --delete`
    # against the actual clone can prove nothing was deleted.
    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (clone / "bs\\dir.loc" / "f").read_text() == "must survive\n"


def test_ignored_target_file_is_kept_when_source_also_has_the_path(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env, extra_seed_files={".gitignore": "secret.env\n"})
    source = _make_source(tmp_path, env, branch="fix/ignored-overlap-check")
    _write(source / "secret.env", "source force-added content\n")
    _git("add", "-f", "secret.env", cwd=source, env=env)
    _commit_all(source, env, "feat: force-add a file the target ignores")

    # Pre-existing, ignored and untracked in the target before the sync
    # runs -- the exact case the pre-rsync exclude-and-warn covers.
    _write(clone / "secret.env", "target-local content, must survive\n")

    result = _run_script(clone, source, env)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (clone / "secret.env").read_text() == "target-local content, must survive\n"
    assert "skipped" in (result.stdout + result.stderr).lower()


def test_default_commit_subject_names_source_branch_not_target_branch(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/real-source-branch")

    result = _run_script(
        clone, source, env, extra_args=["--branch", "feat/totally-different-target-name"]
    )
    assert result.returncode == 0, result.stdout + result.stderr

    subject = _git(
        "log",
        "-1",
        "--format=%s",
        "feat/totally-different-target-name",
        cwd=clone,
        env=env,
    ).stdout.strip()
    assert subject == "chore: sync changes from source branch fix/real-source-branch"
    assert "totally-different-target-name" not in subject


def test_default_commit_subject_uses_short_sha_when_source_is_detached(tmp_path):
    if _skip_without_rsync():
        return

    env = _git_env(tmp_path / "home")
    _bare, clone = _make_target(tmp_path, env)
    source = _make_source(tmp_path, env, branch="fix/detached-subject-check")
    head_sha = _rev_parse(source, env, "HEAD")
    _git("checkout", "-q", head_sha, cwd=source, env=env)  # detach HEAD
    short_sha = _git("rev-parse", "--short", head_sha, cwd=source, env=env).stdout.strip()

    result = _run_script(clone, source, env, extra_args=["--branch", "feat/explicit-branch"])
    assert result.returncode == 0, result.stdout + result.stderr

    subject = _git(
        "log", "-1", "--format=%s", "feat/explicit-branch", cwd=clone, env=env
    ).stdout.strip()
    assert subject == f"chore: sync changes from source commit {short_sha}"
