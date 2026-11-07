"""Measured resource and evaluation metadata for the offline RAG worker."""

from __future__ import annotations

from .workspace import DEFAULT_MAX_CHUNK_CHARS

MIB = 1024 * 1024

# These counts describe only the frozen synthetic evaluation corpus; they are not
# workspace scan limits. The enforced workspace limits live in workspace.py.
RAG_TESTED_WORKSPACE_FILES = 24
RAG_TESTED_WORKSPACE_SOURCE_BYTES = 8_615
RAG_TESTED_WORKSPACE_CHUNKS = 33
RAG_TESTED_SEARCH_TEXT_BYTES = 8_565
RAG_TESTED_MAX_CHUNK_CHARS = DEFAULT_MAX_CHUNK_CHARS

# E5 uses two ONNX intra-op threads. This 1.5x memory reserve is a policy value
# derived from the highest observed Windows worker Commit, not a prediction or
# an active admission gate.
RAG_WORKER_CPU_THREADS = 2
RAG_MEASURED_MAX_PEAK_COMMIT_BYTES = 1_455_636_480
RAG_MEMORY_RESERVE_FACTOR_NUMERATOR = 3
RAG_MEMORY_RESERVE_FACTOR_DENOMINATOR = 2
RAG_RESERVED_HOST_MEMORY_MIB = (
    RAG_MEASURED_MAX_PEAK_COMMIT_BYTES * RAG_MEMORY_RESERVE_FACTOR_NUMERATOR
    + (MIB * RAG_MEMORY_RESERVE_FACTOR_DENOMINATOR - 1)
) // (MIB * RAG_MEMORY_RESERVE_FACTOR_DENOMINATOR)
