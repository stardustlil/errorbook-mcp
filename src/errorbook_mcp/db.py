from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    number TEXT UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('single_choice', 'multiple_choice', 'short_answer', 'solution')),
    subject TEXT NOT NULL,
    stem_markdown TEXT NOT NULL,
    choices_json TEXT NOT NULL DEFAULT '[]',
    source TEXT,
    importance REAL NOT NULL DEFAULT 0.5 CHECK (importance >= 0 AND importance <= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'mastered', 'archived')),
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    fsrs_card_json TEXT NOT NULL,
    due_at TEXT NOT NULL,
    last_reviewed_at TEXT,
    stability REAL,
    difficulty REAL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    lapse_count INTEGER NOT NULL DEFAULT 0,
    wrong_streak INTEGER NOT NULL DEFAULT 0,
    lapse_mass REAL NOT NULL DEFAULT 0,
    lapse_mass_updated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_problems_status_due ON problems(status, due_at);
CREATE INDEX IF NOT EXISTS idx_problems_subject ON problems(subject);
CREATE INDEX IF NOT EXISTS idx_problems_hash ON problems(content_hash);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS problem_tags (
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (problem_id, tag_id)
);

CREATE TABLE IF NOT EXISTS problem_revisions (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(problem_id, version)
);

CREATE TABLE IF NOT EXISTS review_events (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK (outcome IN ('incorrect', 'partial', 'correct', 'easy', 'skipped')),
    response_markdown TEXT,
    notes TEXT,
    duration_seconds INTEGER,
    reviewed_at TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_problem_time ON review_events(problem_id, reviewed_at DESC);

CREATE TABLE IF NOT EXISTS priority_events (
    id TEXT PRIMARY KEY,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    points REAL NOT NULL,
    reason TEXT NOT NULL,
    half_life_days REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_priority_problem_time ON priority_events(problem_id, created_at DESC);

CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, key)
);

CREATE TABLE IF NOT EXISTS review_sets (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    selection_json TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_set_items (
    review_set_id TEXT NOT NULL REFERENCES review_sets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    problem_id INTEGER NOT NULL REFERENCES problems(id),
    problem_number TEXT NOT NULL,
    problem_version INTEGER NOT NULL,
    priority_score REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    PRIMARY KEY(review_set_id, position)
);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    review_set_id TEXT NOT NULL REFERENCES review_sets(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('generating', 'ready', 'failed')),
    questions_path TEXT,
    questions_sha256 TEXT,
    error_message TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            has_meta = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_meta'"
            ).fetchone()
            if has_meta:
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is None or int(row["value"]) != SCHEMA_VERSION:
                    found = "missing" if row is None else row["value"]
                    raise RuntimeError(
                        f"Unsupported database schema {found}; expected {SCHEMA_VERSION}"
                    )
            else:
                existing_tables = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
                if existing_tables:
                    raise RuntimeError("Refusing to modify an unversioned non-empty database")

            connection.executescript(SCHEMA_SQL)
            if not has_meta:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            return [dict(row) for row in rows]


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
