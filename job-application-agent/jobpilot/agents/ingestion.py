"""Ingestion specialist: raw resume + job description -> structured, verified records."""
from __future__ import annotations

import io
import re

from google.adk.tools import ToolContext

from ..core import bus as ev
from ..core.mesh import INJECTION_PATTERNS
from ..rag.grounding import contains, ground_profile
from .base import Ctx, PermanentError, Specialist

ROLE_RE = re.compile(r"^[^|]+\|[^|]+\|\s*\d{4}(-\d{2})?\s*\|\s*(\d{4}(-\d{2})?|present|current)\s*$", re.I)


def record_candidate_profile(full_name: str, location: str, summary: str, skills: list[str],
                             roles: list[str], certifications: list[str], education: list[str],
                             work_authorization_stated: str, tool_context: ToolContext) -> dict:
    """Stores the structured candidate profile extracted from the resume.

    Args:
        full_name: Candidate name as written.
        location: City/region as written ("" if absent).
        summary: Two-sentence factual summary.
        skills: Skills/tools exactly as evidenced in the resume.
        roles: One per role: "Title | Company | YYYY-MM | YYYY-MM or present".
        certifications: Certifications as written.
        education: Degrees as written ("BSc Computer Science, University X, 2015").
        work_authorization_stated: Only if the resume states it, else "".
    """
    bad = [r for r in roles if not ROLE_RE.match(r.strip())]
    if bad:
        return {"status": "error", "message": f"Roles must be 'Title | Company | YYYY-MM | YYYY-MM or present'. Fix: {bad[:3]}"}
    tool_context.state["out_candidate_profile"] = {
        "full_name": full_name, "location": location, "summary": summary, "skills": skills[:80],
        "roles": roles[:30], "certifications": certifications[:30], "education": education[:10],
        "work_authorization_stated": work_authorization_stated}
    return {"status": "ok", "roles": len(roles), "skills": len(skills)}


def record_job_posting(title: str, company: str, location: str, remote_policy: str, must_haves: list[str],
                       nice_to_haves: list[str], required_years_experience: float,
                       certifications_required: list[str], education_required: str, red_flags: list[str],
                       apply_email: str, tool_context: ToolContext) -> dict:
    """Stores the structured job posting.

    Args:
        title: Role title.
        company: Hiring company ("unknown" if absent).
        location: Location as written.
        remote_policy: "remote", "hybrid", "onsite" or "unspecified".
        must_haves: Requirements the posting states as required.
        nice_to_haves: Preferred / bonus qualifications.
        required_years_experience: Minimum years stated (0 if none).
        certifications_required: Required certifications (empty if none).
        education_required: Required education ("" if none).
        red_flags: Fraud signals (asks for payment, bank details, ID numbers, off-platform chat). Empty if none.
        apply_email: Application email address only if the posting explicitly gives one, else "".
    """
    tool_context.state["out_job_posting"] = {
        "title": title, "company": company, "location": location, "remote_policy": remote_policy,
        "must_haves": must_haves[:40], "nice_to_haves": nice_to_haves[:40],
        "required_years_experience": float(required_years_experience or 0),
        "certifications_required": certifications_required[:20], "education_required": education_required,
        "red_flags": red_flags[:10],
        "apply_email": apply_email.strip() if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", apply_email.strip() or "x") else ""}
    return {"status": "ok", "must_haves": len(must_haves), "red_flags": len(red_flags)}


def neutralize(text: str) -> tuple[str, int]:
    """Removes instruction-like text from untrusted documents (keeps the rest usable)."""
    count = 0
    for pattern, _ in INJECTION_PATTERNS:
        text, n = re.subn(pattern, "[instruction-like text removed]", text, flags=re.IGNORECASE)
        count += n
    return text, count


class IngestionAgent(Specialist):
    name = "ingestion"
    triggers = {ev.CASE_CREATED}

    async def run(self, ctx: Ctx) -> None:
        rt = self.rt

        def load_inputs():
            opts = ctx.mem("options", {}) or {}
            resume = ctx.mem("resume_raw", "") or ""
            job = ctx.mem("job_raw", "") or ""
            warnings = []
            if not resume and opts.get("resume_gcs_uri"):
                resume = self._load_gcs(opts["resume_gcs_uri"])
            if not job and opts.get("job_url"):
                job = self._fetch(rt.mesh.scan_url(opts["job_url"]))
            if len(resume) < 200:
                raise PermanentError("Resume missing or too short (<200 characters).")
            if len(job) < 150:
                raise PermanentError("Job description missing or too short (could not fetch it? paste the text).")
            # Defense in depth: fetched/uploaded documents are scanned again and neutralized.
            for key, text in (("resume_raw", resume), ("job_raw", job)):
                scan = rt.mesh.scan_input(text, key, untrusted_document=True)
                clean, removed = neutralize(scan.text)
                if removed or scan.findings:
                    warnings.append(f"{key}: {', '.join(scan.findings) or 'instruction-like text removed'}")
                ctx.write(key, clean)
            return {"resume_chars": len(resume), "job_chars": len(job), "warnings": warnings}

        loaded = await ctx.step("load_inputs", load_inputs)
        if loaded["warnings"]:
            ctx.post("ingestion.warning", "Untrusted content was sanitized: " + "; ".join(loaded["warnings"]))

        async def extract():
            out = await ctx.llm(
                stages=["extraction"],
                prompt="Extract the candidate profile and the job posting, then record both.",
                inputs={"resume_raw": ctx.mem("resume_raw"), "job_raw": ctx.mem("job_raw")},
                extra_tools=[record_candidate_profile, record_job_posting],
                deliverables={"record_candidate_profile": "extraction", "record_job_posting": "extraction"},
            )
            if "out_candidate_profile" not in out or "out_job_posting" not in out:
                raise RuntimeError("Extraction finished without recording both records.")
            return {"candidate_profile": out["out_candidate_profile"], "job_posting": out["out_job_posting"]}

        records = await ctx.step("extract", extract)

        def ground_and_resolve():
            resume, job = ctx.mem("resume_raw", ""), ctx.mem("job_raw", "")
            # Anti-hallucination: keep only skills/certs that literally appear in the resume.
            profile, removed = ground_profile(records["candidate_profile"], resume)
            posting = dict(records["job_posting"])
            if posting.get("apply_email") and not contains(posting["apply_email"], job):
                posting["apply_email"] = ""  # never invent a recipient
            rt.ensure_policies()
            role = rt.retriever.resolve_role(posting.get("title", ""), job)
            if role["policy_id"]:
                rt.graph.node(ctx.case_id, "step", f"RAG: matched policy {role['policy_id']} v{role['version']} "
                              f"(confidence {role['confidence']})", self.name, [ctx.cursor])
            return {"candidate_profile": profile, "job_posting": posting, "removed": removed, "role_policy": role}

        grounded = await ctx.step("ground_and_resolve", ground_and_resolve)
        if grounded["removed"]:
            ctx.post("grounding.removed", "Dropped extracted items not found in the resume: "
                     + ", ".join(grounded["removed"][:12]))
        records = {**records, **grounded}

        async def commit():
            ctx.write("candidate_profile", records["candidate_profile"])
            ctx.write("job_posting", records["job_posting"])
            ctx.write("role_policy", records["role_policy"])
            jp = records["job_posting"]
            rt.set_case_status(ctx.case_id, "evaluating", job={"title": jp["title"], "company": jp["company"]})
            ctx.post(ev.INGESTION_COMPLETED,
                     f"Profile: {len(records['candidate_profile']['roles'])} roles, "
                     f"{len(records['candidate_profile']['skills'])} skills. Job: {jp['title']} at {jp['company']} "
                     f"({len(jp['must_haves'])} must-haves, {len(jp['red_flags'])} red flags). Role policy: "
                     f"{records['role_policy'].get('title') or 'none matched'}.")
            await ctx.emit(ev.INGESTION_COMPLETED)
            return True

        await ctx.step("commit", commit)

    def _load_gcs(self, uri: str) -> str:
        if not uri.startswith("gs://"):
            raise PermanentError("resume_gcs_uri must start with gs://")
        from google.cloud import storage
        bucket, _, name = uri[5:].partition("/")
        data = storage.Client(project=self.rt.settings.project_id or None).bucket(bucket).blob(name).download_as_bytes()
        if name.lower().endswith(".pdf"):
            from pypdf import PdfReader
            return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
        if name.lower().endswith(".docx"):
            import docx
            return "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
        return data.decode("utf-8", errors="ignore")

    @staticmethod
    def _fetch(url: str) -> str:
        import httpx
        from bs4 import BeautifulSoup
        r = httpx.get(url, timeout=20, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 (JobPilot job-application assistant)"})
        if r.is_redirect:
            raise PermanentError("Job URL redirects; paste the final URL or the job text.")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
            tag.decompose()
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
        return text[:30000]
