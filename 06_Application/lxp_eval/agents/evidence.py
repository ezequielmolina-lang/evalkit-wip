"""Agent H — Evidence Audit.

The framework's most distinctive agent. Reads vendor-supplied evidence papers
and applies VERBATIM ESSA Tier criteria — not the vendor's self-declaration.
Runs the 5-pattern Potemkin detection to flag misrepresented evidence claims.

Inputs: any vendor-supplied PDFs of evidence papers, RCT reports, white papers.
Outputs: per-paper Tier classification + verification details + Potemkin flags +
D12 score (out of 6 pts).

Cost: ~$2-5 per platform with Opus 4.x (heavier reasoning required).
"""
from __future__ import annotations

import logging
from typing import Any

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig, format_documents_for_prompt
from ..rubric import (
    DOMAINS,
    ESSA_TIER_CRITERIA,
    POTEMKIN_PATTERNS,
    DomainResult,
    TestResult,
    get_tests_for_domain,
)

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Evidence Audit Agent — Agent H — in a multi-agent evaluation \
system for Learning Experience Platforms. Your role is to audit the rigor of evidence \
that a vendor presents for their platform's impact on learning.

CORE PRINCIPLE: You apply VERBATIM ESSA Tier criteria. You do NOT accept the vendor's \
self-declaration of tier. You verify each tier claim against the underlying study.

ESSA Tier criteria (verbatim, US Department of Education 2015):
- Tier 1 (Strong Evidence): Meets WWC Without Reservations + n ≥ 350 students + \
  ≥ 2 educational sites + setting AND samples overlap with deployment context + \
  statistically significant positive effect.
- Tier 2 (Moderate Evidence): Meets WWC With or Without Reservations + n ≥ 350 + \
  ≥ 2 sites + setting OR samples overlap + statistically significant positive effect.
- Tier 3 (Promising Evidence): ≥1 well-designed correlational study with statistical \
  controls for selection bias + statistically significant positive effect.
- Tier 4 (Demonstrates a Rationale): Comprehensive logic model + research-based \
  components + ongoing evaluation effort. No direct causal evidence required.

CRITICAL: If a vendor declares Tier 1 or Tier 2 but the underlying study does not \
meet the verbatim n, sites, or overlap criteria, downgrade to the highest verifiable \
tier and FLAG with the appropriate Potemkin pattern.

The 5 Potemkin patterns to detect:
- P1: Pilot results without control group declared as Tier 2 Moderate.
- P2: Sample size below 350 or single-site declared as Tier 1 Strong.
- P3: 'Evidence-based' claims without peer-reviewed paper.
- P4: Significant gains reported without confidence intervals or without overlap.
- P5: Vendor-funded white papers presented as third-party validation.

Workflow per paper:
1. Identify what the vendor claims (tier? effect size? n? sites?).
2. Read the paper to verify each criterion.
3. Use `audit_paper` to record findings.
4. If the vendor's declared tier exceeds what the evidence supports, use \
   `flag_potemkin`.
5. After auditing all papers, use `score_test` for each D12 test (PT-D12-01 through \
   PT-D12-08).

Be conservative: better to mark PENDING than to award unsupported tier."""


AUDIT_PAPER_TOOL = {
    "name": "audit_paper",
    "description": (
        "Record the verification of a single evidence paper. Call once per paper "
        "supplied by the vendor."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "paper_name": {
                "type": "string",
                "description": "Title or filename of the paper.",
            },
            "vendor_declared_tier": {
                "type": "string",
                "description": "What tier the vendor claims (or 'not declared').",
            },
            "verified_tier": {
                "type": "string",
                "enum": ["Tier 1", "Tier 2", "Tier 3", "Tier 4", "Cannot verify", "Not impact evidence"],
                "description": "What tier the evidence actually supports per verbatim criteria.",
            },
            "study_design": {
                "type": "string",
                "description": "RCT / Quasi-experimental (DiD/RDD/IV/matching) / Correlational / Pilot / Other.",
            },
            "sample_size_n": {
                "type": "integer",
                "description": "Total n of students. -1 if not reported.",
            },
            "num_sites": {
                "type": "integer",
                "description": "Number of educational sites. -1 if not reported.",
            },
            "overlap_with_deployment": {
                "type": "string",
                "enum": ["setting AND samples", "setting OR samples (one of two)", "neither", "unknown"],
                "description": "Whether study setting and/or samples overlap with the SV deployment context.",
            },
            "effect_size_reported": {
                "type": "string",
                "description": "Effect size with CIs if reported; 'not reported' otherwise.",
            },
            "independence": {
                "type": "string",
                "enum": ["third-party", "vendor-funded", "mixed", "unknown"],
                "description": "Is the study third-party or vendor-funded?",
            },
            "verification_notes": {
                "type": "string",
                "description": "Detailed verification notes, including page references.",
            },
        },
        "required": ["paper_name", "verified_tier", "study_design", "verification_notes"],
    },
}


class EvidenceAuditAgent(BaseAgent):
    agent_name = "Agent H — Evidence Audit"
    model = "heavy"  # Statistical reasoning benefits from Opus

    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        d12 = DOMAINS["D12"]
        d12_tests = get_tests_for_domain("D12")

        # Filter for evidence-looking documents
        evidence_docs = self._filter_evidence_docs(vendor_docs)

        if not evidence_docs:
            logger.warning("No evidence documents for %s — D12 PENDING", platform.name)
            return self._all_pending(platform, d12_tests)

        docs_block = format_documents_for_prompt(evidence_docs)

        user_prompt = self._build_user_prompt(platform, docs_block, d12_tests)

        response = self._call_claude(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            tools=[AUDIT_PAPER_TOOL, BaseAgent.tool_score_test(), BaseAgent.tool_flag_potemkin()],
            max_tokens=10_000,
        )

        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        result.transcripts.append({"system": SYSTEM_PROMPT[:200] + "...", "response": response})

        # Extract paper audits — store on the domain result for the report
        paper_audits = self._extract_tool_calls(response, "audit_paper")

        # Extract Potemkin flags
        potemkin_calls = self._extract_tool_calls(response, "flag_potemkin")
        for call in potemkin_calls:
            result.potemkin_flags.append({
                "agent": self.agent_name,
                "pattern_id": call.get("pattern_id"),
                "pattern_name": next(
                    (p["name"] for p in POTEMKIN_PATTERNS if p["id"] == call.get("pattern_id")),
                    "unknown",
                ),
                "vendor_claim": call.get("vendor_claim", ""),
                "actual_evidence_level": call.get("actual_evidence_level", ""),
                "explanation": call.get("explanation", ""),
            })

        # Extract D12 test scores
        score_calls = self._extract_tool_calls(response, "score_test")
        test_results = []
        seen_tests = set()
        for call in score_calls:
            test_id = call.get("test_id")
            seen_tests.add(test_id)
            test = next((t for t in d12_tests if t.id == test_id), None)
            if not test:
                logger.warning("Unknown test_id from D12 agent: %s", test_id)
                continue
            status = call.get("status", "PENDING")
            points = self._points_for_status(status, test.points)
            test_results.append(TestResult(
                test_id=test_id,
                status=status,
                points_earned=points,
                evidence=call.get("evidence", ""),
                notes=call.get("notes", ""),
                sources=call.get("sources", []),
            ))

        # Fill in PENDING for any tests not scored
        for test in d12_tests:
            if test.id not in seen_tests:
                test_results.append(TestResult(
                    test_id=test.id,
                    status="PENDING",
                    points_earned=0,
                    evidence="Not evaluated by agent.",
                    sources=[],
                ))
                result.pending_questions.append(f"{test.id} ({test.name}) — re-run or supply more evidence")

        domain_result = DomainResult(
            domain_code="D12",
            test_results=test_results,
            total_points=sum(r.points_earned for r in test_results),
            notes=f"Audited {len(paper_audits)} paper(s); flagged {len(potemkin_calls)} Potemkin claim(s).",
        )
        result.domain_results.append(domain_result)
        result.notes = self._format_paper_audits(paper_audits)
        return result

    def _filter_evidence_docs(self, docs: list[LoadedDocument]) -> list[LoadedDocument]:
        """Heuristic filter for which docs are evidence papers."""
        keep = []
        for d in docs:
            name = d.name.lower()
            content_snippet = d.content[:2000].lower()
            evidence_hints = (
                "evidence" in name or "rct" in name or "trial" in name or
                "evaluation" in name or "impact" in name or "study" in name or
                "white paper" in name or "research" in name or "paper" in name or
                # Content-based hints
                "randomized" in content_snippet or "quasi-experimental" in content_snippet or
                "effect size" in content_snippet or "sample size" in content_snippet
            )
            if evidence_hints:
                keep.append(d)
        # If nothing matched, fall back to all PDFs (vendor may not name them obviously)
        if not keep:
            keep = [d for d in docs if d.kind == "pdf"]
        return keep

    def _points_for_status(self, status: str, max_pts: float) -> float:
        return {
            "PASS": max_pts,
            "PARTIAL": max_pts * 0.5,
            "FAIL": 0,
            "N/A": 0,
            "PENDING": 0,
        }.get(status, 0)

    def _build_user_prompt(self, platform: PlatformConfig, docs_block: str, tests) -> str:
        test_list = "\n".join(
            f"  {t.id}. {t.name} ({t.points} pts) — verify: {t.verification}"
            for t in tests
        )
        potemkin_list = "\n".join(
            f"  {p['id']}: {p['name']} — {p['description']}"
            for p in POTEMKIN_PATTERNS
        )
        return f"""# Evidence audit for: {platform.name} ({platform.vendor})

## Platform context
- URL: {platform.url}
- Status: {platform.status}
- Schools deployed in el país: {platform.schools_sv}

## D12 tests to evaluate
{test_list}

## Potemkin patterns to check
{potemkin_list}

## Vendor evidence documents (full text below)

{docs_block}

## Your task

1. For EACH paper above, call `audit_paper` with the full verification (n, sites, \
   overlap, study design, independence, verified tier).
2. For ANY vendor claim that exceeds what the evidence supports, call `flag_potemkin` \
   with the matching pattern (P1-P5).
3. For EACH of the {len(tests)} D12 tests, call `score_test` with status (PASS / \
   PARTIAL / FAIL / N/A / PENDING) and evidence.

Be conservative: PENDING is better than awarding unsupported tier.
"""

    def _format_paper_audits(self, audits: list[dict]) -> str:
        if not audits:
            return "No paper audits recorded."
        lines = ["## Paper audits\n"]
        for a in audits:
            lines.append(
                f"### {a.get('paper_name')}\n"
                f"- Vendor declared: {a.get('vendor_declared_tier', 'n/a')}\n"
                f"- **Verified**: {a.get('verified_tier')}\n"
                f"- Design: {a.get('study_design')}\n"
                f"- n: {a.get('sample_size_n', 'n/a')}, sites: {a.get('num_sites', 'n/a')}\n"
                f"- Overlap: {a.get('overlap_with_deployment')}\n"
                f"- Effect size: {a.get('effect_size_reported', 'n/a')}\n"
                f"- Independence: {a.get('independence', 'unknown')}\n"
                f"- Notes: {a.get('verification_notes', '')}\n"
            )
        return "\n".join(lines)

    def _all_pending(self, platform: PlatformConfig, tests) -> AgentResult:
        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        test_results = [
            TestResult(test_id=t.id, status="PENDING", points_earned=0,
                       evidence="No evidence documents supplied to audit.")
            for t in tests
        ]
        result.domain_results.append(DomainResult(
            domain_code="D12",
            test_results=test_results,
            total_points=0,
            notes="All D12 tests PENDING — no evidence documents found.",
        ))
        result.pending_questions.append(
            f"No evidence papers found in platforms/{platform.slug}/vendor_docs/. "
            f"Request impact studies / RCT reports / white papers from the vendor."
        )
        return result
