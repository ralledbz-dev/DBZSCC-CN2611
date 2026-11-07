"""Read-only, allowlisted workspace scanning and source-aware chunking for local RAG.

Only an explicitly supplied absolute workspace root is visited. Excluded paths and
secret-bearing file contents are never returned, hashed, logged, or persisted here.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

CHUNKER_VERSION = "workspace-ast-md-v1"
DEFAULT_MAX_CHUNK_CHARS = 3000
DEFAULT_OVERLAP_CHARS = 240
MAX_WORKSPACE_FILES = 2000
MAX_FILE_BYTES = 256 * 1024
MAX_WORKSPACE_BYTES = 20 * 1024 * 1024
MAX_WORKSPACE_CHUNKS = 25_000

_ALLOWED_SUFFIXES = frozenset(
    {
        ".bat",
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".jsx",
        ".json",
        ".kt",
        ".md",
        ".mjs",
        ".ps1",
        ".py",
        ".rst",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tmp",
        ".tox",
        ".venv",
        "archive",
        "archives",
        "backup",
        "backups",
        "build",
        "cache",
        "caches",
        "codesources",
        "dist",
        "node_modules",
        "out",
        "target",
        "venv",
    }
)
_EXCLUDED_FILE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "id_ed25519",
        "id_rsa",
        "known_hosts",
        "secrets",
        "secrets.json",
        "service_account.json",
    }
)
_EXCLUDED_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".db",
        ".gguf",
        ".jks",
        ".kdbx",
        ".key",
        ".onnx",
        ".p12",
        ".pfx",
        ".pem",
        ".sqlite",
        ".sqlite3",
    }
)
_SECRET_CONTENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
        r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}",
        r"https?://[^/\s:@]+:[^/\s@]+@[^/\s]+",
        (
            r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)"
            r"\s*[:=]\s*['\"`][A-Za-z0-9._~+/-]{20,}['\"`]"
        ),
    )
)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class WorkspaceScanError(ValueError):
    """A workspace could not be scanned completely and safely."""


class WorkspaceLimitError(WorkspaceScanError):
    """An explicit scan or chunking bound was exceeded."""


@dataclass(frozen=True)
class WorkspaceChunk:
    """One allowlisted, source-addressable text chunk."""

    project_id: str
    relative_path: str
    format: str
    symbol: str
    line_start: int
    line_end: int
    ordinal: int
    text: str
    content_sha256: str
    chunker_version: str = CHUNKER_VERSION


@dataclass(frozen=True)
class WorkspaceScanReport:
    """Scan counts contain no paths or contents for excluded material."""

    chunks: tuple[WorkspaceChunk, ...]
    included_file_count: int
    included_bytes: int
    excluded_counts: tuple[tuple[str, int], ...]

    @property
    def excluded_file_count(self) -> int:
        return sum(count for _reason, count in self.excluded_counts)


def scan_workspace(
    root: str | Path,
    project_id: str,
    *,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> WorkspaceScanReport:
    """Scan one explicitly selected local workspace or fail without partial indexing."""
    project_id = _validate_project_id(project_id)
    if type(max_chunk_chars) is not int or not 256 <= max_chunk_chars <= 32_000:
        raise ValueError("Chunk size must be between 256 and 32000 characters.")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < max_chunk_chars // 2:
        raise ValueError("Chunk overlap must be non-negative and less than half the chunk size.")

    candidate = Path(root).expanduser()
    if not candidate.is_absolute() or _is_unc_path(str(candidate)):
        raise WorkspaceScanError("An explicit absolute local workspace root is required.")
    try:
        root_metadata = candidate.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceScanError("The selected workspace is unavailable.") from exc
    if _is_link_or_reparse_point(root_metadata) or not stat.S_ISDIR(root_metadata.st_mode):
        raise WorkspaceScanError("The selected workspace must be a real local directory.")
    try:
        safe_root = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceScanError("The selected workspace could not be resolved safely.") from exc

    excluded: dict[str, int] = {}
    chunks: list[WorkspaceChunk] = []
    included_files = 0
    included_bytes = 0
    scanned_bytes = 0
    pending_directories = [safe_root]

    while pending_directories:
        directory = pending_directories.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold(), reverse=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceScanError("An eligible workspace directory could not be read.") from exc

        for entry in entries:
            name = entry.name
            if _is_hidden_name(name):
                _increment(excluded, "hidden_path")
                continue
            if name.casefold() in _EXCLUDED_DIRECTORIES:
                _increment(excluded, "excluded_directory")
                continue
            if _is_sensitive_name(name):
                _increment(excluded, "sensitive_filename")
                continue
            path = Path(entry.path)
            try:
                # Windows DirEntry.stat may report st_dev/st_ino as zero.
                # lstat gives stable identity for the later open/read checks.
                metadata = path.lstat()
            except (OSError, RuntimeError):
                raise WorkspaceScanError("An eligible workspace entry could not be inspected.")

            if _is_link_or_reparse_point(metadata):
                _increment(excluded, "link_or_reparse_point")
                continue

            if stat.S_ISDIR(metadata.st_mode):
                if _is_windows_hidden(metadata):
                    _increment(excluded, "hidden_path")
                    continue
                pending_directories.append(path)
                continue

            if not stat.S_ISREG(metadata.st_mode):
                _increment(excluded, "non_regular_file")
                continue
            if _is_windows_hidden(metadata):
                _increment(excluded, "hidden_path")
                continue
            if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
                _increment(excluded, "not_allowlisted")
                continue
            if included_files >= MAX_WORKSPACE_FILES:
                raise WorkspaceLimitError("The workspace file-count limit was exceeded.")
            if metadata.st_size > MAX_FILE_BYTES:
                raise WorkspaceLimitError("An allowlisted file exceeds the per-file size limit.")
            if scanned_bytes + metadata.st_size > MAX_WORKSPACE_BYTES:
                raise WorkspaceLimitError("The workspace byte limit was exceeded.")

            relative_path = _safe_relative_path(safe_root, path)
            raw = _read_stable_file(path, metadata)
            if len(raw) > MAX_FILE_BYTES:
                raise WorkspaceLimitError("An allowlisted file grew beyond the per-file size limit.")
            if scanned_bytes + len(raw) > MAX_WORKSPACE_BYTES:
                raise WorkspaceLimitError("The workspace byte limit was exceeded while reading.")
            scanned_bytes += len(raw)
            try:
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                _increment(excluded, "not_utf8_text")
                continue
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if _contains_binary_controls(text):
                _increment(excluded, "binary_controls")
                continue
            if _contains_secret_material(text):
                _increment(excluded, "sensitive_content")
                continue

            file_chunks = chunk_document(
                text,
                project_id=project_id,
                relative_path=relative_path,
                suffix=path.suffix.casefold(),
                max_chunk_chars=max_chunk_chars,
                overlap_chars=overlap_chars,
            )
            if len(chunks) + len(file_chunks) > MAX_WORKSPACE_CHUNKS:
                raise WorkspaceLimitError("The workspace chunk-count limit was exceeded.")
            chunks.extend(file_chunks)
            included_files += 1
            included_bytes += len(raw)

    return WorkspaceScanReport(
        chunks=tuple(chunks),
        included_file_count=included_files,
        included_bytes=included_bytes,
        excluded_counts=tuple(sorted(excluded.items())),
    )


def chunk_document(
    text: str,
    *,
    project_id: str,
    relative_path: str,
    suffix: str,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[WorkspaceChunk, ...]:
    """Chunk one already-vetted file; other languages are marked as line fallbacks."""
    project_id = _validate_project_id(project_id)
    if not isinstance(text, str) or not isinstance(relative_path, str):
        raise TypeError("Chunk input and relative path must be text.")
    normalized_relative_path = relative_path.replace("\\", "/")
    relative_parts = tuple(normalized_relative_path.split("/"))
    if (
        not normalized_relative_path
        or normalized_relative_path.startswith("/")
        or "\x00" in normalized_relative_path
        or ":" in relative_parts[0]
        or any(part in {"", ".", ".."} for part in relative_parts)
        or any(part.startswith(".") for part in relative_parts)
        or any(part.casefold() == "codesources" for part in relative_parts)
    ):
        raise ValueError("A canonical workspace-relative path is required.")
    if suffix not in _ALLOWED_SUFFIXES:
        raise ValueError("Only allowlisted text formats can be chunked.")
    if type(max_chunk_chars) is not int or not 256 <= max_chunk_chars <= 32_000:
        raise ValueError("Chunk size must be between 256 and 32000 characters.")
    if type(overlap_chars) is not int or not 0 <= overlap_chars < max_chunk_chars // 2:
        raise ValueError("Chunk overlap must be non-negative and less than half the chunk size.")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines:
        return ()

    if suffix == ".py":
        try:
            tree = ast.parse(normalized)
        except SyntaxError:
            sections = ((1, len(lines), "line_fallback", "python-line-fallback"),)
        else:
            sections = _python_sections(tree, lines)
    elif suffix in {".md", ".rst"}:
        sections = _markdown_sections(lines) if suffix == ".md" else (
            (1, len(lines), "document", "line_fallback"),
        )
    else:
        sections = ((1, len(lines), "text", "line_fallback"),)

    result: list[WorkspaceChunk] = []
    for start_line, end_line, symbol, strategy in sections:
        for piece, piece_start, piece_end in _split_lines(
            lines[start_line - 1 : end_line],
            source_start_line=start_line,
            max_chars=max_chunk_chars,
            overlap_chars=overlap_chars,
        ):
            content = piece.strip()
            if not content:
                continue
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            result.append(
                WorkspaceChunk(
                    project_id=project_id,
                    relative_path=normalized_relative_path,
                    format=suffix.lstrip("."),
                    symbol=f"{symbol} [{strategy}]",
                    line_start=piece_start,
                    line_end=piece_end,
                    ordinal=len(result),
                    text=content,
                    content_sha256=digest,
                )
            )
    return tuple(result)


def _python_sections(
    tree: ast.Module, lines: list[str]
) -> tuple[tuple[int, int, str, str], ...]:
    symbols: list[tuple[int, int, str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min(
                [node.lineno, *(decorator.lineno for decorator in node.decorator_list)]
            )
            end = node.end_lineno or node.lineno
            symbols.append((start, end, node.name, "python_ast"))

    if not symbols:
        return ((1, len(lines), "module", "python_ast"),)

    sections: list[tuple[int, int, str, str]] = []
    cursor = 1
    for start, end, symbol, strategy in symbols:
        if cursor < start:
            sections.append((cursor, start - 1, "module", "python_ast"))
        sections.append((start, end, symbol, strategy))
        cursor = max(cursor, end + 1)
    if cursor <= len(lines):
        sections.append((cursor, len(lines), "module", "python_ast"))
    return tuple(sections)


def _markdown_sections(lines: list[str]) -> tuple[tuple[int, int, str, str], ...]:
    sections: list[tuple[int, int, str, str]] = []
    heading_stack: list[tuple[int, str]] = []
    section_start = 1
    section_symbol = "document"

    for line_number, line in enumerate(lines, start=1):
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if section_start < line_number:
            sections.append((section_start, line_number - 1, section_symbol, "markdown_heading"))
        level = len(match.group(1))
        title = match.group(2).strip()
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))
        section_start = line_number
        section_symbol = " > ".join(item[1] for item in heading_stack)

    if section_start <= len(lines):
        sections.append((section_start, len(lines), section_symbol, "markdown_heading"))
    return tuple(sections)


def _split_lines(
    lines: list[str],
    *,
    source_start_line: int,
    max_chars: int,
    overlap_chars: int,
) -> tuple[tuple[str, int, int], ...]:
    text = "\n".join(lines)
    if not text:
        return ()
    output: list[tuple[str, int, int]] = []
    cursor = 0
    start_line = source_start_line

    while cursor < len(text):
        end = min(cursor + max_chars, len(text))
        if end < len(text):
            newline = text.rfind("\n", cursor + max_chars // 2, end)
            if newline > cursor:
                end = newline + 1
        piece = text[cursor:end].strip()
        piece_lines = text[cursor:end].count("\n")
        if piece:
            output.append((piece, start_line, start_line + piece_lines))
        if end >= len(text):
            break
        next_cursor = max(cursor + 1, end - overlap_chars)
        start_line += text[cursor:next_cursor].count("\n")
        cursor = next_cursor
    return tuple(output)


def _read_stable_file(path: Path, before: os.stat_result) -> bytes:
    descriptor = -1
    try:
        current = path.lstat()
        if _is_link_or_reparse_point(current) or not _same_file(current, before):
            raise WorkspaceScanError("An eligible file changed before it could be read.")
        if not stat.S_ISREG(current.st_mode):
            raise WorkspaceScanError("An eligible workspace file is not regular text.")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(path), flags)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(opened, before):
            raise WorkspaceScanError("An eligible file changed while it was opened.")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(MAX_FILE_BYTES + 1)
            after_open = os.fstat(handle.fileno())
        after_path = path.lstat()
    except WorkspaceScanError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceScanError("An eligible workspace file could not be read safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        _is_link_or_reparse_point(after_path)
        or not stat.S_ISREG(after_path.st_mode)
        or not _same_file(before, after_open)
        or not _same_file(before, after_path)
    ):
        raise WorkspaceScanError("An eligible file changed while it was being read.")
    return raw


def _safe_relative_path(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceScanError("An eligible path escaped the selected workspace.") from exc
    if not relative.parts or any(part.startswith(".") for part in relative.parts):
        raise WorkspaceScanError("An eligible path contains a hidden component.")
    if any(part.casefold() == "codesources" for part in relative.parts):
        raise WorkspaceScanError("CodeSources is outside the workspace indexing boundary.")
    return relative.as_posix()


def _validate_project_id(value: str) -> str:
    if not isinstance(value, str) or not _PROJECT_ID_RE.fullmatch(value):
        raise ValueError("Project id must be a short stable identifier, not a path.")
    return value


def _is_sensitive_name(name: str) -> bool:
    folded = name.casefold()
    if folded in _EXCLUDED_FILE_NAMES or folded.startswith(".env"):
        return True
    if Path(folded).suffix in _EXCLUDED_SUFFIXES:
        return True
    return any(token in folded for token in ("credential", "secret", "token"))


def _contains_secret_material(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_CONTENT_PATTERNS)


def _contains_binary_controls(text: str) -> bool:
    return any(
        (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
        for character in text
    )


def _is_hidden_name(name: str) -> bool:
    return name.startswith(".")


def _is_link_or_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_point)


def _is_windows_hidden(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    hidden_attribute = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
    return bool(attributes & hidden_attribute)


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


def _is_unc_path(value: str) -> bool:
    return value.startswith("\\\\")


def _increment(counts: dict[str, int], reason: str) -> None:
    counts[reason] = counts.get(reason, 0) + 1
