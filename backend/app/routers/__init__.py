"""Router registry. Feature routers are added here as modules land."""

from __future__ import annotations

from fastapi import FastAPI


def register_all(app: FastAPI) -> None:
    from . import agents, catalog, clusters, datasets, meta, resources, runs, serving, setup

    for module in (meta, setup, catalog, clusters, agents, datasets, runs, resources):
        app.include_router(module.router)
    app.include_router(clusters.plans)
    app.include_router(agents.templates)
    app.include_router(runs.images)
    for r in (serving.exports, serving.endpoints, serving.evals):
        app.include_router(r)
