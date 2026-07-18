from __future__ import annotations

from errorbook_mcp.schemas import Choice, ProblemDraft


def choice_problem(*, suffix: str = "", initial_outcome: str = "incorrect") -> ProblemDraft:
    return ProblemDraft(
        kind="single_choice",
        subject="数学",
        stem_markdown=f"若 $x^2=4$，则 $x$ 的值为多少？{suffix}",
        choices=[
            Choice(label="A", content_markdown="$2$"),
            Choice(label="B", content_markdown="$-2$"),
            Choice(label="C", content_markdown="$\\pm 2$"),
            Choice(label="D", content_markdown="$4$"),
        ],
        tags=["代数", "平方根"],
        initial_outcome=initial_outcome,
    )


def solution_problem(*, suffix: str = "") -> ProblemDraft:
    return ProblemDraft(
        kind="solution",
        subject="数学",
        stem_markdown=("解方程组：\n\n$$\\begin{cases}2x+y=7\\\\x-y=2\\end{cases}$$\n" + suffix),
        tags=["方程组"],
    )
