"""Synthesizer — consolidates agent results into a narrative report.

Not an evaluator agent; produces the final cross-agent synthesis for a single
platform and (optionally) cross-platform comparison.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig
from ..rubric import DOMAINS

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are the Synthesizer in a multi-agent LXP evaluation system. \
You receive results from 7 evaluator agents (A through H, skipping the Synthesizer \
itself) for a single platform, and you produce a 1500-2500 word narrative report.

Your report must:
1. State the platform name, vendor, and overall score (out of 100).
2. Summarize the pre-filter and disqualifier results — if any disqualifier FAILed, \
   that is the headline.
3. Walk through each of the 13 domains with the score and 2-3 sentence narrative \
   of what evidence supports it.
4. Surface any Potemkin claims flagged by Agent H — these are critical for the \
   report's credibility.
5. List PENDING tests grouped by domain — with the specific data request needed.
6. End with a 'For the la organización team' section that summarizes the procurement \
   implications honestly.

CRITICAL: Do not declare a 'winner.' The framework's stated position is that the \
output is a trade-off analysis, not a recommendation. Phrase findings as \
'Platform X has these features, Y has these others, the trade-offs are...'

Use clear, evidence-grounded English. Cite specific test IDs (e.g., PT-D1-02) \
when referring to scores. Be honest about limitations — what is PENDING and why."""


class SynthesizerAgent(BaseAgent):
    agent_name = "Synthesizer"
    model = "heavy"

    def synthesize_platform(
        self,
        platform: PlatformConfig,
        agent_results: list[AgentResult],
    ) -> str:
        """Produce per-platform narrative report."""
        if not agent_results:
            return f"# {platform.name}\n\nNo agent results to synthesize.\n"

        summary = self._compute_summary(platform, agent_results)
        user_prompt = self._build_user_prompt(platform, agent_results, summary)

        response = self._call_claude(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            tools=None,
            max_tokens=8_000,
        )
        return self._extract_text(response)

    def _compute_summary(self, platform: PlatformConfig, agent_results: list[AgentResult]) -> dict:
        all_domain_results = []
        all_pf = []
        all_dq = []
        all_potemkin = []
        all_pending = []
        for ar in agent_results:
            all_domain_results.extend(ar.domain_results)
            all_pf.extend(ar.pre_filter_results)
            all_dq.extend(ar.disqualifier_results)
            all_potemkin.extend(ar.potemkin_flags)
            all_pending.extend(ar.pending_questions)
        total_points = sum(dr.total_points for dr in all_domain_results)
        return {
            "platform": platform.name,
            "total_points": total_points,
            "domain_results": all_domain_results,
            "pre_filter": all_pf,
            "disqualifiers": all_dq,
            "potemkin_flags": all_potemkin,
            "pending": all_pending,
            "any_disqualifier_failed": any(dq.get("status") == "FAIL" for dq in all_dq),
        }

    def _build_user_prompt(self, platform: PlatformConfig, agent_results: list[AgentResult], summary: dict) -> str:
        # Format the structured data for Claude
        domain_lines = []
        for dr in summary["domain_results"]:
            domain = DOMAINS.get(dr.domain_code)
            if not domain:
                continue
            line = f"- {dr.domain_code} {domain.name}: {dr.total_points:.2f}/{domain.points} pts"
            failed = [r for r in dr.test_results if r.status == "FAIL"]
            pending = [r for r in dr.test_results if r.status == "PENDING"]
            if failed:
                line += f" — {len(failed)} FAIL"
            if pending:
                line += f" — {len(pending)} PENDING"
            domain_lines.append(line)
        domains_block = "\n".join(domain_lines)

        pf_block = "\n".join(
            f"  {pf.get('id')}: {pf.get('status')} — {pf.get('justification', '')[:120]}"
            for pf in summary["pre_filter"]
        ) or "(no pre-filter results)"

        dq_block = "\n".join(
            f"  {dq.get('id')}: {dq.get('status')} — {dq.get('justification', '')[:120]}"
            for dq in summary["disqualifiers"]
        ) or "(no disqualifier results)"

        potemkin_block = "\n".join(
            f"  [{p.get('pattern_id')}] {p.get('vendor_claim', '')[:80]} — "
            f"actual: {p.get('actual_evidence_level', '')}"
            for p in summary["potemkin_flags"]
        ) or "(no Potemkin claims flagged)"

        pending_count = len(summary["pending"])

        return f"""# Synthesis for: {platform.name} ({platform.vendor})

## Aggregate scoring
Total score: {summary['total_points']:.2f} / 100
Any disqualifier failed: {summary['any_disqualifier_failed']}

## Pre-filter results
{pf_block}

## Disqualifier results
{dq_block}

## Domain scores
{domains_block}

## Potemkin claims flagged
{potemkin_block}

## Pending questions
{pending_count} test(s) marked PENDING (full list provided to user separately).

## Your task

Write a 1500-2500 word narrative report following the system prompt. Headline with \
the score. Walk through each domain. Surface Potemkin flags prominently. End with \
the 'For the la organización team' section."""

    def synthesize_comparison(self, all_platform_results: dict[str, list[AgentResult]]) -> str:
        """Produce cross-platform comparison report."""
        if not all_platform_results:
            return "# Cross-platform comparison\n\nNo platform results to compare.\n"

        # Build comparison summary
        comp_summary = self._build_comparison_summary(all_platform_results)
        user_prompt = f"""# Cross-platform comparison

You are producing a comparison report across {len(all_platform_results)} platforms. \
The framework's position is that we do NOT declare a winner — we surface trade-offs.

## Per-platform aggregate
{comp_summary}

## Your task

Write a 2000-3000 word cross-platform comparison report. Structure:
1. Summary table of scores by domain across all platforms.
2. Domain-by-domain comparison (each domain: which platform leads, by how much, why).
3. Cross-cutting observations (e.g., common Potemkin patterns, common PENDING items).
4. Trade-offs for the procurement decision-maker — phrased as 'Platform X is stronger on Y, \
   Platform Z is stronger on W; the trade-off depends on...'.
5. Recommendations for follow-up evaluation (which PENDING items to prioritize)."""

        response = self._call_claude(
            system="You are the cross-platform Synthesizer. Produce comparison reports without declaring winners.",
            user=user_prompt,
            tools=None,
            max_tokens=10_000,
        )
        return self._extract_text(response)

    def _build_comparison_summary(self, all_results: dict[str, list[AgentResult]]) -> str:
        lines = []
        for platform_name, agent_results in all_results.items():
            domain_results = []
            potemkin = 0
            for ar in agent_results:
                domain_results.extend(ar.domain_results)
                potemkin += len(ar.potemkin_flags)
            total = sum(dr.total_points for dr in domain_results)
            lines.append(f"### {platform_name}")
            lines.append(f"  Total: {total:.2f}/100 · {potemkin} Potemkin flag(s)")
            for dr in domain_results:
                domain = DOMAINS.get(dr.domain_code)
                if domain:
                    lines.append(f"    {dr.domain_code}: {dr.total_points:.2f}/{domain.points}")
            lines.append("")
        return "\n".join(lines)

    # BaseAgent abstract method (we don't use it for synthesizer)
    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        raise NotImplementedError("Use synthesize_platform() / synthesize_comparison() instead.")
