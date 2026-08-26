"""Agent C — Pedagogy.

Covers D1-D5 (50 pts, half the rubric weight):
- D1 Explicit Instruction Architecture (12 pts; 7 tests)
- D2 Cognitive Load Management (8 pts; 6 tests)
- D3 Retrieval / Spacing / Interleaving (12 pts; 7 tests)
- D4 Feedback + Mastery + BEA 2025 (10 pts; 8 tests)
- D5 Misconceptions + BEA-ML (8 pts; 6 tests)

This is the cognitive-science core of the framework. Most tests require direct
platform interaction (transcripts of student-tutor exchanges, screenshots of
practice screens, recordings of feedback). When such evidence is not available,
the agent marks tests as PENDING with a specific data request.

Cost: ~$3-5 per platform with Opus 4.x (largest agent by token count).
"""
from __future__ import annotations

import logging
from typing import Any

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig, format_documents_for_prompt
from ..rubric import DOMAINS, DomainResult, TestResult, get_tests_for_domains

logger = logging.getLogger(__name__)


DOMAINS_OWNED = ["D1", "D2", "D3", "D4", "D5"]


SYSTEM_PROMPT = """You are the Pedagogy Agent — Agent C — in a multi-agent evaluation \
system for Learning Experience Platforms (LXPs). You own 5 domains (D1-D5, 50 pts \
total) covering the cognitive-science core of the framework.

You evaluate evidence of HOW the platform teaches — not WHAT it teaches (that is \
the Content Agent), not WHO it teaches (that is the Teacher Integration Agent), and \
not its EVIDENCE OF IMPACT (that is the Evidence Audit Agent).

Your domains and weights:
- D1 (12 pts): Explicit Instruction Architecture — Rosenshine, Hattie d=0.59
- D2 (8 pts): Cognitive Load Management — Sweller, Cowan ~4 chunks
- D3 (12 pts): Retrieval / Spacing / Interleaving — Rohrer 63% vs 20%, Bjork
- D4 (10 pts): Feedback + Mastery + BEA 2025 (MI/AC/PG)
- D5 (8 pts): Misconceptions + BEA-ML

Per-test verification approach:
1. For each test, identify what evidence in the supplied documents bears on it. \
   Direct-instruction screenshots, tutor-student transcripts, vendor design docs, \
   pedagogy whitepapers, observation notes — any of these can serve.
2. If the evidence directly supports the criterion: PASS. If contradicts: FAIL. If \
   partial: PARTIAL with 0.5x credit. If not applicable: N/A. If insufficient \
   evidence: PENDING with specific data request.
3. CRITICAL: Do not award PASS based on vendor marketing claims alone. Award based \
   on evidence of how the product actually works.
4. Cite specific page references or quotes. When in doubt, prefer PENDING over PASS.

For each test (PT-D1-01 through PT-D5-06), call `score_test` exactly once. Use \
plain English in evidence/notes."""


class PedagogyAgent(BaseAgent):
    agent_name = "Agent C — Pedagogy"
    model = "heavy"  # Pedagogical reasoning benefits from Opus

    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        tests = get_tests_for_domains(DOMAINS_OWNED)

        all_docs = vendor_docs + observation_notes
        if not all_docs:
            logger.warning("No documents for %s — all D1-D5 tests PENDING", platform.name)
            return self._all_pending(platform, tests)

        docs_block = format_documents_for_prompt(all_docs)

        user_prompt = self._build_user_prompt(platform, docs_block, tests)

        response = self._call_claude(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            tools=[BaseAgent.tool_score_test()],
            max_tokens=16_000,  # Many tests; generous output budget
        )

        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        result.transcripts.append({"system": SYSTEM_PROMPT[:200] + "...", "response": response})

        score_calls = self._extract_tool_calls(response, "score_test")
        results_by_domain: dict[str, list[TestResult]] = {d: [] for d in DOMAINS_OWNED}
        seen_tests = set()

        for call in score_calls:
            test_id = call.get("test_id")
            seen_tests.add(test_id)
            test = next((t for t in tests if t.id == test_id), None)
            if not test:
                logger.warning("Unknown test_id from Pedagogy agent: %s", test_id)
                continue
            status = call.get("status", "PENDING")
            points = self._points_for_status(status, test.points)
            tr = TestResult(
                test_id=test_id,
                status=status,
                points_earned=points,
                evidence=call.get("evidence", ""),
                notes=call.get("notes", ""),
                sources=call.get("sources", []),
            )
            results_by_domain[test.domain].append(tr)

        # Fill missing
        for t in tests:
            if t.id not in seen_tests:
                results_by_domain[t.domain].append(TestResult(
                    test_id=t.id, status="PENDING", points_earned=0,
                    evidence="Not evaluated by agent.",
                ))
                result.pending_questions.append(f"{t.id} ({t.name}) — re-run or supply more docs")

        # Build domain results
        for domain_code in DOMAINS_OWNED:
            d_tests = results_by_domain[domain_code]
            result.domain_results.append(DomainResult(
                domain_code=domain_code,
                test_results=d_tests,
                total_points=sum(r.points_earned for r in d_tests),
            ))

        result.notes = self._extract_text(response)
        return result

    def _points_for_status(self, status: str, max_pts: float) -> float:
        return {
            "PASS": max_pts,
            "PARTIAL": max_pts * 0.5,
            "FAIL": 0,
            "N/A": 0,
            "PENDING": 0,
        }.get(status, 0)

    def _build_user_prompt(self, platform: PlatformConfig, docs_block: str, tests) -> str:
        # Group tests by domain for prompt clarity
        tests_by_dom: dict[str, list] = {d: [] for d in DOMAINS_OWNED}
        for t in tests:
            tests_by_dom[t.domain].append(t)

        domain_blocks = []
        for d_code in DOMAINS_OWNED:
            domain = DOMAINS[d_code]
            d_tests = tests_by_dom[d_code]
            test_lines = "\n".join(
                f"  - **{t.id}** ({t.points} pts, severity {t.severity}): {t.name}\n"
                f"    Evidence base: {t.evidence_base}\n"
                f"    Verify: {t.verification}\n"
                f"    PASS criteria: {t.pass_criteria} | FAIL criteria: {t.fail_criteria}"
                for t in d_tests
            )
            domain_blocks.append(
                f"### {domain.code} — {domain.name} ({domain.points} pts)\n"
                f"Evidence anchor: {domain.evidence_anchor}\n\n{test_lines}"
            )
        domains_text = "\n\n".join(domain_blocks)

        return f"""# Pedagogy evaluation for: {platform.name} ({platform.vendor})

## Platform context
- URL: {platform.url}
- Status: {platform.status}
- Schools deployed in el país: {platform.schools_sv}

## Domains and tests to evaluate

{domains_text}

## Vendor documents and observation notes (full text below)

{docs_block}

## Your task

For EACH of the {len(tests)} tests above, call `score_test` ONCE with:
- test_id (e.g., 'PT-D1-02')
- status (PASS / PARTIAL / FAIL / N/A / PENDING)
- evidence (1-3 sentence justification with source reference)
- notes (additional context, direct quotes, page references — optional)
- sources (list of source references — optional)

Be evidence-based: do NOT award PASS from marketing language alone. PENDING is \
correct when the supplied documents do not show how the platform handles the \
specific criterion. Make the verification visible in your evidence field.
"""

    def _all_pending(self, platform: PlatformConfig, tests) -> AgentResult:
        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        results_by_domain: dict[str, list[TestResult]] = {d: [] for d in DOMAINS_OWNED}
        for t in tests:
            results_by_domain[t.domain].append(TestResult(
                test_id=t.id, status="PENDING", points_earned=0,
                evidence="No documents supplied for evaluation.",
            ))
        for domain_code in DOMAINS_OWNED:
            d_tests = results_by_domain[domain_code]
            result.domain_results.append(DomainResult(
                domain_code=domain_code,
                test_results=d_tests,
                total_points=0,
            ))
        result.pending_questions.append(
            f"No documents found in platforms/{platform.slug}/. "
            f"Add vendor_docs/ or observation_notes/ and re-run."
        )
        return result
