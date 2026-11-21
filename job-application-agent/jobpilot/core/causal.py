"""Causal Dependency Graph — auditability and monitoring.

Every meaningful action is a node with edges to the nodes that caused it:
user request -> event -> agent run -> steps / tool calls / memory writes /
board posts / mesh decisions -> emitted event -> next agent ...

Stored per case (cases/{id}/graph/{node}) and mirrored to Cloud Logging as
structured JSON, so you can answer "why did this happen?" by walking parents.
"""
from __future__ import annotations

import json
import logging
import uuid

from .store import Store, now

log = logging.getLogger("jobpilot.causal")

KIND_STYLE = {
    "human": "([{label}])", "event": "[/{label}/]", "agent_run": "[[{label}]]",
    "step": "({label})", "tool": "({label})", "memory": "[({label})]", "board": "[{label}]",
    "mesh": "{{{{{label}}}}}", "healer": "[[{label}]]",
}


class CausalGraph:
    def __init__(self, store: Store):
        self.store = store

    def node(self, case_id: str, kind: str, label: str, actor: str, parents: list[str] | None = None,
             status: str = "ok", meta: dict | None = None) -> str:
        node_id = f"{int(now() * 1000):013d}-{uuid.uuid4().hex[:6]}"
        doc = {"id": node_id, "kind": kind, "label": label[:120], "actor": actor,
               "parents": [p for p in (parents or []) if p], "status": status,
               "ts": now(), "meta": meta or {}}
        self.store.set(f"cases/{case_id}/graph/{node_id}", doc)
        log.info(json.dumps({"msg": "causal.node", "case_id": case_id, **doc}, default=str))
        return node_id

    def finish(self, case_id: str, node_id: str, status: str = "ok", meta: dict | None = None) -> None:
        def upd(doc):
            if doc is None:
                return None
            doc["status"] = status
            doc["duration_ms"] = int((now() - doc["ts"]) * 1000)
            if meta:
                doc["meta"] = {**doc.get("meta", {}), **meta}
            return doc
        self.store.transact(f"cases/{case_id}/graph/{node_id}", upd)

    def nodes(self, case_id: str) -> list[dict]:
        return [d for _, d in self.store.list(f"cases/{case_id}/graph", order_by="ts")]

    def lineage(self, case_id: str, node_id: str) -> list[dict]:
        """All ancestors of a node (the causal chain that led to it), oldest first."""
        by_id = {n["id"]: n for n in self.nodes(case_id)}
        seen, stack = set(), [node_id]
        while stack:
            cur = stack.pop()
            if cur in seen or cur not in by_id:
                continue
            seen.add(cur)
            stack.extend(by_id[cur]["parents"])
        return sorted((by_id[i] for i in seen), key=lambda n: n["ts"])

    def to_mermaid(self, case_id: str, max_nodes: int = 250) -> str:
        nodes = self.nodes(case_id)[-max_nodes:]
        ids = {n["id"] for n in nodes}
        alias = {n["id"]: f"n{i}" for i, n in enumerate(nodes)}
        lines = ["flowchart TD"]
        for n in nodes:
            label = f'{n["actor"]}: {n["label"]}'.replace('"', "'")
            shape = KIND_STYLE.get(n["kind"], "[{label}]").format(label=f'"{label}"')
            lines.append(f"  {alias[n['id']]}{shape}")
            if n["status"] not in ("ok",):
                lines.append(f"  class {alias[n['id']]} {('bad' if n['status'] in ('error', 'blocked', 'failed') else 'warn')}")
        for n in nodes:
            for p in n["parents"]:
                if p in ids:
                    lines.append(f"  {alias[p]} --> {alias[n['id']]}")
        lines += ["  classDef bad fill:#fde2e1,stroke:#c0392b", "  classDef warn fill:#fff4d6,stroke:#b7791f"]
        return "\n".join(lines)
