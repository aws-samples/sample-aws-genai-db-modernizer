# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The ModelFlag schema pin (#746).

``ModelFlag`` is what a model may say about a routing decision, in both LLM
modes and in every PE reviewer's answer: ``RoutingFlagEntry`` embeds its
schema in ``RealityCheckModelResponse``, the DynamoDB PE review result and the
DocumentDB and OpenSearch schema agents' answers. It is never written on its
own, so it has no ``contract_version`` field and the contract schema snapshot
(``tests/contract/schema_snapshot.json``) does not list it; only the snapshot
entries of the versioned contracts that embed it would move. This pins the
flag's own schema to ``MODEL_FLAG_SCHEMA_VERSION``: a change to its shape fails
here until the version moves (and its Version History says why) and the pin
below is updated, whichever contract embeds it.
"""

from __future__ import annotations

import re

from scripts.contract_schema_snapshot import field_summary, schema_hash
from src.contracts import model_flag
from src.contracts.model_flag import (
    MODEL_FLAG_SCHEMA_VERSION,
    ModelFlag,
    RoutingFlagEntry,
    _inline_refs,
)

# One schema hash (of ``field_summary(ModelFlag)``) per version the flag has
# had. An entry is never edited: a shape change is a new version, with a new
# entry here and a Version History entry in src/contracts/model_flag.py.
PINS = {
    "1.0": "f853e8193f4991db4fc080b3e4d59126b75869144ba1d111db7ad071b610215a",  # pragma: allowlist secret
}


def test_the_flag_schema_matches_its_pinned_version() -> None:
    actual = schema_hash(field_summary(ModelFlag))
    assert MODEL_FLAG_SCHEMA_VERSION in PINS, (
        f"MODEL_FLAG_SCHEMA_VERSION {MODEL_FLAG_SCHEMA_VERSION!r} has no pin: add "
        f"{MODEL_FLAG_SCHEMA_VERSION!r}: {actual!r} to PINS"
    )
    assert PINS[MODEL_FLAG_SCHEMA_VERSION] == actual, (
        "ModelFlag's schema changed without a version bump: bump MODEL_FLAG_SCHEMA_VERSION "
        "(MINOR for an added optional field or enum value, MAJOR otherwise), add a Version "
        f"History entry and a new PINS entry for it ({actual!r}); never edit an existing one"
    )


def test_the_pins_only_grow() -> None:
    """The current version is the newest pinned one, and no two versions share a
    hash (a shape change is a new version, never a rewritten old entry)."""
    versions = sorted(PINS, key=lambda v: tuple(int(p) for p in v.split(".")))
    assert versions[-1] == MODEL_FLAG_SCHEMA_VERSION
    assert len(set(PINS.values())) == len(PINS)


def test_the_version_history_ends_at_the_pinned_version() -> None:
    entries = re.findall(r"^- (\d+\.\d+) \(", model_flag.__doc__ or "", re.MULTILINE)
    assert entries and entries[-1] == MODEL_FLAG_SCHEMA_VERSION


def test_every_embedding_advertises_exactly_the_pinned_schema() -> None:
    """``RoutingFlagEntry`` shows the model ModelFlag's own schema, refs inlined."""
    advertised = RoutingFlagEntry.__metadata__[0].json_schema  # type: ignore[attr-defined]
    assert advertised == _inline_refs(ModelFlag.model_json_schema())
    assert "$defs" not in str(advertised)
