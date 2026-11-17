"""End-to-end run through the real ADK Runner with a scripted stand-in for Gemini.

Proves: tools are wired, guards block out-of-order calls, the N=3 loop gates
deliverables, documents are written, and submit_application pauses for human
confirmation. No Google Cloud or Gemini calls are made.
"""
import asyncio
import os
import tempfile

os.environ["JOB_AGENT_LOCAL_ONLY"] = "true"
os.environ["LOCAL_OUTPUT_DIR"] = tempfile.mkdtemp()

from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

RESUME = ("Jane Doe — Cloud Engineer, Dallas TX\nSkills: Python, GCP, Terraform, Kubernetes, CI/CD\n"
          "Experience: Cloud Engineer, Acme, 2019-01 to present; DevOps Engineer, Beta, 2016-03 to 2018-12\n"
          "Certifications: Google Professional Cloud Architect\nEducation: BSc Computer Science\n") * 2
JD = ("Senior Cloud Engineer at Contoso (Remote, US). Requirements: 5+ years cloud engineering, GCP, Terraform, "
      "Kubernetes, Python. Preferred: Professional Cloud Architect certification. ") * 2


def call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


def loop(stage):
    return [call("fcot_record_iteration", stage=stage, iteration=i, recap=f"recap {i}", reason=f"reason {i}",
                 verify_checks=["PASS: grounded in resume", "PASS: no fabrication"], verified=True)
            for i in (1, 2, 3)]


SCRIPT = [
    [call("ingest_resume_text", resume_text=RESUME)],
    [call("ingest_job_description_text", job_description=JD, job_title="Senior Cloud Engineer", company="Contoso")],
    [call("analyze_employment_dates", roles=["Cloud Engineer | Acme | 2019-01 | present",
                                              "DevOps Engineer | Beta | 2016-03 | 2018-12"])],
    [call("score_resume_match", skills=90, experience=88, certifications=100, education=85,
          location_eligibility=100, recency_dates=95, unmet_must_haves=[], required_years_experience=5,
          strengths=["GCP + Terraform"], gaps=[], evidence_summary="Strong match.")],  # should be BLOCKED
    *[[p] for p in loop("evaluation")],
    [call("score_resume_match", skills=90, experience=88, certifications=100, education=85,
          location_eligibility=100, recency_dates=95, unmet_must_haves=[], required_years_experience=5,
          strengths=["GCP + Terraform"], gaps=[], evidence_summary="Strong match.")],
    *[[p] for p in loop("cover_letter")],
    [call("save_document", kind="cover_letter", content_markdown="# Cover Letter\nDear Hiring Team,\n- **GCP** expert")],
    [call("submit_application", method="portal", recipient_email="", email_subject="Contoso SCE", email_body="")],
    [types.Part(text="Done.")],
]


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    step: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        parts = SCRIPT[min(self.step, len(SCRIPT) - 1)]
        self.step += 1
        yield LlmResponse(content=types.Content(role="model", parts=parts))


async def _run():
    from job_app_agent.agent import root_agent
    agent = root_agent.model_copy(update={"model": ScriptedLlm()})
    runner = InMemoryRunner(agent=agent, app_name="job_app_agent")
    session = await runner.session_service.create_session(app_name="job_app_agent", user_id="u1")
    responses, confirmation_requested = [], False
    msg = types.Content(role="user", parts=[types.Part(text="Evaluate my resume for this job")])
    async for ev in runner.run_async(user_id="u1", session_id=session.id, new_message=msg):
        for fr in ev.get_function_responses() or []:
            responses.append((fr.name, fr.response))
        for fc in ev.get_function_calls() or []:
            if fc.name == "adk_request_confirmation":
                confirmation_requested = True
    final = await runner.session_service.get_session(app_name="job_app_agent", user_id="u1", session_id=session.id)
    return responses, confirmation_requested, final.state


def test_offline_pipeline():
    responses, confirmation_requested, state = asyncio.run(_run())
    names = [n for n, _ in responses]
    first_score = next(r for n, r in responses if n == "score_resume_match")
    assert first_score["status"] == "blocked_by_policy"           # guard fired before loop
    assert state["evaluation"]["decision"] == "APPROVE"          # deterministic score after loop
    assert state["evaluation"]["confidence_score"] >= 80
    assert "cover_letter" in state["documents"]
    assert os.path.exists(state["documents"]["cover_letter"]["docx"])
    assert confirmation_requested, names                         # human-in-the-loop pause
    assert "application" not in state                            # nothing submitted without confirmation


async def _run_and_confirm():
    from job_app_agent.agent import root_agent
    agent = root_agent.model_copy(update={"model": ScriptedLlm()})
    runner = InMemoryRunner(agent=agent, app_name="job_app_agent")
    session = await runner.session_service.create_session(app_name="job_app_agent", user_id="u1")
    msg = types.Content(role="user", parts=[types.Part(text="go")])
    confirm_call = None
    async for ev in runner.run_async(user_id="u1", session_id=session.id, new_message=msg):
        for fc in ev.get_function_calls() or []:
            if fc.name == "adk_request_confirmation":
                confirm_call = fc
    assert confirm_call is not None
    # The user clicks "Confirm" in the ADK web UI — equivalent to this function response:
    reply = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=confirm_call.id, name="adk_request_confirmation", response={"confirmed": True}))])
    async for _ in runner.run_async(user_id="u1", session_id=session.id, new_message=reply):
        pass
    final = await runner.session_service.get_session(app_name="job_app_agent", user_id="u1", session_id=session.id)
    return final.state


def test_confirmed_submission_is_recorded():
    state = asyncio.run(_run_and_confirm())
    assert state["application"]["status"] == "ready_for_portal"
    assert os.path.exists(state["application"]["stored_at"])
