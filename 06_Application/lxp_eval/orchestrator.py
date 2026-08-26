"""Orchestrator — runs the 8-agent pipeline for one or more platforms.

5-phase sequence:
  1. Setup    — load configs and documents
  2. Eligibility — Agent B (pre-filter + disqualifiers)
  3. Scoring  — Agents C, D, E, F, G, H in parallel
  4. Operational context — documented (currently no agent; see roadmap)
  5. Synthesis — narrative report + comparison + dashboard

Outputs go to outputs/<run-id>/.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from .agents import (
    ContentAgent,
    EligibilityAgent,
    EvidenceAuditAgent,
    ImplementationAgent,
    PedagogyAgent,
    PersonalizationAgent,
    SynthesizerAgent,
    TeacherAgent,
)
from .base_agent import AgentResult
from .config import CONFIG
from .platform_loader import PlatformConfig, load_documents, load_platform
from .tools import populate_rubric_xlsx, write_comparison_report, write_html_dashboard, write_platform_report

logger = logging.getLogger(__name__)
console = Console()


# Agent registry — concrete instances created once
def build_agents() -> dict[str, object]:
    return {
        "eligibility": EligibilityAgent(),
        "pedagogy": PedagogyAgent(),
        "content": ContentAgent(),
        "teacher": TeacherAgent(),
        "personalization": PersonalizationAgent(),
        "implementation": ImplementationAgent(),
        "evidence": EvidenceAuditAgent(),
    }


def evaluate_platform(
    platform: PlatformConfig,
    agents: dict[str, object],
    skip_agents: set[str] | None = None,
) -> list[AgentResult]:
    """Evaluate one platform with all (or selected) agents.

    Agents run sequentially for now to avoid rate-limit issues. Set
    LXP_MAX_PARALLEL_AGENTS > 1 to enable thread-pool parallelism (still uses
    independent API calls — Anthropic SDK is thread-safe).
    """
    skip_agents = skip_agents or set()
    console.print(f"[bold cyan]Evaluating {platform.name}[/bold cyan]")

    vendor_docs = load_documents(platform.vendor_docs)
    obs_notes = load_documents(platform.observation_notes)
    console.print(f"  Loaded {len(vendor_docs)} vendor doc(s) + {len(obs_notes)} observation note(s)")

    results: list[AgentResult] = []
    agent_order = ["eligibility", "pedagogy", "content", "teacher", "personalization", "implementation", "evidence"]
    active_agents = [(name, agents[name]) for name in agent_order if name not in skip_agents]

    if CONFIG.max_parallel_agents > 1:
        with ThreadPoolExecutor(max_workers=CONFIG.max_parallel_agents) as ex:
            futures = {
                ex.submit(_run_agent, name, agent, platform, vendor_docs, obs_notes): name
                for name, agent in active_agents
            }
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    result = fut.result()
                    results.append(result)
                    console.print(f"  ✓ {result.agent_name} done")
                except Exception as exc:  # noqa: BLE001
                    console.print(f"  ✗ {name} failed: {exc}", style="red")
                    logger.exception("Agent %s failed", name)
    else:
        for name, agent in active_agents:
            try:
                result = _run_agent(name, agent, platform, vendor_docs, obs_notes)
                results.append(result)
                console.print(f"  ✓ {result.agent_name} done")
            except Exception as exc:  # noqa: BLE001
                console.print(f"  ✗ {name} failed: {exc}", style="red")
                logger.exception("Agent %s failed", name)

    return results


def _run_agent(name, agent, platform, vendor_docs, obs_notes):
    start = time.time()
    result = agent.evaluate(platform, vendor_docs, obs_notes)
    elapsed = time.time() - start
    logger.info("%s for %s finished in %.1fs", name, platform.name, elapsed)
    return result


def run_full_pipeline(
    platform_names: list[str],
    skip_agents: set[str] | None = None,
    skip_synthesis: bool = False,
) -> Path:
    """Run the full pipeline for multiple platforms and write outputs.

    Returns the output directory path.
    """
    errors = CONFIG.validate()
    if errors:
        for e in errors:
            console.print(f"[red]Config error:[/red] {e}")
        raise SystemExit(1)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = CONFIG.outputs_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Run ID:[/bold] {run_id}")
    console.print(f"[bold]Output dir:[/bold] {output_dir}\n")

    agents = build_agents()

    # Phase 2-3: evaluate each platform
    all_results: dict[str, list[AgentResult]] = {}
    platforms: dict[str, PlatformConfig] = {}
    for name in platform_names:
        platform_dir = CONFIG.platforms_dir / name
        try:
            platform = load_platform(platform_dir)
        except FileNotFoundError as exc:
            console.print(f"[red]Skipping {name}: {exc}[/red]")
            continue
        platforms[name] = platform
        all_results[platform.name] = evaluate_platform(platform, agents, skip_agents)

    if not all_results:
        console.print("[red]No platforms evaluated.[/red]")
        return output_dir

    # Phase 5: synthesis
    synthesizer = SynthesizerAgent()
    for platform_name, agent_results in all_results.items():
        platform = next((p for p in platforms.values() if p.name == platform_name), None)
        if not platform:
            continue
        if not skip_synthesis:
            console.print(f"[yellow]Synthesizing {platform_name}...[/yellow]")
            narrative = synthesizer.synthesize_platform(platform, agent_results)
        else:
            narrative = "_Synthesis skipped (--no-synthesis)._"
        write_platform_report(platform, agent_results, narrative, output_dir)

    if len(all_results) > 1 and not skip_synthesis:
        console.print("[yellow]Synthesizing cross-platform comparison...[/yellow]")
        comp_narrative = synthesizer.synthesize_comparison(all_results)
        write_comparison_report(all_results, comp_narrative, output_dir)

    # Populate XLSX
    populate_rubric_xlsx(
        template_xlsx=CONFIG.rubric_xlsx,
        output_xlsx=output_dir / "Logging_Rubrica_LXP_ElPais_populated.xlsx",
        results_by_platform=all_results,
    )

    # HTML dashboard
    write_html_dashboard(all_results, output_dir / "dashboard")

    console.print(f"\n[bold green]✓ Done.[/bold green] Outputs in {output_dir}")
    return output_dir
