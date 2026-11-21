"""Private specialist-agent service (one per agent on Cloud Run) and the healer service.

Agents receive work only as Pub/Sub push deliveries on POST /events. Cloud Run IAM
lets only the push service account invoke them; the Defense Mesh additionally checks
the caller identity and that the event's actor is allowed to emit that event type.
Response codes drive Pub/Sub's retry/dead-letter behaviour:
  204 done / duplicate / not-for-me      -> ack
  429 busy (another instance leases it)  -> retry with backoff
  500 transient failure                  -> retry with backoff, then dead-letter
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ..core.bus import Event
from ..core.checkpoint import Busy
from ..core.healing import Healer
from ..core.mesh import MeshViolation
from ..core.runtime import Runtime

log = logging.getLogger("jobpilot.service")


def create_agent_app(rt: Runtime, specialist) -> FastAPI:
    app = FastAPI(title=f"JobPilot {specialist.name} agent", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "role": specialist.name}

    @app.post("/events")
    async def events(request: Request):
        try:
            rt.mesh.verify_service_caller(request.headers, rt.settings.push_invoker_sa)
        except MeshViolation as e:
            return JSONResponse(status_code=e.code, content={"error": str(e)})
        try:
            event, delivery = Event.from_push(await request.json())
        except Exception as e:  # noqa: BLE001 — malformed message: ack it so it doesn't loop forever
            log.error("Malformed push body dropped: %s", e)
            return Response(status_code=204)
        try:
            rt.mesh.check_emit(event.actor, event.type)  # forged/unauthorized events are dropped
        except MeshViolation as e:
            rt.mesh.record_block(event.case_id, event.actor, str(e), [])
            return Response(status_code=204)
        if not specialist.accepts(event):
            return Response(status_code=204)
        try:
            result = await specialist.handle(event)
            return JSONResponse(status_code=200, content={"result": result, "delivery": delivery})
        except Busy:
            return Response(status_code=429)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {str(e)[:200]}"})

    return app


def create_healer_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="JobPilot healer", docs_url=None, redoc_url=None)
    healer = Healer(rt)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "role": "healer"}

    @app.post("/heal")
    async def heal(request: Request):
        try:
            rt.mesh.verify_service_caller(request.headers, rt.settings.scheduler_sa)
        except MeshViolation as e:
            return JSONResponse(status_code=e.code, content={"error": str(e)})
        return await healer.heal()

    return app
