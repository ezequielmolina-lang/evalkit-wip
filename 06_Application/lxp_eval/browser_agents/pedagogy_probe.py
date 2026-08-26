"""BT-2 Pedagogy Probe — agent that drives a single (platform, subject, grade, lesson) cell.

This is the proof-of-concept implementation of Agent BT-2 from
`platforms/AGENTS_SPEC.md`. For one platform x subject x grade x lesson, it:

1. Logs into the platform using credentials from .env
2. Navigates to the target class + lesson
3. Hands control to Claude with a tightly-scoped task: capture the lesson
   architecture, find the first practice question, submit a deliberately
   wrong answer (only if --allow-submit), classify the feedback against
   BEA 2025 (Mistake Identification, Actionability, Providing Guidance),
   optionally probe the AI tutor with prompt T-1
4. Writes a markdown observation note + screenshots to the platform's
   `observation_notes/` directory

The Claude-side reasoning is what makes this non-trivial: rather than
hardcoded selectors for "the wrong-answer button", Claude reads the page,
identifies an input question, types something nonsensical, and classifies
whatever the platform shows back.

Output format mirrors `REAL_obs_2026-05-22_b_nacional_student.md` so SYNTH-1 can
parse multiple notes uniformly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import plataforma_b_recipes, plataforma_a_recipes
from .claude_browser_agent import AgentRunResult, ClaudeBrowserAgent
from .playwright_driver import PlaywrightDriver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------


SYSTEM_PROMPT_PEDAGOGY_PROBE = """You are Agent BT-2 (Pedagogy Probe) in a multi-agent \
LXP evaluation campaign for the la organización x el país Ministry of \
Education comparison of PLATAFORMA_B (B-Nacional) vs Plataforma A vs Plataforma C.

Your job is narrow and specific: for ONE lesson, observe and classify how \
the platform teaches against the cognitive-science rubric below. You do not \
score the whole campaign — just this lesson.

## Rubric tests in scope for this run

You are scoring up to these tests for this lesson:

**D1 — Explicit Instruction Architecture (Rosenshine)**
- PT-D1-01 Architecture visible (Repaso/Comprensión/Ejercitación sections labeled)
- PT-D1-02 I Do (worked example with think-aloud)
- PT-D1-03 We Do (guided practice)
- PT-D1-04 You Do (independent practice — separable from We Do)
- PT-D1-06 NO default discovery (lesson does NOT start with open-ended exploration)

**D4 — Feedback (BEA 2025 — Behaviour Education Adoption framework)**
- PT-D4-01 Feedback latency < 1 second after submission
- PT-D4-02 BEA-MI Mistake Identification: feedback explicitly names the error type
- PT-D4-03 Mastery gating: cannot advance without demonstrating mastery
- PT-D4-06 Re-teach (not just retry) after a wrong answer
- PT-D4-07 BEA-AC Actionability: feedback says what to do next
- PT-D4-08 BEA-PG Providing Guidance: hint or scaffolded step provided

**D5 — Misconceptions**
- PT-D5-02 Distractors map to known misconceptions (for MC questions)
- PT-D5-04 Refutational handling: tutor explicitly addresses the misconception

**D9 — Personalization (only if you open the AI tutor)**
- PT-D9-01 First response uses generation effect (asks a question / probes thinking) rather than dumping the answer

## Operating principles

1. **Read before you click.** Always call `read_page` after navigation so you understand the layout, then choose precise click targets from what you actually see.
2. **Screenshot the architecture, the question, and every feedback state.** You cannot over-screenshot; you CAN under-screenshot.
3. **Be honest about evidence.** If you only observed half of D1's architecture, mark the rest PENDING. Do NOT extrapolate PASS from marketing language or platform reputation.
4. **One observation per test_id.** Call `record_observation` once per test you have evidence to evaluate. Use status BLOCKED if a step you needed to perform failed (e.g. couldn't find the submit button).
5. **Stop when done.** Don't keep exploring — call `done` with a one-paragraph summary as soon as you've covered the in-scope tests.
6. **Stay in this lesson.** Do not navigate away from the current lesson URL.

## Constraints

- You have a hard cap of approximately 25 turns and $5 of API spend.
- Allow_submit gate: if the user has not enabled wrong-answer submission, \
the harness will tell you and you should observe architecture / read \
feedback hints only, not actually submit.
- The browser is headful — a human can interrupt at any time.
"""


@dataclass
class CellSpec:
    platform: Literal["plataforma_b", "plataforma_a"]
    subject: Literal["math", "language"]
    grade: int
    lesson: str  # CNEB code like "1.4" or OVA code
    allow_submit: bool = False
    open_tutor: bool = False


# ---------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------


async def run_pedagogy_probe(
    cell: CellSpec,
    *,
    headless: bool = False,
    cost_cap_usd: float = 5.0,
    max_iterations: int = 25,
    output_root: Path | None = None,
) -> dict:
    """Execute one BT-2 Pedagogy Probe cell end-to-end.

    Returns a dict with the run result, observation-note path, and screenshot
    list. Caller is responsible for updating CELL_TRACKER.md.
    """
    output_root = output_root or _default_output_root()
    cell_id = f"{cell.platform}_{cell.subject}_g{cell.grade}_L{cell.lesson}"
    run_ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    cell_dir = output_root / cell.platform / "observation_notes" / f"BT2_{cell_id}_{run_ts}"
    screenshots_dir = cell_dir / "screenshots"
    transcripts_dir = cell_dir / "transcripts"
    cell_dir.mkdir(parents=True, exist_ok=True)

    driver = PlaywrightDriver(screenshots_dir=screenshots_dir, headless=headless)
    await driver.launch()

    setup_result = {"login": False, "class_opened": False, "lesson_opened": False}
    try:
        # ----- Phase 1: deterministic setup (login + navigate to lesson) -----
        if cell.platform == "plataforma_b":
            creds = plataforma_b_recipes.Plataforma BCredentials.from_env("student")
            setup_result["login"] = await plataforma_b_recipes.login(driver, creds)
            if setup_result["login"]:
                setup_result["class_opened"] = await plataforma_b_recipes.open_class(
                    driver, subject=cell.subject, grade=cell.grade
                )
            if setup_result["class_opened"]:
                setup_result["lesson_opened"] = await plataforma_b_recipes.open_lesson(driver, cell.lesson)
        elif cell.platform == "plataforma_a":
            # Default to teacher role for Plataforma A (toggle to student view inside the app)
            creds = plataforma_a_recipes.Plataforma ACredentials.from_env("teacher")
            setup_result["login"] = await plataforma_a_recipes.login(driver, creds)
            if setup_result["login"]:
                setup_result["class_opened"] = await plataforma_a_recipes.open_class(
                    driver, subject=cell.subject, grade=cell.grade
                )
            if setup_result["class_opened"]:
                setup_result["lesson_opened"] = await plataforma_a_recipes.open_lesson(driver, cell.lesson)
        else:
            raise ValueError(f"Unknown platform: {cell.platform}")

        if not setup_result["lesson_opened"]:
            # Cannot run probe — mark BLOCKED and bail
            note_path = _write_blocked_note(
                cell_dir, cell, setup_result, screenshots=list(driver.state.screenshots_taken),
            )
            return {
                "cell_id": cell_id,
                "status": "BLOCKED",
                "block_reason": _describe_setup_failure(setup_result),
                "observation_note": str(note_path),
                "screenshots": [str(p) for p in driver.state.screenshots_taken],
                "estimated_cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
            }

        # ----- Phase 2: Claude tool-use loop drives the probe -----
        agent = ClaudeBrowserAgent(
            driver,
            cost_cap_usd=cost_cap_usd,
            max_iterations=max_iterations,
            transcript_dir=transcripts_dir,
        )
        user_task = _build_user_task(cell)
        run = await agent.run(system=SYSTEM_PROMPT_PEDAGOGY_PROBE, user_task=user_task)

        # ----- Phase 3: write observation note -----
        note_path = _write_observation_note(cell_dir, cell, run, setup_result)

        return {
            "cell_id": cell_id,
            "status": "BLOCKED" if run.blocked else "COMPLETE",
            "block_reason": run.block_reason,
            "observation_note": str(note_path),
            "screenshots": [str(p) for p in run.screenshots],
            "n_observations": len(run.observations),
            "turns": run.turns,
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "estimated_cost_usd": round(run.estimated_cost_usd, 4),
            "summary": run.summary,
        }
    finally:
        await driver.close()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _default_output_root() -> Path:
    # Default to the platforms/ tree that sits two levels above this file
    here = Path(__file__).resolve().parent
    return (here.parent.parent / "platforms").resolve()


def _build_user_task(cell: CellSpec) -> str:
    submit_clause = (
        "You ARE allowed to submit a deliberate wrong answer to exactly ONE input "
        "question. Use a clearly wrong value (e.g., type the digit 9 for an "
        "arithmetic question whose correct answer is obviously different). "
        "Screenshot before and after submission."
        if cell.allow_submit
        else "You are NOT allowed to submit answers (--allow-submit not set). Only "
        "OBSERVE the architecture, the practice question shapes, and any "
        "default scaffolding / hints visible without interacting. Mark D4 BEA "
        "tests as PENDING with a note that submission is disabled."
    )
    tutor_clause = (
        "After completing the lesson observation, OPEN the AI tutor (Tutor IA / "
        "Plataforma A) and send exactly the prompt T-1: \"Me equivoqué, ¿puedes "
        "ayudarme a entender qué hice mal?\" — then screenshot the response "
        "and score PT-D9-01 based on whether the first response asks a "
        "question (generation effect) or dumps the answer."
        if cell.open_tutor
        else "Do NOT open the AI tutor this run. Mark D9 tests as PENDING."
    )
    return f"""Run BT-2 Pedagogy Probe for ONE lesson.

**Target cell:**
- Platform: {cell.platform.upper()}
- Subject: {cell.subject}
- Grade: {cell.grade}
- Lesson: {cell.lesson}

The harness has already logged in and opened the lesson page in the browser. \
Begin by calling `read_page` and `screenshot('lesson_architecture')` to \
capture the starting state.

**Procedure:**
1. Read the page. Identify and screenshot the lesson architecture (sidebar \
   sections, lesson header). Record observations for PT-D1-01 and PT-D1-06 \
   based on what you see.
2. Click into each visible architecture section in order. Screenshot the \
   start of each section. Record observations for PT-D1-02 / PT-D1-03 / \
   PT-D1-04 as evidence accumulates.
3. Navigate to the first input or multiple-choice question. Screenshot it.
4. {submit_clause}
5. If you submitted: read the feedback, screenshot it, and record \
   observations for PT-D4-01 (latency by your subjective sense), PT-D4-02 \
   (does the feedback name the error?), PT-D4-03 (can you press Next \
   anyway?), PT-D4-06 / PT-D4-07 / PT-D4-08.
6. {tutor_clause}
7. Call `done` with a one-paragraph summary.

Be conservative: PENDING is correct when you didn't observe enough. Don't \
fabricate evidence. The goal is honest, replicable observations the human \
reviewer can defend.
"""


def _describe_setup_failure(setup: dict) -> str:
    if not setup["login"]:
        return "Login failed — credentials missing/wrong or login form changed."
    if not setup["class_opened"]:
        return "Could not find the target class card on the post-login dashboard."
    if not setup["lesson_opened"]:
        return "Could not find the target lesson within the class view."
    return "Unknown setup failure."


def _write_observation_note(
    cell_dir: Path,
    cell: CellSpec,
    run: AgentRunResult,
    setup: dict,
) -> Path:
    out = cell_dir / "OBSERVATION.md"
    lines: list[str] = []
    lines.append(f"# BT-2 Pedagogy Probe — {cell.platform.upper()} {cell.subject} g{cell.grade} L{cell.lesson}")
    lines.append("")
    lines.append(f"- **Date (UTC):** {datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append(f"- **Observer:** Claude (Playwright PoC, model {run_model_id_from_env()})")
    lines.append(f"- **Platform:** {cell.platform.upper()}")
    lines.append(f"- **Subject / grade / lesson:** {cell.subject} / {cell.grade} / {cell.lesson}")
    lines.append(f"- **Submission allowed:** {cell.allow_submit}")
    lines.append(f"- **Tutor probed:** {cell.open_tutor}")
    lines.append("")
    lines.append("## Run telemetry")
    lines.append("")
    lines.append(f"- Turns: {run.turns}")
    lines.append(f"- Input tokens: {run.input_tokens}")
    lines.append(f"- Output tokens: {run.output_tokens}")
    lines.append(f"- Estimated cost: ${run.estimated_cost_usd:.4f}")
    lines.append(f"- Blocked: {run.blocked}" + (f" — {run.block_reason}" if run.block_reason else ""))
    lines.append(f"- Login: {setup['login']}; Class opened: {setup['class_opened']}; Lesson opened: {setup['lesson_opened']}")
    lines.append("")
    lines.append("## Claude summary")
    lines.append("")
    lines.append(run.summary or "_(none)_")
    lines.append("")
    lines.append("## Observations")
    lines.append("")
    if not run.observations:
        lines.append("_No observations recorded._")
    else:
        lines.append("| Test ID | Status | Evidence | Screenshots |")
        lines.append("|---|---|---|---|")
        for obs in run.observations:
            ev = obs.evidence.replace("|", "\\|").replace("\n", " ")[:400]
            scrs = ", ".join(Path(p).name for p in obs.screenshot_paths) or "—"
            lines.append(f"| {obs.test_id} | {obs.status} | {ev} | {scrs} |")
            if obs.notes:
                notes = obs.notes.replace("|", "\\|").replace("\n", " ")[:600]
                lines.append(f"| | | _notes:_ {notes} | |")
    lines.append("")
    lines.append("## Screenshots")
    lines.append("")
    for p in run.screenshots:
        lines.append(f"- `{Path(p).name}`")
    lines.append("")
    lines.append("---")
    lines.append("Generated by `lxp_eval.browser_agents.pedagogy_probe`.")
    out.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Observation note -> %s", out)
    return out


def _write_blocked_note(
    cell_dir: Path,
    cell: CellSpec,
    setup: dict,
    screenshots: list[Path],
) -> Path:
    out = cell_dir / "BLOCKED.md"
    lines = [
        f"# BT-2 BLOCKED — {cell.platform.upper()} {cell.subject} g{cell.grade} L{cell.lesson}",
        "",
        f"- **Date (UTC):** {datetime.utcnow().isoformat(timespec='seconds')}",
        f"- **Reason:** {_describe_setup_failure(setup)}",
        f"- **Login:** {setup['login']}",
        f"- **Class opened:** {setup['class_opened']}",
        f"- **Lesson opened:** {setup['lesson_opened']}",
        "",
        "## Screenshots captured before block",
        "",
    ]
    for p in screenshots:
        lines.append(f"- `{Path(p).name}`")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_model_id_from_env() -> str:
    import os
    return os.getenv("LXP_MODEL_HEAVY", "unknown")
