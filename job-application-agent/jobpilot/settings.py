"""Level 2 configuration (environment variables).

Locally:   copy jobpilot/.env.example to jobpilot/.env (loaded by jobpilot.local)
Cloud Run: set per service by scripts/level2/deploy_level2.ps1
"""
import os
from dataclasses import dataclass, field


def _b(key: str, default: str = "false") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes")


def _list(key: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(key, "").split(",") if x.strip()]


@dataclass
class Settings:
    # local = in-memory store + in-process bus; gcp = Firestore + Pub/Sub
    backend: str = field(default_factory=lambda: os.getenv("BACKEND", "gcp" if os.getenv("K_SERVICE") else "local"))
    service_role: str = field(default_factory=lambda: os.getenv("SERVICE_ROLE", "local"))
    project_id: str = field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    region: str = field(default_factory=lambda: os.getenv("REGION", "us-central1"))

    # Gemini (Vertex AI). Failover model is used when the primary is unavailable.
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.8-flash"))
    gemini_fallback_model: str = field(default_factory=lambda: os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash"))

    # RAG over role policies (Vertex AI embeddings; falls back to keyword-only retrieval when off/unavailable)
    # Off by default: at this corpus size keyword retrieval is enough. Turn on as the policy library grows.
    embeddings: str = field(default_factory=lambda: os.getenv("EMBEDDINGS", "none"))
    embedding_model: str = field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "gemini-embedding-001"))
    embedding_location: str = field(default_factory=lambda: os.getenv("EMBEDDING_LOCATION", "us-central1"))
    embedding_dim: int = field(default_factory=lambda: int(os.getenv("EMBEDDING_DIM", "768")))
    rag_top_k: int = field(default_factory=lambda: int(os.getenv("RAG_TOP_K", "6")))

    # Infrastructure
    docs_bucket: str = field(default_factory=lambda: os.getenv("DOCS_BUCKET", ""))
    events_topic: str = field(default_factory=lambda: os.getenv("EVENTS_TOPIC", "jobpilot-events"))
    dlq_subscription: str = field(default_factory=lambda: os.getenv("DLQ_SUBSCRIPTION", "jobpilot-dlq-sub"))
    local_output_dir: str = field(default_factory=lambda: os.getenv("LOCAL_OUTPUT_DIR", "local_output"))

    # Authentication / authorization (IAP replaces an API gateway)
    auth_mode: str = field(default_factory=lambda: os.getenv("AUTH_MODE", "iap" if os.getenv("K_SERVICE") else "dev"))
    iap_audience: str = field(default_factory=lambda: os.getenv("IAP_AUDIENCE", ""))
    admin_emails: list[str] = field(default_factory=lambda: _list("ADMIN_EMAILS"))
    allowed_emails: list[str] = field(default_factory=lambda: _list("ALLOWED_EMAILS"))
    allowed_domains: list[str] = field(default_factory=lambda: _list("ALLOWED_DOMAINS"))
    rate_limit_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MIN", "30")))
    # Expected callers of the private agent / healer endpoints (defense in depth on top of Cloud Run IAM)
    push_invoker_sa: str = field(default_factory=lambda: os.getenv("PUSH_INVOKER_SA", "").lower())
    scheduler_sa: str = field(default_factory=lambda: os.getenv("SCHEDULER_SA", "").lower())

    # Defense Mesh
    model_armor_template: str = field(default_factory=lambda: os.getenv("MODEL_ARMOR_TEMPLATE", ""))
    mesh_fail_closed: bool = field(default_factory=lambda: _b("MESH_FAIL_CLOSED", "false"))
    max_input_chars: int = field(default_factory=lambda: int(os.getenv("MAX_INPUT_CHARS", "40000")))

    # Fault tolerance
    lease_seconds: int = field(default_factory=lambda: int(os.getenv("LEASE_SECONDS", "300")))
    max_agent_attempts: int = field(default_factory=lambda: int(os.getenv("MAX_AGENT_ATTEMPTS", "5")))
    breaker_threshold: int = field(default_factory=lambda: int(os.getenv("BREAKER_THRESHOLD", "3")))
    breaker_cooldown_s: int = field(default_factory=lambda: int(os.getenv("BREAKER_COOLDOWN_S", "120")))
    retry_base_s: float = field(default_factory=lambda: float(os.getenv("RETRY_BASE_S", "1.0")))
    stale_case_minutes: int = field(default_factory=lambda: int(os.getenv("STALE_CASE_MINUTES", "15")))

    # Application submission
    apply_email_enabled: bool = field(default_factory=lambda: _b("APPLY_EMAIL_ENABLED"))
    gmail_token_secret: str = field(default_factory=lambda: os.getenv("GMAIL_TOKEN_SECRET", "gmail-oauth-token"))
    applicant_email: str = field(default_factory=lambda: os.getenv("APPLICANT_EMAIL", ""))

    @property
    def on_cloud_run(self) -> bool:
        return bool(os.getenv("K_SERVICE"))

    def validate(self) -> None:
        if self.on_cloud_run and self.auth_mode != "iap" and self.service_role == "edge":
            raise RuntimeError("AUTH_MODE must be 'iap' for the edge service on Cloud Run.")
        if self.auth_mode == "iap" and self.service_role == "edge" and not self.iap_audience:
            raise RuntimeError("IAP_AUDIENCE is required when AUTH_MODE=iap.")
        if self.backend == "gcp" and not self.project_id:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for BACKEND=gcp.")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings(**overrides) -> Settings:
    """Used by tests and the local runner."""
    global _settings
    _settings = Settings(**overrides) if overrides else Settings()
    return _settings
