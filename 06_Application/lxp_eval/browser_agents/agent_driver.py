"""Drive a probe's PROSE steps with the Claude browser agent.

Probes are written in plain language on purpose -- the pitch is "criteria in
plain language -> probes -> an agent executes them", and the original evaluation
was literally Claude driving a browser. A deterministic DSL would force the
probe generator to know each platform's DOM, which breaks "point it at any
platform".

So this adapter wraps `ClaudeBrowserAgent` (the tool-use loop) plus the
`PlaywrightDriver` it drives, and exposes exactly the narrow interface the probe
runner already expects:

    assert_session_alive() / do(step) / screenshot(name) / read_page() / url()

`run_probe` therefore needs no change: it works with a PlaywrightDriver (fast,
deterministic DSL steps) or an AgentDriver (prose steps), whichever it is given.

Two invariants carry over from the rest of evalkit:

* Fail loud. If the agent cannot accomplish a step, `do()` raises. A step that
  silently no-ops leaves the page in the wrong state and turns the NEXT
  screenshot into evidence of something else entirely -- the same bug class as
  attaching a screenshot of a different probe.

  This used to key off `result.blocked`, which was the WRONG SIGNAL: `blocked`
  is set only when the agent hits an iteration/cost cap or the API errors. The
  step prompt tells the agent that if a step is impossible it should call `done`
  and explain why -- and `done` is the SUCCESS path, so a genuinely impossible
  step returned a clean result, the step silently no-opped, and the next
  screenshot became evidence of the wrong page. The `done` tool therefore now
  carries a REQUIRED `accomplished: bool`, and only `accomplished is True`
  counts as done.

* Bounded work. A confused agent must not spin or burn tokens, so each single
  step gets a much tighter iteration and cost cap than a whole task would.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..evalkit.errors import EvalkitError

logger = logging.getLogger(__name__)

#: One prose step is usually read_page -> act -> read_page -> done (~4 turns).
#:
#: Was 8, on the assumption that a step is one action. The first live run
#: disproved it: on Plataforma A the AI tutor is not on the student home page at all --
#: it lives inside a course, behind a lesson -- so "open the tutor chat and
#: send a message" is four or five navigations, each needing its own page read.
#: The agent was doing the right thing and running out of turns.
#:
#: 20 fits a multi-hop navigation while staying a bounded runaway guard. A step
#: that needs more than this is a step that should be split.
DEFAULT_MAX_ITERATIONS_PER_STEP = int(os.getenv("LXP_MAX_ITERS_PER_STEP", "20"))

#: A single step should cost cents, not dollars. Second bound in case the page
#: is huge and each turn carries a big accessibility tree.
#:
#: Raised from 0.50 after the first live run, where it stopped every step
#: mid-way. Plataforma A's pages carry a large accessibility tree, so one screenshot
#: plus one page read can be several thousand tokens, and eight turns of that
#: does not fit in fifty cents. The cap is a runaway guard, not a budget: a
#: step that legitimately needs a few turns of a big page must be able to
#: finish, or the run produces no evidence at all -- which costs more than the
#: step would have.
DEFAULT_COST_CAP_USD_PER_STEP = float(os.getenv("LXP_COST_CAP_PER_STEP", "2.00"))

#: Verbs PlaywrightDriver.do() understands. If every step is one of these we can
#: skip the LLM entirely and run the deterministic fast path.
DSL_VERBS = ("goto", "click", "fill", "press", "wait", "expect_text")

#: The `done` tool as a SINGLE STEP must offer it. `accomplished` is required
#: precisely because the old schema made "I did it" and "I could not do it"
#: indistinguishable -- both were just a call to `done` with a summary.
STEP_DONE_TOOL: dict[str, Any] = {
    "name": "done",
    "description": (
        "Call when you have finished with this step -- whether you managed it "
        "or not. You MUST say which: set `accomplished` to true only if the "
        "step actually happened on the page. If the element does not exist, the "
        "account lacks access, or you are not sure it worked, set it to false "
        "and say why in `reason`. Reporting false is not a failure on your part; "
        "reporting true for a step that did not happen corrupts the evaluation, "
        "because the screenshot taken next would be filed as evidence of this "
        "step."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "accomplished": {
                "type": "boolean",
                "description": "True only if the step was really carried out on the page.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you did, or -- when accomplished is false -- exactly "
                    "what stopped you."
                ),
            },
        },
        "required": ["accomplished", "reason"],
    },
}

STEP_SYSTEM = (
    "Sos un agente que opera un navegador para ejecutar UN paso de una prueba en "
    "una plataforma educativa. Hacé exactamente el paso que te piden sobre la "
    "página actual y nada más: no evalúes, no saques conclusiones, no sigas a "
    "otros pasos. Usá las herramientas del navegador. Cuando termines, llamá "
    "siempre a `done`.\n\n"
    "`done` lleva un campo obligatorio `accomplished`. Poné accomplished=true "
    "SOLO si el paso ocurrió de verdad en la página. Si el elemento no existe, "
    "si hace falta otra cuenta, o si no estás seguro de que haya funcionado, "
    "poné accomplished=false y explicá en `reason` qué te lo impidió.\n\n"
    "Decir que no pudiste NO es un error tuyo: la captura de pantalla que se "
    "toma después de este paso se guarda como evidencia de este paso, así que un "
    "accomplished=true sobre un paso que no pasó convierte la evaluación en una "
    "conclusión falsa sobre la plataforma."
)


def _step_tools() -> list[dict[str, Any]]:
    """The browser tools, with `done` replaced by the per-step version.

    Imported lazily and defensively: the tool list lives with the agent, and
    this module must stay importable (and testable) without it.
    """
    try:
        from .claude_browser_agent import BROWSER_TOOLS
    except Exception:  # noqa: BLE001 - missing SDK, config, etc.
        logger.debug("no pude importar BROWSER_TOOLS; uso sólo el `done` del paso")
        return [STEP_DONE_TOOL]
    return [t for t in BROWSER_TOOLS if t.get("name") != "done"] + [STEP_DONE_TOOL]


STEP_TOOLS: list[dict[str, Any]] = _step_tools()


def _step_task(step: str) -> str:
    return (
        f"Ejecutá este paso en la página actual:\n\n{step}\n\n"
        "Cuando termines, llamá a `done` con accomplished=true si el paso "
        "ocurrió, o accomplished=false y el motivo si no."
    )


class AgentDriver:
    """Adapts ClaudeBrowserAgent to the probe runner's driver interface.

    Capture and navigation-state reads go straight to the underlying
    PlaywrightDriver; only `do(step)` goes through the LLM loop.
    """

    def __init__(
        self,
        agent: Any,
        driver: Any,
        *,
        max_iterations_per_step: int = DEFAULT_MAX_ITERATIONS_PER_STEP,
        cost_cap_usd_per_step: float = DEFAULT_COST_CAP_USD_PER_STEP,
    ) -> None:
        self._agent = agent
        self._driver = driver
        self.max_iterations_per_step = max_iterations_per_step
        self.cost_cap_usd_per_step = cost_cap_usd_per_step
        # Tighten the agent's whole-task budgets down to per-step budgets. Done
        # here rather than at construction so the caller cannot forget.
        agent.max_iterations = max_iterations_per_step
        agent.cost_cap_usd = cost_cap_usd_per_step
        # Swap in the per-step `done` (the one with `accomplished`) so the model
        # can distinguish "I did it" from "I could not", which the whole-task
        # `done` cannot express.
        agent.tools = list(STEP_TOOLS)
        # THE BILL. Every step computed its cost and dropped it: nothing
        # logged, nothing persisted, so "what did this run cost" had no answer
        # anywhere in the product -- and a caching change could not be measured
        # because no run had ever recorded a number to compare against.
        self.spend_usd = 0.0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.steps_run = 0

    # --- delegated straight to the real browser ---------------------------

    async def assert_session_alive(self) -> None:
        await self._driver.assert_session_alive()

    async def is_logged_out(self) -> bool:
        """Forwarded, not reimplemented. run_probe's second, independent refusal
        reads this off whatever driver it was handed; an adapter that dropped it
        would make that gate inert for every prose probe -- which is all of them.
        """
        predicate = getattr(self._driver, "is_logged_out", None)
        return bool(await predicate()) if predicate is not None else False

    @property
    def last_read_truncated(self) -> bool:
        """The inner browser is what truncates, so it is what knows."""
        return getattr(self._driver, "last_read_truncated", False)

    @property
    def last_read_full_chars(self) -> int:
        return getattr(self._driver, "last_read_full_chars", 0)

    async def screenshot(self, label: str = "snap", *, full_page: bool = True):
        # Same parameter NAMES as PlaywrightDriver, not just the same arity.
        # This took `name` while the real driver takes `label` and a keyword
        # `full_page`, so any caller using either by keyword would break on the
        # adapter and only on the adapter -- and the runner is only ever handed
        # the adapter.
        return await self._driver.screenshot(label, full_page=full_page)

    async def read_page(self, max_chars: int | None = 8000) -> str:
        # Forwarded WITH the cap. The adapter stands in for the browser
        # everywhere the runner is concerned, so a parameter it silently drops
        # is a parameter the runner cannot use: `max_chars=None` (capture the
        # whole page for the evidence record) died here with an unexpected
        # keyword argument on the first live run after it was added.
        return await self._driver.read_page(max_chars=max_chars)

    async def url(self) -> str:
        return await self._driver.url()

    # --- the one method that needs the LLM --------------------------------

    async def do(self, step: str) -> None:
        """Let the agent accomplish one prose step. Raises if it could not.

        Two distinct ways a step fails, and BOTH raise, because run_probe must
        not capture evidence for a step that did not happen:

        * `blocked`  -- the loop died on a cap or an API error;
        * `accomplished is not True` -- the agent itself reported that the step
          did not happen (or ended without ever saying it did). This is the
          common case in practice and used to pass silently.
        """
        logger.info("agente ejecutando paso: %s", step)
        result = await self._agent.run(system=STEP_SYSTEM, user_task=_step_task(step))

        step_cost = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
        self.spend_usd += step_cost
        self.cache_read_tokens += int(getattr(result, "cache_read_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(result, "cache_write_tokens", 0) or 0)
        self.steps_run += 1
        logger.info(
            "paso %d: %.4f USD (acumulado %.4f) | tokens frescos %s, "
            "cache leidos %s, cache escritos %s, %s turnos",
            self.steps_run, step_cost, self.spend_usd,
            getattr(result, "input_tokens", "?"),
            getattr(result, "cache_read_tokens", 0),
            getattr(result, "cache_write_tokens", 0),
            getattr(result, "turns", "?"),
        )

        if getattr(result, "blocked", False):
            reason = getattr(result, "block_reason", None) or "sin motivo informado"
            raise self._not_done(step, reason)

        accomplished = getattr(result, "accomplished", True)
        if accomplished is not True:
            reason = (
                getattr(result, "done_reason", None)
                or getattr(result, "summary", None)
                or (
                    "el agente terminó sin declarar que el paso se hubiera hecho"
                    if accomplished is None
                    else "sin motivo informado"
                )
            )
            raise self._not_done(step, reason)

        logger.debug("paso completado en %s turnos", getattr(result, "turns", "?"))

    @staticmethod
    def _not_done(step: str, reason: str) -> EvalkitError:
        return EvalkitError(
            f"el agente no pudo completar el paso: {step!r} — {reason}",
            hint=(
                "(un paso no ejecutado deja la página en otro estado y la "
                "captura siguiente sería evidencia falsa, así que no se "
                "guarda evidencia de este paso)"
            ),
        )


def driver_kind_for(steps: list[str]) -> str:
    """'dsl' only if EVERY step parses as a DSL verb, else 'agent'.

    Mixed probes go to the agent: a prose step handed to the DSL driver would
    raise, and we would rather run the whole probe one way.
    """
    if not steps:
        return "agent"
    for step in steps:
        verb = (step or "").strip().split(" ", 1)[0].lower()
        if verb not in DSL_VERBS:
            return "agent"
    return "dsl"


def make_driver(kind: str, *, driver: Any, agent: Any | None = None, **kwargs: Any) -> Any:
    """Pick the driver `run_probe` will use.

    'dsl'   -> the PlaywrightDriver itself (deterministic, no API calls)
    'agent' -> an AgentDriver wrapping it (prose steps, LLM in the loop)
    """
    if kind == "dsl":
        return driver
    if kind == "agent":
        if agent is None:
            raise EvalkitError(
                "make_driver('agent') necesita un ClaudeBrowserAgent — "
                "pasá agent=... o usá kind='dsl'"
            )
        return AgentDriver(agent, driver, **kwargs)
    raise EvalkitError(
        f"tipo de driver desconocido: {kind!r}", hint="(usá 'agent' o 'dsl')"
    )
