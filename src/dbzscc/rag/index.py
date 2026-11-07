"""Local FTS5 + bounded exact CPU-vector search with project-scoped RRF fusion."""

from __future__ import annotations

import hashlib
import heapq
import json
import math
import re
import sqlite3
import stat
import struct
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from typing_extensions import Self

from .embeddings import (
    EMBEDDING_DIMENSION,
    MODEL_ID,
    PASSAGE_PREFIX,
    QUERY_PREFIX,
    EmbeddingSetupError,
)
from .workspace import CHUNKER_VERSION, WorkspaceChunk, scan_workspace

RRF_K = 60
MAX_RESULTS = 20
MAX_CANDIDATES_PER_CHANNEL = 100
MAX_INDEXED_CHUNKS = 25_000
VECTOR_SCAN_BLOCK_SIZE = 256
_QUERY_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_HEX_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class LocalRagError(RuntimeError):
    """A local index or retrieval operation could not complete safely."""


class NoActiveIndexError(LocalRagError):
    """No complete local index is active for the requested project."""


class EmbeddingFingerprintMismatch(LocalRagError):
    """The active vectors and query embedder use different model contracts."""


class WorkspaceEmbedder(Protocol):
    """Narrow contract for the approved local E5 encoder and test doubles."""

    model_id: str
    dimension: int
    execution_provider: str
    query_prefix: str
    passage_prefix: str
    fingerprint: str

    def embed_query(self, text: str) -> Sequence[float]: ...

    def embed_passage(self, text: str) -> Sequence[float]: ...


@dataclass(frozen=True)
class IndexBuildResult:
    project_id: str
    generation_id: str
    index_fingerprint: str
    chunk_count: int
    included_file_count: int | None
    included_bytes: int | None
    excluded_counts: tuple[tuple[str, int], ...] | None
    unchanged: bool
    created_at: str


@dataclass(frozen=True)
class RagSearchResult:
    """One source-bearing result with lexical, dense, and fusion diagnostics."""

    project_id: str
    generation_id: str
    chunk_id: str
    relative_path: str
    format: str
    symbol: str
    line_start: int
    line_end: int
    text: str
    content_sha256: str
    lexical_rank: int | None
    dense_rank: int | None
    bm25_score: float | None
    cosine_similarity: float | None
    rrf_score: float


class LocalRagIndex:
    """Explicit local SQLite index; no network access, default paths, or model fallback."""

    def __init__(self, database: str | Path, embedder: WorkspaceEmbedder) -> None:
        self._validate_embedder(embedder)
        if not isinstance(database, (str, Path)) or not str(database):
            raise ValueError("An explicit local SQLite database path is required.")
        database_text = str(database)
        if database_text != ":memory:":
            path = Path(database_text).expanduser()
            if not path.is_absolute() or database_text.startswith("\\\\"):
                raise ValueError("The RAG SQLite database must use an explicit local absolute path.")
            if path.exists():
                try:
                    metadata = path.lstat()
                except OSError as exc:
                    raise ValueError("The RAG SQLite database path is unavailable.") from exc
                if _is_link_or_reparse_point(metadata) or not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("The RAG SQLite database must be a regular local file.")
            database_text = str(path)

        self._embedder = embedder
        self._connection = sqlite3.connect(database_text, timeout=10.0, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self._initialize_schema()
        except Exception:
            self._connection.close()
            raise

    def rebuild_workspace(self, root: str | Path, project_id: str) -> IndexBuildResult:
        """Build a complete generation and atomically switch the active project pointer."""
        report = scan_workspace(root, project_id)
        if len(report.chunks) > MAX_INDEXED_CHUNKS:
            raise LocalRagError("The local vector scan limit was exceeded.")
        index_fingerprint = _index_fingerprint(report.chunks, self._embedder.fingerprint)

        with self._lock:
            active = self._active_generation(project_id)
            if active is not None and active["index_fingerprint"] == index_fingerprint:
                return IndexBuildResult(
                    project_id=project_id,
                    generation_id=active["generation_id"],
                    index_fingerprint=index_fingerprint,
                    chunk_count=int(active["chunk_count"]),
                    included_file_count=report.included_file_count,
                    included_bytes=report.included_bytes,
                    excluded_counts=report.excluded_counts,
                    unchanged=True,
                    created_at=active["created_at"],
                )

        packed_vectors = tuple(
            _pack_vector(self._embedder.embed_passage(chunk.text)) for chunk in report.chunks
        )
        if len(packed_vectors) != len(report.chunks):
            raise LocalRagError("The E5 passage embedding count did not match the chunk count.")

        generation_id = uuid.uuid4().hex
        created_at = _utc_now()
        manifest = json.dumps(
            {
                "schema_version": 1,
                "model_id": self._embedder.model_id,
                "embedding_dimension": self._embedder.dimension,
                "execution_provider": self._embedder.execution_provider,
                "embedding_fingerprint": self._embedder.fingerprint,
                "query_prefix": self._embedder.query_prefix,
                "passage_prefix": self._embedder.passage_prefix,
                "pooling": "masked_mean",
                "normalization": "l2",
                "chunker_version": CHUNKER_VERSION,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """INSERT INTO rag_generations (
                        project_id, generation_id, index_fingerprint,
                        embedding_fingerprint, chunk_count, created_at, manifest_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        project_id,
                        generation_id,
                        index_fingerprint,
                        self._embedder.fingerprint,
                        len(report.chunks),
                        created_at,
                        manifest,
                    ),
                )
                for chunk, vector_blob in zip(report.chunks, packed_vectors, strict=True):
                    self._insert_chunk(project_id, generation_id, chunk, vector_blob)
                chunk_count = self._count_generation(project_id, generation_id)
                fts_count = self._count_fts(project_id, generation_id)
                if chunk_count != len(report.chunks) or fts_count != len(report.chunks):
                    raise LocalRagError("The staged local RAG generation failed consistency checks.")
                connection.execute(
                    """INSERT INTO rag_active (project_id, generation_id)
                       VALUES (?, ?)
                       ON CONFLICT(project_id) DO UPDATE SET generation_id = excluded.generation_id""",
                    (project_id, generation_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        return IndexBuildResult(
            project_id=project_id,
            generation_id=generation_id,
            index_fingerprint=index_fingerprint,
            chunk_count=len(report.chunks),
            included_file_count=report.included_file_count,
            included_bytes=report.included_bytes,
            excluded_counts=report.excluded_counts,
            unchanged=False,
            created_at=created_at,
        )

    def search(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 8,
    ) -> tuple[RagSearchResult, ...]:
        """Search only one project's active generation and fuse independent ranked lists."""
        _validate_project_id(project_id)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Search text must contain 1 to 2000 characters.")
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"Search limit must be between 1 and {MAX_RESULTS}.")

        with self._lock:
            generation = self._active_generation(project_id)
            if generation is None:
                raise NoActiveIndexError("No complete local index is active for this project.")
            if generation["embedding_fingerprint"] != self._embedder.fingerprint:
                raise EmbeddingFingerprintMismatch(
                    "The active vectors were built with a different local E5 fingerprint."
                )
            generation_id = generation["generation_id"]
            query_vector = _validated_vector(self._embedder.embed_query(query))
            candidate_limit = min(max(limit * 4, limit), MAX_CANDIDATES_PER_CHANNEL)
            lexical = self._lexical_search(project_id, generation_id, query, candidate_limit)
            dense = self._dense_search(
                project_id, generation_id, query_vector, candidate_limit
            )
            candidate_ids = set(lexical) | {chunk_id for chunk_id, _score in dense}
            metadata = self._get_chunks(project_id, generation_id, candidate_ids)

        lexical_ranks = {chunk_id: rank for rank, chunk_id in enumerate(lexical, start=1)}
        dense_ranks = {chunk_id: rank for rank, (chunk_id, _score) in enumerate(dense, start=1)}
        dense_scores = {chunk_id: score for chunk_id, score in dense}
        lexical_scores = {chunk_id: score for chunk_id, score in lexical.items()}
        results: list[RagSearchResult] = []
        for chunk_id in candidate_ids:
            row = metadata.get(chunk_id)
            if row is None:
                continue
            lexical_rank = lexical_ranks.get(chunk_id)
            dense_rank = dense_ranks.get(chunk_id)
            rrf_score = 0.0
            if lexical_rank is not None:
                rrf_score += 1.0 / (RRF_K + lexical_rank)
            if dense_rank is not None:
                rrf_score += 1.0 / (RRF_K + dense_rank)
            results.append(
                RagSearchResult(
                    project_id=project_id,
                    generation_id=generation_id,
                    chunk_id=chunk_id,
                    relative_path=row["relative_path"],
                    format=row["format"],
                    symbol=row["symbol"],
                    line_start=int(row["line_start"]),
                    line_end=int(row["line_end"]),
                    text=row["text"],
                    content_sha256=row["content_sha256"],
                    lexical_rank=lexical_rank,
                    dense_rank=dense_rank,
                    bm25_score=lexical_scores.get(chunk_id),
                    cosine_similarity=dense_scores.get(chunk_id),
                    rrf_score=rrf_score,
                )
            )
        results.sort(
            key=lambda item: (
                -item.rrf_score,
                min(
                    item.lexical_rank or MAX_CANDIDATES_PER_CHANNEL + 1,
                    item.dense_rank or MAX_CANDIDATES_PER_CHANNEL + 1,
                ),
                item.relative_path.casefold(),
                item.line_start,
                item.chunk_id,
            )
        )
        return tuple(results[:limit])

    def active_generation(self, project_id: str) -> IndexBuildResult | None:
        """Return active generation metadata without exposing a workspace root or content."""
        _validate_project_id(project_id)
        with self._lock:
            row = self._active_generation(project_id)
            if row is None:
                return None
            return IndexBuildResult(
                project_id=project_id,
                generation_id=row["generation_id"],
                index_fingerprint=row["index_fingerprint"],
                chunk_count=int(row["chunk_count"]),
                included_file_count=None,
                included_bytes=None,
                excluded_counts=None,
                unchanged=True,
                created_at=row["created_at"],
            )

    def close(self) -> None:
        """Close the caller-selected local database connection."""
        with self._lock:
            self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        with self._lock:
            connection = self._connection
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS rag_generations (
                        project_id TEXT NOT NULL,
                        generation_id TEXT NOT NULL,
                        index_fingerprint TEXT NOT NULL,
                        embedding_fingerprint TEXT NOT NULL,
                        chunk_count INTEGER NOT NULL CHECK(chunk_count >= 0),
                        created_at TEXT NOT NULL,
                        manifest_json TEXT NOT NULL,
                        PRIMARY KEY (project_id, generation_id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS rag_chunks (
                        project_id TEXT NOT NULL,
                        generation_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        relative_path TEXT NOT NULL,
                        format TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        line_start INTEGER NOT NULL CHECK(line_start >= 1),
                        line_end INTEGER NOT NULL CHECK(line_end >= line_start),
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        text TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        vector BLOB NOT NULL,
                        PRIMARY KEY (project_id, generation_id, chunk_id),
                        FOREIGN KEY (project_id, generation_id)
                            REFERENCES rag_generations(project_id, generation_id)
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS rag_active (
                        project_id TEXT PRIMARY KEY,
                        generation_id TEXT NOT NULL,
                        FOREIGN KEY (project_id, generation_id)
                            REFERENCES rag_generations(project_id, generation_id)
                    )"""
                )
                connection.execute(
                    """CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
                        chunk_id UNINDEXED,
                        generation_id UNINDEXED,
                        project_id UNINDEXED,
                        text,
                        tokenize = 'unicode61'
                    )"""
                )
                connection.commit()
            except sqlite3.OperationalError as exc:
                connection.rollback()
                raise LocalRagError("This local SQLite build does not provide FTS5.") from exc

    def _insert_chunk(
        self,
        project_id: str,
        generation_id: str,
        chunk: WorkspaceChunk,
        vector_blob: bytes,
    ) -> None:
        chunk_id = _chunk_id(chunk)
        self._connection.execute(
            """INSERT INTO rag_chunks (
                project_id, generation_id, chunk_id, relative_path, format, symbol,
                line_start, line_end, ordinal, text, content_sha256, vector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                project_id,
                generation_id,
                chunk_id,
                chunk.relative_path,
                chunk.format,
                chunk.symbol,
                chunk.line_start,
                chunk.line_end,
                chunk.ordinal,
                chunk.text,
                chunk.content_sha256,
                vector_blob,
            ),
        )
        self._connection.execute(
            "INSERT INTO rag_fts (chunk_id, generation_id, project_id, text) "
            "VALUES (?, ?, ?, ?)",
            (chunk_id, generation_id, project_id, chunk.text),
        )

    def _active_generation(self, project_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """SELECT g.* FROM rag_active AS a
               JOIN rag_generations AS g
                 ON g.project_id = a.project_id AND g.generation_id = a.generation_id
               WHERE a.project_id = ?""",
            (project_id,),
        ).fetchone()

    def _count_generation(self, project_id: str, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM rag_chunks WHERE project_id = ? AND generation_id = ?",
            (project_id, generation_id),
        ).fetchone()
        return int(row["count"])

    def _count_fts(self, project_id: str, generation_id: str) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM rag_fts WHERE project_id = ? AND generation_id = ?",
            (project_id, generation_id),
        ).fetchone()
        return int(row["count"])

    def _lexical_search(
        self,
        project_id: str,
        generation_id: str,
        query: str,
        limit: int,
    ) -> dict[str, float]:
        match_query = _fts_match_query(query)
        if not match_query:
            return {}
        rows = self._connection.execute(
            """SELECT rag_fts.chunk_id, bm25(rag_fts) AS bm25_score
               FROM rag_fts
               JOIN rag_chunks AS c
                 ON c.project_id = rag_fts.project_id
                AND c.generation_id = rag_fts.generation_id
                AND c.chunk_id = rag_fts.chunk_id
               WHERE rag_fts MATCH ?
                 AND rag_fts.project_id = ?
                 AND rag_fts.generation_id = ?
               ORDER BY bm25(rag_fts) ASC, rag_fts.chunk_id ASC
               LIMIT ?""",
            (match_query, project_id, generation_id, limit),
        ).fetchall()
        return {row["chunk_id"]: float(row["bm25_score"]) for row in rows}

    def _dense_search(
        self,
        project_id: str,
        generation_id: str,
        query_vector: tuple[float, ...],
        limit: int,
    ) -> tuple[tuple[str, float], ...]:
        cursor = self._connection.execute(
            """SELECT chunk_id, vector FROM rag_chunks
               WHERE project_id = ? AND generation_id = ? ORDER BY chunk_id""",
            (project_id, generation_id),
        )
        best: list[tuple[float, str]] = []
        while True:
            rows = cursor.fetchmany(VECTOR_SCAN_BLOCK_SIZE)
            if not rows:
                break
            for row in rows:
                blob = row["vector"]
                if len(blob) != EMBEDDING_DIMENSION * 4:
                    raise LocalRagError("A stored local vector has an invalid length.")
                vector = struct.unpack(f"<{EMBEDDING_DIMENSION}f", blob)
                score = math.fsum(left * right for left, right in zip(query_vector, vector, strict=True))
                score = max(-1.0, min(1.0, score))
                candidate = (score, row["chunk_id"])
                if len(best) < limit:
                    heapq.heappush(best, candidate)
                elif candidate > best[0]:
                    heapq.heapreplace(best, candidate)
        best.sort(key=lambda item: (-item[0], item[1]))
        return tuple((chunk_id, score) for score, chunk_id in best)

    def _get_chunks(
        self, project_id: str, generation_id: str, chunk_ids: set[str]
    ) -> dict[str, sqlite3.Row]:
        if not chunk_ids:
            return {}
        ordered_ids = sorted(chunk_ids)
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self._connection.execute(
            f"""SELECT * FROM rag_chunks
                WHERE project_id = ? AND generation_id = ? AND chunk_id IN ({placeholders})""",
            (project_id, generation_id, *ordered_ids),
        ).fetchall()
        return {row["chunk_id"]: row for row in rows}

    @staticmethod
    def _validate_embedder(embedder: WorkspaceEmbedder) -> None:
        if getattr(embedder, "model_id", None) != MODEL_ID:
            raise EmbeddingSetupError("Only local multilingual-e5-small embeddings are accepted.")
        if getattr(embedder, "dimension", None) != EMBEDDING_DIMENSION:
            raise EmbeddingSetupError("The local E5 embedding dimension must be 384.")
        if getattr(embedder, "execution_provider", None) != "CPUExecutionProvider":
            raise EmbeddingSetupError("RAG embeddings must use CPUExecutionProvider only.")
        if getattr(embedder, "query_prefix", None) != QUERY_PREFIX:
            raise EmbeddingSetupError("The E5 query prefix does not match the local contract.")
        if getattr(embedder, "passage_prefix", None) != PASSAGE_PREFIX:
            raise EmbeddingSetupError("The E5 passage prefix does not match the local contract.")
        fingerprint = getattr(embedder, "fingerprint", None)
        if not isinstance(fingerprint, str) or not _HEX_SHA256_RE.fullmatch(fingerprint):
            raise EmbeddingSetupError("A verified 64-character embedding fingerprint is required.")


def _index_fingerprint(chunks: tuple[WorkspaceChunk, ...], embedding_fingerprint: str) -> str:
    payload = {
        "schema_version": 1,
        "chunker_version": CHUNKER_VERSION,
        "embedding_fingerprint": embedding_fingerprint,
        "chunks": [
            [
                chunk.project_id,
                chunk.relative_path,
                chunk.format,
                chunk.symbol,
                chunk.line_start,
                chunk.line_end,
                chunk.ordinal,
                chunk.content_sha256,
            ]
            for chunk in chunks
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _chunk_id(chunk: WorkspaceChunk) -> str:
    payload = "\0".join(
        (
            chunk.project_id,
            chunk.relative_path,
            chunk.format,
            chunk.symbol,
            str(chunk.line_start),
            str(chunk.line_end),
            str(chunk.ordinal),
            chunk.content_sha256,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fts_match_query(query: str) -> str:
    tokens = tuple(dict.fromkeys(_QUERY_TOKEN_RE.findall(query)))[:32]
    if not tokens:
        return ""
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _pack_vector(values: Sequence[float]) -> bytes:
    vector = _validated_vector(values)
    return struct.pack(f"<{EMBEDDING_DIMENSION}f", *vector)


def _validated_vector(values: Sequence[float]) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise EmbeddingSetupError("The local E5 encoder returned an invalid vector.")
    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EmbeddingSetupError("The local E5 encoder returned an invalid vector.") from exc
    if len(vector) != EMBEDDING_DIMENSION or not all(math.isfinite(value) for value in vector):
        raise EmbeddingSetupError("The local E5 encoder must return 384 finite values.")
    norm = math.sqrt(math.fsum(value * value for value in vector))
    if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
        raise EmbeddingSetupError("The local E5 encoder must return an L2-normalized vector.")
    return vector


def _validate_project_id(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(character in value for character in ("/", "\\", "\x00"))
        or value in {".", ".."}
    ):
        raise ValueError("Project id must be a stable identifier, not a path.")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_link_or_reparse_point(metadata: object) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)
