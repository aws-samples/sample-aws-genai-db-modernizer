"""Guard: every router module under src/api/routes is registered on the app.

Catches the failure where a route module defines a `router` but was never
`include_router()`'d in main.py (which shipped a 404 to production).

This checks router-object identity rather than flattened paths: FastAPI stores
each included router as a mount that keeps a reference to the original
``APIRouter`` via ``original_router``. A module whose ``router`` is not among
those references was never registered.
"""

import importlib
import pkgutil

import src.api.routes
from src.api.main import app


def _router_modules():
    for mod in pkgutil.iter_modules(src.api.routes.__path__):
        module = importlib.import_module(f"src.api.routes.{mod.name}")
        if hasattr(module, "router"):
            yield mod.name, module.router


def _registered_router_ids() -> set[int]:
    ids: set[int] = set()
    for route in app.routes:
        original = getattr(route, "original_router", None)
        if original is not None:
            ids.add(id(original))
    return ids


def test_all_router_modules_are_registered():
    registered = _registered_router_ids()
    missing = [name for name, router in _router_modules() if id(router) not in registered]
    assert not missing, f"Route modules defined but not registered on app: {missing}"
