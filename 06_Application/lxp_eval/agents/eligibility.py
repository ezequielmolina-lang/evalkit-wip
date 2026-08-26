"""Agent B — Eligibility Gate.

Applies the 7-question UNICEF + ESSA pre-filter and the 8 binary disqualifiers
to a candidate platform. This is the foundational gating step in the workflow.

Inputs: vendor documentation (privacy policy, ToS, theory of change, evidence
papers). Outputs: structured pre-filter and disqualifier results plus the ESSA
tier classification from QA7.

Cost: ~$0.50–$2 per platform with Opus 4.x.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig, format_documents_for_prompt
from ..rubric import DISQUALIFIERS, PRE_FILTER, ESSA_TIER_CRITERIA

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Eligibility Gate Agent in a multi-agent evaluation system \
for Learning Experience Platforms (LXPs). Your role is to apply the 7-question \
UNICEF + ESSA pre-filter and the 8 binary disqualifiers from the v7.1 evaluation \
framework to a candidate platform.

CRITICAL RULES:
1. You evaluate ONLY from the supplied documents and observation notes. If a question \
   cannot be answered from the supplied evidence, mark it PENDING and specify what data \
   is needed.
2. For QA3 (No sale of data), QA5 (Children's data not for marketing), and QA7 (ESSA \
   Tier), failure has procurement consequences — be conservative. If the vendor's \
   documentation is ambiguous, mark PENDING, not YES.
3. For QA7 ESSA Tier classification, apply VERBATIM criteria — a vendor declaring \
   Tier 1 must have a study with n ≥ 350 + ≥ 2 sites + setting AND samples overlap. \
   Without these specifics in evidence, the tier is downgraded to the highest \
   verifiable level (often Tier 4).
4. For disqualifiers, DQ1 (NO discovery for new material), DQ3 (Bundle structured \
   pedagogy not CAL standalone), DQ4 (Mastery gating), and DQ7 (Data ownership \
   assignable to MoE) are the procurement-critical items. Be thorough.
5. Cite specific page references or quotes where possible.

You will use the `record_prefilter` tool for QA1-QA7 and `record_disqualifier` \
for DQ1-DQ8. Call each tool exactly once per item. Use plain English in justifications.
"""


PRE_FILTER_TOOL = {
    "name": "record_prefilter",
    "description": "Record a determination for one of the 7 pre-filter items (QA1-QA7).",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "enum": ["QA1", "QA2", "QA3", "QA4", "QA5", "QA6", "QA7"],
            },
            "status": {
                "type": "string",
                "enum": ["YES", "NO", "PENDING", "Tier 1", "Tier 2", "Tier 3", "Tier 4"],
                "description": (
                    "For QA1-QA6 use YES/NO/PENDING. For QA7 use Tier 1/2/3/4 or PENDING."
                ),
            },
            "justification": {
                "type": "string",
                "description": "1-3 sentence justification with page references.",
            },
            "quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Direct quotes from vendor documents supporting the determination.",
            },
            "verification_details": {
                "type": "string",
                "description": (
                    "Only for QA7: ESSA verification details (n, sites, overlap, study design)."
                ),
            },
        },
        "required": ["id", "status", "justification"],
    },
}

DISQUALIFIER_TOOL = {
    "name": "record_disqualifier",
    "description": "Record PASS / FAIL / PENDING for one of the 8 disqualifiers (DQ1-DQ8).",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "enum": ["DQ1", "DQ2", "DQ3", "DQ4", "DQ5", "DQ6", "DQ7", "DQ8"],
            },
            "status": {
                "type": "string",
                "enum": ["PASS", "FAIL", "PENDING"],
            },
            "justification": {
                "type": "string",
                "description": "1-3 sentence justification with page references.",
            },
            "quotes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Direct quotes from vendor documents supporting the determination.",
            },
        },
        "required": ["id", "status", "justification"],
    },
}


class EligibilityAgent(BaseAgent):
    agent_name = "Agent B — Eligibility Gate"
    model = "light"  # Binary decisions, no deep statistical reasoning

    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        if not vendor_docs and not observation_notes:
            logger.warning("No documents for %s; all pre-filter / disqualifiers PENDING", platform.name)
            return self._all_pending(platform)

        all_docs = vendor_docs + observation_notes
        docs_block = format_documents_for_prompt(all_docs)

        user_prompt = self._build_user_prompt(platform, docs_block)

        response = self._call_claude(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            tools=[PRE_FILTER_TOOL, DISQUALIFIER_TOOL],
            max_tokens=8192,
        )

        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        result.transcripts.append({"system": SYSTEM_PROMPT[:200] + "...", "response": response})

        # Extract pre-filter calls
        pf_calls = self._extract_tool_calls(response, "record_prefilter")
        for call in pf_calls:
            result.pre_filter_results.append({
                "id": call.get("id"),
                "status": call.get("status"),
                "justification": call.get("justification", ""),
                "quotes": call.get("quotes", []),
                "verification_details": call.get("verification_details", ""),
            })

        # Extract disqualifier calls
        dq_calls = self._extract_tool_calls(response, "record_disqualifier")
        for call in dq_calls:
            result.disqualifier_results.append({
                "id": call.get("id"),
                "status": call.get("status"),
                "justification": call.get("justification", ""),
                "quotes": call.get("quotes", []),
            })

        # Identify missing items (Claude may not have called for all)
        seen_pf = {p["id"] for p in result.pre_filter_results}
        seen_dq = {d["id"] for d in result.disqualifier_results}
        for pf in PRE_FILTER:
            if pf.id not in seen_pf:
                result.pre_filter_results.append({
                    "id": pf.id,
                    "status": "PENDING",
                    "justification": "Not evaluated in this run — agent did not surface a determination.",
                    "quotes": [],
                })
                result.pending_questions.append(f"{pf.id} ({pf.name}) — re-run agent or supply more docs")
        for dq in DISQUALIFIERS:
            if dq.id not in seen_dq:
                result.disqualifier_results.append({
                    "id": dq.id,
                    "status": "PENDING",
                    "justification": "Not evaluated in this run — agent did not surface a determination.",
                    "quotes": [],
                })
                result.pending_questions.append(f"{dq.id} ({dq.name}) — re-run agent or supply more docs")

        result.notes = self._extract_text(response)
        return result

    def _build_user_prompt(self, platform: PlatformConfig, docs_block: str) -> str:
        pre_filter_summary = "\n".join(
            f"  {pf.id}. {pf.name} ({pf.type}) — source: {pf.source}"
            for pf in PRE_FILTER
        )
        dq_summary = "\n".join(
            f"  {dq.id}. {dq.name} — evidence: {dq.evidence}"
            for dq in DISQUALIFIERS
        )
        tier_summary = "\n".join(
            f"  {tier}: {info['name']} — REQUIRES: {'; '.join(info['criteria'])}"
            for tier, info in ESSA_TIER_CRITERIA.items()
        )
        return f"""# Eligibility evaluation for: {platform.name} ({platform.vendor})

## Platform context
- URL: {platform.url}
- Status: {platform.status}
- Schools deployed in el país: {platform.schools_sv}

## Items to evaluate

### Pre-filter (UNICEF QA + ESSA Tier classification)
{pre_filter_summary}

### Eight disqualifiers
{dq_summary}

### ESSA Tier verbatim criteria (for QA7)
{tier_summary}

## Vendor documents and observation notes (full text below)

{docs_block}

## Your task

For EACH of the 7 pre-filter items, call `record_prefilter` once with your determination.
For EACH of the 8 disqualifiers, call `record_disqualifier` once with PASS/FAIL/PENDING.
Be thorough — cite page references and direct quotes from vendor documents.
If evidence is insufficient, use PENDING and specify what data is needed.
"""

    def _all_pending(self, platform: PlatformConfig) -> AgentResult:
        """Return all-PENDING result when no documents supplied."""
        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        for pf in PRE_FILTER:
            result.pre_filter_results.append({
                "id": pf.id,
                "status": "PENDING",
                "justification": "No documents supplied for evaluation.",
                "quotes": [],
            })
        for dq in DISQUALIFIERS:
            result.disqualifier_results.append({
                "id": dq.id,
                "status": "PENDING",
                "justification": "No documents supplied for evaluation.",
                "quotes": [],
            })
        result.pending_questions.append(
            f"No vendor documents found in platforms/{platform.slug}/vendor_docs/. "
            f"Add vendor materials and re-run."
        )
        result.notes = "All items PENDING — no documents to evaluate."
        return result
