"""Small, explicit application configuration with local-first defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_MODEL_ENDPOINT = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL_ROOT = r"D:\Models"


@dataclass(frozen=True)
class AppSettings:
    """Non-secret preferences persisted by the UI through QSettings."""

    model_endpoint: str = DEFAULT_MODEL_ENDPOINT
    model_name: str = ""
    model_roots: tuple[str, ...] = ()
    runtime_roots: tuple[str, ...] = ()
    project_root: str = ""

    def validated_endpoint(self) -> str:
        """Return a normalized HTTP(S) endpoint or raise a safe ValueError."""
        raw = self.model_endpoint.strip().rstrip("/")
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Die Runtime-Adresse ist ungültig.") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Bitte eine HTTP(S)-Adresse ohne Zugangsdaten, Query oder Fragment angeben."
            )
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Der Runtime-Port liegt außerhalb des gültigen Bereichs.")
        return raw


def application_data_directory() -> Path:
    """Return a per-user data directory, never a path inside the checkout."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "DBZSCC2611"
    return Path.home() / ".local" / "share" / "DBZSCC2611"
