# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CI runs the mysqld integration tests for step 1's routine scripts (#2274).

``ci/test.sh`` deselects ``-m integration``, so the tests that run the generated MySQL
scripts on a real server need an entry point and a job of their own. These guards keep
the two in step: the script lists exactly the files that need a server, runs them under
``--fail-on-skip``, pins the server; the GitHub job calls the script and nothing else.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import shlex
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ci" / "test-mysqld.sh"
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
TEST_SH = REPO / "ci" / "test.sh"


def _code(text: str) -> list[str]:
    """The commands of a shell script: comments dropped, backslash continuations joined."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        out.append((pending + line).strip())
        pending = ""
    return [c for c in out if c]


def _pytest_line(text: str, start: str) -> str:
    lines = [c for c in _code(text) if c.startswith(start)]
    assert len(lines) == 1, f"expected one `{start}` command, found {lines}"
    return lines[0]


def _files(text: str | None = None) -> list[str]:
    block = re.search(r"MYSQLD_TEST_FILES=\((.*?)\)", text or SCRIPT.read_text(), re.S)
    assert block, "ci/test-mysqld.sh must list its test files in MYSQLD_TEST_FILES=(...)"
    return block.group(1).split()


def script_problems(text: str) -> list[str]:
    """What is wrong with ci/test-mysqld.sh's commands (comments and log lines do not count)."""
    problems = []
    code = _code(text)
    cmd = next((c for c in code if c.startswith("uv run pytest ")), "")
    if not re.match(r"uv run pytest -m integration --fail-on-skip\b", cmd):
        problems.append("the pytest command must start `-m integration --fail-on-skip`")
    if "MYSQLD_TEST_FILES[@]" not in cmd:
        problems.append("the pytest command must run MYSQLD_TEST_FILES")
    url = next((c for c in code if c.startswith("MYSQL_URL=")), "")
    if not url.startswith('MYSQL_URL="https://cdn.mysql.com/'):
        problems.append("MYSQL_URL must be https from cdn.mysql.com")
    for series in ("8.0", "8.4"):
        pattern = rf'MYSQL_VERSION="{re.escape(series)}\.\d+"'
        if sum(bool(re.fullmatch(pattern, c)) for c in code) != 1:
            problems.append(f"MYSQL_VERSION must be pinned to one exact {series}.x")
    for name in ("MYSQL_SHA256", "MYSQLD_BIN_SHA256", "MYSQL_BIN_SHA256"):
        pins = [c for c in code if re.fullmatch(rf'{name}="[0-9a-f]{{64}}"', c)]
        if len(pins) != 2 or len(set(pins)) != 2:
            problems.append(f"{name} must be a SHA-256, one per series (8.0 and 8.4)")
    if "https://cdn.mysql.com/archives/mysql-${MYSQL_SERIES}/" not in url:
        problems.append("MYSQL_URL must follow the series")
    check = next(
        (
            n
            for n, c in enumerate(code)
            if re.fullmatch(r'if \[ "\$\(sha256_of "\$archive"\)" != "\$MYSQL_SHA256" \]; then', c)
        ),
        None,
    )
    untar = next((n for n, c in enumerate(code) if c.startswith("tar -x")), None)
    if check is None or untar is None or check > untar:
        problems.append("the archive's SHA-256 must be compared before it is unpacked")
    elif code[check + 1 : check + 4].count("exit 2") != 1:
        problems.append("a checksum mismatch must exit")
    if not any("binaries_ok" in c and c.startswith("if ! binaries_ok") for c in code):
        problems.append("the unpacked binaries must be checked on every run")
    problems += _curl_problems(code)
    problems += _mariadb_problems(code)
    return problems


def _curl_problems(code: list[str]) -> list[str]:
    """Every download is https only, also after a redirect, never --insecure, and stalls end."""
    problems = []
    opts = re.search(r"CURL_OPTS=\((.*?)\)", "\n".join(code), re.S)
    words = opts.group(1).split() if opts else []
    for need in ("--proto", "'=https'", "--proto-redir", "--speed-limit", "--speed-time"):
        if need not in words:
            problems.append(f"CURL_OPTS must hold {need}")
    if words.count("'=https'") != 2:
        problems.append("CURL_OPTS must allow https only, for the first request and redirects")
    curls = [c for c in code if re.search(r"(^|\s|\()curl\s", c)]
    if not curls or any('curl "${CURL_OPTS[@]}" -o "$archive"' not in c for c in curls):
        problems.append('every curl must run as `curl "${CURL_OPTS[@]}" -o "$archive" ...`')
    if any(re.search(r"(^|\s)(-k|--insecure)(\s|$)", c) for c in curls + words):
        problems.append("no download may pass -k or --insecure")
    return problems


def _mariadb_problems(code: list[str]) -> list[str]:
    """The MariaDB pin of the collector's LOB-size tests (#2620), held to MySQL's bar."""
    problems = []
    if sum(bool(re.fullmatch(r'MARIADB_VERSION="10\.11\.\d+"', c)) for c in code) != 1:
        problems.append("MARIADB_VERSION must be pinned to one exact 10.11.x")
    for name in (
        "MARIADB_SHA256",
        "MARIADBD_BIN_SHA256",
        "MARIADB_BIN_SHA256",
        "MARIADB_TREE_SHA256",
    ):
        if sum(bool(re.fullmatch(rf'{name}="[0-9a-f]{{64}}"', c)) for c in code) != 1:
            problems.append(f"{name} must be one SHA-256")
    path = next((c for c in code if c.startswith("MARIADB_PATH=")), "")
    if not path.startswith('MARIADB_PATH="mariadb-${MARIADB_VERSION}/'):
        problems.append("MARIADB_PATH must follow the pinned version")
    urls = next((c for c in code if c.startswith("MARIADB_URLS=")), "")
    want = (
        '"https://mirror.mariadb.org/${MARIADB_PATH}" "https://archive.mariadb.org/${MARIADB_PATH}"'
    )
    if urls != f"MARIADB_URLS=({want})":
        problems.append("MARIADB_URLS must be the vendor's https mirror, then its archive")
    function = next((n for n, c in enumerate(code) if c == "install_pinned_mariadb() {"), None)
    body = code[function:] if function is not None else []
    end = body.index("}") if "}" in body else len(body)
    body = body[:end]
    check = next(
        (
            n
            for n, c in enumerate(body)
            if re.fullmatch(
                r'if \[ "\$\(sha256_of "\$archive"\)" != "\$MARIADB_SHA256" \]; then', c
            )
        ),
        None,
    )
    untar = next((n for n, c in enumerate(body) if c.startswith("tar -x")), None)
    if check is None or untar is None or check > untar:
        problems.append("the MariaDB archive's SHA-256 must be compared before it is unpacked")
    elif body[check + 1 : check + 4].count("exit 2") != 1:
        problems.append("a MariaDB checksum mismatch must exit")
    rechecks = [n for n, c in enumerate(body) if c == 'if ! mariadb_binaries_ok "$base"; then']
    if not rechecks or rechecks[0] > (untar or 0):
        problems.append("a cached MariaDB must be checked before it is used")
    after = [n for n in rechecks if untar is not None and n > untar]
    if not after or body[after[0] + 1 : after[0] + 4].count("exit 2") != 1:
        problems.append("the unpacked MariaDB must be checked, and a mismatch exit, after tar -x")
    paths = [c for c in body if c.startswith("export PATH=")]
    if paths != ['export PATH="$links:$PATH"']:
        problems.append("only the links folder may go on PATH for MariaDB")
    links = [c for c in body if c.startswith("ln ")]
    if links != [
        'ln -sf "$base/bin/mariadbd" "$links/mariadbd"',
        'ln -sf "$base/bin/mariadb" "$links/mariadb"',
    ]:
        problems.append("the links folder must hold only mariadbd and mariadb")
    if not any(c.startswith("install_pinned_mariadb") for c in code[end + (function or 0) :]):
        problems.append("the script must install the pinned MariaDB")
    return problems


def _shell_function(text: str, name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S)
    assert match, f"{name}() not found in ci/test-mysqld.sh"
    return match.group(0)


def _binaries_ok_exit(text: str, tmp_path: Path, mysqld_sha: str, mysql_sha: str) -> int:
    """Run the script's own binaries_ok() on a folder holding two stand-in binaries."""
    for name in ("mysqld", "mysql"):
        binary = tmp_path / "bin" / name
        binary.parent.mkdir(exist_ok=True)
        binary.write_text(f"stand-in {name}\n")
        binary.chmod(0o755)
    shell = "\n".join(
        [
            _shell_function(text, "sha256_of"),
            _shell_function(text, "binaries_ok"),
            f'MYSQLD_BIN_SHA256="{mysqld_sha}"',
            f'MYSQL_BIN_SHA256="{mysql_sha}"',
            f'binaries_ok "{tmp_path}"',
        ]
    )
    return subprocess.run(["bash", "-c", shell], capture_output=True).returncode


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _binaries_cases(text: str, tmp_path: Path) -> dict[str, int]:
    good_d, good_c = _sha("stand-in mysqld\n"), _sha("stand-in mysql\n")
    return {
        "both match": _binaries_ok_exit(text, tmp_path, good_d, good_c),
        "mysqld differs": _binaries_ok_exit(text, tmp_path, "0" * 64, good_c),
        "mysql differs": _binaries_ok_exit(text, tmp_path, good_d, "0" * 64),
    }


def test_binaries_ok_accepts_the_pinned_binaries_and_rejects_any_other(tmp_path: Path) -> None:
    cases = _binaries_cases(SCRIPT.read_text(), tmp_path)
    assert cases == {"both match": 0, "mysqld differs": 1, "mysql differs": 1}


def test_a_binaries_ok_that_accepts_anything_fails_the_behavioural_guard(tmp_path: Path) -> None:
    text = SCRIPT.read_text()
    body = _shell_function(text, "binaries_ok")
    mutated = text.replace(body, "binaries_ok() {\n  true\n}\n")
    assert mutated != text
    assert _binaries_cases(mutated, tmp_path)["mysqld differs"] == 0


# MariaDB (#2620): the script's own functions run on stand-in files, so a weakened check
# fails a test by what it lets through, not by how it reads.
_MARIADB_FUNCTIONS = (
    "sha256_of",
    "sha256_stdin",
    "tree_sha256",
    "mariadb_binaries_ok",
    "install_pinned_mariadb",
)
_STAND_IN = {
    "bin/mariadbd": "stand-in mariadbd\n",
    "bin/mariadb": "stand-in mariadb\n",
    "bin/my_print_defaults": "stand-in my_print_defaults\n",
    "bin/resolveip": "stand-in resolveip\n",
    "scripts/mariadb-install-db": "#!/bin/sh\necho stand-in install-db\n",
    "share/english/errmsg.sys": "stand-in messages\n",
    "lib/plugin/auth_pam.so": "stand-in plugin\n",
}


def _mariadb_name(text: str) -> str:
    match = re.search(r'^MARIADB_VERSION="([^"]+)"', text, re.M)
    assert match, "MARIADB_VERSION not found in ci/test-mysqld.sh"
    return f"mariadb-{match.group(1)}-linux-systemd-x86_64"


def _stand_in_tree(root: Path, name: str, changes: dict[str, str] | None = None) -> Path:
    base = root / name
    for rel, content in {**_STAND_IN, **(changes or {})}.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o755)
    (base / "scripts" / "mysql_install_db").symlink_to("mariadb-install-db")
    return base


def _tree_sha(base: Path) -> str:
    """tree_sha256's format, computed here on its own: a line per file and per link."""
    entries = [p for p in base.rglob("*") if p.is_symlink() or p.is_file()]
    lines = []
    for path in sorted(entries, key=lambda p: os.fsencode("./" + p.relative_to(base).as_posix())):
        rel = "./" + path.relative_to(base).as_posix()
        if path.is_symlink():
            lines.append(f"link {rel} {os.readlink(path)}\n")
        else:
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()} {rel}\n")
    return _sha("".join(lines))


def _pins(base: Path) -> dict[str, str]:
    return {
        "MARIADBD_BIN_SHA256": hashlib.sha256((base / "bin/mariadbd").read_bytes()).hexdigest(),
        "MARIADB_BIN_SHA256": hashlib.sha256((base / "bin/mariadb").read_bytes()).hexdigest(),
        "MARIADB_TREE_SHA256": _tree_sha(base),
    }


def _mariadb_shell(text: str, variables: dict[str, str], command: str) -> list[str]:
    """bash running the script's MariaDB functions on a Linux x86_64 it is told it runs on."""
    stubs = [
        "set -euo pipefail",
        "log() { :; }",
        "system_libraries() { :; }",
        'uname() { case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac; }',
        # The script passes GNU tar's --wildcards; a BSD tar matches the patterns as it is.
        "tar() { if command tar --version 2>/dev/null | grep -q GNU; then command tar"
        ' "$@"; else local a=() x; for x in "$@"; do [ "$x" = --wildcards ] || a+=("$x");'
        ' done; command tar "${a[@]}"; fi; }',
        'SCRATCH=""',
        "MARIADB_URLS=()",
    ]
    assigns = [f"{k}={shlex.quote(v)}" for k, v in variables.items()]
    funcs = [_shell_function(text, name) for name in _MARIADB_FUNCTIONS]
    return ["bash", "-c", "\n".join([*stubs, *assigns, *funcs, command])]


def _ok_exit(text: str, base: Path, pins: dict[str, str]) -> int:
    argv = _mariadb_shell(text, pins, f"mariadb_binaries_ok {shlex.quote(str(base))}")
    return subprocess.run(argv, capture_output=True).returncode


def _ok_cases(text: str, tmp_path: Path) -> dict[str, int]:
    name = _mariadb_name(text)
    good = _stand_in_tree(tmp_path / "good", name)
    pins = _pins(good)
    changed = _stand_in_tree(
        tmp_path / "script", name, {"scripts/mariadb-install-db": "#!/bin/sh\nevil\n"}
    )
    added = _stand_in_tree(tmp_path / "added", name, {"lib/plugin/extra.so": "planted\n"})
    moved = _stand_in_tree(tmp_path / "moved", name, {"scripts/other": "x\n"})
    (moved / "scripts" / "mysql_install_db").unlink()
    (moved / "scripts" / "mysql_install_db").symlink_to("other")
    (moved / "scripts" / "other").unlink()
    (moved / "scripts" / "other").write_text((good / "scripts" / "mariadb-install-db").read_text())
    return {
        "all match": _ok_exit(text, good, pins),
        "server differs": _ok_exit(text, good, {**pins, "MARIADBD_BIN_SHA256": "0" * 64}),
        "client differs": _ok_exit(text, good, {**pins, "MARIADB_BIN_SHA256": "0" * 64}),
        "a script changed": _ok_exit(text, changed, pins),
        "a plugin added": _ok_exit(text, added, pins),
        "a link moved": _ok_exit(text, moved, pins),
    }


def test_mariadb_binaries_ok_checks_the_binaries_and_every_unpacked_file(tmp_path: Path) -> None:
    assert _ok_cases(SCRIPT.read_text(), tmp_path) == {
        "all match": 0,
        "server differs": 1,
        "client differs": 1,
        "a script changed": 1,
        "a plugin added": 1,
        "a link moved": 1,
    }


_WEAKER_OK = {
    "accepts anything": (
        lambda t: t.replace(
            _shell_function(t, "mariadb_binaries_ok"), "mariadb_binaries_ok() {\n  true\n}\n"
        ),
        "server differs",
    ),
    "no client comparison": (
        lambda t: t.replace(
            '    && [ "$(sha256_of "$1/bin/mariadb")" = "$MARIADB_BIN_SHA256" ] \\\n', ""
        ),
        "client differs",
    ),
    "no folder comparison": (
        lambda t: t.replace('[ "$(tree_sha256 "$1")" = "$MARIADB_TREE_SHA256" ]', "true"),
        "a script changed",
    ),
}


@pytest.mark.parametrize("name", sorted(_WEAKER_OK))
def test_a_weaker_mariadb_binaries_ok_fails_the_behavioural_guard(
    name: str, tmp_path: Path
) -> None:
    mutate, case = _WEAKER_OK[name]
    text = SCRIPT.read_text()
    mutated = mutate(text)
    assert mutated != text, "the mutation must change the script"
    assert _ok_cases(mutated, tmp_path)[case] == 0


def _archive(tmp_path: Path, base: Path) -> Path:
    archive = tmp_path / f"{base.name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(base, arcname=base.name)
    return archive


def _install(text: str, tmp_path: Path, served: Path, pinned: Path) -> subprocess.CompletedProcess:
    """install_pinned_mariadb with a cold cache, MARIADB_TARBALL serving `served` and the
    pins of `pinned`; prints PATH and the links folder on success."""
    archive = _archive(tmp_path, served)
    cache = tmp_path / "cache"
    variables = {
        **_pins(pinned),
        "MARIADB_NAME": served.name,
        "MARIADB_VERSION": "stand-in",
        "MARIADB_SHA256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "MYSQL_CACHE_DIR": str(cache),
        "MARIADB_TARBALL": str(archive),
    }
    command = 'install_pinned_mariadb\necho "PATH=$PATH"\nls -A "$MYSQL_CACHE_DIR/mariadb-path"'
    argv = _mariadb_shell(
        text,
        {},
        "\n".join(f"export {k}={shlex.quote(v)}" for k, v in variables.items()) + "\n" + command,
    )
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _install_cases(text: str, tmp_path: Path) -> dict[str, subprocess.CompletedProcess]:
    name = _mariadb_name(text)
    good = _stand_in_tree(tmp_path / "good", name)
    other = _stand_in_tree(tmp_path / "other", name, {"bin/mariadbd": "another server\n"})
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    return {
        "pinned": _install(text, tmp_path / "a", good, good),
        "another server": _install(text, tmp_path / "b", other, good),
    }


def _only_links_on_path(run: subprocess.CompletedProcess, cache: Path, name: str) -> bool:
    lines = run.stdout.splitlines()
    path = next(line for line in lines if line.startswith("PATH="))[5:].split(":")
    links = cache / "mariadb-path"
    listed = sorted(line for line in lines if not line.startswith("PATH="))
    targets = {n: os.path.realpath(links / n) for n in listed}
    return (
        path[0] == str(links)
        and str(cache / name / "bin") not in path
        and listed == ["mariadb", "mariadbd"]
        and targets == {n: os.path.realpath(cache / name / "bin" / n) for n in listed}
    )


def test_install_pinned_mariadb_rechecks_the_unpacked_files_and_links_only_two(
    tmp_path: Path,
) -> None:
    text = SCRIPT.read_text()
    cases = _install_cases(text, tmp_path)
    pinned, other = cases["pinned"], cases["another server"]
    assert pinned.returncode == 0, pinned.stderr
    assert _only_links_on_path(pinned, tmp_path / "a" / "cache", _mariadb_name(text))
    assert other.returncode == 2
    assert "does not match the pinned SHA-256" in other.stderr


def test_an_install_that_skips_the_recheck_or_shadows_mysql_fails_the_guard(tmp_path: Path) -> None:
    text = SCRIPT.read_text()
    no_recheck = _mutations()["MariaDB not rechecked after unpack"](text)
    assert no_recheck != text
    assert _install_cases(no_recheck, tmp_path / "r")["another server"].returncode == 0
    bin_on_path = _mutations()["MariaDB bin folder on PATH"](text)
    assert bin_on_path != text
    run = _install_cases(bin_on_path, tmp_path / "p")["pinned"]
    assert run.returncode == 0, run.stderr
    assert not _only_links_on_path(run, tmp_path / "p" / "a" / "cache", _mariadb_name(text))


def _fake_tools(folder: Path, names: list[str], uname: str | None = None) -> Path:
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_text(f'#!/bin/sh\necho "fake {name} $*"\n')
    if uname:
        (folder / "uname").write_text(
            f'#!/bin/sh\ncase "$1" in -s) echo {uname} ;; *) echo arm64 ;; esac\n'
        )
    for tool in folder.iterdir():
        tool.chmod(0o755)
    return folder


def _run_script_outside_ci(tmp_path: Path, tools: Path) -> subprocess.CompletedProcess:
    env = {
        "PATH": f"{tools}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MYSQL_CACHE_DIR": str(tmp_path / "cache"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_outside_ci_the_mariadb_on_path_is_used(tmp_path: Path) -> None:
    tools = _fake_tools(tmp_path / "tools", ["mysqld", "mysql", "mariadbd", "mariadb", "uv"])
    run = _run_script_outside_ci(tmp_path, tools)
    assert run.returncode == 0, run.stderr
    assert f"using {tools}/mariadbd: fake mariadbd --version" in run.stdout
    assert "downloading" not in run.stdout
    assert "fake uv run pytest -m integration --fail-on-skip" in run.stdout


def test_outside_ci_without_mariadb_a_machine_that_cannot_run_the_pin_exits_2(
    tmp_path: Path,
) -> None:
    tools = _fake_tools(tmp_path / "tools", ["mysqld", "mysql", "uv"], uname="Darwin")
    run = _run_script_outside_ci(tmp_path, tools)
    assert run.returncode == 2
    assert "is a Linux x86_64 build; install mariadbd and the mariadb client" in run.stderr
    assert "fake uv" not in run.stdout


def test_the_script_passes_its_own_guards() -> None:
    assert script_problems(SCRIPT.read_text()) == []


def _mutations() -> dict[str, Callable[[str], str]]:
    return {
        "no -m integration --fail-on-skip": lambda t: t.replace(
            "uv run pytest -m integration --fail-on-skip", "uv run pytest"
        ),
        "no --fail-on-skip": lambda t: t.replace(
            "uv run pytest -m integration --fail-on-skip", "uv run pytest -m integration"
        ),
        "checksum never fails": lambda t: t.replace(
            '"$(sha256_of "$archive")" != "$MYSQL_SHA256"', "false"
        ),
        "checksum does not exit": lambda t: t.replace(
            'does not match the pinned SHA-256 for MySQL $MYSQL_VERSION" >&2\n      exit 2',
            'does not match" >&2',
        ),
        "http url": lambda t: t.replace('MYSQL_URL="https://', 'MYSQL_URL="http://'),
        "other host": lambda t: t.replace("https://cdn.mysql.com/", "https://example.org/"),
        "unpinned version": lambda t: t.replace('MYSQL_VERSION="8.0.45"', 'MYSQL_VERSION="8.0"'),
        "unpinned 8.4 version": lambda t: t.replace(
            'MYSQL_VERSION="8.4.11"', 'MYSQL_VERSION="8.4"'
        ),
        "8.4 shares the 8.0 archive checksum": lambda t: t.replace(
            "94e204cc94dede3746d2773fa5818f28f555cd8368c75ca0612eac124e6f3e58",
            "c09137539ab42590c8682d498716e6b97a45d83d2188339fa2e980dd54b6e0af",
        ),
        "8.4 has no binary checksum": lambda t: t.replace(
            "0ae85e8c4efb8457e9230e42a1b54b9b542a3b27a711f0ac4b190649bc857dc9", "0"
        ),
        "binaries not rechecked": lambda t: t.replace("if ! binaries_ok", "if false"),
        "unpinned MariaDB version": lambda t: t.replace(
            'MARIADB_VERSION="10.11.20"', 'MARIADB_VERSION="10.11"'
        ),
        "MariaDB http url": lambda t: t.replace(
            '"https://mirror.mariadb.org/', '"http://mirror.mariadb.org/'
        ),
        "MariaDB unpinned path": lambda t: t.replace(
            'MARIADB_PATH="mariadb-${MARIADB_VERSION}/', 'MARIADB_PATH="mariadb-10.11/'
        ),
        "MariaDB other host": lambda t: t.replace(
            "https://archive.mariadb.org/", "https://example.org/"
        ),
        "MariaDB archive has no checksum": lambda t: t.replace(
            "3e28539f79de3ca9df72f4a4b93f9092eb4f05a9648391ddf37c44fb4ed84727", "0"
        ),
        "MariaDB server has no checksum": lambda t: t.replace(
            "794f323ae291a7c121958f9e7c8639128dd041eed8b61b0d8c7a71b8b8870a86", "0"
        ),
        "MariaDB checksum never fails": lambda t: t.replace(
            '"$(sha256_of "$archive")" != "$MARIADB_SHA256"', "false"
        ),
        "MariaDB checksum does not exit": lambda t: t.replace(
            'does not match the pinned SHA-256 for MariaDB $MARIADB_VERSION" >&2\n      exit 2',
            'does not match" >&2',
        ),
        "MariaDB binaries not rechecked": lambda t: t.replace(
            'if ! mariadb_binaries_ok "$base"', "if false"
        ),
        "MariaDB never installed": lambda t: t.replace(
            "  install_pinned_mariadb\nfi", "  true\nfi"
        ),
        "MariaDB folder has no tree checksum": lambda t: t.replace(
            "0a80a9efd876f58652b842e66b500de179f5b63d2897fee11fb1ba743f5d63dd", "0"
        ),
        "MariaDB not rechecked after unpack": lambda t: _nth_replace(
            t, 'if ! mariadb_binaries_ok "$base"; then', "if false; then", 2
        ),
        "MariaDB bin folder on PATH": lambda t: t.replace(
            'export PATH="$links:$PATH"', 'export PATH="$base/bin:$PATH"'
        ),
        "a third link": lambda t: t.replace(
            '  ln -sf "$base/bin/mariadb" "$links/mariadb"\n',
            '  ln -sf "$base/bin/mariadb" "$links/mariadb"\n  ln -sf "$base/bin/mysqld" "$links/mysqld"\n',
        ),
        "curl insecure": lambda t: t.replace("--connect-timeout 20", "--connect-timeout 20 -k"),
        "curl redirect to http": lambda t: t.replace(
            "--proto-redir '=https'", "--proto-redir '=http,https'"
        ),
        "curl without the options": lambda t: t.replace(
            'curl "${CURL_OPTS[@]}" -o "$archive" "$MYSQL_URL"',
            'curl -fsSL -o "$archive" "$MYSQL_URL"',
        ),
        "curl never stalls out": lambda t: t.replace("--speed-limit 100000 --speed-time 60", ""),
    }


def _nth_replace(text: str, old: str, new: str, n: int) -> str:
    parts = text.split(old)
    if len(parts) <= n:
        return text
    return old.join(parts[:n]) + new + old.join(parts[n:])


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_each_script_guard_fails_when_its_property_is_removed(name: str) -> None:
    mutated = _mutations()[name](SCRIPT.read_text())
    assert mutated != SCRIPT.read_text(), "the mutation must change the script"
    assert script_problems(mutated), name


def test_an_unknown_series_exits_before_any_download() -> None:
    run = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "MYSQL_SERIES": "8.5", "CI": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 2
    assert "MYSQL_SERIES must be 8.0 or 8.4" in run.stderr


def test_a_comment_does_not_satisfy_a_guard() -> None:
    text = SCRIPT.read_text().replace(
        "uv run pytest -m integration --fail-on-skip",
        "uv run pytest # -m integration --fail-on-skip",
    )
    assert script_problems(text)


def test_the_script_lists_the_step_1_routine_script_tests() -> None:
    assert _files() == [
        "tests/integration/test_routine_scripts_mysql.py",
        "tests/integration/test_triggers_scripts_mysql.py",
        "tests/integration/test_view_scripts_mysql.py",
        # a routine's DEFINER account on the disposable source engine (#2426)
        "tests/unit/tools/database/test_disposable_engine_definer.py",
        # the collector's LOB sizes on MySQL and MariaDB, named by doc claims (#2620)
        "tests/integration/test_collect_mysql_lob_lengths.py",
    ]


def test_the_lob_size_tests_never_skip_a_mariadb_case() -> None:
    """The job runs the LOB-size tests under --fail-on-skip, where a skip is a failure, so
    a MySQL-only test generates no MariaDB case instead of skipping it, and the script
    installs the MariaDB server the other cases need (#2620)."""
    name = "tests/integration/test_collect_mysql_lob_lengths.py"
    assert name in _files()
    body = (REPO / name).read_text()
    assert "pytest.skip(" not in body[body.index("\ndef test_") :]
    assert any(c.startswith("install_pinned_mariadb") for c in _code(SCRIPT.read_text()))


def test_every_listed_file_exists_and_is_marked_integration() -> None:
    for name in _files():
        path = REPO / name
        assert path.is_file(), name
        assert "pytest.mark.integration" in path.read_text(), name


def test_the_server_fixture_removes_its_data_folder_when_the_stop_times_out() -> None:
    text = (REPO / "tests/integration/test_routine_scripts_mysql.py").read_text()
    teardown = text[text.index("    finally:") : text.index("def _mysql(")]
    assert "TimeoutExpired" in teardown and "proc.kill()" in teardown
    assert re.search(r"finally:\s+shutil\.rmtree\(work", teardown)


def test_test_sh_keeps_deselecting_these_tests_and_failing_on_skip() -> None:
    cmd = _pytest_line(TEST_SH.read_text(), "uv run pytest tests/")
    assert '-m "not integration and not e2e"' in cmd
    assert "--fail-on-skip" in cmd


@pytest.mark.parametrize("marker", ['-m "not e2e"', "", '-m "not integration"'])
def test_the_test_sh_guard_fails_on_a_weaker_marker(marker: str) -> None:
    text = TEST_SH.read_text().replace('-m "not integration and not e2e"', marker)
    cmd = _pytest_line(text, "uv run pytest tests/")
    assert '-m "not integration and not e2e"' not in cmd


def _aggregate_exit(step: dict, results: dict[str, str]) -> int:
    """Run the aggregate step's shell with each `needs.<job>.result` set from `results`."""
    env = {"PATH": os.environ["PATH"]}
    for name, value in step["env"].items():
        match = re.fullmatch(r"\$\{\{ needs\.([\w-]+)\.result \}\}", str(value))
        env[name] = results[match.group(1)] if match else str(value)
    return subprocess.run(
        ["bash", "-e", "-c", step["run"]], env=env, capture_output=True
    ).returncode


def _aggregate_problems(step: dict) -> list[str]:
    """The step must succeed only when test-shard and mysqld-tests both succeeded."""
    problems = []
    states = ("success", "failure", "cancelled", "skipped")
    for shard, mysqld in itertools.product(states, states):
        code = _aggregate_exit(step, {"test-shard": shard, "mysqld-tests": mysqld})
        want_ok = shard == "success" and mysqld == "success"
        if (code == 0) != want_ok:
            problems.append(
                f"the aggregate step exits {code} for test-shard={shard}, mysqld-tests={mysqld}"
            )
    return problems


def _job_problems(workflow: dict) -> list[str]:
    problems = []
    jobs = workflow["jobs"]
    job = jobs.get("mysqld-tests", {})
    steps = job.get("steps", [])
    runs = [str(step["run"]).strip() for step in steps if "run" in step]
    if "./ci/test-mysqld.sh" not in runs:
        problems.append("the job must run ./ci/test-mysqld.sh")
    if any("pytest" in r or ("mysqld" in r and r != "./ci/test-mysqld.sh") for r in runs):
        problems.append("the job must inline no mysqld or pytest command")
    matrix = job.get("strategy", {}).get("matrix", {}).get("mysql-series")
    if matrix != ["8.0", "8.4"]:
        problems.append("the job must run a mysql-series matrix of 8.0 and 8.4")
    if not any(
        step.get("env", {}).get("MYSQL_SERIES") == "${{ matrix.mysql-series }}"
        for step in steps
        if step.get("run") == "./ci/test-mysqld.sh"
    ):
        problems.append("the script step must pass MYSQL_SERIES from the matrix")
    if any(
        "matrix.mysql-series" not in str(step.get("with", {}).get("key", ""))
        for step in steps
        if str(step.get("uses", "")).startswith("actions/cache@")
    ):
        problems.append("the server cache key must include the series")
    if "continue-on-error" in job or any("continue-on-error" in step for step in steps):
        problems.append("the job and its steps must not set continue-on-error")
    test = jobs.get("test", {})
    needs = test.get("needs", [])
    needs = [needs] if isinstance(needs, str) else needs
    if "mysqld-tests" not in needs:
        problems.append("the required `test` job must need mysqld-tests")
    aggregate = [
        step
        for step in test.get("steps", [])
        if "needs.mysqld-tests.result" in str(step.get("env", ""))
    ]
    if len(aggregate) != 1:
        problems.append("one step of the required `test` job must read needs.mysqld-tests.result")
    else:
        problems += _aggregate_problems(aggregate[0])
    return problems


def _workflow() -> dict:
    workflow: dict = yaml.safe_load(CI_YML.read_text())
    return workflow


def test_the_github_job_is_required_blocking_and_runs_only_the_script() -> None:
    assert _job_problems(_workflow()) == []


def test_the_job_guard_fails_on_each_weakening() -> None:
    import copy

    base = _workflow()

    def drop_need(w: dict) -> None:
        w["jobs"]["test"]["needs"] = ["test-shard"]

    def drop_result(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            step.get("env", {}).pop("MYSQLD_RESULT", None)

    def job_continue(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["continue-on-error"] = True

    def step_continue(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["steps"][-1]["continue-on-error"] = True

    def one_series(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["strategy"]["matrix"]["mysql-series"] = ["8.0"]

    def series_not_passed(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["steps"][-1]["env"].pop("MYSQL_SERIES")

    def shared_cache(w: dict) -> None:
        for step in w["jobs"]["mysqld-tests"]["steps"]:
            if "key" in step.get("with", {}):
                step["with"]["key"] = "mysql-${{ runner.os }}"

    def inline(w: dict) -> None:
        w["jobs"]["mysqld-tests"]["steps"][-1]["run"] = "pytest -m integration"

    def ignore_mysqld_result(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            if "MYSQLD_RESULT" in step.get("env", {}):
                step["run"] = '[ "$RESULT" = "success" ]'

    def accept_skipped(w: dict) -> None:
        for step in w["jobs"]["test"]["steps"]:
            if "MYSQLD_RESULT" in step.get("env", {}):
                step["run"] = step["run"].replace(
                    '[ "$MYSQLD_RESULT" = "success" ]', '[ "$MYSQLD_RESULT" != "failure" ]'
                )

    for weaken in (
        drop_need,
        drop_result,
        job_continue,
        step_continue,
        one_series,
        series_not_passed,
        shared_cache,
        inline,
        ignore_mysqld_result,
        accept_skipped,
    ):
        w = copy.deepcopy(base)
        weaken(w)
        assert _job_problems(w), weaken.__name__


def test_system_libraries_rebuilds_the_compat_folder_and_installs_every_library(
    tmp_path: Path,
) -> None:
    """The compat folder is on LD_LIBRARY_PATH, so a file a restored cache planted there is
    gone after the call; the apt fallback names every library the two servers link."""
    text = SCRIPT.read_text()
    compat = tmp_path / "libcompat"
    compat.mkdir()
    (compat / "libplanted.so.1").write_text("planted\n")
    shell = "\n".join(
        [
            "set -euo pipefail",
            "log() { :; }",
            "ldd() { :; }",  # nothing missing: apt never runs
            _shell_function(text, "link_libaio"),
            re.search(r"^system_libraries\(\) \{.*?^\}\n", text, re.M | re.S).group(0),
            f"system_libraries {shlex.quote(str(tmp_path))} /bin/sh",
        ]
    )
    run = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert compat.is_dir() and not (compat / "libplanted.so.1").exists()
    installs = [c for c in _code(text) if "apt-get install" in c]
    assert installs and all(
        all(lib in c for lib in ("libnuma1", "libtinfo6", "libsystemd0", "libcrypt1"))
        for c in installs
    )
