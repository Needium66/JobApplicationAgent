"""Central Message Board — the shared blackboard every specialist agent can read.

Agents read the board before they start (it becomes part of their Recap) and
post to it after each task. Posts pass through the Defense Mesh (capability +
output scan) and are linked into the causal graph.
"""
from __future__ import annotations

import uuid

from .causal import CausalGraph
from .store import Store, now


class MessageBoard:
    def __init__(self, store: Store, graph: CausalGraph, mesh):
        self.store, self.graph, self.mesh = store, graph, mesh

    def post(self, case_id: str, agent: str, msg_type: str, summary: str, data: dict | None = None,
             parents: list[str] | None = None) -> str:
        self.mesh.check_board(agent, msg_type)
        summary = self.mesh.scan_output(summary, agent=agent).text
        msg_id = f"{int(now() * 1000):013d}-{uuid.uuid4().hex[:6]}"
        node = self.graph.node(case_id, "board", f"posted {msg_type}", agent, parents)
        self.store.set(f"cases/{case_id}/board/{msg_id}", {
            "id": msg_id, "agent": agent, "type": msg_type, "summary": summary[:2000],
            "data": data or {}, "ts": now(), "graph_node": node,
        })
        return msg_id

    def read(self, case_id: str, limit: int = 200) -> list[dict]:
        return [d for _, d in self.store.list(f"cases/{case_id}/board", order_by="ts", limit=limit)]

    def digest(self, case_id: str, max_items: int = 25) -> str:
        """Compact board view injected into each agent's instruction (part of the FCoT Recap)."""
        items = self.read(case_id)[-max_items:]
        if not items:
            return "(board is empty)"
        return "\n".join(f"- [{m['agent']}] {m['type']}: {m['summary'][:300]}" for m in items)
