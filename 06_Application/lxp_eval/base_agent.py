"""Base agent class — wraps the Anthropic Messages API with tool use.

All concrete agents inherit from BaseAgent and implement `evaluate()`.
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic, APIError

from .config import CONFIG
from .platform_loader import LoadedDocument, PlatformConfig
from .rubric import DomainResult, Test, TestResult

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Standard return shape for any agent."""
    agent_name: str
    platform: str
    domain_results: list[DomainResult] = field(default_factory=list)
    pre_filter_results: list[dict] = field(default_factory=list)
    disqualifier_results: list[dict] = field(default_factory=list)
    potemkin_flags: list[dict] = field(default_factory=list)
    transcripts: list[dict] = field(default_factory=list)  # raw Claude interactions
    pending_questions: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def total_points(self) -> float:
        return sum(dr.total_points for dr in self.domain_results)


class BaseAgent(ABC):
    """Base class for all evaluation agents."""

    agent_name: str = "BaseAgent"
    model: str = "heavy"  # "heavy" or "light"
    max_iterations: int = 5  # for tool-use loops

    def __init__(self):
        if not CONFIG.dry_run:
            if not CONFIG.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set; set LXP_DRY_RUN=true to skip API.")
            self.client = Anthropic(api_key=CONFIG.anthropic_api_key)
        else:
            self.client = None

    @property
    def model_id(self) -> str:
        return CONFIG.model_heavy if self.model == "heavy" else CONFIG.model_light

    @abstractmethod
    def evaluate(
        self,
        platform: PlatformConfig,
        vendor_docs: list[LoadedDocument],
        observation_notes: list[LoadedDocument],
        **kwargs: Any,
    ) -> AgentResult:
        """Subclasses implement."""

    # ------------------------------------------------------------
    # Helpers for subclasses
    # ------------------------------------------------------------

    def _call_claude(
        self,
        system: str,
        user: str,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Single Claude API call. Returns raw response as dict.

        In dry-run mode, prints the prompt and returns a stub response.
        """
        max_tokens = max_tokens or CONFIG.max_tokens_per_call

        if CONFIG.dry_run:
            logger.info("[DRY RUN] Would call Claude (model=%s, tools=%s)", self.model_id, bool(tools))
            print(f"\n{'=' * 80}\n[DRY RUN PROMPT — model={self.model_id}]\n{'=' * 80}")
            print(f"SYSTEM:\n{system[:500]}{'...' if len(system) > 500 else ''}")
            print(f"\nUSER:\n{user[:1500]}{'...' if len(user) > 1500 else ''}")
            print(f"\nTOOLS: {[t['name'] for t in tools] if tools else None}")
            return {
                "id": "dry_run",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "[DRY RUN — no API call made]"}],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }

        last_exc: Exception | None = None
        for attempt in range(CONFIG.max_retries + 1):
            try:
                kwargs: dict[str, Any] = dict(
                    model=self.model_id,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
                if tools:
                    kwargs["tools"] = tools
                response = self.client.messages.create(**kwargs)
                return response.model_dump()
            except APIError as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning("APIError on attempt %d: %s — retrying in %ds", attempt + 1, exc, wait)
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.error("Unexpected error: %s", exc)
                break
        raise RuntimeError(f"Claude API call failed after {CONFIG.max_retries + 1} attempts: {last_exc}")

    def _extract_tool_calls(self, response: dict, tool_name: str) -> list[dict]:
        """Extract all tool_use blocks for a given tool from the response."""
        if not response.get("content"):
            return []
        calls = []
        for block in response["content"]:
            if block.get("type") == "tool_use" and block.get("name") == tool_name:
                calls.append(block.get("input", {}))
        return calls

    def _extract_text(self, response: dict) -> str:
        """Concatenate all text blocks from the response."""
        if not response.get("content"):
            return ""
        return "\n".join(
            b.get("text", "") for b in response["content"] if b.get("type") == "text"
        )

    # ------------------------------------------------------------
    # Tool schemas (reusable)
    # ------------------------------------------------------------

    @staticmethod
    def tool_score_test() -> dict:
        """Standard tool for scoring a rubric test."""
        return {
            "name": "score_test",
            "description": (
                "Record a score for a single rubric test. Call once per test you have "
                "evidence to evaluate. If you cannot evaluate a test from the supplied "
                "documents, use status=PENDING and explain in `evidence` what data is "
                "needed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "test_id": {
                        "type": "string",
                        "description": "Test ID, e.g., 'PT-D1-02'.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["PASS", "PARTIAL", "FAIL", "N/A", "PENDING"],
                        "description": (
                            "PASS = test fully meets criteria; PARTIAL = partial credit; "
                            "FAIL = does not meet; N/A = not applicable to this platform; "
                            "PENDING = insufficient evidence in supplied documents."
                        ),
                    },
                    "evidence": {
                        "type": "string",
                        "description": (
                            "1-3 sentence justification with reference to source "
                            "(e.g., 'p.4 of privacy_policy.pdf states ...'). For PENDING, "
                            "describe what data is needed."
                        ),
                    },
                    "notes": {
                        "type": "string",
                        "description": "Additional notes, direct quotes, or page references.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of source references (e.g., ['vendor_doc:p.4']).",
                    },
                },
                "required": ["test_id", "status", "evidence"],
            },
        }

    @staticmethod
    def tool_flag_potemkin() -> dict:
        """Standard tool for flagging a Potemkin claim."""
        return {
            "name": "flag_potemkin",
            "description": (
                "Flag a vendor claim that matches one of the 5 Potemkin patterns from "
                "the framework (P1-P5). Call this whenever you detect a misrepresented "
                "evidence claim. This does not disqualify the platform — it documents "
                "the discrepancy."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern_id": {
                        "type": "string",
                        "enum": ["P1", "P2", "P3", "P4", "P5"],
                        "description": (
                            "P1=Pilot without control; P2=Underpowered/single-site; "
                            "P3=Unsupported 'evidence-based'; P4=Gains without CIs/overlap; "
                            "P5=Vendor-funded as independent."
                        ),
                    },
                    "vendor_claim": {
                        "type": "string",
                        "description": "Direct quote or paraphrase of the vendor's claim.",
                    },
                    "actual_evidence_level": {
                        "type": "string",
                        "description": "What the evidence actually supports (e.g., 'Tier 4 only').",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Why this matches the pattern; what gap exists.",
                    },
                },
                "required": ["pattern_id", "vendor_claim", "actual_evidence_level", "explanation"],
            },
        }
