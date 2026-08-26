"""Populate the v7.1 rubric XLSX with agent results.

Takes the canonical rubric (Logging_Rubrica_LXP_ElPais.xlsx) as template,
fills the Logging_<PLATFORM> sheets with status + points + evidence + notes.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from openpyxl import load_workbook

from ..base_agent import AgentResult

logger = logging.getLogger(__name__)


def populate_rubric_xlsx(
    template_xlsx: Path,
    output_xlsx: Path,
    results_by_platform: dict[str, list[AgentResult]],
) -> Path:
    """Copy the template and fill in agent results.

    Args:
        template_xlsx: Source XLSX (from 00_Master_Documents/Logging_Rubrica_LXP_ElPais.xlsx).
        output_xlsx: Destination XLSX path (will be created/overwritten).
        results_by_platform: Mapping of platform name → list of AgentResult.

    Returns:
        Path to the populated XLSX.
    """
    if not template_xlsx.exists():
        raise FileNotFoundError(f"Template XLSX not found: {template_xlsx}")

    # Copy template
    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(template_xlsx, output_xlsx)

    wb = load_workbook(output_xlsx)

    # Determine sheet names — XLSX has Logging_PLATAFORMA_B, Logging_Plataforma A, Logging_Plataforma C by convention
    for platform_name, agent_results in results_by_platform.items():
        sheet_name = _platform_to_sheet_name(platform_name, wb.sheetnames)
        if not sheet_name:
            logger.warning("No Logging sheet for platform %s — adding nothing.", platform_name)
            continue
        _populate_logging_sheet(wb[sheet_name], agent_results)

        # Update pre-filter sheet
        if "Pre_filter_UNICEF_ESSA" in wb.sheetnames:
            _populate_prefilter(wb["Pre_filter_UNICEF_ESSA"], platform_name, agent_results)
        # Update disqualifiers sheet
        if "Disqualifiers" in wb.sheetnames:
            _populate_disqualifiers(wb["Disqualifiers"], platform_name, agent_results)
        # Update Pending_Questions sheet
        if "Pending_Questions" in wb.sheetnames:
            _populate_pending(wb["Pending_Questions"], platform_name, agent_results)

    wb.save(output_xlsx)
    logger.info("Wrote populated XLSX to %s", output_xlsx)
    return output_xlsx


def _platform_to_sheet_name(platform_name: str, sheet_names: list[str]) -> str | None:
    candidates = [
        f"Logging_{platform_name}",
        f"Logging_{platform_name.upper()}",
        f"Logging_{platform_name.lower()}",
        f"Logging_{platform_name.capitalize()}",
    ]
    for c in candidates:
        if c in sheet_names:
            return c
    # Fuzzy: any sheet starting with Logging_ whose suffix contains the platform name
    for s in sheet_names:
        if s.startswith("Logging_") and platform_name.lower() in s.lower():
            return s
    return None


def _populate_logging_sheet(ws, agent_results: list[AgentResult]) -> None:
    """Fill Status (col G) and points (col H) + Notas (col M) for each test."""
    # Build a lookup: test_id → (status, points, evidence_text)
    test_lookup = {}
    for ar in agent_results:
        for dr in ar.domain_results:
            for tr in dr.test_results:
                # Combine evidence + sources into the notes field
                evidence_text = tr.evidence
                if tr.sources:
                    evidence_text += " [Sources: " + "; ".join(tr.sources) + "]"
                if tr.notes:
                    evidence_text += f" Notes: {tr.notes}"
                test_lookup[tr.test_id] = (tr.status, tr.points_earned, evidence_text, ar.agent_name)

    # Iterate rows: Test ID is in col A (index 1)
    for row in ws.iter_rows(min_row=2, values_only=False):
        test_id_cell = row[0]
        if not test_id_cell.value:
            continue
        if test_id_cell.value in test_lookup:
            status, points, evidence_text, agent_name = test_lookup[test_id_cell.value]
            # Status: col G (index 6)
            if len(row) > 6:
                row[6].value = status
            # Tester: col I (index 8)
            if len(row) > 8:
                row[8].value = agent_name
            # Notas: col M (index 12)
            if len(row) > 12:
                row[12].value = evidence_text[:500]  # Cap length


def _populate_prefilter(ws, platform_name: str, agent_results: list[AgentResult]) -> None:
    """Fill platform column with pre-filter results."""
    # Find the platform column (E=PLATAFORMA_B, F=Plataforma A, G=Plataforma C by convention)
    col_idx = _find_platform_column(ws, platform_name, header_row=4)
    if col_idx is None:
        return

    # Build lookup
    pf_lookup = {}
    for ar in agent_results:
        for pf in ar.pre_filter_results:
            pf_lookup[pf["id"]] = pf

    # Iterate rows starting at row 5
    for row_idx in range(5, ws.max_row + 1):
        qa_id = ws.cell(row=row_idx, column=1).value
        if qa_id and qa_id in pf_lookup:
            ws.cell(row=row_idx, column=col_idx).value = pf_lookup[qa_id]["status"]


def _populate_disqualifiers(ws, platform_name: str, agent_results: list[AgentResult]) -> None:
    col_idx = _find_platform_column(ws, platform_name, header_row=3)
    if col_idx is None:
        return
    dq_lookup = {}
    for ar in agent_results:
        for dq in ar.disqualifier_results:
            dq_lookup[dq["id"]] = dq
    for row_idx in range(4, ws.max_row + 1):
        dq_id = ws.cell(row=row_idx, column=1).value
        if dq_id and dq_id in dq_lookup:
            ws.cell(row=row_idx, column=col_idx).value = dq_lookup[dq_id]["status"]


def _populate_pending(ws, platform_name: str, agent_results: list[AgentResult]) -> None:
    """Append pending questions for this platform."""
    next_row = ws.max_row + 1
    if ws.cell(row=1, column=1).value is None:
        # First time — add header
        ws.cell(row=1, column=1).value = "Platform"
        ws.cell(row=1, column=2).value = "Pending question"
        next_row = 2
    for ar in agent_results:
        for q in ar.pending_questions:
            ws.cell(row=next_row, column=1).value = platform_name
            ws.cell(row=next_row, column=2).value = q
            next_row += 1


def _find_platform_column(ws, platform_name: str, header_row: int) -> int | None:
    for col_idx in range(1, ws.max_column + 1):
        header = ws.cell(row=header_row, column=col_idx).value
        if header and platform_name.lower() in str(header).lower():
            return col_idx
    return None
