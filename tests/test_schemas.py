from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from errorbook_mcp.schemas import Choice, ProblemDraft, ProblemPatch, SearchFilters


def test_choice_shape_is_strict() -> None:
    with pytest.raises(PydanticValidationError, match="at least two choices"):
        ProblemDraft(
            kind="single_choice",
            subject="数学",
            stem_markdown="题干",
            choices=[Choice(label="A", content_markdown="一项")],
        )

    with pytest.raises(PydanticValidationError, match="cannot contain choices"):
        ProblemDraft(
            kind="solution",
            subject="数学",
            stem_markdown="题干",
            choices=[
                Choice(label="A", content_markdown="一项"),
                Choice(label="B", content_markdown="二项"),
            ],
        )


def test_math_delimiters_and_datetime_are_validated() -> None:
    with pytest.raises(PydanticValidationError, match="unbalanced"):
        ProblemDraft(kind="solution", subject="数学", stem_markdown="broken $x")

    with pytest.raises(PydanticValidationError, match="timezone"):
        SearchFilters(due_before=datetime(2026, 1, 1))

    ProblemDraft(
        kind="solution",
        subject="数学",
        stem_markdown=r"价格写作 \$5，代码 `$not_math` 不应被当作公式。",
    )


@pytest.mark.parametrize(
    "field", ["kind", "subject", "stem_markdown", "choices", "tags", "importance"]
)
def test_patch_required_content_rejects_explicit_null(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        ProblemPatch.model_validate({field: None})

    schema = ProblemPatch.model_json_schema()["properties"][field]
    assert not any(option.get("type") == "null" for option in schema.get("anyOf", []))
