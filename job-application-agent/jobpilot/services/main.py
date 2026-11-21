"""Cloud Run entry point. One image, six services; SERVICE_ROLE picks which one this is:
edge | ingestion | evaluation | cover_letter | application | healer
"""
from __future__ import annotations

import logging
import os

import uvicorn

from ..agents.application import all_specialists
from ..core.runtime import get_runtime
from ..settings import get_settings
from .agent_service import create_agent_app, create_healer_app
from .edge import create_edge_app

logging.basicConfig(level=logging.INFO, format="%(message)s")


def build_app():
    settings = get_settings()
    settings.validate()
    rt = get_runtime()
    role = settings.service_role
    if role == "edge":
        return create_edge_app(rt)
    if role == "healer":
        return create_healer_app(rt)
    for specialist in all_specialists(rt):
        if specialist.name == role:
            return create_agent_app(rt, specialist)
    raise SystemExit(f"Unknown SERVICE_ROLE '{role}'")


app = build_app() if os.getenv("K_SERVICE") or os.getenv("SERVICE_ROLE") not in (None, "", "local") else None

if __name__ == "__main__":
    uvicorn.run(app or build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
