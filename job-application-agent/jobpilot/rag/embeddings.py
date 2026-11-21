"""Text embeddings for policy retrieval (Vertex AI gemini-embedding-001), with a safe fallback."""
from __future__ import annotations

import logging
import math

log = logging.getLogger("jobpilot.rag")


class NullEmbedder:
    """Keyword-only retrieval (local runs, tests, or when the embedding API is unavailable)."""
    enabled = False

    def embed(self, texts: list[str], task: str = "RETRIEVAL_QUERY") -> list[list[float] | None]:
        return [None for _ in texts]


class VertexEmbedder:
    enabled = True

    def __init__(self, project: str, location: str, model: str, dim: int):
        from google import genai
        self._client = genai.Client(vertexai=True, project=project, location=location)
        self.model, self.dim = model, dim

    def embed(self, texts: list[str], task: str = "RETRIEVAL_QUERY") -> list[list[float] | None]:
        from google.genai import types
        out: list[list[float] | None] = []
        for text in texts:  # gemini-embedding-001 accepts one input per request on Vertex AI
            try:
                r = self._client.models.embed_content(
                    model=self.model, contents=text[:8000],
                    config=types.EmbedContentConfig(task_type=task, output_dimensionality=self.dim))
                vec = list(r.embeddings[0].values)
                norm = math.sqrt(sum(x * x for x in vec)) or 1.0
                out.append([x / norm for x in vec])
            except Exception as e:  # noqa: BLE001 — degrade to keyword retrieval, never fail the agent
                log.warning("embedding failed (%s); keyword retrieval only for this text", e)
                out.append(None)
        return out


def make_embedder(settings):
    if settings.embeddings == "vertex" and settings.project_id:
        try:
            return VertexEmbedder(settings.project_id, settings.embedding_location,
                                  settings.embedding_model, settings.embedding_dim)
        except Exception as e:  # noqa: BLE001
            log.warning("Vertex embeddings unavailable (%s); using keyword retrieval", e)
    return NullEmbedder()
