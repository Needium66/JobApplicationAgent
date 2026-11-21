"""Tools shared by the specialist agents, and the guard that enforces contracts in code."""
from __future__ import annotations

from datetime import datetime, timezone

from google.adk.tools import ToolContext

from ..core.runtime import get_runtime
from .contracts import FCOT_N


def fcot_record_iteration(stage: str, iteration: int, recap: str, reason: str,
                          verify_checks: list[str], verified: bool, tool_context: ToolContext) -> dict:
    """Records one Recap/Reason/Verify iteration of the FCoT Recursive Loop (persisted as a checkpoint).

    Args:
        stage: The stage name you are running (see stages_to_run in STATE_JSON).
        iteration: 1-based iteration number (1..3), strictly in order. Continue from checkpointed progress.
        recap: What you know, this stage's goal, and what the last Verify flagged.
        reason: The work done / changes made in this iteration.
        verify_checks: Checks against success criteria and hard constraints, each prefixed PASS: or FAIL:.
        verified: True only if every check passed.
    """
    st = tool_context.state
    if stage not in st.get("_stages", []):
        return {"status": "error", "message": f"Stage must be one of {st.get('_stages')}."}
    key = f"fcot_{stage}"
    history = list(st.get(key, []))
    if iteration == 1 and history and len(history) < FCOT_N:
        return {"status": "error", "message": f"Checkpoint has {len(history)} iterations; continue at {len(history) + 1}."}
    if iteration == 1:
        history = []
    expected = len(history) + 1
    if iteration != expected or iteration > FCOT_N:
        return {"status": "error", "message": f"Out of order. Next iteration for '{stage}' is {min(expected, FCOT_N)}."}
    if not recap.strip() or not reason.strip() or not verify_checks:
        return {"status": "error", "message": "recap, reason and verify_checks are all required."}
    failed = [c for c in verify_checks if c.strip().upper().startswith("FAIL")]
    verified = bool(verified) and not failed
    history.append({"iteration": iteration, "recap": recap[:1500], "reason": reason[:1500],
                    "verify_checks": [c[:300] for c in verify_checks[:20]], "verified": verified,
                    "at": datetime.now(timezone.utc).isoformat()})
    st[key] = history

    # Incremental checkpoint: persist this iteration immediately.
    rt = get_runtime()
    cp = rt.active.get((st["_case_id"], st["_agent"]))
    if cp is not None:
        cp.save_fcot(stage, history)
    rt.fault(f"{st['_agent']}:fcot:{stage}:{iteration}")

    done = iteration == FCOT_N
    nxt = ("Loop complete; produce the deliverable." if done and verified else
           "Iteration 3 did not verify: do NOT produce the deliverable; explain what is missing." if done else
           f"Proceed to iteration {iteration + 1}.")
    return {"status": "ok", "stage": stage, "iteration": iteration, "verified": verified,
            "failed_checks": failed, "stage_complete": done and verified, "next": nxt}


def loop_complete(state, stage: str) -> bool:
    h = state.get(f"fcot_{stage}", [])
    return len(h) == FCOT_N and bool(h[-1].get("verified"))


def make_guard(deliverable_stage: dict[str, str | callable]):
    """before_tool_callback: deliverable tools are blocked until their stage's loop is complete."""

    def guard(tool, args, tool_context):
        stage = deliverable_stage.get(tool.name)
        if callable(stage):
            stage = stage(args)
        if stage and not loop_complete(tool_context.state, stage):
            return {"status": "blocked_by_policy",
                    "message": f"Complete the '{stage}' FCoT loop (3 verified iterations) first."}
        return None

    return guard


def audit_tool_call(tool, args, tool_context, tool_response):
    """after_tool_callback: every tool call becomes a node in the causal graph."""
    st = tool_context.state
    rt = get_runtime()
    status = "ok"
    if isinstance(tool_response, dict) and tool_response.get("status") in ("error", "blocked_by_policy"):
        status = "blocked" if tool_response.get("status") == "blocked_by_policy" else "warn"
    label = tool.name
    if tool.name == "fcot_record_iteration":
        label = f"FCoT {args.get('stage')} #{args.get('iteration')} verified={args.get('verified')}"
    rt.graph.node(st["_case_id"], "tool", label, st["_agent"], [st.get("_parent_node", "")], status=status)
    return None
