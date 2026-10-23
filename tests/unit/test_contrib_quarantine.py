"""contrib/ is a quarantined snapshot (#1412): the product never imports it, and the tools skip it.

contrib/cdc-elasticache-prototype/ is a third-party prototype kept for porting, not
built, imported or tested. Nothing that runs (src/, scripts/, ci/, infrastructure/,
pipeline/, the workflows, the Claude Code commands, the Makefile) may import from it
or point at it, in any file type; Python docstrings and comments may credit it, and the format, lint, type, test and packaging
configs must skip it while secret scanning still covers it.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
QUARANTINE = "contrib/cdc-elasticache-prototype"
# Everything that runs, builds or deploys: none of it may reach into contrib/.
PRODUCT_ROOTS = (
    "src",
    "scripts",
    "ci",
    "infrastructure",
    "pipeline",
    ".github/workflows",
    ".claude/commands",
    "Makefile",
)
_SKIP_DIRS = {"node_modules", "build", "__pycache__", ".venv", "dist", "coverage"}
# Text files of any type (Dockerfiles, .bash, .mjs, .cfg, .txt, ...): the top-level
# contrib directory named as a path or a module, not as part of a longer name.
_TEXT_REFERENCE = re.compile(
    r"(?:^|[^A-Za-z0-9_.-])contrib(?=[/\\.\"'`\s:;,)\]}]|$)|cdc-elasticache-prototype"
)


def _is_contrib_string(value: str) -> bool:
    """A Python string that names the contrib package or directory, alone or as a path."""
    if "cdc-elasticache-prototype" in value:
        return True
    if value == "contrib" or value.startswith(("contrib/", "contrib.", "contrib\\")):
        return True
    return re.search(r"(?:^|[/\\])contrib(?:[/\\]|$)", value) is not None


def _files(root: Path):
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not _SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The module, class and function docstring constants: they may credit contrib/."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _python_violations(tree: ast.AST, rel: str) -> list[str]:
    """Imports of contrib and string constants naming it; docstrings (attribution) and
    comments (not in the AST) may name the directory."""
    found: list[str] = []
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        line = getattr(node, "lineno", 0)
        found += [f"{rel}:{line}: imports {m}" for m in modules if m.split(".")[0] == "contrib"]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and _is_contrib_string(node.value):
                found.append(f"{rel}:{line}: points at contrib/ ({node.value!r})")
    return found


def _text_violations(text: str, rel: str) -> list[str]:
    return [
        f"{rel}:{lineno}: points at contrib/"
        for lineno, line in enumerate(text.splitlines(), 1)
        if _TEXT_REFERENCE.search(line)
    ]


def quarantine_violations(repo: Path) -> list[str]:
    """Every import of, or reference to, contrib/ in the files that run."""
    found: list[str] = []
    for name in PRODUCT_ROOTS:
        for path in _files(repo / name):
            rel = path.relative_to(repo).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary (images, fonts): cannot import or name a path
            if path.suffix == ".py":
                try:
                    found += _python_violations(ast.parse(text, filename=rel), rel)
                    continue
                except SyntaxError:
                    pass  # not parseable: fall back to the text scan
            found += _text_violations(text, rel)
    return found


@pytest.mark.passes_on_main
def test_product_code_never_imports_or_points_at_contrib() -> None:
    assert quarantine_violations(REPO) == []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        ("src/a.py", "import contrib\n"),
        ("src/a.py", "from contrib.cdc import sink\n"),
        ("scripts/a.py", "import contrib.cdc as c\n"),
        ("src/a.py", "import importlib\nimportlib.import_module('contrib')\n"),
        ("src/a.py", "importlib.import_module('contrib.cdc')\n"),
        ("src/a.py", "__import__('contrib')\n"),
        ("src/a.py", "import os, sys\nsys.path.append(os.path.join(ROOT, 'contrib'))\n"),
        ("src/a.py", "from pathlib import Path\np = Path(__file__).parents[2] / 'contrib' / 'x'\n"),
        ("src/a.py", "p = ROOT / '../contrib/x'\n"),
        ("src/a.py", "import sys\nsys.path.insert(0, 'contrib/cdc-elasticache-prototype/tools')\n"),
        ("scripts/a.py", "import runpy\nrunpy.run_path('contrib/x/quickstart.py')\n"),
        ("src/ui/src/a.js", "import x from '../../../contrib/cdc-elasticache-prototype/x';\n"),
        ("src/ui/src/a.mjs", "import x from '../../contrib/x.mjs';\n"),
        ("src/ui/src/a.cjs", "const x = require('../../contrib');\n"),
        ("scripts/a.sh", "bash contrib/cdc-elasticache-prototype/quickstart.sh\n"),
        ("scripts/a.bash", 'source "$ROOT/contrib/x.sh"\n'),
        ("infrastructure/docker/x/Dockerfile", "COPY contrib/ /app/contrib/\n"),
        ("src/setup.cfg", "[options]\npackages = contrib\n"),
        ("src/x.ini", "[paths]\nextra = ../contrib\n"),
        ("scripts/requirements.txt", "-e ./contrib/cdc-elasticache-prototype\n"),
        ("ci/a.sh", "cd contrib && make\n"),
        ("pipeline/a.yaml", "Path: contrib/x\n"),
        (".github/workflows/a.yml", "run: python contrib/x.py\n"),
        (".claude/commands/a.md", "Run `uv run python contrib/x.py`.\n"),
        ("Makefile", "demo:\n\tbash contrib/cdc-elasticache-prototype/quickstart.sh\n"),
        ("src/a.py", '"""Credits the prototype."""\nopen("contrib/cdc-elasticache-prototype/x")\n'),
        (
            "src/a.py",
            'def f():\n    """Doc."""\n    return Path(\'contrib/cdc-elasticache-prototype\')\n',
        ),
        ("src/a.py", "x = 1\n'contrib/cdc-elasticache-prototype'\n"),
        ("src/broken.py", "def (:\n  'contrib/x'\n"),
    ],
)
def test_the_guard_catches_an_import_or_a_path(tmp_path: Path, relpath: str, content: str) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert quarantine_violations(tmp_path) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        ("src/a.py", "from contributors import names\n"),
        ("src/a.py", "import contribution\n"),
        ("src/a.py", "x = 'see the contributing guide'\n"),
        ("src/a.py", "from .contrib import helper\n"),
        ("src/a.py", "x = 'contributor'\n"),
        ("scripts/a.sh", "pip install django-contrib-comments\n"),
        ("src/ui/src/a.js", "import x from './contributors';\n"),
        ("ci/a.md", "See CONTRIBUTING.md.\n"),
        (
            "src/a.py",
            '"""Follows the CDC prototype by Norbert Funke and Dinesh Kumar\n'
            '(contrib/cdc-elasticache-prototype); nothing here imports from it."""\n',
        ),
        ("src/a.py", 'class C:\n    """Ported from contrib/cdc-elasticache-prototype."""\n'),
        (
            "src/a.py",
            'def f():\n    """See contrib/x."""\n    # ported from contrib/x\n    return 1\n',
        ),
        ("src/a.py", 'async def f():\n    """See contrib/x."""\n'),
    ],
)
def test_the_guard_ignores_names_that_only_start_with_contrib(
    tmp_path: Path, relpath: str, content: str
) -> None:
    target = tmp_path / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    assert quarantine_violations(tmp_path) == []


def test_the_quarantine_directory_is_where_the_guard_looks() -> None:
    readme = REPO / QUARANTINE / "README.md"
    assert readme.is_file()
    assert "#1412" in readme.read_text(encoding="utf-8")


_SKIPPING_HOOKS = {
    "trailing-whitespace",
    "end-of-file-fixer",
    "check-yaml",
    "check-json",
    "mixed-line-ending",
    "black",
    "isort",
    "ruff",
    "mypy",
    "bandit",
    "markdownlint",
    "validate-json-schemas",
}
_SCANNING_HOOKS = {"detect-secrets", "detect-private-key", "check-added-large-files"}


def _hook_excludes() -> dict[str, list[str]]:
    """Each hook id's exclude patterns (a top-level exclude applies to every hook)."""
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    top = [config["exclude"]] if config.get("exclude") else []
    hooks: dict[str, list[str]] = {}
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            own = [hook["exclude"]] if hook.get("exclude") else []
            hooks.setdefault(hook["id"], []).append("|".join(f"(?:{p})" for p in top + own))
    return hooks


def _skips(exclude: str, path: str) -> bool:
    return bool(exclude) and re.search(exclude, path) is not None


@pytest.mark.parametrize("suffix", ["tools/observe.py", "README.md", "infra/cdc-ecs.yaml"])
def test_format_and_lint_hooks_skip_contrib_and_secret_scanning_does_not(suffix: str) -> None:
    path = f"{QUARANTINE}/{suffix}"
    hooks = _hook_excludes()
    for hook_id in _SKIPPING_HOOKS:
        assert all(_skips(e, path) for e in hooks[hook_id]), hook_id
    for hook_id in _SCANNING_HOOKS:
        assert not any(_skips(e, path) for e in hooks[hook_id]), hook_id


def test_tool_configs_skip_contrib() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    tool = pyproject["tool"]
    assert "contrib" in tool["ruff"]["extend-exclude"]
    assert re.search(tool["black"]["extend-exclude"], "/contrib/x.py")
    assert "contrib/*" in tool["isort"]["extend_skip_glob"]
    assert any(re.search(p, f"{QUARANTINE}/x.py") for p in tool["mypy"]["exclude"])
    assert "contrib" in tool["bandit"]["exclude_dirs"]
    assert "--ignore=contrib" in tool["pytest"]["ini_options"]["addopts"].split()
    assert "/contrib" in tool["hatch"]["build"]["targets"]["sdist"]["exclude"]
    assert tool["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src"]
    semgrepignore = (REPO / ".semgrepignore").read_text(encoding="utf-8").splitlines()
    assert "contrib/" in semgrepignore
    dockerignore = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "contrib/" in dockerignore
