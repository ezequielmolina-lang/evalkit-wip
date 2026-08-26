"""Run just the Eligibility Gate (Agent B) against one platform.

Useful as a quick sanity check before running the full pipeline.

Usage:
    python examples/run_eligibility.py --platform plataforma_b
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add parent to path so we can import lxp_eval/ when run as a script
sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from lxp_eval.agents import EligibilityAgent
from lxp_eval.config import CONFIG
from lxp_eval.platform_loader import load_documents, load_platform

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Run Agent B (Eligibility) on a single platform.")
    parser.add_argument("--platform", required=True, help="Platform directory name (e.g., 'plataforma_b')")
    parser.add_argument("--log-level", default="INFO", help="Logging level")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(levelname)s [%(name)s] %(message)s")

    errors = CONFIG.validate()
    if errors:
        for e in errors:
            console.print(f"[red]Config error:[/red] {e}")
        return 1

    platform_dir = CONFIG.platforms_dir / args.platform
    if not platform_dir.exists():
        console.print(f"[red]Platform dir not found:[/red] {platform_dir}")
        return 1

    platform = load_platform(platform_dir)
    vendor_docs = load_documents(platform.vendor_docs)
    obs_notes = load_documents(platform.observation_notes)

    console.print(f"[bold]Eligibility Gate evaluation:[/bold] {platform.name}")
    console.print(f"  {len(vendor_docs)} vendor doc(s), {len(obs_notes)} observation note(s)")

    agent = EligibilityAgent()
    result = agent.evaluate(platform, vendor_docs, obs_notes)

    console.print(f"\n[bold]Pre-filter results:[/bold]")
    for pf in result.pre_filter_results:
        status_color = "green" if pf.get("status") == "YES" else "red" if pf.get("status") == "NO" else "yellow"
        console.print(f"  [{status_color}]{pf.get('status'):8}[/{status_color}] {pf.get('id')}  — {pf.get('justification', '')[:80]}")

    console.print(f"\n[bold]Disqualifier results:[/bold]")
    for dq in result.disqualifier_results:
        status_color = "green" if dq.get("status") == "PASS" else "red" if dq.get("status") == "FAIL" else "yellow"
        console.print(f"  [{status_color}]{dq.get('status'):8}[/{status_color}] {dq.get('id')}  — {dq.get('justification', '')[:80]}")

    if result.pending_questions:
        console.print(f"\n[bold yellow]Pending questions:[/bold yellow]")
        for q in result.pending_questions[:10]:
            console.print(f"  · {q}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
