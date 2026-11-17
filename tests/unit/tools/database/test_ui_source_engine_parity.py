# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2327: the new-assessment form prefills the source engine with a JavaScript copy of
``recognised_source_engine`` (``src/ui/src/utils/collectorFile.js``). Both sides read
one fixture, so a change to the detector that the form does not follow fails here or
in ``collectorFile.test.js``."""

import json
from pathlib import Path

import pytest

from src.tools.database.offline_parser import recognised_source_engine

_FIXTURE = (
    Path(__file__).resolve().parents[4]
    / "src/ui/src/utils/__tests__/fixtures/sourceEngineFromFile.json"
)
_CASES = json.loads(_FIXTURE.read_text())["cases"]


@pytest.mark.parametrize("case", _CASES, ids=[json.dumps(c["metadata"]) for c in _CASES])
def test_the_server_detector_names_the_fixture_engine(case):
    found = recognised_source_engine(case["metadata"])
    assert (found[0] if found else None) == case["engine"]
