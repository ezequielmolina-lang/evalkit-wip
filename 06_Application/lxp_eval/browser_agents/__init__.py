"""Browser-driving agents for LXP evaluation campaign.

This package provides a Playwright-based browser harness controlled by Claude
via the Anthropic API tool-use loop. It implements the BT-2 Pedagogy Probe as a
proof-of-concept for unattended overnight execution of browser test agents
against live LXP platforms (PLATAFORMA_B, Plataforma A).

Modules:
    playwright_driver  - low-level browser primitives (launch, navigate, click, type, screenshot)
    plataforma_b_recipes       - PLATAFORMA_B-specific login + navigation flows
    plataforma_a_recipes       - Plataforma A-specific login + navigation flows
    claude_browser_agent - Claude tool-use loop wrapping the browser
    pedagogy_probe     - BT-2 Pedagogy Probe orchestrator
    run_browser_cell   - CLI entrypoint
"""
__version__ = "0.1.0-poc"
