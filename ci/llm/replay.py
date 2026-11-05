# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Zero-token replay of a saved real-model run (#1409).

A real-model run (``ci/e2e-llm.sh``) costs about $4 and ten minutes, so it cannot
run on every push; but most of what it checks is deterministic once the model has
answered. This keeps one passing run's model-written artifacts as a fixture
(``tests/fixtures/real_runs/<name>/``) and replays the checks over them with the
current code and no model call:

- ``ci/llm/run.py check-transcript`` over the saved transcript, against the current
  allowlist (``.claude/settings.ci.json``);
- the deliverables re-rendered from the saved ``report.json`` by the current
  renderers (``scripts/run_report.py``), then the external-mode e2e checks
  (``tests/e2e``, the job parametrized as ``external``), which include the
  customer-text guard and ``ci/llm/judge_facts.py``'s agreement with the report's
  own figures.

Synthesis and the facts layer are not re-run: the saved ``report.json`` (its
``facts`` included) is what that run wrote, so a change there is covered by the
deterministic shards and e2e-llm, not here, until the fixture is refreshed.

``ci/replay.sh`` runs both; ``ci/pre_merge.sh`` and the ``replay`` e2e shard call
it. The unit tests read the same fixture
(``tests/unit/ci/test_replay.py``,
``tests/unit/tools/validation/test_customer_text_guard_id_fields.py``).

The e2e-llm upload carries the whole job folder as ``job.tar.gz`` (#1463, secrets
redacted and rendered files left out; ``ci/llm/run.py collect-job``), so the fixture
holds the assignment versions, collector output, triage, reality-check and schema
outputs too (``job/``) and every external-mode check is replayed. ``save`` refuses a
run whose archive was redacted (#2053) unless given ``--allow-redacted``. A job file
the run did not record and that was rebuilt for the fixture is named with ``--rebuilt
FILE=NOTE`` (#1900): ``run.json`` keeps ``rebuilt_files`` (job file to how and why) and
``prepare`` prints a note for each one.

A fixture file over ``MAX_FIXTURE_FILE_BYTES`` (900 KB; a JSON file is written compact
first) is stored gzip-compressed as ``<name>.gz`` (#1847): the repo refuses a file over
1000 KB (``check-added-large-files``), and a discourse run's transcript, report and
largest job files are each several MB. The compressed bytes are deterministic (level 9,
mtime 0, no file name), so saving the same run again writes the same bytes; a file still
over ``MAX_COMPRESSED_FILE_BYTES`` compressed is refused. ``prepare`` unpacks every
``.gz`` file before the replay runs, and ``ci/llm/private_scan.py`` scans the compressed
files' text, which detect-secrets' own hook skips as binary.

There is one fixture per sample (``FIXTURES``): ``ci/replay.sh`` replays each one that
has been saved and skips the others with a line saying so.

Usage::

    uv run python ci/llm/replay.py save --from test-results/llm-both-wordpress \\
        --to tests/fixtures/real_runs/wordpress-both [--allow-redacted]
    uv run python ci/llm/replay.py prepare --fixture tests/fixtures/real_runs/wordpress-both \\
        --root test-results/replay/artifacts [--transcript-out test-results/replay/transcript.jsonl]
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ci.llm.private_scan import (  # noqa: E402
    COMPRESSED_SUFFIX,
    REDACTION_RE,
    identity_findings,
    line_of,
    read_text,
    secret_findings,
)

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "real_runs"
DEFAULT_FIXTURE = FIXTURES_DIR / "wordpress-both"
# Every sample's fixture, in the order ci/replay.sh replays them; one not saved yet
# (no run.json) is skipped there.
FIXTURES = (DEFAULT_FIXTURE, FIXTURES_DIR / "discourse-both")

# The synthesis files a saved run keeps: the report every deliverable renders, and
# the model's input and answer (the judge's facts read the input's table scope).
SYNTHESIS_FILES = ("report.json", "llm_input.json", "llm_response.json")
RUN_FILE = "run.json"
TRANSCRIPT_FILE = "transcript.jsonl"

# The saved transcript's working directory, in place of the runner's own path, and
# the placeholders for the session's home and temp folders: a saved run names no
# machine, user or runner.
PLACEHOLDER_CWD = "/workspace/repo"
PLACEHOLDER_HOME = "/home/user"
PLACEHOLDER_TMP = "/tmp/claude"  # nosec B108 -- a placeholder written into text, never opened
ALLOWED_PATH_PREFIXES = (PLACEHOLDER_CWD, PLACEHOLDER_HOME, PLACEHOLDER_TMP)

# Rewrites applied in order, after the working directory: a home folder (macOS,
# Linux, root), the macOS per-user temp folder, the session's temp folder and
# socket, and any other temp path (under the temp placeholder). A path segment
# stops at a quote, whitespace or a JSON escape. Every /tmp here is text to match
# or write, never a file this module opens (hence the B108 marks).
_SEG = r"[^/\s\"'\\]+"
_PATH_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(rf"(?:/private)?/var/folders/{_SEG}/{_SEG}/T(?=/|\b)"),
        "/tmp",  # nosec B108 -- path pattern, never opened
    ),
    (
        re.compile(r"/private/tmp(?=/|\b)"),
        "/tmp",  # nosec B108 -- path pattern, never opened
    ),
    (
        re.compile(rf"/tmp/claude-{_SEG}"),  # nosec B108 -- path pattern, never opened
        PLACEHOLDER_TMP,
    ),
    (
        re.compile(rf"/tmp/cc-socks/{_SEG}"),  # nosec B108 -- path pattern, never opened
        f"{PLACEHOLDER_TMP}/agent.sock",
    ),
    (re.compile(r"(?<![\w.-])/tmp/(?!claude(?=/|\b))"), f"{PLACEHOLDER_TMP}/"),
    (re.compile(rf"/(?:Users|home)/(?!user(?=/|\b)){_SEG}"), PLACEHOLDER_HOME),
    (re.compile(r"(?<![\w.-])/root(?=/|\b)"), PLACEHOLDER_HOME),
)
# An absolute path under a machine's own folders, for private_data(): any one that
# does not start with an allowed placeholder names the machine it ran on.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.:/~-])/(?:Users|home|root|tmp|var|private|builds|opt|mnt|srv|Volumes|runner|github|workspace)"
    r"(?=/|\b)[^\s\"'\\]*"
)
_PRIVATE_VALUE_RES = (
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws arn", re.compile(r"\barn:aws[\w-]*:")),
)

# A tool result longer than this keeps its first and last TRIM_KEEP characters:
# the checks read short results (a script's JSON status line, a hook's refusal),
# never a file's contents.
TRIM_OVER = 1000
TRIM_KEEP = 400
# Tool-call inputs the transcript checks read whole (a Bash command, an Agent prompt).
KEEP_INPUT_KEYS = frozenset({"command", "prompt", "description", "file_path", "skill"})

# The job folder the fixture keeps next to the synthesis files (everything the
# e2e-llm upload's job.tar.gz holds except synthesis/, which is the top-level files).
JOB_DIR = "job"
# The repo refuses files over 1000 KB (check-added-large-files, --maxkb=1000, that is
# 1,024,000 bytes): a larger file is written compact first, then, if still over this,
# gzip-compressed as <name>.gz; one over MAX_COMPRESSED_FILE_BYTES compressed is refused.
MAX_FIXTURE_FILE_BYTES = 900_000
MAX_COMPRESSED_FILE_BYTES = 1_000_000
# A member of job.tar.gz is read into memory: the upload never writes one over
# ci/llm/run.py's JOB_MAX_FILE_BYTES (30 MB), so a larger one (a crafted archive) is
# refused before it is read.
MAX_ARCHIVE_MEMBER_BYTES = 30_000_000


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > TRIM_OVER:
        head, tail = TRIM_KEEP, len(value) - TRIM_KEEP
        # Never cut through a number: twelve digits left of a 19-digit pattern id read
        # as an AWS account id to private_data (#1847, seen on a discourse run).
        while head > 0 and value[head - 1].isdigit() and value[head].isdigit():
            head -= 1
        while tail < len(value) and value[tail - 1].isdigit() and value[tail].isdigit():
            tail += 1
        cut = tail - head
        return f"{value[:head]}\n[... {cut} characters trimmed ...]\n{value[tail:]}"
    if isinstance(value, list):
        return [_trim(v) for v in value]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    return value


def _session_cwds(lines: list[str]) -> set[str]:
    cwds = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "system" and rec.get("subtype") == "init":
            cwd = rec.get("cwd")
            if isinstance(cwd, str) and cwd not in ("", "/"):
                cwds.add(cwd)
    return cwds


def scrub_paths(text: str) -> str:
    """``text`` with every home, temp and socket path replaced by its placeholder."""
    for pattern, placeholder in _PATH_REWRITES:
        text = pattern.sub(placeholder, text)
    return text


def private_data(text: str) -> list[str]:
    """What in ``text`` would name a machine, a user or an account: an absolute path
    outside the placeholders, an AWS access key, account id or ARN, and what
    ``identity_findings`` finds. Each finding is ``"<kind> at line N"``, never the value."""
    found = [
        f"absolute path at line {line_of(text, m)}"
        for m in _ABSOLUTE_PATH_RE.finditer(text)
        if not m.group(0).startswith(ALLOWED_PATH_PREFIXES)
    ]
    found += [
        f"{kind} at line {line_of(text, m)}"
        for kind, rx in _PRIVATE_VALUE_RES
        for m in rx.finditer(text)
    ]
    return found + identity_findings(text)


def job_private_data(text: str) -> list[str]:
    """``private_data`` and every credential detect-secrets finds (``ci/llm/private_scan.py``,
    the scan the upload runs): for the saved job's files and the synthesis files."""
    return private_data(text) + secret_findings(text)


def trim_transcript(text: str) -> str:
    """The transcript with the runner's working directory replaced by
    ``PLACEHOLDER_CWD``, every home, temp and socket path by its placeholder, every long tool result cut to its ends and every thinking
    block emptied. The records, their order and every tool call stay as they were,
    so ``check-transcript`` reads the same run."""
    lines = text.splitlines()
    cwds = sorted(_session_cwds(lines), key=len, reverse=True)
    out = []
    for line in lines:
        for cwd in cwds:
            # The path itself, and Claude Code's project folder name made from it.
            line = line.replace(cwd, PLACEHOLDER_CWD)
            line = line.replace(cwd.replace("/", "-"), PLACEHOLDER_CWD.replace("/", "-"))
        line = scrub_paths(line)
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        message = rec.get("message") if isinstance(rec, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    block["content"] = _trim(block.get("content"))
                elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    # A file the model wrote is cut; a command and a dispatch prompt,
                    # which the checks read whole, are kept.
                    block["input"] = {
                        k: v if k in KEEP_INPUT_KEYS else _trim(v)
                        for k, v in block["input"].items()
                    }
                elif block.get("type") == "thinking":
                    block["thinking"] = ""
                    block.pop("signature", None)
        if isinstance(rec, dict) and "tool_use_result" in rec:
            rec["tool_use_result"] = _trim(rec["tool_use_result"])
        out.append(json.dumps(rec, separators=(",", ":")))
    return "\n".join(out) + "\n"


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


JOB_ARCHIVE = "job.tar.gz"


def _read_job_archive(archive: Path, db: str, job: str) -> tuple[dict[str, str], int]:
    """The text files of ``archive`` (the e2e-llm upload's ``job.tar.gz``, members
    ``<db>/<job>/...``) except the synthesis folder, by relative path, and the
    version of the synthesis folder the deliverables came from. Only regular files
    under that prefix are read; a path that climbs out of it is refused."""
    if not archive.is_file():
        raise ValueError(
            f"{archive}: no job.tar.gz; the run predates the full-job upload (#1463), "
            "keep a newer passing run"
        )
    prefix = f"{db}/{job}/"
    files: dict[str, str] = {}
    versions: set[int] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            if not member.isreg() or not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix) :]
            if not rel or ".." in Path(rel).parts or Path(rel).is_absolute():
                raise ValueError(f"{archive}: unsafe member {member.name}")
            if rel.startswith("synthesis/"):
                m = re.match(r"synthesis/v(\d+)/", rel)
                if m:
                    versions.add(int(m.group(1)))
                continue
            if member.size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(
                    f"{archive}: {member.name} is over {MAX_ARCHIVE_MEMBER_BYTES} bytes"
                )
            extracted = tar.extractfile(member)
            if extracted is None:
                raise ValueError(f"{archive}: {member.name} has no data")
            # Never more than the cap, whatever the header says.
            raw = extracted.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
            if len(raw) > MAX_ARCHIVE_MEMBER_BYTES:
                raise ValueError(
                    f"{archive}: {member.name} is over {MAX_ARCHIVE_MEMBER_BYTES} bytes"
                )
            text = raw.decode("utf-8")
            files[rel] = text if text.endswith("\n") else text + "\n"
    if not files:
        raise ValueError(f"{archive}: holds no files of {prefix}")
    return files, max(versions, default=1)


def stored_form(name: str, text: str) -> tuple[str, bytes]:
    """The name and bytes fixture file ``name`` is written as: ``text`` as it is up to
    ``MAX_FIXTURE_FILE_BYTES``; over it, a JSON file compact; and if still over,
    gzip-compressed as ``<name>.gz`` with fixed header fields (level 9, mtime 0, no file
    name), so the same text always gives the same bytes. Refused (``ValueError``) when
    even the compressed bytes are over ``MAX_COMPRESSED_FILE_BYTES``."""
    data = text.encode()
    if len(data) <= MAX_FIXTURE_FILE_BYTES:
        return name, data
    try:
        data = (json.dumps(json.loads(text), separators=(",", ":")) + "\n").encode()
    except ValueError:
        pass
    if len(data) <= MAX_FIXTURE_FILE_BYTES:
        return name, data
    packed = gzip.compress(data, compresslevel=9, mtime=0)
    if len(packed) > MAX_COMPRESSED_FILE_BYTES:
        raise ValueError(
            f"{name}: {len(packed)} bytes even gzip-compressed, over the "
            f"{MAX_COMPRESSED_FILE_BYTES}-byte limit for a fixture file"
        )
    return name + COMPRESSED_SUFFIX, packed


def fixture_file(fixture: Path, name: str) -> Path:
    """The committed file of ``name`` in ``fixture``: ``name`` or ``name.gz``. Both at
    once is refused, since nothing says which one is current."""
    plain, packed = fixture / name, fixture / (name + COMPRESSED_SUFFIX)
    if plain.is_file() and packed.is_file():
        raise ValueError(f"{fixture}: both {name} and {packed.name}; keep one")
    return packed if packed.is_file() else plain


def fixture_text(fixture: Path, name: str) -> str:
    """The text of ``name`` in ``fixture``, decompressed when it is stored as ``.gz``."""
    return read_text(fixture_file(fixture, name))


def saved_fixtures() -> list[Path]:
    """The fixtures of ``FIXTURES`` that have been saved (they have a ``run.json``)."""
    return [f for f in FIXTURES if (f / RUN_FILE).is_file()]


#: ``ci/llm/run.py collect-job``'s list of the findings it redacted (#2053).
JOB_FINDINGS = "job.findings.json"


def _redacted_count(source: Path, results: dict[str, Any], job_files: dict[str, str]) -> int:
    """How many findings ``collect-job`` redacted in the run's archive: its manifest,
    the mark in ``results.json``, or the redaction markers left in the files."""
    markers = sum(len(REDACTION_RE.findall(text)) for text in job_files.values())
    scan = results.get("private_scan")
    marked = int(scan.get("findings") or 0) if isinstance(scan, dict) else 0
    listed = 0
    if (source / JOB_FINDINGS).is_file():
        listed = max(1, len(_load(source / JOB_FINDINGS).get("findings") or []))
    return max(markers, marked, listed)


def save(
    source: Path,
    dest: Path,
    allow_redacted: bool = False,
    rebuilt: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Keep the passing run in ``source`` (an e2e-llm output folder,
    ``test-results/llm-<mode>-<fixture>``) as the replay fixture ``dest``.

    A run the private scan found values in (``collect-job`` redacted them and failed
    the run) is refused unless ``allow_redacted`` (``--allow-redacted``), so redacted data
    never becomes a fixture unnoticed. With it, such a run counts as passing when it
    passed before the scan (``private_scan.pass_before_scan``), and ``run.json`` records
    ``redacted_findings``.

    ``rebuilt`` names job files (paths inside ``job/``) the run did not record, each with
    a note on how and why it was rebuilt; each must be in the archive and the note must
    not be empty. ``run.json`` keeps them as ``rebuilt_files``."""
    results = _load(source / "results.json")
    scan = results.get("private_scan")
    scan_failed_it = isinstance(scan, dict) and scan.get("pass_before_scan") is True
    if results.get("pass") is not True and not (allow_redacted and scan_failed_it):
        if scan_failed_it:
            raise ValueError(
                f"{source}: the private scan failed the run ({JOB_FINDINGS}); review the "
                "findings, then pass --allow-redacted to keep it anyway"
            )
        raise ValueError(f"{source}: results.json does not pass; keep a passing run only")
    db, job = results.get("db"), results.get("job_id")
    if not db or not job:
        raise ValueError(f"{source}: results.json names no db and job_id")
    synthesis = source / "deliverables" / str(db) / "synthesis"
    if not (synthesis / "report.json").is_file():
        raise ValueError(f"{synthesis}: no report.json")
    files = {
        name: (synthesis / name).read_text()
        for name in SYNTHESIS_FILES
        if (synthesis / name).is_file()
    }
    files[TRANSCRIPT_FILE] = trim_transcript((source / TRANSCRIPT_FILE).read_text())
    job_files, synthesis_version = _read_job_archive(source / JOB_ARCHIVE, str(db), str(job))
    for name, note in (rebuilt or {}).items():
        if name not in job_files:
            raise ValueError(f"{source}: --rebuilt {name}: no such file in {JOB_ARCHIVE}")
        if not note.strip():
            raise ValueError(f"{source}: --rebuilt {name}: say how and why it was rebuilt")
    redacted = _redacted_count(source, results, job_files)
    if redacted and not allow_redacted:
        raise ValueError(
            f"{source}: the private scan redacted {redacted} finding(s) in the job archive "
            f"({JOB_FINDINGS}); review them, then pass --allow-redacted to keep it anyway"
        )
    # The transcript is prose and tool output: key-name findings (a word such as
    # "password" in a sentence) would flag it, so it gets the credential-shape scan only.
    found = [
        f"{name}: {hit}"
        for name, text in files.items()
        for hit in (
            private_data(text) + secret_findings(text, key_names=False)
            if name == TRANSCRIPT_FILE
            else job_private_data(text)
        )
    ]
    found += [
        f"{JOB_DIR}/{name}: {hit}"
        for name, text in job_files.items()
        for hit in job_private_data(text)
    ]
    if found:
        # The fixture is public: nothing is written until every file is clean.
        raise ValueError(f"{source}: the run names a machine or an account: {found[:10]}")
    # Every file's stored form first, so a file too large even compressed writes nothing.
    top = [stored_form(n, t if t.endswith("\n") else t + "\n") for n, t in files.items()]
    job_stored = [stored_form(f"{JOB_DIR}/{n}", t) for n, t in job_files.items()]
    dest.mkdir(parents=True, exist_ok=True)
    for name in (*SYNTHESIS_FILES, TRANSCRIPT_FILE):
        (dest / name).unlink(missing_ok=True)
        (dest / (name + COMPRESSED_SUFFIX)).unlink(missing_ok=True)
    shutil.rmtree(dest / JOB_DIR, ignore_errors=True)
    for name, data in (*top, *job_stored):
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    judge = source / "judge.json"
    judge_mean = _load(judge).get("mean") if judge.is_file() else None
    run = {
        "db": db,
        "job_id": job,
        "mode": results.get("mode"),
        "fixture": results.get("fixture"),
        "git_sha": results.get("git_sha"),
        "timestamp": results.get("timestamp"),
        "cost_usd": results.get("cost_usd"),
        "judge_mean": judge_mean,
        "job_source": "e2e-llm",
        "synthesis_version": synthesis_version,
        "checks": results.get("checks"),
    }
    if redacted:
        run["redacted_findings"] = redacted
    if rebuilt:
        run["rebuilt_files"] = dict(sorted(rebuilt.items()))
    (dest / RUN_FILE).write_text(json.dumps(run, indent=2) + "\n")
    return run


def _unpack_tree(src: Path, dest: Path) -> None:
    """Copy ``src`` to ``dest`` with every ``<name>.gz`` file written as ``<name>``,
    decompressed. A file present both ways is refused."""
    for path in sorted(src.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(src)
        if path.name.endswith(COMPRESSED_SUFFIX):
            rel = rel.with_name(path.name[: -len(COMPRESSED_SUFFIX)])
            if (src / rel).is_file():
                raise ValueError(f"{src}: both {rel} and {rel}{COMPRESSED_SUFFIX}; keep one")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.name.endswith(COMPRESSED_SUFFIX):
            target.write_text(read_text(path), encoding="utf-8")
        else:
            shutil.copy2(path, target)


def prepare(fixture: Path, root: Path, transcript_out: Path | None = None) -> dict[str, Any]:
    """Lay the saved run out as a job under ``root`` (its saved job folder and its
    synthesis folder at the version it had, every compressed file unpacked) and render
    its deliverables with the current renderers. With ``transcript_out``, the saved
    transcript is written there unpacked, for ``check-transcript``."""
    from scripts.run_report import run as render_job

    run = _load(fixture / RUN_FILE)
    db, job = str(run["db"]), str(run["job_id"])
    job_dir = root / db / job
    if job_dir.exists():
        shutil.rmtree(job_dir)
    saved_job = fixture / JOB_DIR
    if not saved_job.is_dir():
        raise RuntimeError(
            f"{fixture}: no {JOB_DIR}/ folder; refresh the fixture from a run that uploaded "
            "the whole job (ci/llm/replay.py save, #1463)"
        )
    _unpack_tree(saved_job, job_dir)
    synthesis = job_dir / "synthesis" / f"v{int(run.get('synthesis_version') or 1)}"
    synthesis.mkdir(parents=True)
    for name in SYNTHESIS_FILES:
        if fixture_file(fixture, name).is_file():
            (synthesis / name).write_text(fixture_text(fixture, name), encoding="utf-8")
    if transcript_out is not None:
        transcript_out.parent.mkdir(parents=True, exist_ok=True)
        transcript_out.write_text(fixture_text(fixture, TRANSCRIPT_FILE), encoding="utf-8")
    rendered = render_job(job, db, str(root), None)
    if rendered.get("status") != "complete":
        raise RuntimeError(f"rendering the saved report failed: {rendered}")
    return {
        "artifact_root": str(root),
        "db": db,
        "job_id": job,
        "mode": run.get("mode"),
        "rebuilt_files": dict(run.get("rebuilt_files") or {}),
    }


def _rebuilt_arg(value: str) -> tuple[str, str]:
    name, sep, note = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError(f"--rebuilt {value!r}: use FILE=NOTE")
    return name, note


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_save = sub.add_parser("save", help="Keep a passing e2e-llm run as the replay fixture.")
    p_save.add_argument("--from", dest="source", type=Path, required=True)
    p_save.add_argument("--to", dest="dest", type=Path, default=DEFAULT_FIXTURE)
    p_save.add_argument(
        "--allow-redacted",
        action="store_true",
        help="Keep a run whose job archive the private scan redacted (#2053).",
    )
    p_save.add_argument(
        "--rebuilt",
        type=_rebuilt_arg,
        action="append",
        default=[],
        metavar="FILE=NOTE",
        help="A job file the run did not record, rebuilt for the fixture: how and why.",
    )
    p_prep = sub.add_parser("prepare", help="Lay the fixture out as a job and render it.")
    p_prep.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p_prep.add_argument("--root", type=Path, required=True)
    p_prep.add_argument("--transcript-out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "save":
        print(json.dumps(save(args.source, args.dest, args.allow_redacted, dict(args.rebuilt))))
    elif args.command == "prepare":
        info = prepare(args.fixture, args.root, args.transcript_out)
        for name, note in info["rebuilt_files"].items():
            print(f"replay: {JOB_DIR}/{name} was rebuilt, not recorded: {note}", file=sys.stderr)
        print(f"{info['db']} {info['job_id']} {info['mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
