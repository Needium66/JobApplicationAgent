"""Runtime configuration, read from environment variables.

Locally these come from job_app_agent/.env (ADK loads it automatically).
On Cloud Run they are passed with `adk deploy cloud_run --env KEY=value`.
"""
import os

# Gemini model. gemini-2.5-* was retired on Vertex on 2026-10-20, so default to a GA 3.x model.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "")

# Google Cloud infrastructure (all optional for local dev — tools fall back to ./local_output).
DOCS_BUCKET = os.getenv("DOCS_BUCKET", "")                 # e.g. my-project-job-agent-docs
FIRESTORE_DATABASE = os.getenv("FIRESTORE_DATABASE", "(default)")
FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "applications")

# Email submission via Gmail API (off by default; see README step 9).
APPLY_EMAIL_ENABLED = os.getenv("APPLY_EMAIL_ENABLED", "false").lower() == "true"
GMAIL_TOKEN_SECRET = os.getenv("GMAIL_TOKEN_SECRET", "gmail-oauth-token")
APPLICANT_EMAIL = os.getenv("APPLICANT_EMAIL", "")

# FCoT + decision policy
FCOT_N = 3
APPROVE_THRESHOLD = 80.0

LOCAL_OUTPUT_DIR = os.getenv("LOCAL_OUTPUT_DIR", "local_output")


def gcp_enabled() -> bool:
    return bool(PROJECT_ID) and os.getenv("JOB_AGENT_LOCAL_ONLY", "false").lower() != "true"
