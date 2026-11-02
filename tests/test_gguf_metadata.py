import hashlib
import struct
from pathlib import Path

import pytest

from dbzscc.miro import GGUFMetadataReader


def _u32(value: int) -> bytes:
    return struct.pack("<I", value)


def _u64(value: int) -> bytes:
    return struct.pack("<Q", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _entry(key: str, value_type: int, value: bytes) -> bytes:
    return _string(key) + _u32(value_type) + value


def _document(entries: list[bytes], version: int = 3) -> tuple[bytes, bytes]:
    prefix = (
        b"GGUF"
        + _u32(version)
        + _u64(2)
        + _u64(len(entries))
        + b"".join(entries)
    )
    # The parser must stop at the metadata boundary and ignore later bytes.
    return prefix + b"unparsed tensor information and payload", prefix


def test_reads_metadata_prefix_with_offsets_and_file_provenance(tmp_path: Path) -> None:
    entries = [
        _entry("general.name", 8, _string("fixture-model")),
        _entry("general.alignment", 4, _u32(32)),
        _entry("general.use_mmap", 7, b"\x01"),
        _entry(
            "tokenizer.ggml.tokens",
            9,
            _u32(8) + _u64(3) + _string("a") + _string("b") + _string("c"),
        ),
    ]
    contents, expected_prefix = _document(entries)
    path = tmp_path / "model.gguf"
    path.write_bytes(contents)

    result = GGUFMetadataReader().read(path)

    assert result.ok
    assert result.issue is None
    assert result.metadata is not None
    metadata = result.metadata
    assert metadata.version == 3
    assert metadata.tensor_count == 2
    assert metadata.metadata_count == 4
    assert metadata.metadata_end_offset == len(expected_prefix)
    assert metadata.file_size_bytes == len(contents)
    assert metadata.modified_at.utcoffset().total_seconds() == 0
    assert metadata.metadata_prefix_sha256 == hashlib.sha256(expected_prefix).hexdigest()
    assert metadata.field("general.name").value == "fixture-model"
    assert metadata.field("general.alignment").value == 32
    assert metadata.field("general.use_mmap").value is True

    tokens = metadata.field("tokenizer.ggml.tokens")
    assert tokens.type_name == "array"
    assert tokens.value.element_type == "string"
    assert tokens.value.item_count == 3
    assert tokens.value.values == ("a", "b", "c")
    assert tokens.offset > 0


def test_reads_gguf_v2_little_endian_prefix(tmp_path: Path) -> None:
    path = tmp_path / "version-2.gguf"
    path.write_bytes(_document([], version=2)[0])

    result = GGUFMetadataReader().read(path)

    assert result.ok
    assert result.metadata.version == 2
    assert result.metadata.fields == ()


def test_large_array_is_validated_but_not_retained(tmp_path: Path) -> None:
    array = _u32(4) + _u64(3) + _u32(10) + _u32(20) + _u32(30)
    contents, _ = _document([_entry("values", 9, array)])
    path = tmp_path / "large-array.gguf"
    path.write_bytes(contents)

    result = GGUFMetadataReader(max_retained_array_items=2).read(path)

    assert result.ok
    value = result.metadata.field("values").value
    assert value.item_count == 3
    assert value.values is None


def test_array_retention_budget_is_shared_across_metadata_fields(
    tmp_path: Path,
) -> None:
    first = _u32(4) + _u64(2) + _u32(1) + _u32(2)
    second = _u32(4) + _u64(2) + _u32(3) + _u32(4)
    path = tmp_path / "array-budget.gguf"
    path.write_bytes(
        _document([_entry("first", 9, first), _entry("second", 9, second)])[0]
    )

    result = GGUFMetadataReader(max_retained_array_items=3).read(path)

    assert result.ok
    assert result.metadata.field("first").value.values == (1, 2)
    assert result.metadata.field("second").value.values is None


def test_reads_nested_arrays_and_enforces_nesting_depth(tmp_path: Path) -> None:
    inner_one = _u32(4) + _u64(2) + _u32(10) + _u32(20)
    inner_two = _u32(4) + _u64(1) + _u32(30)
    nested_array = _u32(9) + _u64(2) + inner_one + inner_two
    path = tmp_path / "nested-array.gguf"
    path.write_bytes(_document([_entry("values", 9, nested_array)])[0])

    result = GGUFMetadataReader().read(path)

    assert result.ok
    value = result.metadata.field("values").value
    assert value.element_type == "array"
    assert value.values[0].values == (10, 20)
    assert value.values[1].values == (30,)

    limited = GGUFMetadataReader(max_array_depth=1).read(path)
    assert limited.issue.code == "array_depth_limit_exceeded"


def test_reports_invalid_magic_unsupported_version_and_truncation(
    tmp_path: Path,
) -> None:
    cases = (
        ("bad-magic.gguf", b"NOPE" + _u32(3) + _u64(0) + _u64(0), "invalid_magic"),
        (
            "future-version.gguf",
            b"GGUF" + _u32(99) + _u64(0) + _u64(0),
            "unsupported_version",
        ),
        (
            "big-endian-v3.gguf",
            b"GGUF" + struct.pack(">I", 3) + _u64(0) + _u64(0),
            "unsupported_byte_order",
        ),
        ("truncated.gguf", b"GGUF", "truncated_file"),
    )

    for filename, contents, issue_code in cases:
        path = tmp_path / filename
        path.write_bytes(contents)
        result = GGUFMetadataReader().read(path)
        assert not result.ok
        assert result.metadata is None
        assert result.issue.code == issue_code
        assert result.issue.path == str(path.absolute())


def test_reports_malformed_utf8_unknown_types_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "bad-utf8.gguf",
            _document([_entry("name", 8, _u64(1) + b"\xff")])[0],
            "invalid_utf8",
        ),
        (
            "unknown-type.gguf",
            _document([_entry("name", 77, b"")])[0],
            "unsupported_value_type",
        ),
        (
            "duplicate.gguf",
            _document(
                [_entry("name", 4, _u32(1)), _entry("name", 4, _u32(2))]
            )[0],
            "duplicate_metadata_key",
        ),
        (
            "non-ascii-key.gguf",
            _document([_entry("näme", 4, _u32(1))])[0],
            "invalid_key",
        ),
        (
            "oversized-key.gguf",
            _document([_string("k" * 65_536) + _u32(4) + _u32(1)])[0],
            "string_limit_exceeded",
        ),
    )

    for filename, contents, issue_code in cases:
        path = tmp_path / filename
        path.write_bytes(contents)
        result = GGUFMetadataReader().read(path)
        assert result.issue is not None
        assert result.issue.code == issue_code
        assert result.issue.offset is not None


def test_enforces_header_string_and_array_limits(tmp_path: Path) -> None:
    header_path = tmp_path / "header-limit.gguf"
    header_path.write_bytes(_document([])[0])
    header_result = GGUFMetadataReader(max_header_bytes=23).read(header_path)
    assert header_result.issue.code == "header_size_limit_exceeded"

    string_path = tmp_path / "string-limit.gguf"
    string_path.write_bytes(_document([_entry("long-key", 4, _u32(1))])[0])
    string_result = GGUFMetadataReader(max_string_bytes=4).read(string_path)
    assert string_result.issue.code == "string_limit_exceeded"

    array_path = tmp_path / "array-limit.gguf"
    array = _u32(4) + _u64(3) + _u32(10) + _u32(20) + _u32(30)
    array_path.write_bytes(_document([_entry("values", 9, array)])[0])
    array_result = GGUFMetadataReader(max_array_items=2).read(array_path)
    assert array_result.issue.code == "array_item_limit_exceeded"


def test_validates_boolean_encodings_in_scalar_and_array_values(tmp_path: Path) -> None:
    scalar_path = tmp_path / "invalid-bool.gguf"
    scalar_path.write_bytes(_document([_entry("flag", 7, b"\x02")])[0])
    scalar_result = GGUFMetadataReader().read(scalar_path)
    assert scalar_result.issue.code == "invalid_boolean"

    array_path = tmp_path / "invalid-bool-array.gguf"
    array = _u32(7) + _u64(2) + b"\x00\x02"
    array_path.write_bytes(_document([_entry("flags", 9, array)])[0])
    array_result = GGUFMetadataReader(max_retained_array_items=1).read(array_path)
    assert array_result.issue.code == "invalid_boolean"


def test_rejects_non_regular_and_missing_paths(tmp_path: Path) -> None:
    missing = GGUFMetadataReader().read(tmp_path / "missing.gguf")
    assert missing.issue.code == "file_not_found"

    directory = GGUFMetadataReader().read(tmp_path)
    assert directory.issue.code == "not_a_regular_file"


def test_rejects_invalid_reader_limits() -> None:
    with pytest.raises(ValueError):
        GGUFMetadataReader(max_header_bytes=0)

    with pytest.raises(ValueError):
        GGUFMetadataReader(max_retained_array_items=-1)

    with pytest.raises(ValueError):
        GGUFMetadataReader(max_array_depth=0)
