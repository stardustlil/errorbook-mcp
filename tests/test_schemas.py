from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from errorbook_mcp.schemas import (
    Choice,
    ProblemDraft,
    ProblemPatch,
    ReviewSheetRequest,
    SearchFilters,
)


def test_choice_shape_is_strict() -> None:
    with pytest.raises(PydanticValidationError, match="at least two choices"):
        ProblemDraft(
            kind="single_choice",
            subject="数学",
            stem_markdown="题干",
            choices=[Choice(label="A", content_markdown="一项")],
        )

    with pytest.raises(PydanticValidationError, match="unique"):
        ProblemDraft(
            kind="single_choice",
            subject="数学",
            stem_markdown="题干",
            choices=[
                Choice(label="A", content_markdown="一项"),
                Choice(label="a", content_markdown="二项"),
            ],
        )

    with pytest.raises(PydanticValidationError, match="Extra inputs"):
        Choice.model_validate({"label": "A", "content_markdown": "一项", "unknown": True})

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

    ProblemDraft(
        kind="solution",
        subject="数学",
        stem_markdown="```text\n$not_math\n```\n正文 $x$",
    )


def test_filter_items_and_review_sheet_modes_are_bounded() -> None:
    with pytest.raises(PydanticValidationError, match="at most 64"):
        SearchFilters(numbers=["E" * 65])
    with pytest.raises(PydanticValidationError, match="at most 50"):
        SearchFilters(tags=["t" * 51])
    with pytest.raises(PydanticValidationError, match="requires at least one"):
        ReviewSheetRequest(mode="numbers")


@pytest.mark.parametrize(
    "field", ["kind", "subject", "stem_markdown", "choices", "tags", "importance"]
)
def test_patch_required_content_rejects_explicit_null(field: str) -> None:
    with pytest.raises(PydanticValidationError):
        ProblemPatch.model_validate({field: None})

    schema = ProblemPatch.model_json_schema()["properties"][field]
    assert not any(option.get("type") == "null" for option in schema.get("anyOf", []))
