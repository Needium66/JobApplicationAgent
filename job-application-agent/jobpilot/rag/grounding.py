"""Grounding guards: deterministic checks that keep agent output tied to evidence.

  * profile grounding     skills/certifications the model extracted must literally appear in the resume
  * policy coverage       how many of the role policy's core skills the resume actually evidences
  * skills ceiling        the model's skills score can't exceed what the coverage supports
  * citation validation   evaluation claims must cite policy chunks that were really retrieved
  * document grounding    a cover letter / tailored resume can't claim policy skills or tools the resume lacks
"""
from __future__ import annotations

import re


def alternatives(term: str) -> list[str]:
    return [a.strip().lower() for a in term.split("|") if a.strip()]


def contains(term: str, text: str) -> bool:
    text = (text or "").lower()
    for alt in alternatives(term):
        if re.search(rf"(?<![a-z0-9]){re.escape(alt)}(?![a-z0-9])", text):
            return True
    return False


GENERIC = {"cloud", "security", "engineering", "systems", "management", "data", "design", "development",
           "testing", "platforms", "tools", "services", "software", "network", "operations"}


def ground_profile(profile: dict, resume: str) -> tuple[dict, list[str]]:
    """Drops extracted skills/certifications that aren't in the resume text (extraction hallucinations)."""
    clean, removed = dict(profile), []
    for key in ("skills", "certifications"):
        kept = []
        for item in profile.get(key, []) or []:
            pieces = [w.strip() for w in re.split(r"[/,()]", item) if len(w.strip()) >= 3 and w.strip().lower() not in GENERIC]
            if contains(item, resume) or (key == "skills" and any(contains(w, resume) for w in pieces)):
                kept.append(item)
            else:
                removed.append(f"{key}:{item}")
        clean[key] = kept
    return clean, removed


def policy_coverage(policy: dict, resume: str, profile: dict | None = None, posting_text: str = "") -> dict:
    """The job posting is the primary standard; the policy supplies vocabulary and synonyms.

    * relevant terms  = policy skills/tools/certifications the POSTING itself mentions -> they drive the cap
    * advisory terms  = policy core skills the posting does NOT mention -> shown as advice, never penalised
    """
    evidence = " ".join([resume or "", " ".join((profile or {}).get("skills", []))])
    pool = [*policy.get("core_skills", []), *policy.get("preferred_skills", []), *policy.get("tools", []),
            *policy.get("certifications", [])]
    seen, relevant = set(), []
    for t in pool:
        key = t.split("|")[0]
        if key not in seen and posting_text and contains(t, posting_text):
            seen.add(key)
            relevant.append(t)
    rel_hit = [t.split("|")[0] for t in relevant if contains(t, evidence)]
    rel_miss = [t.split("|")[0] for t in relevant if not contains(t, evidence)]
    core_hit = [t.split("|")[0] for t in policy.get("core_skills", []) if contains(t, evidence)]
    advisory = [t.split("|")[0] for t in policy.get("core_skills", [])
                if not contains(t, evidence) and t.split("|")[0] not in seen]
    return {"policy_id": policy.get("id"), "policy_version": policy.get("version"),
            "relevant_coverage": round(len(rel_hit) / len(relevant), 3) if relevant else None,
            "relevant_matched": rel_hit, "relevant_missing": rel_miss,
            "core_coverage": round(len(core_hit) / (len(policy.get("core_skills", [])) or 1), 3),
            "core_matched": core_hit, "advisory_missing": advisory}


def skills_ceiling(coverage: float | None, tolerance: int = 10) -> int:
    """Soft ceiling from the posting-relevant coverage: 60 with none of the posting's policy terms evidenced,
    100 with all of them (+ tolerance). No posting-relevant terms -> no ceiling."""
    if coverage is None:
        return 100
    return min(100, round(50 + 50 * coverage) + tolerance)


HARD_CAP_MARGIN = 15  # above ceiling + margin -> clamp; between ceiling and that -> warning only


def validate_citations(citations: list[str], allowed_ids: set[str]) -> tuple[list[str], list[str]]:
    valid = [c for c in citations if c in allowed_ids]
    invalid = [c for c in citations if c not in allowed_ids]
    return valid, invalid


def _concrete(alt: str, named: set[str]) -> bool:
    """A specific, checkable claim: an acronym (GCP), a token with digits/symbols (k8s, CI/CD, C#),
    or something the policy also lists as a tool/certification. Generic words ("Automation") are not."""
    a = alt.strip()
    return (a.lower() in named or (a.isupper() and len(a) >= 2)
            or any(ch.isdigit() for ch in a) or any(ch in "+#/." for ch in a))


def unsupported_claims(document: str, resume: str, policy: dict | None) -> list[str]:
    """Concrete policy skills/tools/certifications named in the document but absent from the resume."""
    if not policy:
        return []
    always = [*policy.get("tools", []), *policy.get("certifications", [])]
    skills = [*policy.get("core_skills", []), *policy.get("preferred_skills", [])]
    named = {a for t in always for a in alternatives(t)}
    bad = []
    for term in always + skills:
        if contains(term, resume):
            continue
        for alt in (a.strip() for a in term.split("|")):
            if len(alt) >= 2 and (term in always or _concrete(alt, named)) and contains(alt, document):
                bad.append(alt)
                break
    return sorted(set(bad))
