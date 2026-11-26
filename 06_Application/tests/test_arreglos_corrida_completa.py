"""Lo que arregló la corrida completa del 2026-11-25.

120 pruebas contra una plataforma viva: 47 llegaron al final, 50 se cortaron y
23 quedaron en compuerta. Ninguno de los 50 cortes fue por algo que la
plataforma hiciera mal. Cada prueba de acá fija la causa de un grupo de cortes
(o del costo, o de un veredicto que no se sostenía) para que no vuelva.

Lo que estas pruebas NO ven: corren contra dobles. Que un agente de verdad
cierre un paso [SI APARECE] como "no apareció", o que el chequeo de veredictos
detecte una lectura equivocada, sólo se ve en vivo.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from lxp_eval.browser_agents import agent_driver as ad
from lxp_eval.evalkit import generator as gen
from lxp_eval.evalkit import inventory as inv
from lxp_eval.evalkit import planner as pl
from lxp_eval.evalkit import sampling as sm
from lxp_eval.evalkit import scoring as sc
from lxp_eval.evalkit import steps as st
from lxp_eval.evalkit.criteria import CriterionTest
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.runner import run_probe
from lxp_eval.service import serialize

from tests.test_service import env  # noqa: F401  (fixture)


# --- 1. pasos: lo que se sabe inejecutable sin mirar la plataforma -------------


def test_a_conditional_step_is_caught_before_any_judge_sees_it():
    """PT-D4-05, dos pruebas cortadas: "Si tras la actividad 2 aparece una
    pregunta de método, elegir la primera opción". La regla estaba en el prompt
    del redactor y en el del consejo, y pasó por los dos."""
    issues = st.lint_steps([
        "Tocar el subtema Monomios.",
        "Si tras la actividad 2 aparece una pregunta de método, elegir la primera opción.",
    ])
    assert len(issues) == 1 and "Paso 2" in issues[0] and "condicional" in issues[0]


def test_a_quoted_button_that_starts_with_si_is_not_a_conditional():
    assert st.lint_steps(["Tocar el botón 'Si quieres, pide ayuda'."]) == []
    assert st.lint_steps(["Tocar el botón 'Sí, hice eso'."]) == []


def test_a_looping_step_and_an_impossible_one_are_caught():
    """Ocho cortes por "responder bien hasta que..." (30 turnos agotados) y dos
    por "iniciar una grabación de pantalla" para medir una latencia."""
    issues = st.lint_steps([
        "Responder correctamente de forma seguida hasta que aparezca un ítem más avanzado.",
        "Iniciar una grabación de pantalla con marca de tiempo visible.",
    ])
    assert any("Paso 1" in x and "repite" in x for x in issues)
    assert any("Paso 2" in x and "grabar" in x for x in issues)


def test_a_reference_to_another_probe_is_not_a_loop():
    """"Repetir el estímulo de la sonda 9.1.1" cita otra prueba, y de esa cita
    depende la compuerta heredada: no se puede mandar a reescribir."""
    assert st.lint_steps(["Repetir el estimulo de la sonda 9.3.1"]) == []


def test_the_optional_mark_is_the_one_allowed_conditional():
    assert st.lint_steps([
        "Tocar una opción incorrecta.",
        f"{st.OPTIONAL_MARK} Confirmar con el tutor el error que propone.",
        "Capturar la pantalla tal como quede.",
    ]) == []


def test_a_probe_made_mostly_of_optional_steps_is_not_a_probe():
    issues = st.lint_steps([f"{st.OPTIONAL_MARK} a", f"{st.OPTIONAL_MARK} b", "c"])
    assert issues and "pasos llevan" in issues[0]


def test_what_the_drafter_does_not_fix_the_code_does():
    fixed, left = st.repair_steps([
        "Si aparece el diálogo, confirmarlo.",
        "Iniciar una grabación de pantalla.",
        "Tocar una opción.",
        "Capturar la pantalla.",
    ])
    assert fixed[0].startswith(st.OPTIONAL_MARK)
    assert len(fixed) == 3, "el paso imposible se saca"
    assert left == []


def test_marks_are_read_off_the_step_and_never_shown_as_text_to_do():
    text, optional, long = st.split_marks(f"{st.OPTIONAL_MARK} {st.LONG_MARK} Ir al contenido X")
    assert (text, optional, long) == ("Ir al contenido X", True, True)
    task = ad._step_task(f"{st.OPTIONAL_MARK} Confirmar la hipótesis", "https://p.test")
    assert "no_aparecio=true" in task and "La prueba sigue" in task
    assert "[SI APARECE] Confirmar" not in task, "la marca no viaja como parte del paso"


# --- 2. el paso opcional que no aplica no corta la prueba ----------------------


class _Agente:
    """Un agente de mentira: devuelve, por paso, lo que se le programe."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.max_iterations = 0
        self.topes: list[int] = []
        self.tools = []
        self.cost_cap_usd = 0.0

    async def run(self, *, system, user_task):
        self.topes.append(self.max_iterations)
        return self.respuestas.pop(0)


class _Navegador:
    def __init__(self, carpeta: Path):
        self.carpeta = carpeta
        self.n = 0

    async def assert_session_alive(self):
        return None

    async def is_logged_out(self):
        return False

    async def url(self):
        return "https://p.test/"

    async def screenshot(self, label="snap", *, full_page=True):
        self.n += 1
        path = self.carpeta / f"{label}_{self.n}.png"
        path.write_bytes(b"png")
        return path

    async def read_page(self, max_chars=8000):
        return "texto de la página"


def _res(**kw):
    base = dict(blocked=False, accomplished=True, done_reason="", not_present=False,
                estimated_cost_usd=0.01, cache_read_tokens=0, cache_write_tokens=0, turns=2)
    base.update(kw)
    return SimpleNamespace(**base)


def _probe(pasos):
    return serialize.probe_from_plan(
        {"probeId": "PB-X-c1", "criterionId": "X", "goal": "g", "verdictRule": "r",
         "steps": [{"n": i, "action": s, "capture": "screenshot, page_text"}
                   for i, s in enumerate(pasos, 1)],
         "capture": ["screenshot", "page_text"], "repeats": 1, "repeatJudging": 1,
         "requiresSignoff": False},
        None, {},
    )


def test_an_optional_step_that_did_not_appear_is_noted_and_the_probe_goes_on(tmp_path):
    """Veinte cortes: el paso pedía "Sí, hice eso" y la plataforma había tomado
    el camino del diálogo guiado, donde ese botón no existe."""
    agente = _Agente([
        _res(),
        _res(accomplished=False, not_present=True,
             done_reason="No hay diálogo de confirmación: Gallito abrió preguntas guiadas."),
        _res(),
    ])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), platform_url="https://p.test/")
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    probe = _probe([
        "Tocar una opción incorrecta.",
        f"{st.OPTIONAL_MARK} Confirmar con el tutor el error que propone.",
        "Capturar la pantalla tal como quede.",
    ])
    asyncio.run(run_probe(probe, driver, ledger, platform="p.test", text_dir=tmp_path / "txt"))

    notas = [r["note"] for r in ledger.read_all()]
    del_paso_2 = [n for n in notas if n.startswith("paso 2:")]
    assert del_paso_2 and all("NO APARECIÓ" in n and "preguntas guiadas" in n for n in del_paso_2)
    assert all("[SI APARECE]" not in n for n in notas)
    assert any(n.startswith("paso 3:") for n in notas), "la prueba siguió"


def test_a_plain_step_that_did_not_happen_still_cuts_the_probe(tmp_path):
    """La tolerancia es SÓLO para el paso marcado. Un paso común que no ocurrió
    sigue cortando: la captura siguiente sería evidencia falsa."""
    agente = _Agente([_res(accomplished=False, not_present=True, done_reason="no está")])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), platform_url="https://p.test/")
    with pytest.raises(EvalkitError):
        asyncio.run(driver.do("Tocar el botón Empezar."))


def test_only_a_long_step_gets_more_turns(tmp_path):
    """Subir el tope para todos dejó a quince pruebas gastar 30 turnos cada una
    buscando algo que no existía."""
    agente = _Agente([_res(), _res()])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), max_iterations_per_step=12)
    asyncio.run(driver.do("Tocar una opción."))
    asyncio.run(driver.do(f"{st.LONG_MARK} Ir al contenido X y abrir su actividad."))
    assert agente.topes == [12, 24]
    assert agente.max_iterations == 12, "el tope vuelve a su valor"


def test_a_step_that_only_looks_is_recognised_and_one_that_acts_is_not():
    """Los pasos reales de la verificación en vivo del 2026-11-25: seis pruebas
    se cortaron en uno que sólo pedía mirar, con lo que nombraba ya en pantalla."""
    miran = [
        "Guardar captura y texto de la tercera pregunta tal como aparece.",
        "Guardar captura y texto de lo que la plataforma muestra inmediatamente "
        "después de responder la tercera pregunta.",
        "Guardar captura y texto del cuarto ítem que aparezca (enunciado completo y las 4 opciones).",
        "Capturar la pantalla de la solución paso a paso, incluyendo el recuadro 'PARA RECORDAR'.",
        f"{st.OPTIONAL_MARK} Capturar el recuadro de sugerencia de repaso (por ejemplo "
        "'Parece que te conviene repasar' con el botón 'Ir a repasar') y guardar su texto.",
        "Guardar el texto completo del bloque 'PARA RECORDAR' tal como aparece en pantalla.",
    ]
    actuan = [
        "Avanzar a la siguiente pregunta (por ejemplo, 'Siguiente pregunta').",
        "En el segundo ítem, responder eligiendo la opción correcta.",
        "Guardar captura de la pregunta y luego tocar una opción incorrecta.",
        "Capturar la pantalla y avanzar a la siguiente pregunta.",
        f"{st.LONG_MARK} Ir al contenido X y abrir su primera actividad.",
        "Registrar al estudiante con el nombre Evalkit 3.",
    ]
    assert all(st.is_observation(p) for p in miran), [p for p in miran if not st.is_observation(p)]
    assert not any(st.is_observation(p) for p in actuan), [p for p in actuan if st.is_observation(p)]


def test_a_step_that_only_looks_gets_few_turns_and_is_told_not_to_count(tmp_path):
    """Con 20 turnos, un paso de mirar que dudó de "la tercera pregunta" los
    gastó todos respondiendo preguntas para llegar a una que ya tenía delante."""
    agente = _Agente([_res(), _res(), _res()])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), max_iterations_per_step=20)
    asyncio.run(driver.do("Guardar captura y texto de la tercera pregunta tal como aparece."))
    asyncio.run(driver.do(f"{st.OPTIONAL_MARK} Confirmar con el tutor el error que propone."))
    asyncio.run(driver.do("Tocar una opción."))
    assert agente.topes == [ad.OBSERVE_MAX_ITERATIONS, ad.OPTIONAL_MAX_ITERATIONS, 20]

    tarea = ad._step_task("Guardar captura y texto de la tercera pregunta tal como aparece.")
    assert "OBSERVACIÓN" in tarea and "No la verifiques contra indicadores de avance" in tarea
    assert "OBSERVACIÓN" not in ad._step_task("Tocar una opción.")


def test_an_optional_step_that_runs_out_of_turns_is_skipped_and_says_so(tmp_path):
    """Verificación en vivo del 2026-11-25: el diálogo que el paso nombraba ya
    había pasado, el agente lo buscó hasta el tope y la prueba se cortó con lo
    que el criterio preguntaba en pantalla. Ahora la prueba sigue, y la nota no
    dice "no apareció" (nadie lo comprobó): dice que no se encontró."""
    agente = _Agente([
        _res(),
        _res(blocked=True, accomplished=None, block_reason="max_iterations (8) reached", turns=8),
        _res(),
    ])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), platform_url="https://p.test/")
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    probe = _probe([
        "Tocar una opción incorrecta.",
        f"{st.OPTIONAL_MARK} Confirmar con el tutor el error que propone.",
        "Capturar la pantalla tal como quede.",
    ])
    asyncio.run(run_probe(probe, driver, ledger, platform="p.test", text_dir=tmp_path / "txt"))

    notas = [r["note"] for r in ledger.read_all()]
    del_paso_2 = [n for n in notas if n.startswith("paso 2:")]
    assert del_paso_2 and all("NO SE ENCONTRÓ, EL PASO NO SE HIZO" in n for n in del_paso_2)
    assert all("NO APARECIÓ" not in n and st.NOT_FOUND not in n for n in notas)
    assert any(n.startswith("paso 3:") for n in notas), "la prueba siguió"
    assert "[NO SE ENCONTRÓ, EL PASO NO SE HIZO]" in sc.JUDGE_SYSTEM


def test_only_the_turn_cap_skips_an_optional_step_and_a_plain_step_is_never_skipped(tmp_path):
    """Un error de la API o el tope de costo no dicen nada del paso: cortan. Y
    un paso común que agota sus turnos corta como siempre."""
    agente = _Agente([
        _res(blocked=True, accomplished=None, block_reason="APIError: 500"),
        _res(blocked=True, accomplished=None, block_reason="max_iterations (20) reached"),
    ])
    driver = ad.AgentDriver(agente, _Navegador(tmp_path), platform_url="https://p.test/")
    with pytest.raises(EvalkitError):
        asyncio.run(driver.do(f"{st.OPTIONAL_MARK} Confirmar con el tutor el error que propone."))
    with pytest.raises(EvalkitError):
        asyncio.run(driver.do("Tocar el botón Empezar."))


def test_the_drafter_is_told_a_capture_step_does_not_assume_a_shape():
    assert "sin suponer su forma" in gen.DRAFT_SYSTEM


def test_the_agent_is_told_to_stop_looking_early():
    assert "terminá YA con accomplished=false" in ad.STEP_SYSTEM
    assert "EL PASO ES UN OBJETIVO" in ad.STEP_SYSTEM


# --- 3. el muestreo sólo usa contenidos del inventario -------------------------


_ARBOL = (
    '- button "Operaciones y expresiones algebraicas 6 temas ›"\n'
    '- button "Conceptos básicos del álgebra 3 subtemas ›"\n'
    "- text: 8.º Valor numérico de una expresión algebraica ¿Cuál es el valor?\n"
)


def test_a_route_with_a_level_nobody_saw_is_dropped():
    rutas = inv.validate_inventory(
        [
            {"niveles": ["Operaciones y expresiones algebraicas", "Conceptos básicos del álgebra",
                         "Valor numérico de una expresión algebraica"]},
            {"niveles": ["Operaciones y expresiones algebraicas", "Pantalla de pregunta y feedback"]},
        ],
        _ARBOL,
    )
    assert [r["hoja"] for r in rutas] == ["Valor numérico de una expresión algebraica"]


def test_a_screen_name_is_not_a_topic_and_a_real_topic_carries_its_route():
    """Catorce cortes: el tema de la celda era "Pantalla de pregunta y feedback
    de error" o "Chat con Gallito: sonda de memoria inventada". `tema_no_visto`
    no lo frenaba porque ese texto figuraba en la descripción del recorrido."""
    inventario = inv.validate_inventory(
        [{"niveles": ["Operaciones y expresiones algebraicas", "Conceptos básicos del álgebra",
                      "Valor numérico de una expresión algebraica"]}],
        _ARBOL,
    )
    fila = sm.validate_matrix(
        [{"id": "PT-X", "sensible_al_tema": True, "motivo": "", "confirmado": True, "celdas": [
            {"tema": "valor numérico de una expresión algebraica", "variante": "un error de signo"},
            {"tema": "Chat con Gallito — sonda de memoria inventada", "variante": "pseudo-recuerdo"},
        ]}],
        ["PT-X"],
        "... Chat con Gallito — sonda de memoria inventada ...",
        inventario,
    )["PT-X"]
    buena, mala = fila["celdas"]
    assert buena["ruta"][-1] == "Valor numérico de una expresión algebraica"
    assert mala["tema"] == "" and "Chat con Gallito" in mala["tema_descartado"]
    assert any("borrados" in a for a in fila["advertencias"])


def test_a_topic_written_as_its_whole_route_is_still_that_topic():
    """Primera verificación en vivo (2026-11-25): el muestreo copió la ruta
    entera como tema y las 15 pruebas quedaron sin tema."""
    inventario = [{"ruta": ["Eje A", "Tema B", "Subtema C"], "hoja": "Subtema C"}]
    assert inv.find(inventario, "Eje A › Tema B › Subtema C")["hoja"] == "Subtema C"
    assert inv.find(inventario, "Como el programa > Eje A > Tema B > subtema c")["hoja"] == "Subtema C"
    assert inv.find(inventario, "Tema B") is None, "un nivel intermedio no es la hoja"
    assert inv.render_inventory(inventario).startswith("- Subtema C")


def test_a_variant_that_describes_a_sequence_does_not_turn_one_step_into_many():
    """Misma verificación: la variante decía "secuencia que mezcla aciertos y
    errores" y el paso "responder la actividad 1" gastó sus 20 turnos
    intentando hacer la secuencia entera."""
    inst = pl.instantiate(
        {"probeId": "PB-X", "criterionId": "X", "goal": "g", "verdictRule": "r",
         "steps": [{"n": 1, "action": "Responder de modo que ocurra esto: [VARIANTE]"}]},
        [{"tema": "", "variante": "Secuencia que mezcla aciertos y errores"}],
    )[0]
    assert "hacé una sola acción" in inst["steps"][0]["action"]


def test_the_navigation_step_gets_the_whole_route():
    """Cinco cortes: el contenido existía, bajo otro eje del que suponían los pasos."""
    inst = pl.instantiate(
        {"probeId": "PB-X", "criterionId": "X", "goal": "g", "verdictRule": "r",
         "steps": [{"n": 1, "action": f"{st.LONG_MARK} Ir al contenido [TEMA] y abrir su actividad"}]},
        [{"tema": "Congruencia de triángulos", "variante": "v",
          "ruta": ["Geometría y medidas", "Propiedades", "Congruencia de triángulos"]}],
    )[0]
    paso = inst["steps"][0]["action"]
    assert "Geometría y medidas › Propiedades › Congruencia de triángulos" in paso
    assert inst["celda"]["ruta"][0] == "Geometría y medidas"


def test_the_drafter_is_told_one_navigation_step_and_goals_not_buttons():
    guia = pl.guidance_for({"que_se_prueba": "x", "partes": [], "celdas": []})
    assert st.LONG_MARK in guia and "NO escribas un paso por nivel" in guia
    assert "OBJETIVO, no el botón" in gen.DRAFT_SYSTEM
    assert st.OPTIONAL_MARK in gen.DRAFT_SYSTEM


def test_the_planner_is_told_what_the_executor_cannot_do():
    """PT-D4-01 (latencia) fue a prueba y las dos se cortaron pidiendo una
    grabación de pantalla."""
    assert "NO mide tiempos ni latencias" in pl.STRATEGY_SYSTEM


# --- 4. el consejo reducido y el lint antes del consejo ------------------------


def test_the_reduced_council_keeps_the_risk_classifier():
    """El asiento de seguridad no vota, pero es el que alimenta la compuerta."""
    nombres = {j.name for j in gen.REDUCED_COUNCIL}
    assert nombres == {"coherencia_criterio", "seguridad"}, "un juez que vota y el clasificador"
    assert gen.CLASSIFIER_JUDGE in nombres
    assert [j.name for j in gen.REDUCED_COUNCIL if j.voting] == ["coherencia_criterio"]


class _Cliente:
    """Redacta lo que se le programe y aprueba todo, salvo que se diga otra cosa."""

    def __init__(self, borradores, objeta=()):
        self.borradores = list(borradores)
        self.objeta = set(objeta)
        self.redacciones = 0
        self.jueces: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        nombre = kw["tools"][0]["name"]
        if nombre == gen.DRAFT_TOOL["name"]:
            self.redacciones += 1
            pasos = self.borradores.pop(0) if self.borradores else ["Tocar una opción."]
            data = {"goal": "g", "steps": pasos, "verdict_rule": "CUMPLE si x; NO_CUMPLE si y.",
                    "capture": ["screenshot", "page_text"], "repeats": 1, "repeat_judging": 1}
        else:
            sistema = " ".join(b.get("text", "") for b in kw["system"])
            asiento = next(j.name for j in gen.COUNCIL if f"[JUEZ: {j.name}]" in sistema)
            self.jueces.append(asiento)
            data = {"verdict": gen.REVISE if asiento in self.objeta else gen.APPROVE,
                    "reasons": ["motivo"], "suggested_fix": None}
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=nombre, input=data)],
            stop_reason="tool_use", usage=None,
        )


def _criterios():
    from lxp_eval.evalkit.criteria import Criteria, CriterionDomain

    return Criteria(name="Ejemplo", domains=[CriterionDomain(code="D4", name="D4", points=1, tests=[
        CriterionTest(id="PT-D4-05", name="Check", points=1.0, verification="v",
                      pass_criteria="p", fail_criteria="f"),
    ])])


def test_a_bad_draft_goes_back_to_the_drafter_before_the_council_is_paid():
    cliente = _Cliente([
        ["Si aparece una pregunta de método, elegir la primera opción."],
        [f"{st.OPTIONAL_MARK} Elegir la primera opción de la pregunta de método.", "Capturar.", "Tocar."],
    ])
    resultado = gen.generate_probes(
        _criterios(), client=cliente, council=gen.REDUCED_COUNCIL, max_rounds=1,
    )
    assert cliente.redacciones == 2, "una vuelta por el lint"
    assert len(cliente.jueces) == 2, "el juez y el clasificador vieron UN borrador, el limpio"
    assert resultado.probes[0].probe.steps[0].startswith(st.OPTIONAL_MARK)


def test_the_rest_of_the_council_sits_only_when_someone_objects():
    tranquilo = _Cliente([])
    gen.generate_probes(_criterios(), client=tranquilo, council=gen.REDUCED_COUNCIL,
                        escalate_to=gen.COUNCIL, max_rounds=1)
    assert sorted(tranquilo.jueces) == ["coherencia_criterio", "seguridad"]

    discutido = _Cliente([], objeta={"coherencia_criterio"})
    gen.generate_probes(_criterios(), client=discutido, council=gen.REDUCED_COUNCIL,
                        escalate_to=gen.COUNCIL, max_rounds=1)
    assert sorted(discutido.jueces) == sorted(j.name for j in gen.COUNCIL)


# --- 5. el chequeo de los veredictos ------------------------------------------


def _chequeo(responden, choque):
    def create(**kw):
        assert kw["tools"][0]["name"] == sc.CHECK_TOOL["name"]
        return SimpleNamespace(content=[SimpleNamespace(
            type="tool_use", name=sc.CHECK_TOOL["name"],
            input={"responden_a_la_regla": responden, "mismo_hecho_distinto_veredicto": choque,
                   "motivo": "la prueba 1 juzga la navegación"},
        )], stop_reason="tool_use")
    return SimpleNamespace(messages=SimpleNamespace(create=create))


_TEST = CriterionTest(id="PT-D1-06", name="NO default discovery", points=1.5,
                      verification="Nuevo concepto: ¿explica o espera descubrimiento?",
                      pass_criteria="Explicación directa", fail_criteria="Discovery default")


def test_a_verdict_that_judges_something_else_is_not_ok():
    """PT-D1-06: el juez leyó "descubrimiento" como navegación automática."""
    r = sc.check_consistency(_TEST, [("Monomios", "CUMPLE", "navegación explícita")],
                             _chequeo(False, False), model="m")
    assert r["ok"] is False and "navegación" in r["motivo"]


def test_the_same_fact_with_two_verdicts_is_not_ok():
    """PT-D4-03: en tres pruebas se vio lo mismo y el juez lo calificó distinto."""
    assert sc.check_consistency(_TEST, [("a", "CUMPLE", "x"), ("b", "NO_CUMPLE", "x")],
                                _chequeo(True, True), model="m")["ok"] is False


def test_different_verdicts_over_different_facts_are_fine():
    assert sc.check_consistency(_TEST, [("a", "CUMPLE", "x"), ("b", "NO_CUMPLE", "y")],
                                _chequeo(True, False), model="m")["ok"] is True


def test_the_judge_is_told_to_read_the_rule_literally():
    assert "LEÉ LA REGLA LITERALMENTE" in sc.JUDGE_SYSTEM
    assert "NO APARECIÓ" in sc.JUDGE_SYSTEM


# --- 6. cada prueba arranca con su propia preparación --------------------------


def test_every_probe_runs_its_preamble_first_and_a_broken_one_cuts_that_probe(tmp_path, env):  # noqa: F811
    """Las 120 pruebas del 2026-11-25 usaron la misma cuenta de estudiante: al
    final la vista docente le contaba 92 respuestas, y las pruebas tardías
    encontraron ejercicios y diálogos que sus pasos no suponían."""
    from tests.test_plan_ejecucion import _desde_plan, _plan_listo

    store, engine, job_id = _plan_listo(tmp_path)
    resultado = _desde_plan(
        store, engine, job_id,
        preamble=["[SI APARECE] Tocar 'Cambiar estudiante'.", "Escribir el nombre: Evalkit {n}"],
    )
    registro = store.get(job_id)
    assert resultado.stage == "done", registro.error
    pasos = engine.test_drivers[-1].steps
    nombres = [p for p in pasos if p.startswith("Escribir el nombre: Evalkit ")]
    # Las pruebas con pasos idénticos se ejecutan una sola vez (comparten la
    # captura), así que la preparación va una vez por ejecución.
    ejecuciones = {
        tuple(x["action"] for x in p["steps"])
        for p in registro.probes if not p["requiresSignoff"]
    }
    assert len(nombres) == len(ejecuciones) > 1, "una preparación por ejecución"
    assert len(set(nombres)) == len(nombres), "con un nombre distinto cada vez"
    assert all("{n}" not in p for p in pasos)
    assert registro.spend_by_stage.keys() >= {"redaccion", "juicio"}


def test_the_manifest_carries_the_preamble(tmp_path):
    from lxp_eval.service.batch import load_manifest

    ruta = tmp_path / "lote.yaml"
    ruta.write_text(
        "- platform_url: https://plataforma.test\n"
        "  objective: Un tutor de matemática\n"
        "  antes_de_cada_prueba:\n"
        "    - Tocar Cambiar estudiante\n"
        "    - 'Escribir el nombre: Evalkit {n}'\n",
        encoding="utf-8",
    )
    entrada = load_manifest(ruta).entries[0]
    assert entrada.antes_de_cada_prueba == ["Tocar Cambiar estudiante", "Escribir el nombre: Evalkit {n}"]
