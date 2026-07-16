from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _find_browser() -> Path | None:
    configured = os.getenv("ERRORBOOK_BROWSER_PATH")
    if configured:
        path = Path(configured).expanduser().resolve()
        return path if path.is_file() else None

    candidates = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    return None


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    timezone: ZoneInfo
    desired_retention: float
    browser_path: Path | None
    pdf_font: str
    log_level: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / "errorbook.sqlite3"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @classmethod
    def from_env(cls) -> Settings:
        data_dir = Path(os.getenv("ERRORBOOK_DATA_DIR", "./data")).expanduser().resolve()
        timezone_name = os.getenv("ERRORBOOK_TIMEZONE", "Asia/Tokyo")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown ERRORBOOK_TIMEZONE: {timezone_name}") from exc

        settings = cls(
            data_dir=data_dir,
            timezone=timezone,
            desired_retention=_env_float("ERRORBOOK_DESIRED_RETENTION", 0.90, 0.70, 0.97),
            browser_path=_find_browser(),
            pdf_font=os.getenv(
                "ERRORBOOK_PDF_FONT",
                '"Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC", sans-serif',
            ),
            log_level=os.getenv("ERRORBOOK_LOG_LEVEL", "INFO").upper(),
        )
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.exports_dir.mkdir(parents=True, exist_ok=True)
        settings.temp_dir.mkdir(parents=True, exist_ok=True)
        return settings
