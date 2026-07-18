from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database, json_dumps
from .errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from .scheduler import (
    ALGORITHM_VERSION,
    MANUAL_BOOST_HALF_LIFE_DAYS,
    MemoryScheduler,
    decay,
    iso_utc,
    parse_datetime,
    queue_score,
    update_lapse_mass,
    utc_now,
)
from .schemas import ProblemDraft, ProblemPatch, ReviewOutcome, SearchFilters

MAX_FUTURE_REVIEW_SKEW = timedelta(minutes=5)
IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


def _normalized_markdown(value: str | None) -> str | None:
    if value is None:
        return None
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


def _content_hash(draft: ProblemDraft) -> str:
    canonical = {
        "kind": draft.kind,
        "stem": _normalized_markdown(draft.stem_markdown),
        "choices": [
            {
                "label": choice.label.casefold(),
                "content": _normalized_markdown(choice.content_markdown),
            }
            for choice in draft.choices
        ],
    }
    return hashlib.sha256(json_dumps(canonical).encode("utf-8")).hexdigest()


def _request_hash(payload: Any) -> str:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json")
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


class ErrorbookService:
    def __init__(self, settings: Settings, database: Database | None = None) -> None:
        self.settings = settings
        self.db = database or Database(settings.database_path)
        self.db.initialize()
        self.memory = MemoryScheduler(settings.desired_retention)
        self.algorithm_version = f"{ALGORITHM_VERSION};retention={settings.desired_retention:.3f}"

    def create_problem(
        self,
        draft: ProblemDraft,
        *,
        idempotency_key: str,
        duplicate_policy: str = "return_existing",
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        if duplicate_policy not in {"return_existing", "create_anyway"}:
            raise ValidationError("duplicate_policy must be return_existing or create_anyway")

        request_payload = {
            "draft": draft.model_dump(mode="json"),
            "duplicate_policy": duplicate_policy,
        }
        request_hash = _request_hash(request_payload)
        now = utc_now()
        digest = _content_hash(draft)

        with self.db.transaction(immediate=True) as connection:
            replay = self._idempotency_replay(
                connection, "create_problem", idempotency_key, request_hash
            )
            if replay is not None:
                return replay

            duplicate = connection.execute(
                """
                SELECT * FROM problems
                WHERE content_hash = ? AND status != 'archived'
                ORDER BY id LIMIT 1
                """,
                (digest,),
            ).fetchone()
            if duplicate is not None and duplicate_policy == "return_existing":
                problem = self._serialize_problem(connection, duplicate, now=now)
                response = {
                    "ok": True,
                    "created": False,
                    "duplicate": True,
                    "message": "An active problem with the same stem and choices already exists.",
                    "problem": problem,
                }
                self._save_idempotency(
                    connection, "create_problem", idempotency_key, request_hash, response, now
                )
                return response

            card = self.memory.new_card(now)
            before_card = card.to_dict()
            lapse_mass = 0.0
            attempt_count = 0
            lapse_count = 0
            wrong_streak = 0
            last_reviewed_at: str | None = None
            if draft.initial_outcome == "incorrect":
                card = self.memory.review(card, "incorrect", now)
                lapse_mass = 1.0
                attempt_count = 1
                lapse_count = 1
                wrong_streak = 1
                last_reviewed_at = iso_utc(now)

            cursor = connection.execute(
                """
                INSERT INTO problems(
                    kind, subject, stem_markdown, choices_json, source, importance, content_hash,
                    fsrs_card_json, due_at, last_reviewed_at, stability, difficulty,
                    attempt_count, lapse_count, wrong_streak, lapse_mass,
                    lapse_mass_updated_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.kind,
                    draft.subject,
                    _normalized_markdown(draft.stem_markdown),
                    json_dumps([choice.model_dump() for choice in draft.choices]),
                    _normalized_markdown(draft.source),
                    draft.importance,
                    digest,
                    json_dumps(card.to_dict()),
                    iso_utc(card.due),
                    last_reviewed_at,
                    card.stability,
                    card.difficulty,
                    attempt_count,
                    lapse_count,
                    wrong_streak,
                    lapse_mass,
                    iso_utc(now),
                    iso_utc(now),
                    iso_utc(now),
                ),
            )
            problem_id = int(cursor.lastrowid)
            number = f"EB-{now.year}-{problem_id:06d}"
            connection.execute("UPDATE problems SET number = ? WHERE id = ?", (number, problem_id))
            self._replace_tags(connection, problem_id, draft.tags)

            if draft.initial_outcome == "incorrect":
                connection.execute(
                    """
                    INSERT INTO review_events(
                        id, problem_id, outcome, notes, reviewed_at, algorithm_version,
                        before_json, after_json, created_at
                    ) VALUES (?, ?, 'incorrect', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        problem_id,
                        "Initial error recorded when the problem was added.",
                        iso_utc(now),
                        self.algorithm_version,
                        json_dumps(before_card),
                        json_dumps(card.to_dict()),
                        iso_utc(now),
                    ),
                )

            row = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (problem_id,)
            ).fetchone()
            assert row is not None
            snapshot = self._content_snapshot(connection, row)
            connection.execute(
                """
                INSERT INTO problem_revisions(id, problem_id, version, snapshot_json, reason, created_at)
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    problem_id,
                    json_dumps(snapshot),
                    "Problem created",
                    iso_utc(now),
                ),
            )
            response = {
                "ok": True,
                "created": True,
                "duplicate": False,
                "problem": self._serialize_problem(connection, row, now=now),
            }
            self._save_idempotency(
                connection, "create_problem", idempotency_key, request_hash, response, now
            )
            return response

    def get_problem(self, number: str, *, include_history: bool = False) -> dict[str, Any]:
        with self.db.transaction() as connection:
            row = self._find_problem(connection, number)
            problem = self._serialize_problem(
                connection, row, now=utc_now(), include_history=include_history
            )
            return {"ok": True, "problem": problem}

    def search_problems(self, filters: SearchFilters) -> dict[str, Any]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if filters.text:
            escaped = filters.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(
                "(p.number LIKE ? ESCAPE '\\' OR p.subject LIKE ? ESCAPE '\\' "
                "OR p.stem_markdown LIKE ? ESCAPE '\\')"
            )
            parameters.extend([f"%{escaped}%"] * 3)
        if filters.numbers:
            placeholders = ",".join("?" for _ in filters.numbers)
            clauses.append(f"p.number IN ({placeholders})")
            parameters.extend(self._canonical_number(number) for number in filters.numbers)
        if filters.subjects:
            placeholders = ",".join("?" for _ in filters.subjects)
            clauses.append(f"p.subject IN ({placeholders})")
            parameters.extend(filters.subjects)
        if filters.kinds:
            placeholders = ",".join("?" for _ in filters.kinds)
            clauses.append(f"p.kind IN ({placeholders})")
            parameters.extend(filters.kinds)
        if filters.statuses:
            placeholders = ",".join("?" for _ in filters.statuses)
            clauses.append(f"p.status IN ({placeholders})")
            parameters.extend(filters.statuses)
        if filters.due_before:
            clauses.append("p.due_at <= ?")
            parameters.append(iso_utc(filters.due_before))
        for tag in filters.tags:
            clauses.append(
                "EXISTS (SELECT 1 FROM problem_tags pt JOIN tags t ON t.id = pt.tag_id "
                "WHERE pt.problem_id = p.id AND t.name = ? COLLATE NOCASE)"
            )
            parameters.append(tag)

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT p.* FROM problems p" + where,
                tuple(parameters),
            ).fetchall()
            now = utc_now()
            serialized = [self._serialize_problem(connection, row, now=now) for row in rows]

        if filters.sort == "priority":
            serialized.sort(
                key=lambda item: (
                    -item["priority"]["score"],
                    item["memory"]["due_at"],
                    item["number"],
                )
            )
        elif filters.sort == "due":
            serialized.sort(key=lambda item: (item["memory"]["due_at"], item["number"]))
        elif filters.sort == "created":
            serialized.sort(key=lambda item: (item["created_at"], item["number"]), reverse=True)
        else:
            serialized.sort(key=lambda item: item["number"])

        total = len(serialized)
        page = serialized[filters.offset : filters.offset + filters.limit]
        return {
            "ok": True,
            "items": page,
            "page": {"total": total, "offset": filters.offset, "limit": filters.limit},
        }

    def update_problem(
        self,
        number: str,
        patch: ProblemPatch,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise ValidationError("reason must contain 1 to 500 characters")
        if not patch.model_fields_set:
            raise ValidationError("patch must contain at least one field")

        canonical_number = self._canonical_number(number)
        request_payload = {
            "number": canonical_number,
            "patch": patch.model_dump(mode="json", exclude_unset=True),
            "expected_version": expected_version,
            "reason": reason,
        }
        request_hash = _request_hash(request_payload)
        now = utc_now()
        scope = f"update_problem:{canonical_number}"
        with self.db.transaction(immediate=True) as connection:
            replay = self._idempotency_replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._find_problem(connection, canonical_number)
            if row["version"] != expected_version:
                raise VersionConflictError(
                    f"Problem {row['number']} is at version {row['version']}, not {expected_version}",
                    details={"current_version": row["version"]},
                )

            tags = self._tags_for_problem(connection, row["id"])
            current = {
                "kind": row["kind"],
                "subject": row["subject"],
                "stem_markdown": row["stem_markdown"],
                "choices": json.loads(row["choices_json"]),
                "source": row["source"],
                "tags": tags,
                "importance": row["importance"],
                "initial_outcome": "unreviewed",
            }
            for field in patch.model_fields_set:
                current[field] = getattr(patch, field)
            validated = ProblemDraft.model_validate(current)
            new_version = row["version"] + 1
            connection.execute(
                """
                UPDATE problems SET kind = ?, subject = ?, stem_markdown = ?, choices_json = ?,
                    source = ?, importance = ?,
                    content_hash = ?, version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    validated.kind,
                    validated.subject,
                    _normalized_markdown(validated.stem_markdown),
                    json_dumps([choice.model_dump() for choice in validated.choices]),
                    _normalized_markdown(validated.source),
                    validated.importance,
                    _content_hash(validated),
                    new_version,
                    iso_utc(now),
                    row["id"],
                ),
            )
            self._replace_tags(connection, row["id"], validated.tags)
            updated = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (row["id"],)
            ).fetchone()
            assert updated is not None
            connection.execute(
                """
                INSERT INTO problem_revisions(id, problem_id, version, snapshot_json, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    row["id"],
                    new_version,
                    json_dumps(self._content_snapshot(connection, updated)),
                    reason,
                    iso_utc(now),
                ),
            )
            response = {
                "ok": True,
                "problem": self._serialize_problem(connection, updated, now=now),
            }
            self._save_idempotency(connection, scope, idempotency_key, request_hash, response, now)
            return response

    def record_review(
        self,
        number: str,
        outcome: ReviewOutcome,
        *,
        idempotency_key: str,
        response_markdown: str | None = None,
        notes: str | None = None,
        duration_seconds: int | None = None,
        reviewed_at: datetime | None = None,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        if duration_seconds is not None and not 0 <= duration_seconds <= 86_400:
            raise ValidationError("duration_seconds must be between 0 and 86400")
        provided_reviewed_at = reviewed_at
        if reviewed_at is not None:
            reviewed_at = parse_datetime(reviewed_at)
        else:
            reviewed_at = utc_now()
        if reviewed_at > utc_now() + MAX_FUTURE_REVIEW_SKEW:
            raise ValidationError("reviewed_at cannot be more than five minutes in the future")

        canonical_number = self._canonical_number(number)
        payload = {
            "number": canonical_number,
            "outcome": outcome,
            "response_markdown": response_markdown,
            "notes": notes,
            "duration_seconds": duration_seconds,
            "reviewed_at": iso_utc(provided_reviewed_at) if provided_reviewed_at else None,
        }
        request_hash = _request_hash(payload)
        scope = f"record_review:{canonical_number}"
        created_at = utc_now()
        with self.db.transaction(immediate=True) as connection:
            replay = self._idempotency_replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._find_problem(connection, canonical_number)
            if row["status"] == "archived":
                raise ConflictError(f"Problem {row['number']} is archived")

            card = self.memory.load_card(json.loads(row["fsrs_card_json"]))
            before = card.to_dict()
            reviewed_card = self.memory.review(
                card, outcome, reviewed_at, duration_seconds=duration_seconds
            )
            after = reviewed_card.to_dict()

            updated_mass = update_lapse_mass(
                row["lapse_mass"],
                parse_datetime(row["lapse_mass_updated_at"]),
                outcome,
                reviewed_at,
            )
            attempt_increment = 0 if outcome == "skipped" else 1
            correct_increment = 1 if outcome in {"correct", "easy"} else 0
            lapse_increment = 1 if outcome == "incorrect" else 0
            if outcome in {"correct", "easy"}:
                wrong_streak = 0
            elif outcome in {"incorrect", "partial"}:
                wrong_streak = row["wrong_streak"] + 1
            else:
                wrong_streak = row["wrong_streak"]
            updated_status = "active" if outcome in {"incorrect", "partial"} else row["status"]

            connection.execute(
                """
                INSERT INTO review_events(
                    id, problem_id, outcome, response_markdown, notes, duration_seconds,
                    reviewed_at, algorithm_version, before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    row["id"],
                    outcome,
                    _normalized_markdown(response_markdown),
                    _normalized_markdown(notes),
                    duration_seconds,
                    iso_utc(reviewed_at),
                    self.algorithm_version,
                    json_dumps(before),
                    json_dumps(after),
                    iso_utc(created_at),
                ),
            )
            if outcome != "skipped":
                connection.execute(
                    """
                    UPDATE problems SET status = ?, fsrs_card_json = ?, due_at = ?, last_reviewed_at = ?,
                        stability = ?, difficulty = ?, attempt_count = attempt_count + ?,
                        correct_count = correct_count + ?, lapse_count = lapse_count + ?,
                        wrong_streak = ?, lapse_mass = ?, lapse_mass_updated_at = ?,
                        updated_at = ? WHERE id = ?
                    """,
                    (
                        updated_status,
                        json_dumps(after),
                        iso_utc(reviewed_card.due),
                        iso_utc(reviewed_at),
                        reviewed_card.stability,
                        reviewed_card.difficulty,
                        attempt_increment,
                        correct_increment,
                        lapse_increment,
                        wrong_streak,
                        updated_mass,
                        iso_utc(reviewed_at),
                        iso_utc(created_at),
                        row["id"],
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (row["id"],)
            ).fetchone()
            assert updated is not None
            response = {
                "ok": True,
                "review": {
                    "problem_number": row["number"],
                    "outcome": outcome,
                    "reviewed_at": iso_utc(reviewed_at),
                    "algorithm_version": self.algorithm_version,
                },
                "problem": self._serialize_problem(connection, updated, now=created_at),
            }
            self._save_idempotency(
                connection, scope, idempotency_key, request_hash, response, created_at
            )
            return response

    def adjust_priority(
        self,
        number: str,
        delta: int,
        reason: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._validate_idempotency_key(idempotency_key)
        if delta == 0 or not -5 <= delta <= 5:
            raise ValidationError("delta must be an integer from -5 to -1 or 1 to 5")
        reason = reason.strip()
        if not reason or len(reason) > 500:
            raise ValidationError("reason must contain 1 to 500 characters")
        canonical_number = self._canonical_number(number)
        payload = {"number": canonical_number, "delta": delta, "reason": reason}
        request_hash = _request_hash(payload)
        scope = f"adjust_priority:{canonical_number}"
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            replay = self._idempotency_replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._find_problem(connection, canonical_number)
            connection.execute(
                """
                INSERT INTO priority_events(id, problem_id, points, reason, half_life_days, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    row["id"],
                    float(delta),
                    reason,
                    MANUAL_BOOST_HALF_LIFE_DAYS,
                    iso_utc(now),
                ),
            )
            problem = self._serialize_problem(connection, row, now=now)
            response = {
                "ok": True,
                "problem_number": row["number"],
                "adjustment": delta,
                "effective_manual_boost": problem["priority"]["manual_boost_mass"],
                "priority": problem["priority"],
            }
            self._save_idempotency(connection, scope, idempotency_key, request_hash, response, now)
            return response

    def set_status(
        self,
        number: str,
        status: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if status not in {"active", "mastered", "archived"}:
            raise ValidationError("status must be active, mastered, or archived")
        self._validate_idempotency_key(idempotency_key)
        canonical_number = self._canonical_number(number)
        payload = {"number": canonical_number, "status": status}
        request_hash = _request_hash(payload)
        scope = f"set_status:{canonical_number}"
        now = utc_now()
        with self.db.transaction(immediate=True) as connection:
            replay = self._idempotency_replay(connection, scope, idempotency_key, request_hash)
            if replay is not None:
                return replay
            row = self._find_problem(connection, canonical_number)
            if row["status"] != status:
                connection.execute(
                    "UPDATE problems SET status = ?, updated_at = ? WHERE id = ?",
                    (status, iso_utc(now), row["id"]),
                )
            updated = connection.execute(
                "SELECT * FROM problems WHERE id = ?", (row["id"],)
            ).fetchone()
            assert updated is not None
            response = {
                "ok": True,
                "problem": self._serialize_problem(connection, updated, now=now),
            }
            self._save_idempotency(connection, scope, idempotency_key, request_hash, response, now)
            return response

    def stats(self) -> dict[str, Any]:
        now = utc_now()
        horizon = now + timedelta(days=7)
        with self.db.transaction() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM problems GROUP BY status"
                ).fetchall()
            }
            due_now = connection.execute(
                "SELECT COUNT(*) AS count FROM problems WHERE status = 'active' AND due_at <= ?",
                (iso_utc(now),),
            ).fetchone()["count"]
            due_week = connection.execute(
                "SELECT COUNT(*) AS count FROM problems WHERE status = 'active' AND due_at <= ?",
                (iso_utc(horizon),),
            ).fetchone()["count"]
            by_subject = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT subject, COUNT(*) AS count FROM problems
                    WHERE status = 'active' GROUP BY subject ORDER BY count DESC, subject
                    """
                ).fetchall()
            ]
        return {
            "ok": True,
            "counts": {
                "active": counts.get("active", 0),
                "mastered": counts.get("mastered", 0),
                "archived": counts.get("archived", 0),
                "due_now": due_now,
                "due_within_7_days": due_week,
            },
            "active_by_subject": by_subject,
            "algorithm_version": self.algorithm_version,
            "desired_retention": self.settings.desired_retention,
            "as_of": iso_utc(now),
        }

    def select_review_candidates(
        self,
        *,
        mode: str,
        numbers: list[str],
        subjects: list[str],
        tags: list[str],
        horizon_days: int,
        max_questions: int,
        now: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        clauses = ["p.status = 'active'"]
        parameters: list[Any] = []
        if mode == "numbers":
            placeholders = ",".join("?" for _ in numbers)
            clauses.append(f"p.number IN ({placeholders})")
            parameters.extend(self._canonical_number(number) for number in numbers)
        if subjects:
            placeholders = ",".join("?" for _ in subjects)
            clauses.append(f"p.subject IN ({placeholders})")
            parameters.extend(subjects)
        for tag in tags:
            clauses.append(
                "EXISTS (SELECT 1 FROM problem_tags pt JOIN tags t ON t.id = pt.tag_id "
                "WHERE pt.problem_id = p.id AND t.name = ? COLLATE NOCASE)"
            )
            parameters.append(tag)

        with self.db.transaction() as connection:
            rows = connection.execute(
                "SELECT p.* FROM problems p WHERE " + " AND ".join(clauses),
                tuple(parameters),
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            horizon = now + timedelta(days=horizon_days)
            for row in rows:
                problem = self._serialize_problem(connection, row, now=now)
                if mode == "scheduled":
                    due = parse_datetime(row["due_at"])
                    manual = problem["priority"]["manual_boost_mass"]
                    if due > horizon and manual < 0.25:
                        continue
                candidates.append(problem)

        candidates.sort(
            key=lambda item: (-item["priority"]["score"], item["memory"]["due_at"], item["number"])
        )
        fairness_cutoff = now - timedelta(days=28)
        must_include = [
            item
            for item in candidates
            if parse_datetime(item["memory"]["due_at"]) <= now
            and parse_datetime(item["created_at"]) <= fairness_cutoff
        ]
        must_include.sort(
            key=lambda item: (
                item["created_at"],
                item["memory"]["due_at"],
                item["number"],
            )
        )
        must_numbers = {item["number"] for item in must_include}
        for item in must_include:
            item["priority"]["reasons"].append("fairness_must_include")
        ranked_remainder = [item for item in candidates if item["number"] not in must_numbers]
        selected = (must_include + ranked_remainder)[:max_questions]
        backlog_count = max(len(candidates) - len(selected), 0)
        metadata = {
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "backlog_count": backlog_count,
            "estimated_weeks_to_clear_backlog": math.ceil(backlog_count / max_questions),
            "must_include_count": len(must_include),
            "must_include_overflow": max(len(must_include) - max_questions, 0),
            "horizon_end": iso_utc(now + timedelta(days=horizon_days)),
        }
        return selected, metadata

    def _serialize_problem(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        include_history: bool = False,
    ) -> dict[str, Any]:
        card = self.memory.load_card(json.loads(row["fsrs_card_json"]))
        retrievability = self.memory.retrievability(card, now)
        manual_mass = self._manual_boost_mass(connection, row["id"], now)
        last_reviewed = parse_datetime(row["last_reviewed_at"]) if row["last_reviewed_at"] else None
        score = queue_score(
            now=now,
            due_at=parse_datetime(row["due_at"]),
            last_reviewed_at=last_reviewed,
            stability=row["stability"],
            difficulty=row["difficulty"],
            lapse_mass=row["lapse_mass"],
            lapse_mass_updated_at=parse_datetime(row["lapse_mass_updated_at"]),
            manual_boost_mass=manual_mass,
            importance=row["importance"],
            retrievability=retrievability,
        )
        result: dict[str, Any] = {
            "id": row["id"],
            "number": row["number"],
            "kind": row["kind"],
            "subject": row["subject"],
            "stem_markdown": row["stem_markdown"],
            "choices": json.loads(row["choices_json"]),
            "source": row["source"],
            "tags": self._tags_for_problem(connection, row["id"]),
            "importance": row["importance"],
            "status": row["status"],
            "version": row["version"],
            "memory": {
                "state": card.state.name.lower(),
                "step": card.step,
                "due_at": row["due_at"],
                "last_reviewed_at": row["last_reviewed_at"],
                "stability_days": row["stability"],
                "difficulty": row["difficulty"],
                "retrievability": score.retrievability,
                "attempt_count": row["attempt_count"],
                "correct_count": row["correct_count"],
                "lapse_count": row["lapse_count"],
                "wrong_streak": row["wrong_streak"],
            },
            "priority": {
                "score": score.total,
                "components": score.components,
                "reasons": score.reasons,
                "manual_boost_mass": round(manual_mass, 4),
            },
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if include_history:
            result["review_history"] = [
                {
                    **dict(event),
                    "before": json.loads(event["before_json"]),
                    "after": json.loads(event["after_json"]),
                }
                for event in connection.execute(
                    """
                    SELECT id, outcome, response_markdown, notes, duration_seconds,
                           reviewed_at, algorithm_version, before_json, after_json
                    FROM review_events WHERE problem_id = ? ORDER BY reviewed_at, created_at
                    """,
                    (row["id"],),
                ).fetchall()
            ]
            for event in result["review_history"]:
                event.pop("before_json")
                event.pop("after_json")
            result["priority_history"] = [
                dict(event)
                for event in connection.execute(
                    """
                    SELECT id, points, reason, half_life_days, created_at
                    FROM priority_events WHERE problem_id = ? ORDER BY created_at
                    """,
                    (row["id"],),
                ).fetchall()
            ]
        return result

    def _content_snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "number": row["number"],
            "kind": row["kind"],
            "subject": row["subject"],
            "stem_markdown": row["stem_markdown"],
            "choices": json.loads(row["choices_json"]),
            "source": row["source"],
            "tags": self._tags_for_problem(connection, row["id"]),
            "importance": row["importance"],
            "version": row["version"],
        }

    def _find_problem(self, connection: sqlite3.Connection, number: str) -> sqlite3.Row:
        normalized = self._canonical_number(number)
        row = connection.execute(
            "SELECT * FROM problems WHERE number = ?", (normalized,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Problem {normalized} was not found")
        return row

    def _canonical_number(self, number: str) -> str:
        normalized = number.strip().upper()
        if not normalized:
            raise ValidationError("problem number cannot be empty")
        return normalized

    def _tags_for_problem(self, connection: sqlite3.Connection, problem_id: int) -> list[str]:
        return [
            row["name"]
            for row in connection.execute(
                """
                SELECT t.name FROM tags t JOIN problem_tags pt ON pt.tag_id = t.id
                WHERE pt.problem_id = ? ORDER BY t.name COLLATE NOCASE
                """,
                (problem_id,),
            ).fetchall()
        ]

    def _replace_tags(
        self, connection: sqlite3.Connection, problem_id: int, tags: Iterable[str]
    ) -> None:
        normalized: dict[str, str] = {}
        for tag in tags:
            clean = tag.strip()
            if clean:
                normalized.setdefault(clean.casefold(), clean)
        connection.execute("DELETE FROM problem_tags WHERE problem_id = ?", (problem_id,))
        for tag in sorted(normalized.values(), key=str.casefold):
            connection.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            tag_row = connection.execute(
                "SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (tag,)
            ).fetchone()
            assert tag_row is not None
            connection.execute(
                "INSERT INTO problem_tags(problem_id, tag_id) VALUES (?, ?)",
                (problem_id, tag_row["id"]),
            )

    def _manual_boost_mass(
        self, connection: sqlite3.Connection, problem_id: int, now: datetime
    ) -> float:
        mass = 0.0
        for event in connection.execute(
            "SELECT points, half_life_days, created_at FROM priority_events WHERE problem_id = ?",
            (problem_id,),
        ).fetchall():
            elapsed = (now - parse_datetime(event["created_at"])).total_seconds() / 86_400
            mass += decay(event["points"], elapsed, event["half_life_days"])
        return min(max(mass, -5.0), 5.0)

    def _validate_idempotency_key(self, key: str) -> None:
        if not IDEMPOTENCY_PATTERN.fullmatch(key):
            raise ValidationError(
                "idempotency_key must be 8-128 characters using letters, digits, . _ : or -"
            )

    def _idempotency_replay(
        self,
        connection: sqlite3.Connection,
        scope: str,
        key: str,
        request_hash: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT request_hash, response_json FROM idempotency_records WHERE scope = ? AND key = ?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError(
                "The idempotency key was already used with different input",
                details={"scope": scope, "idempotency_key": key},
            )
        return json.loads(row["response_json"])

    def _save_idempotency(
        self,
        connection: sqlite3.Connection,
        scope: str,
        key: str,
        request_hash: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_records(scope, key, request_hash, response_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, key, request_hash, json_dumps(response), iso_utc(now)),
        )
