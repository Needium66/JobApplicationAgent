"""Application submission + tracking (Firestore, Gmail API, Secret Manager)."""
import base64
import json
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage

from google.adk.tools import ToolContext

from .. import config
from ..state import K
from .documents import markdown_to_docx, read_bytes, store_bytes

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _record(app_id: str, record: dict) -> str:
    if config.gcp_enabled():
        from google.cloud import firestore
        db = firestore.Client(project=config.PROJECT_ID, database=config.FIRESTORE_DATABASE)
        db.collection(config.FIRESTORE_COLLECTION).document(app_id).set(record)
        return f"firestore://{config.FIRESTORE_COLLECTION}/{app_id}"
    return store_bytes(f"applications/{app_id}.json", json.dumps(record, indent=2, default=str).encode(),
                       "application/json")


def _gmail_service():
    from google.cloud import secretmanager
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    sm = secretmanager.SecretManagerServiceClient()
    name = f"projects/{config.PROJECT_ID}/secrets/{config.GMAIL_TOKEN_SECRET}/versions/latest"
    info = json.loads(sm.access_secret_version(name=name).payload.data.decode())
    creds = Credentials.from_authorized_user_info(info, ["https://www.googleapis.com/auth/gmail.send"])
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _send_email(to: str, subject: str, body: str, attachments: dict[str, str]) -> str:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    if config.APPLICANT_EMAIL:
        msg["From"] = config.APPLICANT_EMAIL
    msg.set_content(body)
    for filename, loc in attachments.items():
        msg.add_attachment(read_bytes(loc), maintype="application",
                           subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                           filename=filename)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    sent = _gmail_service().users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent.get("id", "")


def submit_application(method: str, recipient_email: str, email_subject: str, email_body: str,
                       tool_context: ToolContext) -> dict:
    """Submits the application. The user is asked to confirm before this runs.

    Args:
        method: "email" to send the resume + cover letter to a recruiter/hiring address via Gmail,
            or "portal" to log a hand-off packet the user submits on the employer's site.
        recipient_email: Recipient address for method="email"; empty string for "portal".
        email_subject: Subject line for email (or a short label for portal).
        email_body: Short email body (the cover letter is attached separately).
    """
    ev = tool_context.state.get(K.EVAL, {})
    docs = tool_context.state.get(K.DOCS, {})
    meta = tool_context.state.get(K.JOB_META, {})
    app_id = uuid.uuid4().hex[:12]
    attachments = {}
    resume_doc = docs.get("tailored_resume", {}).get("docx")
    if not resume_doc and tool_context.state.get(K.RESUME):
        # No tailoring requested: attach the original resume as submitted.
        resume_doc = store_bytes(f"{tool_context.user_id}/original_resume_{app_id}.docx",
                                 markdown_to_docx(tool_context.state[K.RESUME]),
                                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if resume_doc:
        attachments["Resume.docx"] = resume_doc
    if docs.get("cover_letter", {}).get("docx"):
        attachments["Cover_Letter.docx"] = docs["cover_letter"]["docx"]

    record = {
        "id": app_id, "user_id": tool_context.user_id, "job": meta,
        "confidence_score": ev.get("confidence_score"), "decision": ev.get("decision"),
        "documents": docs, "method": method, "recipient": recipient_email,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "pending",
    }

    if method == "email":
        if not config.APPLY_EMAIL_ENABLED:
            return {"status": "error", "message": "Email submission is disabled (APPLY_EMAIL_ENABLED=false). Use method='portal'."}
        if not EMAIL_RE.match(recipient_email or ""):
            return {"status": "error", "message": "A valid recipient_email is required."}
        if not resume_doc:
            return {"status": "error", "message": "No resume available to attach."}
        record["gmail_message_id"] = _send_email(recipient_email, email_subject, email_body, attachments)
        record["status"] = "submitted_email"
    elif method == "portal":
        record["status"] = "ready_for_portal"
        record["next_step"] = (f"Open {meta.get('url') or 'the employer portal'} and upload the attached documents. "
                               "The agent does not log in to portals on your behalf at Level 1.")
    else:
        return {"status": "error", "message": "method must be 'email' or 'portal'."}

    record["stored_at"] = _record(app_id, record)
    tool_context.state[K.APPLICATION] = record
    return {"status": "ok", **{k: record[k] for k in ("id", "status", "stored_at")},
            "attachments": list(attachments), "next_step": record.get("next_step", "")}


def get_application_status(tool_context: ToolContext) -> dict:
    """Returns what the agent has done so far in this session."""
    s = tool_context.state
    return {
        "resume_loaded": bool(s.get(K.RESUME)),
        "job_loaded": bool(s.get(K.JD)), "job": s.get(K.JOB_META, {}),
        "fcot_progress": {st: len(s.get(K.fcot(st), [])) for st in ("evaluation", "tailoring", "cover_letter")},
        "evaluation": {k: s.get(K.EVAL, {}).get(k) for k in ("confidence_score", "decision")},
        "documents": s.get(K.DOCS, {}),
        "application": s.get(K.APPLICATION, {}).get("status"),
    }
