"""Del plan aprobado al informe: una prueba por celda, un juicio por celda.

Lo que se fija acá:

  * el redactor recibe lo que el plan decidió y escribe los pasos UNA vez, con
    marcadores; cada celda del muestreo es una instancia con su propio id;
  * el criterio se puntúa "en x de n": cada celda se juzga sola, una celda que
    se cortó no cuenta en contra, y una que no corrió por compuerta tampoco;
  * la decisión del plan manda sobre la clasificación previa del marco;
  * cada NO_EVALUADO lleva su causa, y las causas que son falla del evaluador
    hacen fallar el autochequeo de la corrida;
  * la aprobación deja escrito en el ledger quién aprobó el plan, y NO firma
    ninguna prueba con compuerta.

LO QUE ESTOS DOBLES NO VEN: que el redactor real use bien los marcadores, que
el agente real ejecute cada celda sobre el tema correcto, ni cuánto cuesta.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from lxp_eval.evalkit import planner as pl
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.service.batch import ManifestEntry, run_batch, run_from_plan
from lxp_eval.service.store import JobStore
from lxp_eval.service.watchdog import Stage

from tests.test_service import (
    DRAFT_TOOL,
    REVISE,
    FakeAnthropic,
    FakeDriver,
    env,  # noqa: F401 -- fixture
    make_engine,
)
from tests.test_plan_servicio import PNG, _muestreo

APROBO = "la responsable de la evaluación, sobre el documento del plan"


# --- 1. las celdas se vuelven pruebas ----------------------------------------


def _redactada():
    return {
        "probeId": "PB-D4-02", "criterionId": "D4-02",
        "goal": "Ver si nombra el error en [TEMA]",
        "verdictRule": "CUMPLE si nombra el error",
        "steps": [
            {"n": 1, "action": "Tocar el nombre guardado", "capture": "screenshot"},
            {"n": 2, "action": "Tocar el subtema [TEMA]", "capture": "screenshot"},
            {"n": 3, "action": "Responder de modo que ocurra esto: [VARIANTE]", "capture": "screenshot"},
        ],
        "requiresSignoff": False,
    }


def test_each_cell_becomes_a_probe_with_its_own_id_and_steps():
    celdas = [{"tema": "Monomios", "variante": "error de signo"},
              {"tema": "Potencias", "variante": "orden de operaciones"}]
    instancias = pl.instantiate(_redactada(), celdas)

    assert [i["probeId"] for i in instancias] == ["PB-D4-02-c1", "PB-D4-02-c2"]
    assert all(i["criterionId"] == "D4-02" for i in instancias), "mismo criterio: se juntan en x de n"
    assert instancias[1]["steps"][1]["action"] == "Tocar el subtema Potencias"
    assert "orden de operaciones" in instancias[1]["steps"][2]["action"]
    # La variante orienta, no exige: el paso lleva la salida dicha (ver abajo).
    assert "orientación" in instancias[1]["steps"][2]["action"]
    assert "orientación" not in instancias[1]["steps"][1]["action"], "sólo en el paso de la variante"
    assert instancias[0]["goal"] == "Ver si nombra el error en Monomios"
    assert instancias[1]["celda"] == {"n": 2, "tema": "Potencias", "variante": "orden de operaciones"}
    assert not any("[TEMA]" in s["action"] or "[VARIANTE]" in s["action"]
                   for i in instancias for s in i["steps"]), "ningún marcador llega al ejecutor"


def test_cells_that_leave_the_steps_identical_are_one_probe_and_it_says_so():
    """Correr dos veces lo mismo y contarlo como dos es inflar el n."""
    sin_marcadores = _redactada()
    sin_marcadores["steps"] = [{"n": 1, "action": "Abrir la política", "capture": "page_text"}]
    instancias = pl.instantiate(sin_marcadores, [{"tema": "", "variante": "a"}, {"tema": "", "variante": "b"}])
    assert len(instancias) == 1
    assert instancias[0]["celdasFundidas"] == 1


def test_a_criterion_without_cells_still_gets_one_probe():
    assert [i["probeId"] for i in pl.instantiate(_redactada(), [])] == ["PB-D4-02-c1"]


def test_the_drafter_is_told_what_the_plan_decided():
    guia = pl.guidance_for(
        {"que_se_prueba": "Responder mal y leer la hipótesis", "partes": ["P2"],
         "celdas": [{"tema": "Monomios", "variante": "error de signo"}]},
        [{"id": "P2", "nombre": "Después de un error", "como_llegar": "Responder mal"}],
    )
    for texto in ("Responder mal y leer la hipótesis", "P2 (Después de un error", "Monomios",
                  "error de signo", "[TEMA]", "[VARIANTE]", "UNA sola vez"):
        assert texto in guia


# --- 2. el camino entero, con dobles -----------------------------------------


class _DriverQueRegistra(FakeDriver):
    corta_en: str | None = None

    async def explore(self, task: str, **kw) -> dict:
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
        if self.corta_en and self.corta_en in step:
            raise EvalkitError(f"el agente no pudo completar el paso: {step!r}")


def _cliente(**kw):
    """El modelo de prueba, redactando con marcadores como pide la guía."""
    cliente = FakeAnthropic(**kw)
    original = cliente.messages.create

    def create(**llamada):
        r = original(**llamada)
        if llamada["tools"][0]["name"] == DRAFT_TOOL["name"]:
            r.content[0].input["steps"] = [
                "Tocar el nombre guardado", "Tocar el subtema [TEMA]",
                "Responder de modo que ocurra esto: [VARIANTE]",
            ]
        return r

    cliente.messages = SimpleNamespace(create=create)
    return cliente


def _estrategia_dos(criteria, *, profile, **kw):
    """Prueba los dos primeros criterios; el resto hereda del marco."""
    return pl.validate_strategy(
        [{"id": c["id"], "estado": "se_prueba", "partes": ["P1"], "motivo": "se vio",
          "que_se_prueba": "Responder mal y leer la corrección"} for c in criteria[:2]]
        + [{"id": criteria[2]["id"], "estado": "falta_recorrer",
            "motivo": "no se vio la pantalla donde se probaría"}],
        criteria, profile,
    )


def _plan_listo(tmp_path, *, cliente=None, corta_en=None):
    engine = make_engine(
        tmp_path, client=cliente or _cliente(),
        plan_strategy=_estrategia_dos, plan_sampling=_muestreo,
    )

    def factory(job, credentials, artifacts_dir):
        d = _DriverQueRegistra(artifacts_dir, credentials=credentials)
        d.corta_en = corta_en if engine.test_drivers else None   # sólo en la corrida
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


def _desde_plan(store, engine, job_id, **kw):
    return asyncio.run(run_from_plan(
        store, job_id, engine=engine, box=None, approved_by=APROBO, **kw
    ))


def _filas(store, job_id):
    return {r["id"]: r for r in store.get(job_id).report["rows"]}


def test_from_an_approved_plan_to_a_report_in_x_of_n(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path)
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    assert len(probados) == 2

    resultado = _desde_plan(store, engine, job_id)
    registro = store.get(job_id)

    assert resultado.stage == Stage.DONE, registro.error
    # Una prueba por celda: dos criterios, tres celdas cada uno.
    assert len(registro.probes) == 6
    assert {p["probeId"].rsplit("-", 1)[1] for p in registro.probes} == {"c1", "c2", "c3"}
    ejecutados = [s for d in engine.test_drivers[1:] for s in d.steps]
    assert "Tocar el subtema Potencias" in ejecutados, "cada celda corre sobre SU tema"
    assert not any("[TEMA]" in s for s in ejecutados)

    fila = _filas(store, job_id)[probados[0]]
    assert fila["verdict"] == "CUMPLE"
    assert fila["rationale"].startswith("CUMPLE en 3 de 3 prueba(s) evaluada(s)")
    assert "Monomios" in fila["rationale"] and "Ecuaciones" in fila["rationale"]
    assert fila["runs"] == 3 and fila["evidence"], "el veredicto cita la evidencia de sus celdas"


def test_the_ledger_says_who_approved_the_plan_and_signs_nothing(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path)
    _desde_plan(store, engine, job_id)
    registro = store.get(job_id)

    filas = [json.loads(l) for l in store.ledger_path(job_id).read_text(encoding="utf-8").splitlines() if l.strip()]
    aprobaciones = [f for f in filas if f.get("kind") == "approval"]
    assert len(aprobaciones) == 1
    assert APROBO in aprobaciones[0]["note"]
    assert "versión 1" in aprobaciones[0]["note"]
    assert registro.signoffs == {}, "aprobar el plan no firma ninguna prueba"


def test_the_plans_decision_overrides_the_frameworks_earlier_class(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path)
    _desde_plan(store, engine, job_id)
    registro = store.get(job_id)

    por_id = {c["id"]: c for c in registro.criteria}
    pendiente = next(d for d in registro.strategy if d["estado"] == "falta_recorrer")
    criterio = por_id[pendiente["id"]]
    assert criterio["observability"] == "NO_DECIDIBLE_EN_EVALUACION"
    assert criterio["observabilityOriginal"], "la clase anterior queda guardada al lado"
    assert criterio["observabilityReason"].startswith("[causa: falta recorrer]")
    assert not any(p["criterionId"] == pendiente["id"] for p in registro.probes), \
        "lo que el plan dejó indefinido no se redacta"


def test_every_unevaluated_criterion_names_its_cause(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path)
    _desde_plan(store, engine, job_id)
    informe = store.get(job_id).report

    sin_evaluar = [r for r in informe["rows"] if r["verdict"] == "NO_EVALUADO"]
    assert sin_evaluar
    assert all(r["rationale"].startswith("[causa: ") for r in sin_evaluar), \
        [r["rationale"][:60] for r in sin_evaluar if not r["rationale"].startswith("[causa: ")]
    causas = informe["meta"]["causasNoEvaluado"]
    assert causas.get("falta recorrer") == 1
    # "Falta recorrer" es una falla del evaluador, no un resultado: el
    # autochequeo de la corrida la nombra.
    assert informe["meta"]["autochequeo"]["ok"] is False
    assert "falta recorrer" in informe["meta"]["autochequeo"]["fallasDelEvaluador"]


def test_a_cell_that_was_cut_short_does_not_count_against_the_platform(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path, corta_en="Potencias")
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _desde_plan(store, engine, job_id)

    fila = _filas(store, job_id)[probados[0]]
    assert fila["verdict"] == "CUMPLE", "dos celdas cumplieron; la cortada no se juzga"
    assert fila["rationale"].startswith("CUMPLE en 2 de 2 prueba(s) evaluada(s)")
    assert "1 cortada(s)" in fila["rationale"]
    assert "CORTADA" in fila["rationale"], "y el informe dice cuál se cortó y por qué"
    assert fila["earned"] == fila["points"]


def test_a_gated_criterion_stays_unevaluated_and_says_it_needs_a_signature(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path, cliente=_cliente(safety_verdict=REVISE))
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _desde_plan(store, engine, job_id)
    registro = store.get(job_id)

    assert all(p["requiresSignoff"] for p in registro.probes)
    assert [s for d in engine.test_drivers[1:] for s in d.steps] == [], \
        "una prueba con compuerta no corre sin firma, tampoco por este camino"
    fila = _filas(store, job_id)[probados[0]]
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: compuerta sin firma] 3 prueba(s)")


def test_a_pilot_drafts_only_the_criteria_it_names(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan_listo(tmp_path)
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _desde_plan(store, engine, job_id, only=[probados[0]])
    registro = store.get(job_id)

    assert {p["criterionId"] for p in registro.probes} == {probados[0]}
    otra = _filas(store, job_id)[probados[1]]
    assert otra["verdict"] == "NO_EVALUADO"
    assert otra["rationale"].startswith("[causa: sin redactar]")


def test_a_job_without_a_plan_cannot_be_run_from_a_plan(tmp_path, env):  # noqa: F811
    store = JobStore(tmp_path / "runs")
    engine = make_engine(tmp_path)
    registro = store.create(objective="x", platform_url="https://plataforma.test", platform="p")
    resultado = _desde_plan(store, engine, registro.job_id)
    assert resultado.stage == Stage.ERROR
    assert "plan" in resultado.error


# --- 3. lo que mostró el piloto en vivo (2026-11-25, USD 4,18) ----------------
#
# Siete pruebas de tres criterios: tres corrieron, tres se cortaron y una quedó
# en compuerta. Las tres cortadas eran del mismo origen: la celda pedía una
# situación que la plataforma no ofreció en ese momento.


def test_a_variant_is_a_direction_not_a_demand():
    """La celda pedía "el error de signo de potencia" y la plataforma mostró una
    resta. El agente dijo que no podía, con razón, y la prueba se cortó."""
    instancias = pl.instantiate(_redactada(), [{"tema": "Potencias", "variante": "error de signo de potencia"}])
    paso = instancias[0]["steps"][2]["action"]
    assert "error de signo de potencia" in paso
    assert "la más parecida que sí permita" in paso
    assert "No lo des por no hecho" in paso


def test_the_drafter_is_told_a_variant_step_is_still_one_action():
    guia = pl.guidance_for({"que_se_prueba": "x", "partes": [], "celdas": []})
    assert "pasos separados" in guia


def test_sampling_is_told_the_platform_picks_the_question():
    from lxp_eval.evalkit import sampling as sm

    assert "elige qué pregunta muestra" in sm.SAMPLING_SYSTEM


class _DriverQueRevienta(_DriverQueRegistra):
    async def do(self, step):
        if "Potencias" in step:
            raise TimeoutError("Locator.click: Timeout 4000ms exceeded")
        await super().do(step)


def test_a_probe_that_blows_up_does_not_take_the_whole_run_with_it(tmp_path, env):  # noqa: F811
    """Un timeout del navegador no es un EvalkitError. Salía por arriba y
    marcaba la etapa entera como fallida: con más de cien pruebas, perderlo
    todo por una."""
    engine = make_engine(
        tmp_path, client=_cliente(), plan_strategy=_estrategia_dos, plan_sampling=_muestreo,
    )

    def factory(job, credentials, artifacts_dir):
        clase = _DriverQueRevienta if engine.test_drivers else _DriverQueRegistra
        d = clase(artifacts_dir, credentials=credentials)
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
    job_id = resumen.jobs[0].job_id
    resultado = _desde_plan(store, engine, job_id)
    registro = store.get(job_id)

    assert resultado.stage == Stage.DONE, registro.error
    reventadas = [m for m in registro.unevaluated.values() if "error inesperado" in m]
    assert len(reventadas) == 2, "las dos celdas de Potencias, una por criterio"
    assert "TimeoutError" in reventadas[0]
    probados = [d["id"] for d in registro.strategy if d["estado"] == "se_prueba"]
    assert _filas(store, job_id)[probados[0]]["rationale"].startswith("CUMPLE en 2 de 2")
