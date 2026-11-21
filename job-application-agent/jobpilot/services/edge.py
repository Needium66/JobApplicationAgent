"""Edge service — the only user-facing entry point.

Cloud Run with IAP enabled directly (no API Gateway / load balancer): Google
authenticates the user, IAP authorizes them (roles/iap.httpsResourceAccessor),
then every request passes through the Defense Mesh before touching the backend.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from ..core import bus as ev
from ..core.bus import Event
from ..core.mesh import Identity, MeshViolation
from ..core.runtime import Runtime
from ..core.store import now

STATIC = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class CaseRequest(BaseModel):
    resume_text: str = Field("", description="Pasted resume text")
    resume_gcs_uri: str = Field("", description="gs:// URI of a resume in the docs bucket")
    job_description: str = Field("", description="Pasted job description")
    job_url: str = Field("", description="Public link to the job posting")
    tailor_resume: bool = False
    notes: str = Field("", description="Optional note to the agents")


class ApproveRequest(BaseModel):
    method: Literal["portal", "email"] = "portal"
    recipient_email: str = ""


class StandardAnswer(BaseModel):
    question: str = Field(max_length=300)
    answer: str = Field(max_length=2000)


class ApplicationProfile(BaseModel):
    """Reusable answers for job applications. Deliberately has no password/credential fields
    (extra fields are rejected): JobPilot never stores or uses job-board logins."""
    model_config = {"extra": "forbid"}
    full_name: str = Field("", max_length=120)
    email: str = Field("", max_length=254)
    phone: str = Field("", max_length=40)
    location: str = Field("", max_length=120)
    linkedin_url: str = Field("", max_length=300)
    github_url: str = Field("", max_length=300)
    portfolio_url: str = Field("", max_length=300)
    work_authorization: str = Field("", max_length=200)
    requires_sponsorship: Literal["", "yes", "no", "prefer_not_to_say"] = ""
    willing_to_relocate: Literal["", "yes", "no", "depends"] = ""
    notice_period: str = Field("", max_length=80)
    earliest_start_date: str = Field("", max_length=40)
    salary_expectation: str = Field("", max_length=80)
    preferred_submission: Literal["auto", "portal", "email"] = "auto"
    standard_answers: list[StandardAnswer] = Field(default_factory=list, max_length=30)


class SwipeRequest(BaseModel):
    direction: Literal["right", "left"]
    method: Literal["", "portal", "email"] = ""
    recipient_email: str = ""


class PolicyRollback(BaseModel):
    version: int


def create_edge_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="JobPilot Level 2 — Edge", version="2.1.0", docs_url=None, redoc_url=None)
    mesh = rt.mesh
    try:
        seeded = rt.ensure_policies()  # role-policy knowledge base for RAG (never overwrites edits)
        if seeded:
            logging.getLogger("jobpilot.edge").info("seeded %s role policies", seeded)
    except Exception:  # noqa: BLE001 — agents seed lazily too; don't block the edge from starting
        logging.getLogger("jobpilot.edge").exception("policy seeding failed")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        return response

    @app.exception_handler(MeshViolation)
    async def mesh_error(_: Request, exc: MeshViolation):
        return JSONResponse(status_code=exc.code, content={"error": str(exc), "findings": exc.findings})

    def who(request: Request) -> Identity:
        ident = mesh.authenticate(request.headers)
        if ident.role == "none":
            raise MeshViolation("Your account is not on the allow-list for JobPilot.", 403)
        mesh.rate_limit(ident)
        return ident

    def load_case(case_id: str, ident: Identity, action: str) -> dict:
        case = rt.case(case_id)
        if not case:
            raise MeshViolation("Not found.", 404)
        mesh.authorize(ident, action, owner=case.get("owner"))
        return case

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "role": "edge", "backend": rt.settings.backend}

    @app.get("/", response_class=HTMLResponse)
    def index():
        with open(STATIC, encoding="utf-8") as f:
            return f.read()

    @app.get("/api/me")
    def me(ident: Identity = Depends(who)):
        return {"email": ident.email, "role": ident.role, "auth": ident.auth_method,
                "can_view_policies": "policy.read" in mesh.PERMISSIONS.get(ident.role, set()),
                "history": rt.memory.user_profile(ident.email).get("history", [])[-10:]}

    @app.post("/api/cases", status_code=201)
    async def create_case(body: CaseRequest, ident: Identity = Depends(who)):
        mesh.authorize(ident, "case.create")
        findings: list[str] = []

        def scanned(value: str, name: str) -> str:
            if not value:
                return ""
            r = mesh.scan_input(value, name)
            if r.blocked:
                mesh.record_block(None, ident.email, f"{name} blocked", r.findings)
                raise MeshViolation(f"{name} was blocked by the Defense Mesh (instruction-like or malicious content). "
                                    f"Remove it and try again.", 422, r.findings)
            findings.extend(f"{name}:{f}" for f in r.findings)
            return r.text

        resume = scanned(body.resume_text, "resume_text")
        job = scanned(body.job_description, "job_description")
        notes = scanned(body.notes, "notes")
        job_url = mesh.scan_url(body.job_url) if body.job_url else ""
        gcs = body.resume_gcs_uri.strip()
        if gcs:
            bucket_prefix = f"gs://{rt.settings.docs_bucket}/"
            if not rt.settings.docs_bucket or not gcs.startswith(bucket_prefix):
                raise MeshViolation(f"resume_gcs_uri must be inside {bucket_prefix or 'the docs bucket'}.", 400)
        if not (resume or gcs):
            raise MeshViolation("Provide resume_text or resume_gcs_uri.", 400)
        if not (job or job_url):
            raise MeshViolation("Provide job_description or job_url.", 400)

        case_id = uuid.uuid4().hex[:12]
        rt.store.create(f"cases/{case_id}", {
            "id": case_id, "owner": ident.email, "status": "ingesting", "created_at": now(), "updated_at": now(),
            "job": {"url": job_url}, "decision": None, "score": None})
        human = rt.graph.node(case_id, "human", f"{ident.email} submitted an application request", ident.email,
                              meta={"mesh_findings": findings})
        mem = rt.memory
        if resume:
            mem.write(case_id, "resume_raw", resume, "edge", [human])
        if job:
            mem.write(case_id, "job_raw", job, "edge", [human])
        mem.write(case_id, "options", {"tailor_resume": body.tailor_resume, "job_url": job_url,
                                       "resume_gcs_uri": gcs, "notes": notes}, "edge", [human])
        rt.board.post(case_id, "edge", ev.CASE_CREATED, f"New application request from {ident.email}.", parents=[human])
        await rt.emit("edge", ev.CASE_CREATED, case_id, {}, [human])
        return {"case_id": case_id, "status": "ingesting", "mesh_findings": findings}

    @app.get("/api/cases")
    def list_cases(ident: Identity = Depends(who)):
        mesh.authorize(ident, "case.read")
        where = None if ident.role in ("admin", "reviewer") else [("owner", "==", ident.email)]
        rows = rt.store.list("cases", where=where, limit=100)
        rows.sort(key=lambda r: r[1].get("created_at", 0), reverse=True)
        return [{k: c.get(k) for k in ("id", "owner", "status", "job", "decision", "score", "created_at", "updated_at")}
                for _, c in rows]

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str, ident: Identity = Depends(who)):
        case = load_case(case_id, ident, "case.read")
        snap = rt.memory.snapshot(case_id)
        checkpoints = {a: {"status": d.get("status"), "attempts": d.get("attempts"),
                           "steps_done": [s for s, v in d.get("steps", {}).items() if v.get("status") == "done"],
                           "fcot": {st: len(h) for st, h in d.get("fcot", {}).items()}, "last_error": d.get("last_error")}
                       for a, d in rt.store.list(f"cases/{case_id}/checkpoints")}
        return {
            "case": {k: v for k, v in case.items() if k != "last_event"},
            "board": rt.board.read(case_id),
            "evaluation": snap.get("evaluation"),
            "job_posting": snap.get("job_posting"),
            "documents": {k: {"words": v.get("words")} for k, v in (snap.get("documents") or {}).items()},
            "application": snap.get("application"),
            "checkpoints": checkpoints,
        }

    @app.get("/api/cases/{case_id}/graph")
    def graph(case_id: str, format: Literal["mermaid", "json"] = "mermaid", ident: Identity = Depends(who)):
        load_case(case_id, ident, "graph.read")
        if format == "json":
            return rt.graph.nodes(case_id)
        return PlainTextResponse(rt.graph.to_mermaid(case_id))

    @app.get("/api/cases/{case_id}/lineage/{node_id}")
    def lineage(case_id: str, node_id: str, ident: Identity = Depends(who)):
        load_case(case_id, ident, "graph.read")
        return rt.graph.lineage(case_id, node_id)

    async def approve_case(case: dict, ident: Identity, method: str, recipient: str, via: str) -> dict:
        case_id = case["id"]
        if case.get("status") != "awaiting_approval":
            raise MeshViolation(f"Case is '{case.get('status')}', not awaiting approval.", 409)
        if method == "email":
            if not rt.settings.apply_email_enabled:
                raise MeshViolation("Email submission is disabled on this deployment; use the portal method.", 409)
            recipient = mesh.scan_email(recipient)
        else:
            recipient = ""
        prior = Event.from_json(case["last_event"]).causation_id if case.get("last_event") else ""
        human = rt.graph.node(case_id, "human", f"{ident.email} approved submission via {method} ({via})", ident.email,
                              [prior])  # links the approval to what the human saw (documents.ready)
        rt.memory.write(case_id, "approval", {"approver": ident.email, "method": method, "via": via,
                                              "recipient_email": recipient, "at": now()}, "edge", [human])
        rt.set_case_status(case_id, "applying")
        rt.board.post(case_id, "edge", ev.APPLICATION_APPROVED, f"{ident.email} approved submission via {method} ({via}).",
                      parents=[human])
        await rt.emit("edge", ev.APPLICATION_APPROVED, case_id, {"method": method}, [human])
        return {"case_id": case_id, "status": "applying", "method": method}

    @app.post("/api/cases/{case_id}/approve")
    async def approve(case_id: str, body: ApproveRequest, ident: Identity = Depends(who)):
        case = load_case(case_id, ident, "case.approve")
        return await approve_case(case, ident, body.method, body.recipient_email, "button")

    @app.post("/api/cases/{case_id}/retry")
    async def retry(case_id: str, ident: Identity = Depends(who)):
        case = load_case(case_id, ident, "case.retry")
        if case.get("status") != "failed" or not case.get("last_event"):
            raise MeshViolation("Only failed applications can be retried.", 409)
        last = Event.from_json(case["last_event"])
        if last.type not in (ev.CASE_CREATED, ev.INGESTION_COMPLETED, ev.EVALUATION_COMPLETED, ev.APPLICATION_APPROVED):
            raise MeshViolation(f"Nothing to retry after '{last.type}'.", 409)
        human = rt.graph.node(case_id, "human", f"{ident.email} requested a retry", ident.email, [last.causation_id])
        data = dict(last.data)
        data["generation"] = int(data.get("generation", 1)) + 1  # fresh run: dead checkpoints are not resumable
        status = {ev.CASE_CREATED: "ingesting", ev.INGESTION_COMPLETED: "evaluating",
                  ev.EVALUATION_COMPLETED: "drafting", ev.APPLICATION_APPROVED: "applying"}.get(last.type, "ingesting")
        rt.set_case_status(case_id, status, error="")
        actor = "edge" if last.type in (ev.CASE_CREATED, ev.APPLICATION_APPROVED) else "healer"
        await rt.emit(actor, last.type, case_id, data, [human])
        return {"case_id": case_id, "status": status, "redriven": last.type}

    @app.get("/api/cases/{case_id}/documents/{kind}")
    def document(case_id: str, kind: Literal["cover_letter", "tailored_resume"], ident: Identity = Depends(who)):
        load_case(case_id, ident, "case.read")
        docs = rt.memory.read(case_id, "documents") or {}
        if kind not in docs:
            raise MeshViolation("Document not found.", 404)
        data = rt.blobs.get(docs[kind]["docx"])
        return Response(content=data, media_type=DOCX,
                        headers={"Content-Disposition": f'attachment; filename="{kind}.docx"'})

    # ================= Application profile (no credentials, ever) =================
    @app.get("/api/profile")
    def get_profile(ident: Identity = Depends(who)):
        return ApplicationProfile(**{**rt.memory.application_profile(ident.email)}).model_dump()

    @app.put("/api/profile")
    def put_profile(body: ApplicationProfile, ident: Identity = Depends(who)):
        mesh.authorize(ident, "profile.write")
        data = body.model_dump()
        for key in ("full_name", "location", "work_authorization", "notice_period", "salary_expectation"):
            data[key] = scan_or_block(data[key], key, ident)
        for url_key in ("linkedin_url", "github_url", "portfolio_url"):
            if data[url_key] and not data[url_key].startswith("https://"):
                raise MeshViolation(f"{url_key} must start with https://", 400)
        if data["email"]:
            data["email"] = mesh.scan_email(data["email"])
        data["standard_answers"] = [{"question": scan_or_block(a["question"], "question", ident),
                                     "answer": scan_or_block(a["answer"], "answer", ident)}
                                    for a in data["standard_answers"]]
        rt.memory.save_application_profile(ident.email, data, "edge")
        return {"saved": True}

    def scan_or_block(value: str, name: str, ident: Identity) -> str:
        if not value:
            return value
        if re.search(r"\b(password|passwd|pwd|passcode)\b", value, re.I):
            raise MeshViolation(f"{name}: JobPilot never stores passwords or job-board logins.", 422)
        r = mesh.scan_input(value, name)
        if r.blocked:
            mesh.record_block(None, ident.email, f"{name} blocked", r.findings)
            raise MeshViolation(f"{name} was blocked by the Defense Mesh.", 422, r.findings)
        return r.text

    # ================= Swipe to apply =================
    @app.get("/api/swipe/deck")
    def swipe_deck(ident: Identity = Depends(who)):
        mesh.authorize(ident, "case.read")
        rows = rt.store.list("cases", where=[("owner", "==", ident.email)])
        deck = []
        for cid, c in rows:
            if c.get("status") != "awaiting_approval":
                continue
            snap = rt.memory.snapshot(cid)
            evaluation, posting = snap.get("evaluation") or {}, snap.get("job_posting") or {}
            align = evaluation.get("policy_alignment") or {}
            deck.append({
                "case_id": cid, "title": posting.get("title"), "company": posting.get("company"),
                "location": posting.get("location"), "remote_policy": posting.get("remote_policy"),
                "score": evaluation.get("confidence_score"), "strengths": evaluation.get("strengths", [])[:3],
                "gaps": evaluation.get("gaps", [])[:3], "role_policy": (snap.get("role_policy") or {}).get("title"),
                "relevant_coverage": align.get("relevant_coverage"), "relevant_missing": align.get("relevant_missing", [])[:5],
                "advisory_missing": align.get("advisory_missing", [])[:4],
                "documents": list((snap.get("documents") or {}).keys()),
                "apply_email_available": bool(posting.get("apply_email")) and rt.settings.apply_email_enabled,
                "job_url": (snap.get("options") or {}).get("job_url", "")})
        deck.sort(key=lambda d: d["score"] or 0, reverse=True)
        profile = rt.memory.application_profile(ident.email)
        return {"cards": deck, "profile_complete": bool(profile.get("full_name") and profile.get("email"))}

    @app.post("/api/swipe/{case_id}")
    async def swipe(case_id: str, body: SwipeRequest, ident: Identity = Depends(who)):
        case = load_case(case_id, ident, "case.approve")
        if case.get("status") != "awaiting_approval":
            raise MeshViolation(f"Case is '{case.get('status')}', not waiting for a decision.", 409)
        if body.direction == "left":
            human = rt.graph.node(case_id, "human", f"{ident.email} swiped left (not interested)", ident.email)
            rt.memory.write(case_id, "dismissal", {"by": ident.email, "at": now()}, "edge", [human])
            rt.set_case_status(case_id, "dismissed")
            rt.board.post(case_id, "edge", "case.dismissed", f"{ident.email} passed on this role.", parents=[human])
            rt.memory.user_append_history(ident.email, {"case_id": case_id, "job": case.get("job", {}),
                                                        "score": case.get("score"), "outcome": "dismissed", "at": now()},
                                          "edge")
            return {"case_id": case_id, "status": "dismissed"}

        profile = rt.memory.application_profile(ident.email)
        if not (profile.get("full_name") and profile.get("email")):
            raise MeshViolation("Complete your application profile (at least name and email) before swiping right.", 409)
        posting = rt.memory.read(case_id, "job_posting") or {}
        method, recipient = body.method, body.recipient_email
        if not method:
            pref = profile.get("preferred_submission", "auto")
            can_email = rt.settings.apply_email_enabled and bool(posting.get("apply_email"))
            method = "email" if (pref == "email" or pref == "auto") and can_email else "portal"
        if method == "email" and not recipient:
            recipient = posting.get("apply_email", "")
        return await approve_case(case, ident, method, recipient, "swipe")

    # ================= Role policies (RAG knowledge base) =================
    @app.get("/api/policies")
    def list_policies(ident: Identity = Depends(who)):
        mesh.authorize(ident, "policy.read")
        return rt.policies.list()

    @app.get("/api/policies/{policy_id}")
    def get_policy(policy_id: str, ident: Identity = Depends(who)):
        mesh.authorize(ident, "policy.read")
        pol = rt.policies.get(policy_id)
        if not pol:
            raise MeshViolation("Policy not found.", 404)
        hist = [{"version": h["version"], "author": h["author"], "note": h["note"], "at": h["at"]}
                for h in rt.policies.history(policy_id)]
        return {"policy": pol, "history": hist}

    @app.put("/api/policies/{policy_id}")
    def put_policy(policy_id: str, body: dict, ident: Identity = Depends(who)):
        mesh.authorize(ident, "policy.write")
        body = {**body, "id": policy_id}
        body.pop("version", None)
        flat = json.dumps(body)
        r = mesh.scan_input(flat, "policy")      # policies feed every agent's context: no injected instructions
        if r.blocked:
            mesh.record_block(None, ident.email, "policy update blocked", r.findings)
            raise MeshViolation("Policy contains instruction-like content and was blocked.", 422, r.findings)
        try:
            result = rt.policies.put(body, author=ident.email, note=str(body.get("_note", ""))[:200])
        except ValueError as e:
            raise MeshViolation(f"Invalid policy: {e}", 422) from e
        return result

    @app.post("/api/policies/{policy_id}/rollback")
    def rollback_policy(policy_id: str, body: PolicyRollback, ident: Identity = Depends(who)):
        mesh.authorize(ident, "policy.write")
        try:
            return rt.policies.rollback(policy_id, body.version, ident.email)
        except KeyError as e:
            raise MeshViolation(str(e), 404) from e

    return app


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
