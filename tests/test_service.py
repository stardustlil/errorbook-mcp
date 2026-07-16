from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from errorbook_mcp.errors import IdempotencyConflictError, ValidationError, VersionConflictError
from errorbook_mcp.scheduler import iso_utc, parse_datetime, utc_now
from errorbook_mcp.schemas import ProblemPatch, SearchFilters
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
