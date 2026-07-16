from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from typing import Any

from fsrs import Card, Rating, Scheduler

from .errors import ValidationError
from .schemas import ReviewOutcome

FSRS_PACKAGE_VERSION = version("fsrs")
ALGORITHM_VERSION = f"fsrs-{FSRS_PACKAGE_VERSION}+errorbook-queue-v1"
ERROR_HALF_LIFE_DAYS = 42.0
MANUAL_BOOST_HALF_LIFE_DAYS = 14.0

RATING_BY_OUTCOME: dict[str, Rating] = {
    "incorrect": Rating.Again,
    "partial": Rating.Hard,
    "correct": Rating.Good,
    "easy": Rating.Easy,
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValidationError("datetime values must include a timezone")
    return parsed.astimezone(UTC)


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def decay(value: float, elapsed_days: float, half_life_days: float) -> float:
    return value * (2.0 ** (-max(elapsed_days, 0.0) / half_life_days))


@dataclass(frozen=True, slots=True)
class QueueScore:
    total: float
    components: dict[str, float]
    reasons: list[str]
    retrievability: float | None


class MemoryScheduler:
    def __init__(self, desired_retention: float) -> None:
        self.scheduler = Scheduler(
            desired_retention=desired_retention,
            enable_fuzzing=False,
        )

    def new_card(self, now: datetime) -> Card:
        return Card(due=now.astimezone(UTC))

    def load_card(self, payload: dict[str, Any]) -> Card:
        return Card.from_dict(payload)

    def review(
        self,
        card: Card,
        outcome: ReviewOutcome,
        reviewed_at: datetime,
        duration_seconds: int | None = None,
    ) -> Card:
        reviewed_at = reviewed_at.astimezone(UTC)
        if outcome == "skipped":
            return card
        if card.last_review and reviewed_at < card.last_review:
            raise ValidationError(
                "reviewed_at predates the latest review; out-of-order reviews are rejected",
                details={"latest_reviewed_at": iso_utc(card.last_review)},
            )
        reviewed, _ = self.scheduler.review_card(
            card,
            RATING_BY_OUTCOME[outcome],
            review_datetime=reviewed_at,
            review_duration=duration_seconds,
        )
        return reviewed

    def retrievability(self, card: Card, now: datetime) -> float | None:
        if card.last_review is None or card.stability is None:
            return None
        return self.scheduler.get_card_retrievability(card, current_datetime=now.astimezone(UTC))


def update_lapse_mass(
    current: float,
    updated_at: datetime,
    outcome: ReviewOutcome,
    reviewed_at: datetime,
) -> float:
    elapsed = max((reviewed_at - updated_at).total_seconds() / 86_400, 0.0)
    mass = decay(current, elapsed, ERROR_HALF_LIFE_DAYS)
    if outcome == "incorrect":
        return min(6.0, mass + 1.0)
    if outcome == "partial":
        return min(6.0, mass + 0.5)
    if outcome == "correct":
        return mass * 0.5
    if outcome == "easy":
        return mass * 0.25
    return mass


def queue_score(
    *,
    now: datetime,
    due_at: datetime,
    last_reviewed_at: datetime | None,
    stability: float | None,
    difficulty: float | None,
    lapse_mass: float,
    lapse_mass_updated_at: datetime,
    manual_boost_mass: float,
    importance: float,
    last_selected_at: datetime | None,
    retrievability: float | None,
) -> QueueScore:
    scheduled_days = max(stability or 1.0, 1.0)
    relative_due = (now - due_at).total_seconds() / 86_400 / scheduled_days
    if relative_due < 0:
        urgency = 0.5 * math.exp(max(relative_due, -30.0))
    else:
        urgency = 1.0 - 0.5 * math.exp(-min(relative_due, 30.0))

    error_mass = decay(
        lapse_mass,
        (now - lapse_mass_updated_at).total_seconds() / 86_400,
        ERROR_HALF_LIFE_DAYS,
    )
    error_pressure = 1.0 - math.exp(-max(error_mass, 0.0))
    boost_pressure = math.copysign(1.0 - math.exp(-abs(manual_boost_mass)), manual_boost_mass)
    normalized_difficulty = min(max(((difficulty or 5.0) - 1.0) / 9.0, 0.0), 1.0)
    if last_selected_at is None:
        age = 1.0
    else:
        age = min(max(((now - last_selected_at).total_seconds() / 86_400 - 7) / 49, 0), 1)

    components = {
        "urgency": 42.0 * urgency,
        "recent_error": 22.0 * error_pressure,
        "difficulty": 12.0 * normalized_difficulty,
        "manual_boost": 10.0 * boost_pressure,
        "importance": 8.0 * importance,
        "not_recently_selected": 6.0 * age,
    }
    reasons: list[str] = []
    overdue_days = max((now - due_at).days, 0)
    if overdue_days:
        reasons.append(f"overdue_{overdue_days}d")
    elif due_at <= now:
        reasons.append("due_now")
    else:
        reasons.append("due_soon")
    if error_mass >= 1.5:
        reasons.append("recent_repeated_errors")
    elif error_mass >= 0.5:
        reasons.append("recent_error")
    if manual_boost_mass >= 0.25:
        reasons.append("manual_boost")
    if difficulty and difficulty >= 7:
        reasons.append("high_difficulty")
    if last_selected_at is None:
        reasons.append("never_selected")

    return QueueScore(
        total=round(sum(components.values()), 4),
        components={name: round(value, 4) for name, value in components.items()},
        reasons=reasons,
        retrievability=None if retrievability is None else round(retrievability, 4),
    )
