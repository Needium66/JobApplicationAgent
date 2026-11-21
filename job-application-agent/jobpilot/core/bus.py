"""Event bus: decentralized choreography between specialist agents.

No agent calls another agent. Each agent subscribes to event types and emits new
events when its work is done. Cloud: Pub/Sub topic + one filtered push
subscription per agent (with retries and a dead-letter topic). Local: an
in-process bus with the same at-least-once + retry + dead-letter semantics.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from .store import now

log = logging.getLogger("jobpilot.bus")

# Event types (the protocol between agents)
CASE_CREATED = "case.created"
INGESTION_COMPLETED = "ingestion.completed"
EVALUATION_COMPLETED = "evaluation.completed"
DOCUMENTS_READY = "documents.ready"
APPLICATION_APPROVED = "application.approved"
APPLICATION_SUBMITTED = "application.submitted"
CASE_FAILED = "case.failed"


@dataclass
class Event:
    type: str
    case_id: str
    actor: str
    data: dict = field(default_factory=dict)
    causation_id: str = ""          # causal-graph node that produced this event
    attempt: int = 1                # bumped by the healer when it re-drives
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    ts: float = field(default_factory=now)

    def attributes(self) -> dict[str, str]:
        attrs = {"type": self.type, "case_id": self.case_id, "attempt": str(self.attempt)}
        if "decision" in self.data:
            attrs["decision"] = str(self.data["decision"])  # used by Pub/Sub subscription filters
        return attrs

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str | bytes) -> "Event":
        d = json.loads(raw)
        return Event(**{k: d[k] for k in ("type", "case_id", "actor", "data", "causation_id", "attempt", "id", "ts") if k in d})

    @staticmethod
    def from_push(body: dict) -> tuple["Event", int]:
        """Decodes a Pub/Sub push request. Returns (event, delivery_attempt)."""
        msg = body["message"]
        event = Event.from_json(base64.b64decode(msg["data"]))
        return event, int(body.get("deliveryAttempt", 1) or 1)

    def redrive(self) -> "Event":
        return Event(type=self.type, case_id=self.case_id, actor=self.actor, data=self.data,
                     causation_id=self.causation_id, attempt=self.attempt + 1)


Handler = Callable[[Event], Awaitable[None]]


class Bus:
    async def publish(self, event: Event) -> None: ...


class PubSubBus(Bus):
    def __init__(self, project: str, topic: str):
        from google.cloud import pubsub_v1
        self._client = pubsub_v1.PublisherClient()
        self._topic = self._client.topic_path(project, topic)

    async def publish(self, event: Event) -> None:
        future = self._client.publish(self._topic, event.to_json().encode(), **event.attributes())
        await asyncio.wrap_future(future)
        log.info(json.dumps({"msg": "event.published", "type": event.type, "case_id": event.case_id, "event_id": event.id}))


class LocalBus(Bus):
    """In-process bus with Pub/Sub-like semantics: filtered subscriptions, retries, dead-letter."""

    def __init__(self, max_attempts: int = 5):
        self.subscriptions: list[tuple[str, Callable[[Event], bool], Handler]] = []
        self.queue: collections.deque = collections.deque()  # loop-agnostic (safe across test clients)
        self.max_attempts = max_attempts
        self.dead_letters: list[Event] = []
        self.published: list[Event] = []

    def subscribe(self, name: str, handler: Handler, filter_fn: Callable[[Event], bool]) -> None:
        self.subscriptions.append((name, filter_fn, handler))

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        for name, flt, handler in self.subscriptions:
            if flt(event):
                self.queue.append((name, handler, event, 1))

    async def drain(self, max_steps: int = 500) -> None:
        """Delivers queued events until idle (used by tests and the local server)."""
        steps = 0
        while self.queue and steps < max_steps:
            steps += 1
            name, handler, event, delivery = self.queue.popleft()
            try:
                await handler(event)
            except Exception as e:  # noqa: BLE001 — mirrors a non-2xx push response
                log.warning("delivery %s of %s to %s failed: %s", delivery, event.type, name, e)
                if delivery >= self.max_attempts:
                    self.dead_letters.append(event)
                else:
                    self.queue.append((name, handler, event, delivery + 1))
