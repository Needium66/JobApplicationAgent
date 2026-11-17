"""FCoT Element 2 — the Recursive Loop, enforced as a state machine.

Level 1 is a single monolithic agent, so the loop is not a separate LoopAgent:
the one LlmAgent performs Recap -> Reason -> Verify itself and must record every
iteration here. The tool enforces order and N, and the guard callback blocks
deliverable tools until the stage's loop is complete.
"""
from datetime import datetime, timezone

from google.adk.tools import ToolContext

from .. import config
from ..contract import IC
from ..state import K


def fcot_record_iteration(stage: str, iteration: int, recap: str, reason: str,
                          verify_checks: list[str], verified: bool,
                          tool_context: ToolContext) -> dict:
    """Records one Recap/Reason/Verify iteration of the FCoT Recursive Loop.

    Args:
        stage: One of "evaluation", "tailoring", "cover_letter".
        iteration: 1-based iteration number (1..3), strictly in order.
        recap: What you know, the goal for this stage, and what the last Verify flagged.
        reason: The work done / changes made in this iteration.
        verify_checks: Checks against success criteria and hard constraints, each prefixed PASS: or FAIL:.
        verified: True only if every check passed.
    """
    if stage not in IC.loop_stages:
        return {"status": "error", "message": f"Unknown stage. Use one of {list(IC.loop_stages)}."}
    key = K.fcot(stage)
    history = list(tool_context.state.get(key, []))

    # A fresh run of a stage (e.g. new job description) restarts at iteration 1.
    if iteration == 1:
        history = []
    expected = len(history) + 1
    if iteration != expected or iteration > config.FCOT_N:
        return {"status": "error",
                "message": f"Out of order. Next iteration for '{stage}' is {min(expected, config.FCOT_N)} "
                           f"(N={config.FCOT_N}). Start again at 1 to restart the stage."}
    if not recap.strip() or not reason.strip() or not verify_checks:
        return {"status": "error", "message": "recap, reason and verify_checks are all required."}

    failed = [c for c in verify_checks if c.strip().upper().startswith("FAIL")]
    if verified and failed:
        verified = False  # self-consistency: can't be verified with failing checks

    history.append({
        "iteration": iteration, "recap": recap, "reason": reason,
        "verify_checks": verify_checks, "verified": verified,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    tool_context.state[key] = history

    done = iteration == config.FCOT_N
    if done and verified:
        nxt = "Loop complete. You may now produce this stage's deliverable."
    elif done:
        nxt = ("Iteration 3 did not verify. Do NOT produce the deliverable. Tell the user what could not be "
               "verified and ask for the missing information, then restart this stage at iteration 1.")
    else:
        nxt = f"Proceed to iteration {iteration + 1}: Recap what failed or is weak, Reason to fix it, Verify again."
    return {"status": "ok", "stage": stage, "iteration": iteration, "verified": verified,
            "failed_checks": failed, "stage_complete": done and verified, "next": nxt}


def loop_complete(state, stage: str) -> bool:
    history = state.get(K.fcot(stage), [])
    return len(history) == config.FCOT_N and bool(history[-1].get("verified"))
