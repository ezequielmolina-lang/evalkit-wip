"""El camino "sólo el plan" por el servicio y por el lote, con dobles.

Lo que se fija: que ese camino se DETIENE antes de redactar una sola prueba, que
deja el documento con las capturas del recorrido en la carpeta del trabajo, y
que un trabajo parado ahí no se puede aprobar ni ejecutar por el camino viejo
(la aprobación del plan es de una persona, con cualquier política).

LO QUE ESTOS DOBLES NO VEN: que un explorador real registre de verdad cada
pantalla, que el planificador real cite las pantallas correctas, ni cuánto
cuesta. Eso se mira en una corrida contra una plataforma.
"""
from __future__ import annotations

import asyncio
import base64
import json

from lxp_eval.evalkit import planner as pl
from lxp_eval.service.batch import ManifestEntry, job_failed, run_batch, JobOutcome
from lxp_eval.service.store import JobStore
from lxp_eval.service.watchdog import ACTIVE_STAGES, TERMINAL_STAGES, Stage

from tests.test_service import (
    DRAFT_TOOL,
    FakeDriver,
    client_factory,  # noqa: F401 -- fixture
    env,  # noqa: F401 -- fixture
    make_engine,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class _DriverQueRegistra(FakeDriver):
    """Explora y deja una parte con su captura en la carpeta de artefactos."""

    async def explore(self, task: str, **kw) -> dict:
        if "INVENTARIO" not in task:
            return {"no_encontrado": ["nada nuevo"]}
        (self.dir / "recon_parte_01.png").write_bytes(PNG)
        return {
            "entrada": "Tocar el nombre guardado.",
            "partes": [{
                "nombre": "Después de un error", "rol": "estudiante",
                "como_llegar": "Responder mal", "que_muestra": "La hipótesis del tutor",
                "captura": "recon_parte_01.png", "sha256": "a" * 64,
            }],
        }


def _estrategia(criteria, *, profile, **kw):
    """Un planificador de prueba: prueba el primero en la pantalla vista."""
    primero = criteria[0]["id"]
    return pl.validate_strategy(
        [{"id": primero, "estado": "se_prueba", "partes": ["P1"], "motivo": "se vio",
          "que_se_prueba": "Responder mal y leer la corrección"}],
        criteria, profile,
    )


def _muestreo(chosen, **kw):
    return {c["id"]: {
        "sensible_al_tema": True, "n": 3, "confirmado": True, "objecion": "", "motivo": "varía",
        "advertencias": [],
        "celdas": [{"tema": "Monomios", "variante": "a"}, {"tema": "Potencias", "variante": "b"},
                   {"tema": "Ecuaciones", "variante": "c"}],
    } for c in chosen}


def _motor(tmp_path):
    engine = make_engine(
        tmp_path, plan_strategy=_estrategia, plan_sampling=_muestreo,
        # El recorrido dirigido tiene sus propias pruebas.
        plan_revisit=False,
    )

    def factory(job, credentials, artifacts_dir):
        d = _DriverQueRegistra(artifacts_dir, credentials=credentials)
        engine.test_drivers.append(d)
        return d

    engine.driver_factory = factory
    return engine


def _entrada():
    return ManifestEntry(
        platform_url="https://plataforma.test",
        objective="Un tutor de IA de matemática para 1.º a 6.º grado.",
        areas=["D9", "D1", "PRIV"],
        approval_policy="auto",
    )


def _correr(tmp_path, **kw):
    store = JobStore(tmp_path / "runs")
    engine = _motor(tmp_path)
    resumen = asyncio.run(run_batch(
        [_entrada()], store=store, engine=engine, box=None, plan_only=True, **kw
    ))
    return store, engine, resumen


def test_the_plan_only_path_stops_before_drafting_a_single_probe(tmp_path, env):  # noqa: F811
    store, engine, resumen = _correr(tmp_path)
    trabajo = resumen.jobs[0]
    registro = store.get(trabajo.job_id)

    assert registro.stage == Stage.PLAN_READY, registro.error
    assert registro.probes == [], "el plan se aprueba antes de redactar las pruebas"
    redacciones = [
        c for c in engine.test_client.calls if c["tools"][0]["name"] == DRAFT_TOOL["name"]
    ]
    assert redacciones == [], "se llamó al redactor antes de que una persona viera el plan"
    assert len(engine.test_drivers) == 1, "sólo el navegador del recorrido: nada se ejecutó"
    # Política `auto` y aun así no se ejecutó nada: aprobar el plan es de una persona.
    assert registro.report is None and not registro.approved_at


def test_the_document_lands_in_the_job_folder_with_the_recon_captures(tmp_path, env):  # noqa: F811
    store, _, resumen = _correr(tmp_path)
    trabajo = resumen.jobs[0]
    carpeta = store.dir_of(trabajo.job_id)

    plan = json.loads((carpeta / "plan.json").read_text(encoding="utf-8"))
    doc = (carpeta / "plan.html").read_text(encoding="utf-8")

    assert plan["version"] == 1 and plan["cambios"] == {"primera_version": True}
    assert plan["conteos"]["se_prueba"] == 1 and plan["conteos"]["pruebas"] == 3
    assert plan["superficies"][0]["nombre"] == "Después de un error"
    assert "data:image/" in doc, "la captura del recorrido está dentro del documento"
    assert "Monomios" in doc
    assert trabajo.plan_path.endswith("plan.html")
    assert job_failed(trabajo) is False, "plan listo es el final PEDIDO de este trabajo"

    registro = store.get(trabajo.job_id)
    assert registro.plan_document["conteos"]["se_prueba"] == 1
    assert registro.strategy and registro.sampling_matrix
    assert "Plan listo" in registro.progress["message"]


def test_a_persons_corrections_reach_the_walk_and_the_plan(tmp_path, env):  # noqa: F811
    correcciones = [{"tipo": "existe", "superficie": "Informes", "url": "/informes",
                     "nota": "está en el menú del docente"}]
    store, _, resumen = _correr(tmp_path, corrections=correcciones)
    registro = store.get(resumen.jobs[0].job_id)

    assert registro.plan_corrections == correcciones
    plan = json.loads((store.dir_of(registro.job_id) / "plan.json").read_text(encoding="utf-8"))
    assert plan["correcciones"] == correcciones
    # Nadie la recorrió (el doble no sabe): queda nombrada, no olvidada.
    assert "Informes" in registro.plan_document["sin_cubrir"]
    assert any(s["nombre"] == "Informes" and s["estado"] == "indefinido" for s in plan["superficies"])


def test_a_job_parked_at_the_plan_cannot_be_approved_the_old_way(tmp_path, client_factory):  # noqa: F811
    store, engine, resumen = _correr(tmp_path)
    c = client_factory(engine=engine, root=tmp_path / "runs")
    job_id = resumen.jobs[0].job_id

    r = c.post(f"/jobs/{job_id}/approve", json={"plan": {"criteria": [], "probes": []}})

    assert r.status_code >= 400, "aprobar un trabajo sin pruebas ejecutaría un plan vacío"
    assert store.get(job_id).stage == Stage.PLAN_READY


def test_plan_ready_waits_for_a_person_so_the_watchdog_leaves_it_alone():
    assert Stage.PLAN_READY not in ACTIVE_STAGES, "una persona leyendo no está colgada"
    assert Stage.PLAN_READY not in TERMINAL_STAGES, "todavía falta aprobar y correr"


def test_plan_ready_without_a_document_counts_as_a_failure():
    sin_documento = JobOutcome("j", "p", Stage.PLAN_READY, "human", 0.0, 0, 0, "")
    assert job_failed(sin_documento) is True
