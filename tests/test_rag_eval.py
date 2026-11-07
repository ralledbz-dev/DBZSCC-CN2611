from __future__ import annotations

import math

import pytest

from tools.rag_eval import (
    _validate_relative_path,
    load_inputs,
    percentile,
    score_rankings,
)


def test_frozen_fixture_is_balanced_and_ground_truth_paths_are_relevant() -> None:
    fixture, targets = load_inputs()
    assert targets["frozen_before_real_worker_measurement"] is True
    assert len(fixture["documents"]) == 24
    assert len(fixture["queries"]) == 18

    documents = {item["path"]: item for item in fixture["documents"]}
    for language in ("de", "en", "code"):
        assert sum(item["language"] == language for item in fixture["queries"]) == 6
        assert sum(item["language"] == language for item in fixture["documents"]) == 8
    for query in fixture["queries"]:
        relevant = documents[query["relevant_paths"][0]]
        assert relevant["role"] == "relevant"
        assert relevant["language"] == query["language"]


def test_relative_fixture_paths_reject_absolute_hidden_and_parent_paths() -> None:
    assert _validate_relative_path("docs/de/guide.md") == "docs/de/guide.md"
    for unsafe in ("../outside.md", ".hidden.md", "docs/.secret.md", "C:/outside.md", "/root.md"):
        with pytest.raises(ValueError):
            _validate_relative_path(unsafe)


def test_percentile_uses_documented_linear_interpolation() -> None:
    assert percentile([40.0, 10.0, 30.0, 20.0], 0.0) == 10.0
    assert percentile([40.0, 10.0, 30.0, 20.0], 0.5) == 25.0
    assert percentile([40.0, 10.0, 30.0, 20.0], 0.95) == pytest.approx(38.5)
    assert percentile([40.0, 10.0, 30.0, 20.0], 1.0) == 40.0
    with pytest.raises(ValueError, match="at least one"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="between zero and one"):
        percentile([1.0], 1.1)


def test_retrieval_metrics_are_binary_macro_averages_and_deduplicate_paths() -> None:
    queries = [
        {"id": "q1", "language": "de", "relevant_paths": ["target.md"]},
        {"id": "q2", "language": "en", "relevant_paths": ["other.md"]},
    ]
    metrics = score_rankings(
        queries,
        {
            "q1": ["noise.md", "target.md", "target.md"],
            "q2": ["other.md"],
        },
    )
    assert metrics["overall"]["recall_at_1"] == 0.5
    assert metrics["overall"]["recall_at_3"] == 1.0
    assert metrics["overall"]["recall_at_5"] == 1.0
    assert metrics["overall"]["mrr_at_5"] == 0.75
    assert metrics["overall"]["ndcg_at_5"] == pytest.approx((1.0 / math.log2(3) + 1.0) / 2)
    assert metrics["by_group"]["de"]["mrr_at_5"] == 0.5
    assert metrics["by_group"]["en"]["mrr_at_5"] == 1.0
    assert metrics["by_group"]["code"] == {}
    assert metrics["per_query"][0]["first_relevant_rank"] == 2
