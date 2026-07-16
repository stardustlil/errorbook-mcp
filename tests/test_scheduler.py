from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from errorbook_mcp.scheduler import (
    ERROR_HALF_LIFE_DAYS,
    MemoryScheduler,
    decay,
    queue_score,
    update_lapse_mass,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def test_decay_halves_at_half_life() -> None:
    assert decay(4.0, ERROR_HALF_LIFE_DAYS, ERROR_HALF_LIFE_DAYS) == pytest.approx(2.0)


def test_fsrs_ratings_order_initial_due_dates() -> None:
    scheduler = MemoryScheduler(0.90)
    due_dates = {}
    for outcome in ("incorrect", "partial", "correct", "easy"):
        card = scheduler.review(scheduler.new_card(NOW), outcome, NOW)
        due_dates[outcome] = card.due
    assert due_dates["incorrect"] < due_dates["partial"] < due_dates["correct"] < due_dates["easy"]


def test_error_mass_saturates_and_success_reduces_it() -> None:
    mass = 0.0
    updated = NOW
    for day in range(10):
        reviewed = NOW + timedelta(days=day)
        mass = update_lapse_mass(mass, updated, "incorrect", reviewed)
        updated = reviewed
    assert mass <= 6.0
    reduced = update_lapse_mass(mass, updated, "correct", updated)
    assert reduced == pytest.approx(mass * 0.5)


def test_queue_score_is_monotonic_for_overdue_and_boost() -> None:
    common = dict(
        now=NOW,
        last_reviewed_at=NOW - timedelta(days=3),
        stability=3.0,
        difficulty=5.0,
        lapse_mass=0.0,
        lapse_mass_updated_at=NOW,
        importance=0.5,
        last_selected_at=None,
        retrievability=0.8,
    )
    future = queue_score(due_at=NOW + timedelta(days=3), manual_boost_mass=0.0, **common)
    overdue = queue_score(due_at=NOW - timedelta(days=3), manual_boost_mass=0.0, **common)
    boosted = queue_score(due_at=NOW - timedelta(days=3), manual_boost_mass=2.0, **common)
    assert future.total < overdue.total < boosted.total
    assert "manual_boost" in boosted.reasons
