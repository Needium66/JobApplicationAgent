"""Base class for decentralized specialist agents.

A specialist:
  * is triggered only by events it subscribes to (no central orchestrator)
  * reads shared memory + the message board, does its job in checkpointed steps,
    writes its results to shared memory, posts to the board, and emits the next event
  * runs its LLM work through its own ADK agent with its own Instruction Contract
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Awaitable, Callable

from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.genai import types

from ..core.bus import Event
from ..core.checkpoint import Busy, Checkpoint, GiveUp
from ..core.llm import run_with_failover
from ..core.mesh import MeshViolation
from ..core.runtime import Runtime
from .contracts import CONTRACTS, render
from .tools import audit_tool_call, fcot_record_iteration, make_guard

log = logging.getLogger("jobpilot.agents")


class PermanentError(Exception):
    """Business/policy failure: retrying will not help. The case is failed and the event acknowledged."""


class Ctx:
    """Per-run context handed to a specialist's run()."""

    def __init__(self, rt: Runtime, agent: "Specialist", event: Event, cp: Checkpoint, run_node: str):
        self.rt, self.agent, self.event, self.cp = rt, agent, event, cp
        self.case_id = event.case_id
        self.cursor = run_node  # latest causal node; next nodes hang off it

    def mem(self, key: str, default=None):
        return self.rt.memory.read(self.case_id, key, default)

    async def step(self, name: str, fn: Callable[[], Any | Awaitable[Any]]):
        if self.cp.done(name):
            self.cursor = self.rt.graph.node(self.case_id, "step", f"{name} (restored from checkpoint)",
                                             self.agent.name, [self.cursor], status="skipped")
            return self.cp.output(name)
        node = self.rt.graph.node(self.case_id, "step", name, self.agent.name, [self.cursor])
        self.cursor = node
        try:
            self.rt.fault(f"{self.agent.name}:step:{name}")
            out = fn()
            if inspect.isawaitable(out):
                out = await out
        except Exception as e:
            self.rt.graph.finish(self.case_id, node, "error", {"error": str(e)[:300]})
            raise
        self.cp.save_step(name, out)  # incremental checkpoint
        self.rt.graph.finish(self.case_id, node)
        return out

    def write(self, key: str, value) -> None:
        self.rt.memory.write(self.case_id, key, value, self.agent.name, parents=[self.cursor])

    def post(self, msg_type: str, summary: str, data: dict | None = None) -> None:
        self.rt.board.post(self.case_id, self.agent.name, msg_type, summary, data, parents=[self.cursor])

    async def emit(self, event_type: str, data: dict | None = None) -> None:
        await self.rt.emit(self.agent.name, event_type, self.case_id, data or {}, [self.cursor])

    async def llm(self, stages: list[str], prompt: str, inputs: dict[str, Any], extra_tools: list,
                  deliverables: dict, extra_state: dict | None = None) -> dict:
        """Runs this specialist's ADK agent; FCoT progress is seeded from (and saved to) the checkpoint."""
        ic = CONTRACTS[self.agent.name]

        def state_fn() -> dict:
            st = {"_case_id": self.case_id, "_agent": self.agent.name, "_stages": stages,
                  "_parent_node": self.cursor, "_board_digest": self.rt.board.digest(self.case_id)}
            for k, v in inputs.items():
                st["in_" + k] = v if isinstance(v, str) else json.dumps(v, indent=1, default=str)[:20000]
            st.update(extra_state or {})
            for stage in stages:
                hist = self.cp.fcot_history(stage)
                if hist:
                    st[f"fcot_{stage}"] = hist
            return st

        def instruction(ctx: ReadonlyContext) -> str:
            return render(ic, dict(ctx.state))

        adk_agent = Agent(
            name=f"{self.agent.name}_agent",
            model=self.rt.settings.gemini_model,
            description=ic.mission,
            instruction=instruction,
            tools=[fcot_record_iteration, *extra_tools],
            before_tool_callback=make_guard(deliverables),
            after_tool_callback=audit_tool_call,
            generate_content_config=types.GenerateContentConfig(temperature=0.2),
        )
        self.rt.active[(self.case_id, self.agent.name)] = self.cp
        try:
            return await run_with_failover(self.rt, adk_agent, prompt, state_fn, self.case_id,
                                           self.agent.name, self.cursor)
        finally:
            self.rt.active.pop((self.case_id, self.agent.name), None)


class Specialist:
    name: str = ""
    triggers: set[str] = set()

    def __init__(self, rt: Runtime):
        self.rt = rt

    def accepts(self, event: Event) -> bool:
        return event.type in self.triggers

    async def run(self, ctx: Ctx) -> None:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    async def handle(self, event: Event) -> str:
        """Entry point for a delivered event. Raises on transient failure so the bus retries."""
        rt, case_id = self.rt, event.case_id
        run_node = rt.graph.node(case_id, "agent_run", f"handle {event.type} (attempt {event.attempt})",
                                 self.name, [event.causation_id])
        try:
            cp = rt.checkpointer.claim(case_id, self.name, event)
        except Busy:
            rt.graph.finish(case_id, run_node, "warn", {"reason": "busy: another instance holds the lease"})
            raise
        except GiveUp as e:
            await rt.fail_case(case_id, self.name, str(e), [run_node])
            return "gave_up"
        if cp is None:
            rt.graph.finish(case_id, run_node, "ok", {"duplicate": True})
            return "duplicate"
        if cp.resumed:
            rt.graph.node(case_id, "healer", f"resume from checkpoint (attempt {cp.attempts})", self.name,
                          [run_node], status="warn", meta={"steps_done": list(cp.doc["steps"])})

        ctx = Ctx(rt, self, event, cp, run_node)
        try:
            await self.run(ctx)
        except (PermanentError, MeshViolation) as e:
            cp.fail(str(e), permanent=True)
            rt.graph.finish(case_id, run_node, "failed", {"error": str(e)[:300]})
            if isinstance(e, MeshViolation):
                rt.mesh.record_block(case_id, self.name, str(e), e.findings)
            await rt.fail_case(case_id, self.name, str(e), [ctx.cursor])
            return "failed"
        except Exception as e:
            cp.fail(f"{type(e).__name__}: {e}")
            rt.graph.finish(case_id, run_node, "error", {"error": str(e)[:300]})
            log.exception("%s failed on %s (attempt %s); will be retried", self.name, case_id, cp.attempts)
            raise
        cp.complete()
        rt.graph.finish(case_id, run_node)
        return "ok"
