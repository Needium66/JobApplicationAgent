"""Application specialist: submits an application the applicant explicitly approved."""
from __future__ import annotations

import base64
import json
from email.message import EmailMessage

from google.adk.tools import ToolContext

from job_app_agent.tools.documents import markdown_to_docx

from ..core import bus as ev
from ..core.store import now
from .base import Ctx, PermanentError, Specialist

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def prepare_submission(email_subject: str, email_body: str, tool_context: ToolContext) -> dict:
    """Stores the subject and short body of the application message.

    Args:
        email_subject: Subject naming the role and the applicant.
        email_body: Professional body (<= 150 words); documents are attached separately.
    """
    if len(email_body.split()) > 220:
        return {"status": "error", "message": "Body too long; keep it under 150 words."}
    tool_context.state["out_submission"] = {"subject": email_subject[:200], "body": email_body[:2000]}
    return {"status": "ok"}


class ApplicationAgent(Specialist):
    name = "application"
    triggers = {ev.APPLICATION_APPROVED}

    async def run(self, ctx: Ctx) -> None:
        rt = self.rt
        case = rt.case(ctx.case_id) or {}

        def policy_check():
            evaluation = ctx.mem("evaluation") or {}
            documents = ctx.mem("documents") or {}
            approval = ctx.mem("approval") or {}
            posting = ctx.mem("job_posting") or {}
            problems = []
            if evaluation.get("decision") != "APPROVE":
                problems.append(f"decision is {evaluation.get('decision')}")
            if "cover_letter" not in documents:
                problems.append("no cover letter")
            if not approval or approval.get("approver") not in (case.get("owner"), *rt.settings.admin_emails):
                problems.append("no valid approval from the applicant")
            if posting.get("red_flags"):
                problems.append(f"possible fraud signals: {', '.join(posting['red_flags'][:3])}")
            if approval.get("method") == "email" and not rt.settings.apply_email_enabled:
                problems.append("email submission is disabled")
            profile = rt.memory.application_profile(case.get("owner", ""))
            if approval.get("via") == "swipe" and not (profile.get("full_name") and profile.get("email")):
                problems.append("application profile incomplete (name and email required)")
            if problems:
                ctx.post("application.blocked", "Application blocked by policy: " + "; ".join(problems))
                raise PermanentError("Application blocked: " + "; ".join(problems))
            return {"approval": approval, "profile": profile}

        checked = await ctx.step("policy_check", policy_check)
        approval, profile = checked["approval"], checked.get("profile") or {}

        async def packet():
            out = await ctx.llm(["application_packet"], "Prepare the submission message for the approved application.",
                                {"job_posting": ctx.mem("job_posting"), "evaluation": ctx.mem("evaluation"),
                                 "documents": ctx.mem("documents"), "approval": approval},
                                [prepare_submission], {"prepare_submission": "application_packet"})
            if "out_submission" not in out:
                raise RuntimeError("Packet stage finished without a message.")
            msg = out["out_submission"]
            signature = "\n".join(x for x in [
                profile.get("full_name"), profile.get("email"), profile.get("phone"),
                profile.get("linkedin_url"), profile.get("github_url"), profile.get("portfolio_url")] if x)
            body = msg["body"] + (f"\n\n{signature}" if signature else "")
            msg["body"] = rt.mesh.scan_output(body, agent=self.name).text
            return msg

        message = await ctx.step("packet", packet)

        def submit():
            # At-most-once guard: a crash between "sending" and the checkpoint must not double-send.
            marker = f"cases/{ctx.case_id}/outbox/submission"
            prior = rt.store.get(marker)
            if prior and prior.get("status") == "sent":
                return prior["result"]
            if prior and prior.get("status") == "sending":
                raise PermanentError("A previous submission attempt was interrupted; verify manually before retrying.")
            rt.store.set(marker, {"status": "sending", "at": now()})
            if approval["method"] == "email":
                result = {"method": "email", "message_id": self._send_email(ctx, approval, message), "status": "submitted_email"}
            else:
                posting = ctx.mem("job_posting") or {}
                opts = ctx.mem("options", {}) or {}
                fields = {k: profile.get(k) for k in (
                    "full_name", "email", "phone", "location", "linkedin_url", "github_url", "portfolio_url",
                    "work_authorization", "requires_sponsorship", "willing_to_relocate", "notice_period",
                    "earliest_start_date", "salary_expectation") if profile.get(k)}
                result = {"method": "portal", "status": "ready_for_portal",
                          "packet": {"job_url": opts.get("job_url", ""), "fields": fields,
                                     "standard_answers": profile.get("standard_answers", []),
                                     "documents": list((ctx.mem("documents") or {}).keys()),
                                     "message": message},
                          "next_step": f"Open {opts.get('job_url') or 'the employer portal'}, paste the prepared fields "
                                       f"and upload your documents for {posting.get('title', 'the role')}. "
                                       "JobPilot never logs in to job boards for you."}
            rt.store.set(marker, {"status": "sent", "at": now(), "result": result})
            return result

        result = await ctx.step("submit", submit)

        async def commit():
            record = {**result, "subject": message["subject"], "approved_by": approval["approver"], "at": now()}
            ctx.write("application", record)
            rt.memory.user_append_history(case.get("owner", ""), {
                "case_id": ctx.case_id, "job": case.get("job", {}), "score": case.get("score"),
                "outcome": result["status"], "at": now()}, self.name)
            rt.set_case_status(ctx.case_id, "submitted")
            ctx.post(ev.APPLICATION_SUBMITTED, f"Application {result['status']} via {result['method']}.")
            await ctx.emit(ev.APPLICATION_SUBMITTED, {"status": result["status"]})
            return True

        await ctx.step("commit", commit)

    def _send_email(self, ctx: Ctx, approval: dict, message: dict) -> str:
        rt, s = self.rt, self.rt.settings
        from google.cloud import secretmanager
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        docs = ctx.mem("documents") or {}
        msg = EmailMessage()
        msg["To"], msg["Subject"] = approval["recipient_email"], message["subject"]
        if s.applicant_email:
            msg["From"] = s.applicant_email
        msg.set_content(message["body"])
        resume_loc = docs.get("tailored_resume", {}).get("docx")
        attachments = {"Cover_Letter.docx": rt.blobs.get(docs["cover_letter"]["docx"])}
        attachments["Resume.docx"] = rt.blobs.get(resume_loc) if resume_loc else markdown_to_docx(ctx.mem("resume_raw", ""))
        for name, data in attachments.items():
            maintype, subtype = DOCX.split("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)

        sm = secretmanager.SecretManagerServiceClient()
        info = json.loads(sm.access_secret_version(
            name=f"projects/{s.project_id}/secrets/{s.gmail_token_secret}/versions/latest").payload.data.decode())
        creds = Credentials.from_authorized_user_info(info, ["https://www.googleapis.com/auth/gmail.send"])
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return gmail.users().messages().send(userId="me", body={"raw": raw}).execute().get("id", "")


def all_specialists(rt) -> list[Specialist]:
    from .cover_letter import CoverLetterAgent
    from .evaluation import EvaluationAgent
    from .ingestion import IngestionAgent
    return [IngestionAgent(rt), EvaluationAgent(rt), CoverLetterAgent(rt), ApplicationAgent(rt)]
