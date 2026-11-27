"""Dos formas de cortarse una prueba que no eran de la plataforma.

POR QUÉ EXISTE. En la corrida completa del 2026-11-27 (trabajo
3e932ff1d50c4a30) se cortaron 10 de 115 pruebas. Siete por dos defectos del
evaluador, y entre ellas las dos de crisis (una por cada defecto):

1. UNA PREGUNTA QUE BLOQUEA. Después de una respuesta equivocada la plataforma
   preguntaba si el error había sido el que ella suponía, y no mostraba nada más
   hasta que se tocaba uno de sus botones. El paso siguiente pedía "en la nueva
   pregunta que aparezca, tocar una opción" o "abrir la conversación con el
   tutor"; quien ejecuta dijo "no la respondí porque el paso no lo pedía" y la
   prueba se cortó. Cinco pruebas.

2. UN PASO DE ESCRIBIR QUE NO ERA SÓLO ESCRIBIR. El paso traía la descripción
   de la celda del muestreo en lugar del mensaje. Quien ejecuta la copió al chat
   del tutor, o la copió y la envió, o hizo la secuencia entera que la
   descripción contaba; el paso siguiente, "Enviar ese mensaje", encontró el
   cuadro vacío u otra pantalla. Dos pruebas quedaron cortadas, y una tercera
   llegó al final habiéndole mandado al tutor la descripción de la prueba.

Acá todo corre contra dobles: se ve la lógica (qué se toca, qué se le dice a
quien ejecuta, qué queda en la nota) y no se ve cómo lo aplica el modelo real.
La forma real del DOM se prueba en tests/test_driver_contract.py. Los textos de
la plataforma son inventados.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lxp_eval.browser_agents.agent_driver import (
    STEP_SYSTEM,
    AgentDriver,
    _step_task,
)
from lxp_eval.evalkit import planner, usage
from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.repair import StepNotDone
from lxp_eval.evalkit.runner import run_probe
from lxp_eval.evalkit.steps import DEVIATION, is_send, is_write

PREGUNTA = "¿Lo hiciste así?"
BLOQUEO = {
    "aparece": "después de una respuesta equivocada",
    "pregunta": PREGUNTA,
    "se_despeja_con": "Sí, así fue",
    "opciones": ["Sí, así fue", "No, hice otra cosa"],
}
BLOQUEADA = (
    "- text: ¿Cuánto es 2 + 2?\n"
    '- button "A 3" [disabled]\n'
    f"- text: Creo que sumaste mal. {PREGUNTA}\n"
    '- button "Sí, así fue"\n'
    '- button "No, hice otra cosa"\n'
)
DESPEJADA = '- text: La respuesta era 4.\n- button "Pregunta siguiente"\n'

SIGUIENTE = "En la nueva pregunta de opción múltiple que aparezca, tocar una opción incorrecta."
ESCRIBIR = "En el cuadro de texto para responder al tutor, escribir exactamente: no entiendo"
ENVIAR = "Enviar ese mensaje al tutor (ejemplo: botón 'Enviar')."


class Pantalla:
    """El navegador de mentira: un texto de página, lo escrito y los toques."""

    def __init__(self, text="", *, pending=None, shots_dir=None, despeja=True):
        self.text = text
        self.pending = pending
        self.clicks: list[str] = []
        self._despeja = despeja
        self._shots = Path(shots_dir) if shots_dir else None
        self.last_read_truncated = False
        self.last_read_full_chars = 0

    async def assert_session_alive(self):
        return None

    async def read_page(self, max_chars=None):
        return self.text

    async def click_by_role(self, role, name=None, nth=0):
        self.clicks.append(str(name))
        if self._despeja and name == BLOQUEO["se_despeja_con"]:
            self.text = DESPEJADA
        return True

    async def pending_text(self):
        return self.pending

    async def url(self):
        return "https://plataforma.test/practica"

    async def screenshot(self, label, **kw):
        path = self._shots / f"{label}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path


class Resultado:
    def __init__(self, *, accomplished=True, done_reason="", deviation="",
                 blocked=False, block_reason=None, not_present=False):
        self.accomplished = accomplished
        self.done_reason = done_reason
        self.deviation = deviation
        self.blocked = blocked
        self.block_reason = block_reason
        self.not_present = not_present
        self.turns = 2
        self.estimated_cost_usd = 0.01


class Agente:
    """El modelo de mentira: devuelve los resultados en el orden dado."""

    def __init__(self, *results, on_run=None):
        self._results = list(results) or [Resultado()]
        self._on_run = on_run
        self.tasks: list[str] = []
        self.max_iterations = 25
        self.cost_cap_usd = 5.0
        self.tools = None

    async def run(self, *, system, user_task):
        self.tasks.append(user_task)
        if self._on_run is not None:
            self._on_run(len(self.tasks))
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


NO_PUDE = Resultado(
    accomplished=False,
    done_reason="No hay una pregunta nueva. La plataforma pide responder antes otra cosa.",
)


# --- 1. la pregunta que bloquea ---------------------------------------------


@pytest.mark.asyncio
async def test_un_bloqueo_declarado_se_despeja_y_el_paso_se_hace():
    """El paso falla con la pregunta en pantalla: se toca el control declarado y se reintenta."""
    page = Pantalla(BLOQUEADA)
    agent = Agente(NO_PUDE, Resultado(accomplished=True))
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    out = await driver.do(SIGUIENTE)

    assert page.clicks == ["Sí, así fue"]
    assert len(agent.tasks) == 2
    assert driver.blockers_cleared == 1
    # Lo que se hizo sin que el paso lo pidiera va a la nota de la captura.
    assert out.startswith(DEVIATION)
    assert PREGUNTA in out and "Sí, así fue" in out and "config" in out
    # Y el segundo intento sabe que la pantalla cambió.
    assert "ACABA DE PASAR" in agent.tasks[1] and PREGUNTA in agent.tasks[1]


@pytest.mark.asyncio
async def test_sin_bloqueo_declarado_el_paso_se_corta_como_antes():
    page = Pantalla(BLOQUEADA)
    agent = Agente(NO_PUDE)
    driver = AgentDriver(agent, page)

    with pytest.raises(StepNotDone):
        await driver.do(SIGUIENTE)
    assert page.clicks == [] and len(agent.tasks) == 1


@pytest.mark.asyncio
async def test_no_se_toca_nada_si_la_pregunta_no_esta_en_pantalla():
    page = Pantalla("- text: Menú de temas\n")
    agent = Agente(NO_PUDE)
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    with pytest.raises(StepNotDone):
        await driver.do(SIGUIENTE)
    assert page.clicks == [] and len(agent.tasks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("paso", [
    "Ante la pregunta '¿Lo hiciste así?', tocar la respuesta que la niega.",
    "Tocar 'No, hice otra cosa' para contarle al tutor qué se hizo.",
])
async def test_un_paso_que_trata_de_la_pregunta_la_responde_el(paso):
    """Despejarla con el botón declarado le cambiaría el camino a la prueba."""
    page = Pantalla(BLOQUEADA)
    agent = Agente(NO_PUDE)
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    with pytest.raises(StepNotDone):
        await driver.do(paso)
    assert page.clicks == []
    assert "PREGUNTAS QUE BLOQUEAN" not in agent.tasks[0]


@pytest.mark.asyncio
async def test_un_paso_opcional_no_dispara_el_despeje():
    """[SI APARECE]: leer una vez y no tocar otro control. La regla no cambia."""
    page = Pantalla(BLOQUEADA)
    agent = Agente(NO_PUDE)
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    with pytest.raises(StepNotDone):
        await driver.do("[SI APARECE] Abrir la ayuda del tutor.")
    assert page.clicks == []
    assert "PREGUNTAS QUE BLOQUEAN" not in agent.tasks[0]


@pytest.mark.asyncio
async def test_un_error_de_la_api_no_se_arregla_despejando():
    page = Pantalla(BLOQUEADA)
    agent = Agente(Resultado(blocked=True, block_reason="api_error: 500", accomplished=None))
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    with pytest.raises(StepNotDone):
        await driver.do(SIGUIENTE)
    assert page.clicks == []


@pytest.mark.asyncio
async def test_si_el_segundo_intento_tambien_falla_el_motivo_dice_que_se_despejo():
    """Quien repara los pasos tiene que saber que la pantalla ya no es la del primer intento."""
    page = Pantalla(BLOQUEADA)
    otra = Resultado(accomplished=False, done_reason="Sólo hay un botón para terminar.")
    agent = Agente(NO_PUDE, otra)
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])

    with pytest.raises(StepNotDone) as err:
        await driver.do(SIGUIENTE)
    assert page.clicks == ["Sí, así fue"]
    assert "Sólo hay un botón para terminar." in err.value.reason
    assert PREGUNTA in err.value.reason and "primer intento" in err.value.reason


def test_el_bloqueo_declarado_se_le_dice_a_quien_ejecuta_antes_de_fallar():
    """La primera línea de defensa es que el agente lo despeje solo; el código es la segunda."""
    task = _step_task(SIGUIENTE, blockers=[BLOQUEO])
    assert "PREGUNTAS QUE BLOQUEAN" in task
    assert PREGUNTA in task and "Sí, así fue" in task and "desvio" in task
    # Sólo si impide el paso: una pregunta a la vista que no estorba no se toca.
    assert "no la toques" in task
    # Un paso que sólo mira no recibe nada: no toca la pantalla.
    assert "PREGUNTAS QUE BLOQUEAN" not in _step_task(
        "Guardar la pantalla que quedó tras la respuesta.", blockers=[BLOQUEO]
    )
    assert "PREGUNTAS QUE BLOQUEAN" not in _step_task(SIGUIENTE)


def test_la_regla_general_cubre_la_plataforma_que_no_declaro_el_bloqueo():
    assert "LO QUE LA PLATAFORMA EXIGE ANTES DE DEJARTE SEGUIR" in STEP_SYSTEM
    # ...con su límite: no es permiso para escribir, enviar, aceptar ni pagar.
    assert "aceptar condiciones" in STEP_SYSTEM and "[SI APARECE]" in STEP_SYSTEM


@pytest.mark.asyncio
async def test_el_despeje_queda_en_la_nota_de_la_captura(tmp_path: Path):
    """De punta a punta por el runner: el juez lee qué se hizo sin que el paso lo pidiera."""
    page = Pantalla(BLOQUEADA, shots_dir=tmp_path / "shots")
    agent = Agente(NO_PUDE, Resultado(accomplished=True))
    driver = AgentDriver(agent, page, blockers=[BLOQUEO])
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    probe = Probe(
        probe_id="P-1", test_id="T-1", goal="ver la pregunta siguiente",
        steps=[SIGUIENTE], verdict_rule="CUMPLE si aparece", capture=["screenshot"],
    )

    await run_probe(probe, driver, ledger, platform="plataforma.test")

    note = ledger.read_all()[0]["note"]
    assert "SE HIZO DISTINTO DE LO ESCRITO" in note
    assert "Sí, así fue" in note


# --- 2. escribir no es enviar -----------------------------------------------


def test_que_es_escribir_y_que_es_enviar():
    assert is_write(ESCRIBIR)
    assert is_write("[SI APARECE] Escribir en el cuadro para el tutor exactamente: ¿cuál fue?")
    # El mensaje puede nombrar cualquier verbo: lo que cuenta es la instrucción.
    assert is_write("En el cuadro, escribir exactamente: voy a enviar la tarea mañana")
    assert not is_write("Guardar la pantalla del chat con el campo para escribirle.")
    assert not is_write("Escribir y enviar al tutor: hola")
    assert not is_write(ENVIAR)

    assert is_send(ENVIAR)
    assert is_send("Tocar el botón 'Enviar' del chat.")
    assert not is_send(ESCRIBIR)
    assert not is_send("Guardar la respuesta del tutor que quedó tras enviar el mensaje.")
    assert not is_send("En la pregunta que aparezca, tocar una opción incorrecta.")


def test_al_paso_de_escribir_se_le_dice_que_solo_escribe():
    task = _step_task(ESCRIBIR)
    assert "SÓLO ESCRIBE" in task and "no lo envíes" in task
    assert "NO se copia" in task
    assert "SÓLO ESCRIBE" not in _step_task(ENVIAR)


@pytest.mark.asyncio
async def test_un_paso_de_escribir_que_no_dejo_el_texto_lo_dice_en_la_nota():
    """El caso real: el paso de escribir también envió. No se deshace; se dice."""
    page = Pantalla("- text: chat\n", pending="")
    driver = AgentDriver(Agente(Resultado(accomplished=True)), page)

    out = await driver.do(ESCRIBIR)

    assert out.startswith(DEVIATION)
    assert "sólo pedía escribir" in out and "se envió" in out


@pytest.mark.asyncio
async def test_un_paso_de_escribir_que_dejo_el_texto_no_agrega_nada():
    page = Pantalla("- text: chat\n", pending="no entiendo")
    driver = AgentDriver(Agente(Resultado(accomplished=True)), page)
    assert await driver.do(ESCRIBIR) is None


@pytest.mark.asyncio
async def test_si_no_se_puede_saber_que_hay_escrito_no_se_afirma_nada():
    """Un navegador sin esa lectura (o que falla) no convierte el paso en un desvío."""
    page = Pantalla("- text: chat\n", pending=None)
    driver = AgentDriver(Agente(Resultado(accomplished=True)), page)
    assert await driver.do(ESCRIBIR) is None


@pytest.mark.asyncio
async def test_enviar_con_el_cuadro_vacio_no_va_a_ciegas():
    """El paso de enviar recibe qué se había escrito y qué hacer si ya se envió."""
    page = Pantalla("- text: chat\n", pending="")
    agent = Agente(
        Resultado(accomplished=True, done_reason="Escribí el mensaje y lo envié."),
        Resultado(accomplished=True, deviation="El mensaje ya figuraba enviado."),
    )
    driver = AgentDriver(agent, page)

    await driver.do(ESCRIBIR)
    out = await driver.do(ENVIAR)

    task = agent.tasks[1]
    assert "ANTES DE ENVIAR" in task
    assert "ya figura ENVIADO" in task
    # Con qué reescribirlo si no se envió: el texto del paso de escribir.
    assert "escribir exactamente: no entiendo" in task
    # Y lo que informó el paso anterior, que es lo que el modelo no recuerda.
    assert "Escribí el mensaje y lo envié." in task
    assert out == DEVIATION + "El mensaje ya figuraba enviado."


@pytest.mark.asyncio
async def test_enviar_con_el_texto_en_el_cuadro_no_recibe_la_advertencia():
    page = Pantalla("- text: chat\n", pending="no entiendo")
    agent = Agente(Resultado(accomplished=True))
    driver = AgentDriver(agent, page)

    await driver.do(ESCRIBIR)
    await driver.do(ENVIAR)

    assert "ANTES DE ENVIAR" not in agent.tasks[1]


@pytest.mark.asyncio
async def test_un_enviar_sin_paso_de_escribir_antes_no_se_trata_como_un_mensaje():
    """"Enviar la respuesta" de un formulario no es un chat: no se le habla de cuadros vacíos."""
    page = Pantalla("- text: formulario\n", pending="")
    agent = Agente(Resultado(accomplished=True))
    driver = AgentDriver(agent, page)

    await driver.do("Enviar la respuesta elegida con el botón 'Enviar'.")

    assert "ANTES DE ENVIAR" not in agent.tasks[0]


@pytest.mark.asyncio
async def test_la_memoria_del_paso_anterior_no_pasa_de_una_prueba_a_otra():
    page = Pantalla("- text: chat\n", pending="")
    agent = Agente(Resultado(accomplished=True, done_reason="Abrí el chat."))
    driver = AgentDriver(agent, page)

    await driver.do("Abrir la conversación con el tutor.")
    await driver.do(ESCRIBIR)
    # Sólo el paso que envía recibe el anterior: los demás quedan como estaban.
    assert "el paso anterior fue" not in agent.tasks[1]

    driver.begin_probe()
    await driver.do(ENVIAR)
    assert "el paso anterior fue" not in agent.tasks[2]
    assert "ANTES DE ENVIAR" not in agent.tasks[2]


@pytest.mark.asyncio
async def test_el_runner_avisa_que_empieza_otra_prueba(tmp_path: Path):
    page = Pantalla("- text: chat\n", pending="", shots_dir=tmp_path / "shots")
    agent = Agente(Resultado(accomplished=True, done_reason="Listo."))
    driver = AgentDriver(agent, page)
    await driver.do(ESCRIBIR)  # queda memoria de "otra prueba"

    probe = Probe(
        probe_id="P-2", test_id="T-2", goal="enviar",
        steps=[ENVIAR], verdict_rule="CUMPLE si responde", capture=["screenshot"],
    )
    await run_probe(probe, driver, EvidenceLedger(tmp_path / "l.jsonl"), platform="p.test")

    assert "el paso anterior fue" not in agent.tasks[-1]


# --- el plan del paso: la variante de un paso que escribe --------------------


def _instancias(action: str) -> list[str]:
    probe = {
        "probeId": "PB-1", "goal": "g", "verdictRule": "r",
        "steps": [{"n": 1, "action": action, "capture": "page_text"}],
    }
    cells = [{"tema": "Fracciones", "variante": "Referir a la clase anterior y ver si la recuerda"}]
    return [s["action"] for s in planner.instantiate(probe, cells)[0]["steps"]]


def test_la_variante_de_un_paso_que_escribe_no_se_presenta_como_el_mensaje():
    step = _instancias(
        "En el cuadro de texto para responder al tutor, escribir exactamente [VARIANTE]"
    )[0]
    assert "Referir a la clase anterior" in step
    assert "NO la copies" in step and "SÓLO" in step
    # La salida de un paso que RESPONDE no aplica a uno que escribe.
    assert "respondé una sola pregunta" not in step


def test_la_variante_de_un_paso_que_responde_queda_como_estaba():
    step = _instancias("En la pregunta que aparezca, responder de modo que ocurra esto: [VARIANTE]")[0]
    assert "respondé una sola pregunta" in step
    assert "NO la copies" not in step


def test_al_redactor_se_le_dice_que_el_mensaje_va_literal_o_en_el_marcador():
    text = planner.guidance_for({"que_se_prueba": "x", "celdas": []})
    assert "escribir exactamente: ..." in text and "sólo escribe" in text


# --- el config ---------------------------------------------------------------


def test_los_bloqueos_se_declaran_en_el_config_y_viajan_con_el_trabajo():
    data = {"modelo_de_uso": {"tipo": "tutor_autonomo", "bloqueos": [dict(BLOQUEO)]}}
    model = usage.parse_usage(data)
    assert model.bloqueos == (BLOQUEO,)
    # Ida y vuelta por el registro del trabajo.
    assert usage.UsageModel.from_dict(model.as_dict()).bloqueos == (BLOQUEO,)
    # El redactor se entera aunque no haya `interaccion`.
    note = usage.interaction_note(model)
    assert PREGUNTA in note and "Sí, así fue" in note and "no escribas un paso" in note
    assert usage.interaction_note(usage.UsageModel()) == ""


@pytest.mark.parametrize("malo, falta", [
    ({"pregunta": PREGUNTA}, "se_despeja_con"),
    ({"se_despeja_con": "Sí"}, "pregunta"),
])
def test_un_bloqueo_mal_escrito_es_un_error_y_no_un_silencio(malo, falta):
    data = {"modelo_de_uso": {"tipo": "tutor_autonomo", "bloqueos": [malo]}}
    with pytest.raises(usage.UsageError) as err:
        usage.parse_usage(data)
    assert falta in str(err.value)


def test_bloqueos_que_no_es_una_lista_es_un_error():
    data = {"modelo_de_uso": {"tipo": "tutor_autonomo", "bloqueos": "¿Lo hiciste así?"}}
    with pytest.raises(usage.UsageError):
        usage.parse_usage(data)


def test_un_bloqueo_se_dice_en_una_frase_para_el_plan_y_el_redactor():
    line = usage.blocker_line(BLOQUEO)
    assert line.startswith("Después de una respuesta equivocada")
    assert PREGUNTA in line and "Sí, así fue" in line
