"""Evaluation specialist: candidate_profile vs job_posting -> confidence score + APPROVE/DENY."""
from __future__ import annotations

from google.adk.tools import ToolContext

from job_app_agent.tools.scoring import compute_employment, compute_score

from ..core import bus as ev
from ..core.runtime import get_runtime
from ..rag.grounding import HARD_CAP_MARGIN, skills_ceiling, validate_citations
from .base import Ctx, Specialist


def search_role_policy(query: str, tool_context: ToolContext) -> dict:
    """Searches the role-policy knowledge base (RAG) for standards relevant to a question.

    Args:
        query: What you need to check, e.g. "Kubernetes expectations for a senior DevOps engineer".
    """
    st = tool_context.state
    rt = get_runtime()
    hits = rt.retriever.search(query, k=4, policy_id=st.get("_policy_id") or None)
    allowed = set(st.get("_allowed_citations", [])) | {h["chunk_id"] for h in hits}
    st["_allowed_citations"] = sorted(allowed)
    return {"results": [{"chunk_id": h["chunk_id"], "text": h["text"]} for h in hits],
            "note": "Cite chunk_id values in policy_citations."}


def score_resume_match(skills: float, experience: float, certifications: float, education: float,
                       location_eligibility: float, recency_dates: float, unmet_must_haves: list[str],
                       strengths: list[str], gaps: list[str], evidence_summary: str,
                       policy_citations: list[str], tool_context: ToolContext) -> dict:
    """Computes the final confidence score (0-100) and APPROVE/DENY decision (>= 80 approves).

    Each dimension is 0-100; use -1 for certifications, education or location_eligibility when the
    posting does not mention them.

    Args:
        skills: Match of required + preferred skills/tools.
        experience: Relevance, seniority and scope of experience.
        certifications: Required/preferred certifications held (-1 if none required).
        education: Degree/field match (-1 if not specified).
        location_eligibility: Location / remote / stated work-authorization fit (-1 if not specified).
        recency_dates: Recency of relevant experience and date consistency.
        unmet_must_haves: Must-haves from job_posting the profile does not show. Empty list if none.
        strengths: Top matching points with evidence.
        gaps: Missing or weak points.
        evidence_summary: Two to four sentences justifying the scores.
        policy_citations: Role-policy chunk ids (e.g. "devops_engineer#core_skills") your judgement relied on.
    """
    st = tool_context.state
    notes = []
    coverage = st.get("_coverage")
    if st.get("_policy_id"):
        valid, invalid = validate_citations(policy_citations, set(st.get("_allowed_citations", [])))
        if invalid or not valid:
            return {"status": "error",
                    "message": f"Cite at least one retrieved role-policy chunk, and only those. Invalid: {invalid}. "
                               f"Allowed: {st.get('_allowed_citations', [])}"}
        policy_citations = valid
        ceiling = skills_ceiling(coverage["relevant_coverage"])
        evidenced = (f"{len(coverage['relevant_matched'])}/"
                     f"{len(coverage['relevant_matched']) + len(coverage['relevant_missing'])}")
        if skills > ceiling + HARD_CAP_MARGIN:   # big gap: cap it
            notes.append(f"skills score capped {skills} -> {ceiling + HARD_CAP_MARGIN}: the resume evidences only "
                         f"{evidenced} of the skills/tools this posting asks for")
            skills = ceiling + HARD_CAP_MARGIN
        elif skills > ceiling:                    # small gap: warn, don't change the score
            notes.append(f"warning: skills score {skills} is above what the resume evidences ({evidenced} of the "
                         f"posting's skills/tools); not capped")
    required = float(st.get("_required_years", 0) or 0)
    actual = st.get("_actual_years")
    result = compute_score(
        {"skills": skills, "experience": experience, "certifications": certifications, "education": education,
         "location_eligibility": location_eligibility, "recency_dates": recency_dates},
        unmet_must_haves, required, actual)
    result.update({"policy_citations": policy_citations, "policy_alignment": coverage, "grounding_notes": notes,
                   "strengths": strengths[:10], "gaps": gaps[:10], "evidence_summary": evidence_summary[:1500],
                   "dimension_scores": {"skills": skills, "experience": experience, "certifications": certifications,
                                        "education": education, "location_eligibility": location_eligibility,
                                        "recency_dates": recency_dates}})
    st["out_evaluation"] = result
    return result


class EvaluationAgent(Specialist):
    name = "evaluation"
    triggers = {ev.INGESTION_COMPLETED}

    async def run(self, ctx: Ctx) -> None:
        rt = self.rt
        profile = ctx.mem("candidate_profile") or {}
        posting = ctx.mem("job_posting") or {}

        dates = await ctx.step("employment_dates", lambda: compute_employment(profile.get("roles", [])))

        def retrieve_policy():
            from ..rag.grounding import policy_coverage
            role = ctx.mem("role_policy") or {}
            if not role.get("policy_id"):
                return {"policy_id": None, "chunks": [], "coverage": None}
            rt.ensure_policies()
            policy = rt.policies.get(role["policy_id"])
            query = f"{posting.get('title', '')} " + " ".join(posting.get("must_haves", [])[:15])
            hits = rt.retriever.search(query, k=rt.settings.rag_top_k, policy_id=policy["id"])
            must = [c for c in rt.retriever.search(policy["title"], k=20, policy_id=policy["id"])
                    if c["section"] in ("core_skills", "experience", "guidance")]
            chunks = {c["chunk_id"]: c for c in [*must, *hits]}
            coverage = policy_coverage(policy, ctx.mem("resume_raw", ""), profile, ctx.mem("job_raw", ""))
            rel = coverage["relevant_coverage"]
            rt.graph.node(ctx.case_id, "step", f"RAG: {len(chunks)} chunks from {policy['id']} v{policy['version']}, "
                          f"posting-relevant coverage {'n/a' if rel is None else f'{rel:.0%}'}", self.name, [ctx.cursor])
            return {"policy_id": policy["id"], "version": policy["version"], "coverage": coverage,
                    "chunks": [{"chunk_id": c["chunk_id"], "text": c["text"]} for c in chunks.values()]}

        policy_ctx = await ctx.step("retrieve_policy", retrieve_policy)
        context_text = "\n".join(f"[{c['chunk_id']}] {c['text']}" for c in policy_ctx["chunks"]) or \
            "(no role policy matched this posting; judge against the posting only)"

        async def assess():
            out = await ctx.llm(
                stages=["evaluation"],
                prompt="Evaluate the candidate against the job posting and call score_resume_match.",
                inputs={"candidate_profile": profile, "job_posting": posting, "employment_dates": dates,
                        "role_policy_context": context_text, "policy_coverage": policy_ctx["coverage"] or "n/a"},
                extra_tools=[search_role_policy, score_resume_match],
                deliverables={"score_resume_match": "evaluation"},
                extra_state={"_required_years": float(posting.get("required_years_experience", 0) or 0),
                             "_actual_years": dates.get("total_years_experience"),
                             "_policy_id": policy_ctx["policy_id"] or "", "_coverage": policy_ctx["coverage"],
                             "_allowed_citations": [c["chunk_id"] for c in policy_ctx["chunks"]]},
            )
            if "out_evaluation" not in out:
                raise RuntimeError("Evaluation finished without a score.")
            return out["out_evaluation"]

        evaluation = await ctx.step("assess", assess)

        async def commit():
            ctx.write("evaluation", evaluation)
            decision, score = evaluation["decision"], evaluation["confidence_score"]
            job = (rt.case(ctx.case_id) or {}).get("job", {})
            if decision == "APPROVE":
                rt.set_case_status(ctx.case_id, "drafting", decision=decision, score=score)
                ctx.post(ev.EVALUATION_COMPLETED, f"APPROVE at {score}%. Strengths: {'; '.join(evaluation['strengths'][:3])}",
                         {"decision": decision, "score": score})
            else:
                rt.set_case_status(ctx.case_id, "denied", decision=decision, score=score)
                ctx.post("case.denied", f"DENY at {score}% (< 80). Gaps: {'; '.join(evaluation['gaps'][:3]) or 'see evaluation'}. "
                         f"Unmet must-haves: {'; '.join(evaluation['unmet_must_haves'][:3]) or 'none'}",
                         {"decision": decision, "score": score})
                owner = (rt.case(ctx.case_id) or {}).get("owner", "")
                if owner:
                    rt.memory.user_append_history(owner, {"case_id": ctx.case_id, "job": job, "decision": decision,
                                                          "score": score, "outcome": "denied"}, self.name)
            await ctx.emit(ev.EVALUATION_COMPLETED, {"decision": decision, "score": score})
            return decision

        await ctx.step("commit", commit)
