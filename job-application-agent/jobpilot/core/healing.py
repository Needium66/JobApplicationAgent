"""Auto-Healing.

  * retry_async     — exponential backoff with jitter for transient errors (429/5xx/timeouts)
  * CircuitBreaker  — shared (store-backed) breaker per model, so all instances stop
                      hammering a failing model at the same time
  * model failover  — handled in llm.py: primary model -> fallback model when the breaker is open
  * Healer          — periodic sweeper (Cloud Scheduler -> healer service):
                        - re-drives work whose lease expired (crashed/timed-out instance)
                        - drains the Pub/Sub dead-letter subscription and re-drives once more
                        - re-drives cases that went quiet without a live lease
                        - gives up cleanly (case.failed + board post) after MAX attempts
  * Pub/Sub itself  — at-least-once delivery, retry policy with backoff, dead-letter topic
  * Cloud Run       — restarts crashed containers; checkpoints make restarts cheap
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

from .bus import Event
from .store import Store, now

log = logging.getLogger("jobpilot.healing")

TERMINAL = {"submitted", "denied", "failed", "awaiting_approval", "dismissed"}
TRANSIENT_MARKERS = ("429", "500", "502", "503", "504", "resource_exhausted", "unavailable",
                     "deadline", "timeout", "timed out", "temporarily", "overloaded", "connection reset")


def is_transient(exc: BaseException) -> bool:
    """True if this error (or anything it wraps, e.g. ADK's DynamicNodeFailError) is worth retrying."""
    seen = 0
    while exc is not None and seen < 10:
        seen += 1
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if isinstance(code, int) and (code == 429 or code >= 500):
            return True
        text = f"{type(exc).__name__} {exc}".lower()
        if any(m in text for m in TRANSIENT_MARKERS):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def retry_async(fn, attempts: int = 4, base: float = 1.0, cap: float = 20.0, on_retry=None):
    for i in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001
            if i == attempts or not is_transient(e):
                raise
            delay = min(cap, base * 2 ** (i - 1)) * (0.5 + random.random() / 2)
            if on_retry:
                on_retry(i, e, delay)
            await asyncio.sleep(delay)


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, store: Store, name: str, threshold: int, cooldown_s: int):
        self.store, self.path, self.threshold, self.cooldown = store, f"health/{name.replace('/', '_')}", threshold, cooldown_s

    def allow(self) -> bool:
        doc = self.store.get(self.path) or {}
        return doc.get("open_until", 0) <= now()

    def success(self) -> None:
        self.store.set(self.path, {"failures": 0, "open_until": 0, "updated_at": now()})

    def failure(self) -> None:
        def upd(doc):
            doc = doc or {"failures": 0, "open_until": 0}
            doc["failures"] += 1
            if doc["failures"] >= self.threshold:
                doc["open_until"] = now() + self.cooldown
                doc["failures"] = 0
            doc["updated_at"] = now()
            return doc
        self.store.transact(self.path, upd)


class Healer:
    def __init__(self, runtime):
        self.rt = runtime

    async def heal(self) -> dict:
        report = {"redriven": [], "gave_up": [], "dlq_seen": 0, "stale_cases": 0}
        t = now()
        s = self.rt.settings

        # 1) expired leases: an agent instance died or timed out mid-work
        for _, lease in self.rt.store.list("leases", where=[("lease_until", "<", t)]):
            if lease.get("status") != "running":
                continue
            await self._redrive_or_give_up(Event.from_json(lease["event"]), f"lease expired ({lease['agent']})", report)
            self.rt.store.set(f"leases/{lease['case_id']}__{lease['agent']}", {**lease, "status": "redriven"})

        # 2) dead-lettered events: Pub/Sub gave up after its own retries
        for ev in await self._pull_dead_letters():
            report["dlq_seen"] += 1
            await self._redrive_or_give_up(ev, "dead-lettered", report)

        # 3) quiet cases: in progress, nothing changed for a while, no live lease
        cutoff = t - s.stale_case_minutes * 60
        for case_id, case in self.rt.store.list("cases", where=[("updated_at", "<", cutoff)], limit=200):
            if case.get("status") in TERMINAL or not case.get("last_event"):
                continue
            live = any(l.get("status") == "running" and l.get("lease_until", 0) > t
                       for _, l in self.rt.store.list("leases", where=[("case_id", "==", case_id)]))
            if not live:
                report["stale_cases"] += 1
                await self._redrive_or_give_up(Event.from_json(case["last_event"]), "case went quiet", report)
        log.info(json.dumps({"msg": "healer.report", **{k: (len(v) if isinstance(v, list) else v) for k, v in report.items()}}))
        return report

    async def _redrive_or_give_up(self, ev: Event, reason: str, report: dict) -> None:
        case = self.rt.store.get(f"cases/{ev.case_id}") or {}
        if case.get("status") in TERMINAL:
            return
        if ev.attempt >= self.rt.settings.max_agent_attempts:
            await self.rt.fail_case(ev.case_id, "healer", f"Gave up after {ev.attempt} attempts: {reason}",
                                    parents=[ev.causation_id], board_type="healer.gave_up")
            report["gave_up"].append(ev.case_id)
            return
        node = self.rt.graph.node(ev.case_id, "healer", f"re-drive {ev.type} ({reason})", "healer",
                                  [ev.causation_id], status="warn")
        self.rt.board.post(ev.case_id, "healer", "healer.redrive",
                           f"Re-driving {ev.type} (attempt {ev.attempt + 1}): {reason}", parents=[node])
        new = ev.redrive()
        new.causation_id = node
        new.actor = "healer"
        self.rt.mesh.check_emit("healer", new.type)
        self.rt.store.set(f"cases/{ev.case_id}", {"updated_at": now(), "last_event": new.to_json()}, merge=True)
        await self.rt.bus.publish(new)
        report["redriven"].append(ev.case_id)

    async def _pull_dead_letters(self) -> list[Event]:
        bus = self.rt.bus
        if hasattr(bus, "dead_letters"):  # LocalBus
            evs, bus.dead_letters = list(bus.dead_letters), []
            return evs
        from google.cloud import pubsub_v1
        sub = pubsub_v1.SubscriberClient()
        path = sub.subscription_path(self.rt.settings.project_id, self.rt.settings.dlq_subscription)
        resp = await asyncio.to_thread(sub.pull, request={"subscription": path, "max_messages": 50}, timeout=20)
        events, ack_ids = [], []
        for rm in resp.received_messages:
            ack_ids.append(rm.ack_id)
            try:
                events.append(Event.from_json(rm.message.data))
            except Exception:  # noqa: BLE001 — poison message: drop it but keep the audit log
                log.error("Unparseable dead letter dropped: %r", rm.message.data[:200])
        if ack_ids:
            await asyncio.to_thread(sub.acknowledge, request={"subscription": path, "ack_ids": ack_ids})
        return events
