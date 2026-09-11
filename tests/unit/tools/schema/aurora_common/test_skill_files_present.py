from pathlib import Path

# Resolve repo root from this test file's location, then src/skills.
# tests/unit/tools/schema/aurora_common/test_skill_files_present.py -> repo root is parents[5]
SKILLS = Path(__file__).resolve().parents[5] / "src" / "skills"


def test_designer_skill_present_and_bounded():
    text = (SKILLS / "aurora_postgresql-data-modeling.md").read_text(encoding="utf-8")
    assert "Aurora PostgreSQL" in text
    assert "draft" in text.lower()  # draft must be declared authoritative
    assert "needs_judgment" in text  # residual resolution instruction
    assert "migration_strategy" in text


def test_pe_review_skill_present():
    text = (SKILLS / "aurora_postgresql-pe-review.md").read_text(encoding="utf-8")
    assert "Principal Engineer" in text
    assert "verdict" in text.lower()
