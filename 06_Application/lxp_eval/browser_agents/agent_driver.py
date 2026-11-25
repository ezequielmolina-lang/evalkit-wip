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
    "conclusión falsa sobre la plataforma.\n\n"
    # Agregado tras el 2026-11-25: un paso decía "abrir la plataforma" sin
    # dirección y el agente inventó una. El navegador ya se niega a salir (ver
    # playwright_driver.host_allowed); esto evita gastar turnos en el intento.
    "Trabajás SÓLO dentro de la plataforma bajo evaluación. Nunca inventes una "
    "dirección: si el paso pide abrir algo y no dice dónde, usá la dirección de "
    "la plataforma que figura en la tarea o lo que ya está en pantalla; si no "
    "alcanza, terminá con accomplished=false. No uses buscadores ni sitios de "
    "terceros. Si aparece una verificación de humano (captcha, 'no soy un "
    "robot', control de seguridad), no intentes resolverla: terminá con "
    "accomplished=false y decí que esa verificación lo impidió."
)


#: Topes de UNA exploración del recorrido. Mucho más que un paso (20 turnos, 2
#: USD) porque explorar ES recorrer: entrar, abrir secciones, responder una
#: actividad, volver. Son 40 turnos y no 30: en vivo (2026-11-25), 30 alcanzaron
#: para ver el camino del error y el del acierto de una actividad y no para
#: informarlos. Siguen siendo un freno, no un presupuesto.
DEFAULT_MAX_ITERATIONS_PER_EXPLORATION = int(os.getenv("LXP_RECON_MAX_ITERS", "40"))
DEFAULT_COST_CAP_USD_PER_EXPLORATION = float(os.getenv("LXP_RECON_COST_CAP", "2.00"))
#: Salida por turno durante una exploración. El informe describe cada parte de
#: la plataforma con el texto de sus botones; en vivo no entró en 2048.
EXPLORE_MAX_OUTPUT_TOKENS = int(os.getenv("LXP_RECON_MAX_OUTPUT_TOKENS", "8192"))

#: Los roles con los que el recorrido etiqueta cada parte. `otro` existe para
#: no forzar una plataforma rara a una de las cuatro cajas.
ROLES = ("estudiante", "docente", "administrador", "publico", "otro")

#: Registrar una PARTE mientras se la tiene en pantalla. La captura la toma el
#: código (ver ClaudeBrowserAgent._dispatch_tool), así que no puede existir una
#: parte sin su pantalla: es lo que el documento del plan muestra.
RECORD_PART_TOOL: dict[str, Any] = {
    "name": "record_part",
    "description": (
        "Registrá una pantalla, sección o momento de la plataforma MIENTRAS lo "
        "tenés en pantalla: se guarda con una captura tomada en este instante. "
        "Llamala cada vez que llegues a algo distinto (una sección del menú, lo "
        "que aparece después de un error, el panel de otro rol). Lo que no "
        "registres con esta herramienta no figura en el plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre": {"type": "string"},
            "rol": {
                "type": "string", "enum": list(ROLES),
                "description": "Para quién es esta pantalla.",
            },
            "entrada": {
                "type": "string",
                "description": (
                    "El texto EXACTO del ítem de menú, botón de ingreso o cambio "
                    "de rol por el que se llega a esta parte (por ejemplo "
                    "'Vista docente'). Vacío si se llega por dentro de otra parte."
                ),
            },
            "como_llegar": {
                "type": "string",
                "description": "Los toques, en orden, desde el inicio.",
            },
            "que_muestra": {"type": "string"},
            "que_permite": {
                "type": "string",
                "description": (
                    "Qué se puede hacer ahí: botones, opciones, si hay un campo "
                    "para escribir y a quién le llega lo escrito."
                ),
            },
        },
        "required": ["nombre", "rol", "como_llegar", "que_muestra"],
    },
}

#: Registrar un COMPORTAMIENTO que se averiguó haciendo, no leyendo: qué se
#: hizo, qué pasó, qué se concluye. También lleva su captura.
RECORD_BEHAVIOR_TOOL: dict[str, Any] = {
    "name": "record_behavior",
    "description": (
        "Registrá algo que averiguaste HACIENDO (no leyendo un texto de ayuda): "
        "qué hiciste, qué pasó en pantalla y qué se concluye. Se guarda con una "
        "captura de este instante, así que llamala con el resultado a la vista."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre": {
                "type": "string",
                "description": "Qué se averiguó, en pocas palabras.",
            },
            "que_hice": {"type": "string"},
            "que_observe": {
                "type": "string",
                "description": "Lo que apareció, con el texto que tenía.",
            },
            "conclusion": {
                "type": "string",
                "description": (
                    "Qué regla o comportamiento muestra. Si no alcanza para "
                    "concluir, decilo: 'no concluyente' es una respuesta válida."
                ),
            },
        },
        "required": ["nombre", "que_hice", "que_observe", "conclusion"],
    },
}

#: Con qué cierra una exploración: lo que VIO, estructurado, para que el plan de
#: pruebas se escriba con esto y no con una etiqueta ("tutor").
EXPLORE_REPORT_TOOL: dict[str, Any] = {
    "name": "describe_platform",
    "description": (
        "Llamala UNA vez, al terminar de explorar, para informar lo que viste. "
        "Describí sólo lo que estuvo en pantalla. Lo que no encontraste va en "
        "`no_encontrado`; lo que un documento dice y no pudiste ver va en "
        "`documentado_sin_verificar`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entrada": {
                "type": "string",
                "description": (
                    "Cómo se entra, desde la dirección inicial hasta poder usar "
                    "la plataforma (qué se toca, en qué orden)."
                ),
            },
            # SIN `partes`. Las partes se registran al pasar con `record_part`,
            # que es lo que les pone la captura. En la primera corrida en vivo
            # (2026-11-25) el cierre todavía las aceptaba y el explorador las
            # volvió a listar ahí: 50 partes para 30 pantallas, veinte de ellas
            # duplicados sin captura con las comillas cambiadas, y el
            # planificador apoyó pruebas en esos duplicados.
            "entradas_vistas": {
                "type": "array",
                "description": (
                    "TODOS los ítems de menú, pestañas de navegación, botones de "
                    "ingreso y cambios de rol que viste, uno por uno, los hayas "
                    "abierto o no. Sólo NAVEGACIÓN: los controles de una actividad "
                    "(opciones de respuesta, 'Siguiente', 'Enviar', 'Cerrar') no "
                    "van acá."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "texto": {
                            "type": "string",
                            "description": "El texto exacto que tiene en pantalla.",
                        },
                        "tipo": {
                            "type": "string",
                            "enum": ["menu", "rol", "ingreso", "otro"],
                        },
                        "estado": {
                            "type": "string",
                            "enum": ["recorrida", "pendiente", "no_disponible"],
                            "description": (
                                "recorrida: entraste y la registraste con "
                                "record_part. pendiente: la viste y no entraste. "
                                "no_disponible: no se puede entrar (decí por qué)."
                            ),
                        },
                        "motivo": {"type": "string"},
                    },
                    "required": ["texto", "tipo", "estado"],
                },
            },
            "no_encontrado": {
                "type": "array", "items": {"type": "string"},
                "description": "Lo que buscaste y no está, con lo que viste en su lugar.",
            },
            "documentado_sin_verificar": {
                "type": "array", "items": {"type": "string"},
                "description": "Lo que un documento afirma y no pudiste ver.",
            },
            "notas": {"type": "string"},
        },
        "required": [],
    },
}

EXPLORE_SYSTEM = (
    "Sos quien recorre una plataforma educativa ANTES de que se escriba el plan "
    "de pruebas. Tu trabajo es entenderla: qué partes tiene, cómo se llega a cada "
    "una y qué deja hacer. Otra persona va a escribir las pruebas con lo que "
    "informes, así que lo que no veas no va a existir para el plan.\n\n"
    "Usala como la usaría un estudiante. Podés abrir secciones y RESPONDER "
    "actividades de práctica, bien y mal, para ver qué pasa después de cada cosa: "
    "ahí suele estar lo importante (la corrección, las ayudas, el tutor).\n\n"
    "Límites:\n"
    "- No podés escribir texto: no tenés esa herramienta. Si aparece un campo de "
    "texto, anotá que existe, cómo se llega y a quién le llega lo escrito.\n"
    "- No cambies configuraciones, no borres nada, no cierres la sesión.\n"
    "- No salgas de la plataforma ni inventes direcciones.\n"
    "- Si aparece una verificación de humano, no intentes resolverla: anotala.\n"
    "- Describí sólo lo que estuvo en pantalla. No completes con lo que una "
    "plataforma así 'suele tener'.\n\n"
    "REGISTRÁ AL PASAR. Cada vez que llegues a una pantalla, sección o momento "
    "distinto, llamá a `record_part` con eso en pantalla: guarda la captura. "
    "Cuando averigües algo haciendo (qué pasa al errar varias veces, qué se "
    "conserva al volver), llamá a `record_behavior` con el resultado a la vista. "
    "No lo dejes para el final: lo que registraste queda aunque te quedes sin "
    "turnos; lo que sólo pensabas contar al cierre, no.\n\n"
    "Sé concreto: nombrá los botones y las opciones con el texto que tienen. "
    "Cuando termines, llamá a `describe_platform` con el inventario de entradas "
    "que viste y lo que no encontraste."
)


def explore_tools() -> list[dict[str, Any]]:
    """Las herramientas de una exploración: mira y toca, no escribe.

    Sin `type_text` por la misma razón que `read_only_tools`: se recorre una
    plataforma que puede estar en uso, y un mensaje escrito ahí es un mensaje
    real. Sin `done` ni `record_observation`: una exploración no ejecuta un paso
    ni emite un veredicto; termina informando con `describe_platform`.

    LO QUE ESTO NO GARANTIZA, dicho en vez de sugerido: tocar una opción de una
    actividad es responderla, y eso se le permite a propósito -- sin responder
    no se ve qué hace la plataforma después de un error, que en la corrida del
    2026-11-25 era exactamente donde vivía el tutor. Lo demás (no cambiar
    configuración, no borrar) se le pide en el prompt y nada más.
    """
    try:
        from .claude_browser_agent import BROWSER_TOOLS
    except Exception:  # noqa: BLE001 - missing SDK, config, etc.
        logger.debug("no pude importar BROWSER_TOOLS; exploro sólo con el informe")
        return [RECORD_PART_TOOL, RECORD_BEHAVIOR_TOOL, EXPLORE_REPORT_TOOL]
    fuera = {"type_text", "done", "record_observation"}
    return [t for t in BROWSER_TOOLS if t.get("name") not in fuera] + [
        RECORD_PART_TOOL, RECORD_BEHAVIOR_TOOL, EXPLORE_REPORT_TOOL,
    ]


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


def _step_task(step: str, platform_url: str = "") -> str:
    # La dirección va en CADA paso: un paso que dice "abrir la plataforma" o
    # "volver al inicio" tiene que poder cumplirse sin adivinar.
    #
    # PERO NO COMO INVITACIÓN. La primera redacción decía "la plataforma está en
    # <dirección>, es la única a la que podés navegar", y en la reprueba del
    # 2026-11-25 el agente abrió esa dirección al empezar CADA paso: 15 pasos,
    # 15 veces de vuelta a la pantalla de ingreso. En una plataforma que no
    # guarda el lugar en la dirección eso borra dónde estaba, y el paso "tocar
    # una opción de la pregunta en pantalla" pasó a ser "volver a entrar, volver
    # a elegir el tema y encontrar la pregunta", que no entra en sus turnos.
    # Fue la causa de casi todos los pasos cortados del piloto de ese día.
    where = (
        "Ya estás dentro de la plataforma, en la pantalla donde te dejó el paso "
        "anterior. Empezá leyendo la página actual y ejecutá el paso desde ahí. "
        "NO vuelvas a cargar la dirección de inicio: eso te saca de donde estás "
        "y el paso deja de poder hacerse. Sólo si el paso pide expresamente "
        f"abrir la plataforma o volver al inicio, la dirección es {platform_url} "
        "(ninguna otra).\n\n"
        if platform_url else ""
    )
    return (
        f"{where}Ejecutá este paso en la página actual:\n\n{step}\n\n"
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
        platform_url: str = "",
    ) -> None:
        self._agent = agent
        self._driver = driver
        #: Dónde está la plataforma. Viaja en cada paso (ver `_step_task`).
        self.platform_url = platform_url
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

    async def start_over(self) -> bool:
        """Deja la plataforma como al entrar, antes de la prueba siguiente.

        Reenviado, no reimplementado: el navegador es el que sabe abrir otro
        contexto (ver PlaywrightDriver.start_over, con el caso que lo motivó).
        Un doble de prueba o un driver sin esa operación no se toca.
        """
        restart = getattr(self._driver, "start_over", None)
        if restart is None or not self.platform_url:
            return False
        return bool(await restart(self.platform_url))

    async def new_session(self) -> bool:
        """El estudiante vuelve otro día: página nueva, MISMO contexto.

        Distinto de `start_over`, que borra lo que la plataforma guardó. Acá se
        conserva, porque la pregunta es justamente qué recuerda la plataforma
        cuando alguien vuelve (ver PlaywrightDriver.new_session).
        """
        reopen = getattr(self._driver, "new_session", None)
        if reopen is None or not self.platform_url:
            return False
        return bool(await reopen(self.platform_url))

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
        result = await self._agent.run(
            system=STEP_SYSTEM, user_task=_step_task(step, self.platform_url)
        )

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

    async def explore(
        self,
        task: str,
        *,
        max_iterations: int | None = None,
        cost_cap_usd: float | None = None,
    ) -> dict:
        """Deja que el agente RECORRA la plataforma y devuelve lo que informó.

        Distinto de `do()` en todo lo que importa: no ejecuta un paso de una
        prueba, no puede escribir, tiene más turnos, y termina con un informe
        estructurado en lugar de un sí o un no. Es lo que el recorrido usa para
        entender la plataforma antes de que nadie redacte el plan.

        Las herramientas y los topes de un PASO se devuelven siempre, pase lo
        que pase: el mismo agente ejecuta después las pruebas, y una prueba que
        corriera sin `type_text` no podría mandarle nada al tutor.

        Levanta si el agente no informó (se quedó sin turnos, pasó el tope, la
        API falló). El costo se cuenta igual: una exploración que no informó se
        pagó.
        """
        agent = self._agent
        before = (agent.tools, agent.max_iterations, agent.cost_cap_usd)
        agent.tools = explore_tools()
        # Informar es obligatorio en el último turno (ver ClaudeBrowserAgent.run).
        agent.closing_tool = EXPLORE_REPORT_TOOL["name"]
        # ...y necesita lugar para escribirlo: con los 2048 tokens de un paso,
        # el informe en vivo se cortó antes de llegar a la lista de partes.
        tokens_before = getattr(agent, "max_tokens", None)
        agent.max_tokens = EXPLORE_MAX_OUTPUT_TOKENS
        agent.max_iterations = max_iterations or DEFAULT_MAX_ITERATIONS_PER_EXPLORATION
        agent.cost_cap_usd = cost_cap_usd or DEFAULT_COST_CAP_USD_PER_EXPLORATION
        try:
            result = await agent.run(system=EXPLORE_SYSTEM, user_task=task)
        finally:
            agent.tools, agent.max_iterations, agent.cost_cap_usd = before
            agent.closing_tool = None
            if tokens_before is None:
                try:
                    del agent.max_tokens
                except AttributeError:
                    pass
            else:
                agent.max_tokens = tokens_before

        cost = float(getattr(result, "estimated_cost_usd", 0.0) or 0.0)
        self.spend_usd += cost
        self.cache_read_tokens += int(getattr(result, "cache_read_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(result, "cache_write_tokens", 0) or 0)
        logger.info(
            "exploración: %.4f USD (acumulado %.4f), %s turnos",
            cost, self.spend_usd, getattr(result, "turns", "?"),
        )

        report = getattr(result, "report", None)
        recorded_parts = [dict(p) for p in (getattr(result, "parts", None) or [])]
        recorded_behaviors = [dict(b) for b in (getattr(result, "behaviors", None) or [])]
        if not isinstance(report, dict) and (recorded_parts or recorded_behaviors):
            # LO REGISTRADO NO SE PIERDE. Antes, una exploración que se quedaba
            # sin turnos tiraba todo lo que había visto (2026-11-25: tres
            # exploraciones de 30 turnos, cero partes). Ahora cada parte se
            # guarda al pasar; si falta el cierre, el informe lo dice.
            reason = (
                getattr(result, "block_reason", None)
                or "el agente terminó sin el informe de cierre"
            )
            report = {"incompleto": str(reason)}
        if not isinstance(report, dict):
            reason = (
                getattr(result, "block_reason", None)
                or "el agente terminó sin informar lo que vio"
            )
            raise EvalkitError(
                f"la exploración de la plataforma no produjo un informe: {reason}",
                hint="(el plan se escribe sin esa parte; queda anotado en el mapa)",
            )
        # UN INFORME VACÍO ES UNA EXPLORACIÓN FALLIDA. Aceptarlo en silencio
        # deja un perfil que dice "partes: ninguna", y el plan se escribiría
        # creyendo que la plataforma no tiene nada. Que no se haya visto nada
        # es posible, pero entonces el explorador lo dice en `no_encontrado`.
        # Las partes registradas al pasar van PRIMERO: son las que tienen
        # captura. Las que el modelo sólo describió en el cierre se conservan
        # (un explorador viejo, un doble de prueba), marcadas sin captura.
        if recorded_parts or recorded_behaviors:
            # Lo registrado al pasar REEMPLAZA lo que el cierre quiera listar:
            # es lo único que tiene captura. (Un explorador viejo o un doble que
            # no registra nada conserva su lista: ver la rama de arriba.)
            report = dict(report)
            report["partes"] = recorded_parts
            report["comportamientos"] = recorded_behaviors
        if not any(
            report.get(k)
            for k in ("partes", "no_encontrado", "comportamientos", "entradas_vistas")
        ):
            raise EvalkitError(
                "la exploración de la plataforma devolvió un informe vacío: sin "
                "partes vistas y sin nada anotado como no encontrado",
                hint="(el plan se escribe sin esa parte; queda anotado en el mapa)",
            )
        return report

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
