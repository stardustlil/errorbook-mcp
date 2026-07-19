from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from errorbook_mcp.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from errorbook_mcp.scheduler import iso_utc, parse_datetime, utc_now
from errorbook_mcp.schemas import (
    MAX_REVIEW_NOTES_LENGTH,
    MAX_REVIEW_RESPONSE_LENGTH,
    ProblemPatch,
    SearchFilters,
)
from errorbook_mcp.service import ErrorbookService

from .helpers import choice_problem, solution_problem


def test_create_duplicate_and_idempotency(service: ErrorbookService) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="create-key-0001")
    problem = created["problem"]
    assert created["created"] is True
    assert problem["number"].startswith("EB-")
    assert problem["memory"]["attempt_count"] == 1
    assert problem["memory"]["wrong_streak"] == 1

    replay = service.create_problem(choice_problem(), idempotency_key="create-key-0001")
    assert replay == created

    duplicate = service.create_problem(choice_problem(), idempotency_key="create-key-0002")
    assert duplicate["created"] is False
    assert duplicate["problem"]["number"] == problem["number"]

    with pytest.raises(IdempotencyConflictError):
        service.create_problem(choice_problem(suffix="不同"), idempotency_key="create-key-0001")


def test_concurrent_numbers_are_unique(service: ErrorbookService) -> None:
    def create(index: int) -> str:
        result = service.create_problem(
            choice_problem(suffix=f"-{index}"),
            idempotency_key=f"concurrent-{index:04d}",
            duplicate_policy="create_anyway",
        )
        return result["problem"]["number"]

    with ThreadPoolExecutor(max_workers=6) as executor:
        numbers = list(executor.map(create, range(18)))
    assert len(numbers) == len(set(numbers)) == 18


def test_update_uses_optimistic_lock_and_keeps_number(service: ErrorbookService) -> None:
    original = service.create_problem(choice_problem(), idempotency_key="update-create-01")
    number = original["problem"]["number"]
    updated = service.update_problem(
        number,
        ProblemPatch(stem_markdown="修正后的题干 $x^2=4$"),
        expected_version=1,
        reason="修正 OCR 字符",
        idempotency_key="update-write-001",
    )
    assert updated["problem"]["number"] == number
    assert updated["problem"]["version"] == 2
    assert "修正" in updated["problem"]["stem_markdown"]

    with pytest.raises(VersionConflictError):
        service.update_problem(
            number,
            ProblemPatch(subject="物理"),
            expected_version=1,
            reason="stale write",
            idempotency_key="update-write-002",
        )


def test_review_retry_is_idempotent_when_time_omitted(service: ErrorbookService) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="review-create-01")
    number = created["problem"]["number"]
    first = service.record_review(
        number,
        "correct",
        idempotency_key="review-event-0001",
        duration_seconds=90,
    )
    replay = service.record_review(
        number,
        "correct",
        idempotency_key="review-event-0001",
        duration_seconds=90,
    )
    assert replay == first
    assert first["problem"]["memory"]["attempt_count"] == 2
    assert first["problem"]["memory"]["wrong_streak"] == 0

    spaced_replay = service.record_review(
        f"  {number.lower()}  ",
        "correct",
        idempotency_key="review-event-0001",
        duration_seconds=90,
    )
    assert spaced_replay == first
    assert service.get_problem(number)["problem"]["memory"]["attempt_count"] == 2


def test_out_of_order_review_is_rejected(service: ErrorbookService) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="order-create-001")
    number = created["problem"]["number"]
    previous = parse_datetime(created["problem"]["memory"]["last_reviewed_at"])
    with pytest.raises(ValidationError, match="out-of-order"):
        service.record_review(
            number,
            "correct",
            idempotency_key="order-review-001",
            reviewed_at=previous - timedelta(seconds=1),
        )


def test_unreviewed_problem_rejects_review_before_creation(service: ErrorbookService) -> None:
    created = service.create_problem(
        choice_problem(initial_outcome="unreviewed"),
        idempotency_key="backdate-create-001",
    )["problem"]

    with pytest.raises(ValidationError, match="predate problem creation"):
        service.record_review(
            created["number"],
            "correct",
            idempotency_key="backdate-review-001",
            reviewed_at=parse_datetime(created["created_at"]) - timedelta(seconds=1),
        )

    problem = service.get_problem(created["number"], include_history=True)["problem"]
    assert problem["memory"]["attempt_count"] == 0
    assert problem["review_history"] == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        (
            "response_markdown",
            "x" * (MAX_REVIEW_RESPONSE_LENGTH + 1),
            "response_markdown",
        ),
        ("notes", "x" * (MAX_REVIEW_NOTES_LENGTH + 1), "notes"),
    ],
    ids=["response_markdown", "notes"],
)
def test_review_text_limits_are_enforced(
    service: ErrorbookService, field: str, value: str, message: str
) -> None:
    created = service.create_problem(choice_problem(), idempotency_key=f"limit-create-{field}")
    number = created["problem"]["number"]

    with pytest.raises(ValidationError, match=message):
        service.record_review(
            number,
            "correct",
            idempotency_key=f"limit-review-{field}",
            **{field: value},
        )

    assert service.get_problem(number)["problem"]["memory"]["attempt_count"] == 1


def test_priority_search_and_status(service: ErrorbookService) -> None:
    first = service.create_problem(choice_problem(), idempotency_key="search-create-01")
    second = service.create_problem(solution_problem(), idempotency_key="search-create-02")
    first_number = first["problem"]["number"]
    second_number = second["problem"]["number"]

    adjusted = service.adjust_priority(
        second_number,
        3,
        "用户再次复习时希望优先出现",
        idempotency_key="priority-event-01",
    )
    assert adjusted["effective_manual_boost"] == pytest.approx(3.0, abs=0.01)
    results = service.search_problems(SearchFilters(sort="priority"))
    assert results["items"][0]["number"] == second_number

    archived = service.set_status(first_number, "archived", idempotency_key="archive-event-01")
    assert archived["problem"]["status"] == "archived"
    active = service.search_problems(SearchFilters())
    assert {item["number"] for item in active["items"]} == {second_number}


def test_non_priority_search_paginates_in_sql(
    service: ErrorbookService, monkeypatch: pytest.MonkeyPatch
) -> None:
    numbers = [
        service.create_problem(
            choice_problem(suffix=str(index)),
            idempotency_key=f"page-create-{index:04d}",
        )["problem"]["number"]
        for index in range(3)
    ]
    statements: list[str] = []
    original_connect = service.db.connect

    def traced_connect():  # type: ignore[no-untyped-def]
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(service.db, "connect", traced_connect)
    result = service.search_problems(SearchFilters(sort="number", limit=1, offset=1))

    assert result["page"] == {"total": 3, "offset": 1, "limit": 1}
    assert [item["number"] for item in result["items"]] == [numbers[1]]
    assert any("ORDER BY p.number LIMIT 1 OFFSET 1" in sql for sql in statements)


def test_search_filters_sorts_and_library_stats(service: ErrorbookService) -> None:
    algebra = service.create_problem(
        choice_problem(suffix="含有百分号 %"), idempotency_key="filters-create-01"
    )["problem"]
    equations = service.create_problem(solution_problem(), idempotency_key="filters-create-02")[
        "problem"
    ]
    service.update_problem(
        equations["number"],
        ProblemPatch(subject="物理"),
        expected_version=1,
        reason="测试学科过滤",
        idempotency_key="filters-update-01",
    )

    assert {
        item["number"] for item in service.search_problems(SearchFilters(text="%"))["items"]
    } == {algebra["number"]}
    assert {
        item["number"]
        for item in service.search_problems(
            SearchFilters(
                numbers=[equations["number"].lower()],
                subjects=["物理"],
                tags=["方程组"],
                kinds=["solution"],
                due_before=utc_now() + timedelta(days=365),
            )
        )["items"]
    } == {equations["number"]}
    for sort in ("due", "created", "number"):
        result = service.search_problems(SearchFilters(sort=sort))
        assert result["page"]["total"] == 2

    service.set_status(algebra["number"], "archived", idempotency_key="filters-archive-01")
    all_statuses = service.search_problems(SearchFilters(statuses=[], sort="number"))
    assert {item["number"] for item in all_statuses["items"]} == {
        algebra["number"],
        equations["number"],
    }
    stats = service.stats()
    assert stats["counts"]["active"] == 1
    assert stats["counts"]["archived"] == 1
    assert stats["active_by_subject"] == [{"subject": "物理", "count": 1}]


def test_write_validation_replays_and_conflicts(service: ErrorbookService) -> None:
    with pytest.raises(ValidationError, match="idempotency_key"):
        service.create_problem(choice_problem(), idempotency_key="short")
    with pytest.raises(ValidationError, match="duplicate_policy"):
        service.create_problem(
            choice_problem(), idempotency_key="validation-create-01", duplicate_policy="invalid"
        )

    created = service.create_problem(choice_problem(), idempotency_key="validation-create-02")
    number = created["problem"]["number"]
    with pytest.raises(ValidationError, match="at least one field"):
        service.update_problem(
            number,
            ProblemPatch(),
            expected_version=1,
            reason="empty patch",
            idempotency_key="validation-update-01",
        )
    with pytest.raises(ValidationError, match="reason"):
        service.update_problem(
            number,
            ProblemPatch(subject="物理"),
            expected_version=1,
            reason=" ",
            idempotency_key="validation-update-02",
        )

    updated = service.update_problem(
        number,
        ProblemPatch(subject="物理"),
        expected_version=1,
        reason="修正学科",
        idempotency_key="validation-update-03",
    )
    assert (
        service.update_problem(
            number,
            ProblemPatch(subject="物理"),
            expected_version=1,
            reason="修正学科",
            idempotency_key="validation-update-03",
        )
        == updated
    )
    with pytest.raises(IdempotencyConflictError):
        service.update_problem(
            number,
            ProblemPatch(subject="化学"),
            expected_version=2,
            reason="不同请求",
            idempotency_key="validation-update-03",
        )

    with pytest.raises(ValidationError, match="duration_seconds"):
        service.record_review(
            number, "correct", idempotency_key="validation-review-01", duration_seconds=-1
        )
    with pytest.raises(ValidationError, match="five minutes"):
        service.record_review(
            number,
            "correct",
            idempotency_key="validation-review-02",
            reviewed_at=utc_now() + timedelta(minutes=6),
        )
    with pytest.raises(ValidationError, match="delta"):
        service.adjust_priority(number, 0, "invalid", idempotency_key="validation-priority-01")
    with pytest.raises(ValidationError, match="reason"):
        service.adjust_priority(number, 1, " ", idempotency_key="validation-priority-02")
    adjusted = service.adjust_priority(
        number, 2, "重点复习", idempotency_key="validation-priority-03"
    )
    assert (
        service.adjust_priority(number, 2, "重点复习", idempotency_key="validation-priority-03")
        == adjusted
    )
    with pytest.raises(ValidationError, match="status"):
        service.set_status(number, "deleted", idempotency_key="validation-status-01")
    archived = service.set_status(number, "archived", idempotency_key="validation-status-02")
    assert (
        service.set_status(number, "archived", idempotency_key="validation-status-02") == archived
    )
    with pytest.raises(ConflictError, match="archived"):
        service.record_review(number, "correct", idempotency_key="validation-review-03")
    with pytest.raises(NotFoundError):
        service.get_problem("EB-2099-999999")
    with pytest.raises(ValidationError, match="cannot be empty"):
        service.get_problem(" ")


def test_skipped_review_does_not_change_card(service: ErrorbookService) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="skip-create-001")
    number = created["problem"]["number"]
    before = created["problem"]["memory"]
    after = service.record_review(number, "skipped", idempotency_key="skip-review-001")["problem"][
        "memory"
    ]
    assert after["attempt_count"] == before["attempt_count"]
    assert after["due_at"] == before["due_at"]


def test_mastered_problem_is_reactivated_after_an_error(service: ErrorbookService) -> None:
    created = service.create_problem(choice_problem(), idempotency_key="mastered-create-01")
    number = created["problem"]["number"]
    service.set_status(number, "mastered", idempotency_key="mastered-status-01")
    reviewed = service.record_review(
        number,
        "incorrect",
        idempotency_key="mastered-review-01",
    )
    assert reviewed["problem"]["status"] == "active"
    assert number in {item["number"] for item in service.search_problems(SearchFilters())["items"]}


def test_long_overdue_problem_enters_fairness_bucket(service: ErrorbookService) -> None:
    old = service.create_problem(
        choice_problem(suffix="old"), idempotency_key="fairness-create-old"
    )["problem"]
    boosted = service.create_problem(
        choice_problem(suffix="boosted"), idempotency_key="fairness-create-boosted"
    )["problem"]
    service.adjust_priority(
        boosted["number"],
        5,
        "测试高优先级竞争",
        idempotency_key="fairness-boost-001",
    )
    now = utc_now()
    with service.db.transaction(immediate=True) as connection:
        connection.execute(
            "UPDATE problems SET created_at = ?, due_at = ? WHERE number = ?",
            (
                iso_utc(now - timedelta(days=60)),
                iso_utc(now - timedelta(days=30)),
                old["number"],
            ),
        )
    selected, metadata = service.select_review_candidates(
        mode="scheduled",
        numbers=[],
        subjects=[],
        tags=[],
        horizon_days=7,
        max_questions=1,
        now=now,
    )
    assert selected[0]["number"] == old["number"]
    assert "fairness_must_include" in selected[0]["priority"]["reasons"]
    assert metadata["must_include_count"] == 1
    assert metadata["backlog_count"] == 1
