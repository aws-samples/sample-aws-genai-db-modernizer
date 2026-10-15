"""The UI's engine display names must match ``ENGINE_DISPLAY_NAMES`` (#225).

Both come from the engine catalog (``src/catalog/engines/*.toml``, #492): Python loads
it directly and ``scripts/render_catalog.py`` writes the UI's copy into
``src/ui/src/utils/engineCatalog.generated.js``, which ``engineNames.js`` re-exports.
``tests/unit/shared/test_engine_catalog.py`` checks the generated module is fresh; this
test parses its object literal independently of the renderer and compares it with the
Python table, so a renderer bug cannot hide a drift either.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.shared.engine_names import ENGINE_DISPLAY_NAMES

UTILS = Path(__file__).resolve().parents[3] / "src" / "ui" / "src" / "utils"
JS_PATH = UTILS / "engineCatalog.generated.js"

_ENTRY_RE = re.compile(r"""^\s*(\w+):\s*'([^']*)',?\s*$""")


def _parse_engine_labels(js_text: str) -> dict[str, str]:
    start = js_text.index("ENGINE_LABELS = {")
    end = js_text.index("};", start)
    body = js_text[start:end]
    labels: dict[str, str] = {}
    for line in body.splitlines():
        m = _ENTRY_RE.match(line)
        if m:
            labels[m.group(1)] = m.group(2)
    return labels


class TestEngineNamesJsMatchesPython:
    def test_js_labels_match_engine_display_names(self):
        js_text = JS_PATH.read_text(encoding="utf-8")
        js_labels = _parse_engine_labels(js_text)
        assert (
            js_labels
        ), "failed to parse any ENGINE_LABELS entries from engineCatalog.generated.js"
        assert js_labels == ENGINE_DISPLAY_NAMES

    def test_extraction_sees_the_known_engines(self):
        # A regression guard on the parser itself: if this starts failing, the
        # regex above stopped matching the file's actual formatting.
        js_text = JS_PATH.read_text(encoding="utf-8")
        js_labels = _parse_engine_labels(js_text)
        assert js_labels["dynamodb"] == "DynamoDB"
        assert js_labels["aurora_mysql"] == "Aurora MySQL"


def test_engine_names_js_re_exports_the_generated_labels():
    text = (UTILS / "engineNames.js").read_text(encoding="utf-8")
    assert "import { ENGINE_LABELS } from './engineCatalog.generated';" in text
    assert "export { ENGINE_LABELS };" in text
