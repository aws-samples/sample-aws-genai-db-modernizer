#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one security scan (#1844): semgrep, gitleaks and bandit, as ``ci/security.toml`` pins them.

Run through ``ci/security.sh``; pre-commit, the GitHub ``security`` job and
``ci/pre_merge.sh`` all do, so a clean run here is a clean run there.

    ci/security.sh                      every tracked file (the GitHub job, pre_merge.sh)
    ci/security.sh --quick [--base REF] the files changed since the merge base with REF
                                        (default origin/main), plus uncommitted and
                                        untracked ones (pre_merge.sh --quick)
    ci/security.sh FILE...              those files (the pre-commit hook)

Each scanner gets the same files in every mode, filtered the same way: semgrep skips
what ``.semgrepignore`` names, bandit only reads ``.py`` files and applies
``[tool.bandit]`` itself, gitleaks reads every file and applies ``.gitleaks.toml``.
Semgrep's rule sources, its environment and the gitleaks release are fetched once into a
cache (``SECURITY_CACHE_DIR``, default ``~/.cache/db-modernizer-security``). What each run
checks against the repository, never against the cache itself:

- the listed rule files' bytes against ``[semgrep] rules_sha256`` (a checkout whose files
  don't match is fetched again once, then the run exits 2), and the merged rules file is
  rebuilt from them;
- the gitleaks archive against its pinned SHA-256, the binary unpacked fresh from it;
- the semgrep environment, reinstalled with ``uv pip sync --reinstall --require-hashes
  --no-build`` from ``ci/semgrep-requirements.txt``, so every installed file comes from an
  archive whose hash the lock lists (9 to 14 s measured). ``--skip-env-reinstall`` (the pre-commit
  hook) syncs without ``--reinstall``: that checks the installed versions, not the files.

``--print-rules-sha256`` fetches the pinned sources into a fresh folder and prints the hash
to write into ``ci/security.toml`` after moving a rule pin.

Exit 0 when every scanner is clean, 1 on a finding, 2 when a scanner could not run: a fetch
or checksum failure, a missing tool, or a scanner that exits with an error rather than
findings. Semgrep and bandit findings are read from their JSON reports, so a crash that exits
1 is "could not run", not a finding; gitleaks gets ``--exit-code 3``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess  # nosec B404 -- runs git and the pinned scanners with fixed argv
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from collections.abc import Iterable, Sequence
from pathlib import Path

import pathspec
import yaml

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / "ci" / "security.toml"
SEMGREPIGNORE = REPO / ".semgrepignore"
SCANNERS = ("semgrep", "gitleaks", "bandit")
# The merged rules file semgrep reads during a run; .gitignore lists the pattern.
RULES_IN_REPO_PREFIX = ".security-semgrep-rules."
REQUIREMENTS = REPO / "ci" / "semgrep-requirements.txt"
# Bandit reads the words of a ``# nosec B603 -- reason`` comment as test names, and warns
# about a ``nosec`` that suppresses nothing on that line of a multi-line call; both are
# logged once per occurrence and are not findings.
BANDIT_NOISE = re.compile(
    r"^\[manager\]\s+WARNING\s+Test in comment: .* is not a test name or id"
    r"|^\[tester\]\s+WARNING\s+nosec encountered \(B\d+\), but no failed test"
)
# Exit codes that mean "findings" (anything else non-zero means the scanner could not run).
# Gitleaks is given --exit-code 3, since it also exits 1 on a fatal error.
FINDING_CODES = {"semgrep": 1, "gitleaks": 3, "bandit": 1}


class SecurityError(Exception):
    """A setup problem (a fetch, a checksum, a missing rule file): the run cannot decide."""


def load_config(path: Path = CONFIG) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def cache_dir() -> Path:
    override = os.environ.get("SECURITY_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "db-modernizer-security"


# ---------------------------------------------------------------- which files


_LOCAL_ENV_VARS: frozenset[str] | None = None


def _cache_env() -> dict[str, str]:
    """The environment for git in a cache checkout, without git's repository-location variables.

    Git sets ``GIT_DIR``, ``GIT_INDEX_FILE`` and others for a hook; inherited, they would point
    git in a cache checkout at the repository being committed to. Only the variables
    ``git rev-parse --local-env-vars`` lists are dropped, so ``GIT_SSL_CAINFO``,
    ``GIT_ASKPASS`` and the like still apply to the fetch.
    """
    global _LOCAL_ENV_VARS
    if _LOCAL_ENV_VARS is None:
        out = subprocess.run(  # nosec B603 B607 -- fixed git argv
            ["git", "rev-parse", "--local-env-vars"], check=True, capture_output=True, text=True
        ).stdout
        _LOCAL_ENV_VARS = frozenset(out.split())
    return {k: v for k, v in os.environ.items() if k not in _LOCAL_ENV_VARS}


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed git argv
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, env=env
    ).stdout
    return [line for line in out.splitlines() if line]


def tracked_files(root: Path = REPO) -> list[str]:
    return _git(root, "ls-files")


def changed_files(base: str, root: Path = REPO) -> list[str]:
    """Files changed since the merge base with ``base``, plus uncommitted and untracked ones."""
    merge_base = _git(root, "merge-base", "HEAD", base)[0]
    names = set(_git(root, "diff", "--name-only", "--diff-filter=d", merge_base))
    names |= set(_git(root, "diff", "--name-only", "--diff-filter=d", "HEAD"))
    names |= set(_git(root, "ls-files", "--others", "--exclude-standard"))
    return sorted(names)


def existing_files(paths: Iterable[str], root: Path = REPO) -> list[str]:
    """Repository-relative regular files (no symlinks, no deleted paths), sorted, unique.

    A path outside ``root`` (absolute or ``..``) is dropped: only repository files are scanned.
    """
    keep = set()
    top = root.resolve()
    for name in paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            keep.add(path.resolve().relative_to(top).as_posix())
        except ValueError:
            continue
    return sorted(keep)


def semgrep_targets(files: Sequence[str], ignore_text: str) -> list[str]:
    """The files semgrep scans: explicit targets bypass ``.semgrepignore``, so filter here."""
    spec = pathspec.GitIgnoreSpec.from_lines(ignore_text.splitlines())
    return [f for f in files if not spec.match_file(f)]


def bandit_targets(files: Sequence[str]) -> list[str]:
    """Python files; bandit applies ``[tool.bandit]`` ``exclude_dirs`` to explicit files too."""
    return [f for f in files if f.endswith(".py")]


# ---------------------------------------------------------------- semgrep rules


def rule_id(id_prefix: str, rule_file: str, own_id: str) -> str:
    """The registry form of a rule's id: prefix, the file path dotted and lowercased, own id.

    The registry lowercases the path part only: ``detect-pseudoRandomBytes.yaml`` holding rule
    ``detect-pseudoRandomBytes`` is
    ``javascript.lang.security.detect-pseudorandombytes.detect-pseudoRandomBytes``.
    """
    stem = rule_file.rsplit(".", 1)[0].replace("/", ".").lower()
    return ".".join(p for p in (id_prefix, stem, own_id) if p)


def checkout_problem(path: Path, commit: str) -> str | None:
    """Why a cached checkout can't be trusted, or None: HEAD must be ``commit``, tree clean."""
    if not (path / ".git").exists():
        return "missing"
    try:
        head = _git(path, "rev-parse", "HEAD", env=_cache_env())
        dirty = _git(
            path, "status", "--porcelain", "--ignored", "--untracked-files=all", env=_cache_env()
        )
    except subprocess.CalledProcessError:
        return "not a readable git checkout"
    if head != [commit]:
        return f"HEAD is {head[0] if head else 'unset'}, not {commit}"
    if dirty:
        return f"{len(dirty)} changed or extra file(s), e.g. {dirty[0].strip()}"
    return None


def missing_rule_files(path: Path, rules: Sequence[str]) -> list[str]:
    """Listed rule files the checkout's git tree lacks, compared with exact case.

    Read from ``git ls-tree``, not the filesystem: a case-insensitive filesystem (macOS)
    would find ``usesriforcdns.yaml`` where the tree has ``UseSRIForCDNs.yaml``.
    """
    tree = set(_git(path, "ls-tree", "-r", "--name-only", "HEAD", env=_cache_env()))
    return [r for r in rules if r not in tree]


def fetch_source(url: str, commit: str, dest: Path, refetch: bool = False) -> Path:
    """A checkout of ``url`` at ``commit`` under ``dest``; fetched again when ``refetch`` is set
    or the cached one's HEAD or tree is off. Its content is checked by ``rules_digest``."""
    target = dest / commit
    if not refetch and checkout_problem(target, commit) is None:
        return target
    shutil.rmtree(target, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="fetch-", dir=dest))
    try:
        for argv in (
            ["init", "-q"],
            ["fetch", "-q", "--depth", "1", url, commit],
            ["checkout", "-q", "FETCH_HEAD"],
        ):
            subprocess.run(  # nosec B603 B607 -- fixed git argv, pinned commit
                ["git", *argv], cwd=work, check=True, capture_output=True, env=_cache_env()
            )
        problem = checkout_problem(work, commit)
        if problem:
            raise SecurityError(f"{url} at {commit}: {problem}")
        try:
            work.rename(target)
        except OSError:  # a parallel run got there first; use it only if it checks out
            if checkout_problem(target, commit):
                raise
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode(errors="replace").strip() if exc.stderr else ""
        raise SecurityError(f"could not fetch {url} at {commit}: {err}") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return target


def merged_rules(sources: Sequence[dict], checkouts: Sequence[Path]) -> dict:
    """One semgrep config holding every listed rule, each under its registry id."""
    rules: list[dict] = []
    for source, root in zip(sources, checkouts, strict=True):
        missing = missing_rule_files(root, source["rules"])
        if missing:
            raise SecurityError(
                f"{source['name']} at {source['commit']} has no {', '.join(missing)} "
                "(exact case); fix ci/security.toml or pick another commit"
            )
        for rule_file in source["rules"]:
            text = (root / rule_file).read_text(encoding="utf-8")
            for rule in yaml.safe_load(text)["rules"]:
                rules.append({**rule, "id": rule_id(source["id_prefix"], rule_file, rule["id"])})
    return {"rules": rules}


def rules_digest(sources: Sequence[dict], checkouts: Sequence[Path]) -> str:
    """SHA-256 over every listed rule file's source, path and bytes, as semgrep would read them.

    The bytes come from the files on disk, so neither a ``--skip-worktree`` edit nor
    ``git replace`` (both invisible to ``git status``) gets past it.
    """
    digest = hashlib.sha256()
    for source, root in zip(sources, checkouts, strict=True):
        for rule_file in source["rules"]:
            path = root / rule_file
            data = path.read_bytes() if path.is_file() else b""
            digest.update(f"{source['name']}\0{rule_file}\0{len(data)}\0".encode())
            digest.update(data)
    return digest.hexdigest()


def _checkouts(sources: Sequence[dict], root: Path, refetch: bool = False) -> list[Path]:
    return [fetch_source(s["url"], s["commit"], root / s["name"], refetch) for s in sources]


def checked_checkouts(config: dict) -> list[Path]:
    """The rule checkouts, their listed files matching ``rules_sha256``; refetched once if not."""
    sources, pin = config["semgrep"]["source"], config["semgrep"]["rules_sha256"]
    checkouts = _checkouts(sources, cache_dir() / "semgrep")
    got = rules_digest(sources, checkouts)
    if got != pin:
        checkouts = _checkouts(sources, cache_dir() / "semgrep", refetch=True)
        got = rules_digest(sources, checkouts)
    if got != pin:
        raise SecurityError(
            f"the pinned rule files hash to {got}, but ci/security.toml pins {pin}; after "
            "moving a rule pin, write the value ./ci/security.sh --print-rules-sha256 prints"
        )
    return checkouts


def build_rules(config: dict, out: Path) -> None:
    """Write the merged rules to ``out`` on every run, from checkouts checked on this run."""
    sources = config["semgrep"]["source"]
    checkouts = checked_checkouts(config)
    out.write_text(yaml.safe_dump(merged_rules(sources, checkouts), sort_keys=False), "utf-8")


def fresh_rules_sha256(config: dict) -> str:
    """The rule files' hash from a fresh fetch of the pinned commits (the pin-bump helper)."""
    sources = config["semgrep"]["source"]
    with tempfile.TemporaryDirectory(prefix="security-rules-") as tmp:
        checkouts = _checkouts(sources, Path(tmp))
        for source, root in zip(sources, checkouts, strict=True):
            missing = missing_rule_files(root, source["rules"])
            if missing:
                raise SecurityError(f"{source['name']} at {source['commit']} has no {missing}")
        return rules_digest(sources, checkouts)


# ---------------------------------------------------------------- semgrep environment


def _uv(*args: str) -> None:
    try:
        subprocess.run(  # nosec B603 B607 -- fixed uv argv
            ["uv", *args], check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise SecurityError("uv is not on PATH (https://docs.astral.sh/uv/)") from exc
    except subprocess.CalledProcessError as exc:
        last = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise SecurityError(f"uv {args[0]} {args[1]} failed: {last[0]}") from exc


def semgrep_executable(
    config: dict, requirements: Path = REQUIREMENTS, reinstall: bool = True
) -> Path:
    """Semgrep from a dedicated environment, reinstalled from the hashed lock on every run.

    ``--reinstall`` rewrites every installed file from an archive whose hash the lock lists,
    so a swapped entry point or package file is replaced; without it (the pre-commit hook),
    only the installed versions are checked. ``--no-build`` refuses an sdist, whose build
    dependencies the lock does not hash.
    """
    key = hashlib.sha256(requirements.read_bytes()).hexdigest()[:16]
    env = cache_dir() / "semgrep-env" / key
    python = env / "bin" / "python"
    if not python.exists():
        shutil.rmtree(env, ignore_errors=True)
        _uv("venv", "--quiet", "--python", "3.12", str(env))
    # sync, not install: it removes anything the lock does not list, and refuses an
    # archive whose hash is not in it.
    flags = ["--quiet", "--require-hashes", "--no-build", *(["--reinstall"] if reinstall else [])]
    _uv("pip", "sync", *flags, "--python", str(python), str(requirements))
    return env / "bin" / "semgrep"


# ---------------------------------------------------------------- gitleaks binary


def gitleaks_platform() -> str:
    system = platform.system().lower()
    machine = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return f"{system}_{machine}"


def gitleaks_archive(config: dict) -> bytes:
    """The pinned gitleaks release archive for this platform, its SHA-256 checked on every run.

    The archive is cached; one that no longer matches the pin is downloaded again.
    """
    gl = config["gitleaks"]
    version, plat = gl["version"], gitleaks_platform()
    digest = gl["sha256"].get(plat)
    if not digest:
        raise SecurityError(f"no pinned gitleaks {version} build for {plat} in ci/security.toml")
    cached = cache_dir() / "gitleaks" / f"gitleaks_{version}_{plat}.tar.gz"
    if cached.exists():
        data = cached.read_bytes()
        if hashlib.sha256(data).hexdigest() == digest:
            return data
        cached.unlink()
    url = (
        f"https://github.com/gitleaks/gitleaks/releases/download/v{version}/"
        f"gitleaks_{version}_{plat}.tar.gz"
    )
    try:
        # A fixed https GitHub release URL, and the bytes are checked against the pin below.
        # nosemgrep: dynamic-urllib-use-detected -- fixed https URL, checksummed below
        with urllib.request.urlopen(url, timeout=60) as resp:  # nosec B310 -- https, checksummed
            data = bytes(resp.read())
    except OSError as exc:
        raise SecurityError(f"could not download {url}: {exc}") from exc
    got = hashlib.sha256(data).hexdigest()
    if got != digest:
        raise SecurityError(f"{url}: SHA-256 {got}, expected {digest}")
    cached.parent.mkdir(parents=True, exist_ok=True)
    tmp = cached.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cached)
    return data


def unpack_gitleaks(data: bytes, dest: Path) -> Path:
    """The ``gitleaks`` binary from a checked archive, unpacked into ``dest`` for this run."""
    archive = dest / "gitleaks.tar.gz"
    archive.write_bytes(data)
    with tarfile.open(archive) as tar:
        tar.extract(tar.getmember("gitleaks"), dest, filter="data")
    binary = dest / "gitleaks"
    binary.chmod(0o755)
    return binary


# ---------------------------------------------------------------- the scanners


def _step(name: str, count: int) -> None:
    print(f"\n==> {name} ({count} file{'s' if count != 1 else ''})", flush=True)


def _run(cmd: Sequence[str], cwd: Path) -> int:
    try:
        return subprocess.run(list(cmd), cwd=cwd).returncode  # nosec B603 -- fixed argv
    except OSError as exc:
        raise SecurityError(f"could not start {cmd[0]}: {exc}") from exc


def run_semgrep(config: dict, files: Sequence[str]) -> int:
    targets = semgrep_targets(files, SEMGREPIGNORE.read_text(encoding="utf-8"))
    _step(f"semgrep {config['semgrep']['version']}, pinned rules", len(targets))
    if not targets:
        return 0
    semgrep = semgrep_executable(config, reinstall=config.get("_reinstall_env", True))
    # Semgrep prefixes a local rule file's ids with the file's folder unless the file sits
    # in the working directory, and run from outside the repository it is many times
    # slower. So the merged rules go into a git-ignored file at the repository root for the
    # run: the ids stay the registry's, the ids the repository's suppression comments name.
    local = REPO / f"{RULES_IN_REPO_PREFIX}{os.getpid()}.yaml"
    report = REPO / f"{RULES_IN_REPO_PREFIX}{os.getpid()}.json"
    try:
        build_rules(config, local)
        cmd = [
            str(semgrep),
            "scan",
            "--config",
            local.name,
            "--metrics=off",
            "--disable-version-check",
            "--error",
            "--quiet",
            # No per-rule time limit: a rule that times out on a slow machine would skip a
            # file there and not elsewhere, and the verdict would depend on the machine.
            "--timeout",
            "0",
            f"--json-output={report}",
            *targets,
        ]
        return classify("semgrep", _run(cmd, REPO), report)
    finally:
        local.unlink(missing_ok=True)
        report.unlink(missing_ok=True)


def report_results(report: Path) -> int | None:
    """How many results a scanner's JSON report holds, or None when there is no readable report."""
    try:
        results = json.loads(report.read_text(encoding="utf-8")).get("results")
    except (OSError, ValueError, AttributeError):
        return None
    return len(results) if isinstance(results, list) else None


def classify(name: str, rc: int, report: Path) -> int:
    """Exit 1 is a finding only when the JSON report holds results; otherwise it is a crash."""
    if rc == 1 and not report_results(report):
        raise SecurityError(f"{name} exited 1 with no results in its JSON report (a crash)")
    return rc


def run_gitleaks(config: dict, files: Sequence[str]) -> int:
    """Gitleaks over a copy of exactly ``files``, so local caches and ``.venv`` stay out."""
    _step(f"gitleaks {config['gitleaks']['version']}", len(files))
    if not files:
        return 0
    data = gitleaks_archive(config)
    gl_config = REPO / config["gitleaks"]["config"]
    with tempfile.TemporaryDirectory(prefix="gitleaks-bin-") as bindir:
        binary = unpack_gitleaks(data, Path(bindir))
        with tempfile.TemporaryDirectory(prefix="gitleaks-") as tmp:
            for name in files:
                dest = Path(tmp) / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPO / name, dest)
            cmd = [str(binary), "dir", ".", "--config", str(gl_config), "--redact", "--no-banner"]
            cmd += ["--exit-code", str(FINDING_CODES["gitleaks"])]
            return _run(cmd, Path(tmp))


def run_bandit(config: dict, files: Sequence[str]) -> int:
    targets = bandit_targets(files)
    _step("bandit", len(targets))
    if not targets:
        return 0
    with tempfile.TemporaryDirectory(prefix="bandit-") as tmp:
        report = Path(tmp) / "bandit.json"
        cmd = [sys.executable, "-m", "bandit", "-q", "-c", config["bandit"]["config"]]
        cmd += ["-f", "json", "-o", str(report), *targets]
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)  # nosec B603 -- argv
        for line in (proc.stdout + proc.stderr).splitlines():
            if not BANDIT_NOISE.search(line):
                print(line)
        for issue in _bandit_issues(report):
            print(issue)
        return classify("bandit", proc.returncode, report)


def _bandit_issues(report: Path) -> list[str]:
    try:
        results = json.loads(report.read_text(encoding="utf-8")).get("results") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [
        f"{r.get('filename')}:{r.get('line_number')}: {r.get('test_id')} "
        f"[{r.get('issue_severity')}/{r.get('issue_confidence')}] {r.get('issue_text')}"
        for r in results
    ]


RUNNERS = {"semgrep": run_semgrep, "gitleaks": run_gitleaks, "bandit": run_bandit}


def select_files(args: argparse.Namespace) -> tuple[str, list[str]]:
    if args.files:
        return f"{len(args.files)} given file(s)", existing_files(args.files)
    if args.quick:
        return f"changed since the merge base with {args.base}", existing_files(
            changed_files(args.base)
        )
    return "every tracked file", existing_files(tracked_files())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ci/security.sh", description=__doc__.split("\n")[0])
    parser.add_argument("--quick", action="store_true", help="scan only the changed files")
    parser.add_argument("--base", default="origin/main", help="--quick's base (origin/main)")
    parser.add_argument("--only", choices=SCANNERS, action="append", help="run just this one")
    parser.add_argument(
        "--skip-env-reinstall",
        action="store_true",
        help="sync the semgrep environment without --reinstall (the pre-commit hook)",
    )
    parser.add_argument(
        "--print-rules-sha256",
        action="store_true",
        help="print the pinned rule files' SHA-256 from a fresh fetch, for ci/security.toml",
    )
    parser.add_argument("files", nargs="*", help="scan exactly these files")
    args = parser.parse_args(argv)
    config = load_config()
    if args.print_rules_sha256:
        try:
            print(fresh_rules_sha256(config))
        except SecurityError as exc:
            print(f"security: {exc}", file=sys.stderr)
            return 2
        return 0
    config["_reinstall_env"] = not args.skip_env_reinstall
    try:
        scope, files = select_files(args)
    except subprocess.CalledProcessError as exc:
        print(f"security: could not list the files to scan: {exc}", file=sys.stderr)
        return 2
    print(f"security: {len(files)} file(s), {scope}")
    failed, broken = [], []
    for name in args.only or SCANNERS:
        try:
            rc = RUNNERS[name](config, files)
        except SecurityError as exc:
            print(f"security: {name} could not run: {exc}", file=sys.stderr)
            broken.append(name)
            continue
        if rc == FINDING_CODES[name]:
            failed.append(name)
        elif rc != 0:
            print(f"security: {name} could not run: it exited {rc}", file=sys.stderr)
            broken.append(name)
    if broken:
        print(f"\nsecurity: COULD NOT RUN ({', '.join(broken)})")
        return 2
    if failed:
        print(f"\nsecurity: FAILED ({', '.join(failed)})")
        return 1
    print("\nsecurity: OK (" + ", ".join(args.only or SCANNERS) + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
