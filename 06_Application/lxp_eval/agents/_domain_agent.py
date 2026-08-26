"""DomainAgent — shared implementation for any agent that scores a fixed set
of domains using the standard `score_test` tool.

The 4 previously-stub agents (Content, Teacher, Personalization, Implementation)
inherit from this class and just declare DOMAINS_OWNED + SYSTEM_PROMPT.

Why this exists: Pedagogy/Content/Teacher/Personalization/Implementation all
follow the same scoring shape — receive docs, evaluate N tests across M domains,
emit score_test calls, aggregate. Only the system prompt + which domains differ.

Evidence Audit (Agent H) is structurally different (verbatim ESSA matrix +
Potemkin detection) and is NOT built on this class.
"""
from __future__ import annotations

import logging
from typing import Any, ClassVar

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig, format_documents_for_prompt
from ..rubric import DOMAINS, DomainResult, TestResult, get_tests_for_domains

logger = logging.getLogger(__name__)


class DomainAgent(BaseAgent):
    """Base for any agent that scores rubric tests across a fixed domain set."""

    DOMAINS_OWNED: ClassVar[list[str]] = []
    SYSTEM_PROMPT: ClassVar[str] = ""
    max_tokens: int = 16_000

    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        if not self.DOMAINS_OWNED or not self.SYSTEM_PROMPT:
            raise RuntimeError(
                f"{self.__class__.__name__} must set DOMAINS_OWNED and SYSTEM_PROMPT"
            )

        tests = get_tests_for_domains(self.DOMAINS_OWNED)
        if not tests:
            logger.warning(
                "%s: no tests defined in rubric.py for domains %s — returning empty result",
                self.agent_name, self.DOMAINS_OWNED,
            )
            return self._empty(platform)

        all_docs = vendor_docs + observation_notes
        if not all_docs:
            logger.warning("%s: no documents for %s — all PENDING", self.agent_name, platform.name)
            return self._all_pending(platform, tests)

        docs_block = format_documents_for_prompt(all_docs)
        user_prompt = self._build_user_prompt(platform, docs_block, tests)

        response = self._call_claude(
            system=self.SYSTEM_PROMPT,
            user=user_prompt,
            tools=[BaseAgent.tool_score_test()],
            max_tokens=self.max_tokens,
        )

        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        result.transcripts.append({
            "system": self.SYSTEM_PROMPT[:200] + "...",
            "response": response,
        })

        score_calls = self._extract_tool_calls(response, "score_test")
        results_by_domain: dict[str, list[TestResult]] = {d: [] for d in self.DOMAINS_OWNED}
        seen_tests = set()

        for call in score_calls:
            test_id = call.get("test_id")
            seen_tests.add(test_id)
            test = next((t for t in tests if t.id == test_id), None)
            if not test:
                logger.warning("Unknown test_id from %s: %s", self.agent_name, test_id)
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

        # Fill PENDING for tests the agent did not surface
        for t in tests:
            if t.id not in seen_tests:
                results_by_domain[t.domain].append(TestResult(
                    test_id=t.id, status="PENDING", points_earned=0,
                    evidence="Not evaluated by agent.",
                ))
                result.pending_questions.append(f"{t.id} ({t.name}) — re-run or supply more docs")

        for domain_code in self.DOMAINS_OWNED:
            d_tests = results_by_domain[domain_code]
            result.domain_results.append(DomainResult(
                domain_code=domain_code,
                test_results=d_tests,
                total_points=sum(r.points_earned for r in d_tests),
            ))

        result.notes = self._extract_text(response)
        return result

    def _build_user_prompt(self, platform: PlatformConfig, docs_block: str, tests) -> str:
        tests_by_dom: dict[str, list] = {d: [] for d in self.DOMAINS_OWNED}
        for t in tests:
            tests_by_dom[t.domain].append(t)

        domain_blocks = []
        for d_code in self.DOMAINS_OWNED:
            domain = DOMAINS[d_code]
            d_tests = tests_by_dom[d_code]
            test_lines = "\n".join(
                f"  - **{t.id}** ({t.points} pts, severity {t.severity}): {t.name}\n"
                f"    Evidence base: {t.evidence_base}\n"
                f"    Verify: {t.verification}\n"
                f"    PASS: {t.pass_criteria} | FAIL: {t.fail_criteria}"
                for t in d_tests
            )
            domain_blocks.append(
                f"### {domain.code} — {domain.name} ({domain.points} pts)\n"
                f"Evidence anchor: {domain.evidence_anchor}\n\n{test_lines}"
            )
        domains_text = "\n\n".join(domain_blocks)

        return f"""# {self.agent_name} evaluation for: {platform.name} ({platform.vendor})

## Platform context
- URL: {platform.url}
- Status: {platform.status}
- Schools in el país: {platform.schools_sv}

## Domains and tests

{domains_text}

## Vendor documents and observation notes (full text below)

{docs_block}

## Your task

For EACH of the {len(tests)} tests above, call `score_test` exactly once with:
- test_id (e.g., 'PT-D6-01')
- status (PASS / PARTIAL / FAIL / N/A / PENDING)
- evidence (1-3 sentence justification with source reference)
- notes (additional context, direct quotes — optional)
- sources (list of source references — optional)

Be evidence-based: do NOT award PASS from marketing language alone. PENDING is \
correct when the supplied documents do not show how the platform handles the \
specific criterion.
"""

    def _points_for_status(self, status: str, max_pts: float) -> float:
        return {
            "PASS": max_pts,
            "PARTIAL": max_pts * 0.5,
            "FAIL": 0,
            "N/A": 0,
            "PENDING": 0,
        }.get(status, 0)

    def _all_pending(self, platform: PlatformConfig, tests) -> AgentResult:
        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        results_by_domain: dict[str, list[TestResult]] = {d: [] for d in self.DOMAINS_OWNED}
        for t in tests:
            results_by_domain[t.domain].append(TestResult(
                test_id=t.id, status="PENDING", points_earned=0,
                evidence="No documents supplied for evaluation.",
            ))
        for domain_code in self.DOMAINS_OWNED:
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

    def _empty(self, platform: PlatformConfig) -> AgentResult:
        result = AgentResult(agent_name=self.agent_name, platform=platform.name)
        result.notes = "No tests defined for owned domains in rubric.py."
        return result
