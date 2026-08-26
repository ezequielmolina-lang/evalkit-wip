"""Run the full 8-agent pipeline for one or more platforms.

Outputs: filled XLSX rubric + per-platform Markdown reports + cross-platform
comparison + HTML dashboard, all in outputs/<run-id>/.

Usage:
    python examples/run_full_evaluation.py --platforms plataforma_b plataforma_a plataforma_c
    python examples/run_full_evaluation.py --platforms plataforma_b --no-synthesis  # skip narrative
    python examples/run_full_evaluation.py --platforms plataforma_b --skip implementation evidence  # partial run

Cost estimate: ~$13 per platform with Opus 4.x; ~$40 for all three.
Set LXP_DRY_RUN=true in .env to skip API calls and print prompts only.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from lxp_eval.config import CONFIG
from lxp_eval.orchestrator import run_full_pipeline

console = Console()


def main():
    parser = argparse.ArgumentParser(description="Run the full LXP evaluation pipeline.")
    parser.add_argument("--platforms", nargs="+", required=True,
                        help="Platform directory names (e.g., plataforma_b plataforma_a plataforma_c)")
    parser.add_argument("--skip", nargs="*", default=[],
                        choices=["eligibility", "pedagogy", "content", "teacher",
                                 "personalization", "implementation", "evidence"],
                        help="Agents to skip")
    parser.add_argument("--no-synthesis", action="store_true",
                        help="Skip Claude-generated narrative reports (cheaper, faster)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(levelname)s [%(name)s] %(message)s")

    if CONFIG.dry_run:
        console.print("[bold yellow]DRY RUN MODE[/bold yellow] — no API calls will be made.\n")

    console.print(f"[bold]Platforms:[/bold] {', '.join(args.platforms)}")
    console.print(f"[bold]Skipping agents:[/bold] {', '.join(args.skip) if args.skip else '(none)'}")
    console.print(f"[bold]Model (heavy):[/bold] {CONFIG.model_heavy}")
    console.print(f"[bold]Model (light):[/bold] {CONFIG.model_light}\n")

    output_dir = run_full_pipeline(
        platform_names=args.platforms,
        skip_agents=set(args.skip),
        skip_synthesis=args.no_synthesis,
    )

    console.print(f"\n[bold]Files written:[/bold]")
    for p in sorted(output_dir.rglob("*")):
        if p.is_file():
            console.print(f"  · {p.relative_to(output_dir)}")

    console.print(f"\n[bold]To view the dashboard:[/bold]")
    console.print(f"  open {output_dir}/dashboard/comparison.html")

    return 0


if __name__ == "__main__":
    sys.exit(main())
