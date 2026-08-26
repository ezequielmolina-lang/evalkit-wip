"""Claude tool-use loop wrapping the Playwright browser.

Exposes a small set of high-level browser tools (navigate, click, type,
screenshot, read_page, wait, record_observation, done) to Claude via the
Anthropic Messages API. Claude reads the page, decides what to do, calls a
tool, we execute it, append the tool_result to the conversation, and loop
until Claude calls `done` (or we hit cost / iteration / time caps).

Cost discipline:
- Per-turn input pricing is dominated by accumulated conversation; each
  `read_page` adds ~2-3K tokens of accessibility-tree text. We cap individual
  page reads at 8000 chars (~2000 tokens) by default.
- A hard `cost_cap_usd` aborts the run if the running estimate goes over.
  Default $5 per cell.
- A hard `max_iterations` aborts if Claude loops without converging.

Token accounting uses the per-message usage object the API returns
(`input_tokens` and `output_tokens`). We convert with the published Opus 4.x
rates (override via env vars LXP_PRICE_INPUT and LXP_PRICE_OUTPUT, both per
million tokens).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from anthropic import Anthropic, APIError

from ..config import CONFIG
from ..evalkit import pricing as _pricing
from .playwright_driver import PlaywrightDriver

logger = logging.getLogger(__name__)


# Published rates for claude-opus-4-8 in USD per 1M tokens. Override via env if
# rates change or a different model is configured.
#
# These were $15/$75 -- the rate of an older Opus generation, three times the
# real price. Nothing failed loudly: the cap is enforced against this estimate,
# so every step was cut off at a third of its intended budget while the invoice
# said otherwise. A stale price constant is a silent budget cut.
DEFAULT_PRICE_PER_M_INPUT = float(os.getenv("LXP_PRICE_INPUT", "5.0"))
DEFAULT_PRICE_PER_M_OUTPUT = float(os.getenv("LXP_PRICE_OUTPUT", "25.0"))


@dataclass
class Observation:
    test_id: str
    status: str  # PASS / PARTIAL / FAIL / N/A / PENDING / BLOCKED
    evidence: str
    notes: str = ""
    screenshot_paths: list[str] = field(default_factory=list)


@dataclass
class AgentRunResult:
    summary: str = ""
    observations: list[Observation] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    turns: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    blocked: bool = False
    block_reason: str | None = None
    #: What the agent said in `done`. None means it never called `done` at all,
    #: so nothing was claimed -- which callers must NOT read as success. See
    #: agent_driver.STEP_DONE_TOOL: `blocked` only covers caps and API errors,
    #: not "I called done to explain that I could not do this".
    accomplished: bool | None = None
    done_reason: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "observations": [obs.__dict__ for obs in self.observations],
            "screenshots": [str(p) for p in self.screenshots],
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "accomplished": self.accomplished,
            "done_reason": self.done_reason,
        }


# ----------------------------------------------------------------------
# Tool schemas Claude sees
# ----------------------------------------------------------------------

BROWSER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "navigate",
        "description": (
            "Navigate the browser to a URL. Only use this for top-level "
            "navigation; for in-app clicks use `click`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL to navigate to."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "click",
        "description": (
            "Click on an element described by its visible text or accessible "
            "name. The harness tries text match, then button role, then link "
            "role. Pass the exact label you see in the page text, e.g. "
            "'Iniciar sesión' or 'Comprobar'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_description": {
                    "type": "string",
                    "description": "Visible text or accessible name of the element to click.",
                },
            },
            "required": ["element_description"],
        },
    },
    {
        "name": "type_text",
        "description": (
            "Type text into the currently focused element. Call `click` on the "
            "input first to focus it. Use for filling form fields and "
            "submitting tutor messages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to type."},
                "press_enter_after": {
                    "type": "boolean",
                    "description": "If true, press Enter after typing. Default false.",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "screenshot",
        "description": (
            "Capture a full-page screenshot. Use a short descriptive label so "
            "the file is easy to find later. Always screenshot architecture "
            "views and feedback states."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Short descriptive label (e.g., 'wrong_answer_feedback').",
                },
            },
            "required": ["label"],
        },
    },
    {
        "name": "read_page",
        "description": (
            "Return a compact text rendering of the current page (accessibility "
            "tree). Use this to decide what to click next. Cap with max_chars "
            "to keep token spend in check."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 6000).",
                },
            },
        },
    },
    {
        "name": "wait",
        "description": (
            "Pause for a number of seconds. Use after triggering an action "
            "that requires the SPA to load (max 10s)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Seconds to wait (max 10)."},
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "record_observation",
        "description": (
            "Record a finding for a specific rubric test. Call this whenever "
            "you have enough evidence to score a test_id (PT-D1-02, "
            "PT-D4-02, etc.). Status must be one of PASS / PARTIAL / FAIL / "
            "N/A / PENDING / BLOCKED. Cite which screenshot(s) support the "
            "finding in the evidence text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "test_id": {"type": "string", "description": "Test ID from the rubric."},
                "status": {
                    "type": "string",
                    "enum": ["PASS", "PARTIAL", "FAIL", "N/A", "PENDING", "BLOCKED"],
                },
                "evidence": {
                    "type": "string",
                    "description": "1-3 sentence justification with screenshot reference if relevant.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional additional context, direct quotes from the page, etc.",
                },
            },
            "required": ["test_id", "status", "evidence"],
        },
    },
    {
        "name": "done",
        "description": (
            "Call when the task is complete. Provide a short summary of what "
            "you observed and decided. After calling `done` you will not get "
            "another turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Short summary of findings."},
            },
            "required": ["summary"],
        },
    },
]


def read_only_tools() -> list[dict[str, Any]]:
    """`BROWSER_TOOLS` sin `type_text`: el explorador mira, no escribe.

    El reconocimiento recorre plataformas EN PRODUCCIÓN -- Plataforma A está en piloto
    en 20 escuelas con alumnos reales -- y entra con una cuenta de ese piloto.
    Un `type_text` en el chat del tutor no es una prueba: es un mensaje real en
    la cuenta de un chico. Pedirlo en el prompt no alcanza, porque el modelo
    puede decidir ignorarlo y para cuando se nota ya escribió. Sin la
    herramienta, escribir deja de ser una decisión suya.

    Devuelve una lista NUEVA: `BROWSER_TOOLS` es el mismo objeto que devuelve
    `tools_default()`, así que filtrarlo en el lugar dejaría al próximo agente
    normal sin poder enviarle estímulos al tutor.
    """
    return [t for t in BROWSER_TOOLS if t["name"] != "type_text"]


# ----------------------------------------------------------------------
# Claude browser agent
# ----------------------------------------------------------------------


#: Anthropic rejects images whose longest edge exceeds this many pixels.
_API_IMAGE_MAX_PX = 8000


def _image_block_for_api(path) -> dict:
    """A base64 image block for the model, downscaled to fit the API limit.

    The agent SEES its screenshots (that is what let it confirm the tutor panel
    opened), but a full-page shot of a long page -- a vendor's public site --
    exceeds 8000px on one edge and the API 400s, killing the probe. Only the
    copy SENT to the model is downscaled; the file on disk stays full-res for
    the ledger and the auditor, so provenance is untouched.
    """
    from pathlib import Path as _P

    raw = _P(path).read_bytes()
    try:
        import io
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        if max(im.size) > _API_IMAGE_MAX_PX:
            scale = _API_IMAGE_MAX_PX / max(im.size)
            new = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
            im = im.convert("RGB").resize(new, Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG", optimize=True)
            raw = buf.getvalue()
    except Exception:  # noqa: BLE001 -- if PIL is absent, send as-is and let the API judge
        pass
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(raw).decode("ascii"),
        },
    }


def _with_cache_breakpoint(messages: list[Any]) -> list[Any]:
    """Copy of `messages` with a cache breakpoint on the last content block.

    Only the LAST message is marked: the prefix before it is already stable, so
    one breakpoint there lets every later turn read the whole conversation from
    cache. Marking earlier turns too would spend breakpoints (max 4) without
    buying anything.

    Returns a shallow copy -- mutating the caller's history would leave stale
    `cache_control` keys on old turns and quietly waste the breakpoints.
    """
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(content, list) and content:
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        if isinstance(blocks[-1], dict):
            blocks[-1]["cache_control"] = {"type": "ephemeral"}
        last["content"] = blocks
    else:
        return messages
    out[-1] = last
    return out


class ClaudeBrowserAgent:
    """Runs a Claude tool-use loop that drives `driver` (a PlaywrightDriver).

    The agent receives an initial system prompt + user task, and converses
    with Claude in turns. Each turn it executes any tool calls and feeds the
    results back. The loop ends when:
    - Claude calls `done`
    - cost cap reached
    - iteration cap reached
    - an unrecoverable error occurs (logged, agent marked BLOCKED)
    """

    def __init__(
        self,
        driver: PlaywrightDriver,
        *,
        model: str | None = None,
        max_iterations: int = 25,
        cost_cap_usd: float = 5.0,
        price_per_m_input: float = DEFAULT_PRICE_PER_M_INPUT,
        price_per_m_output: float = DEFAULT_PRICE_PER_M_OUTPUT,
        transcript_dir: Path | None = None,
    ) -> None:
        self.driver = driver
        self.model = model or CONFIG.model_heavy
        self.max_iterations = max_iterations
        self.cost_cap_usd = cost_cap_usd
        # Per-instance so a caller can swap in a different tool list -- notably
        # AgentDriver, which replaces `done` with one that has a required
        # `accomplished` field. Sending the module global unconditionally would
        # make that swap silently inert.
        self.tools = self.tools_default()
        self.price_per_m_input = price_per_m_input
        self.price_per_m_output = price_per_m_output
        self.transcript_dir = transcript_dir

        if CONFIG.dry_run:
            self.client = None
        else:
            if not CONFIG.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set (or set LXP_DRY_RUN=true)")
            self.client = Anthropic(api_key=CONFIG.anthropic_api_key)

    # ------------------------------------------------------------------

    @staticmethod
    def tools_default() -> list[dict[str, Any]]:
        """The whole-task tool list a fresh agent starts with."""
        return BROWSER_TOOLS

    async def run(self, *, system: str, user_task: str) -> AgentRunResult:
        result = AgentRunResult()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_task},
        ]

        if CONFIG.dry_run:
            logger.info("[DRY RUN] Skipping Claude API tool-use loop")
            result.summary = "Dry-run — no API calls made."
            result.accomplished = True  # nothing was attempted, nothing is claimed real
            result.transcript.append({"system": system, "user": user_task, "note": "dry-run"})
            self._maybe_save_transcript(result)
            return result

        assert self.client is not None

        for iteration in range(self.max_iterations):
            result.turns = iteration + 1
            logger.info("Claude turn %d / %d", result.turns, self.max_iterations)

            try:
                # PROMPT CACHING. This loop runs up to 20 turns per step and
                # every turn appends a screenshot (1-5k tokens as an image)
                # plus a page read, so an uncached request N pays full price
                # for everything turns 1..N-1 already sent -- cost grows with
                # the square of the turn count. The scorer cached from the
                # start; this loop never did, and feeding it image blocks made
                # each turn heavier still.
                #
                # `system` and `tools` are fixed for the whole step, so they
                # cache once. The second breakpoint rides the LAST message,
                # which is what lets turn N read turn N-1's prefix instead of
                # re-processing it.
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=[{
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    tools=self.tools,
                    messages=_with_cache_breakpoint(messages),
                )
            except APIError as e:
                logger.error("Claude API error on turn %d: %s", result.turns, e)
                result.blocked = True
                result.block_reason = f"APIError: {e}"
                break

            usage = getattr(response, "usage", None)
            if usage:
                result.input_tokens += getattr(usage, "input_tokens", 0)
                result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
                result.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
                result.output_tokens += getattr(usage, "output_tokens", 0)
                result.estimated_cost_usd = self._cost(
                    result.input_tokens,
                    result.output_tokens,
                    cache_read=result.cache_read_tokens,
                    cache_write=result.cache_write_tokens,
                )

            if result.estimated_cost_usd > self.cost_cap_usd:
                logger.error(
                    "Cost cap exceeded: $%.2f > $%.2f — aborting",
                    result.estimated_cost_usd,
                    self.cost_cap_usd,
                )
                result.blocked = True
                result.block_reason = f"Cost cap ${self.cost_cap_usd:.2f} exceeded"
                break

            # Append the assistant message to history so the next turn sees it.
            assistant_content = []
            tool_uses: list[Any] = []
            text_blocks: list[str] = []
            for block in response.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    text_blocks.append(getattr(block, "text", ""))
                    assistant_content.append({"type": "text", "text": getattr(block, "text", "")})
                elif btype == "tool_use":
                    tool_uses.append(block)
                    assistant_content.append({
                        "type": "tool_use",
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "input": getattr(block, "input", {}),
                    })
            messages.append({"role": "assistant", "content": assistant_content})

            if text_blocks:
                logger.info("Claude said: %s", " | ".join(t.strip()[:200] for t in text_blocks if t.strip()))

            stop_reason = getattr(response, "stop_reason", None)

            if not tool_uses:
                # No tools called — Claude is done talking. Treat as soft end.
                logger.info("No tool calls — ending loop (stop_reason=%s)", stop_reason)
                if not result.summary:
                    result.summary = "\n".join(text_blocks).strip()[:1000]
                break

            # Execute every tool call, build tool_result blocks, append to history
            tool_results: list[dict[str, Any]] = []
            done_requested = False
            for tu in tool_uses:
                tool_name = getattr(tu, "name", "")
                tool_input = getattr(tu, "input", {}) or {}
                tool_id = getattr(tu, "id", "")
                logger.info("Tool call: %s(%s)", tool_name, _short_json(tool_input))

                try:
                    output_text, is_done = await self._dispatch_tool(tool_name, tool_input, result)
                except Exception as e:  # noqa: BLE001
                    logger.exception("Tool %s raised", tool_name)
                    output_text = f"Tool error: {e}"
                    is_done = False

                # A list is already content blocks (the screenshot's image +
                # caption) and passes through untouched -- truncating or
                # str()-ing it would put a Python repr in front of the model.
                if isinstance(output_text, list):
                    content: Any = output_text
                elif isinstance(output_text, str):
                    content = output_text[:6000]
                else:
                    content = str(output_text)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": content,
                })
                if is_done:
                    done_requested = True

            messages.append({"role": "user", "content": tool_results})

            if done_requested:
                logger.info("Claude called `done` — ending loop")
                break

        else:
            logger.warning("Hit max_iterations=%d without `done`", self.max_iterations)
            result.blocked = True
            result.block_reason = f"max_iterations ({self.max_iterations}) reached"

        result.screenshots = list(self.driver.state.screenshots_taken)
        result.transcript = messages
        self._maybe_save_transcript(result)
        return result

    # ------------------------------------------------------------------

    async def _dispatch_tool(
        self,
        name: str,
        args: dict[str, Any],
        result: AgentRunResult,
    ) -> tuple[str, bool]:
        """Execute a tool. Returns (text_for_claude, done_flag)."""
        d = self.driver
        if name == "navigate":
            url = args.get("url", "")
            final = await d.navigate(url)
            return f"Navigated. Current URL: {final}", False

        if name == "click":
            desc = args.get("element_description", "")
            ok = await d.click_element_description(desc)
            if ok:
                return f"Clicked {desc!r}. Current URL: {await d.get_url()}", False
            return f"Click on {desc!r} FAILED — element not found. Read the page and try a different label.", False

        if name == "type_text":
            text = args.get("text", "")
            ok = await d.type_into_focused(text)
            if ok and args.get("press_enter_after"):
                await d.press_key("Enter")
            return ("Typed text." if ok else "Type FAILED — no focused element. Click an input first."), False

        if name == "screenshot":
            label = args.get("label", "snap")
            path = await d.screenshot(label)
            # THE AGENT MUST SEE IT. This used to return the filename alone, so
            # the agent captured the evidence the report is built from and
            # judged every page from the accessibility tree instead. That cost
            # a run on Plataforma A: it clicked the tutor button, the chat panel opened
            # -- plainly visible in the screenshot it had just taken -- and it
            # reported the step unaccomplished because it could not confirm in
            # text what was obvious in the image. Correct behaviour for an
            # agent told to say so when unsure; it simply had no eyes.
            #
            # Sent as an image block so the model sees exactly the bytes the
            # ledger hashes and the auditor will open.
            return [
                _image_block_for_api(path),
                {"type": "text", "text": f"Screenshot saved: {Path(path).name}"},
            ], False

        if name == "read_page":
            max_chars = int(args.get("max_chars", 6000))
            text = await d.read_page(max_chars=max_chars)
            return f"PAGE CONTENT:\n{text}", False

        if name == "wait":
            seconds = min(10.0, float(args.get("seconds", 1)))
            await d.wait(seconds)
            return f"Waited {seconds}s.", False

        if name == "record_observation":
            obs = Observation(
                test_id=args.get("test_id", ""),
                status=args.get("status", "PENDING"),
                evidence=args.get("evidence", ""),
                notes=args.get("notes", ""),
                screenshot_paths=[str(p) for p in d.state.screenshots_taken[-3:]],
            )
            result.observations.append(obs)
            return f"Observation recorded: {obs.test_id} = {obs.status}", False

        if name == "done":
            # `accomplished`/`reason` come from the per-step tool schema
            # (agent_driver.STEP_DONE_TOOL); `summary` from the whole-task one.
            reason = args.get("reason", "") or args.get("summary", "")
            result.summary = result.summary or reason
            result.done_reason = reason
            if "accomplished" in args:
                result.accomplished = bool(args["accomplished"])
            else:
                # Whole-task `done` cannot express failure, so it can only mean
                # "the loop ended cleanly" -- never "the step really happened".
                result.accomplished = True
            return "Acknowledged.", True

        return f"Unknown tool: {name}", False

    # ------------------------------------------------------------------

    #: Published cache multipliers on the input rate: a write costs 1.25x a
    #: fresh token, a read 0.1x. Sourced from `evalkit.pricing` so the formula
    #: has exactly one home -- this stage and the framework/probe/score stages
    #: bill identically.
    CACHE_WRITE_MULT = _pricing.CACHE_WRITE_MULT
    CACHE_READ_MULT = _pricing.CACHE_READ_MULT

    def _cost(
        self,
        in_tok: int,
        out_tok: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> float:
        """USD for one turn. Cached tokens are cheaper, never free.

        Caching moves tokens OUT of `input_tokens` and into their own counters,
        so a version of this that only read `input_tokens` would report a drop
        the moment caching was switched on -- flattering, meaningless as a
        before/after, and worse: the per-step cost cap is enforced against this
        number, so a step would quietly run past the ceiling it was given.

        The arithmetic lives in `evalkit.pricing.cost_usd`; this keeps its own
        signature and prices so every existing caller is undisturbed.
        """
        return _pricing.cost_usd(
            in_tok, out_tok, cache_read, cache_write,
            price_in=self.price_per_m_input, price_out=self.price_per_m_output,
        )

    def _maybe_save_transcript(self, result: AgentRunResult) -> None:
        if not self.transcript_dir:
            return
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        out = self.transcript_dir / f"transcript_{int(time.time())}.json"
        try:
            out.write_text(json.dumps({
                "meta": result.to_dict(),
                "messages": _safe_messages(result.transcript),
            }, indent=2, default=str), encoding="utf-8")
            logger.info("Transcript -> %s", out.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not save transcript: %s", e)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _short_json(obj: Any, n: int = 120) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s if len(s) <= n else s[:n] + "…"


def _safe_messages(messages: list[Any]) -> list[Any]:
    """Best-effort serialisation of a list that may contain nested lists/dicts."""
    out = []
    for m in messages:
        if isinstance(m, dict):
            out.append(m)
        else:
            out.append({"raw": str(m)[:2000]})
    return out
