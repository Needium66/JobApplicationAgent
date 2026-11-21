"""Run the whole Level 2 system in one process (no Pub/Sub / Firestore needed).

    python -m jobpilot.local            # real Gemini via your gcloud credentials
    python -m jobpilot.local --demo     # fully offline with a scripted model

Open http://localhost:8080. Every specialist, the message board, shared memory,
checkpoints, the causal graph, the Defense Mesh and the healer run exactly as in
the cloud, but on an in-memory store and an in-process event bus.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os

import uvicorn

from .agents.application import all_specialists
from .core.healing import Healer
from .core.runtime import Runtime, set_runtime
from .settings import reset_settings


def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        from dotenv import load_dotenv
        load_dotenv(path, override=False)


def build_local(demo: bool = False) -> Runtime:
    os.environ.setdefault("BACKEND", "local")
    os.environ.setdefault("AUTH_MODE", "dev")
    settings = reset_settings()
    settings.backend, settings.auth_mode = "local", "dev"
    rt = set_runtime(Runtime(settings))
    if demo:
        from .testing.scripted_llm import ScriptedLlm
        rt.model_override = lambda name: ScriptedLlm(agent_name=name)
    for sp in all_specialists(rt):
        rt.bus.subscribe(sp.name, sp.handle, sp.accepts)
    return rt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="offline scripted model instead of Gemini")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args()
    _load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    rt = build_local(args.demo)

    from .services.edge import create_edge_app
    app = create_edge_app(rt)

    async def pump():
        healer, tick = Healer(rt), 0
        while True:
            await rt.bus.drain()
            tick += 1
            if tick % 120 == 0:  # ~every 60s
                await healer.heal()
            await asyncio.sleep(0.5)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(pump())
        yield
        task.cancel()

    app.router.lifespan_context = lifespan
    print(f"JobPilot Level 2 (local{' demo' if args.demo else ''}) on http://localhost:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
