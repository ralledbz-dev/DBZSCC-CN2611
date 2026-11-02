"""Application boundary for explicitly requested local MIRO discovery."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from dbzscc.miro.discovery_pipeline import LocalDiscoveryResult, run_local_discovery
from dbzscc.miro.local_inventory import RootInput

from .config import application_data_directory


class MiroApplicationService:
    """Use the existing MIRO scanner, parser and registry without duplication."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path else (
            application_data_directory() / "miro-models.sqlite"
        )
        self.database_path = self.database_path.expanduser().absolute()

    def discover(
        self,
        *,
        model_roots: Iterable[RootInput],
        runtime_roots: Iterable[RootInput],
    ) -> LocalDiscoveryResult:
        models = tuple(model_roots)
        runtimes = tuple(runtime_roots)
        if not models and not runtimes:
            raise ValueError("Wähle zuerst mindestens einen Modell- oder Runtime-Ordner.")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        return run_local_discovery(
            database_path=self.database_path,
            model_roots=models,
            runtime_roots=runtimes,
        )
