"""Role policies: the RAG knowledge source, kept in persistent storage (Firestore).

A role policy is the standard profile of a role: summary, context, level expectations,
core/preferred skills, tools, certifications, responsibilities, keywords and
evaluation guidance. Policies are versioned (every update keeps the previous version
in history), chunked per section, and embedded for retrieval.

    role_policies/{id}                    current version
    role_policies/{id}/versions/{n}       full history
    policy_chunks/{id}__{section}         retrieval units (+ embedding)
    rag/meta                              revision counter (retriever cache invalidation)
"""
from __future__ import annotations

import glob
import os
import re

import yaml
from pydantic import BaseModel, Field, field_validator

from ..core.store import Store, now

POLICY_DIR = os.path.join(os.path.dirname(__file__), "policies")


class Level(BaseModel):
    min_years: float = 0
    expectations: str = ""


class RolePolicy(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{2,60}$")
    title: str = Field(min_length=2, max_length=120)
    aliases: list[str] = []
    summary: str = Field(min_length=20, max_length=3000)
    context: str = Field("", max_length=3000)
    levels: dict[str, Level] = {}
    core_skills: list[str] = Field(min_length=1, max_length=40)
    preferred_skills: list[str] = Field(default_factory=list, max_length=40)
    tools: list[str] = Field(default_factory=list, max_length=60)
    certifications: list[str] = Field(default_factory=list, max_length=40)
    responsibilities: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=60)
    evaluation_guidance: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("summary", "context", "title")
    @classmethod
    def _strip(cls, v):
        return re.sub(r"\s+", " ", str(v)).strip()

    @field_validator("aliases", "core_skills", "preferred_skills", "tools", "certifications",
                     "responsibilities", "keywords", "evaluation_guidance")
    @classmethod
    def _clean(cls, v):
        return [re.sub(r"\s+", " ", str(x)).strip()[:300] for x in v if str(x).strip()]


def display(term: str) -> str:
    """'Kubernetes|k8s' -> 'Kubernetes (k8s)'."""
    head, *alts = term.split("|")
    return f"{head} ({', '.join(alts)})" if alts else head


def chunk_policy(p: RolePolicy, version: int) -> list[dict]:
    def lst(xs):
        return "; ".join(display(x) for x in xs) or "none"

    levels = " ".join(f"{name.title()}: {lvl.min_years}+ years. {lvl.expectations}" for name, lvl in p.levels.items())
    sections = {
        "overview": f"{p.title}. {p.summary} {p.context}",
        "core_skills": f"{p.title} core (required-baseline) skills: {lst(p.core_skills)}.",
        "preferred_skills": f"{p.title} preferred skills: {lst(p.preferred_skills)}.",
        "tools": f"{p.title} common tools: {lst(p.tools)}.",
        "experience": f"{p.title} experience levels. {levels}",
        "certifications": f"{p.title} relevant certifications: {lst(p.certifications)}.",
        "responsibilities": f"{p.title} responsibilities: {lst(p.responsibilities)}.",
        "keywords": f"{p.title} keywords: {', '.join(p.keywords)}. Also known as: {', '.join(p.aliases)}.",
        "guidance": f"{p.title} evaluation guidance: {' '.join(p.evaluation_guidance)}",
    }
    return [{"chunk_id": f"{p.id}#{sec}", "policy_id": p.id, "title": p.title, "section": sec,
             "text": text, "version": version} for sec, text in sections.items() if text.strip()]


class PolicyStore:
    def __init__(self, store: Store, embedder):
        self.store, self.embedder = store, embedder

    def list(self) -> list[dict]:
        return [{"id": i, "title": d["policy"]["title"], "version": d["version"], "updated_at": d["updated_at"],
                 "updated_by": d["updated_by"]} for i, d in self.store.list("role_policies")]

    def get(self, policy_id: str) -> dict | None:
        doc = self.store.get(f"role_policies/{policy_id}")
        return {**doc["policy"], "version": doc["version"]} if doc else None

    def history(self, policy_id: str) -> list[dict]:
        return [d for _, d in self.store.list(f"role_policies/{policy_id}/versions", order_by="-version")]

    def lexicon(self) -> dict:
        """All skills/tools/certifications across every policy: the vocabulary of checkable claims."""
        rev = self.revision()
        if getattr(self, "_lex_rev", None) != rev:
            merged = {"core_skills": [], "preferred_skills": [], "tools": [], "certifications": []}
            for p in self.list():
                pol = self.get(p["id"]) or {}
                for k in merged:
                    merged[k].extend(pol.get(k, []))
            self._lex, self._lex_rev = {k: sorted(set(v)) for k, v in merged.items()}, rev
        return self._lex

    def revision(self) -> int:
        return (self.store.get("rag/meta") or {}).get("revision", 0)

    def put(self, data: dict, author: str, note: str = "") -> dict:
        policy = RolePolicy(**data)
        holder: dict = {}

        def upd(doc):
            version = (doc["version"] if doc else 0) + 1
            holder["version"] = version
            return {"policy": policy.model_dump(), "version": version, "updated_at": now(), "updated_by": author}

        self.store.transact(f"role_policies/{policy.id}", upd)
        version = holder["version"]
        self.store.set(f"role_policies/{policy.id}/versions/{version:05d}", {
            "version": version, "policy": policy.model_dump(), "author": author, "note": note[:500], "at": now()})

        chunks = chunk_policy(policy, version)
        vectors = self.embedder.embed([c["text"] for c in chunks], task="RETRIEVAL_DOCUMENT")
        for old_id, _ in self.store.list("policy_chunks", where=[("policy_id", "==", policy.id)]):
            self.store.delete(f"policy_chunks/{old_id}")
        for c, v in zip(chunks, vectors):
            self.store.set(f"policy_chunks/{c['chunk_id'].replace('#', '__')}", {**c, "embedding": v})

        self.store.transact("rag/meta", lambda d: {"revision": (d or {}).get("revision", 0) + 1, "at": now()})
        return {"id": policy.id, "version": version, "chunks": len(chunks)}

    def rollback(self, policy_id: str, version: int, author: str) -> dict:
        old = self.store.get(f"role_policies/{policy_id}/versions/{version:05d}")
        if not old:
            raise KeyError(f"{policy_id} v{version} not found")
        return self.put(old["policy"], author, note=f"rollback to v{version}")

    def seed_if_empty(self, directory: str = POLICY_DIR) -> int:
        """Loads the bundled YAML policies for any role not yet in storage. Never overwrites edits."""
        existing = {p["id"] for p in self.list()}
        added = 0
        for path in sorted(glob.glob(os.path.join(directory, "*.yaml"))):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data["id"] not in existing:
                self.put(data, author="seed", note=f"seeded from {os.path.basename(path)}")
                added += 1
        return added
