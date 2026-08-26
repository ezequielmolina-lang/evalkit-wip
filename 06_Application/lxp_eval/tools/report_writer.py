"""Markdown report writer — per-platform + cross-platform comparison."""
from __future__ import annotations

import logging
from pathlib import Path

from ..base_agent import AgentResult
from ..platform_loader import PlatformConfig
from ..rubric import DOMAINS

logger = logging.getLogger(__name__)


def write_platform_report(
    platform: PlatformConfig,
    agent_results: list[AgentResult],
    narrative: str,
    output_dir: Path,
) -> Path:
    """Write Report_<platform>.md with structured summary + narrative."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"Report_{platform.slug}.md"

    # Aggregate
    total_points = 0.0
    per_domain = {}
    all_potemkin = []
    all_pending = []
    all_pf = []
    all_dq = []

    for ar in agent_results:
        for dr in ar.domain_results:
            per_domain[dr.domain_code] = dr
            total_points += dr.total_points
        all_potemkin.extend(ar.potemkin_flags)
        all_pending.extend(ar.pending_questions)
        all_pf.extend(ar.pre_filter_results)
        all_dq.extend(ar.disqualifier_results)

    lines = []
    lines.append(f"# Evaluation Report: {platform.name}")
    lines.append(f"\n**Vendor:** {platform.vendor}")
    lines.append(f"**URL:** {platform.url}")
    lines.append(f"**Status:** {platform.status}")
    lines.append(f"**Schools in SV:** {platform.schools_sv}")
    lines.append(f"\n**Total score:** {total_points:.2f} / 100")
    lines.append(f"**Potemkin claims flagged:** {len(all_potemkin)}")
    lines.append(f"**PENDING tests:** {len(all_pending)}\n")

    # Pre-filter
    lines.append("## Pre-filter results\n")
    lines.append("| ID | Status | Justification |")
    lines.append("|---|---|---|")
    for pf in all_pf:
        just = (pf.get("justification") or "").replace("\n", " ").replace("|", "\\|")[:120]
        lines.append(f"| {pf.get('id')} | **{pf.get('status')}** | {just} |")

    # Disqualifiers
    lines.append("\n## Disqualifier results\n")
    lines.append("| ID | Status | Justification |")
    lines.append("|---|---|---|")
    for dq in all_dq:
        just = (dq.get("justification") or "").replace("\n", " ").replace("|", "\\|")[:120]
        lines.append(f"| {dq.get('id')} | **{dq.get('status')}** | {just} |")

    # Domain scores
    lines.append("\n## Domain scores\n")
    lines.append("| Domain | Score | Max | Notes |")
    lines.append("|---|---|---|---|")
    for code in sorted(per_domain.keys(), key=lambda c: int(c[1:])):
        dr = per_domain[code]
        max_pts = DOMAINS[code].points if code in DOMAINS else 0
        notes = (dr.notes or "").replace("\n", " ")[:120]
        lines.append(f"| {code} {DOMAINS[code].name if code in DOMAINS else ''} | {dr.total_points:.2f} | {max_pts} | {notes} |")

    # Potemkin
    if all_potemkin:
        lines.append("\n## Potemkin claims flagged\n")
        for p in all_potemkin:
            lines.append(f"### [{p.get('pattern_id')}] {p.get('pattern_name')}")
            lines.append(f"- **Vendor claim:** {p.get('vendor_claim', '')}")
            lines.append(f"- **Actual evidence:** {p.get('actual_evidence_level', '')}")
            lines.append(f"- **Explanation:** {p.get('explanation', '')}\n")

    # PENDING
    if all_pending:
        lines.append("\n## PENDING items\n")
        for q in all_pending:
            lines.append(f"- {q}")

    # Narrative
    lines.append("\n## Narrative synthesis\n")
    lines.append(narrative or "_No narrative produced._")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote platform report: %s", output_path)
    return output_path


def write_comparison_report(
    all_platform_results: dict[str, list[AgentResult]],
    narrative: str,
    output_dir: Path,
) -> Path:
    """Cross-platform comparison report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "Comparison_Report.md"

    lines = ["# Cross-platform Comparison Report\n"]
    lines.append(f"Comparing {len(all_platform_results)} platforms.\n")

    # Aggregate per platform
    summary_rows = []
    for platform_name, ar_list in all_platform_results.items():
        total = sum(dr.total_points for ar in ar_list for dr in ar.domain_results)
        potemkin = sum(len(ar.potemkin_flags) for ar in ar_list)
        pending = sum(len(ar.pending_questions) for ar in ar_list)
        dq_fails = sum(
            1 for ar in ar_list for dq in ar.disqualifier_results
            if dq.get("status") == "FAIL"
        )
        summary_rows.append((platform_name, total, potemkin, pending, dq_fails))

    lines.append("## Headline scores\n")
    lines.append("| Platform | Score | Potemkin | PENDING | DQ Fails |")
    lines.append("|---|---|---|---|---|")
    for name, total, p, pend, dq in summary_rows:
        lines.append(f"| {name} | {total:.2f}/100 | {p} | {pend} | {dq} |")

    # Domain-by-domain table
    lines.append("\n## Domain-by-domain scores\n")
    header = "| Domain | Max | " + " | ".join(p for p, *_ in summary_rows) + " |"
    sep = "|---|---|" + "|".join("---" for _ in summary_rows) + "|"
    lines.append(header)
    lines.append(sep)
    for d_code in sorted(DOMAINS.keys(), key=lambda c: int(c[1:])):
        domain = DOMAINS[d_code]
        row = [f"{d_code} {domain.name[:40]}", f"{domain.points}"]
        for platform_name, ar_list in all_platform_results.items():
            d_score = 0.0
            found = False
            for ar in ar_list:
                for dr in ar.domain_results:
                    if dr.domain_code == d_code:
                        d_score = dr.total_points
                        found = True
            row.append(f"{d_score:.2f}" if found else "—")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("\n## Narrative comparison\n")
    lines.append(narrative or "_No comparison narrative produced._")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Wrote comparison report: %s", output_path)
    return output_path
