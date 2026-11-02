from __future__ import annotations

from pathlib import Path

import pytest

from dbzscc.app.miro_service import MiroApplicationService


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
