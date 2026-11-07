from __future__ import annotations

import pytest

from dbzscc.app.local_rag_service import (
    LocalRagResourceObservation,
    LocalRagWorkerError,
    _parse_resource_observation,
)
from dbzscc.rag import workspace
from dbzscc.rag.workload_bounds import (
    MIB,
    RAG_MEASURED_MAX_PEAK_COMMIT_BYTES,
    RAG_MEMORY_RESERVE_FACTOR_DENOMINATOR,
    RAG_MEMORY_RESERVE_FACTOR_NUMERATOR,
    RAG_RESERVED_HOST_MEMORY_MIB,
    RAG_TESTED_MAX_CHUNK_CHARS,
    RAG_TESTED_WORKSPACE_FILES,
    RAG_WORKER_CPU_THREADS,
)
from dbzscc.rag.workspace import scan_workspace


def test_workload_bounds_import_and_memory_reserve_rounds_up() -> None:
    denominator = MIB * RAG_MEMORY_RESERVE_FACTOR_DENOMINATOR
    expected = (
        RAG_MEASURED_MAX_PEAK_COMMIT_BYTES * RAG_MEMORY_RESERVE_FACTOR_NUMERATOR
        + denominator
        - 1
    ) // denominator

    assert RAG_WORKER_CPU_THREADS == 2
    assert RAG_RESERVED_HOST_MEMORY_MIB == expected == 2_083
    assert RAG_TESTED_MAX_CHUNK_CHARS == workspace.DEFAULT_MAX_CHUNK_CHARS


def test_evaluation_file_count_is_not_a_workspace_scan_limit(tmp_path) -> None:
    root = tmp_path / "synthetic-workspace"
    root.mkdir()
    file_count = RAG_TESTED_WORKSPACE_FILES + 1
    for ordinal in range(file_count):
        (root / f"document-{ordinal:02}.md").write_text(
            f"# Document {ordinal}\nSmall synthetic content.\n", encoding="utf-8"
        )

    report = scan_workspace(root, "rag-bounds-test")

    assert report.included_file_count == file_count
    assert workspace.MAX_WORKSPACE_FILES > RAG_TESTED_WORKSPACE_FILES
    assert workspace.MAX_WORKSPACE_BYTES == 20 * MIB
    assert workspace.MAX_WORKSPACE_CHUNKS == 25_000


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_chunk_chars": 255}, "Chunk size"),
        ({"max_chunk_chars": 256, "overlap_chars": 128}, "overlap"),
    ],
)
def test_workspace_rejects_invalid_chunk_bounds(tmp_path, options, message: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()

    with pytest.raises(ValueError, match=message):
        scan_workspace(root, "rag-bounds-test", **options)


def test_resource_observation_parser_keeps_measured_and_unknown_distinct() -> None:
    measured = _parse_resource_observation(
        {
            "status": "measured",
            "cpu_time_seconds": 1.25,
            "peak_working_set_bytes": 1_024,
            "peak_commit_bytes": 2_048,
            "source": "windows_process_counters",
        }
    )
    unknown = _parse_resource_observation(
        {
            "status": "unknown",
            "cpu_time_seconds": None,
            "peak_working_set_bytes": None,
            "peak_commit_bytes": None,
            "source": "unavailable",
        }
    )

    assert measured == LocalRagResourceObservation(
        status="measured",
        cpu_time_seconds=1.25,
        peak_working_set_bytes=1_024,
        peak_commit_bytes=2_048,
        source="windows_process_counters",
    )
    assert unknown == LocalRagResourceObservation(
        status="unknown",
        cpu_time_seconds=None,
        peak_working_set_bytes=None,
        peak_commit_bytes=None,
        source="unavailable",
    )
    assert "unbekannt" in unknown.status_text()


def test_resource_observation_rejects_unknown_values_filled_with_zero() -> None:
    with pytest.raises(LocalRagWorkerError, match="Ressourcendaten"):
        _parse_resource_observation(
            {
                "status": "unknown",
                "cpu_time_seconds": 0,
                "peak_working_set_bytes": None,
                "peak_commit_bytes": None,
                "source": "unavailable",
            }
        )
