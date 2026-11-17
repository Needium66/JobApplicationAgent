"""Level 1 — single, monolithic, centrally managed job-application agent.

One LlmAgent (Gemini) orchestrated by Google ADK. FCoT is implemented as:
  * Element 1, Instruction Contract  -> contract.py (rendered into the instruction)
  * Element 2, Recursive Loop (N=3)  -> tools/fcot.py (state machine) + guards.py (enforcement)
"""
from google.adk.agents import Agent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools import FunctionTool
from google.genai import types

from . import config
from .contract import render_instruction
from .guards import enforce_contract
from .state import K
from .tools.application import get_application_status, submit_application
from .tools.documents import save_document
from .tools.fcot import fcot_record_iteration
from .tools.ingest import (fetch_job_description, ingest_job_description_text,
                           ingest_resume_text, load_resume_from_gcs)
from .tools.scoring import analyze_employment_dates, score_resume_match

_STATIC_IC = render_instruction()


def instruction_provider(ctx: ReadonlyContext) -> str:
    """IC + a live snapshot of pipeline state, so every turn starts with an accurate Recap."""
    s = ctx.state
    snapshot = {
        "resume_loaded": bool(s.get(K.RESUME)),
        "job_loaded": bool(s.get(K.JD)),
        "job": s.get(K.JOB_META, {}),
        "fcot_iterations_done": {st: len(s.get(K.fcot(st), []))
                                 for st in ("evaluation", "tailoring", "cover_letter")},
        "decision": (s.get(K.EVAL) or {}).get("decision"),
        "score": (s.get(K.EVAL) or {}).get("confidence_score"),
        "documents": list((s.get(K.DOCS) or {}).keys()),
        "application_status": (s.get(K.APPLICATION) or {}).get("status"),
    }
    data = ""
    if s.get(K.RESUME):
        data += f"\n<resume untrusted='true'>\n{s[K.RESUME]}\n</resume>\n"
    if s.get(K.JD):
        data += f"\n<job_description untrusted='true'>\n{s[K.JD]}\n</job_description>\n"
    return f"{_STATIC_IC}\n## Current pipeline state\n{snapshot}\n{data}"


root_agent = Agent(
    name="job_application_agent",
    model=config.GEMINI_MODEL,
    description="Evaluates a resume against a job, scores fit (>=80% approve), tailors documents and applies.",
    instruction=instruction_provider,
    tools=[
        ingest_resume_text,
        load_resume_from_gcs,
        ingest_job_description_text,
        fetch_job_description,
        analyze_employment_dates,
        fcot_record_iteration,
        score_resume_match,
        save_document,
        FunctionTool(submit_application, require_confirmation=True),  # human-in-the-loop
        get_application_status,
    ],
    before_tool_callback=enforce_contract,
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
)
