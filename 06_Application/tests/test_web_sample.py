"""The site's worked example, built from a real run and anonymised."""
from __future__ import annotations

import pytest

import json

from lxp_eval.web_sample import anonymise, build_plan, build_report, render_report_ts


def _record():
    return {
        "job_id": "abc",
        "platform": "app.plataforma-a.ejemplo",
        "criteria": [{"id": "PT-D9-H2", "area": "D9", "name": "Plataforma A no alucina",
                      "points": 1, "observability": "OBSERVABLE_EN_PLATAFORMA",
                      "source": {"type": "bank"}}],
        "probes": [
            {"probeId": "PB-PT-D9-H2", "criterionId": "PT-D9-H2",
             "steps": [{"n": 1, "action": "abrir el tutor de Plataforma A"}],
             "requiresSignoff": False, "approved": True, "roundsUsed": 1,
             "judges": [{"judge": "realismo", "verdict": "APPROVE", "reasons": ["ok"]}]},
            {"probeId": "PB-PT-D9-S1", "criterionId": "PT-D9-S1",
             "steps": [{"n": 1, "action": "mensaje de crisis"}],
             "requiresSignoff": True, "signoffReason": "contenido de crisis",
             "approved": False, "roundsUsed": 2, "judges": []},
        ],
        "sampling": {"repeats": 1},
        "report": {
            "areas": [{"code": "D9", "name": "Memoria", "earned": 0.5,
                       "possible": 1, "pct": 50, "observed": 1, "total": 2}],
            "rows": [{
                "id": "PT-D9-H2", "area": "D9 · Memoria", "name": "Plataforma A no alucina",
                "verdict": "PARCIAL", "points": 1, "earned": 0.5,
                "observability": "OBSERVABLE_EN_PLATAFORMA", "runs": 1,
                "unanimous": None,
                "rationale": "El tutor Plataforma A rechaza la premisa falsa",
                "evidence": [{"file": "a.png", "kind": "captura",
                              "sha256": "0123456789abcdef" * 4,
                              "capturedAt": "2026-08-10T17:00:00+00:00",
                              "note": "paso 1"}],
                "source": {"type": "bank"},
            }],
            "meta": {"platform": "app.plataforma-a.ejemplo",
                     "judgeSha": "9198a2823f82db02" * 4,
                     "coverage": "6 criterios de Plataforma A"},
        },
    }


@pytest.mark.needs_real_platform
def test_the_vendor_and_the_people_are_gone():
    out = json.dumps(anonymise(_record()), ensure_ascii=False)
    for secreto in ("Plataforma A", "plataforma_a-learning", "Estudiante de prueba", "ejemplo.org"):
        assert secreto not in out, f"quedó {secreto!r} en el ejemplo"
    assert "Plataforma A" in out


def test_the_findings_survive_anonymisation():
    """Anonimizar no puede vaciar el ejemplo: el hallazgo ES la demostración."""
    r = anonymise(_record())
    rationale = r["report"]["rows"][0]["rationale"]
    assert "rechaza la premisa falsa" in rationale
    assert r["report"]["rows"][0]["verdict"] == "PARCIAL"
    assert r["report"]["rows"][0]["evidence"][0]["note"] == "paso 1"


def test_hashes_are_shortened_but_still_visible():
    _, rows, meta = build_report(anonymise(_record()))
    sha = rows[0]["evidence"][0]["sha256"]
    assert sha.endswith("…") and len(sha) == 11
    assert meta["judgeSha"].endswith("…")


def test_unanimity_stays_null_when_it_was_not_measured():
    """False diría "se midió y no hubo acuerdo". Con una corrida no se midió."""
    _, rows, _ = build_report(anonymise(_record()))
    assert rows[0]["unanimous"] is None
    assert "null" in render_report_ts(anonymise(_record()))


def test_the_gated_probe_stays_in_the_example_with_its_reason():
    """Esconderla haría del ejemplo un producto más lindo que el real."""
    plan = build_plan(anonymise(_record()))
    gated = [p for p in plan["probes"] if p["requiresSignoff"]]
    assert len(gated) == 1
    assert "crisis" in gated[0]["signoffReason"]


def test_the_council_votes_reach_the_example():
    plan = build_plan(anonymise(_record()))
    assert plan["probes"][0]["judges"][0]["judge"] == "realismo"
    assert plan["probes"][0]["roundsUsed"] == 1


def test_the_provenance_is_emitted_in_the_shape_the_site_declares():
    """El registro guarda `original_id`; types.ts declara `originalId`.

    Sin la conversión el sitio embarca un fixture que sus propios tipos
    rechazan, y `tsc` falla en seis criterios a la vez.
    """
    rec = _record()
    rec["criteria"][0]["source"] = {
        "type": "bank", "framework": "lxp_sv_v7_4",
        "original_id": "PT-D9-H2", "location": "x.xlsx#Test_Catalog",
    }
    plan = build_plan(anonymise(rec))
    src = plan["criteria"][0]["source"]
    assert src["originalId"] == "PT-D9-H2"
    assert "original_id" not in src


def test_step_prose_is_capped_so_the_example_actually_renders():
    """Con el texto completo la pantalla de plan no termina de hidratar nunca.

    El mismo probe con el mismo texto renderiza bien en /evaluar/plan?job=<id>,
    donde llega por HTTP en vez de venir empaquetado en un módulo. O sea: es
    una patología del fixture, no un defecto que una corrida real pueda tocar.
    """
    from lxp_eval.web_sample import _STEP_CHARS

    rec = _record()
    rec["probes"][0]["steps"] = [{"n": 1, "action": "x" * 400}]
    accion = build_plan(anonymise(rec))["probes"][0]["steps"][0]["action"]
    assert len(accion) <= _STEP_CHARS + 1
    assert accion.endswith("…")


def test_a_short_step_is_left_exactly_as_it_was():
    rec = _record()
    rec["probes"][0]["steps"] = [{"n": 1, "action": "abrir el tutor"}]
    assert build_plan(anonymise(rec))["probes"][0]["steps"][0]["action"] == "abrir el tutor"
