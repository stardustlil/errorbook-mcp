from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ProblemKind = Literal["single_choice", "multiple_choice", "short_answer", "solution"]
ProblemStatus = Literal["active", "mastered", "archived"]
ReviewOutcome = Literal["incorrect", "partial", "correct", "easy", "skipped"]
PriorityDelta = Literal[-5, -4, -3, -2, -1, 1, 2, 3, 4, 5]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Choice(StrictModel):
    label: Annotated[str, StringConstraints(min_length=1, max_length=12)]
    content_markdown: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


class ProblemDraft(StrictModel):
    kind: ProblemKind
    subject: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    stem_markdown: Annotated[str, StringConstraints(min_length=1, max_length=100_000)]
    choices: list[Choice] = Field(default_factory=list, max_length=20)
    source: Annotated[str, StringConstraints(max_length=1_000)] | None = None
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=50)]] = Field(
        default_factory=list, max_length=30
    )
    importance: float = Field(default=0.5, ge=0, le=1)
    initial_outcome: Literal["incorrect", "unreviewed"] = "incorrect"

    @model_validator(mode="after")
    def validate_shape(self) -> ProblemDraft:
        labels = [choice.label.casefold() for choice in self.choices]
        if len(labels) != len(set(labels)):
            raise ValueError("choice labels must be unique")
        if self.kind in {"single_choice", "multiple_choice"} and len(self.choices) < 2:
            raise ValueError("choice problems require at least two choices")
        if self.kind in {"short_answer", "solution"} and self.choices:
            raise ValueError(f"{self.kind} problems cannot contain choices")
        _validate_math_delimiters(self.stem_markdown)
        for choice in self.choices:
            _validate_math_delimiters(choice.content_markdown)
        return self


class ProblemPatch(StrictModel):
    # A None default makes each field optional in a patch; the non-null annotation
    # ensures that explicitly sending null is still rejected for required content.
    kind: ProblemKind = Field(default=None)
    subject: Annotated[str, StringConstraints(min_length=1, max_length=100)] = Field(default=None)
    stem_markdown: Annotated[str, StringConstraints(min_length=1, max_length=100_000)] = Field(
        default=None
    )
    choices: list[Choice] = Field(default=None, max_length=20)
    source: Annotated[str, StringConstraints(max_length=1_000)] | None = None
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=50)]] = Field(
        default=None, max_length=30
    )
    importance: float = Field(default=None, ge=0, le=1)


class SearchFilters(StrictModel):
    text: Annotated[str, StringConstraints(max_length=500)] | None = None
    numbers: list[str] = Field(default_factory=list, max_length=100)
    subjects: list[str] = Field(default_factory=list, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=30)
    kinds: list[ProblemKind] = Field(default_factory=list, max_length=4)
    statuses: list[ProblemStatus] = Field(default_factory=lambda: ["active"])
    due_before: datetime | None = None
    sort: Literal["priority", "due", "created", "number"] = "priority"
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)

    @field_validator("due_before")
    @classmethod
    def require_due_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("due_before must include a timezone")
        return value


class ReviewSheetRequest(StrictModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)] = "错题复习卷"
    mode: Literal["scheduled", "all_active", "numbers"] = "scheduled"
    numbers: list[str] = Field(default_factory=list, max_length=200)
    subjects: list[str] = Field(default_factory=list, max_length=30)
    tags: list[str] = Field(default_factory=list, max_length=30)
    horizon_days: int = Field(default=7, ge=0, le=90)
    max_questions: int = Field(default=30, ge=1, le=200)

    @model_validator(mode="after")
    def validate_mode(self) -> ReviewSheetRequest:
        if self.mode == "numbers" and not self.numbers:
            raise ValueError("numbers mode requires at least one problem number")
        return self


def _validate_math_delimiters(text: str) -> None:
    # Accept the TeX delimiters commonly produced by OCR and PDF copy/paste.
    text = re.sub(r"\\\[(.*?)\\\]", lambda match: f"$$\n{match.group(1)}\n$$", text, flags=re.DOTALL)
    text = re.sub(r"\\\((.*?)\\\)", lambda match: f"${match.group(1)}$", text, flags=re.DOTALL)
    visible_lines: list[str] = []
    fence_character: str | None = None
    fence_length = 0
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        marker = stripped[0] if stripped else ""
        run_length = len(stripped) - len(stripped.lstrip(marker)) if marker else 0
        if marker in {"`", "~"} and run_length >= 3:
            if fence_character is None:
                fence_character = marker
                fence_length = run_length
                continue
            if marker == fence_character and run_length >= fence_length:
                fence_character = None
                fence_length = 0
                continue
        if fence_character is None:
            visible_lines.append(line)

    visible = re.sub(r"(`+)(?:(?!\1).)*\1", "", "".join(visible_lines))
    state: str | None = None
    index = 0
    while index < len(visible):
        if visible[index] == "\\":
            index += 2
            continue
        if visible[index] != "$":
            index += 1
            continue
        run_length = 1
        while index + run_length < len(visible) and visible[index + run_length] == "$":
            run_length += 1
        if run_length > 2:
            raise ValueError("math delimiters must use $ or $$")
        delimiter = "$$" if run_length == 2 else "$"
        if state is None:
            state = delimiter
        elif state == delimiter:
            state = None
        elif state == "$" and delimiter == "$$":
            raise ValueError("inline math cannot contain a $$ delimiter")
        index += run_length
    if state is not None:
        raise ValueError(f"unbalanced {state} math delimiter")
