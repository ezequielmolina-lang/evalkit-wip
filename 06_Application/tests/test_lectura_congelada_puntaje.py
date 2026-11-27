"""La lectura congelada del lado del puntaje: se congela al aprobar, se usa sin volver a pedirla.

EL CASO. Con la lectura de la regla generada al puntuar, dos corridas sobre la
misma evidencia podían leer la regla distinto: sobre evidencia congelada el
juez cambió el veredicto en 23 de 43 criterios (2026-11-27), y con la lectura
siempre presente seguían volcando 13 de 24. Lo que una persona aprobaba era una
etiqueta, no la lectura con la que se iba a juzgar.

Lo que estas pruebas fijan (tareas 5, 6 y 7 del plan de la lectura congelada):

* al aprobar, la lectura que se puede congelar queda en el plan aprobado, que
  es la instantánea que va al ledger, con dos huellas: la del observable (se
  vence si se reescribe la regla) y la del recorrido (se vence si cambió la
  superficie);
* al puntuar se usa esa lectura y NO se llama a `read_rule`; un plan viejo, sin
  lecturas congeladas, se puntúa con la lectura generada en el momento, como
  siempre, y el informe lo dice;
* una lectura que nadie decidió o que se venció no se reemplaza en silencio: el
  criterio queda NO_EVALUADO con su causa;
* las huellas del juez y del consejo no cambian: la lectura viaja como dato.

Todo corre contra dobles, con textos inventados. Lo que NO ven: si la lectura
congelada es la CORRECTA. Que se reproduzca no dice que acierte: eso se mide
contra veredictos de personas.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from lxp_eval.evalkit import lectura as lec
from lxp_eval.evalkit import replay
from lxp_eval.evalkit import scoring
from lxp_eval.service.jobs import JobRunner
from lxp_eval.service.ledger_decisions import approve_from_plan
from lxp_eval.service.watchdog import Stage

from tests.test_arreglos_corrida_2026_11_26 import (  # noqa: F401 -- `env` es un fixture
    _correr,
    _fila,
    _plan,
    env,
)
from tests.test_lectura_congelada import (
    _HUELLA_DEL_CONSEJO,
    _HUELLA_DEL_JUEZ,
    _criterio,
    _estable,
    _generar,
    _lectura,
)
from tests.test_plan_ejecucion import APROBO


class _Lector:
    """Lee al generar; después se le puede prohibir, para ver que el puntaje no lo llama."""

    def __init__(self, lectura=None):
        self.lectura = lectura or _lectura()
        self.permitido = True
        self.llamadas_prohibidas = 0

    def __call__(self, test, client, *, model=None, context=""):
        if not self.permitido:
            self.llamadas_prohibidas += 1
            raise AssertionError("se llamó a read_rule con una lectura congelada en el plan")
        return self.lectura


def _juez_que_anota(vistas):
    def score(test, rows, client, **kw):
        vistas.append(kw.get("reading", ""))
        score.criterios.append(test.id)
        return scoring.score_test(test, rows, client, **kw)
    score.criterios = []
    return score


def _armar(tmp_path, *, equivalentes=True, lector=None, **kw):
    lector = lector or _Lector()
    vistas: list[str] = []
    kw.setdefault("frozen_readings", True)
    store, engine, job_id = _plan(
        tmp_path, read_rule=lector, score_test=_juez_que_anota(vistas),
        compare_readings=lambda *a, **k: {"equivalentes": equivalentes, "diferencias": "" if equivalentes else "otro umbral"},
        check_verdicts=lambda *a, **k: {"ok": True, "motivo": ""},
        **kw,
    )
    return store, engine, job_id, lector, vistas


def _aprobar(store, job_id):
    record = store.get(job_id)
    approve_from_plan(store, record, approved_by=APROBO)
    return store.get(job_id)


def _puntuar(store, engine, job_id):
    engine.en_corrida = True
    asyncio.run(JobRunner(store, engine, None).execute(store.get(job_id)))
    return store.get(job_id)


def _probados(record):
    return [d["id"] for d in record.strategy if d["estado"] == "se_prueba"]


# --- Tarea 5: congelar, hashear, y puntuar con la congelada -------------------


def test_approving_freezes_the_readings_into_the_plan_that_goes_to_the_ledger(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    _generar(store, engine, job_id)
    record = _aprobar(store, job_id)

    congeladas = record.approved_plan["lecturas"]
    assert set(congeladas) == set(_probados(record))
    una = congeladas[_probados(record)[0]]
    assert una["lectura"]["cumple"] == "Cuatro o menos." and una["estado"] == lec.ESTABLE
    assert una["hash_lectura"] == lec.reading_hash(una["lectura"])
    assert una["hash_observable"] and una["hash_recon"]

    guardado = json.loads((store.evidence_dir(job_id) / "approved_plan.json").read_text(encoding="utf-8"))
    assert guardado["lecturas"] == congeladas, "la instantánea del ledger trae las lecturas"


def test_score_usa_lectura_congelada_sin_rellamar(tmp_path, env):  # noqa: F811
    store, engine, job_id, lector, vistas = _armar(tmp_path)
    _generar(store, engine, job_id)
    _aprobar(store, job_id)
    lector.permitido = False            # desde acá, llamar a read_rule es un error
    record = _puntuar(store, engine, job_id)

    assert record.stage == Stage.DONE, record.error
    assert lector.llamadas_prohibidas == 0
    fila = _fila(store, job_id, _probados(record)[0])
    assert fila["verdict"] == "CUMPLE"
    assert vistas and all(v == _lectura().as_text() for v in vistas), \
        "el juez recibe la lectura congelada, tal cual, en todas las pruebas"


def test_scoring_again_reads_the_same_frozen_reading(tmp_path, env):  # noqa: F811
    """Re-correr reproduce la lectura: el plan aprobado es de donde sale, no una llamada."""
    store, engine, job_id, lector, vistas = _armar(tmp_path)
    _generar(store, engine, job_id)
    _aprobar(store, job_id)
    lector.permitido = False
    _puntuar(store, engine, job_id)
    primera = list(vistas)
    vistas.clear()
    _puntuar(store, engine, job_id)

    assert vistas == primera and lector.llamadas_prohibidas == 0


def test_judge_fingerprint_no_cambia(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    _correr(store, engine, job_id)
    meta = store.get(job_id).report["meta"]
    assert meta["judgeSha"] == _HUELLA_DEL_JUEZ == scoring.judge_fingerprint()


def test_council_fingerprint_no_cambia(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    _correr(store, engine, job_id)
    meta = store.get(job_id).report["meta"]
    assert meta["councilSha"] == _HUELLA_DEL_CONSEJO


def test_the_report_carries_the_two_new_hashes_beside_the_old_ones(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    _correr(store, engine, job_id)
    record = store.get(job_id)
    meta = record.report["meta"]

    assert meta["observableSha"] == lec.set_hash(
        e["hash_observable"] for e in record.approved_plan["lecturas"].values()
    )
    assert meta["reconSha"] == lec.set_hash(
        e["hash_recon"] for e in record.approved_plan["lecturas"].values()
    )
    assert meta["lecturas"] == {"congeladas": len(_probados(record)), "en_vivo": 0, "sin_lectura": 0}


def test_hash_observable_invalida_si_se_reescribe_la_regla():
    lectura = _lectura().as_dict()
    antes = lec.observable_hash(_criterio(), lectura, ("screenshot", "page_text"))
    reescrita = _criterio()
    reescrita.pass_criteria = "como mucho TRES"

    assert lec.observable_hash(reescrita, lectura, ("screenshot", "page_text")) != antes
    assert lec.observable_hash(_criterio(), _lectura(cumple="Tres o menos.").as_dict(), ("screenshot", "page_text")) != antes
    assert lec.observable_hash(_criterio(), lectura, ("page_text",)) != antes, "y si cambia el tipo de evidencia"
    assert lec.observable_hash(_criterio(), lectura, ("page_text", "screenshot")) == antes, "el orden no importa"
    otro_nombre = _criterio()
    otro_nombre.name = "Otro título"
    assert lec.observable_hash(otro_nombre, lectura, ("screenshot", "page_text")) == antes, \
        "un cambio que no toca la regla no la vence"


def test_a_frozen_reading_written_against_another_wording_is_not_used(tmp_path, env):  # noqa: F811
    store, engine, job_id, lector, vistas = _armar(tmp_path)
    _generar(store, engine, job_id)
    record = _aprobar(store, job_id)
    cid = _probados(record)[0]
    for c in record.approved_plan["criteria"]:
        if c["id"] == cid:
            c["passCriteria"] = "REGLA REESCRITA DESPUÉS DE CONGELAR"
    store.save(record)
    lector.permitido = False
    record = _puntuar(store, engine, job_id)

    fila = _fila(store, job_id, cid)
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: lectura congelada vencida]")
    assert lector.llamadas_prohibidas == 0, "no se reemplaza por una lectura nueva en silencio"
    assert "lectura congelada vencida" in record.report["meta"]["autochequeo"]["fallasDelEvaluador"]


def test_a_reading_nobody_decided_is_not_frozen_and_not_improvised(tmp_path, env):  # noqa: F811
    store, engine, job_id, lector, vistas = _armar(tmp_path, equivalentes=False)
    _generar(store, engine, job_id)
    record = _aprobar(store, job_id)
    cid = _probados(record)[0]

    assert record.approved_plan["lecturas"][cid]["lectura"] is None, "sin acuerdo no se congela n=1"
    lector.permitido = False
    record = _puntuar(store, engine, job_id)

    fila = _fila(store, job_id, cid)
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: lectura sin adjudicar]")
    assert "otro umbral" in fila["rationale"]
    assert vistas == [] and lector.llamadas_prohibidas == 0


def test_a_reading_a_person_decided_is_the_one_the_judge_gets(tmp_path, env):  # noqa: F811
    store, engine, job_id, lector, vistas = _armar(
        tmp_path, equivalentes=False,
        check_reading=lambda test, reading, client, *, model=None, sobre="": {
            "vaga": False, "parcial_definido": True, "anclas_contables": True, "objeciones": [], "sobre": sobre},
    )
    record = _generar(store, engine, job_id)
    runner = JobRunner(store, engine, None)
    for cid in _probados(record):
        asyncio.run(runner.edit_reading(store.get(job_id), cid, _lectura(cumple="Tres o menos.").as_dict(), by="E. M."))
    _aprobar(store, job_id)
    lector.permitido = False
    record = _puntuar(store, engine, job_id)

    assert _fila(store, job_id, _probados(record)[0])["verdict"] == "CUMPLE"
    assert vistas and all("Tres o menos." in v for v in vistas)


def test_an_old_plan_without_frozen_readings_is_scored_as_always_and_says_so(tmp_path, env):  # noqa: F811
    """Migración: un plan aprobado antes del contrato cae a la lectura generada al puntuar."""
    store, engine, job_id, lector, vistas = _armar(tmp_path, frozen_readings=False)
    _correr(store, engine, job_id)
    record = store.get(job_id)

    assert "lecturas" not in record.approved_plan
    assert _fila(store, job_id, _probados(record)[0])["verdict"] == "CUMPLE"
    assert vistas and all(v == _lectura().as_text() for v in vistas), "la lectura se generó al puntuar"
    meta = record.report["meta"]
    assert meta["lecturas"] == {"congeladas": 0, "en_vivo": len(_probados(record)), "sin_lectura": 0}
    assert "observableSha" not in meta and "reconSha" not in meta
    assert "no trae lecturas congeladas" in meta["lecturasAdvertencia"]


def test_replay_reads_the_frozen_readings_of_a_job_and_checks_them(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    _generar(store, engine, job_id)
    record = _aprobar(store, job_id)
    job_dir = store.dir_of(job_id)

    lecturas = replay.frozen_readings(job_dir)
    assert set(lecturas) == set(_probados(record))
    assert lecturas[_probados(record)[0]] == _lectura()

    path = store.evidence_dir(job_id) / "approved_plan.json"
    plan = json.loads(path.read_text(encoding="utf-8"))
    plan["lecturas"][_probados(record)[0]]["lectura"]["cumple"] = "TOCADA A MANO"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay.frozen_readings(job_dir)


def test_a_job_without_frozen_readings_has_none_to_replay(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, frozen_readings=False)
    _correr(store, engine, job_id)
    assert replay.frozen_readings(store.dir_of(job_id)) == {}


# --- Tarea 6: una compuerta determinista por TIPO de artefacto ----------------


def test_the_required_artifact_types_come_from_what_the_probes_capture():
    assert lec.required_artifacts([{"capture": ["screenshot", "page_text"]}, {"capture": ["page_text"]}]) == \
        ["page_text", "screenshot"]
    assert lec.required_artifacts([{}]) == ["page_text", "screenshot"], "sin dato, lo que una prueba guarda por defecto"
    assert lec.missing_artifacts(["page_text", "screenshot"], [{"kind": "screenshot"}]) == ["page_text"]
    assert lec.missing_artifacts(["page_text", "screenshot"], [{"kind": "screenshot"}, {"kind": "page_text"}]) == []


def test_the_reading_declares_what_evidence_its_probes_capture(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path)
    record = _generar(store, engine, job_id)
    for cid in _probados(record):
        assert record.readings[cid]["artefactos_requeridos"] == ["page_text", "screenshot"]
    record = _aprobar(store, job_id)
    assert record.approved_plan["lecturas"][_probados(record)[0]]["artefactos_requeridos"] == ["page_text", "screenshot"]


def _pedir_otro_tipo(store, job_id, cid, tipos):
    """Deja el plan aprobado pidiendo `tipos` para un criterio, con su huella al día."""
    from lxp_eval.service import serialize

    record = store.get(job_id)
    entrada = record.approved_plan["lecturas"][cid]
    entrada["artefactos_requeridos"] = list(tipos)
    criterios = serialize.criteria_from_plan(record.approved_plan["criteria"], record.objective)
    test = next(t for t in criterios.all_tests() if t.id == cid)
    entrada["hash_observable"] = lec.observable_hash(test, entrada["lectura"], tipos)
    store.save(record)


def test_gate_por_tipo_de_artefacto(tmp_path, env):  # noqa: F811
    """Si el tipo de evidencia que el plan pidió no está, el criterio no llega al juez."""
    store, engine, job_id, lector, vistas = _armar(tmp_path)
    _generar(store, engine, job_id)
    record = _aprobar(store, job_id)
    cid, otro = _probados(record)[0], _probados(record)[-1]
    _pedir_otro_tipo(store, job_id, cid, ["page_text", "screenshot", "video"])
    lector.permitido = False
    record = _puntuar(store, engine, job_id)

    fila = _fila(store, job_id, cid)
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: falta el tipo de evidencia]")
    assert "video" in fila["rationale"]
    if otro != cid:
        assert _fila(store, job_id, otro)["verdict"] == "CUMPLE", "los demás criterios se juzgan"
    assert cid not in engine.score_test.criterios, \
        "el juez no se llamó para las pruebas del criterio sin su evidencia"
    assert engine.score_test.criterios, "y sí para los demás"
    assert "falta el tipo de evidencia" in record.report["meta"]["autochequeo"]["fallasDelEvaluador"]


def test_precondicion_semantica_es_no_alcanza(tmp_path, env):  # noqa: F811
    """"¿Comparó dos distractores de la misma pregunta?" lo decide el juez leyendo, no una compuerta."""
    semantica = _lectura(no_alcanza="Un solo distractor por pregunta: hace falta comparar dos de la misma.")
    store, engine, job_id, lector, vistas = _armar(tmp_path, lector=_Lector(semantica))
    _generar(store, engine, job_id)
    _aprobar(store, job_id)
    lector.permitido = False
    record = _puntuar(store, engine, job_id)

    assert vistas, "el juez SÍ se llama: la precondición no es una compuerta"
    assert all("hace falta comparar dos de la misma" in v for v in vistas), "y la recibe en la lectura, como dato"
    fila = _fila(store, job_id, _probados(record)[0])
    assert not fila["rationale"].startswith("[causa: falta el tipo de evidencia]")


def test_recon_hash_invalida_superficie_cambiada():
    antes = [{"id": "P1", "sha256": "aaa"}, {"id": "P2", "sha256": "bbb"}]
    huella = lec.recon_hash(antes)

    assert lec.recon_hash(list(reversed(antes))) == huella, "el orden no importa"
    cambiada = [{"id": "P1", "sha256": "aaa"}, {"id": "P2", "sha256": "OTRA"}]
    assert lec.recon_hash(cambiada) != huella

    entrada = {"superficies": antes, "hash_recon": huella}
    assert lec.stale_surfaces(entrada, [{"id": "P1", "sha256": "aaa"}, {"id": "P2", "sha256": "bbb"}]) == []
    assert lec.stale_surfaces(entrada, [{"id": "P1", "sha256": "aaa"}, {"id": "P2", "sha256": "OTRA"}]) == ["P2"]
    assert lec.stale_surfaces(entrada, [{"id": "P1", "sha256": "aaa"}]) == ["P2"], "una superficie que ya no está"

    una = lec.freeze({"X-01": {**_estable(), "superficies": antes, "hash_recon": huella}}, [_criterio()])["X-01"]
    otra = lec.freeze(
        {"X-01": {**_estable(), "superficies": cambiada, "hash_recon": lec.recon_hash(cambiada)}}, [_criterio()],
    )["X-01"]
    assert una["hash_recon"] != otra["hash_recon"]
    assert una["hash_observable"] == otra["hash_observable"], \
        "que cambie una pantalla no vence la lectura de la regla: son dos huellas"
