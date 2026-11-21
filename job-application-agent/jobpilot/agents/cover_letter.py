"""Cover-letter specialist: writes the cover letter (and an optional tailored resume)."""
from __future__ import annotations

from google.adk.tools import ToolContext

from job_app_agent.tools.documents import markdown_to_docx

from ..core import bus as ev
from ..core.runtime import get_runtime
from ..rag.grounding import unsupported_claims
from .base import Ctx, Specialist

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def save_document(kind: str, content_markdown: str, tool_context: ToolContext) -> dict:
    """Saves a tailored resume or cover letter (Markdown -> DOCX + Markdown).

    Args:
        kind: "tailored_resume" or "cover_letter".
        content_markdown: The final document in Markdown (# headings, - bullets, **bold**).
    """
    if kind not in ("tailored_resume", "cover_letter"):
        return {"status": "error", "message": "kind must be tailored_resume or cover_letter"}
    if kind == "tailored_resume" and "tailoring" not in tool_context.state.get("_stages", []):
        return {"status": "error", "message": "Tailoring was not requested for this case."}
    rt = get_runtime()
    st = tool_context.state
    # Grounding: no policy skill/tool/certification the resume doesn't evidence.
    bad = unsupported_claims(content_markdown, st.get("_resume", ""), st.get("_policy"))
    if bad:
        return {"status": "error", "message": f"Rejected: the document claims {bad}, which the resume does not show. "
                                              "Remove or rephrase those claims and save again."}
    scan = rt.mesh.scan_output(content_markdown, agent="cover_letter")  # Defense Mesh on agent output
    base = f"{st['_owner'].replace('@', '_at_')}/{st['_case_id']}/{kind}"
    docx_loc = rt.blobs.put(base + ".docx", markdown_to_docx(scan.text), DOCX)
    md_loc = rt.blobs.put(base + ".md", scan.text.encode(), "text/markdown")
    docs = dict(st.get("out_documents", {}))
    docs[kind] = {"docx": docx_loc, "markdown": md_loc, "words": len(scan.text.split()), "mesh_findings": scan.findings}
    st["out_documents"] = docs
    return {"status": "ok", "kind": kind, "docx": docx_loc, "mesh_findings": scan.findings}


class CoverLetterAgent(Specialist):
    name = "cover_letter"
    triggers = {ev.EVALUATION_COMPLETED}

    def accepts(self, event):
        return event.type in self.triggers and event.data.get("decision") == "APPROVE"

    async def run(self, ctx: Ctx) -> None:
        rt = self.rt
        options = ctx.mem("options", {}) or {}
        owner = (rt.case(ctx.case_id) or {}).get("owner", "unknown")
        role = ctx.mem("role_policy") or {}
        policy = rt.policies.get(role["policy_id"]) if role.get("policy_id") else None
        policy_text = ""
        if policy:
            hits = rt.retriever.search(f"{policy['title']} responsibilities keywords", k=4, policy_id=policy["id"])
            policy_text = "\n".join(f"[{h['chunk_id']}] {h['text']}" for h in hits)
        inputs = {"resume_raw": ctx.mem("resume_raw"), "candidate_profile": ctx.mem("candidate_profile"),
                  "job_posting": ctx.mem("job_posting"), "evaluation": ctx.mem("evaluation"),
                  "role_policy_context": policy_text or "(no role policy matched)"}
        # Claims are checked against the vocabulary of ALL role policies, not just the matched one.
        grounding_state = {"_owner": owner, "_resume": ctx.mem("resume_raw", ""), "_policy": rt.policies.lexicon()}
        if (ctx.mem("evaluation") or {}).get("decision") != "APPROVE":
            return  # defense in depth: never draft for a DENY even if an event slips through

        documents: dict = {}
        if options.get("tailor_resume"):
            async def tailor():
                out = await ctx.llm(["tailoring"], "Tailor the resume truthfully for this posting and save it.",
                                    inputs, [save_document], {"save_document": "tailoring"},
                                    extra_state=grounding_state)
                if "tailored_resume" not in out.get("out_documents", {}):
                    raise RuntimeError("Tailoring finished without saving the tailored resume.")
                return out["out_documents"]["tailored_resume"]
            documents["tailored_resume"] = await ctx.step("tailoring", tailor)

        async def letter():
            out = await ctx.llm(["cover_letter"], "Write the cover letter for this posting and save it.",
                                inputs, [save_document], {"save_document": "cover_letter"},
                                extra_state=grounding_state)
            if "cover_letter" not in out.get("out_documents", {}):
                raise RuntimeError("Cover-letter stage finished without saving the letter.")
            return out["out_documents"]["cover_letter"]
        documents["cover_letter"] = await ctx.step("cover_letter", letter)

        async def commit():
            ctx.write("documents", documents)
            rt.set_case_status(ctx.case_id, "awaiting_approval")
            ctx.post(ev.DOCUMENTS_READY, f"Documents ready: {', '.join(documents)}. Waiting for the applicant's approval.",
                     {"documents": list(documents)})
            await ctx.emit(ev.DOCUMENTS_READY, {"documents": list(documents)})
            return True

        await ctx.step("commit", commit)
