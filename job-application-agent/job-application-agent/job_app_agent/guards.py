"""Hard constraints enforced in code (not just in the prompt).

before_tool_callback runs before every tool call. Returning a dict skips the tool
and hands that dict back to the model as the tool result.
"""
from typing import Any

from .state import K
from .tools.fcot import loop_complete


def _block(msg: str) -> dict:
    return {"status": "blocked_by_policy", "message": msg}


def enforce_contract(tool, args: dict[str, Any], tool_context) -> dict | None:
    s = tool_context.state
    name = tool.name

    if name in ("fcot_record_iteration", "score_resume_match", "analyze_employment_dates"):
        if not s.get(K.RESUME) or not s.get(K.JD):
            return _block("Resume and job description must both be ingested first.")

    if name == "score_resume_match" and not loop_complete(s, "evaluation"):
        return _block("Complete the 'evaluation' FCoT loop (3 verified iterations) before scoring.")

    if name == "save_document":
        if not s.get(K.EVAL):
            return _block("Run the evaluation and scoring before generating documents.")
        stage = "tailoring" if args.get("kind") == "tailored_resume" else "cover_letter"
        if not loop_complete(s, stage):
            return _block(f"Complete the '{stage}' FCoT loop (3 verified iterations) before saving.")

    if name == "submit_application":
        ev = s.get(K.EVAL) or {}
        if ev.get("decision") != "APPROVE":
            return _block(f"Decision is {ev.get('decision', 'missing')} "
                          f"(score {ev.get('confidence_score')}). Applying is not allowed below 80%.")
        if "cover_letter" not in (s.get(K.DOCS) or {}):
            return _block("Generate and save the cover letter before applying.")
        prior = s.get(K.APPLICATION) or {}
        if prior and prior.get("job") == s.get(K.JOB_META):
            return _block("An application for this job was already recorded in this session.")
    return None
