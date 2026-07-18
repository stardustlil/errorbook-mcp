from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from errorbook_mcp.db import SCHEMA_VERSION, Database


def test_incompatible_database_is_rejected_before_schema_changes(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO schema_meta VALUES ('schema_version', '999')")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="Unsupported database schema"):
        Database(path).initialize()

    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    connection.close()
    assert tables == {"schema_meta"}


def test_unversioned_nonempty_database_is_not_modified(tmp_path: Path) -> None:
    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE user_data(value TEXT)")

    with pytest.raises(RuntimeError, match="unversioned non-empty"):
        Database(path).initialize()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"user_data"}


def test_database_initialization_is_idempotent_and_enables_integrity_pragmas(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "errorbook.sqlite3")
    database.initialize()
    database.initialize()

    with database.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert version == str(SCHEMA_VERSION)
    assert foreign_keys == 1
    assert journal_mode == "wal"
