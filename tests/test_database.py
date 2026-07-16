from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from errorbook_mcp.db import Database


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
