"""Bounded, read-only extraction of GGUF metadata prefixes.

The reader stops after the metadata key/value section. It does not inspect
tensor descriptors or tensor payloads, and it never executes model files.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Union

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_SUPPORTED_VERSIONS = frozenset({2, 3})
_MAX_HEADER_BYTES = 64 * 1024 * 1024
_MAX_STRING_BYTES = 1024 * 1024
_MAX_KEY_BYTES = 65_535
_MAX_METADATA_ENTRIES = 100_000
_MAX_ARRAY_ITEMS = 1_000_000
_MAX_RETAINED_ARRAY_ITEMS = 4096
_READ_CHUNK_BYTES = 64 * 1024

PathInput = Union[str, os.PathLike]

_SCALAR_TYPES = {
    0: ("uint8", struct.Struct("<B")),
    1: ("int8", struct.Struct("<b")),
    2: ("uint16", struct.Struct("<H")),
    3: ("int16", struct.Struct("<h")),
    4: ("uint32", struct.Struct("<I")),
    5: ("int32", struct.Struct("<i")),
    6: ("float32", struct.Struct("<f")),
    7: ("bool", struct.Struct("<B")),
    10: ("uint64", struct.Struct("<Q")),
    11: ("int64", struct.Struct("<q")),
    12: ("float64", struct.Struct("<d")),
}


@dataclass(frozen=True)
class GGUFArrayValue:
    """A bounded summary of a GGUF array metadata value.

    Values are retained only while the reader's shared per-document budget
    allows. Larger arrays are still parsed and validated, but their individual
    values are omitted to keep memory use bounded.
    """

    element_type: str
    item_count: int
    values: tuple[Any, ...] | None


@dataclass(frozen=True)
class GGUFMetadataField:
    """One parsed metadata field with its source offset in the file."""

    key: str
    type_name: str
    value: Any
    offset: int


@dataclass(frozen=True)
class GGUFMetadataDocument:
    """Successfully parsed metadata prefix and its local-file provenance."""

    path: str
    version: int
    tensor_count: int
    metadata_count: int
    fields: tuple[GGUFMetadataField, ...]
    metadata_end_offset: int
    file_size_bytes: int
    modified_at: datetime
    metadata_prefix_sha256: str

    def field(self, key: str) -> GGUFMetadataField | None:
        """Return the unique field for ``key``, if present."""
        return next((item for item in self.fields if item.key == key), None)


@dataclass(frozen=True)
class GGUFParseIssue:
    """A structured, non-fatal parse or file-access failure."""

    code: str
    path: str
    offset: int | None
    detail: str


@dataclass(frozen=True)
class GGUFParseResult:
    """Either parsed metadata or one explicit issue; never both."""

    metadata: GGUFMetadataDocument | None
    issue: GGUFParseIssue | None

    @property
    def ok(self) -> bool:
        return self.metadata is not None and self.issue is None


class GGUFMetadataReader:
    """Parse a little-endian GGUF v2/v3 metadata prefix without reading tensors.

    Prefix bytes, individual strings, metadata entries, array sizes, nesting
    depth and retained array items all have explicit limits. Large arrays are
    validated but summarized rather than retained in memory. Unsupported byte
    order, versions and malformed input are returned as structured issues.
    """

    def __init__(
        self,
        max_header_bytes: int = _MAX_HEADER_BYTES,
        max_string_bytes: int = _MAX_STRING_BYTES,
        max_metadata_entries: int = _MAX_METADATA_ENTRIES,
        max_array_items: int = _MAX_ARRAY_ITEMS,
        max_array_depth: int = 8,
        max_retained_array_items: int = _MAX_RETAINED_ARRAY_ITEMS,
    ) -> None:
        limits = {
            "max_header_bytes": max_header_bytes,
            "max_string_bytes": max_string_bytes,
            "max_metadata_entries": max_metadata_entries,
            "max_array_items": max_array_items,
            "max_array_depth": max_array_depth,
        }
        for name, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(max_retained_array_items, bool)
            or not isinstance(max_retained_array_items, int)
            or max_retained_array_items < 0
        ):
            raise ValueError("max_retained_array_items must be a non-negative integer")

        self._max_header_bytes = max_header_bytes
        self._max_string_bytes = max_string_bytes
        self._max_metadata_entries = max_metadata_entries
        self._max_array_items = max_array_items
        self._max_array_depth = max_array_depth
        self._max_retained_array_items = max_retained_array_items

    def read(self, raw_path: PathInput) -> GGUFParseResult:
        """Read and validate a local GGUF metadata prefix, without writing."""
        try:
            path = Path(raw_path).expanduser()
            display_path = os.path.abspath(os.fspath(path))
        except (OSError, RuntimeError, TypeError, ValueError):
            return self._failure(
                str(raw_path), "invalid_path", None, "The supplied path is invalid."
            )

        try:
            path_stat = path.lstat()
        except OSError as exc:
            return self._file_failure(display_path, exc)

        if _is_link_or_reparse_point(path_stat):
            return self._failure(
                display_path,
                "linked_file_skipped",
                None,
                "File is a symlink or filesystem reparse point.",
            )
        if not stat.S_ISREG(path_stat.st_mode):
            return self._failure(
                display_path, "not_a_regular_file", None, "Path is not a regular file."
            )

        try:
            with path.open("rb") as stream:
                opened_stat = os.fstat(stream.fileno())
                if _is_link_or_reparse_point(opened_stat) or not stat.S_ISREG(
                    opened_stat.st_mode
                ):
                    return self._failure(
                        display_path,
                        "not_a_regular_file",
                        None,
                        "Opened path is not a regular file.",
                    )

                if _file_identity_changed(path_stat, opened_stat):
                    return self._failure(
                        display_path,
                        "file_changed_before_read",
                        None,
                        "File identity changed after path validation.",
                    )

                cursor = _Cursor(stream, self._max_header_bytes)
                try:
                    magic = cursor.read_exact(4)
                    if magic != b"GGUF":
                        raise _ParseFailure(
                            "invalid_magic", 0, "Expected the GGUF magic bytes."
                        )

                    version_offset = cursor.offset
                    version_bytes = cursor.read_exact(4)
                    version = struct.unpack("<I", version_bytes)[0]
                    if version not in _SUPPORTED_VERSIONS:
                        big_endian_version = struct.unpack(">I", version_bytes)[0]
                        if big_endian_version in _SUPPORTED_VERSIONS:
                            raise _ParseFailure(
                                "unsupported_byte_order",
                                version_offset,
                                "Big-endian GGUF is not supported; values are read as little-endian.",
                            )
                        raise _ParseFailure(
                            "unsupported_version",
                            version_offset,
                            "Only little-endian GGUF versions 2 and 3 are supported.",
                        )

                    tensor_count = cursor.read_u64()
                    metadata_count = cursor.read_u64()
                    if metadata_count > self._max_metadata_entries:
                        raise _ParseFailure(
                            "metadata_entry_limit_exceeded",
                            16,
                            "Metadata entry count exceeds the configured limit.",
                        )

                    fields = []
                    seen_keys = set()
                    array_budget = _ParseBudget(self._max_retained_array_items)
                    for _ in range(metadata_count):
                        field_offset = cursor.offset
                        key = self._read_string(cursor, max_bytes=_MAX_KEY_BYTES)
                        try:
                            key.encode("ascii")
                        except UnicodeEncodeError as exc:
                            raise _ParseFailure(
                                "invalid_key",
                                field_offset,
                                "GGUF metadata keys must contain ASCII characters only.",
                            ) from exc
                        if key in seen_keys:
                            raise _ParseFailure(
                                "duplicate_metadata_key",
                                field_offset,
                                "Metadata keys must be unique.",
                            )
                        seen_keys.add(key)

                        value_type = cursor.read_u32()
                        type_name, value = self._read_value(
                            cursor, value_type, budget=array_budget
                        )
                        fields.append(
                            GGUFMetadataField(
                                key=key,
                                type_name=type_name,
                                value=value,
                                offset=field_offset,
                            )
                        )
                except _ParseFailure as exc:
                    return self._failure(
                        display_path, exc.code, exc.offset, exc.detail
                    )

                after_stat = os.fstat(stream.fileno())
                if _file_signature(opened_stat) != _file_signature(after_stat):
                    return self._failure(
                        display_path,
                        "file_changed_during_read",
                        cursor.offset,
                        "File size or timestamps changed while metadata was read.",
                    )

                document = GGUFMetadataDocument(
                    path=display_path,
                    version=version,
                    tensor_count=tensor_count,
                    metadata_count=metadata_count,
                    fields=tuple(fields),
                    metadata_end_offset=cursor.offset,
                    file_size_bytes=opened_stat.st_size,
                    modified_at=datetime.fromtimestamp(
                        opened_stat.st_mtime, tz=timezone.utc
                    ),
                    metadata_prefix_sha256=cursor.hexdigest(),
                )
                return GGUFParseResult(metadata=document, issue=None)
        except OSError as exc:
            return self._file_failure(display_path, exc)

    def _read_string(self, cursor: _Cursor, max_bytes: int | None = None) -> str:
        length_offset = cursor.offset
        length = cursor.read_u64()
        limit = self._max_string_bytes if max_bytes is None else min(
            self._max_string_bytes, max_bytes
        )
        if length > limit:
            raise _ParseFailure(
                "string_limit_exceeded",
                length_offset,
                "String length exceeds the configured limit.",
            )
        value_offset = cursor.offset
        raw_value = cursor.read_exact(length)
        try:
            return raw_value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _ParseFailure(
                "invalid_utf8", value_offset + exc.start, "String is not valid UTF-8."
            ) from exc

    def _read_value(
        self,
        cursor: _Cursor,
        value_type: int,
        budget: _ParseBudget,
        depth: int = 0,
        retain: bool = True,
    ) -> tuple[str, Any]:
        if value_type in _SCALAR_TYPES:
            return self._read_scalar(cursor, value_type)
        if value_type == 8:
            return "string", self._read_string(cursor)
        if value_type != 9:
            raise _ParseFailure(
                "unsupported_value_type",
                cursor.offset - 4,
                f"Unsupported metadata value type id {value_type}.",
            )

        array_depth = depth + 1
        if array_depth > self._max_array_depth:
            raise _ParseFailure(
                "array_depth_limit_exceeded",
                cursor.offset - 4,
                "Nested metadata array depth exceeds the configured limit.",
            )
        element_type = cursor.read_u32()
        count_offset = cursor.offset
        item_count = cursor.read_u64()
        if element_type not in (8, 9) and element_type not in _SCALAR_TYPES:
            raise _ParseFailure(
                "unsupported_array_element_type",
                count_offset - 4,
                f"Unsupported array element type id {element_type}.",
            )
        if item_count > self._max_array_items:
            raise _ParseFailure(
                "array_item_limit_exceeded",
                count_offset,
                "Array item count exceeds the configured limit.",
            )

        if element_type == 8:
            element_name = "string"
        elif element_type == 9:
            element_name = "array"
        else:
            element_name = _SCALAR_TYPES[element_type][0]
        retain_values = retain and item_count <= budget.remaining
        if retain_values:
            budget.remaining -= item_count
        if element_type in _SCALAR_TYPES and not retain_values:
            scalar_name, scalar_struct = _SCALAR_TYPES[element_type]
            cursor.skip_exact(
                item_count * scalar_struct.size,
                validate_boolean=scalar_name == "bool",
            )
            values = None
        else:
            retained = []
            for _ in range(item_count):
                _, value = self._read_value(
                    cursor,
                    element_type,
                    budget=budget,
                    depth=array_depth,
                    retain=retain_values,
                )
                if retain_values:
                    retained.append(value)
            values = tuple(retained) if retain_values else None

        return "array", GGUFArrayValue(
            element_type=element_name,
            item_count=item_count,
            values=values,
        )

    @staticmethod
    def _read_scalar(cursor: _Cursor, value_type: int) -> tuple[str, Any]:
        type_name, value_struct = _SCALAR_TYPES[value_type]
        value_offset = cursor.offset
        (value,) = value_struct.unpack(cursor.read_exact(value_struct.size))
        if type_name == "bool":
            if value not in (0, 1):
                raise _ParseFailure(
                    "invalid_boolean",
                    value_offset,
                    "Boolean metadata values must be encoded as 0 or 1.",
                )
            return type_name, bool(value)
        return type_name, value

    @staticmethod
    def _failure(
        path: str, code: str, offset: int | None, detail: str
    ) -> GGUFParseResult:
        return GGUFParseResult(
            metadata=None,
            issue=GGUFParseIssue(
                code=code, path=path, offset=offset, detail=detail
            ),
        )

    def _file_failure(self, path: str, exc: OSError) -> GGUFParseResult:
        if isinstance(exc, FileNotFoundError):
            code = "file_not_found"
        elif isinstance(exc, PermissionError):
            code = "file_access_denied"
        else:
            code = "file_read_failed"
        detail = getattr(exc, "strerror", None) or type(exc).__name__
        return self._failure(path, code, None, detail)


class _ParseFailure(Exception):
    def __init__(self, code: str, offset: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.offset = offset
        self.detail = detail


class _ParseBudget:
    def __init__(self, retained_array_items: int) -> None:
        self.remaining = retained_array_items


class _Cursor:
    def __init__(self, stream: BinaryIO, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._digest = hashlib.sha256()
        self.offset = 0

    def read_exact(self, byte_count: int) -> bytes:
        if byte_count < 0:
            raise _ParseFailure(
                "invalid_length", self.offset, "Negative byte length is invalid."
            )
        if self.offset + byte_count > self._max_bytes:
            raise _ParseFailure(
                "header_size_limit_exceeded",
                self.offset,
                "Metadata prefix exceeds the configured byte limit.",
            )

        chunks = []
        remaining = byte_count
        while remaining:
            chunk = self._stream.read(remaining)
            if not chunk:
                raise _ParseFailure(
                    "truncated_file", self.offset, "Unexpected end of file."
                )
            chunks.append(chunk)
            self._digest.update(chunk)
            self.offset += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def skip_exact(self, byte_count: int, validate_boolean: bool = False) -> None:
        if byte_count < 0:
            raise _ParseFailure(
                "invalid_length", self.offset, "Negative byte length is invalid."
            )
        if self.offset + byte_count > self._max_bytes:
            raise _ParseFailure(
                "header_size_limit_exceeded",
                self.offset,
                "Metadata prefix exceeds the configured byte limit.",
            )

        remaining = byte_count
        while remaining:
            chunk_size = min(remaining, _READ_CHUNK_BYTES)
            chunk_offset = self.offset
            chunk = self.read_exact(chunk_size)
            if validate_boolean and max(chunk) > 1:
                raise _ParseFailure(
                    "invalid_boolean",
                    chunk_offset,
                    "Boolean array values must be encoded as 0 or 1.",
                )
            remaining -= len(chunk)

    def read_u32(self) -> int:
        return struct.unpack("<I", self.read_exact(4))[0]

    def read_u64(self) -> int:
        return struct.unpack("<Q", self.read_exact(8))[0]

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _is_link_or_reparse_point(file_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _file_identity_changed(
    original: os.stat_result, opened: os.stat_result
) -> bool:
    if not original.st_ino or not opened.st_ino:
        return False
    return (original.st_dev, original.st_ino) != (opened.st_dev, opened.st_ino)


def _file_signature(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )
