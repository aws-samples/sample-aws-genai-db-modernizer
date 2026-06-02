"""
Property-based tests for the storage abstraction layer.

Tests correctness property from the design document:
- Property 14: Storage Abstraction Equivalence — LocalArtifactStore.read_json(path)
  returns the same data as was written via write_json(path, data) for any valid JSON dict.

**Validates: Requirements 8.1, 8.4**
"""

from __future__ import annotations

import tempfile

from hypothesis import given
from hypothesis import strategies as st

from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-safe values (no NaN/Inf which aren't valid JSON)
_json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**53), max_value=2**53),
    st.text(min_size=0, max_size=50),
)

_json_values = st.recursive(
    _json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=20), children, max_size=5),
    ),
    max_leaves=15,
)

_json_dicts = st.dictionaries(
    st.text(min_size=1, max_size=20),
    _json_values,
    min_size=1,
    max_size=8,
)

# Safe path segments — alphanumeric with hyphens, no leading dots or slashes
_path_segment = st.from_regex(r"[a-z][a-z0-9\-]{0,9}", fullmatch=True)

_artifact_path = st.builds(
    lambda parts: "/".join(parts) + ".json",
    st.lists(_path_segment, min_size=1, max_size=3),
)


# ---------------------------------------------------------------------------
# Property 14: Storage Abstraction Equivalence — write/read round-trip
# ---------------------------------------------------------------------------


class TestStorageRoundTrip:
    """**Validates: Requirements 8.1, 8.4**

    Property 14: LocalArtifactStore.read_json(path) returns the same data
    as was written via write_json(path, data) for any valid JSON dict.
    """

    @given(data=_json_dicts, path=_artifact_path)
    def test_write_then_read_round_trip(self, data: dict, path: str) -> None:
        """write_json followed by read_json returns identical data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            store.write_json(path, data)
            result = store.read_json(path)
            assert result == data

    @given(data=_json_dicts, path=_artifact_path)
    def test_exists_true_after_write(self, data: dict, path: str) -> None:
        """exists returns True after write_json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            store.write_json(path, data)
            assert store.exists(path) is True

    @given(path=_artifact_path)
    def test_exists_false_before_write(self, path: str) -> None:
        """exists returns False for a path that has not been written."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            assert store.exists(path) is False

    @given(
        data=_json_dicts,
        segments=st.lists(_path_segment, min_size=2, max_size=4),
    )
    def test_list_prefix_returns_written_paths(self, data: dict, segments: list[str]) -> None:
        """list_prefix returns paths that were written under a directory prefix."""
        path = "/".join(segments) + ".json"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            store.write_json(path, data)
            # Use the first segment as prefix directory
            prefix = segments[0]
            listed = store.list_prefix(prefix)
            assert path in listed

    @given(
        data=_json_dicts,
        segments=st.lists(_path_segment, min_size=2, max_size=4),
    )
    def test_write_json_creates_parent_directories(self, data: dict, segments: list[str]) -> None:
        """write_json creates parent directories automatically."""
        path = "/".join(segments) + ".json"
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(base_dir=tmpdir)
            # Should not raise even though parent dirs don't exist
            store.write_json(path, data)
            assert store.exists(path) is True
            assert store.read_json(path) == data
