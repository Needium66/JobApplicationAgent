"""Deterministic evaluation tools.

The LLM judges each dimension with evidence; these functions do the arithmetic
and apply the decision policy, so the 80% threshold is enforced in code.
"""
from datetime import date

from google.adk.tools import ToolContext

from .. import config
from ..state import K

WEIGHTS = {
    "skills": 0.35,
    "experience": 0.25,
    "certifications": 0.10,
    "education": 0.10,
    "location_eligibility": 0.10,
    "recency_dates": 0.10,
}
NOT_APPLICABLE = -1


def _parse_ym(s: str, today: date) -> date | None:
    s = s.strip().lower()
    if s in ("present", "current", "now", "today"):
        return today.replace(day=1)
    for sep in ("-", "/"):
        parts = s.split(sep)
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            a, b = int(parts[0]), int(parts[1])
            y, m = (a, b) if a > 12 else (b, a)
            if 1 <= m <= 12:
                return date(y, m, 1)
    if s.isdigit() and len(s) == 4:
        return date(int(s), 1, 1)
    return None


def _months(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def compute_employment(roles: list[str], today: date | None = None) -> dict:
    today = today or date.today()
    parsed, errors = [], []
    for r in roles:
        parts = [p.strip() for p in r.split("|")]
        if len(parts) != 4:
            errors.append(f"Bad format: {r!r}")
            continue
        start, end = _parse_ym(parts[2], today), _parse_ym(parts[3], today)
        if not start or not end:
            errors.append(f"Unparseable dates: {r!r}")
            continue
        if end < start:
            errors.append(f"End before start: {r!r}")
            continue
        if start > today:
            errors.append(f"Start date in the future: {r!r}")
            continue
        parsed.append({"title": parts[0], "company": parts[1], "start": start, "end": end})

    parsed.sort(key=lambda x: x["start"])
    # Merge intervals so overlapping roles aren't double-counted.
    merged: list[list[date]] = []
    for p in parsed:
        if merged and p["start"] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], p["end"])
        else:
            merged.append([p["start"], p["end"]])
    total_months = sum(_months(s, e) + 1 for s, e in merged)
    gaps = [{"from": a[1].isoformat()[:7], "to": b[0].isoformat()[:7], "months": _months(a[1], b[0]) - 1}
            for a, b in zip(merged, merged[1:]) if _months(a[1], b[0]) - 1 >= 6]
    overlaps = [f"{x['title']} @ {x['company']} overlaps {y['title']} @ {y['company']}"
                for i, x in enumerate(parsed) for y in parsed[i + 1:] if y["start"] < x["end"]]
    last_end = max((p["end"] for p in parsed), default=None)
    return {
        "roles_parsed": len(parsed),
        "total_years_experience": round(total_months / 12, 1),
        "gaps_6mo_plus": gaps,
        "overlapping_roles": overlaps,
        "months_since_last_role": _months(last_end, today.replace(day=1)) if last_end else None,
        "errors": errors,
    }


def analyze_employment_dates(roles: list[str], tool_context: ToolContext) -> dict:
    """Computes total experience, employment gaps and overlaps from the resume's dated roles.

    Args:
        roles: One string per role, formatted "Title | Company | YYYY-MM | YYYY-MM or present".
    """
    result = compute_employment(roles)
    tool_context.state[K.DATES] = result
    return result


def compute_score(scores: dict[str, float], unmet_must_haves: list[str],
                  required_years: float = 0.0, actual_years: float | None = None) -> dict:
    applicable = {k: max(0.0, min(100.0, float(v))) for k, v in scores.items()
                  if k in WEIGHTS and float(v) != NOT_APPLICABLE}
    if "skills" not in applicable or "experience" not in applicable:
        raise ValueError("skills and experience scores are always required")
    wsum = sum(WEIGHTS[k] for k in applicable)
    raw = sum(WEIGHTS[k] * v for k, v in applicable.items()) / wsum

    unmet = list(unmet_must_haves)
    if required_years and actual_years is not None and actual_years < 0.8 * required_years:
        unmet.append(f"Years of experience: {actual_years} vs {required_years} required")

    final = raw
    if unmet:  # any unmet must-have caps the score below the approval line
        final = max(0.0, min(raw, config.APPROVE_THRESHOLD - 1) - 5 * (len(unmet) - 1))
    final = round(final, 1)
    return {
        "raw_weighted_score": round(raw, 1),
        "confidence_score": final,
        "decision": "APPROVE" if final >= config.APPROVE_THRESHOLD else "DENY",
        "threshold": config.APPROVE_THRESHOLD,
        "applied_weights": {k: round(WEIGHTS[k] / wsum, 3) for k in applicable},
        "unmet_must_haves": unmet,
    }


def score_resume_match(skills: float, experience: float, certifications: float, education: float,
                       location_eligibility: float, recency_dates: float,
                       unmet_must_haves: list[str], required_years_experience: float,
                       strengths: list[str], gaps: list[str], evidence_summary: str,
                       tool_context: ToolContext) -> dict:
    """Computes the final confidence score (0-100) and APPROVE/DENY decision.

    Call only after the "evaluation" FCoT loop is complete. Each dimension is 0-100;
    use -1 for certifications, education or location_eligibility when the job does not mention them.

    Args:
        skills: Match of required + preferred skills/tools.
        experience: Relevance, seniority and scope of experience.
        certifications: Required/preferred certifications held (-1 if none required).
        education: Degree/field match (-1 if not specified).
        location_eligibility: Location, onsite/remote, stated work-authorization fit (-1 if not specified).
        recency_dates: Recency of relevant experience and date consistency.
        unmet_must_haves: Explicit must-have requirements the resume does not show. Empty list if none.
        required_years_experience: Minimum years stated in the posting (0 if none).
        strengths: Top matching points with evidence.
        gaps: Missing or weak points.
        evidence_summary: Two to four sentences justifying the scores.
    """
    dates = tool_context.state.get(K.DATES) or {}
    result = compute_score(
        {"skills": skills, "experience": experience, "certifications": certifications,
         "education": education, "location_eligibility": location_eligibility,
         "recency_dates": recency_dates},
        unmet_must_haves, required_years_experience, dates.get("total_years_experience"),
    )
    result.update({"strengths": strengths, "gaps": gaps, "evidence_summary": evidence_summary,
                   "job": tool_context.state.get(K.JOB_META, {})})
    tool_context.state[K.EVAL] = result
    return result
