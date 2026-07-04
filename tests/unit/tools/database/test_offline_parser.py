"""Unit tests for offline_parser.py.

Focused on hardening changes from the Oracle production JSON test run:
- Whitespace-tolerant sentinel-object regex (handles both inline and
  DBMS_OUTPUT-style multi-line emission).
"""

import json
from unittest.mock import MagicMock, patch


class TestSentinelRegex:
    """The two `.replace()` calls previously used could not handle
    Oracle's DBMS_OUTPUT.PUT_LINE emission style, which puts the sentinel
    object on its own line (,\\n{"_sentinel": true}\\n). The new
    whitespace-tolerant regex must handle both forms.
    """

    @staticmethod
    def _run_fetch(body_text: str) -> dict:
        """Invoke fetch_offline_json with a mocked S3 body."""
        from src.tools.database.offline_parser import fetch_offline_json

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: body_text.encode("utf-8"))
        }
        with patch("src.tools.database.offline_parser.boto3.client", return_value=mock_s3):
            return fetch_offline_json("bucket", "key")

    def test_inline_sentinel_stripped(self):
        content = '{"tables":[{"table_name":"t1"},{"_sentinel":true}]}'
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_inline_sentinel_with_space_stripped(self):
        # Legacy form the old code handled
        content = '{"tables":[{"table_name":"t1"},{"_sentinel": true}]}'
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_multiline_sentinel_stripped(self):
        """Oracle DBMS_OUTPUT emits this multi-line pattern."""
        content = '{"tables":[\n' '{"table_name":"t1"},\n' '{"_sentinel": true}\n' "]}"
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_sentinel_only_element_stripped(self):
        """Real Oracle emission pattern for an empty result set: the
        sentinel is the ONLY element in the array. The regex must
        handle this too (no leading comma).
        """
        content = '{"triggers":[\n{"_sentinel": true}\n]}'
        result = self._run_fetch(content)
        assert result["triggers"] == []

    def test_nested_object_with_sentinel_field_preserved(self):
        """Guard against false-positive: a legitimate nested object that
        happens to have a `_sentinel: true` field (not at end of array)
        must NOT be stripped.
        """
        content = '{"config":{"_sentinel":true}}'
        result = self._run_fetch(content)
        # Nested object preserved — lookahead requires `]` after sentinel
        assert result["config"] == {"_sentinel": True}

    def test_control_chars_stripped(self):
        """Oracle 19c JSON_OBJECT does not escape control chars — the
        parser strips them (except \\n) before json.loads. Unchanged
        behavior, but sanity-check the ordering: control-char strip must
        happen before the sentinel regex.
        """
        # \x00 embedded in a string value would break json.loads
        content = '{"tables":[{"table_name":"t\x001"}]}'
        result = self._run_fetch(content)
        # Control char became space, JSON still parses
        assert result["tables"] == [{"table_name": "t 1"}]

    def test_multiple_sentinels_across_sections(self):
        """Real Oracle output has one sentinel per array (tables, columns,
        indexes, ...). All should be stripped in a single pass.
        """
        content = (
            "{"
            '"tables":[{"table_name":"t1"},\n{"_sentinel": true}\n],'
            '"columns":[{"table_name":"t1","column_name":"c1"},\n{"_sentinel": true}\n],'
            '"triggers":[\n{"_sentinel": true}\n]'
            "}"
        )
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]
        assert result["columns"] == [{"table_name": "t1", "column_name": "c1"}]
        assert result["triggers"] == []

    def test_produces_valid_json(self):
        """After sentinel stripping, the content must be valid JSON."""
        content = '{"tables":[\n' '{"table_name":"t1"},\n' '{"_sentinel": true}\n' "]}"
        result = self._run_fetch(content)
        # Round-trip through json to prove it's a real dict
        assert json.loads(json.dumps(result))["tables"] == [{"table_name": "t1"}]
