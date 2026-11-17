"""Document generation & persistence (Cloud Storage, with a local fallback)."""
import io
import os
import re

from google.adk.tools import ToolContext
from google.genai import types

from .. import config
from ..state import K

KINDS = ("tailored_resume", "cover_letter")


def markdown_to_docx(markdown: str) -> bytes:
    """Minimal Markdown -> DOCX (headings, bullets, bold) so the file opens cleanly in Word/Docs."""
    import docx
    from docx.shared import Pt

    d = docx.Document()
    d.styles["Normal"].font.name = "Calibri"
    d.styles["Normal"].font.size = Pt(11)
    for line in markdown.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        m = re.match(r"^(#{1,3})\s+(.*)", s)
        if m:
            d.add_heading(m.group(2).strip(), level=len(m.group(1)))
            continue
        bullet = re.match(r"^\s*[-*•]\s+(.*)", s)
        p = d.add_paragraph(style="List Bullet" if bullet else None)
        text = bullet.group(1) if bullet else s
        for i, chunk in enumerate(re.split(r"\*\*", text)):
            p.add_run(chunk).bold = i % 2 == 1
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "job").lower()).strip("-")[:60] or "job"


def store_bytes(path: str, data: bytes, content_type: str) -> str:
    """Writes to gs://DOCS_BUCKET/path when configured, else ./local_output/path. Returns the location."""
    if config.gcp_enabled() and config.DOCS_BUCKET:
        from google.cloud import storage
        blob = storage.Client(project=config.PROJECT_ID).bucket(config.DOCS_BUCKET).blob(path)
        blob.upload_from_string(data, content_type=content_type)
        return f"gs://{config.DOCS_BUCKET}/{path}"
    local = os.path.join(config.LOCAL_OUTPUT_DIR, path)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)
    return os.path.abspath(local)


def read_bytes(location: str) -> bytes:
    if location.startswith("gs://"):
        from google.cloud import storage
        bucket, _, name = location[5:].partition("/")
        return storage.Client(project=config.PROJECT_ID or None).bucket(bucket).blob(name).download_as_bytes()
    with open(location, "rb") as f:
        return f.read()


async def save_document(kind: str, content_markdown: str, tool_context: ToolContext) -> dict:
    """Saves a tailored resume or cover letter as DOCX + Markdown.

    Call only after that stage's FCoT loop is complete ("tailoring" for tailored_resume,
    "cover_letter" for cover_letter).

    Args:
        kind: "tailored_resume" or "cover_letter".
        content_markdown: The final document in Markdown (# headings, - bullets, **bold**).
    """
    if kind not in KINDS:
        return {"status": "error", "message": f"kind must be one of {KINDS}"}
    meta = tool_context.state.get(K.JOB_META, {})
    session_id = tool_context.session.id if tool_context.session else "session"
    base = f"{tool_context.user_id}/{session_id}/{_slug(meta.get('company'))}-{_slug(meta.get('title'))}/{kind}"

    docx_bytes = markdown_to_docx(content_markdown)
    docx_loc = store_bytes(base + ".docx", docx_bytes,
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    md_loc = store_bytes(base + ".md", content_markdown.encode(), "text/markdown")

    # Also expose it as an ADK artifact so it can be downloaded from the ADK web UI.
    try:
        await tool_context.save_artifact(
            f"{kind}.docx",
            types.Part.from_bytes(data=docx_bytes,
                                  mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )
    except Exception:  # noqa: BLE001 — artifact service is optional
        pass

    docs = dict(tool_context.state.get(K.DOCS, {}))
    docs[kind] = {"docx": docx_loc, "markdown": md_loc}
    tool_context.state[K.DOCS] = docs
    return {"status": "ok", "kind": kind, "docx": docx_loc, "markdown": md_loc}
