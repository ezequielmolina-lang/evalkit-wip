"""CLI entrypoint to run a single browser-test cell.

Usage examples (from the `06_Application/` directory):

    # Dry-run a BT-2 probe (no API calls, no submission)
    python -m lxp_eval.browser_agents.run_browser_cell \\
        --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2

    # Real run with browser visible, no answer submission
    python -m lxp_eval.browser_agents.run_browser_cell \\
        --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2 \\
        --cost-cap 5

    # Real run that ALSO submits a wrong answer and probes the AI tutor
    python -m lxp_eval.browser_agents.run_browser_cell \\
        --platform plataforma_b --subject math --grade 3 --lesson 1.4 --agent BT-2 \\
        --allow-submit --open-tutor

Output: an observation note + screenshots in
`platforms/<platform>/observation_notes/BT2_<cell>_<timestamp>/`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from ..config import CONFIG
from .pedagogy_probe import CellSpec, run_pedagogy_probe


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_browser_cell",
        description="Run a single LXP browser test cell (BT-2 Pedagogy Probe PoC).",
    )
    p.add_argument("--platform", required=True, choices=["plataforma_b", "plataforma_a"])
    p.add_argument("--subject", required=True, choices=["math", "language"])
    p.add_argument("--grade", required=True, type=int)
    p.add_argument(
        "--lesson",
        required=True,
        help="Lesson code (e.g. CNEB '1.4' for PLATAFORMA_B, OVA code for Plataforma A).",
    )
    p.add_argument(
        "--agent",
        default="BT-2",
        choices=["BT-2"],
        help="Browser-test agent to run (only BT-2 supported in this PoC).",
    )
    p.add_argument(
        "--allow-submit",
        action="store_true",
        help="Allow the agent to submit ONE deliberate wrong answer.",
    )
    p.add_argument(
        "--open-tutor",
        action="store_true",
        help="After lesson observation, open the AI tutor and run prompt T-1.",
    )
    p.add_argument(
        "--headless",
        action="store_true",
        help="Run Chromium headless. Default: headful (visible browser).",
    )
    p.add_argument(
        "--cost-cap",
        type=float,
        default=5.0,
        help="Abort if estimated API cost exceeds this USD value (default 5).",
    )
    p.add_argument(
        "--max-iterations",
        type=int,
        default=25,
        help="Max Claude turns before aborting (default 25).",
    )
    p.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Override platforms/ root for output notes (default: ./platforms/).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.agent != "BT-2":
        print(f"Only BT-2 is implemented in this PoC; got {args.agent!r}", file=sys.stderr)
        return 2

    cell = CellSpec(
        platform=args.platform,
        subject=args.subject,
        grade=args.grade,
        lesson=args.lesson,
        allow_submit=args.allow_submit,
        open_tutor=args.open_tutor,
    )

    print(f"\n=== Running {args.agent} on {cell.platform} {cell.subject} g{cell.grade} L{cell.lesson} ===")
    print(f"    dry_run={CONFIG.dry_run}, headless={args.headless}, allow_submit={cell.allow_submit}, "
          f"open_tutor={cell.open_tutor}, cost_cap=${args.cost_cap}\n")

    try:
        result = asyncio.run(run_pedagogy_probe(
            cell,
            headless=args.headless,
            cost_cap_usd=args.cost_cap,
            max_iterations=args.max_iterations,
            output_root=args.output_root,
        ))
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        logging.exception("Cell failed with unhandled exception")
        print(f"\nCELL FAILED: {e}", file=sys.stderr)
        return 1

    print("\n=== Result ===")
    print(json.dumps(result, indent=2, default=str))
    print("\nObservation note:", result.get("observation_note"))
    print(f"Estimated cost: ${result.get('estimated_cost_usd', 0):.4f}")

    return 0 if result.get("status") == "COMPLETE" else 3


if __name__ == "__main__":
    raise SystemExit(main())
