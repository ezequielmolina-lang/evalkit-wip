"""Smoke test — verify the app is installed correctly without calling the API.

Run: `python -m lxp_eval.smoke_test`
"""
from __future__ import annotations

import os
import sys

from rich.console import Console

# Force UTF-8 stdout on Windows so we can render unicode badges
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

console = Console(legacy_windows=False, force_terminal=True)

OK = "[green]OK[/green]"
FAIL = "[red]FAIL[/red]"


def check(name: str, fn) -> bool:
    try:
        fn()
        console.print(f"  {OK} {name}")
        return True
    except Exception as exc:  # noqa: BLE001
        console.print(f"  {FAIL} {name}: {exc}")
        return False


def main() -> int:
    console.print("[bold]Smoke test for LXP Evaluation Workflow[/bold]\n")
    all_ok = True

    console.print("Imports:")
    all_ok &= check("rubric module", lambda: __import__("lxp_eval.rubric", fromlist=["DOMAINS", "TESTS"]))
    all_ok &= check("base_agent", lambda: __import__("lxp_eval.base_agent", fromlist=["BaseAgent"]))
    all_ok &= check("agents (8)", _check_agents_import)
    all_ok &= check("tools", _check_tools_import)
    all_ok &= check("orchestrator", lambda: __import__("lxp_eval.orchestrator", fromlist=["run_full_pipeline"]))

    console.print("\nRubric integrity:")
    all_ok &= check("13 domains, 100 pts total", _check_13_domains)
    all_ok &= check("All 85 tests defined", _check_tests)
    all_ok &= check("ESSA tier criteria", _check_essa)
    all_ok &= check("5 Potemkin patterns", _check_potemkin)

    console.print("\nConfig:")
    all_ok &= check("Config loads from env", _check_config)

    console.print()
    if all_ok:
        console.print("[bold green]All checks passed.[/bold green]")
        console.print("Next: copy .env.example to .env, set ANTHROPIC_API_KEY, then run examples/.")
        return 0
    else:
        console.print("[bold red]Some checks failed.[/bold red] See above.")
        return 1


def _check_agents_import():
    from lxp_eval.agents import (
        ContentAgent,
        EligibilityAgent,
        EvidenceAuditAgent,
        ImplementationAgent,
        PedagogyAgent,
        PersonalizationAgent,
        SynthesizerAgent,
        TeacherAgent,
    )
    assert EligibilityAgent and PedagogyAgent and EvidenceAuditAgent
    assert ContentAgent and TeacherAgent and PersonalizationAgent and ImplementationAgent
    assert SynthesizerAgent


def _check_tools_import():
    from lxp_eval.tools import populate_rubric_xlsx, write_comparison_report, write_html_dashboard, write_platform_report
    assert populate_rubric_xlsx and write_platform_report and write_comparison_report and write_html_dashboard


def _check_13_domains():
    from lxp_eval.rubric import DOMAINS
    assert len(DOMAINS) == 13, f"Expected 13 domains, got {len(DOMAINS)}"
    total = sum(d.points for d in DOMAINS.values())
    assert total == 100, f"Expected 100 pts total, got {total}"


def _check_tests():
    from lxp_eval.rubric import TESTS
    assert len(TESTS) == 85, f"Expected 85 tests total, got {len(TESTS)}"
    total_pts = sum(t.points for t in TESTS)
    assert abs(total_pts - 100.0) < 0.01, f"Expected 100 pts, got {total_pts}"


def _check_essa():
    from lxp_eval.rubric import ESSA_TIER_CRITERIA
    assert set(ESSA_TIER_CRITERIA.keys()) == {"Tier 1", "Tier 2", "Tier 3", "Tier 4"}


def _check_potemkin():
    from lxp_eval.rubric import POTEMKIN_PATTERNS
    assert len(POTEMKIN_PATTERNS) == 5


def _check_config():
    from lxp_eval.config import CONFIG
    # Just verify it loads — don't require API key for smoke test
    assert CONFIG.model_heavy and CONFIG.model_light
    assert CONFIG.max_tokens_per_call > 0


if __name__ == "__main__":
    sys.exit(main())
