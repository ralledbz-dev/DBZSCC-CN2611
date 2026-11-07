"""Offline-only multilingual-E5-small embeddings through a verified local ONNX bundle."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL_ID = "intfloat/multilingual-e5-small"
EMBEDDING_DIMENSION = 384
MAX_SEQUENCE_TOKENS = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
_EXECUTION_PROVIDER = "CPUExecutionProvider"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_REVISION_RE = re.compile(r"[0-9a-fA-F]{40}\Z")
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "model_id",
        "revision",
        "license",
        "onnx_file",
        "onnx_sha256",
        "tokenizer_file",
        "tokenizer_sha256",
        "embedding_dimension",
        "max_tokens",
        "pooling",
        "normalization",
        "query_prefix",
        "passage_prefix",
    }
)


class EmbeddingSetupError(RuntimeError):
    """Local model artifacts or CPU inference dependencies are unavailable or invalid."""


@dataclass(frozen=True)
class LocalE5Manifest:
    """Hash-pinned local E5 artifact metadata; no Hub or network lookup is performed."""

    manifest_path: Path
    model_path: Path
    tokenizer_path: Path
    model_id: str
    revision: str
    license: str
    onnx_sha256: str
    tokenizer_sha256: str
    embedding_dimension: int
    max_tokens: int
    pooling: str
    normalization: str
    query_prefix: str
    passage_prefix: str

    @classmethod
    def load(cls, manifest_path: str | Path) -> LocalE5Manifest:
        """Read and verify one explicit local manifest and both local artifact hashes."""
        candidate = Path(manifest_path).expanduser()
        try:
            metadata = candidate.lstat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise EmbeddingSetupError("The local E5 manifest is unavailable.") from exc
        if _is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise EmbeddingSetupError("The local E5 manifest must be a regular file.")
        if metadata.st_size > 64 * 1024:
            raise EmbeddingSetupError("The local E5 manifest exceeds its size limit.")
        try:
            manifest_file = candidate.resolve(strict=True)
            manifest_file.relative_to(manifest_file.parent)
            raw_manifest = manifest_file.read_bytes()
            current = manifest_file.lstat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise EmbeddingSetupError("The local E5 manifest could not be read safely.") from exc
        if _is_link_or_reparse_point(current) or not _same_file(metadata, current):
            raise EmbeddingSetupError("The local E5 manifest changed while being read.")
        try:
            document = json.loads(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmbeddingSetupError("The local E5 manifest is not valid UTF-8 JSON.") from exc
        if not isinstance(document, dict) or frozenset(document) != _REQUIRED_MANIFEST_FIELDS:
            raise EmbeddingSetupError("The local E5 manifest has an unsupported schema.")
        _validate_manifest_values(document)

        asset_root = manifest_file.parent.resolve(strict=True)
        model_path = _local_asset(asset_root, document["onnx_file"], ".onnx")
        tokenizer_path = _local_asset(asset_root, document["tokenizer_file"], ".json")
        actual_model_hash = _sha256_file(model_path)
        actual_tokenizer_hash = _sha256_file(tokenizer_path)
        if actual_model_hash.casefold() != document["onnx_sha256"].casefold():
            raise EmbeddingSetupError("The local E5 ONNX hash does not match its manifest.")
        if actual_tokenizer_hash.casefold() != document["tokenizer_sha256"].casefold():
            raise EmbeddingSetupError("The local E5 tokenizer hash does not match its manifest.")

        return cls(
            manifest_path=manifest_file,
            model_path=model_path,
            tokenizer_path=tokenizer_path,
            model_id=document["model_id"],
            revision=document["revision"].casefold(),
            license=document["license"],
            onnx_sha256=actual_model_hash,
            tokenizer_sha256=actual_tokenizer_hash,
            embedding_dimension=document["embedding_dimension"],
            max_tokens=document["max_tokens"],
            pooling=document["pooling"],
            normalization=document["normalization"],
            query_prefix=document["query_prefix"],
            passage_prefix=document["passage_prefix"],
        )

    def artifact_fingerprint(self) -> str:
        """Fingerprint the model/tokenizer contract without storing local absolute paths."""
        payload = {
            "model_id": self.model_id,
            "revision": self.revision,
            "license": self.license,
            "onnx_sha256": self.onnx_sha256,
            "tokenizer_sha256": self.tokenizer_sha256,
            "embedding_dimension": self.embedding_dimension,
            "max_tokens": self.max_tokens,
            "pooling": self.pooling,
            "normalization": self.normalization,
            "query_prefix": self.query_prefix,
            "passage_prefix": self.passage_prefix,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class E5OnnxEmbedder:
    """Local E5 encoder pinned to CPU, a verified ONNX file, and a local tokenizer JSON."""

    model_id = MODEL_ID
    dimension = EMBEDDING_DIMENSION
    execution_provider = _EXECUTION_PROVIDER
    query_prefix = QUERY_PREFIX
    passage_prefix = PASSAGE_PREFIX

    def __init__(
        self,
        manifest_path: str | Path,
        *,
        intra_op_num_threads: int = 1,
    ) -> None:
        if type(intra_op_num_threads) is not int or not 1 <= intra_op_num_threads <= 4:
            raise ValueError("CPU intra-op threads must be between 1 and 4.")
        self.manifest = LocalE5Manifest.load(manifest_path)
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as exc:
            raise EmbeddingSetupError(
                "Local numpy, onnxruntime, and tokenizers packages are required for E5 inference."
            ) from exc

        if _EXECUTION_PROVIDER not in ort.get_available_providers():
            raise EmbeddingSetupError("ONNX Runtime does not provide the CPU execution provider.")
        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_num_threads
        options.inter_op_num_threads = 1
        if hasattr(ort, "ExecutionMode") and hasattr(options, "execution_mode"):
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        try:
            session = ort.InferenceSession(
                str(self.manifest.model_path),
                sess_options=options,
                providers=[_EXECUTION_PROVIDER],
            )
            providers = tuple(session.get_providers())
            tokenizer = Tokenizer.from_file(str(self.manifest.tokenizer_path))
            inputs = {item.name: item.type for item in session.get_inputs()}
        except Exception as exc:
            raise EmbeddingSetupError("The local E5 ONNX or tokenizer artifacts could not load.") from exc
        if providers != (_EXECUTION_PROVIDER,):
            raise EmbeddingSetupError("E5 inference must use CPUExecutionProvider only.")
        _validate_input_contract(inputs)

        runtime_version = getattr(ort, "__version__", "unknown")
        tokenizer_version = getattr(__import__("tokenizers"), "__version__", "unknown")
        fingerprint_data = {
            "artifacts": self.manifest.artifact_fingerprint(),
            "onnxruntime_version": runtime_version,
            "tokenizers_version": tokenizer_version,
            "execution_provider": _EXECUTION_PROVIDER,
            "intra_op_num_threads": intra_op_num_threads,
            "inter_op_num_threads": 1,
            "truncation": "keep-first-max_tokens",
        }
        encoded = json.dumps(fingerprint_data, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        self.fingerprint = hashlib.sha256(encoded).hexdigest()
        self._np = np
        self._session = session
        self._tokenizer = tokenizer
        self._inputs = inputs

    def embed_query(self, text: str) -> tuple[float, ...]:
        """Encode one query with the exact required `query: ` prefix."""
        return self._encode(text, self.manifest.query_prefix)

    def embed_passage(self, text: str) -> tuple[float, ...]:
        """Encode one canonical workspace chunk with the exact `passage: ` prefix."""
        return self._encode(text, self.manifest.passage_prefix)

    def _encode(self, text: str, prefix: str) -> tuple[float, ...]:
        if not isinstance(text, str) or not text.strip() or len(text) > 32_000:
            raise ValueError("Embedding text must contain 1 to 32000 characters.")
        try:
            encoding = self._tokenizer.encode(prefix + text, add_special_tokens=True)
            token_ids = list(encoding.ids[: self.manifest.max_tokens])
        except Exception as exc:
            raise EmbeddingSetupError("The local E5 tokenizer failed to encode text.") from exc
        if not token_ids:
            raise EmbeddingSetupError("The local E5 tokenizer returned no tokens.")

        np = self._np
        length = len(token_ids)
        feed: dict[str, Any] = {}
        for name, tensor_type in self._inputs.items():
            dtype = _numpy_dtype(np, tensor_type)
            if name == "input_ids":
                values = token_ids
            elif name == "attention_mask":
                values = [1] * length
            elif name == "token_type_ids":
                values = [0] * length
            elif name == "position_ids":
                values = list(range(length))
            else:
                raise EmbeddingSetupError("The local E5 ONNX input contract is unsupported.")
            feed[name] = np.asarray([values], dtype=dtype)

        try:
            outputs = self._session.run(None, feed)
        except Exception as exc:
            raise EmbeddingSetupError("Local CPU E5 inference failed.") from exc
        hidden_states = None
        for output in outputs:
            candidate = np.asarray(output)
            if (
                candidate.ndim == 3
                and candidate.shape[0] == 1
                and candidate.shape[1] == length
                and candidate.shape[2] == self.manifest.embedding_dimension
            ):
                hidden_states = candidate.astype(np.float32, copy=False)
                break
        if hidden_states is None:
            raise EmbeddingSetupError("The E5 ONNX output is not token-level 384D hidden state.")

        mask = np.ones((1, length, 1), dtype=np.float32)
        pooled = (hidden_states * mask).sum(axis=1)[0] / float(length)
        norm = float(np.linalg.norm(pooled))
        if not math.isfinite(norm) or norm <= 0.0:
            raise EmbeddingSetupError("The local E5 output cannot be normalized safely.")
        vector = pooled / norm
        if not np.isfinite(vector).all():
            raise EmbeddingSetupError("The local E5 output contains non-finite values.")
        return tuple(float(value) for value in vector.tolist())


def _validate_manifest_values(document: dict[str, Any]) -> None:
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise EmbeddingSetupError("The local E5 manifest schema version is unsupported.")
    if document["model_id"] != MODEL_ID:
        raise EmbeddingSetupError("The local model is not multilingual-e5-small.")
    if not isinstance(document["revision"], str) or not _REVISION_RE.fullmatch(
        document["revision"]
    ):
        raise EmbeddingSetupError("The local E5 revision must be an immutable 40-character commit.")
    if document["license"] != "MIT":
        raise EmbeddingSetupError("The local E5 manifest must identify the expected MIT license.")
    if type(document["embedding_dimension"]) is not int or document["embedding_dimension"] != 384:
        raise EmbeddingSetupError("The local E5 embedding dimension must be 384.")
    if type(document["max_tokens"]) is not int or document["max_tokens"] != MAX_SEQUENCE_TOKENS:
        raise EmbeddingSetupError("The local E5 maximum sequence length must be 512 tokens.")
    if document["pooling"] != "masked_mean" or document["normalization"] != "l2":
        raise EmbeddingSetupError("The local E5 pooling and normalization contract is unsupported.")
    if document["query_prefix"] != QUERY_PREFIX or document["passage_prefix"] != PASSAGE_PREFIX:
        raise EmbeddingSetupError("The local E5 query/passage prefixes do not match the contract.")
    for field in ("onnx_sha256", "tokenizer_sha256"):
        if not isinstance(document[field], str) or not _SHA256_RE.fullmatch(document[field]):
            raise EmbeddingSetupError("The local E5 manifest contains an invalid SHA-256 value.")
    if not _safe_relative_filename(document["onnx_file"], ".onnx"):
        raise EmbeddingSetupError("The local E5 ONNX filename must be a safe relative .onnx path.")
    if not _safe_relative_filename(document["tokenizer_file"], ".json"):
        raise EmbeddingSetupError("The local E5 tokenizer filename must be a safe relative .json path.")


def _safe_relative_filename(value: object, required_suffix: str) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or ":" in value:
        return False
    if any(part in {"", "."} for part in path.parts):
        return False
    return path.suffix.casefold() == required_suffix


def _local_asset(asset_root: Path, relative_name: str, suffix: str) -> Path:
    if not _safe_relative_filename(relative_name, suffix):
        raise EmbeddingSetupError("A local E5 artifact filename is unsafe.")
    target = asset_root / relative_name
    current = asset_root
    try:
        for part in Path(relative_name).parts:
            current = current / part
            metadata = current.lstat()
            if _is_link_or_reparse_point(metadata):
                raise EmbeddingSetupError("Local E5 artifacts cannot use links or reparse points.")
        resolved = target.resolve(strict=True)
        resolved.relative_to(asset_root)
        metadata = resolved.stat()
    except EmbeddingSetupError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise EmbeddingSetupError("A local E5 artifact is unavailable.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise EmbeddingSetupError("A local E5 artifact must be a regular file.")
    return resolved


def _sha256_file(path: Path) -> str:
    try:
        before = path.lstat()
        if _is_link_or_reparse_point(before) or not stat.S_ISREG(before.st_mode):
            raise EmbeddingSetupError("A local E5 artifact is not a regular file.")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
            after = os.fstat(handle.fileno())
        current = path.lstat()
    except EmbeddingSetupError:
        raise
    except (OSError, RuntimeError) as exc:
        raise EmbeddingSetupError("A local E5 artifact could not be hashed.") from exc
    if not _same_file(before, after) or not _same_file(before, current):
        raise EmbeddingSetupError("A local E5 artifact changed while being verified.")
    return digest.hexdigest()


def _validate_input_contract(inputs: dict[str, str]) -> None:
    required = {"input_ids", "attention_mask"}
    allowed = required | {"token_type_ids", "position_ids"}
    if not required.issubset(inputs) or not set(inputs).issubset(allowed):
        raise EmbeddingSetupError("The local E5 ONNX input contract is unsupported.")
    if any(value not in {"tensor(int64)", "tensor(int32)"} for value in inputs.values()):
        raise EmbeddingSetupError("The local E5 ONNX inputs must use integer token tensors.")


def _numpy_dtype(np: Any, tensor_type: str) -> Any:
    if tensor_type == "tensor(int64)":
        return np.int64
    if tensor_type == "tensor(int32)":
        return np.int32
    raise EmbeddingSetupError("The local E5 ONNX input type is unsupported.")


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )
