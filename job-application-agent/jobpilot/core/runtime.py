"""Wires the shared infrastructure every service uses (one per process)."""
from __future__ import annotations

import logging

from ..settings import Settings, get_settings
from .blobs import Blobs
from .board import MessageBoard
from .bus import Bus, Event, LocalBus, PubSubBus
from .causal import CausalGraph
from .checkpoint import Checkpointer
from .memory import SharedMemory
from .mesh import DefenseMesh
from .store import FirestoreStore, MemoryStore, Store, now

log = logging.getLogger("jobpilot.runtime")


class Runtime:
    def __init__(self, settings: Settings, store: Store | None = None, bus: Bus | None = None):
        self.settings = settings
        if store is None:
            store = FirestoreStore(settings.project_id) if settings.backend == "gcp" else MemoryStore()
        if bus is None:
            bus = PubSubBus(settings.project_id, settings.events_topic) if settings.backend == "gcp" \
                else LocalBus(max_attempts=settings.max_agent_attempts)
        self.store, self.bus = store, bus
        self.graph = CausalGraph(store)
        self.mesh = DefenseMesh(settings, store, self.graph)
        self.board = MessageBoard(store, self.graph, self.mesh)
        self.memory = SharedMemory(store, self.graph, self.mesh)
        self.checkpointer = Checkpointer(store, settings.lease_seconds, settings.max_agent_attempts)
        self.blobs = Blobs(settings)
        self.model_override = None      # callable(agent_name) -> BaseLlm, for offline tests
        self.faults: dict[str, int] = {}  # fault injection for resilience tests, e.g. {"evaluation:fcot:evaluation:2": 1}
        self.active: dict[tuple[str, str], object] = {}  # (case_id, agent) -> Checkpoint, used by tools
        self._policies = None
        self._retriever = None

    # ---- RAG: role policies (persistent, versioned) + hybrid retriever ----
    @property
    def policies(self):
        if self._policies is None:
            from ..rag.embeddings import make_embedder
            from ..rag.policy_store import PolicyStore
            self._embedder = make_embedder(self.settings)
            self._policies = PolicyStore(self.store, self._embedder)
        return self._policies

    @property
    def retriever(self):
        if self._retriever is None:
            from ..rag.retriever import PolicyRetriever
            self._retriever = PolicyRetriever(self.store, self.policies, self._embedder)
        return self._retriever

    def ensure_policies(self) -> int:
        """Seeds the bundled role policies into storage if a role is missing (never overwrites edits)."""
        return self.policies.seed_if_empty()

    # ---- case document helpers ----
    def case(self, case_id: str) -> dict | None:
        return self.store.get(f"cases/{case_id}")

    def set_case_status(self, case_id: str, status: str, **extra) -> None:
        self.store.set(f"cases/{case_id}", {"status": status, "updated_at": now(), **extra}, merge=True)

    async def emit(self, actor: str, event_type: str, case_id: str, data: dict, parents: list[str]) -> Event:
        self.mesh.check_emit(actor, event_type)
        node = self.graph.node(case_id, "event", event_type, actor, parents)
        ev = Event(type=event_type, case_id=case_id, actor=actor, data=data, causation_id=node)
        self.store.set(f"cases/{case_id}", {"updated_at": now(), "last_event": ev.to_json()}, merge=True)
        await self.bus.publish(ev)
        return ev

    async def fail_case(self, case_id: str, actor: str, reason: str, parents: list[str] | None = None,
                        board_type: str = "case.failed") -> None:
        node = self.graph.node(case_id, "step", f"case failed: {reason[:60]}", actor, parents, status="failed")
        self.set_case_status(case_id, "failed", error=reason[:500])
        try:
            self.board.post(case_id, actor, board_type, reason, parents=[node])
        except Exception:  # noqa: BLE001 — never let auditing failure mask the real failure
            log.exception("board post failed")

    def fault(self, key: str) -> None:
        """Fault injection point (tests only)."""
        if self.faults.get(key, 0) > 0:
            self.faults[key] -= 1
            raise RuntimeError(f"injected fault at {key}")


_rt: Runtime | None = None


def get_runtime() -> Runtime:
    global _rt
    if _rt is None:
        _rt = Runtime(get_settings())
    return _rt


def set_runtime(rt: Runtime) -> Runtime:
    global _rt
    _rt = rt
    return rt
