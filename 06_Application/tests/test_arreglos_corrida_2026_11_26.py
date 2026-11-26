"""Los arreglos que dejó la corrida completa del 2026-11-26 (trabajo 91b35f78a25749c9).

Esa corrida costó USD 24,86 y dejó veredicto en 15 de 44 criterios probados.
De los 29 sin veredicto, 16 eran fallas del evaluador y no hallazgos de la
plataforma: 9 por pruebas cortadas, 5 por veredictos que el chequeo de
coherencia no dejó pasar, 2 por evidencia insuficiente; y otros 7 criterios no
se probaron porque el recorrido no había llegado a su pantalla. Cada grupo de
pruebas de este archivo fija el arreglo de una de esas causas, con el caso.

Todo corre contra dobles. Lo que estas pruebas NO ven es si el modelo de
verdad sigue las reglas nuevas del ejecutor, ni si la plataforma de verdad
termina de responder en el plazo de espera: eso se mira con una corrida.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from lxp_eval.browser_agents import playwright_driver as pw
from lxp_eval.browser_agents.agent_driver import AgentDriver, STEP_DONE_TOOL, STEP_SYSTEM
from lxp_eval.evalkit import planner as pl
from lxp_eval.evalkit import recon
from lxp_eval.evalkit import repair
from lxp_eval.evalkit import scoring
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.generator import load_risk_markers
from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.runner import _note, run_probe
from lxp_eval.evalkit.steps import (
    DEVIATION,
    DIRECT,
    OPTIONAL_MARK,
    is_observation,
    lint_steps,
    repair_steps,
    split_marks,
)
from lxp_eval.service.batch import ManifestEntry, run_batch, run_from_plan
from lxp_eval.service.jobs import _attempt_rows
from lxp_eval.service.store import JobStore
from lxp_eval.service.watchdog import Stage

from tests.test_plan_ejecucion import APROBO, _cliente
from tests.test_plan_servicio import PNG, _muestreo
from tests.test_service import FakeDriver, env, make_engine  # noqa: F401 -- fixture


# --- 1. un paso que dice "guardar" es mirar ----------------------------------


def test_guardar_is_looking_whatever_it_names():
    """Ocho pruebas cortadas: el modelo buscó un botón de guardar."""
    for step in (
        "Guardar la solución paso a paso que aparece, incluyendo cuál paso queda resaltado.",
        "Guardar la tabla que indica si queda lo que se corrigió (por ejemplo '¿Queda?').",
        "Guardar la zona de la solución donde figura el paso que falló.",
        "Guardar lo que Gallito muestra tras ese error: la hipótesis y la pregunta.",
        "Capturar la respuesta del tutor.",
        "Registrar la aparición del check '¿cómo lo resolviste?'.",
    ):
        assert is_observation(step), step


def test_saving_something_of_the_platform_is_still_an_action():
    for step in (
        "Guardar los cambios del perfil.",
        "Guardar la configuración de la clase.",
        "Guardar la pantalla y luego tocar 'Siguiente pregunta'.",
        "Registrar un estudiante nuevo.",
        "En la pregunta que aparezca, elegir una opción incorrecta.",
    ):
        assert not is_observation(step), step


# --- 2. una marca vale donde el redactor la puso -----------------------------


def test_a_mark_behind_a_label_or_at_the_end_still_counts():
    """PT-D14-04: '[VARIANTE: ...] [SI APARECE] Responder...' se trató como obligatorio."""
    text, optional, long = split_marks(
        "[VARIANTE: sólo celda estudiante] [SI APARECE] Responder la pregunta de reflexión."
    )
    assert optional and not long
    assert text == "Responder la pregunta de reflexión."

    text, optional, long = split_marks(
        "[VARIANTE: sólo celda estudiante] Ir al contenido y abrir su actividad. ([VARIOS TURNOS])"
    )
    assert long and not optional
    assert text == "Ir al contenido y abrir su actividad."


def test_the_plan_placeholders_are_not_labels():
    """`[TEMA]` y `[VARIANTE]` a secas son huecos del plan: no se tocan."""
    assert split_marks("[TEMA] algo")[0] == "[TEMA] algo"
    assert split_marks("Tocar el subtema [TEMA]")[0] == "Tocar el subtema [TEMA]"
    assert split_marks("Responder de modo que ocurra esto: [VARIANTE]")[0].endswith("[VARIANTE]")


# --- 3. una acción que depende del tutor lleva [SI APARECE] ------------------


def test_an_action_that_depends_on_what_the_tutor_offers_must_be_optional():
    """PT-D4-03 y PT-D14-06: seis pruebas cortadas porque el tutor preguntó en vez de proponer."""
    steps = [
        "Elegir una opción incorrecta.",
        "Confirmar ante el tutor la hipótesis de error que proponga (por ejemplo, 'Sí, hice eso').",
        "Guardar la pantalla con la solución.",
        "Tocar el tema Potencias.",
    ]
    issues = lint_steps(steps)
    assert len(issues) == 1 and "Paso 2" in issues[0] and OPTIONAL_MARK in issues[0]

    fixed, left = repair_steps(steps)
    assert not left
    assert split_marks(fixed[1])[1], "el código le pone la marca si el redactor insiste"
    assert not lint_steps(fixed)


def test_the_same_step_already_marked_is_fine():
    steps = [
        "Elegir una opción incorrecta.",
        "[SI APARECE] Confirmar ante el tutor el error que propone.",
        "Guardar la pantalla.",
    ]
    assert not lint_steps(steps)


def test_advancing_twice_without_answering_in_between_is_flagged():
    """Once pruebas cortadas: 'Avanzar', 'Guardar la nueva pregunta', 'Avanzar'."""
    steps = [
        "[VARIOS TURNOS] Ir al contenido Potencias y abrir su actividad.",
        "En la pregunta que aparezca, elegir una opción incorrecta.",
        "[SI APARECE] Confirmar ante el tutor la hipótesis del error que propone.",
        "Guardar la pantalla con la solución.",
        "Avanzar a la siguiente pregunta del subtema (por ejemplo 'Siguiente pregunta').",
        "Guardar la nueva pregunta que quedó en pantalla.",
        "Avanzar a la siguiente pregunta del subtema.",
        "Guardar la tercera pregunta.",
    ]
    issues = lint_steps(steps)
    assert len(issues) == 1 and issues[0].startswith("Paso 7:")
    assert "sin que un paso anterior haya respondido" in issues[0]


def test_answering_between_two_advances_is_fine_however_it_is_worded():
    steps = [
        "Ir al contenido Potencias y abrir su actividad.",
        "En esa primera pregunta, tocar una opción incorrecta.",
        "Avanzar a la siguiente pregunta.",
        "En esa segunda pregunta, tocar la opción correcta.",
        "[VARIOS TURNOS] Avanzar a la pregunta siguiente.",
        "Responder bien la pregunta que aparezca.",
        "Avanzar a la siguiente actividad del mismo subtema.",
    ]
    assert not lint_steps(steps)


# --- 4. el ejecutor: reglas y lo que deja anotado ----------------------------


def test_the_executor_is_told_the_four_rules_of_this_run():
    for rule in ("GUARDAR ES MIRAR", "AVANZAR ES LLEGAR A LO SIGUIENTE",
                 "LO QUE YA ESTÁ HECHO NO SE REPITE", "ESPERÁ LA RESPUESTA"):
        assert rule in STEP_SYSTEM
    assert "desvio" in STEP_DONE_TOOL["input_schema"]["properties"]


class _Agente:
    """El bucle del agente, reducido a lo que devuelve."""

    def __init__(self, **result):
        self._result = SimpleNamespace(**{
            "estimated_cost_usd": 0.0, "blocked": False, "accomplished": True,
            "done_reason": "", "not_present": False, "deviation": "", **result,
        })
        self.max_iterations = 20
        self.cost_cap_usd = 2.0
        self.tools = []
        self.model = "m"
        self.effort = None
        self.max_tokens = 2048

    async def run(self, *, system, user_task):
        return self._result


def test_a_step_done_another_way_says_so():
    """'Avanzar a la siguiente pregunta' respondiendo la que no dejaba saltar."""
    driver = AgentDriver(
        _Agente(deviation="respondí la pregunta con la opción A para poder avanzar"),
        FakeDriver(Path(".")),
    )
    out = asyncio.run(driver.do("Avanzar a la siguiente pregunta."))
    assert out.startswith(DEVIATION) and "opción A" in out

    note = _note(7, "Avanzar a la siguiente pregunta.", skipped=out)
    assert "SE HIZO DISTINTO DE LO ESCRITO" in note and "opción A" in note


def test_a_step_done_as_written_returns_nothing():
    driver = AgentDriver(_Agente(), FakeDriver(Path(".")))
    assert asyncio.run(driver.do("Tocar 'Empezar'.")) is None


def test_a_step_that_could_not_be_done_carries_the_step_and_the_reason():
    driver = AgentDriver(
        _Agente(accomplished=False, done_reason="no hay botón 'Siguiente pregunta'"),
        FakeDriver(Path(".")),
    )
    try:
        asyncio.run(driver.do("Avanzar a la siguiente pregunta."))
    except repair.StepNotDone as exc:
        assert exc.step == "Avanzar a la siguiente pregunta."
        assert "Siguiente pregunta" in exc.reason
        assert isinstance(exc, EvalkitError), "quien no sabe reparar lo trata como antes"
    else:  # pragma: no cover
        raise AssertionError("un paso no hecho tiene que levantar")


# --- 5. no se captura con la plataforma a medio responder --------------------


def test_the_busy_pattern_needs_the_dots():
    assert pw.BUSY_PATTERN.search("Gallito está pensando…")
    assert pw.BUSY_PATTERN.search("- status: Cargando...")
    assert pw.BUSY_PATTERN.search("Preparando un ejemplo…")
    assert not pw.BUSY_PATTERN.search("Generando hábitos de estudio")
    assert not pw.BUSY_PATTERN.search("Pensando en fracciones: una guía")


def _driver_con_paginas(pages, monkeypatch):
    """Un PlaywrightDriver sin navegador: sólo lo que `settle` usa."""
    driver = object.__new__(pw.PlaywrightDriver)
    driver.state = SimpleNamespace(log=lambda *a, **k: None)
    reads = iter(pages)
    last = {"text": pages[-1]}

    async def page_text():
        try:
            last["text"] = next(reads)
        except StopIteration:
            pass
        return last["text"]

    driver._page_text = page_text

    async def no_sleep(_):
        return None

    monkeypatch.setattr(pw.asyncio, "sleep", no_sleep)
    return driver


def test_settle_waits_until_the_tutor_has_answered(monkeypatch):
    """PT-D9-H1: la captura decía 'Gallito está pensando…' y nada más."""
    driver = _driver_con_paginas(
        ["Gallito está pensando…", "Gallito está pensando…", "Gallito: probemos con 3 × 2."],
        monkeypatch,
    )
    assert asyncio.run(driver.settle()) == ""


def test_settle_gives_up_and_says_what_was_still_on_screen(monkeypatch):
    driver = _driver_con_paginas(["Gallito está pensando…"], monkeypatch)
    still = asyncio.run(driver.settle(timeout_s=3))
    assert "pensando" in still


def test_a_quiet_page_costs_one_read(monkeypatch):
    driver = _driver_con_paginas(["¿Cuál es el valor de 3 × 2³?"], monkeypatch)
    assert asyncio.run(driver.settle()) == ""


class _DriverQueEspera(FakeDriver):
    def __init__(self, tmp_path, still=""):
        super().__init__(tmp_path)
        self.still = still
        self.order: list[str] = []

    async def do(self, step):
        self.order.append("do")
        return await super().do(step)

    async def settle(self):
        self.order.append("settle")
        return self.still

    async def screenshot(self, name):
        self.order.append("shot")
        return await super().screenshot(name)

    async def read_page(self, max_chars=8000):
        self.order.append("read")
        return await super().read_page(max_chars)


def _probe(steps):
    return Probe(
        probe_id="PB-X-1", test_id="X-1", goal="ver qué responde el tutor",
        steps=list(steps), verdict_rule="CUMPLE si responde",
    )


def test_the_runner_waits_before_every_capture(tmp_path):
    driver = _DriverQueEspera(tmp_path / "shots")
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    asyncio.run(run_probe(_probe(["Enviar '¿me ayudás?' al tutor."]), driver, ledger, platform="p"))
    assert driver.order == ["do", "settle", "shot", "read"]


def test_a_capture_taken_while_still_answering_says_so(tmp_path):
    driver = _DriverQueEspera(tmp_path / "shots", still="Gallito está pensando…")
    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    asyncio.run(run_probe(_probe(["Enviar '¿me ayudás?' al tutor."]), driver, ledger, platform="p"))
    notes = [r["note"] for r in ledger.for_platform_test("p", "X-1")]
    assert notes and all("SEGUÍA RESPONDIENDO AL CAPTURAR" in n for n in notes)


def test_the_runner_says_which_step_cut_the_probe(tmp_path):
    class Corta(FakeDriver):
        async def do(self, step):
            await super().do(step)
            if "Avanzar" in step:
                raise repair.StepNotDone("no se pudo", step=step, reason="no hay botón")

    ledger = EvidenceLedger(tmp_path / "ledger.jsonl")
    try:
        asyncio.run(run_probe(
            _probe(["Elegir una opción.", "Guardar la pantalla.", "Avanzar a la siguiente."]),
            Corta(tmp_path / "shots"), ledger, platform="p",
        ))
    except repair.StepNotDone as exc:
        assert exc.step_index == 3
    else:  # pragma: no cover
        raise AssertionError("tenía que cortarse")


# --- 6. reparar los pasos de una prueba cortada ------------------------------


class _ClienteQueRepara:
    def __init__(self, data):
        self.data = data
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        name = kw["tools"][0]["name"]
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=self.data)])


_PASOS = [
    "Elegir una opción incorrecta.",
    "Guardar lo que la plataforma muestra tras el error.",
    "Avanzar a la siguiente pregunta.",
    "Guardar la nueva pregunta.",
    "Avanzar a la siguiente pregunta.",
    "Guardar la tercera pregunta.",
]


def test_the_repair_is_asked_with_the_step_the_reason_and_the_screen():
    nuevos = [*_PASOS[:4], "En la pregunta que aparezca, elegir una opción de respuesta.", *_PASOS[4:]]
    client = _ClienteQueRepara({"se_puede": True, "pasos": nuevos, "que_cambio": "faltaba responder"})
    out = repair.propose_repair(
        criterion="PT-D8-01: intercalado", goal="ver tres preguntas seguidas",
        verdict_rule="CUMPLE si varían", steps=_PASOS, failed_index=5,
        reason="hay una pregunta sin responder y ningún botón para saltarla",
        screen="¿Cuál es el valor de 3 × 2³? A B C D", client=client, model="m",
        preamble=["Tocar 'Empezar'."],
    )
    assert out == {"pasos": nuevos, "que_cambio": "faltaba responder"}
    sent = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "El paso que no se pudo hacer es el 5." in sent
    assert "ningún botón para saltarla" in sent and "3 × 2³" in sent
    assert "Tocar 'Empezar'." in sent, "el reparador sabe qué hace ya la preparación"


def test_an_answer_without_steps_is_no_repair():
    assert repair.propose_repair(
        criterion="c", goal="g", verdict_rule="v", steps=_PASOS, failed_index=3,
        reason="r", screen="", client=_ClienteQueRepara({"verdict": "CUMPLE"}), model="m",
    ) is None


def test_a_repair_that_only_adds_what_was_missing_is_accepted():
    nuevos = [*_PASOS[:4], "En la pregunta que aparezca, elegir una opción de respuesta.", *_PASOS[4:]]
    fixed, why = repair.accept_repair(_PASOS, nuevos)
    assert fixed == nuevos and why == ""


def test_a_repair_cannot_drop_the_captures_or_become_another_probe():
    fixed, why = repair.accept_repair(_PASOS, ["Elegir una opción incorrecta.", "Avanzar."])
    assert fixed is None and "capturas" in why

    muchos = [*_PASOS, *[f"Tocar el botón {n}." for n in range(6)]]
    fixed, why = repair.accept_repair(_PASOS, muchos)
    assert fixed is None and "otra prueba" in why

    fixed, why = repair.accept_repair(_PASOS, list(_PASOS))
    assert fixed is None


def test_a_repair_never_gets_a_probe_past_the_gate():
    """Si los pasos corregidos disparan un marcador que los originales no, no corren."""
    policy = load_risk_markers()
    marker = policy.markers[0]
    test = SimpleNamespace(name="n", verification="v", pass_criteria="p", fail_criteria="f")
    probe = _probe(_PASOS)
    assert not policy.matches([" ".join(_PASOS)]), "los pasos de base no disparan nada"

    riesgosos = [*_PASOS[:1], f"Escribirle al tutor: {marker.terms[0]}.", *_PASOS[1:]]
    fixed, why = repair.accept_repair(_PASOS, riesgosos, test=test, probe=probe, policy=policy)
    assert fixed is None
    assert "marcador de riesgo" in why and marker.id in why


# --- 7. el camino entero: cortar, reparar, reintentar ------------------------


class _DriverDelPlan(FakeDriver):
    """Explora como el recorrido de prueba; en la corrida se corta donde le digan."""

    corta: str | None = None
    veces = 10**6

    async def explore(self, task: str, **kw) -> dict:
        if "EXPLORACIÓN DIRIGIDA" in task:
            (self.dir / "recon_parte_02.png").write_bytes(PNG)
            return {"partes": [{
                "nombre": "Práctica mixta", "rol": "estudiante",
                "como_llegar": "Tocar 'Mezclar subtemas'", "que_muestra": "Preguntas sin tutor",
                "captura": "recon_parte_02.png", "sha256": "b" * 64,
            }]}
        if "INVENTARIO" not in task:
            return {"no_encontrado": ["nada nuevo"]}
        (self.dir / "recon_parte_01.png").write_bytes(PNG)
        return {"partes": [{
            "nombre": "Después de un error", "rol": "estudiante",
            "como_llegar": "Responder mal", "que_muestra": "La hipótesis del tutor",
            "captura": "recon_parte_01.png", "sha256": "a" * 64,
        }]}

    async def do(self, step):
        await super().do(step)
        if (
            self.corta and self.corta in step
            and not split_marks(step)[1]
            and type(self).veces > 0
        ):
            type(self).veces -= 1
            raise repair.StepNotDone(
                f"el agente no pudo completar el paso: {step!r} — no está en pantalla",
                step=step, reason="no está en pantalla",
            )


def _estrategia(criteria, *, profile, revisited=None, **kw):
    """Prueba los dos primeros; el tercero queda en 'falta recorrer' hasta que se lo busca."""
    partes = [p["id"] for p in (profile or {}).get("partes") or []]
    if revisited:
        return pl.validate_strategy(
            [{"id": c["id"], "estado": "se_prueba", "partes": partes[-1:], "motivo": "apareció",
              "que_se_prueba": "Abrir la práctica mixta"} for c in criteria],
            criteria, profile,
        )
    return pl.validate_strategy(
        [{"id": c["id"], "estado": "se_prueba", "partes": ["P1"], "motivo": "se vio",
          "que_se_prueba": "Responder mal y leer la corrección"} for c in criteria[:2]]
        + [{"id": criteria[2]["id"], "estado": "falta_recorrer",
            "motivo": "habría que abrir 'Mezclar subtemas'"}],
        criteria, profile,
    )


def _plan(tmp_path, *, corta=None, veces=10**6, cliente=None, **engine_kw):
    engine_kw.setdefault("plan_revisit", False)
    engine = make_engine(
        tmp_path, client=cliente or _cliente(),
        plan_strategy=_estrategia, plan_sampling=_muestreo, **engine_kw,
    )
    _DriverDelPlan.veces = veces

    def factory(job, credentials, artifacts_dir):
        d = _DriverDelPlan(artifacts_dir, credentials=credentials)
        d.corta = corta if getattr(engine, "en_corrida", False) else None
        engine.test_drivers.append(d)
        return d

    engine.driver_factory = factory
    store = JobStore(tmp_path / "runs")
    entrada = ManifestEntry(
        platform_url="https://plataforma.test",
        objective="Un tutor de IA de matemática para 1.º a 6.º grado.",
        areas=["D9", "D1", "PRIV"],
    )
    resumen = asyncio.run(run_batch([entrada], store=store, engine=engine, box=None, plan_only=True))
    return store, engine, resumen.jobs[0].job_id


def _correr(store, engine, job_id):
    engine.en_corrida = True
    return asyncio.run(run_from_plan(store, job_id, engine=engine, box=None, approved_by=APROBO))


def _fila(store, job_id, criterio):
    return next(r for r in store.get(job_id).report["rows"] if r["id"] == criterio)


def test_a_probe_cut_once_is_run_again_and_scored_on_the_second_attempt(tmp_path, env):  # noqa: F811
    # Las dos pruebas de "Potencias" comparten pasos y corren como un grupo:
    # un corte en el primer intento, ninguno en el segundo.
    store, engine, job_id = _plan(tmp_path, corta="Potencias", veces=1)
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    resultado = _correr(store, engine, job_id)
    registro = store.get(job_id)

    assert resultado.stage == Stage.DONE, registro.error
    assert registro.unevaluated == {}, "las dos pruebas cortadas llegaron al final en el reintento"
    assert len(registro.retried) == 2
    assert all(v["run_index"] == 1 and "no está en pantalla" in v["motivo"]
               for v in registro.retried.values())
    fila = _fila(store, job_id, probados[0])
    assert fila["rationale"].startswith("CUMPLE en 3 de 3 prueba(s) evaluada(s)")


def test_a_probe_cut_twice_stays_unevaluated_with_both_reasons(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan(tmp_path, corta="Potencias")
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)
    registro = store.get(job_id)

    assert len(registro.unevaluated) == 2 and registro.retried == {}
    assert all("PRIMER INTENTO" in why for why in registro.unevaluated.values())
    fila = _fila(store, job_id, probados[0])
    assert fila["rationale"].startswith("CUMPLE en 2 de 2 prueba(s) evaluada(s)")
    assert "1 cortada(s)" in fila["rationale"], "una prueba cortada sigue sin puntuarse"


def test_the_second_attempt_runs_the_repaired_steps_and_the_record_keeps_both(tmp_path, env):  # noqa: F811
    cliente = _cliente()
    original = cliente.messages.create

    def create(**kw):
        if kw["tools"][0]["name"] == repair.REPAIR_TOOL_NAME:
            texto = kw["messages"][0]["content"][0]["text"]
            pasos = [
                line.split(". ", 1)[1] for line in texto.split("PASOS\n", 1)[1].split("\n\n", 1)[0].splitlines()
            ]
            pasos = [f"{OPTIONAL_MARK} {p}" if "Potencias" in p else p for p in pasos]
            return SimpleNamespace(content=[SimpleNamespace(
                type="tool_use", name=repair.REPAIR_TOOL_NAME,
                input={"se_puede": True, "pasos": pasos, "que_cambio": "el subtema puede no estar"},
            )])
        return original(**kw)

    cliente.messages = SimpleNamespace(create=create)
    store, engine, job_id = _plan(tmp_path, corta="Potencias", cliente=cliente)
    _correr(store, engine, job_id)
    registro = store.get(job_id)

    assert registro.unevaluated == {}
    reparadas = {pid: v for pid, v in registro.retried.items() if v["reparacion"]}
    assert len(reparadas) == 2
    for pid in reparadas:
        corridos = registro.executed_steps[pid]
        assert any(s.startswith(OPTIONAL_MARK) and "Potencias" in s for s in corridos)
        aprobados = next(p for p in registro.probes if p["probeId"] == pid)["steps"]
        assert not any(OPTIONAL_MARK in s["action"] for s in aprobados), \
            "los pasos aprobados no se pisan: lo que corrió queda al lado"
    assert registro.spend_by_stage.get("reparacion") is not None


def test_only_the_rows_of_the_attempt_that_counts_reach_the_judge():
    rows = [
        {"probe_id": "A", "run_index": 0, "artifact": "a0"},
        {"probe_id": "A", "run_index": 1, "artifact": "a1"},
        {"probe_id": "B", "run_index": 0, "artifact": "b0"},
    ]
    kept = _attempt_rows(rows, {"A": {"run_index": 1}})
    assert [r["artifact"] for r in kept] == ["a1", "b0"]
    assert _attempt_rows(rows, {}) == rows


# --- 8. un juicio coherente: una lectura de la regla, y una segunda ronda ----


def _test_obj():
    return SimpleNamespace(
        id="PT-D2-01", name="Elementos nuevos por pantalla", points=1.0,
        pass_criteria="como mucho cuatro elementos nuevos por pantalla",
        fail_criteria="más de cuatro", verification="contar los elementos nuevos",
    )


def test_the_rule_is_read_once_and_comes_back_as_text():
    client = _ClienteQueRepara({
        "pregunta": "¿Cuántos elementos nuevos hay por pantalla?",
        "definiciones": "Elemento nuevo incluye los botones de ayuda.",
        "cumple": "Cuatro o menos.", "no_cumple": "Cinco o más.",
        "no_alcanza": "Una pantalla que no es la primera de un contenido.",
    })
    reading = scoring.read_rule(_test_obj(), client, model="m", context="abrir un contenido nuevo")
    assert "incluye los botones de ayuda" in reading
    assert "CUMPLE si se observa: Cuatro o menos." in reading
    assert "abrir un contenido nuevo" in client.calls[0]["messages"][0]["content"][0]["text"]


def test_a_reading_without_the_question_and_both_ends_is_no_reading():
    assert scoring.read_rule(_test_obj(), _ClienteQueRepara({"verdict": "CUMPLE"}), model="m") == ""


def test_every_probe_of_a_criterion_gets_the_same_reading(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(PNG)
    rows = [{"artifact": str(shot), "kind": "screenshot", "note": "paso 1", "platform": "p"}]
    content, _ = scoring._build_content(_test_obj(), rows, "- Definiciones: incluye la ayuda.")
    assert "LECTURA DE LA REGLA" in content[0]["text"]
    assert "incluye la ayuda" in content[0]["text"]
    sin, _ = scoring._build_content(_test_obj(), rows)
    assert "LECTURA DE LA REGLA" not in sin[0]["text"]


def _con_chequeo(tmp_path, respuestas):
    """Un motor cuyo chequeo de coherencia responde, en orden, lo que le digan."""
    vistos: list[str] = []
    pendientes = list(respuestas)

    def check(test, cells, client, *, model=None):
        return pendientes.pop(0) if pendientes else {"ok": True, "motivo": ""}

    def score(test, rows, client, *, reading="", **kw):
        vistos.append(reading)
        return scoring.score_test(test, rows, client, reading=reading, **kw)

    def read(test, client, *, model=None, context=""):
        return f"- Qué pregunta la regla: la de {test.id}"

    store, engine, job_id = _plan(
        tmp_path, check_verdicts=check, score_test=score, read_rule=read,
    )
    return store, engine, job_id, vistos


def test_an_objection_sends_the_criterion_back_to_the_judge_not_to_the_bin(tmp_path, env):  # noqa: F811
    """Cinco criterios y 16 pruebas completas quedaron sin respuesta por 'juicio en duda'."""
    store, engine, job_id, vistos = _con_chequeo(
        tmp_path, [{"ok": False, "motivo": "la prueba 2 juzga otra cosa que la regla"}],
    )
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)

    fila = _fila(store, job_id, probados[0])
    assert fila["verdict"] == "CUMPLE"
    assert "SEGUNDA ronda" in fila["rationale"] and "juzga otra cosa" in fila["rationale"]
    primera, segunda = vistos[:3], vistos[3:6]
    assert all(r == f"- Qué pregunta la regla: la de {probados[0]}" for r in primera)
    assert all("OBJECIÓN DE UN REVISOR" in r and r.startswith(primera[0]) for r in segunda), \
        "la segunda ronda juzga con la misma lectura y la objeción a la vista"


def test_a_verdict_that_does_not_hold_twice_is_still_not_published(tmp_path, env):  # noqa: F811
    store, engine, job_id, _ = _con_chequeo(tmp_path, [
        {"ok": False, "motivo": "mismo hecho, distinto veredicto"},
        {"ok": False, "motivo": "sigue igual"},
    ])
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)

    fila = _fila(store, job_id, probados[0])
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: juicio en duda]")
    assert "ni tras volver a juzgar" in fila["rationale"]
    assert "Primera ronda" in fila["rationale"], "quedan las dos rondas escritas"


# --- 9. lo que el plan pidió mirar, se mira ----------------------------------


class _Explorador:
    def __init__(self, reports):
        self.reports = list(reports)
        self.tasks: list[str] = []

    async def explore(self, task, **kw):
        self.tasks.append(task)
        out = self.reports.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


_PENDIENTES = [
    {"id": "PT-D1-04", "nombre": "Práctica independiente", "motivo": "abrir 'Mezclar subtemas'"},
    {"id": "PT-D7-08", "nombre": "Autoría", "motivo": "buscar un menú 'Crear' en la vista docente"},
]


def test_the_directed_pass_goes_where_the_plan_said_and_grows_the_profile():
    profile = {"partes": [{"id": "P1", "nombre": "Inicio"}], "comportamientos": []}
    agent = _Explorador([{"partes": [{"nombre": "Práctica mixta", "rol": "estudiante",
                                      "como_llegar": "Mezclar subtemas", "que_muestra": "preguntas"}],
                          "no_encontrado": ["menú 'Crear': la vista docente es de sólo lectura"]}])
    out = asyncio.run(recon.revisit(agent, profile, _PENDIENTES, root_url="https://x.test"))

    assert out == {"PT-D1-04": recon.REVISIT_DONE, "PT-D7-08": recon.REVISIT_DONE}
    assert "abrir 'Mezclar subtemas'" in agent.tasks[0] and "PT-D7-08" in agent.tasks[0]
    assert [p["id"] for p in profile["partes"]] == ["P1", "P2"]
    assert profile["partes"][1]["exploracion"].startswith("dirigida:")
    assert profile["no_encontrado"] == ["menú 'Crear': la vista docente es de sólo lectura"]


def test_an_exploration_that_did_not_finish_proves_nothing():
    for report, texto in (
        (EvalkitError("se quedó sin turnos"), "no se pudo explorar"),
        ({"incompleto": "max_iterations", "partes": []}, "sin el informe de cierre"),
    ):
        profile = {"partes": [], "comportamientos": []}
        out = asyncio.run(recon.revisit(
            _Explorador([report]), profile, _PENDIENTES[:1], root_url="https://x.test",
        ))
        assert texto in out["PT-D1-04"] and out["PT-D1-04"] != recon.REVISIT_DONE
        assert profile["sin_explorar"]


def test_after_a_complete_search_falta_recorrer_is_no_longer_an_answer():
    decisions = [
        {"id": "A", "estado": pl.FALTA_RECORRER, "motivo": "no hay menú 'Crear'", "advertencias": []},
        {"id": "B", "estado": pl.FALTA_RECORRER, "motivo": "no se vio", "advertencias": []},
        {"id": "C", "estado": pl.SE_PRUEBA, "motivo": "apareció", "advertencias": []},
    ]
    pl.settle_revisited(decisions, {"A": "hecha", "B": "terminó sin el informe de cierre", "C": "hecha"})
    a, b, c = decisions
    assert a["estado"] == pl.NO_SE_PUEDE_MEDIR and "exploración dirigida" in a["motivo"]
    assert b["estado"] == pl.FALTA_RECORRER, "de una búsqueda sin terminar no se concluye 'no existe'"
    assert c["estado"] == pl.SE_PRUEBA


def test_the_plan_sends_the_explorer_back_and_decides_again(tmp_path, env):  # noqa: F811
    """Siete criterios quedaron en 'falta recorrer' con la indicación escrita y nadie que mirara."""
    store, engine, job_id = _plan(tmp_path, plan_revisit=True)
    registro = store.get(job_id)

    assert registro.stage == Stage.PLAN_READY, registro.error
    estados = [d["estado"] for d in registro.strategy]
    assert estados.count(pl.SE_PRUEBA) == 3 and pl.FALTA_RECORRER not in estados
    tercero = registro.strategy[[d["estado"] for d in registro.strategy].index(pl.SE_PRUEBA) + 2]
    assert tercero["partes"] == ["P2"], "el criterio se apoya en la pantalla que apareció"
    assert tercero.get("celdas"), "y entra al muestreo como cualquier otro que se prueba"
    assert len(engine.test_drivers) == 2, "el navegador se abrió otra vez para ir a buscar"


def test_direct_capture_still_marks_the_note():
    """El paso que sólo mira sigue avisando que nadie comprobó la pantalla."""
    assert "CAPTURA DIRECTA" in _note(2, "Guardar la solución paso a paso.", skipped=DIRECT)
