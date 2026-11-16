# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DMS control database's grant on a real MySQL (#2133).

A DMS user with no grant on ``awsdms_control`` cannot create the control tables there. The
server's own error text must match the ``mysql_control_database`` failure hint, and the grant
the hint names (written with ``grant_database``, so the underscore is no wildcard) must fix
it. Needs ``mysqld`` and the ``mysql`` client on PATH: the test starts a throwaway server on
a Unix socket in a temp folder (no TCP listener, no binary log) and removes it afterwards.
Run it with ``uv run pytest -m integration tests/integration/test_dms_control_grants_mysql.py``
(put a ``mysql@8.0`` or ``mysql@8.4`` ``bin`` folder first on PATH to pick the version).
"""

from __future__ import annotations

import re
import subprocess  # nosec B404 -- runs mysqld and the mysql client with fixed argv

import pytest

from src.tools.data_movement.dms_control import CONTROL_SCHEMA
from src.tools.data_movement.dms_failures import match_failure
from tests.integration.test_routine_scripts_mysql import _mysql
from tests.integration.test_routine_scripts_mysql import mysql_socket as _server

pytestmark = pytest.mark.integration
mysql_socket = _server  # the module-scoped throwaway server


def _as_dms_user(client, sock, sql: str) -> subprocess.CompletedProcess[bytes]:
    argv = [client, "--no-defaults", "-udms_user", f"--socket={sock}", "-e", sql]
    return subprocess.run(argv, capture_output=True, check=False)


def test_the_servers_own_denial_matches_the_hint_and_its_grant_fixes_it(mysql_socket):
    client, sock = mysql_socket
    setup = _mysql(client, sock, f"CREATE DATABASE IF NOT EXISTS `{CONTROL_SCHEMA}`; "
                   "CREATE DATABASE IF NOT EXISTS shop; "
                   "CREATE USER 'dms_user'@'%'; "
                   "GRANT ALL PRIVILEGES ON `shop`.* TO 'dms_user'@'%';")  # fmt: skip
    assert setup.returncode == 0, setup.stderr
    made = f"CREATE TABLE {CONTROL_SCHEMA}.awsdms_apply_exceptions (TASK_NAME varchar(128))"
    refused = _as_dms_user(client, sock, made)
    text = refused.stderr.decode()
    assert refused.returncode != 0 and "for table 'awsdms_apply_exceptions'" in text, text
    used = _as_dms_user(client, sock, f"USE {CONTROL_SCHEMA}").stderr.decode()
    assert f"to database '{CONTROL_SCHEMA}'" in used, used
    for message in (text, used):
        hint = match_failure(message)
        assert hint is not None and hint.id == "mysql_control_database", message
    grant = re.search(r"GRANT ALL PRIVILEGES ON \S+\.\* TO ", hint.hint)
    assert grant and "`awsdms\\_control`" in grant.group(0)
    fixed = _mysql(client, sock, grant.group(0) + "'dms_user'@'%';")
    assert fixed.returncode == 0, fixed.stderr
    retried = _as_dms_user(client, sock, made)
    assert retried.returncode == 0, retried.stderr
    # The escaped grant covers awsdms_control only, never a look-alike such as awsdmsXcontrol.
    _mysql(client, sock, "CREATE DATABASE awsdmsXcontrol;")
    other = _as_dms_user(client, sock, "CREATE TABLE awsdmsXcontrol.t (x int)")
    assert other.returncode != 0
