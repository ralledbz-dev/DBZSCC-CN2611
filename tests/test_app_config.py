from __future__ import annotations

from pathlib import Path

import pytest

from dbzscc.app.config import DEFAULT_MODEL_ENDPOINT, AppSettings, application_data_directory


def test_local_endpoint_is_the_default_and_trailing_slashes_are_normalized() -> None:
    settings = AppSettings()
    assert settings.model_endpoint == DEFAULT_MODEL_ENDPOINT
    assert settings.validated_endpoint() == DEFAULT_MODEL_ENDPOINT
    assert AppSettings(model_endpoint="http://localhost:8080/v1/").validated_endpoint() == (
        "http://localhost:8080/v1"
    )


def test_application_data_path_uses_user_profile_not_checkout(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert application_data_directory() == tmp_path / "Local" / "DBZSCC2611"


def test_rejects_invalid_endpoint_syntax() -> None:
    with pytest.raises(ValueError):
        AppSettings(model_endpoint="http://localhost:99999/v1").validated_endpoint()
