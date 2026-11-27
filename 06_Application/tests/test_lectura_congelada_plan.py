"""La lectura congelada en el plan que ve la persona: esquema, persistencia, edición por la web y planes viejos.

EL CASO. La lectura de la regla se escribe al generar y se congela al aprobar
(ver test_lectura_congelada.py y test_lectura_congelada_puntaje.py). Faltaba
el tramo que la hace llegar a quien decide: que el estado público del trabajo
la traiga, que la web la pueda corregir por un camino propio, que el plan diga
con qué esquema se guardó, y que un trabajo anterior a todo esto se siga
pudiendo abrir y puntuar.

Todo corre contra dobles, con textos inventados. Lo que NO ven: la pantalla.
El contrato de datos de la web (tipos, cliente y ruta) se compila con `tsc`,
pero no hay todavía una pantalla de edición, y estas pruebas no la simulan.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from lxp_eval.evalkit import lectura as lec
from lxp_eval.evalkit import planner
from lxp_eval.service import serialize
from lxp_eval.service.app import create_app
from lxp_eval.service.store import JobStore

from tests.test_arreglos_corrida_2026_11_26 import _correr, _fila, env  # noqa: F401 -- fixture
from tests.test_lectura_congelada import _criterio, _estable, _generar, _lectura
from tests.test_lectura_congelada_puntaje import _aprobar, _armar, _probados
from tests.test_service import AUTH


def _web(store, engine):
    app = create_app(root=store.root, engine=engine, background=False)
    return TestClient(app, headers=dict(AUTH))


def _limpio(test, reading, client, *, model=None, sobre=lec.SOBRE_MODELO):
    return {"vaga": False, "parcial_definido": True, "anclas_contables": True, "objeciones": [], "sobre": sobre}


# --- el estado público y la edición por la web --------------------------------


def test_the_public_plan_carries_the_readings_for_the_web(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    record = _generar(store, engine, job_id)
    with _web(store, engine) as web:
        plan = web.get(f"/jobs/{job_id}").json()["plan"]

    assert plan["esquema"] == serialize.PLAN_SCHEMA == 2
    por_criterio = {x["criterio"]: x for x in plan["lecturas"]}
    assert set(por_criterio) == set(_probados(record))
    una = por_criterio[_probados(record)[0]]
    assert una["estado"] == lec.ESTABLE and una["congelable"] is True
    assert una["lectura"]["cumple"] == "Cuatro o menos."
    assert len(una["muestras"]) == 2 and una["aviso"] == ""
    assert una["artefactos_requeridos"] == ["page_text", "screenshot"]
    assert una["hash_lectura"] == lec.reading_hash(una["lectura"])


def test_a_job_made_with_the_contract_off_shows_no_readings(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, frozen_readings=False)
    _generar(store, engine, job_id)
    with _web(store, engine) as web:
        plan = web.get(f"/jobs/{job_id}").json()["plan"]
    assert plan["lecturas"] == []


def test_the_web_edits_a_reading_through_its_own_endpoint(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    record = _generar(store, engine, job_id)
    cid = _probados(record)[0]
    pruebas = store.get(job_id).probes

    with _web(store, engine) as web:
        r = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M.", "changes": {"cumple": "Tres o menos."}})
        assert r.status_code == 200, r.text
        vista = next(x for x in r.json()["plan"]["lecturas"] if x["criterio"] == cid)

    assert vista["estado"] == lec.EDITADA and vista["editada_por"] == "E. M."
    assert vista["lectura"]["cumple"] == "Tres o menos." and vista["versiones_anteriores"] == 1
    guardado = store.get(job_id)
    assert guardado.readings[cid]["lectura"]["cumple"] == "Tres o menos."
    assert guardado.probes == pruebas, "editar la lectura no toca las pruebas"


def test_the_web_picks_one_of_two_samples(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, equivalentes=False, check_reading=_limpio)
    record = _generar(store, engine, job_id)
    cid = _probados(record)[0]
    with _web(store, engine) as web:
        r = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M.", "sample": 2})
    assert r.status_code == 200, r.text
    assert store.get(job_id).readings[cid]["estado"] == lec.ADJUDICADA


def test_a_bad_edit_is_told_what_is_wrong_and_changes_nothing(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    record = _generar(store, engine, job_id)
    cid = _probados(record)[0]
    antes = store.get(job_id).readings[cid]
    with _web(store, engine) as web:
        mal_escrito = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M.", "changes": {"cumpel": "Tres."}})
        sin_nombre = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": " ", "changes": {"cumple": "Tres."}})
        sin_extremo = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M.", "changes": {"cumple": ""}})
        sin_nada = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M."})
        otro = web.post(f"/jobs/{job_id}/lecturas/NO-EXISTE", json={"by": "E. M.", "changes": {"cumple": "Tres."}})

    assert mal_escrito.status_code == 422 and "cumpel" in mal_escrito.text
    assert sin_nombre.status_code == 422 and sin_extremo.status_code == 422
    assert sin_nada.status_code == 409 and otro.status_code == 404
    assert store.get(job_id).readings[cid] == antes


def test_a_reading_cannot_be_edited_once_the_plan_is_approved(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    _correr(store, engine, job_id)
    cid = _probados(store.get(job_id))[0]
    with _web(store, engine) as web:
        r = web.post(f"/jobs/{job_id}/lecturas/{cid}", json={"by": "E. M.", "changes": {"cumple": "Tres."}})
    assert r.status_code == 409 and "antes de aprobar" in r.text


# --- el esquema del plan ------------------------------------------------------


def test_the_approved_plan_says_which_schema_it_was_saved_with(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    _generar(store, engine, job_id)
    con = _aprobar(store, job_id).approved_plan
    assert con["esquema"] == 2 and "lecturas" in con

    store2, engine2, job2, _, _ = _armar(tmp_path / "viejo", frozen_readings=False)
    _generar(store2, engine2, job2)
    sin = _aprobar(store2, job2).approved_plan
    assert "esquema" not in sin and "lecturas" not in sin, "un plan sin lecturas se guarda como siempre"


def test_the_plan_document_shows_the_reading_once_it_exists():
    plan = {"version": 1, "criterios": [{"id": "X-01", "estado": "se_prueba"}, {"id": "X-02", "estado": "se_prueba"}]}
    out = planner.with_readings(plan, {"X-01": _estable()})

    assert out["esquema"] == 2
    assert out["criterios"][0]["lectura"]["lectura"]["cumple"] == "Cuatro o menos."
    assert "lectura" not in out["criterios"][1], "un criterio sin lectura no recibe una vacía"
    assert "esquema" not in plan and "lectura" not in plan["criterios"][0], "no se toca el plan que entró"
    assert planner.with_readings(plan, {}) == plan


def test_the_plan_document_on_disk_is_refreshed_after_generating(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, check_reading=_limpio)
    antes = json.loads((store.dir_of(job_id) / "plan.json").read_text(encoding="utf-8"))
    assert "esquema" not in antes
    record = _generar(store, engine, job_id)

    plan = json.loads((store.dir_of(job_id) / "plan.json").read_text(encoding="utf-8"))
    assert plan["esquema"] == 2 and plan["version"] == antes["version"], "mismo plan, con las lecturas"
    con_lectura = [c for c in plan["criterios"] if c.get("lectura")]
    assert {c["id"] for c in con_lectura} == set(_probados(record))
    html = (store.dir_of(job_id) / "plan.html").read_text(encoding="utf-8")
    assert "Lectura de la regla" in html and "Cuatro o menos." in html


def test_with_the_contract_off_the_plan_document_is_left_alone(tmp_path, env):  # noqa: F811
    store, engine, job_id, _, _ = _armar(tmp_path, frozen_readings=False)
    antes = (store.dir_of(job_id) / "plan.json").read_bytes()
    _generar(store, engine, job_id)
    assert (store.dir_of(job_id) / "plan.json").read_bytes() == antes


# --- planes y trabajos viejos -------------------------------------------------


def test_an_old_job_file_without_readings_still_loads_and_scores(tmp_path, env):  # noqa: F811
    """Migración: un job.json de antes del contrato no tiene `readings` ni lecturas en su plan."""
    store, engine, job_id, _, _ = _armar(tmp_path, frozen_readings=False)
    _correr(store, engine, job_id)
    path = store.dir_of(job_id) / "job.json"
    viejo = json.loads(path.read_text(encoding="utf-8"))
    viejo.pop("readings", None)
    path.write_text(json.dumps(viejo, ensure_ascii=False), encoding="utf-8")

    record = JobStore(store.root).get(job_id)
    assert record.readings == {} and "lecturas" not in record.approved_plan
    with _web(store, engine) as web:
        estado = web.get(f"/jobs/{job_id}").json()
    assert estado["plan"]["lecturas"] == []
    assert estado["report"]["meta"]["lecturas"]["en_vivo"] == len(_probados(record))
    assert "no trae lecturas congeladas" in estado["report"]["meta"]["lecturasAdvertencia"]
    assert _fila(store, job_id, _probados(record)[0])["verdict"] == "CUMPLE"


def test_the_public_view_of_a_reading_never_offers_one_that_cannot_be_frozen():
    sin_acuerdo = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
    )["X-01"]
    vista = lec.public_view("X-01", sin_acuerdo)
    assert vista["congelable"] is False and vista["lectura"] is None
    assert len(vista["muestras"]) == 2 and "otro umbral" in vista["aviso"]
