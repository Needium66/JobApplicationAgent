"""Runs a specialist's ADK agent with auto-healing around the model.

  primary model --(transient errors: retry w/ backoff)--> ok
        | breaker open / model unavailable
        v
  fallback model --(retry)--> ok
        | still failing
        v
  raise -> the agent's step fails -> checkpoint keeps progress -> Pub/Sub / healer re-drive
"""
from __future__ import annotations

import logging
from typing import Callable

from google.adk.runners import InMemoryRunner
from google.genai import types

from .healing import CircuitBreaker, is_transient, retry_async

log = logging.getLogger("jobpilot.llm")


def _model_unavailable(exc: BaseException) -> bool:
    seen = 0
    while exc is not None and seen < 10:
        seen += 1
        text = str(exc).lower()
        if ("404" in text and "model" in text) or "not_found" in text or "permission_denied" in text:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def _run_once(agent, prompt: str, state: dict) -> dict:
    runner = InMemoryRunner(agent=agent, app_name="jobpilot")
    session = await runner.session_service.create_session(app_name="jobpilot", user_id="system", state=state)
    msg = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for ev in runner.run_async(user_id="system", session_id=session.id, new_message=msg):
        if ev.content and ev.content.parts and ev.is_final_response():
            final_text = "".join(p.text or "" for p in ev.content.parts)
    done = await runner.session_service.get_session(app_name="jobpilot", user_id="system", session_id=session.id)
    out = dict(done.state)
    out["_final_text"] = final_text
    return out


async def run_with_failover(rt, agent, prompt: str, state_fn: Callable[[], dict], case_id: str,
                            actor: str, parent: str) -> dict:
    s = rt.settings
    if rt.model_override is not None:  # tests / offline demo
        ov = rt.model_override(actor)
        models = ov if isinstance(ov, list) else [ov]
    else:
        models = list(dict.fromkeys([s.gemini_model, s.gemini_fallback_model]))
    last: BaseException | None = None
    for model in models:
        name = model if isinstance(model, str) else getattr(model, "model", "override")
        breaker = CircuitBreaker(rt.store, f"model:{name}", s.breaker_threshold, s.breaker_cooldown_s)
        if not breaker.allow():
            rt.graph.node(case_id, "step", f"skip {name} (circuit open)", actor, [parent], status="warn")
            continue
        candidate = agent.model_copy(update={"model": model})

        def on_retry(i, e, delay, _name=name):
            rt.graph.node(case_id, "step", f"retry {_name} #{i} in {delay:.1f}s", actor, [parent], status="warn",
                          meta={"error": str(e)[:300]})

        try:
            result = await retry_async(lambda: _run_once(candidate, prompt, state_fn()), attempts=3,
                                       base=s.retry_base_s, on_retry=on_retry)
            breaker.success()
            return result
        except Exception as e:  # noqa: BLE001
            if not (is_transient(e) or _model_unavailable(e)):
                raise
            breaker.failure()
            last = e
            rt.graph.node(case_id, "step", f"failover from {name}", actor, [parent], status="warn",
                          meta={"error": str(e)[:300]})
    raise last or RuntimeError("No model available (all circuits open).")
