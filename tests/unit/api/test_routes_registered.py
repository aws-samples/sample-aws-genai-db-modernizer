"""Guard: every router module under src/api/routes is registered on the app.

Catches the failure where a route module defines a `router` but was never
`include_router()`'d in main.py (which shipped a 404 to production).

The check compares route *paths*, not router object identity. Identity is
fragile under the test suite: other tests `importlib.reload` route modules,
which makes a module's ``router`` a new object while ``app`` still holds the
original — an identity check then reports a false "not registered". Paths are
stable strings, so a module whose declared paths all appear on the app is
considered registered regardless of reloads or xdist worker ordering.
"""

import importlib
import pkgutil

from fastapi.routing import APIRoute

import src.api.routes
from src.api.main import app


def _router_modules():
    # mod.name is discovered from the src.api.routes package via pkgutil, not
    # from user input, so the dynamic import is safe.
    for mod in pkgutil.iter_modules(src.api.routes.__path__):
        module = importlib.import_module(  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
            f"src.api.routes.{mod.name}"
        )
        if hasattr(module, "router"):
            yield mod.name, module.router


def _app_paths() -> set[str]:
    """All APIRoute paths registered on the app.

    This FastAPI version stores each included router as a mount object that
    keeps the original ``APIRouter`` on ``original_router``; the flattened
    ``APIRoute``s are reachable through it, not at the top level of
    ``app.routes``. Collect paths from both places.
    """
    paths: set[str] = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            paths.add(route.path)
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                if isinstance(sub, APIRoute):
                    paths.add(sub.path)
    return paths


def test_all_router_modules_are_registered():
    """Every path declared by a route module must be registered on the app."""
    app_paths = _app_paths()
    missing = []
    for name, router in _router_modules():
        module_paths = {r.path for r in router.routes if isinstance(r, APIRoute)}
        if module_paths and not module_paths <= app_paths:
            missing.append(name)
    assert not missing, f"Route modules defined but not registered on app: {missing}"
