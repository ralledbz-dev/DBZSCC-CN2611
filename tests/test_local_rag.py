from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import types

import numpy as np
import pytest

from dbzscc.rag import (
    EMBEDDING_DIMENSION,
    MODEL_ID,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    E5OnnxEmbedder,
    EmbeddingFingerprintMismatch,
    EmbeddingSetupError,
    LocalE5Manifest,
    LocalRagIndex,
    NoActiveIndexError,
    WorkspaceLimitError,
    WorkspaceScanError,
    chunk_document,
    scan_workspace,
)


class DeterministicTestEmbedder:
    """Test-only vector source; never configured or exported as a production fallback."""

    model_id = MODEL_ID
    dimension = EMBEDDING_DIMENSION
    execution_provider = "CPUExecutionProvider"
    query_prefix = QUERY_PREFIX
    passage_prefix = PASSAGE_PREFIX

    def __init__(self, identity: str = "unit-test-e5-contract") -> None:
        self.fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        self.fail_on_passage: str | None = None

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self._vector(text)

    def embed_passage(self, text: str) -> tuple[float, ...]:
        if self.fail_on_passage and self.fail_on_passage in text:
            raise RuntimeError("test-only injected embedding failure")
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> tuple[float, ...]:
        vector = [0.0] * EMBEDDING_DIMENSION
        for token in re.findall(r"\w+", text.casefold(), re.UNICODE):
            slot = int(hashlib.sha256(token.encode("utf-8")).hexdigest()[:8], 16)
            vector[slot % EMBEDDING_DIMENSION] += 1.0
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm == 0.0:
            vector[0] = 1.0
            norm = 1.0
        return tuple(value / norm for value in vector)


def _manifest(directory, *, onnx_file: str = "e5.onnx"):
    directory.mkdir(parents=True, exist_ok=True)
    model = directory / onnx_file
    tokenizer = directory / "tokenizer.json"
    model.write_bytes(b"local-test-onnx-placeholder")
    tokenizer.write_text('{"test":true}', encoding="utf-8")
    payload = {
        "schema_version": 1,
        "model_id": MODEL_ID,
        "revision": "a" * 40,
        "license": "MIT",
        "onnx_file": onnx_file,
        "onnx_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "tokenizer_file": "tokenizer.json",
        "tokenizer_sha256": hashlib.sha256(tokenizer.read_bytes()).hexdigest(),
        "embedding_dimension": 384,
        "max_tokens": 512,
        "pooling": "masked_mean",
        "normalization": "l2",
        "query_prefix": "query: ",
        "passage_prefix": "passage: ",
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path, model, tokenizer, payload


def test_workspace_allowlist_excludes_hidden_secrets_and_build_outputs(tmp_path) -> None:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules" / "lib").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def retrieve(query):\n    return query\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Local\n\nFTS5 and RRF stay offline.\n", encoding="utf-8")
    (root / ".private.md").write_text("hidden content must not be indexed", encoding="utf-8")
    (root / "credentials.json").write_text('{"token":"private"}', encoding="utf-8")
    (root / "settings.py").write_text(
        'api_key = "test-only-not-a-real-secret"\n', encoding="utf-8"
    )
    (root / ".git" / "config.py").write_text("git internals", encoding="utf-8")
    (root / "node_modules" / "lib" / "dependency.py").write_text(
        "vendor code", encoding="utf-8"
    )
    (root / "image.png").write_bytes(b"not text")

    report = scan_workspace(root, "project-a")
    paths = {chunk.relative_path for chunk in report.chunks}
    assert paths == {"README.md", "src/app.py"}
    joined_text = "\n".join(chunk.text for chunk in report.chunks)
    assert "super-secret-token-value" not in joined_text
    assert "credentials.json" not in joined_text
    assert report.included_file_count == 2
    reasons = dict(report.excluded_counts)
    assert reasons["hidden_path"] >= 2
    assert reasons["excluded_directory"] >= 1
    assert reasons["sensitive_filename"] == 1
    assert reasons["sensitive_content"] == 1
    assert reasons["not_allowlisted"] == 1
    assert "settings.py" not in repr(report)


def test_scanner_uses_lstat_identity_when_direntry_ids_are_zero(tmp_path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "guide.md").write_text("# Stable\nLocal FTS5 scan.\n", encoding="utf-8")
    real_scandir = os.scandir

    class ZeroIdentityEntry:
        def __init__(self, entry) -> None:
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks=True):
            observed = self._entry.stat(follow_symlinks=follow_symlinks)
            return types.SimpleNamespace(
                st_dev=0,
                st_ino=0,
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime_ns=observed.st_mtime_ns,
                st_file_attributes=getattr(observed, "st_file_attributes", 0),
            )

    class ScandirContext:
        def __init__(self, path) -> None:
            self.path = path
            self.iterator = None

        def __enter__(self):
            self.iterator = real_scandir(self.path)
            return (ZeroIdentityEntry(entry) for entry in self.iterator)

        def __exit__(self, *_args):
            if self.iterator is not None:
                self.iterator.close()
            return False

    monkeypatch.setattr(os, "scandir", lambda path: ScandirContext(path))
    report = scan_workspace(root, "project-a")
    assert report.included_file_count == 1
    assert {chunk.relative_path for chunk in report.chunks} == {"guide.md"}


def test_scanner_requires_explicit_absolute_root_and_fails_closed_on_limits(tmp_path) -> None:
    with pytest.raises(WorkspaceScanError, match="absolute"):
        scan_workspace("relative-folder", "project-a")

    root = tmp_path / "oversized"
    root.mkdir()
    (root / "large.md").write_text("x" * (256 * 1024 + 1), encoding="utf-8")
    with pytest.raises(WorkspaceLimitError, match="per-file"):
        scan_workspace(root, "project-a")


def test_python_ast_markdown_headings_and_explicit_fallback_metadata() -> None:
    python_chunks = chunk_document(
        "# module\nimport ast\n\n@decorator\ndef first():\n    return 'one'\n\n"
        "class Container:\n    def method(self):\n        return 'two'\n",
        project_id="project-a",
        relative_path="src/sample.py",
        suffix=".py",
    )
    assert any("first [python_ast]" in chunk.symbol for chunk in python_chunks)
    assert any("Container [python_ast]" in chunk.symbol for chunk in python_chunks)
    assert all(chunk.line_start <= chunk.line_end for chunk in python_chunks)

    markdown_chunks = chunk_document(
        "# Guide\nIntro\n## Details\nLocal FTS5 retrieval.\n",
        project_id="project-a",
        relative_path="guide.md",
        suffix=".md",
    )
    assert any("Guide > Details [markdown_heading]" in chunk.symbol for chunk in markdown_chunks)

    fallback_chunks = chunk_document(
        "export function run() { return 1; }\n",
        project_id="project-a",
        relative_path="src/app.ts",
        suffix=".ts",
    )
    assert fallback_chunks[0].symbol == "text [line_fallback]"
    with pytest.raises(ValueError, match="relative"):
        chunk_document(
            "unsafe",
            project_id="project-a",
            relative_path="../outside.md",
            suffix=".md",
        )


def test_e5_manifest_pins_local_artifacts_and_rejects_hash_mismatch(tmp_path) -> None:
    manifest_path, model, _tokenizer, _payload = _manifest(tmp_path / "e5")
    manifest = LocalE5Manifest.load(manifest_path)
    assert manifest.model_id == MODEL_ID
    assert manifest.embedding_dimension == 384
    assert len(manifest.artifact_fingerprint()) == 64

    model.write_bytes(b"tampered local model")
    with pytest.raises(EmbeddingSetupError, match="hash"):
        LocalE5Manifest.load(manifest_path)


def test_e5_manifest_rejects_wrong_prefix_and_path_traversal(tmp_path) -> None:
    manifest_path, _model, _tokenizer, payload = _manifest(tmp_path / "e5")
    payload["passage_prefix"] = "document: "
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EmbeddingSetupError, match="prefix"):
        LocalE5Manifest.load(manifest_path)

    payload["passage_prefix"] = "passage: "
    payload["onnx_file"] = "../outside.onnx"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(EmbeddingSetupError, match="filename"):
        LocalE5Manifest.load(manifest_path)


def test_e5_adapter_uses_exact_prefix_masked_mean_l2_and_cpu_only(tmp_path, monkeypatch) -> None:
    manifest_path, _model, _tokenizer, _payload = _manifest(tmp_path / "e5")
    observations: dict[str, object] = {}

    class FakeOptions:
        def __init__(self) -> None:
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None

    class FakeInput:
        def __init__(self, name: str) -> None:
            self.name = name
            self.type = "tensor(int64)"

    class FakeSession:
        def __init__(self, _model_path, *, sess_options, providers) -> None:
            observations["providers_arg"] = providers
            observations["threads"] = (
                sess_options.intra_op_num_threads,
                sess_options.inter_op_num_threads,
            )
            self.providers = providers

        def get_providers(self):
            return self.providers

        def get_inputs(self):
            return [FakeInput("input_ids"), FakeInput("attention_mask")]

        def run(self, _outputs, feeds):
            observations["feeds"] = feeds
            length = feeds["input_ids"].shape[1]
            hidden = np.zeros((1, length, EMBEDDING_DIMENSION), dtype=np.float32)
            hidden[0, 0, 0] = 2.0
            hidden[0, 1, 1] = 4.0
            hidden[0, 2, 0] = 6.0
            return [hidden]

    class FakeTokenizer:
        def encode(self, text, *, add_special_tokens):
            observations.setdefault("texts", []).append(text)
            observations["special_tokens"] = add_special_tokens
            return types.SimpleNamespace(ids=list(range(513)))

        @classmethod
        def from_file(cls, _path):
            return cls()

    fake_ort = types.ModuleType("onnxruntime")
    fake_ort.__version__ = "test-runtime"
    fake_ort.get_available_providers = lambda: ["CPUExecutionProvider", "CUDAExecutionProvider"]
    fake_ort.SessionOptions = FakeOptions
    fake_ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL="sequential")
    fake_ort.InferenceSession = FakeSession
    fake_tokenizers = types.ModuleType("tokenizers")
    fake_tokenizers.__version__ = "test-tokenizers"
    fake_tokenizers.Tokenizer = FakeTokenizer
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "tokenizers", fake_tokenizers)

    embedder = E5OnnxEmbedder(manifest_path, intra_op_num_threads=2)
    query = embedder.embed_query("local retrieval")
    passage = embedder.embed_passage("workspace chunk")

    assert observations["providers_arg"] == ["CPUExecutionProvider"]
    assert observations["threads"] == (2, 1)
    assert observations["texts"] == ["query: local retrieval", "passage: workspace chunk"]
    assert observations["special_tokens"] is True
    assert observations["feeds"]["input_ids"].shape == (1, 512)
    assert observations["feeds"]["attention_mask"].sum() == 512
    assert len(query) == len(passage) == 384
    assert query[0] == pytest.approx(2 / math.sqrt(5), abs=1e-6)
    assert query[1] == pytest.approx(1 / math.sqrt(5), abs=1e-6)
    assert math.sqrt(math.fsum(value * value for value in passage)) == pytest.approx(1.0)


def test_index_uses_project_scope_fts5_rrf_and_source_metadata(tmp_path) -> None:
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "guide.md").write_text(
        "# Hybrid retrieval\nAlpha uses reciprocal rank fusion and local FTS5.\n",
        encoding="utf-8",
    )
    (root_b / "private.md").write_text(
        "# Other project\nAlpha must not cross the project boundary.\n", encoding="utf-8"
    )

    database = tmp_path / "rag.sqlite"
    with LocalRagIndex(database, DeterministicTestEmbedder()) as index:
        first = index.rebuild_workspace(root_a, "project-a")
        second = index.rebuild_workspace(root_b, "project-b")
        assert first.chunk_count >= 1
        assert second.project_id == "project-b"

        results = index.search(
            project_id="project-a", query="alpha reciprocal rank fusion", limit=5
        )
        assert results
        assert all(result.project_id == "project-a" for result in results)
        assert all(result.relative_path == "guide.md" for result in results)
        assert any(result.lexical_rank is not None for result in results)
        assert any(result.dense_rank is not None for result in results)
        for result in results:
            expected = sum(
                1.0 / (60 + rank)
                for rank in (result.lexical_rank, result.dense_rank)
                if rank is not None
            )
            assert result.rrf_score == pytest.approx(expected)
            assert len(result.content_sha256) == 64

        second_project_results = index.search(
            project_id="project-b", query="alpha local boundary", limit=5
        )
        assert second_project_results
        assert all("private.md" == result.relative_path for result in second_project_results)
        assert all("FTS5" not in result.text for result in second_project_results)


def test_search_merges_dense_candidates_as_chunk_ids(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "alpha.md").write_text("Alpha retrieval uses local FTS5.", encoding="utf-8")
    (root / "beta.md").write_text("Beta retrieval uses dense E5.", encoding="utf-8")
    with LocalRagIndex(tmp_path / "rag.sqlite", DeterministicTestEmbedder()) as index:
        index.rebuild_workspace(root, "project-a")
        results = index.search(project_id="project-a", query="alpha beta retrieval", limit=4)
    assert {result.relative_path for result in results} == {"alpha.md", "beta.md"}
    assert all(isinstance(result.chunk_id, str) for result in results)
    assert all(result.dense_rank is not None for result in results)


def test_failed_rebuild_keeps_previous_generation_active(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    source = root / "notes.md"
    source.write_text("# Existing\nalpha local index remains available.\n", encoding="utf-8")
    embedder = DeterministicTestEmbedder()
    with LocalRagIndex(tmp_path / "rag.sqlite", embedder) as index:
        original = index.rebuild_workspace(root, "project-a")
        source.write_text(
            "# Updated\nalpha local index remains available.\n", encoding="utf-8"
        )
        (root / "new.md").write_text("# New\nfail-me before commit.\n", encoding="utf-8")
        embedder.fail_on_passage = "fail-me"
        with pytest.raises(RuntimeError, match="injected"):
            index.rebuild_workspace(root, "project-a")

        active = index.active_generation("project-a")
        assert active is not None
        assert active.generation_id == original.generation_id
        assert active.included_file_count is None
        assert active.included_bytes is None
        assert active.excluded_counts is None
        results = index.search(project_id="project-a", query="alpha local index")
        assert results
        assert results[0].generation_id == original.generation_id
        assert "Existing" in results[0].text


def test_index_reuses_matching_generation_and_blocks_model_fingerprint_drift(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "readme.md").write_text("# Local\nDense FTS fusion.\n", encoding="utf-8")
    database = tmp_path / "rag.sqlite"
    first_embedder = DeterministicTestEmbedder("model-revision-a")
    with LocalRagIndex(database, first_embedder) as index:
        first = index.rebuild_workspace(root, "project-a")
        second = index.rebuild_workspace(root, "project-a")
        assert second.unchanged is True
        assert second.generation_id == first.generation_id

    with LocalRagIndex(
        database, DeterministicTestEmbedder("model-revision-b")
    ) as changed, pytest.raises(EmbeddingFingerprintMismatch):
        changed.search(project_id="project-a", query="local dense fusion")


def test_search_without_index_and_fts_query_operator_injection_are_safe(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "guide.md").write_text("# Safe\nalpha beta retrieval.\n", encoding="utf-8")
    with LocalRagIndex(tmp_path / "rag.sqlite", DeterministicTestEmbedder()) as index:
        with pytest.raises(NoActiveIndexError):
            index.search(project_id="project-a", query="alpha")
        index.rebuild_workspace(root, "project-a")
        results = index.search(project_id="project-a", query='alpha OR * : " beta')
        assert all(result.project_id == "project-a" for result in results)


def test_database_requires_explicit_local_absolute_path(tmp_path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        LocalRagIndex("relative.sqlite", DeterministicTestEmbedder())
    assert tmp_path.is_absolute()
