"""Hybrid retrieval over role-policy chunks: BM25 keyword scoring + embedding cosine similarity.

The policy corpus is small (tens of roles x ~9 sections), so chunks are loaded once and
cached in memory; the cache refreshes automatically when any policy is updated (the
rag/meta revision changes), so edits take effect without redeploying.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .policy_store import PolicyStore

TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*")
STOP = set("a an and are as at be by for from has have in is it of on or that the to with will you your our we "
           "this who what years year experience role team work".split())


def tokens(text: str) -> list[str]:
    return [t.strip("./-") for t in TOKEN.findall((text or "").lower()) if t.strip("./-") and t not in STOP]


class Bm25:
    def __init__(self, docs: list[list[str]], k1: float = 1.4, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.avg = sum(map(len, docs)) / max(len(docs), 1)
        df = Counter(t for d in docs for t in set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(d) for d in docs]

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for d, tf in zip(self.docs, self.tf):
            s = 0.0
            for q in query:
                if q in tf:
                    f = tf[q]
                    s += self.idf[q] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            out.append(s)
        return out


def _norm(xs: list[float]) -> list[float]:
    hi = max(xs) if xs else 0
    return [x / hi if hi > 0 else 0.0 for x in xs]


class PolicyRetriever:
    def __init__(self, store, policies: PolicyStore, embedder):
        self.store, self.policies, self.embedder = store, policies, embedder
        self._rev = -1
        self.chunks: list[dict] = []

    def _refresh(self) -> None:
        rev = self.policies.revision()
        if rev == self._rev:
            return
        self.chunks = [d for _, d in self.store.list("policy_chunks")]
        self._bm25 = Bm25([tokens(c["text"]) for c in self.chunks])
        self._rev = rev

    def search(self, query: str, k: int = 6, policy_id: str | None = None) -> list[dict]:
        self._refresh()
        if not self.chunks:
            return []
        kw = _norm(self._bm25.scores(tokens(query)))
        qv = self.embedder.embed([query], task="RETRIEVAL_QUERY")[0] if self.embedder.enabled else None
        results = []
        for c, k_score in zip(self.chunks, kw):
            if policy_id and c["policy_id"] != policy_id:
                continue
            cos = sum(a * b for a, b in zip(qv, c["embedding"])) if qv and c.get("embedding") else None
            score = 0.5 * k_score + 0.5 * max(cos, 0.0) if cos is not None else k_score
            results.append((score, c))
        results.sort(key=lambda r: r[0], reverse=True)
        return [{"chunk_id": c["chunk_id"], "policy_id": c["policy_id"], "section": c["section"],
                 "version": c["version"], "text": c["text"], "score": round(s, 4)} for s, c in results[:k] if s > 0]

    def resolve_role(self, job_title: str, job_text: str = "") -> dict:
        """Which role policy applies to this posting? Title/alias match dominates; posting text breaks ties."""
        self._refresh()
        title = (job_title or "").lower()
        title_toks = set(tokens(title))
        candidates = []
        for p in self.policies.list():
            pol = self.policies.get(p["id"])
            names = [pol["title"].lower(), *[a.lower() for a in pol.get("aliases", [])]]
            exact = max((len(n) for n in names if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", title)), default=0)
            overlap = max((len(title_toks & set(tokens(n))) / max(len(set(tokens(n))), 1) for n in names), default=0)
            title_score = 1.0 if exact else 0.7 * overlap
            candidates.append({"policy_id": pol["id"], "title": pol["title"], "version": pol["version"],
                               "title_score": round(title_score, 3), "exact": bool(exact), "exact_len": exact})
        if not candidates:
            return {"policy_id": None, "confidence": 0.0, "candidates": []}
        if job_text:
            text_hits: dict[str, float] = {}
            for r in self.search(job_text[:4000], k=40):
                text_hits[r["policy_id"]] = text_hits.get(r["policy_id"], 0) + r["score"]
            top = max(text_hits.values(), default=0) or 1
            for c in candidates:
                c["text_score"] = round(text_hits.get(c["policy_id"], 0) / top, 3)
        for c in candidates:
            c["confidence"] = round(0.75 * c["title_score"] + 0.25 * c.get("text_score", 0), 3)
        candidates.sort(key=lambda c: (c["confidence"], c["exact_len"]), reverse=True)
        best = candidates[0]
        ok = best["confidence"] >= 0.35
        return {"policy_id": best["policy_id"] if ok else None, "title": best["title"] if ok else None,
                "version": best["version"] if ok else None, "confidence": best["confidence"],
                "candidates": candidates[:3]}
