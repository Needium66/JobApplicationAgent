"""Shared Persistent Memory for all specialist agents.

Two scopes, both in Firestore:
  * case memory  cases/{id}/memory/{key}  — working memory for one application
                 (resume_raw, job_raw, candidate_profile, job_posting, evaluation, documents, application)
  * user memory  users/{uid}/memory/profile — long-term memory across applications
                 (application history, outcomes)

Writes are versioned (optimistic concurrency) and authorised per key by the
Defense Mesh, so each agent can only write what its contract allows.
"""
from __future__ import annotations

from .causal import CausalGraph
from .store import Store, now


class StaleWriteError(Exception):
    pass


class SharedMemory:
    def __init__(self, store: Store, graph: CausalGraph, mesh):
        self.store, self.graph, self.mesh = store, graph, mesh

    # ---- case scope ----
    def read(self, case_id: str, key: str, default=None):
        doc = self.store.get(f"cases/{case_id}/memory/{key}")
        return doc["value"] if doc else default

    def version(self, case_id: str, key: str) -> int:
        doc = self.store.get(f"cases/{case_id}/memory/{key}")
        return doc["version"] if doc else 0

    def write(self, case_id: str, key: str, value, agent: str, parents: list[str] | None = None,
              expected_version: int | None = None) -> int:
        self.mesh.check_memory_write(agent, key)

        def upd(doc):
            current = doc["version"] if doc else 0
            if expected_version is not None and current != expected_version:
                raise StaleWriteError(f"{key}: expected v{expected_version}, found v{current}")
            return {"value": value, "version": current + 1, "writer": agent, "ts": now()}

        new = self.store.transact(f"cases/{case_id}/memory/{key}", upd)
        self.graph.node(case_id, "memory", f"wrote {key} v{new['version']}", agent, parents)
        return new["version"]

    def snapshot(self, case_id: str) -> dict:
        return {k: d["value"] for k, d in self.store.list(f"cases/{case_id}/memory")}

    # ---- user (long-term) scope ----
    def user_profile(self, uid: str) -> dict:
        return self.store.get(f"users/{_uid(uid)}/memory/profile") or {}

    def user_append_history(self, uid: str, entry: dict, agent: str, max_items: int = 50) -> None:
        self.mesh.check_memory_write(agent, "user.history")

        def upd(doc):
            doc = doc or {"history": []}
            doc["history"] = (doc.get("history", []) + [entry])[-max_items:]
            doc["updated_at"] = now()
            return doc

        self.store.transact(f"users/{_uid(uid)}/memory/profile", upd)


    def application_profile(self, uid: str) -> dict:
        return (self.store.get(f"users/{_uid(uid)}/profile/application") or {}).get("profile", {})

    def save_application_profile(self, uid: str, profile: dict, agent: str) -> None:
        self.mesh.check_memory_write(agent, "user.profile")
        self.store.set(f"users/{_uid(uid)}/profile/application", {"profile": profile, "updated_at": now()})


def _uid(email: str) -> str:
    return email.lower().replace("/", "_")
