from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from errorbook_mcp.config import Settings, _find_browser
from errorbook_mcp.service import ErrorbookService


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    (data_dir / "exports").mkdir(parents=True)
    (data_dir / "tmp").mkdir(parents=True)
    return Settings(
        data_dir=data_dir,
        timezone=ZoneInfo("Asia/Tokyo"),
        desired_retention=0.90,
        browser_path=_find_browser(),
        pdf_font='"Microsoft YaHei", sans-serif',
        log_level="INFO",
    )


@pytest.fixture
def service(settings: Settings) -> ErrorbookService:
    return ErrorbookService(settings)
