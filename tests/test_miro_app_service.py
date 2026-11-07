from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from dbzscc.app.miro_service import (
    MiroApplicationService,
    MiroScanAlreadyRunningError,
    _scan_lock_for,
)


def test_discovery_scans_only_explicit_roots_and_reuses_miro_pipeline(tmp_path: Path) -> None:
    selected = tmp_path / "selected-models"
    outside = tmp_path / "not-selected"
    selected.mkdir()
    outside.mkdir()
    (selected / "candidate.gguf").write_bytes(b"not a GGUF file")
    (outside / "must-not-appear.gguf").write_bytes(b"not a GGUF file")
    database = tmp_path / "app-data" / "miro.sqlite"
    service = MiroApplicationService(database)

    result = service.discover(model_roots=(selected,), runtime_roots=())

    assert [item.candidate.name for item in result.artifacts] == ["candidate.gguf"]
    assert result.artifacts[0].registry_record is not None
    assert result.artifacts[0].registry_record.status == "metadata_parse_failed"
    assert result.artifacts[0].capability_profile is None
    assert database.is_file()


def test_discovery_requires_at_least_one_explicit_root(tmp_path: Path) -> None:
    database = tmp_path / "app-data" / "miro.sqlite"
    service = MiroApplicationService(database)

    with pytest.raises(ValueError, match="mindestens einen"):
        service.discover(model_roots=(), runtime_roots=())
    assert not database.exists()


def test_catalog_view_only_returns_artifacts_seen_in_its_current_run(tmp_path: Path) -> None:
    first_root = tmp_path / "first-root"
    current_root = tmp_path / "current-root"
    first_root.mkdir()
    current_root.mkdir()
    (first_root / "old.gguf").write_bytes(b"old candidate")
    (current_root / "current.gguf").write_bytes(b"current candidate")
    service = MiroApplicationService(tmp_path / "app-data" / "miro.sqlite")
    service.discover(model_roots=(first_root,))

    _discovery, current_catalog = service.discover_model_catalog(current_root)

    assert [Path(item.artifact.path).name for item in current_catalog] == ["current.gguf"]


def test_parallel_discovery_for_the_same_registry_is_rejected(tmp_path: Path) -> None:
    service = MiroApplicationService(tmp_path / "app-data" / "miro.sqlite")
    lock = _scan_lock_for(service.database_path)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(MiroScanAlreadyRunningError, match="läuft bereits"):
            service.discover(model_roots=(tmp_path / "selected-root",))
    finally:
        lock.release()


def test_cancelled_discovery_persists_a_resumable_checkpoint(tmp_path: Path) -> None:
    root = tmp_path / "models"
    root.mkdir()
    (root / "candidate.gguf").write_bytes(b"not a GGUF file")
    service = MiroApplicationService(tmp_path / "app-data" / "miro.sqlite")
    cancel_event = Event()
    cancel_event.set()

    partial = service.discover(model_roots=(root,), cancel_event=cancel_event)

    assert not partial.complete
    assert partial.continuation_token
    assert any(issue.code == "scan_cancelled" for issue in partial.scanner_issues)

    reopened_service = MiroApplicationService(service.database_path)
    resumed = reopened_service.discover(model_roots=(root,))

    assert resumed.complete
    assert [item.candidate.name for item in resumed.artifacts] == ["candidate.gguf"]



def test_resumed_scan_does_not_report_historical_cancel_as_current_issue(
    tmp_path: Path,
) -> None:
    root = tmp_path / "models"
    root.mkdir()
    (root / "projector.safetensors").write_bytes(b"candidate only")
    service = MiroApplicationService(tmp_path / "app-data" / "miro.sqlite")
    cancel_event = Event()
    cancel_event.set()

    cancelled = service.discover(model_roots=(root,), cancel_event=cancel_event)
    assert any(issue.code == "scan_cancelled" for issue in cancelled.scanner_issues)

    resumed = MiroApplicationService(service.database_path).discover(model_roots=(root,))
    record = MiroApplicationService(service.database_path).get_discovery_run(
        resumed.registry_sync.run_id
    )

    assert resumed.complete
    assert resumed.scanner_issues == ()
    assert not resumed.has_partial_results
    assert record is not None
    assert record.state == "completed"
    assert '"code":"scan_cancelled"' in record.issues_json
