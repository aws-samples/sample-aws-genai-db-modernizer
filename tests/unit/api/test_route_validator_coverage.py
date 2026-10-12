"""Structural guard: walks ``app.routes`` and asserts that every route
whose path has a ``{job_id}`` segment depends on ``job_id_param``
(directly or through a nested dependency such as ``get_graph_for_job``),
and that no route declares a raw ``database_name``, ``db_name``, or
``filename`` parameter of its own -- every one of those must come from a
``Depends(...)`` wrapper (``database_name_param``, ``validated_filename``),
not a bare ``Query``/path parameter on the endpoint itself.

This is the regression guard #420/#505/#508 were missing: a future route
that reintroduces a bare ``job_id: str`` or ``database_name: str`` (forgets
the ``Depends(...)``) would otherwise only be caught by remembering to add
it to the per-route test tables by hand.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from src.api.dependencies import database_name_param, job_id_param, validated_filename
from src.api.main import app

_UNVALIDATED_PARAM_NAMES = {"database_name", "db_name", "filename"}


def _all_api_routes() -> list[APIRoute]:
    """Every APIRoute registered on the app.

    This FastAPI version stores each included router as a mount object that
    keeps the original APIRouter on ``original_router``; the flattened
    APIRoutes are reachable through it, not at the top level of
    ``app.routes`` (see test_routes_registered.py)."""
    routes: list[APIRoute] = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            routes.append(route)
        original = getattr(route, "original_router", None)
        if original is not None:
            for sub in getattr(original, "routes", []):
                if isinstance(sub, APIRoute):
                    routes.append(sub)
    return routes


def _dependency_callables(route: APIRoute) -> set:
    """Every callable reachable from this route's dependant, recursively
    (its own Depends(...) params, and every nested sub-dependency's own
    Depends(...) params)."""
    seen: set = set()

    def _walk(dependant) -> None:
        for sub in dependant.dependencies:
            if sub.call is not None:
                seen.add(sub.call)
            _walk(sub)

    _walk(route.dependant)
    return seen


def _own_param_names(route: APIRoute) -> set[str]:
    """Path/query parameter names declared directly on this route's own
    dependant -- i.e. NOT behind a Depends(...) wrapper. A parameter
    resolved through Depends(...) only ever appears on the *sub*-dependant
    belonging to the dependency callable, never here."""
    d = route.dependant
    return {p.name for p in d.path_params} | {p.name for p in d.query_params}


_JOB_ID_ROUTES = [route for route in _all_api_routes() if "{job_id}" in route.path]
_ALL_ROUTES = _all_api_routes()


def test_every_job_id_route_was_found() -> None:
    """Sanity check on the walk itself: if route discovery broke, every
    other test in this file would trivially pass over zero routes."""
    assert len(_JOB_ID_ROUTES) >= 30, (
        f"expected at least 30 {{job_id}} routes, found {len(_JOB_ID_ROUTES)} "
        "-- route discovery may be broken"
    )


def test_every_job_id_route_depends_on_job_id_param() -> None:
    missing = [
        f"{route.methods} {route.path}"
        for route in _JOB_ID_ROUTES
        if job_id_param not in _dependency_callables(route)
    ]
    assert not missing, "routes with {job_id} but no job_id_param dependency:\n" + "\n".join(
        missing
    )


def test_no_route_declares_a_raw_unvalidated_param() -> None:
    offenders = [
        f"{route.methods} {route.path}: {sorted(_own_param_names(route) & _UNVALIDATED_PARAM_NAMES)}"
        for route in _ALL_ROUTES
        if _own_param_names(route) & _UNVALIDATED_PARAM_NAMES
    ]
    assert not offenders, (
        "routes with a raw database_name/db_name/filename param "
        "(should go through Depends(...)):\n" + "\n".join(offenders)
    )


def test_every_database_name_route_depends_on_database_name_param() -> None:
    """A route can only reach "database_name" as a usable local variable
    through database_name_param (the previous test rules out a raw one);
    this just confirms the dependency used for the routes that need it is
    actually that function, not some other callable that happens to also
    accept a parameter named database_name."""
    routes_needing_it = [
        route
        for route in _ALL_ROUTES
        if any(sub.name == "database_name" for sub in _all_subparam_names(route))
    ]
    assert routes_needing_it, "expected at least one route resolving database_name"
    missing = [
        f"{route.methods} {route.path}"
        for route in routes_needing_it
        if database_name_param not in _dependency_callables(route)
    ]
    assert not missing, "routes resolving database_name without database_name_param:\n" + "\n".join(
        missing
    )


def _all_subparam_names(route: APIRoute):
    """Every path/query ModelField across every nested sub-dependant."""
    fields = []

    def _walk(dependant) -> None:
        fields.extend(dependant.path_params)
        fields.extend(dependant.query_params)
        for sub in dependant.dependencies:
            _walk(sub)

    _walk(route.dependant)
    return fields


def test_delete_upload_filename_depends_on_validated_filename() -> None:
    routes = [route for route in _ALL_ROUTES if route.path.endswith("/uploads/{filename}")]
    assert routes, "expected the delete_upload route to be registered"
    for route in routes:
        assert validated_filename in _dependency_callables(route), route.path
