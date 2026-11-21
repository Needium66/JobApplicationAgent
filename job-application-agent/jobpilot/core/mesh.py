"""Agent Defense Mesh — the security layer between users, agents and the backend.

Layers (applied in this order at the edge, and partly again inside every agent):
  1. Authentication   — IAP signed JWT (x-goog-iap-jwt-assertion), verified with Google's keys
  2. Authorization    — RBAC (admin / applicant / reviewer) + per-case ownership
  3. Rate limiting    — per-identity, shared across instances via the store
  4. Input scanning   — size limits, control chars, prompt-injection heuristics, PII redaction,
                        URL safety (SSRF), optional Google Cloud Model Armor
  5. Capability policy— which events each agent may emit, which memory keys / board
                        topics it may write (least privilege between agents)
  6. Output scanning  — PII redaction + leak checks on everything agents publish
  7. Service identity — private agent endpoints only accept the expected Pub/Sub /
                        Scheduler service accounts (on top of Cloud Run IAM)
Every block is recorded in the causal graph.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import re
import socket
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .store import Store, now

log = logging.getLogger("jobpilot.mesh")

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS = "https://www.gstatic.com/iap/verify/public_key"


class MeshViolation(Exception):
    def __init__(self, message: str, code: int = 403, findings: list[str] | None = None):
        super().__init__(message)
        self.code, self.findings = code, findings or []


@dataclass
class Identity:
    email: str
    role: str
    auth_method: str
    subject: str = ""


@dataclass
class ScanResult:
    text: str
    findings: list[str] = field(default_factory=list)
    blocked: bool = False
    risk: int = 0


# ---------- capability policy (least privilege between agents) ----------
CAPABILITIES: dict[str, dict[str, set[str]]] = {
    "edge": {
        "emit": {"case.created", "application.approved"},
        "memory": {"resume_raw", "job_raw", "options", "approval", "dismissal", "user.profile", "user.history"},
        "board": {"case.created", "application.approved", "case.dismissed"},
    },
    "ingestion": {
        "emit": {"ingestion.completed", "case.failed"},
        "memory": {"resume_raw", "job_raw", "candidate_profile", "job_posting", "role_policy"},
        "board": {"ingestion.completed", "ingestion.warning", "grounding.removed", "case.failed"},
    },
    "evaluation": {
        "emit": {"evaluation.completed", "case.failed"},
        "memory": {"evaluation", "user.history"},
        "board": {"evaluation.completed", "case.denied", "case.failed"},
    },
    "cover_letter": {
        "emit": {"documents.ready", "case.failed"},
        "memory": {"documents"},
        "board": {"documents.ready", "case.failed"},
    },
    "application": {
        "emit": {"application.submitted", "case.failed"},
        "memory": {"application", "user.history"},
        "board": {"application.submitted", "application.blocked", "case.failed"},
    },
    "healer": {
        "emit": {"case.created", "ingestion.completed", "evaluation.completed", "application.approved", "case.failed"},
        "memory": set(),
        "board": {"healer.redrive", "healer.gave_up", "case.failed"},
    },
}

# ---------- input scanning rules ----------
INJECTION_PATTERNS: list[tuple[str, int]] = [
    (r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts|rules)", 5),
    (r"disregard (all |the )?(previous|prior|above|system) (instructions|prompt|rules)", 5),
    (r"(reveal|print|show|repeat|leak) (me )?(your|the) (system prompt|instructions|instruction contract)", 5),
    (r"you are now (in )?(developer|dan|jailbreak|god) mode", 5),
    (r"\bjailbreak\b", 3),
    (r"(act|pretend) as (an? )?(unrestricted|unfiltered)", 4),
    (r"(call|invoke|use) the (submit_application|score_resume_match|save_document) tool", 4),
    (r"set (the )?(confidence|score) (to|=) ?\d{2,3}", 4),
    (r"(approve|mark) (this|me|the candidate) (as )?(approved|100)", 3),
    (r"<\s*/?\s*(system|assistant|tool)\s*>", 3),
    (r"\bBEGIN (SYSTEM|ADMIN) (PROMPT|OVERRIDE)\b", 4),
    (r"(send|forward|email) (the|all|my) (resume|data|documents) to [^\s@]+@", 3),
]
BLOCK_THRESHOLD = 5

PII_PATTERNS: list[tuple[str, str, str]] = [
    ("ssn", r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b", "[REDACTED-SSN]"),
    ("bank_account", r"\b(account|acct|routing)\s*(no\.?|number|#)?\s*[:#]?\s*\d{6,17}\b", "[REDACTED-BANK]"),
    ("password", r"\b(password|passwd|pwd)\s*[:=]\s*\S+", "[REDACTED-SECRET]"),
    ("api_key", r"\b(AIza[0-9A-Za-z_\-]{35}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{36})\b", "[REDACTED-KEY]"),
]
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
LEAK_MARKERS = ["# Instruction Contract", "## Hard constraints", "STATE_JSON:"]


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


class DefenseMesh:
    def __init__(self, settings, store: Store, graph=None):
        self.s, self.store, self.graph = settings, store, graph
        self._armor_session = None

    # ===== 1. Authentication =====
    def authenticate(self, headers) -> Identity:
        if self.s.auth_mode == "dev":
            email = (headers.get("x-dev-user") or "dev@localhost").lower()
            return Identity(email=email, role=self._role_for(email), auth_method="dev")
        token = headers.get("x-goog-iap-jwt-assertion")
        if not token:
            raise MeshViolation("Missing IAP assertion. Access this service through IAP.", 401)
        try:
            from google.auth.transport import requests as ga_requests
            from google.oauth2 import id_token
            claims = id_token.verify_token(token, ga_requests.Request(), audience=self.s.iap_audience,
                                           certs_url=IAP_CERTS)
        except Exception as e:  # noqa: BLE001
            raise MeshViolation(f"Invalid IAP assertion: {e}", 401) from e
        if claims.get("iss") != IAP_ISSUER:
            raise MeshViolation("Invalid IAP issuer.", 401)
        email = (claims.get("email") or "").lower()
        if not email:
            raise MeshViolation("IAP assertion has no email.", 401)
        return Identity(email=email, role=self._role_for(email), auth_method="iap", subject=claims.get("sub", ""))

    def verify_service_caller(self, headers, expected_sa: str) -> str:
        """Defense in depth for Pub/Sub push and Cloud Scheduler calls (Cloud Run IAM already checked the token)."""
        if not expected_sa:
            return "unchecked"
        auth = headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise MeshViolation("Missing bearer token.", 401)
        token = auth.split(" ", 1)[1]
        parts = token.split(".")
        if len(parts) != 3:
            raise MeshViolation("Malformed token.", 401)
        if parts[2] == "SIGNATURE_REMOVED_BY_GOOGLE":
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        else:
            from google.auth.transport import requests as ga_requests
            from google.oauth2 import id_token
            try:
                claims = id_token.verify_oauth2_token(token, ga_requests.Request())
            except Exception as e:  # noqa: BLE001
                raise MeshViolation(f"Invalid service token: {e}", 401) from e
        email = (claims.get("email") or "").lower()
        if email != expected_sa:
            raise MeshViolation(f"Caller {email or 'unknown'} is not allowed.", 403)
        return email

    # ===== 2. Authorization =====
    def _role_for(self, email: str) -> str:
        if self.s.auth_mode == "dev" and email == "dev@localhost" and not self.s.admin_emails:
            return "admin"  # local single-user development only (dev auth is refused on Cloud Run)
        acl = self.store.get(f"acl/{email}")
        if acl and acl.get("role"):
            return acl["role"]
        if email in self.s.admin_emails:
            return "admin"
        if self.s.allowed_emails or self.s.allowed_domains:
            domain = email.rsplit("@", 1)[-1]
            if email not in self.s.allowed_emails and domain not in self.s.allowed_domains:
                return "none"
        return "applicant"

    PERMISSIONS = {
        "admin": {"case.create", "case.read", "case.read_any", "case.approve", "case.retry", "graph.read",
                  "policy.read", "policy.write", "profile.write"},
        "applicant": {"case.create", "case.read", "case.approve", "case.retry", "graph.read", "profile.write"},
        "reviewer": {"case.read", "case.read_any", "graph.read", "policy.read"},
        "none": set(),
    }

    def authorize(self, ident: Identity, action: str, owner: str | None = None) -> None:
        perms = self.PERMISSIONS.get(ident.role, set())
        if action not in perms:
            raise MeshViolation(f"Role '{ident.role}' may not {action}.", 403)
        if owner and owner != ident.email and "case.read_any" not in perms:
            raise MeshViolation("You can only access your own applications.", 404)  # 404: don't leak existence
        if owner and owner != ident.email and action in ("case.approve", "case.retry") and ident.role != "admin":
            raise MeshViolation("Only the owner can do that.", 403)

    # ===== 3. Rate limiting =====
    def rate_limit(self, ident: Identity) -> None:
        window = int(now() // 60)
        path = f"ratelimits/{ident.email.replace('/', '_')}"

        def upd(doc):
            if not doc or doc.get("window") != window:
                return {"window": window, "count": 1}
            doc["count"] += 1
            return doc

        doc = self.store.transact(path, upd)
        if doc["count"] > self.s.rate_limit_per_min:
            raise MeshViolation("Rate limit exceeded. Try again in a minute.", 429)

    # ===== 4. Input scanning =====
    def scan_input(self, text: str, field_name: str, untrusted_document: bool = False) -> ScanResult:
        if text is None:
            return ScanResult(text="")
        findings: list[str] = []
        if len(text) > self.s.max_input_chars:
            raise MeshViolation(f"{field_name} is too long (max {self.s.max_input_chars} characters).", 413)
        clean = unicodedata.normalize("NFKC", text)
        clean = "".join(ch for ch in clean if ch in "\n\t\r" or unicodedata.category(ch)[0] != "C")
        if clean != text:
            findings.append("normalized_control_or_invisible_chars")

        risk = 0
        lowered = clean.lower()
        for pattern, weight in INJECTION_PATTERNS:
            if re.search(pattern, lowered, re.IGNORECASE):
                risk += weight
                findings.append(f"injection:{pattern[:40]}")

        clean, pii = self.redact(clean)
        findings += [f"pii:{p}" for p in pii]

        armor = self._model_armor(clean, "prompt")
        if armor:
            findings.append(f"model_armor:{armor}")
            risk += BLOCK_THRESHOLD

        blocked = risk >= BLOCK_THRESHOLD
        if blocked and untrusted_document:
            findings.append("note:document_contains_instructions")
        return ScanResult(text=clean, findings=findings, blocked=blocked, risk=risk)

    def redact(self, text: str) -> tuple[str, list[str]]:
        found: list[str] = []
        for name, pattern, repl in PII_PATTERNS:
            text, n = re.subn(pattern, repl, text, flags=re.IGNORECASE)
            if n:
                found.append(name)

        def card(m):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn_ok(digits):
                found.append("payment_card")
                return "[REDACTED-CARD]"
            return m.group(0)

        text = CARD_RE.sub(card, text)
        return text, sorted(set(found))

    def scan_url(self, url: str) -> str:
        url = (url or "").strip()
        if len(url) > 2048:
            raise MeshViolation("URL too long.", 400)
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            raise MeshViolation("Only http(s) URLs are allowed.", 400)
        if p.username or p.password:
            raise MeshViolation("URLs with credentials are not allowed.", 400)
        if not is_public_host(p.hostname):
            raise MeshViolation("URL must point to a public host.", 400)
        if self._model_armor(url, "prompt"):
            raise MeshViolation("URL flagged as malicious.", 400)
        return url

    def scan_email(self, email: str) -> str:
        email = (email or "").strip()
        if not re.fullmatch(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}", email):
            raise MeshViolation("Invalid email address.", 400)
        return email

    # ===== 5. Capability policy =====
    def _cap(self, agent: str, kind: str, item: str) -> None:
        if item not in CAPABILITIES.get(agent, {}).get(kind, set()):
            raise MeshViolation(f"Agent '{agent}' is not allowed to {kind} '{item}'.", 403)

    def check_emit(self, agent: str, event_type: str) -> None:
        self._cap(agent, "emit", event_type)

    def check_memory_write(self, agent: str, key: str) -> None:
        self._cap(agent, "memory", key)

    def check_board(self, agent: str, msg_type: str) -> None:
        self._cap(agent, "board", msg_type)

    # ===== 6. Output scanning =====
    def scan_output(self, text: str, agent: str = "") -> ScanResult:
        text, pii = self.redact(text or "")
        findings = [f"pii:{p}" for p in pii]
        for marker in LEAK_MARKERS:
            if marker in text:
                text = text.replace(marker, "[removed]")
                findings.append("leak:instruction_marker")
        if self._model_armor(text, "response"):
            findings.append("model_armor:response")
        return ScanResult(text=text, findings=findings)

    # ===== Model Armor (optional, Google Cloud) =====
    def _model_armor(self, text: str, kind: str) -> str:
        """Returns a non-empty reason when Model Armor flags the text."""
        tpl = self.s.model_armor_template
        if not tpl or not text:
            return ""
        try:
            from google.auth import default
            from google.auth.transport.requests import AuthorizedSession
            if self._armor_session is None:
                creds, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
                self._armor_session = AuthorizedSession(creds)
            location = tpl.split("/locations/")[1].split("/")[0]
            method, body = (("sanitizeUserPrompt", {"userPromptData": {"text": text[:20000]}}) if kind == "prompt"
                            else ("sanitizeModelResponse", {"modelResponseData": {"text": text[:20000]}}))
            r = self._armor_session.post(f"https://modelarmor.{location}.rep.googleapis.com/v1/{tpl}:{method}",
                                         json=body, timeout=10)
            r.raise_for_status()
            result = r.json().get("sanitizationResult", {})
            return "MATCH_FOUND" if result.get("filterMatchState") == "MATCH_FOUND" else ""
        except Exception as e:  # noqa: BLE001
            log.warning("Model Armor unavailable: %s", e)
            if self.s.mesh_fail_closed:
                return "unavailable(fail-closed)"
            return ""

    # ===== audit =====
    def record_block(self, case_id: str | None, actor: str, reason: str, findings: list[str]) -> None:
        log.warning(json.dumps({"msg": "mesh.blocked", "actor": actor, "reason": reason, "findings": findings}))
        if case_id and self.graph:
            self.graph.node(case_id, "mesh", f"blocked: {reason[:60]}", "mesh", [], status="blocked",
                            meta={"findings": findings, "actor": actor})
