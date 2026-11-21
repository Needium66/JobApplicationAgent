"""Document store abstraction: Firestore in the cloud, an in-memory store locally/in tests.

Paths are slash-separated, alternating collection/document: "cases/abc/board/msg1".
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Iterable


class ConflictError(Exception):
    """Raised when a create() targets an existing document."""


class Store:
    def get(self, path: str) -> dict | None: ...
    def set(self, path: str, data: dict, merge: bool = False) -> None: ...
    def create(self, path: str, data: dict) -> None: ...  # raises ConflictError
    def delete(self, path: str) -> None: ...
    def transact(self, path: str, fn: Callable[[dict | None], dict | None]) -> dict | None: ...
    def list(self, collection: str, order_by: str | None = None, limit: int | None = None,
             where: list[tuple[str, str, Any]] | None = None) -> list[tuple[str, dict]]: ...


def _match(doc: dict, where: Iterable[tuple[str, str, Any]]) -> bool:
    ops = {"==": lambda a, b: a == b, "<": lambda a, b: a is not None and a < b,
           "<=": lambda a, b: a is not None and a <= b, ">": lambda a, b: a is not None and a > b,
           "in": lambda a, b: a in b}
    return all(ops[op](doc.get(f), v) for f, op, v in where)


class MemoryStore(Store):
    """Thread-safe in-memory store with Firestore-like semantics."""

    def __init__(self):
        self._docs: dict[str, dict] = {}
        self._lock = threading.RLock()

    def get(self, path):
        with self._lock:
            d = self._docs.get(path)
            return copy.deepcopy(d) if d is not None else None

    def set(self, path, data, merge=False):
        with self._lock:
            if merge and path in self._docs:
                self._docs[path].update(copy.deepcopy(data))
            else:
                self._docs[path] = copy.deepcopy(data)

    def create(self, path, data):
        with self._lock:
            if path in self._docs:
                raise ConflictError(path)
            self._docs[path] = copy.deepcopy(data)

    def delete(self, path):
        with self._lock:
            self._docs.pop(path, None)

    def transact(self, path, fn):
        with self._lock:
            new = fn(self.get(path))
            if new is not None:
                self._docs[path] = copy.deepcopy(new)
            return copy.deepcopy(new)

    def list(self, collection, order_by=None, limit=None, where=None):
        prefix = collection.rstrip("/") + "/"
        depth = collection.count("/") + 1
        with self._lock:
            rows = [(p.rsplit("/", 1)[1], copy.deepcopy(d)) for p, d in self._docs.items()
                    if p.startswith(prefix) and p.count("/") == depth]
        if where:
            rows = [r for r in rows if _match(r[1], where)]
        if order_by:
            desc = order_by.startswith("-")
            key = order_by.lstrip("-")
            rows.sort(key=lambda r: (r[1].get(key) is None, r[1].get(key)), reverse=desc)
        return rows[:limit] if limit else rows


class FirestoreStore(Store):
    def __init__(self, project: str, database: str = "(default)"):
        from google.cloud import firestore
        self._fs = firestore
        self._db = firestore.Client(project=project, database=database)

    def _ref(self, path):
        return self._db.document(path)

    def get(self, path):
        snap = self._ref(path).get()
        return snap.to_dict() if snap.exists else None

    def set(self, path, data, merge=False):
        self._ref(path).set(data, merge=merge)

    def create(self, path, data):
        from google.api_core.exceptions import AlreadyExists
        try:
            self._ref(path).create(data)
        except AlreadyExists as e:
            raise ConflictError(path) from e

    def delete(self, path):
        self._ref(path).delete()

    def transact(self, path, fn):
        ref = self._ref(path)
        transaction = self._db.transaction(max_attempts=10)

        @self._fs.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            new = fn(snap.to_dict() if snap.exists else None)
            if new is not None:
                tx.set(ref, new)
            return new

        return run(transaction)

    def list(self, collection, order_by=None, limit=None, where=None):
        q = self._db.collection(collection)
        for f, op, v in where or []:
            q = q.where(filter=self._fs.FieldFilter(f, op, v))
        if order_by:
            direction = self._fs.Query.DESCENDING if order_by.startswith("-") else self._fs.Query.ASCENDING
            q = q.order_by(order_by.lstrip("-"), direction=direction)
        if limit:
            q = q.limit(limit)
        return [(s.id, s.to_dict()) for s in q.stream()]


def now() -> float:
    return time.time()
