"""Output tools: XLSX populator, Markdown reports, HTML dashboard."""
from .xlsx_writer import populate_rubric_xlsx
from .report_writer import write_platform_report, write_comparison_report
from .dashboard_writer import write_html_dashboard

__all__ = [
    "populate_rubric_xlsx",
    "write_platform_report",
    "write_comparison_report",
    "write_html_dashboard",
]
