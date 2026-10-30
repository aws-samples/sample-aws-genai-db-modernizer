# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for scripts/license_headers.py (#1722).

The tool adds the two-line SPDX header to every source file (``--fix``) and lists
the files without it (``--check``, exit 1). These tests pin the placement rules
(after a shebang and a Python encoding line, before a JS directive prologue, one
blank line before the code), idempotence, line-ending preservation and the
exclusions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import license_headers as mod

C = "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
S = "SPDX-License-Identifier: MIT-0"
PY_HEADER = f"# {C}\n# {S}\n"
JS_HEADER = f"// {C}\n// {S}\n"


# ---------------------------------------------------------------------------
# add_header: placement
# ---------------------------------------------------------------------------


def test_python_file_gets_header_then_one_blank_line() -> None:
    src = '"""Doc."""\n\nimport os\n'
    assert mod.add_header(src, "src/a.py") == PY_HEADER + "\n" + src


def test_shell_file_uses_hash_comments() -> None:
    src = "set -euo pipefail\n"
    assert mod.add_header(src, "ci/x.sh") == PY_HEADER + "\n" + src


def test_sql_file_uses_double_dash_comments() -> None:
    src = "-- Collection script\nSELECT 1;\n"
    assert mod.add_header(src, "scripts/collect.sql") == f"-- {C}\n-- {S}\n\n" + src


@pytest.mark.parametrize("ext", [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"])
def test_js_family_uses_slash_comments(ext: str) -> None:
    src = "export const x = 1;\n"
    assert mod.add_header(src, f"src/ui/src/a{ext}") == JS_HEADER + "\n" + src


def test_shebang_stays_first() -> None:
    src = '#!/usr/bin/env python3\n"""Doc."""\n'
    assert mod.add_header(src, "scripts/a.py") == (
        "#!/usr/bin/env python3\n" + PY_HEADER + '\n"""Doc."""\n'
    )


def test_shebang_then_blank_line_keeps_exactly_one_blank_line() -> None:
    src = "#!/usr/bin/env bash\n\n\nset -e\n"
    assert mod.add_header(src, "ci/a.sh") == "#!/usr/bin/env bash\n" + PY_HEADER + "\nset -e\n"


@pytest.mark.parametrize(
    "coding", ["# -*- coding: utf-8 -*-\n", "# coding: utf-8\n", "# coding=latin-1\n"]
)
def test_python_encoding_line_stays_before_header(coding: str) -> None:
    src = "#!/usr/bin/env python3\n" + coding + "import os\n"
    assert mod.add_header(src, "scripts/a.py") == (
        "#!/usr/bin/env python3\n" + coding + PY_HEADER + "\nimport os\n"
    )


def test_python_encoding_line_without_shebang() -> None:
    src = "# -*- coding: utf-8 -*-\nimport os\n"
    assert mod.add_header(src, "src/a.py") == (
        "# -*- coding: utf-8 -*-\n" + PY_HEADER + "\nimport os\n"
    )


def test_encoding_like_comment_in_shell_is_not_special() -> None:
    src = "# coding: utf-8\necho hi\n"
    assert mod.add_header(src, "ci/a.sh") == PY_HEADER + "\n" + src


def test_js_header_goes_before_use_strict_directive() -> None:
    src = "'use strict';\n\nconst x = 1;\n"
    out = mod.add_header(src, "src/ui/scripts/start.js")
    assert out == JS_HEADER + "\n" + src
    # The directive is still the first statement: only comments come before it.
    code_lines = [ln for ln in out.splitlines() if ln and not ln.startswith("//")]
    assert code_lines[0] == "'use strict';"


def test_js_shebang_stays_first() -> None:
    src = "#!/usr/bin/env node\n'use strict';\n"
    assert mod.add_header(src, "src/ui/scripts/a.js") == (
        "#!/usr/bin/env node\n" + JS_HEADER + "\n'use strict';\n"
    )


def test_empty_file_gets_only_the_header() -> None:
    assert mod.add_header("", "src/pkg/__init__.py") == PY_HEADER


def test_shebang_only_file_without_newline() -> None:
    assert mod.add_header("#!/bin/sh", "ci/a.sh") == "#!/bin/sh\n" + PY_HEADER


# ---------------------------------------------------------------------------
# Line endings, trailing newline, existing header, idempotence
# ---------------------------------------------------------------------------


def test_crlf_file_keeps_crlf_line_endings() -> None:
    src = "#!/usr/bin/env python3\r\nimport os\r\n"
    out = mod.add_header(src, "scripts/a.py")
    assert out == f"#!/usr/bin/env python3\r\n# {C}\r\n# {S}\r\n\r\nimport os\r\n"
    assert "\n" not in out.replace("\r\n", "")


def test_missing_trailing_newline_is_kept_missing() -> None:
    src = "import os"
    out = mod.add_header(src, "src/a.py")
    assert out == PY_HEADER + "\nimport os"


def test_file_with_spdx_line_is_left_alone() -> None:
    src = "# Copyright Someone Else\n# SPDX-License-Identifier: Apache-2.0\n\nimport os\n"
    assert mod.has_header(src, "src/a.py")
    assert mod.add_header(src, "src/a.py") == src


def test_add_header_is_idempotent() -> None:
    for path, src in [
        ("scripts/a.py", "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nimport os\n"),
        ("src/ui/src/a.js", "'use strict';\nconst a = 1;\n"),
        ("ci/a.sh", "#!/bin/bash\r\necho hi\r\n"),
        ("src/a.py", ""),
        ("src/a.py", "\ufeffimport os\n"),
        ("src/ui/src/a.test.js", "/**\n * @jest-environment node\n */\nconst a = 1;\n"),
    ]:
        once = mod.add_header(src, path)
        assert mod.has_header(once, path)
        assert mod.spdx_identifier(once, path) == "MIT-0"
        assert mod.add_header(once, path) == once


def test_spdx_far_below_the_top_does_not_count_as_header() -> None:
    src = "import os\n" * 40 + 'X = "SPDX-License-Identifier: MIT-0"\n'
    assert not mod.has_header(src, "src/a.py")


# ---------------------------------------------------------------------------
# Only the leading comment block counts (review of #1728)
# ---------------------------------------------------------------------------

DOCSTRING_PROBE = (
    '"""Explain the header.\n\n'
    "    # Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.\n"
    "    # SPDX-License-Identifier: MIT-0\n"
    '"""\n\nimport os\n'
)


def test_spdx_text_in_a_docstring_is_not_a_header() -> None:
    assert not mod.has_header(DOCSTRING_PROBE, "src/a.py")
    assert mod.add_header(DOCSTRING_PROBE, "src/a.py") == PY_HEADER + "\n" + DOCSTRING_PROBE


def test_check_fails_a_file_whose_only_spdx_text_is_in_its_docstring(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(DOCSTRING_PROBE, encoding="utf-8")
    assert mod.main(["--check", "--root", str(tmp_path), "src/a.py"]) == 1


def test_spdx_text_in_a_js_string_is_not_a_header() -> None:
    src = 'const s = "// SPDX-License-Identifier: MIT-0";\n'
    assert not mod.has_header(src, "src/ui/src/a.js")


def test_spdx_after_other_leading_comments_counts() -> None:
    src = "#!/bin/bash\n# Tool notes.\n\n# SPDX-License-Identifier: MIT-0\necho hi\n"
    assert mod.spdx_identifier(src, "ci/a.sh") == "MIT-0"


def test_spdx_in_a_leading_js_block_comment_counts() -> None:
    src = "/*\n * SPDX-License-Identifier: MIT-0\n */\nconst a = 1;\n"
    assert mod.spdx_identifier(src, "src/ui/src/a.js") == "MIT-0"


# ---------------------------------------------------------------------------
# Only MIT-0 passes --check
# ---------------------------------------------------------------------------


def test_check_fails_another_spdx_identifier_and_fix_adds_no_second_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "a.py"
    text = "# SPDX-License-Identifier: GPL-3.0-only\n\nimport os\n"
    f.write_text(text, encoding="utf-8")
    assert mod.main(["--check", "--root", str(tmp_path), "src/a.py"]) == 1
    assert "GPL-3.0-only, expected MIT-0" in capsys.readouterr().out
    mod.main(["--fix", "--root", str(tmp_path), "src/a.py"])
    assert f.read_text(encoding="utf-8") == text
    assert mod.main(["--check", "--root", str(tmp_path), "src/a.py"]) == 1


# ---------------------------------------------------------------------------
# Byte order mark and JS pragma docblocks (review of #1728)
# ---------------------------------------------------------------------------


def test_bom_stays_first_and_header_follows_it() -> None:
    src = "\ufeff#!/usr/bin/env python3\nimport os\n"
    out = mod.add_header(src, "scripts/a.py")
    assert out == "\ufeff#!/usr/bin/env python3\n" + PY_HEADER + "\nimport os\n"
    assert out.count("\ufeff") == 1


def test_bom_file_on_disk_keeps_its_bom(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    f = tmp_path / "src" / "a.py"
    f.write_bytes(b"\xef\xbb\xbfimport os\n")
    assert mod.main(["--fix", "--root", str(tmp_path), "src/a.py"]) == 0
    assert f.read_bytes() == b"\xef\xbb\xbf" + (PY_HEADER + "\nimport os\n").encode()
    assert mod.main(["--check", "--root", str(tmp_path), "src/a.py"]) == 0


@pytest.mark.parametrize(
    "docblock",
    [
        "/**\n * @jest-environment node\n */\n",
        "/** @jest-environment jsdom */\n",
        '/**\n * @jest-environment-options {"url": "http://localhost/"}\n */\n',
        "/* @flow */\n",
        "/** @jsx h */\n",
        "/** @jsxImportSource preact */\n",
    ],
)
def test_a_leading_pragma_docblock_stays_first(docblock: str) -> None:
    src = docblock + "\nconst a = 1;\n"
    assert mod.add_header(src, "src/ui/src/__tests__/a.test.js") == (
        docblock + JS_HEADER + "\nconst a = 1;\n"
    )


def test_a_docblock_without_a_pragma_goes_below_the_header() -> None:
    src = "/**\n * Module notes.\n */\nconst a = 1;\n"
    assert mod.add_header(src, "src/ui/src/a.js") == JS_HEADER + "\n" + src


def test_a_pragma_docblock_after_code_is_not_moved() -> None:
    src = "const a = 1;\n/** @jest-environment node */\n"
    assert mod.add_header(src, "src/ui/src/a.test.js") == JS_HEADER + "\n" + src


def test_the_jest_test_in_the_repo_keeps_its_environment_docblock_first() -> None:
    text = (mod.REPO_ROOT / "src/ui/src/__tests__/devServerConfig.test.js").read_text(
        encoding="utf-8"
    )
    assert text.startswith("/**\n * @jest-environment node\n */\n" + JS_HEADER)


# ---------------------------------------------------------------------------
# Which files are in scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/agents/a.py",
        "src/ui/src/App.js",
        "ci/lint.sh",
        "scripts/collect-mysql.sql",
        "tests/unit/test_x.py",
        "benchmarks/runner/run.py",
    ],
)
def test_source_files_are_in_scope(path: str) -> None:
    assert mod.in_scope(path)


@pytest.mark.parametrize(
    "path",
    [
        "contrib/cdc-elasticache-prototype/tools/go-sink/main.go",
        "contrib/cdc-elasticache-prototype/examples/my-pipeline/setup/seed.py",
        "contrib/cdc-elasticache-prototype/quickstart.sh",
        "src/ui/node_modules/react/index.js",
        ".venv/lib/python3.12/site-packages/x.py",
        "src/ui/build/static/js/main.js",
        "README.md",
        "pyproject.toml",
        "src/catalog/engines/dynamodb.toml",
        "src/agents/load_test/dynamodb/templates/main.js.j2",
    ],
)
def test_excluded_and_non_source_paths_are_out_of_scope(path: str) -> None:
    assert not mod.in_scope(path)


def test_every_listed_exclusion_has_a_reason() -> None:
    for path, reason in mod.EXCLUDED_FILES.items():
        assert path and reason.strip(), path


def test_generated_file_is_never_edited_by_fix(tmp_path: Path) -> None:
    gen = tmp_path / "src" / "gen.js"
    gen.parent.mkdir(parents=True)
    text = "// GENERATED by scripts/x.py -- do not edit.\nexport const A = 1;\n"
    gen.write_text(text, encoding="utf-8")
    assert mod.main(["--fix", "--root", str(tmp_path), "src/gen.js"]) == 0
    assert gen.read_text(encoding="utf-8") == text
    # ...but --check still reports it, so its generator must emit the header.
    assert mod.main(["--check", "--root", str(tmp_path), "src/gen.js"]) == 1


# ---------------------------------------------------------------------------
# main: --check exit codes and --fix on disk
# ---------------------------------------------------------------------------


def _tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "contrib").mkdir()
    (root / "src" / "a.py").write_text("import os\n", encoding="utf-8")
    (root / "src" / "b.js").write_text(JS_HEADER + "\nconst b = 1;\n", encoding="utf-8")
    (root / "src" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (root / "contrib" / "c.py").write_text("import sys\n", encoding="utf-8")


def test_check_exits_1_and_lists_files_without_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path)
    paths = ["src/a.py", "src/b.js", "src/notes.md", "contrib/c.py"]
    assert mod.main(["--check", "--root", str(tmp_path), *paths]) == 1
    out = capsys.readouterr().out
    assert "src/a.py" in out
    assert "src/b.js" not in out
    assert "contrib/c.py" not in out
    assert "notes.md" not in out


def test_check_exits_0_when_every_file_has_header(tmp_path: Path) -> None:
    _tree(tmp_path)
    assert mod.main(["--check", "--root", str(tmp_path), "src/b.js", "contrib/c.py"]) == 0


def test_fix_writes_headers_then_check_passes_and_second_fix_changes_nothing(
    tmp_path: Path,
) -> None:
    _tree(tmp_path)
    paths = ["src/a.py", "src/b.js", "src/notes.md", "contrib/c.py"]
    assert mod.main(["--fix", "--root", str(tmp_path), *paths]) == 0
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == (PY_HEADER + "\nimport os\n")
    assert (tmp_path / "contrib" / "c.py").read_text(encoding="utf-8") == "import sys\n"
    assert (tmp_path / "src" / "notes.md").read_text(encoding="utf-8") == "# notes\n"
    snapshot = {p: (tmp_path / p).read_bytes() for p in paths}
    assert mod.main(["--check", "--root", str(tmp_path), *paths]) == 0
    assert mod.main(["--fix", "--root", str(tmp_path), *paths]) == 0
    assert {p: (tmp_path / p).read_bytes() for p in paths} == snapshot


def test_fix_preserves_crlf_bytes_on_disk(tmp_path: Path) -> None:
    (tmp_path / "ci").mkdir()
    f = tmp_path / "ci" / "a.sh"
    f.write_bytes(b"#!/bin/bash\r\necho hi\r\n")
    assert mod.main(["--fix", "--root", str(tmp_path), "ci/a.sh"]) == 0
    assert f.read_bytes() == (f"#!/bin/bash\r\n# {C}\r\n# {S}\r\n\r\necho hi\r\n").encode()


def test_check_and_fix_are_mutually_exclusive_and_one_is_required() -> None:
    with pytest.raises(SystemExit):
        mod.main([])
    with pytest.raises(SystemExit):
        mod.main(["--check", "--fix"])


def test_header_matches_the_license_copyright_line() -> None:
    license_text = (mod.REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert mod.HEADER_LINES[0] in license_text.splitlines()
    assert mod.HEADER_LINES == (C, S)
