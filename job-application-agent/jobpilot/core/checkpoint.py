"""Incremental Checkpointing.

Each specialist agent's work is a sequence of named steps. After every step (and
after every FCoT iteration) progress is persisted, so a crash, timeout or
redelivery resumes from the last completed step instead of starting over.

Also provides:
  * idempotency  — a duplicate delivery of finished work is acknowledged and ignored
  * leases       — a running agent holds a time-limited lease (heartbeated per step);
                   the healer re-drives work whose lease expired (crashed instance)
"""
from __future__ import annotations

from .bus import Event
from .store import Store, now


class Busy(Exception):
    """Another instance holds a live lease on this work (concurrent duplicate delivery)."""


class GiveUp(Exception):
    """Attempts exhausted."""


class Checkpoint:
    def __init__(self, store: Store, case_id: str, agent: str, event: Event, doc: dict, lease_s: int):
        self.store, self.case_id, self.agent, self.event, self.doc, self.lease_s = store, case_id, agent, event, doc, lease_s
        self.path = f"cases/{case_id}/checkpoints/{agent}"

    @property
    def attempts(self) -> int:
        return self.doc["attempts"]

    @property
    def resumed(self) -> bool:
        return bool(self.doc.get("steps")) or bool(self.doc.get("fcot"))

    def done(self, step: str) -> bool:
        return self.doc["steps"].get(step, {}).get("status") == "done"

    def output(self, step: str):
        return self.doc["steps"][step].get("output")

    def _write(self, **fields) -> None:
        def upd(doc):
            doc = doc or dict(self.doc)
            doc.update(fields)
            doc["lease_until"] = now() + self.lease_s
            doc["updated_at"] = now()
            return doc
        self.doc = self.store.transact(self.path, upd)
        self._mirror_lease()

    def save_step(self, step: str, output) -> None:
        steps = dict(self.doc["steps"])
        steps[step] = {"status": "done", "output": output, "at": now()}
        self._write(steps=steps)

    def heartbeat(self) -> None:
        self._write()

    def fcot_history(self, stage: str) -> list[dict]:
        return list(self.doc.get("fcot", {}).get(stage, []))

    def save_fcot(self, stage: str, history: list[dict]) -> None:
        fcot = dict(self.doc.get("fcot", {}))
        fcot[stage] = history
        self._write(fcot=fcot)

    def complete(self) -> None:
        self._write(status="completed", last_error="")

    def fail(self, error: str, permanent: bool = False) -> None:
        self._write(status="dead" if permanent else "failed", last_error=error[:1000])

    def _mirror_lease(self) -> None:
        # Top-level collection so the healer can find expired leases with a single-field query.
        self.store.set(f"leases/{self.case_id}__{self.agent}", {
            "case_id": self.case_id, "agent": self.agent, "status": self.doc.get("status"),
            "lease_until": self.doc.get("lease_until", 0) if self.doc.get("status") == "running" else 4102444800,
            "attempts": self.doc.get("attempts"), "event": self.event.to_json(), "updated_at": now(),
        })


class Checkpointer:
    def __init__(self, store: Store, lease_seconds: int, max_attempts: int):
        self.store, self.lease_s, self.max_attempts = store, lease_seconds, max_attempts

    @staticmethod
    def work_key(event: Event) -> str:
        return f"{event.type}#{event.data.get('generation', 1)}"

    def claim(self, case_id: str, agent: str, event: Event) -> Checkpoint | None:
        """Returns a Checkpoint to run/resume, None if the work is already finished (duplicate)."""
        path = f"cases/{case_id}/checkpoints/{agent}"
        key = self.work_key(event)
        outcome: dict = {}

        def upd(doc):
            t = now()
            if doc is None or doc.get("work_key") != key:
                outcome["state"] = "new"
                return {"work_key": key, "status": "running", "attempts": 1, "steps": {}, "fcot": {},
                        "event_id": event.id, "lease_until": t + self.lease_s, "created_at": t,
                        "updated_at": t, "last_error": ""}
            if doc["status"] in ("completed", "dead"):
                outcome["state"] = doc["status"]
                return None
            if doc["status"] == "running" and doc.get("lease_until", 0) > t:
                # Someone (maybe a slow instance handling this very event) holds a live lease.
                # Pub/Sub will redeliver with backoff; once the lease expires we resume.
                outcome["state"] = "busy"
                return None
            if doc["attempts"] >= self.max_attempts:
                outcome["state"] = "exhausted"
                doc["status"] = "dead"
                return doc
            outcome["state"] = "resume"
            doc.update(status="running", attempts=doc["attempts"] + 1, event_id=event.id,
                       lease_until=t + self.lease_s, updated_at=t)
            return doc

        doc = self.store.transact(path, upd)
        state = outcome["state"]
        if state in ("completed", "dead"):
            return None
        if state == "busy":
            raise Busy(f"{agent} already working on {case_id}")
        if state == "exhausted":
            raise GiveUp(f"{agent} exhausted {self.max_attempts} attempts on {case_id}")
        cp = Checkpoint(self.store, case_id, agent, event, doc, self.lease_s)
        cp._mirror_lease()
        return cp
