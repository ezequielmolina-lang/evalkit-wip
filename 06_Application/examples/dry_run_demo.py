"""Dry-run demo — show what the orchestrator does without spending API tokens.

Sets LXP_DRY_RUN=true via environment and runs the pipeline.
Prompts are printed; no Claude calls are made.

Usage:
    python examples/dry_run_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Force dry run before importing CONFIG
os.environ["LXP_DRY_RUN"] = "true"

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console

from lxp_eval.config import CONFIG  # noqa: E402 — must come after env mutation
from lxp_eval.orchestrator import run_full_pipeline  # noqa: E402

console = Console()


def main():
    console.print("[bold yellow]DRY RUN DEMO[/bold yellow]")
    console.print("This runs the pipeline against PLATAFORMA_B without calling the Anthropic API.")
    console.print("You will see the prompts the agents would have sent.\n")

    # Force dry-run config
    CONFIG.dry_run = True

    # Set up a minimal PLATAFORMA_B config if not present
    plataforma_b_dir = CONFIG.platforms_dir / "plataforma_b"
    if not (plataforma_b_dir / "config.yaml").exists():
        console.print(f"[red]Need a config at {plataforma_b_dir / 'config.yaml'}. Run smoke_test first.[/red]")
        return 1

    output_dir = run_full_pipeline(["plataforma_b"], skip_synthesis=True)
    console.print(f"\n[bold]Dry run complete.[/bold] Outputs in {output_dir} (stubs only).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
