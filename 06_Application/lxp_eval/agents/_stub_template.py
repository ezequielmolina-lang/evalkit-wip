"""Template for stubbed agents. Copy this and adjust domain list + system prompt.

Each stub follows the same pattern as `pedagogy.py` but is parametrized by domain.
This file is NOT imported directly — it serves as the template.

To implement a stub agent:
1. Set `agent_name` and `DOMAINS_OWNED`.
2. Write `SYSTEM_PROMPT` (see pedagogy.py and evidence.py for examples).
3. The rest of the class is identical to pedagogy.py.
"""
from __future__ import annotations

import logging
from typing import Any

from ..base_agent import AgentResult, BaseAgent
from ..platform_loader import LoadedDocument, PlatformConfig, format_documents_for_prompt
from ..rubric import DOMAINS, DomainResult, TestResult, get_tests_for_domains

logger = logging.getLogger(__name__)


def make_pending_result(
    agent_name: str,
    platform: PlatformConfig,
    domains_owned: list[str],
    note: str,
) -> AgentResult:
    """Helper to return all-PENDING result for a stub agent."""
    result = AgentResult(agent_name=agent_name, platform=platform.name)
    tests = get_tests_for_domains(domains_owned)
    results_by_domain: dict[str, list[TestResult]] = {d: [] for d in domains_owned}
    for t in tests:
        results_by_domain[t.domain].append(TestResult(
            test_id=t.id, status="PENDING", points_earned=0,
            evidence=note,
        ))
    for domain_code in domains_owned:
        d_tests = results_by_domain[domain_code]
        result.domain_results.append(DomainResult(
            domain_code=domain_code,
            test_results=d_tests,
            total_points=0,
            notes=note,
        ))
    result.pending_questions.append(
        f"{agent_name} is a stub — copy lxp_eval/agents/pedagogy.py and adapt for {domains_owned}."
    )
    return result
