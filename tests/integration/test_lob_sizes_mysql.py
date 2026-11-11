# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's ``lob-sizes.sql`` on a real MySQL (#2123), run as ``lob-sizes.md`` says.

A throwaway ``mysqld`` on a Unix socket (no TCP, no binary log). Needs a ``mysqld`` and a
``mysql`` client on PATH, so it is marked ``integration``: the gated unit run has no MySQL
server and turns a skip into a failure (#2091). Run it with
``uv run pytest -m integration tests/integration/test_lob_sizes_mysql.py``.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 -- runs a local mysqld and mysql client with fixed argv
import tempfile
import time
from pathlib import Path

import pytest

from src.contracts.data_movement_plan import LobTable
from src.tools.data_movement.lob_sizes import measure_columns, parse_output, render_script

pytestmark = pytest.mark.integration

SHA = "d" * 64
_SOURCE = """CREATE DATABASE wordpress CHARACTER SET utf8mb4;
CREATE TABLE wordpress.wp_options (option_id bigint PRIMARY KEY, option_value longtext NOT NULL);
CREATE TABLE wordpress.wp_posts (ID bigint PRIMARY KEY, post_content longtext NOT NULL,
  post_content_filtered longtext NOT NULL, `we``ird col` mediumblob);
CREATE TABLE wordpress.empty_one (id int PRIMARY KEY, j json);
INSERT INTO wordpress.wp_options VALUES (1, REPEAT('a', 79934)), (2, ''), (3, REPEAT('é', 10));
INSERT INTO wordpress.wp_posts VALUES (1, REPEAT('x', 70000), '', NULL), (2, 'hi', '', x'00ff');
"""


@pytest.fixture(scope="module")
def mysql():
    """A throwaway mysqld on a Unix socket (no TCP, no binary log), or a skip."""
    mysqld, client = shutil.which("mysqld"), shutil.which("mysql")
    if not mysqld or not client:
        pytest.skip("no mysqld and mysql client on PATH")
    work = Path(tempfile.mkdtemp(prefix="my2123", dir="/tmp"))
    base = [
        mysqld,
        "--no-defaults",
        f"--datadir={work / 'data'}",
        f"--log-error={work / 'err.log'}",
    ]
    if subprocess.run(
        [*base, "--initialize-insecure"], capture_output=True, check=False
    ).returncode:
        shutil.rmtree(work, ignore_errors=True)
        pytest.skip("mysqld cannot initialise a data folder here")
    sock = work / "m.sock"
    proc = subprocess.Popen([*base, "--skip-networking", f"--socket={sock}", "--skip-log-bin",
                             "--mysqlx=OFF", f"--pid-file={work / 'm.pid'}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip

    def run(*args: str, sql: str | None = None, stdin: Path | None = None):
        argv = [client, "--no-defaults", "-uroot", f"--socket={sock}",
                "--default-character-set=utf8mb4", *args]  # fmt: skip
        data = stdin.read_bytes() if stdin else (sql.encode() if sql is not None else None)
        return subprocess.run(argv, input=data, capture_output=True, check=False)

    try:
        for _ in range(120):
            if sock.exists() and run(sql="SELECT 1").returncode == 0:
                break
            time.sleep(0.5)
        else:
            pytest.skip("mysqld did not start")
        done = run(sql=_SOURCE)
        assert done.returncode == 0, done.stderr.decode()
        yield run
    finally:
        proc.terminate()
        proc.wait(timeout=60)
        shutil.rmtree(work, ignore_errors=True)


def _lob(table: str, columns: list[str]) -> LobTable:
    return LobTable(table=table, mode="limited", columns=columns, max_size_kb=64,
                    size_source="unmeasured", reason="LOB size not measured")  # fmt: skip


def test_the_script_runs_read_only_and_its_output_parses(mysql, tmp_path):
    tables = [_lob("wordpress.wp_options", ["option_value"]),
              _lob("wordpress.wp_posts", ["post_content", "post_content_filtered", "we`ird col"]),
              _lob("wordpress.empty_one", ["j"])]  # fmt: skip
    columns = measure_columns(tables, "mysql", {t.table for t in tables})
    script = tmp_path / "lob-sizes.sql"
    script.write_text(render_script(columns, SHA))
    done = mysql("--batch", "--skip-column-names", "--force", stdin=script)
    assert done.returncode == 0, done.stderr.decode()
    started, rows = parse_output(
        done.stdout, name="lob-sizes.tsv", contract_sha=SHA, columns=columns
    )
    assert started
    found = {(c.table, c.column): rows[(c.n, c.k)] for c in columns}
    assert found == {
        ("wordpress.empty_one", "j"): (0, 0),
        ("wordpress.wp_options", "option_value"): (79_934, 3),
        ("wordpress.wp_posts", "post_content"): (70_000, 2),
        ("wordpress.wp_posts", "post_content_filtered"): (0, 2),
        ("wordpress.wp_posts", "we`ird col"): (2, 1),
    }
    # Nothing was written: the source is as it was.
    after = mysql("--batch", "--skip-column-names", sql="SELECT COUNT(*) FROM wordpress.wp_options")
    assert after.stdout.decode().strip() == "3"


def test_a_table_the_user_cannot_read_leaves_no_line_and_the_file_is_refused(mysql, tmp_path):
    from src.tools.data_movement.lob_sizes import LobSizesError

    tables = [_lob("wordpress.wp_options", ["option_value"]), _lob("wordpress.gone", ["body"])]
    columns = measure_columns(tables, "mysql", {t.table for t in tables})
    script = tmp_path / "lob-sizes.sql"
    script.write_text(render_script(columns, SHA))
    done = mysql("--batch", "--skip-column-names", "--force", stdin=script)
    with pytest.raises(LobSizesError, match="no line for table 1 column 1"):
        parse_output(done.stdout, name="lob-sizes.tsv", contract_sha=SHA, columns=columns)
