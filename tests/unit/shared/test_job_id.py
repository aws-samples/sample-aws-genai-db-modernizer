"""src.shared.job_id: the job_id counterpart to validate_database_name
(#420), added in #508. Mirrors tests/unit/shared/test_database_name.py."""

from __future__ import annotations

import pytest

from src.shared.job_id import JOB_ID_ERROR, validate_job_id

# Fixed examples, not freshly generated: a parametrize list is built at
# collection time, and pytest-xdist collects each test module once per
# worker process -- a value from uuid.uuid4() here would differ between
# workers and fail with "Different tests were collected between gw0 and
# gwN". These are frozen samples of the two shapes the pipeline generates
# (scripts/run_assessment.py's str(uuid.uuid4())[:8], and the full
# str(uuid.uuid4()) src/api/routes/assessments.py uses), not generated here.
_SHORT_HEX_JOB_ID = "7c6e6af2"
_FULL_UUID4_JOB_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize(
    "job_id",
    [
        "job1",
        "Demo_Job-42",
        "a",
        "a" * 128,
        "  job1  ",
        "abc\n",
        _SHORT_HEX_JOB_ID,
        _FULL_UUID4_JOB_ID,
    ],
)
def test_accepts_valid_identifiers(job_id: str) -> None:
    assert validate_job_id(job_id) == job_id.strip()


def test_strips_trailing_newline_and_returns_the_stripped_value() -> None:
    result = validate_job_id("abc\n")
    assert result == "abc"
    assert "\n" not in result


@pytest.mark.parametrize(
    "job_id",
    [
        "",
        "../demo",
        "..",
        ".",
        "a/b",
        "a\\b",
        "a.b",
        "café",
        "job id",
        "a,b",
        "a;bad",
        "a" * 129,
        "../../etc/passwd",
        "/etc/passwd",
    ],
)
def test_rejects_invalid_identifiers(job_id: str) -> None:
    with pytest.raises(ValueError, match="job_id"):
        validate_job_id(job_id)


@pytest.mark.parametrize("job_id", [None, True, 123, [], {}])
def test_rejects_non_string_values(job_id: object) -> None:
    with pytest.raises(ValueError, match="job_id"):
        validate_job_id(job_id)


def test_error_message_names_the_accepted_shape() -> None:
    assert "1 to 128" in JOB_ID_ERROR
    assert "letters, digits, underscores or hyphens" in JOB_ID_ERROR


def test_error_message_never_echoes_the_rejected_value() -> None:
    try:
        validate_job_id("../attacker-controlled-value")
    except ValueError as exc:
        assert "attacker-controlled-value" not in str(exc)
    else:
        pytest.fail("expected a ValueError")
