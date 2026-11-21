"""End-to-end tests of the decentralized pipeline (offline: in-memory store, in-process bus, scripted model)."""
import asyncio
import base64
import json

from fastapi.testclient import TestClient

from jobpilot.core.bus import Event
from jobpilot.core.healing import Healer
from jobpilot.core.store import now
from jobpilot.services.agent_service import create_agent_app
from jobpilot.testing.scripted_llm import ScriptedLlm

from .conftest import Env


def test_happy_path_all_agents_board_memory_graph(env):
    r = env.create(tailor_resume=True)
    assert r.status_code == 201, r.text
    cid = r.json()["case_id"]
    env.drain()

    d = env.get(cid).json()
    assert d["case"]["status"] == "awaiting_approval"
    assert d["evaluation"]["decision"] == "APPROVE" and d["evaluation"]["confidence_score"] >= 80
    assert set(d["documents"]) == {"tailored_resume", "cover_letter"}
    posters = {m["agent"] for m in d["board"]}
    assert {"edge", "ingestion", "evaluation", "cover_letter"} <= posters      # shared board used by every agent

    assert env.approve(cid).status_code == 200
    env.drain()
    d = env.get(cid).json()
    assert d["case"]["status"] == "submitted"
    assert d["application"]["status"] == "ready_for_portal"
    assert "application" in {m["agent"] for m in d["board"]}

    # Shared persistent memory: every agent's output is there, plus long-term user history.
    snap = env.rt.memory.snapshot(cid)
    assert {"resume_raw", "job_raw", "candidate_profile", "job_posting", "evaluation", "documents",
            "approval", "application"} <= set(snap)
    assert env.rt.memory.user_profile("ola@example.com")["history"][-1]["outcome"] == "ready_for_portal"

    # Causal graph: the submission traces back to the human request and the approval.
    nodes = env.nodes(cid)
    submitted = next(n for n in nodes if n["kind"] == "event" and n["label"] == "application.submitted")
    chain = env.rt.graph.lineage(cid, submitted["id"])
    labels = [n["label"] for n in chain]
    assert any("submitted an application request" in l for l in labels)
    assert any("approved submission" in l for l in labels)
    assert env.client.get(f"/api/cases/{cid}/graph", headers={"x-dev-user": "ola@example.com"}).text.startswith("flowchart TD")
    # Every FCoT iteration of every agent was recorded (N=3 per stage).
    fcot = [n for n in nodes if n["kind"] == "tool" and n["label"].startswith("FCoT")]
    assert len(fcot) == 3 * 5  # extraction, evaluation, tailoring, cover_letter, application_packet

    # Downloads go through the edge with authorization.
    doc = env.client.get(f"/api/cases/{cid}/documents/cover_letter", headers={"x-dev-user": "ola@example.com"})
    assert doc.status_code == 200 and doc.content[:2] == b"PK"


def test_crash_mid_loop_resumes_from_incremental_checkpoint():
    env = Env()
    env.rt.faults["evaluation:fcot:evaluation:2"] = 1   # instance "crashes" right after FCoT iteration 2 is saved
    cid = env.create().json()["case_id"]
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "awaiting_approval"
    cp = env.rt.store.get(f"cases/{cid}/checkpoints/evaluation")
    assert cp["attempts"] == 2 and cp["status"] == "completed"
    labels = [n["label"] for n in env.nodes(cid) if n["actor"] == "evaluation" and n["kind"] == "tool"]
    assert labels.count("FCoT evaluation #1 verified=True") == 1   # not redone after the crash
    assert labels.count("FCoT evaluation #2 verified=True") == 0   # crashed inside #2 (after it was checkpointed) ...
    assert labels.count("FCoT evaluation #3 verified=True") == 1   # ... and the retry continued at #3, not #1
    assert [h["iteration"] for h in cp["fcot"]["evaluation"]] == [1, 2, 3]
    assert any("resume from checkpoint" in n["label"] for n in env.nodes(cid))
    # Steps finished before the crash are restored, not recomputed.
    assert any(n["label"] == "employment_dates (restored from checkpoint)" for n in env.nodes(cid))


def test_deny_path_stops_choreography():
    env = Env(evaluation={"weak_candidate": True})
    cid = env.create().json()["case_id"]
    env.drain()
    d = env.get(cid).json()
    assert d["case"]["status"] == "denied"
    assert d["evaluation"]["decision"] == "DENY" and d["evaluation"]["confidence_score"] < 80
    assert env.rt.store.get(f"cases/{cid}/checkpoints/cover_letter") is None   # cover-letter agent never triggered
    assert env.approve(cid).status_code == 409


def test_model_failover_and_shared_circuit_breaker():
    env = Env()
    primary = {}

    def override(name):
        p = primary.setdefault(name, ScriptedLlm(model="scripted-primary", agent_name=name, fail_first=10 ** 6))
        return [p, ScriptedLlm(model="scripted-fallback", agent_name=name)]

    env.rt.model_override = override
    cid = env.create().json()["case_id"]
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "awaiting_approval"
    labels = [n["label"] for n in env.nodes(cid)]
    assert labels.count("failover from scripted-primary") == 3          # ingestion, evaluation, cover_letter
    env.approve(cid)
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "submitted"
    app_labels = [n["label"] for n in env.nodes(cid) if n["actor"] == "application"]
    # After 3 failures the shared breaker is open: the application agent skips the dead model straight away.
    assert "skip scripted-primary (circuit open)" in app_labels
    assert not any(l.startswith("retry scripted-primary") for l in app_labels)


def test_dead_letter_then_healer_redrives():
    env = Env()
    env.rt.bus.max_attempts = 2                       # Pub/Sub gives up quickly in this test
    env.rt.faults["ingestion:step:extract"] = 2       # fails on both deliveries
    cid = env.create().json()["case_id"]
    env.drain()
    assert len(env.rt.bus.dead_letters) == 1
    report = asyncio.run(Healer(env.rt).heal())
    assert cid in report["redriven"]
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "awaiting_approval"
    assert any(m["type"] == "healer.redrive" for m in env.get(cid).json()["board"])


def test_healer_recovers_crashed_instance_with_expired_lease():
    env = Env()
    cid = env.create().json()["case_id"]
    created = env.rt.bus.queue.popleft()      # take the delivery away: pretend the instance died mid-run
    event = created[2]
    cp = env.rt.checkpointer.claim(cid, "ingestion", event)
    cp.save_step("load_inputs", {"resume_chars": 1, "job_chars": 1, "warnings": []})
    env.rt.store.transact(f"cases/{cid}/checkpoints/ingestion", lambda d: {**d, "lease_until": now() - 1})
    cp.doc["lease_until"] = now() - 1
    cp._mirror_lease()
    report = asyncio.run(Healer(env.rt).heal())
    assert cid in report["redriven"]
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "awaiting_approval"


def test_idempotent_redelivery_is_ignored(env):
    env.create()
    first = env.rt.bus.published[0]
    env.drain()
    ingestion = next(s for s in env.rt.bus.subscriptions if s[0] == "ingestion")[2]
    assert asyncio.run(ingestion(first)) == "duplicate"


def test_application_blocked_on_fraud_signals():
    env = Env(ingestion={"red_flags": ["asks for bank details before interview"]})
    cid = env.create().json()["case_id"]
    env.drain()
    env.approve(cid)
    env.drain()
    d = env.get(cid).json()
    assert d["case"]["status"] == "failed" and "fraud" in d["case"]["error"]
    assert any(m["type"] == "application.blocked" for m in d["board"])


def test_agent_service_rejects_forged_events_and_wrong_callers():
    env = Env()
    env.rt.settings.push_invoker_sa = "push@p.iam.gserviceaccount.com"
    from jobpilot.agents.application import ApplicationAgent
    app = TestClient(create_agent_app(env.rt, ApplicationAgent(env.rt)))

    def token(email):
        b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        return f"Bearer {b({'alg': 'RS256'})}.{b({'email': email})}.SIGNATURE_REMOVED_BY_GOOGLE"

    forged = Event(type="application.approved", case_id="x", actor="ingestion")   # ingestion may not emit this
    body = {"message": {"data": base64.b64encode(forged.to_json().encode()).decode()}}
    assert app.post("/events", json=body, headers={"authorization": token("evil@x.com")}).status_code == 403
    r = app.post("/events", json=body, headers={"authorization": token("push@p.iam.gserviceaccount.com")})
    assert r.status_code == 204   # acknowledged but dropped by the capability policy
    assert env.rt.store.get("cases/x/checkpoints/application") is None


def test_user_retry_after_attempts_are_exhausted():
    env = Env()
    env.rt.settings.max_agent_attempts = 2
    env.rt.checkpointer.max_attempts = 2
    env.rt.faults["evaluation:step:assess"] = 3            # more failures than attempts allow
    cid = env.create().json()["case_id"]
    env.drain()
    asyncio.run(Healer(env.rt).heal())                     # healer gives up cleanly -> case failed
    env.drain()
    d = env.get(cid).json()
    assert d["case"]["status"] == "failed"
    r = env.client.post(f"/api/cases/{cid}/retry", headers={"x-dev-user": "ola@example.com"})
    assert r.status_code == 200 and r.json()["redriven"] == "ingestion.completed"
    env.drain()
    assert env.get(cid).json()["case"]["status"] == "awaiting_approval"
    other = env.client.post(f"/api/cases/{cid}/retry", headers={"x-dev-user": "ola@example.com"})
    assert other.status_code == 409                         # nothing to retry once it's healthy
