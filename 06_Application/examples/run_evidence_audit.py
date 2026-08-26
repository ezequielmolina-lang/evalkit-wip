"""Run just the Evidence Audit (Agent H) — verifies vendor evidence claims.

Most distinctive agent: applies verbatim ESSA criteria and 5-pattern Potemkin
detection.

Usage:
    python examples/run_evidence_audit.py --platform plataforma_b
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from lxp_eval.agents import EvidenceAuditAgent
from lxp_eval.config import CONFIG
from lxp_eval.platform_loader import load_documents, load_platform

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Run Agent H (Evidence Audit) on a single platform.")
    parser.add_argument("--platform", required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(levelname)s [%(name)s] %(message)s")

    errors = CONFIG.validate()
    if errors:
        for e in errors:
            console.print(f"[red]Config error:[/red] {e}")
        return 1

    platform_dir = CONFIG.platforms_dir / args.platform
    platform = load_platform(platform_dir)
    vendor_docs = load_documents(platform.vendor_docs)
    obs_notes = load_documents(platform.observation_notes)

    console.print(f"[bold]Evidence Audit:[/bold] {platform.name}")
    console.print(f"  {len(vendor_docs)} vendor doc(s), {len(obs_notes)} observation note(s)")

    agent = EvidenceAuditAgent()
    result = agent.evaluate(platform, vendor_docs, obs_notes)

    if result.domain_results:
        d12 = result.domain_results[0]
        console.print(f"\n[bold]D12 Evidence Rigor:[/bold] {d12.total_points:.2f} / 6 pts")
        for tr in d12.test_results:
            status_color = "green" if tr.status == "PASS" else "yellow" if tr.status == "PARTIAL" else "red" if tr.status == "FAIL" else "white"
            console.print(f"  [{status_color}]{tr.status:8}[/{status_color}] {tr.test_id}  ({tr.points_earned:.2f} pts) — {tr.evidence[:80]}")

    if result.potemkin_flags:
        console.print(f"\n[bold red]Potemkin claims flagged ({len(result.potemkin_flags)}):[/bold red]")
        for flag in result.potemkin_flags:
            console.print(f"  [{flag.get('pattern_id')}] {flag.get('pattern_name')}")
            console.print(f"      Vendor claim: {flag.get('vendor_claim', '')[:120]}")
            console.print(f"      Actual support: {flag.get('actual_evidence_level', '')}")
            console.print(f"      Why: {flag.get('explanation', '')[:160]}")

    if result.notes:
        console.print(f"\n[bold]Paper audit summary:[/bold]")
        console.print(result.notes[:2000])

    return 0


if __name__ == "__main__":
    sys.exit(main())
