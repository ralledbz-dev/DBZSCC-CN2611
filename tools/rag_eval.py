from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "rag_eval" / "evaluation.json"
TARGETS_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "rag_eval" / "targets.json"
DEFAULT_OUTPUT_ROOT = REPOSITORY_ROOT / ".work" / "rag-evaluation"
EXPECTED_GROUPS = ("de", "en", "code")
RESULT_LIMIT = 5

if str(REPOSITORY_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "src"))


def load_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and validate the exact fixture snapshot and its pre-measurement targets."""
    fixture_bytes = FIXTURE_PATH.read_bytes()
    fixture = json.loads(fixture_bytes)
    targets = json.loads(TARGETS_PATH.read_text(encoding="utf-8"))
    actual_hash = hashlib.sha256(fixture_bytes).hexdigest()
    if targets.get("frozen_before_real_worker_measurement") is not True:
        raise ValueError("Evaluation targets are not marked frozen before measurement.")
    if targets.get("evaluation_fixture_sha256") != actual_hash:
        raise ValueError("Evaluation fixture differs from its frozen SHA-256 fingerprint.")
    if fixture.get("suite_id") != targets.get("suite_id"):
        raise ValueError("Fixture and targets have different suite identifiers.")

    documents = fixture.get("documents")
    queries = fixture.get("queries")
    if not isinstance(documents, list) or not isinstance(queries, list):
        raise TypeError("Fixture documents and queries must be lists.")
    if len(documents) != 24 or len(queries) != 18:
        raise ValueError("Fixture size differs from the frozen evaluation protocol.")

    paths: set[str] = set()
    document_roles: Counter[str] = Counter()
    for document in documents:
        if not isinstance(document, dict):
            raise TypeError("Each fixture document must be an object.")
        relative = _validate_relative_path(document.get("path"))
        if relative in paths:
            raise ValueError("Fixture document paths must be unique.")
        paths.add(relative)
        if document.get("language") not in EXPECTED_GROUPS:
            raise ValueError("Fixture document has an unsupported language group.")
        if document.get("role") not in {"relevant", "distractor"}:
            raise ValueError("Fixture document has an unsupported role.")
        if not isinstance(document.get("content"), str) or not document["content"].strip():
            raise ValueError("Fixture document content must be non-empty UTF-8 text.")
        document_roles[document["role"]] += 1
    if document_roles != Counter({"relevant": 18, "distractor": 6}):
        raise ValueError("Fixture relevant/distractor counts differ from the frozen protocol.")

    query_ids: set[str] = set()
    query_groups: Counter[str] = Counter()
    for query in queries:
        if not isinstance(query, dict):
            raise TypeError("Each fixture query must be an object.")
        query_id = query.get("id")
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ValueError("Fixture query IDs must be unique non-empty strings.")
        query_ids.add(query_id)
        group = query.get("language")
        if group not in EXPECTED_GROUPS:
            raise ValueError("Fixture query has an unsupported language group.")
        query_groups[group] += 1
        if not isinstance(query.get("query"), str) or not query["query"].strip():
            raise ValueError("Fixture query text must be non-empty.")
        relevant = query.get("relevant_paths")
        if not isinstance(relevant, list) or len(relevant) != 1:
            raise ValueError("Each frozen query must have exactly one relevant source path.")
        if any(_validate_relative_path(path) not in paths for path in relevant):
            raise ValueError("A query refers to a source path absent from the fixture.")
    if query_groups != Counter({"de": 6, "en": 6, "code": 6}):
        raise ValueError("Fixture query groups are not balanced at six each.")

    protocol = targets.get("protocol", {})
    if protocol.get("worker_result_limit") != RESULT_LIMIT:
        raise ValueError("Frozen result limit must match the app worker's five-result bound.")
    if protocol.get("search_trials_per_query") != 1:
        raise ValueError("This runner implements one fixed worker search per query.")
    return fixture, targets


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Fixture paths must be canonical relative POSIX paths.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)
    ):
        raise ValueError("Fixture paths cannot escape or hide content in the synthetic workspace.")
    return path.as_posix()


def percentile(values: list[float], p: float) -> float:
    """Return a linearly interpolated percentile at sorted position (n - 1) * p."""
    if not values:
        raise ValueError("A percentile requires at least one observation.")
    if not 0.0 <= p <= 1.0:
        raise ValueError("Percentile p must be between zero and one.")
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def score_rankings(
    queries: list[dict[str, Any]], rankings: dict[str, list[str]]
) -> dict[str, Any]:
    """Compute binary, macro-averaged retrieval metrics using unique source paths."""
    per_query: list[dict[str, Any]] = []
    for query in queries:
        query_id = query["id"]
        relevant = set(query["relevant_paths"])
        unique_ranked = list(dict.fromkeys(rankings.get(query_id, [])))
        first_relevant_rank = next(
            (rank for rank, path in enumerate(unique_ranked, start=1) if path in relevant),
            None,
        )
        values: dict[str, float] = {}
        for k in (1, 3, 5):
            values[f"recall_at_{k}"] = (
                len(relevant.intersection(unique_ranked[:k])) / len(relevant)
            )
        values["mrr_at_5"] = 1.0 / first_relevant_rank if first_relevant_rank else 0.0
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, path in enumerate(unique_ranked[:5], start=1)
            if path in relevant
        )
        ideal_hits = min(len(relevant), 5)
        ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1))
        values["ndcg_at_5"] = dcg / ideal_dcg if ideal_dcg else 0.0
        per_query.append(
            {
                "query_id": query_id,
                "language": query["language"],
                "first_relevant_rank": first_relevant_rank,
                **values,
            }
        )

    overall = _macro_average(per_query)
    by_group = {
        group: _macro_average([item for item in per_query if item["language"] == group])
        for group in EXPECTED_GROUPS
    }
    return {"overall": overall, "by_group": by_group, "per_query": per_query}


def _macro_average(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}
    keys = ("recall_at_1", "recall_at_3", "recall_at_5", "mrr_at_5", "ndcg_at_5")
    return {key: sum(row[key] for row in rows) / len(rows) for key in keys}


def check_targets(
    metrics: dict[str, Any], targets: dict[str, Any], search_latencies_ms: list[float], index_ms: float
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    retrieval_targets = targets["retrieval_targets"]
    for metric_name, target in retrieval_targets["overall"].items():
        metric = metric_name.removesuffix("_min")
        checks[f"overall.{metric_name}"] = metrics["overall"][metric] >= target
    for group in EXPECTED_GROUPS:
        for metric_name, target in retrieval_targets["per_group"].items():
            metric = metric_name.removesuffix("_min")
            checks[f"{group}.{metric_name}"] = metrics["by_group"][group][metric] >= target

    latency_targets = targets["latency_targets_ms"]
    checks["search.p50_max"] = percentile(search_latencies_ms, 0.50) <= latency_targets[
        "search_worker_roundtrip_p50_max"
    ]
    checks["search.p95_max"] = percentile(search_latencies_ms, 0.95) <= latency_targets[
        "search_worker_roundtrip_p95_max"
    ]
    checks["index.single_call_max"] = index_ms <= latency_targets[
        "index_worker_roundtrip_single_max"
    ]
    return checks


def _materialize_corpus(fixture: dict[str, Any], corpus_root: Path) -> None:
    for document in fixture["documents"]:
        relative = _validate_relative_path(document["path"])
        target = corpus_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(document["content"] + "\n", encoding="utf-8", newline="\n")


def _latency_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(sample["elapsed_ms"]) for sample in samples]
    return {
        "sample_count": len(durations),
        "p50_ms": percentile(durations, 0.50),
        "p95_ms": percentile(durations, 0.95),
        "min_ms": min(durations),
        "max_ms": max(durations),
    }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def run_evaluation(output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, Any]:
    """Run only against the generated synthetic corpus via the fixed app worker."""
    fixture, targets = load_inputs()
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output_root)).resolve()
    corpus_root = run_dir / "synthetic-corpus"
    corpus_root.mkdir()
    _materialize_corpus(fixture, corpus_root)
    database = run_dir / "index.sqlite3"
    result_path = run_dir / "results.json"
    started_at = datetime.now(timezone.utc).isoformat()
    index_ms: float | None = None
    index_summary: dict[str, Any] | None = None
    search_samples: list[dict[str, Any]] = []
    ranking_by_query: dict[str, list[str]] = {}
    outcome = "inconclusive"
    error: dict[str, str] | None = None
    metric_results: dict[str, Any] | None = None
    target_checks: dict[str, bool] | None = None

    try:
        from dbzscc.app.local_rag_service import LocalRagWorkerClient

        client = LocalRagWorkerClient(database)
        started_ns = time.perf_counter_ns()
        indexed = client.index_workspace(corpus_root)
        index_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        index_summary = {
            "model_id": indexed.model_id,
            "execution_provider": indexed.execution_provider,
            "included_file_count": indexed.included_file_count,
            "chunk_count": indexed.chunk_count,
            "excluded_counts": dict(indexed.excluded_counts),
            "unchanged": indexed.unchanged,
            "resource_observation": indexed.resource_observation.to_dict(),
        }
        if indexed.model_id != "intfloat/multilingual-e5-small":
            raise RuntimeError("The app worker did not confirm the pinned E5 model.")
        if indexed.execution_provider != "CPUExecutionProvider":
            raise RuntimeError("The app worker did not confirm CPUExecutionProvider.")
        if indexed.included_file_count != len(fixture["documents"]):
            raise RuntimeError("The synthetic workspace file count differs from the fixture.")

        for query in fixture["queries"]:
            started_ns = time.perf_counter_ns()
            hits = client.search_workspace(corpus_root, query["query"])
            elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            paths = [hit.relative_path for hit in hits]
            if len(paths) > RESULT_LIMIT:
                raise RuntimeError("The app worker exceeded its fixed result limit.")
            search_samples.append(
                {
                    "query_id": query["id"],
                    "language": query["language"],
                    "elapsed_ms": elapsed_ms,
                    "resource_observation": client.last_resource_observation.to_dict(),
                }
            )
            ranking_by_query[query["id"]] = paths

        metric_results = score_rankings(fixture["queries"], ranking_by_query)
        target_checks = check_targets(
            metric_results,
            targets,
            [sample["elapsed_ms"] for sample in search_samples],
            index_ms,
        )
        outcome = "pass" if all(target_checks.values()) else "fail"
    except Exception as exc:  # noqa: BLE001 -- report an explicit inconclusive run, never fake data.
        error = {"type": type(exc).__name__, "message": str(exc)[:500]}

    result: dict[str, Any] = {
        "suite_id": fixture["suite_id"],
        "started_at_utc": started_at,
        "outcome": outcome,
        "fixture_sha256": targets["evaluation_fixture_sha256"],
        "targets_sha256": hashlib.sha256(TARGETS_PATH.read_bytes()).hexdigest(),
        "protocol": targets["protocol"],
        "target_values": {
            "retrieval": targets["retrieval_targets"],
            "latency_ms": targets["latency_targets_ms"],
        },
        "environment": {
            "platform": platform.platform(),
            "processor": platform.processor() or "unknown",
            "logical_cpu_count": os.cpu_count(),
            "runner_python": sys.version.split()[0],
            "worker_interpreter": str(REPOSITORY_ROOT / ".work" / "venv314" / "Scripts" / "python.exe"),
        },
        "corpus": {
            "document_count": len(fixture["documents"]),
            "query_count": len(fixture["queries"]),
            "query_groups": dict(Counter(query["language"] for query in fixture["queries"])),
            "materialized_at": "run_dir/synthetic-corpus",
        },
        "worker_index": index_summary,
        "latency": {
            "measurement": "complete LocalRagWorkerClient call; one fixed worker process per operation",
            "index_roundtrip_ms": index_ms,
            "search_roundtrip": _latency_summary(search_samples) if search_samples else None,
            "search_by_group": {
                group: _latency_summary([item for item in search_samples if item["language"] == group])
                for group in EXPECTED_GROUPS
                if any(item["language"] == group for item in search_samples)
            },
            "search_samples": search_samples,
        },
        "retrieval_metrics": metric_results,
        "returned_paths_by_query": ranking_by_query,
        "target_checks": target_checks,
        "interpretation_limits": targets["interpretation_limits"],
        "error": error,
    }
    _write_result(result_path, result)
    result["result_file"] = str(result_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Local directory for retained synthetic corpus, SQLite index and JSON result.",
    )
    args = parser.parse_args()
    output_root = args.output_root.expanduser()
    if not output_root.is_absolute():
        output_root = (REPOSITORY_ROOT / output_root).resolve()
    result = run_evaluation(output_root)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["outcome"] == "pass" else (2 if result["outcome"] == "fail" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
