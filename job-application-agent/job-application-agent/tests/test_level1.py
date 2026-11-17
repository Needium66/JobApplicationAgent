"""Offline tests: deterministic scoring, date analysis, FCoT state machine, policy guards.

Run:  pytest -q
"""
import os
from datetime import date
from types import SimpleNamespace

os.environ.setdefault("JOB_AGENT_LOCAL_ONLY", "true")

from job_app_agent.guards import enforce_contract  # noqa: E402
from job_app_agent.state import K  # noqa: E402
from job_app_agent.tools.fcot import fcot_record_iteration, loop_complete  # noqa: E402
from job_app_agent.tools.scoring import compute_employment, compute_score  # noqa: E402


class Ctx(SimpleNamespace):
    def __init__(self, state=None):
        super().__init__(state=state or {}, user_id="u1", session=SimpleNamespace(id="s1"))


def tool(name):
    return SimpleNamespace(name=name)


def run_loop(ctx, stage, verified_last=True):
    for i in (1, 2, 3):
        r = fcot_record_iteration(stage, i, "recap", "reason",
                                  ["PASS: evidence cited"] if (i < 3 or verified_last) else ["FAIL: unsupported claim"],
                                  True, ctx)
        assert r["status"] == "ok", r
    return r


# ---------- scoring ----------
def test_strong_match_approves():
    r = compute_score({"skills": 90, "experience": 85, "certifications": 80, "education": 90,
                       "location_eligibility": 100, "recency_dates": 90}, [])
    assert r["decision"] == "APPROVE" and r["confidence_score"] >= 80


def test_threshold_boundary():
    r = compute_score({"skills": 80, "experience": 80, "certifications": -1, "education": -1,
                       "location_eligibility": -1, "recency_dates": 80}, [])
    assert r["confidence_score"] == 80.0 and r["decision"] == "APPROVE"


def test_unmet_must_have_caps_below_80():
    r = compute_score({"skills": 95, "experience": 95, "certifications": 95, "education": 95,
                       "location_eligibility": 95, "recency_dates": 95}, ["Active Secret clearance"])
    assert r["confidence_score"] == 79.0 and r["decision"] == "DENY"


def test_years_shortfall_becomes_must_have():
    r = compute_score({"skills": 95, "experience": 95, "certifications": -1, "education": -1,
                       "location_eligibility": -1, "recency_dates": 95}, [], required_years=8, actual_years=3)
    assert r["decision"] == "DENY" and any("Years" in u for u in r["unmet_must_haves"])


def test_not_applicable_dimensions_renormalise():
    r = compute_score({"skills": 70, "experience": 70, "certifications": -1, "education": -1,
                       "location_eligibility": -1, "recency_dates": 70}, [])
    assert abs(sum(r["applied_weights"].values()) - 1) < 0.01 and r["confidence_score"] == 70.0


# ---------- dates ----------
def test_employment_merges_overlaps_and_finds_gaps():
    r = compute_employment([
        "Engineer | A | 2015-01 | 2018-12",
        "Consultant | B | 2018-06 | 2019-12",   # overlaps A
        "Senior Engineer | C | 2021-01 | present",  # 12-month gap
    ], today=date(2026, 11, 15))
    assert r["roles_parsed"] == 3
    assert r["overlapping_roles"]
    assert r["gaps_6mo_plus"][0]["months"] == 12
    assert 10.5 <= r["total_years_experience"] <= 11


def test_employment_rejects_bad_dates():
    r = compute_employment(["X | Y | 2020-05 | 2019-01", "bad line"], today=date(2026, 1, 1))
    assert r["roles_parsed"] == 0 and len(r["errors"]) == 2


# ---------- FCoT loop ----------
def test_loop_requires_order_and_n3():
    ctx = Ctx()
    assert fcot_record_iteration("evaluation", 2, "r", "r", ["PASS: x"], True, ctx)["status"] == "error"
    run_loop(ctx, "evaluation")
    assert loop_complete(ctx.state, "evaluation")
    assert fcot_record_iteration("evaluation", 4, "r", "r", ["PASS: x"], True, ctx)["status"] == "error"


def test_failed_verify_on_iteration_3_blocks_completion():
    ctx = Ctx()
    r = run_loop(ctx, "cover_letter", verified_last=False)
    assert r["verified"] is False and not loop_complete(ctx.state, "cover_letter")


# ---------- guards ----------
def test_guards_enforce_pipeline():
    ctx = Ctx({K.RESUME: "r" * 300, K.JD: "j" * 300})
    assert enforce_contract(tool("score_resume_match"), {}, ctx)["status"] == "blocked_by_policy"
    run_loop(ctx, "evaluation")
    assert enforce_contract(tool("score_resume_match"), {}, ctx) is None

    ctx.state[K.EVAL] = {"decision": "DENY", "confidence_score": 72}
    assert enforce_contract(tool("submit_application"), {}, ctx)["status"] == "blocked_by_policy"

    ctx.state[K.EVAL] = {"decision": "APPROVE", "confidence_score": 86}
    assert enforce_contract(tool("save_document"), {"kind": "cover_letter"}, ctx)["status"] == "blocked_by_policy"
    run_loop(ctx, "cover_letter")
    assert enforce_contract(tool("save_document"), {"kind": "cover_letter"}, ctx) is None
    assert enforce_contract(tool("submit_application"), {}, ctx)["status"] == "blocked_by_policy"  # no doc yet
    ctx.state[K.DOCS] = {"cover_letter": {"docx": "x"}}
    assert enforce_contract(tool("submit_application"), {}, ctx) is None
