from __future__ import annotations

from pathlib import Path

import pytest

from errorbook_mcp.config import Settings, _find_browser


def _clear_errorbook_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ERRORBOOK_DATA_DIR",
        "ERRORBOOK_TIMEZONE",
        "ERRORBOOK_DESIRED_RETENTION",
        "ERRORBOOK_BROWSER_PATH",
        "ERRORBOOK_PDF_FONT",
        "ERRORBOOK_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_from_env_validates_and_creates_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_errorbook_environment(monkeypatch)
    data_dir = tmp_path / "configured-data"
    monkeypatch.setenv("ERRORBOOK_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ERRORBOOK_TIMEZONE", "UTC")
    monkeypatch.setenv("ERRORBOOK_DESIRED_RETENTION", "0.95")
    monkeypatch.setenv("ERRORBOOK_LOG_LEVEL", "warning")

    settings = Settings.from_env()

    assert settings.data_dir == data_dir.resolve()
    assert settings.timezone.key == "UTC"
    assert settings.desired_retention == 0.95
    assert settings.log_level == "WARNING"
    assert settings.exports_dir.is_dir()
    assert settings.temp_dir.is_dir()


def test_settings_defaults_are_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_errorbook_environment(monkeypatch)
    monkeypatch.setenv("ERRORBOOK_DATA_DIR", str(tmp_path / "default-data"))
    monkeypatch.setenv("ERRORBOOK_BROWSER_PATH", str(tmp_path / "missing-browser"))

    settings = Settings.from_env()

    assert settings.desired_retention == 0.90
    assert settings.timezone.key == "Asia/Tokyo"
    assert settings.log_level == "INFO"
    assert settings.browser_path is None


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("ERRORBOOK_DESIRED_RETENTION", "not-a-number", "must be a number"),
        ("ERRORBOOK_DESIRED_RETENTION", "0.50", "must be between"),
        ("ERRORBOOK_TIMEZONE", "Mars/Olympus", "Unknown ERRORBOOK_TIMEZONE"),
        ("ERRORBOOK_LOG_LEVEL", "TRACE", "ERRORBOOK_LOG_LEVEL"),
    ],
)
def test_invalid_settings_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear_errorbook_environment(monkeypatch)
    monkeypatch.setenv("ERRORBOOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        Settings.from_env()


def test_configured_browser_must_be_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"test")
    monkeypatch.setenv("ERRORBOOK_BROWSER_PATH", str(browser))
    assert _find_browser() == browser.resolve()

    monkeypatch.setenv("ERRORBOOK_BROWSER_PATH", str(tmp_path / "missing.exe"))
    assert _find_browser() is None
