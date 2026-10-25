"""Benign-input corpus for the safety filters (#1161).

``secret_guard`` (refuses or warns about credentials) and ``prose_style`` (rewrites or
rejects model prose) are heuristic filters. Each change to one risks a new false positive
or a miss, so this corpus pins both sides: ordinary SQL, shell and prose must pass
unchanged, and the known-bad strings must still be caught. It runs with the unit tests and
as a pre-commit hook on any change to either module or to the corpus
(``safety-filter-corpus``). Add each reported false positive or miss to
``tests/fixtures/safety_filter_corpus/corpus.json`` together with its fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.api.secret_guard import find_credential_warnings, find_submitted_secrets
from src.shared.prose_style import clean_model_prose, find_style_hits, replace_dashes

CORPUS = json.loads(
    (Path(__file__).parents[2] / "fixtures" / "safety_filter_corpus" / "corpus.json").read_text()
)
SAMPLE_PASSWORD = (
    "Hunter2x"  # pragma: allowlist secret  # nosec B105 -- sample for known-bad strings
)
SECRET = CORPUS["secret_guard"]
PROSE = CORPUS["prose_style"]


@pytest.mark.passes_on_main
def test_corpus_has_both_sides() -> None:
    for section in (SECRET, PROSE):
        assert len(section["benign"]) >= 10
        assert len(section["known_bad"]) >= 4


@pytest.mark.parametrize("text", SECRET["benign"])
@pytest.mark.passes_on_main
def test_secret_guard_lets_benign_text_through(text: str) -> None:
    payload = {"answers": {"q1": text}}
    assert find_submitted_secrets(payload) == []
    assert find_credential_warnings(payload) == []


@pytest.mark.parametrize("text", SECRET["known_bad"])
@pytest.mark.passes_on_main
def test_secret_guard_still_catches_known_bad_text(text: str) -> None:
    payload = {
        "answers": {
            "q1": text.replace("{pw}", SAMPLE_PASSWORD).replace(
                "{creds}", f"admin:{SAMPLE_PASSWORD}"
            )
        }
    }
    assert find_submitted_secrets(payload) or find_credential_warnings(payload)


@pytest.mark.parametrize("key", SECRET["known_bad_keys"])
@pytest.mark.passes_on_main
def test_secret_guard_still_catches_secret_named_keys(key: str) -> None:
    assert find_submitted_secrets({key: "x1"})


@pytest.mark.parametrize("text", PROSE["benign"])
@pytest.mark.passes_on_main
def test_prose_style_leaves_benign_text_unchanged(text: str) -> None:
    assert replace_dashes(text) == text
    assert find_style_hits(text) == []
    cleaned = clean_model_prose(text, "fallback")
    assert (cleaned.text, cleaned.accepted) == (text, True)


@pytest.mark.parametrize("text", PROSE["known_bad"])
@pytest.mark.passes_on_main
def test_prose_style_still_catches_known_bad_text(text: str) -> None:
    cleaned = clean_model_prose(text, "fallback")
    assert not (cleaned.accepted and cleaned.text == text)
