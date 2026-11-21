"""A deterministic stand-in for Gemini (tests + offline demo).

It reads the STATE_JSON line of the agent's live instruction and makes the call a
well-behaved model would make next: FCoT iterations in order (continuing from
checkpointed progress), then the stage deliverable, then a final summary.
"""
from __future__ import annotations

import json
import re

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def _call(name, **args):
    return types.Part(function_call=types.FunctionCall(name=name, args=args))


PROFILE = dict(full_name="Jane Doe", location="Dallas, TX", summary="Cloud engineer with 9 years on GCP.",
               skills=["Python", "GCP", "Terraform", "Kubernetes", "CI/CD"],
               roles=["Cloud Engineer | Acme | 2019-01 | present", "DevOps Engineer | Beta | 2016-03 | 2018-12"],
               certifications=["Google Professional Cloud Architect"], education=["BSc Computer Science"],
               work_authorization_stated="")
POSTING = dict(title="Senior Cloud Engineer", company="Contoso", location="Remote (US)", remote_policy="remote",
               must_haves=["5+ years cloud engineering", "GCP", "Terraform", "Kubernetes"], nice_to_haves=["PCA"],
               required_years_experience=5, certifications_required=[], education_required="", red_flags=[],
               apply_email="")
STRONG = dict(skills=92, experience=88, certifications=100, education=-1, location_eligibility=100,
              recency_dates=95, unmet_must_haves=[], strengths=["GCP + Terraform + Kubernetes at Acme"],
              gaps=[], evidence_summary="All must-haves evidenced in recent roles.")
WEAK = dict(skills=55, experience=50, certifications=-1, education=-1, location_eligibility=100,
            recency_dates=70, unmet_must_haves=["Kubernetes"], strengths=["Python"],
            gaps=["No Kubernetes", "Limited GCP"], evidence_summary="Several must-haves missing.")


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    agent_name: str = ""
    weak_candidate: bool = False
    fail_first: int = 0           # raise a transient 503 this many times (auto-healing tests)
    calls: int = 0
    _last_error: str = ""
    red_flags: list[str] = []
    hallucinate_letter: bool = False   # first draft claims skills the resume lacks (grounding test)
    bad_citations: bool = False        # first score cites a chunk that was never retrieved (grounding test)
    inflate_skills: bool = False       # claims a 100 skills score (ceiling test)
    extra_skills: list[str] = []       # extraction "hallucinates" these skills (grounding test)
    apply_email: str = ""

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.fail_first > 0:
            self.fail_first -= 1
            raise RuntimeError("503 UNAVAILABLE: model overloaded (scripted)")
        instr = llm_request.config.system_instruction if llm_request.config else ""
        if not isinstance(instr, str):
            instr = "".join(p.text or "" for p in getattr(instr, "parts", []) or [])
        m = re.search(r"STATE_JSON: (\{.*\})", instr)
        state = json.loads(m.group(1)) if m else {}
        self._last_error = self._last_tool_error(llm_request)
        part = self._next(state)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))

    @staticmethod
    def _last_tool_error(llm_request) -> str:
        for content in reversed(llm_request.contents or []):
            for p in content.parts or []:
                fr = getattr(p, "function_response", None)
                if fr is not None:
                    resp = fr.response or {}
                    return f"{fr.name}:{resp.get('message', '')}" if resp.get("status") == "error" else ""
        return ""

    def _next(self, state: dict) -> types.Part:
        done = state.get("fcot_iterations_done", {})
        recorded = set(state.get("deliverables_recorded", []))
        for stage in state.get("stages_to_run", []):
            n = done.get(stage, 0)
            if n < 3:
                return _call("fcot_record_iteration", stage=stage, iteration=n + 1,
                             recap=f"Recap {stage} #{n + 1}: inputs from shared memory and board.",
                             reason=f"Reason {stage} #{n + 1}: drafted/refined from evidence.",
                             verify_checks=["PASS: grounded in resume", "PASS: no fabrication",
                                            "PASS: meets success criteria"], verified=True)
            if self.agent_name == "ingestion":
                if "candidate_profile" not in recorded:
                    return _call("record_candidate_profile", **{**PROFILE, "skills": PROFILE["skills"] + self.extra_skills})
                if "job_posting" not in recorded:
                    return _call("record_job_posting", **{**POSTING, "red_flags": self.red_flags,
                                                          "apply_email": self.apply_email})
            elif self.agent_name == "evaluation" and "evaluation" not in recorded:
                args = dict(WEAK if self.weak_candidate else STRONG)
                if self.inflate_skills:
                    args["skills"] = 100
                ids = state.get("policy_chunk_ids", [])
                retried = self._last_error.startswith("score_resume_match")
                args["policy_citations"] = (["made_up_policy#core_skills"] if self.bad_citations and not retried
                                            else ids[:2])
                return _call("score_resume_match", **args)
            elif self.agent_name == "cover_letter" and "documents" not in recorded:
                kind = "tailored_resume" if stage == "tailoring" else "cover_letter"
                rejected = self._last_error.startswith("save_document")
                extra = "\n- Certified CKA; daily Ansible and Helm user" if self.hallucinate_letter and not rejected else ""
                return _call("save_document", kind=kind,
                             content_markdown=f"# {kind.replace('_', ' ').title()}\nDear Hiring Team,\n"
                                              "- **GCP** and Terraform at Acme since 2019\n- Kubernetes in production" + extra)
            elif self.agent_name == "application" and "submission" not in recorded:
                return _call("prepare_submission", email_subject="Application: Senior Cloud Engineer — Jane Doe",
                             email_body="Dear Hiring Team, please find my resume and cover letter attached.")
        return types.Part(text=f"{self.agent_name}: all deliverables recorded.")
