"""Reproducir una grabación guardada por el runner de verdad, en cada cambio.

`test_replay.py` prueba el `ReplayDriver` con grabaciones que arma el propio
test. Esto es lo otro: una grabación REAL, guardada en el repo, que pasa por
`runner.run_probe` sin tocar nada. Si el lazo de captura, el nombrado de
artefactos o la escritura del ledger cambian de forma, esto se entera acá y no
en una corrida paga.

Es el complemento de `test_driver_contract.py`, y cada uno ve lo que el otro no:
el contrato mira el navegador con una página local; esto mira todo lo que pasa
DESPUÉS de capturar, con un navegador ausente. Una grabación está río abajo del
navegador, así que este archivo no puede ver un selector que se movió ni un
login que cambió de forma. Decirlo importa: un arnés en el que se confía de más
es peor que ninguno.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.replay import ReplayDrift, ReplayDriver, steps_as_run
from lxp_eval.evalkit.runner import run_probe

GRABACION = Path(__file__).parent / "fixtures" / "grabacion_probe.jsonl"
PLATAFORMA = "app.plataforma-a.ejemplo"
PROBE = "PB-PT-D9-H2"


def _rows():
    return [
        json.loads(l)
        for l in GRABACION.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def _driver(**kw):
    return ReplayDriver(_rows(), probe_id=PROBE, root=GRABACION.parent, **kw)


def _probe(steps):
    return Probe(
        probe_id=PROBE, test_id="PT-D9-H2", goal="ver si acepta una premisa falsa",
        steps=list(steps), verdict_rule="cumple si la corrige",
    )


@pytest.mark.asyncio
async def test_a_recorded_probe_replays_through_the_real_runner(tmp_path):
    """El lazo entero -- capturar, nombrar, ledgerear -- sin navegador."""
    driver = _driver()
    fresh = EvidenceLedger(tmp_path / "ledger.jsonl")

    await run_probe(
        _probe(driver.recorded_steps), driver, fresh,
        platform=PLATAFORMA, text_dir=tmp_path / "page_text",
    )

    filas = fresh.read_all()
    assert filas, "no se escribió ninguna fila"
    assert {f["kind"] for f in filas} == {"screenshot", "page_text"}
    assert all(f["probe_id"] == PROBE for f in filas)
    # Una fila por captura y por paso: si el lazo empieza a escribir de más o de
    # menos, la cuenta cambia y esto lo dice.
    assert len(filas) == 2 * len(driver.recorded_steps)


@pytest.mark.asyncio
async def test_every_ledger_row_still_says_which_step_produced_it(tmp_path):
    """La nota es lo que hace auditable a la evidencia.

    Sin ella una captura es un archivo suelto: el informe puede citarla pero
    nadie puede decir qué se hizo para obtenerla.
    """
    driver = _driver()
    fresh = EvidenceLedger(tmp_path / "ledger.jsonl")

    await run_probe(
        _probe(driver.recorded_steps), driver, fresh,
        platform=PLATAFORMA, text_dir=tmp_path / "page_text",
    )

    for fila in fresh.read_all():
        assert fila["note"].startswith("paso "), fila["note"][:60]
        assert fila["sha256"], "una fila sin hash no es evidencia"


@pytest.mark.asyncio
async def test_rewriting_the_probe_is_caught_instead_of_served(tmp_path):
    """La guarda que hace honesta a la reproducción.

    Una grabación describe los pasos que la produjeron. Servirla para un probe
    reescrito da verde mientras la evidencia habla de otra cosa -- la misma
    mentira que un consejo que declara haber aprobado un texto que nunca leyó.
    """
    driver = _driver()
    reescrito = list(driver.recorded_steps)
    reescrito[0] = "Abrir una leccion de LENGUAJE en vez de matematica"

    with pytest.raises(ReplayDrift):
        await run_probe(
            _probe(reescrito), driver, EvidenceLedger(tmp_path / "l.jsonl"),
            platform=PLATAFORMA, text_dir=tmp_path / "t",
        )


def test_the_recording_still_describes_the_probe_it_recorded():
    """Los pasos se leen de vuelta del ledger, que es donde sobreviven."""
    pasos = steps_as_run(_rows(), PROBE)
    assert pasos, "la grabación no dice qué pasos corrió"
    assert any("89+47" in p for p in pasos), (
        "esta grabación es la de la premisa aritmética falsa; si eso cambió, "
        "cambió el fixture y no el código"
    )


@pytest.mark.asyncio
async def test_replay_costs_nothing(tmp_path):
    """Un lazo que factura es un lazo que nadie corre en cada cambio."""
    driver = _driver()
    await run_probe(
        _probe(driver.recorded_steps), driver, EvidenceLedger(tmp_path / "l.jsonl"),
        platform=PLATAFORMA, text_dir=tmp_path / "t",
    )
    assert driver.spend_usd == 0.0
