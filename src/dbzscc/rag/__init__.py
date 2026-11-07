"""Offline local workspace retrieval for DBZSCC 2611."""

from .embeddings import (
    EMBEDDING_DIMENSION,
    MODEL_ID,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    E5OnnxEmbedder,
    EmbeddingSetupError,
    LocalE5Manifest,
)
from .index import (
    EmbeddingFingerprintMismatch,
    IndexBuildResult,
    LocalRagError,
    LocalRagIndex,
    NoActiveIndexError,
    RagSearchResult,
)
from .workspace import (
    CHUNKER_VERSION,
    WorkspaceChunk,
    WorkspaceLimitError,
    WorkspaceScanError,
    WorkspaceScanReport,
    chunk_document,
    scan_workspace,
)

__all__ = [
    "CHUNKER_VERSION",
    "EMBEDDING_DIMENSION",
    "MODEL_ID",
    "PASSAGE_PREFIX",
    "QUERY_PREFIX",
    "E5OnnxEmbedder",
    "EmbeddingFingerprintMismatch",
    "EmbeddingSetupError",
    "IndexBuildResult",
    "LocalE5Manifest",
    "LocalRagError",
    "LocalRagIndex",
    "NoActiveIndexError",
    "RagSearchResult",
    "WorkspaceChunk",
    "WorkspaceLimitError",
    "WorkspaceScanError",
    "WorkspaceScanReport",
    "chunk_document",
    "scan_workspace",
]
