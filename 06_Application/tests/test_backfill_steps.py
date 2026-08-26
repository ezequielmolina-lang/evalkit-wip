"""Writing the executed steps back to a job -- beside what was approved."""
from __future__ import annotations

import json

from lxp_eval.backfill_steps import backfill, divergence

PLATFORM = "app.plataforma-a.ejemplo"
APPROVED = ["ingresar con una cuenta de estudiante de prueba", "abrir el tutor"]
EXECUTED = ['hacer clic en el curso "BM - Matematicas Grado 3"',
            "hacer clic en el boton violeta del tutor"]


def _job(tmp_path, *, approved=APPROVED, executed=EXECUTED, probe_id="PB-PT-D8-01"):
    d = tmp_path / "job"
    (d / "evidence").mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({
        "job_id": "abc123",
        "platform": PLATFORM,
        "stage": "done",
        "updated_at": "2026-07-20T22:14:59+00:00",
        "probes": [{"probeId": probe_id, "criterionId": "PT-D8-01",
                    "steps": [{"n": i, "action": s} for i, s in enumerate(approved, 1)]}],
        "approved_plan": {"probes": [{"probeId": probe_id}], "criteria": []},
        "un_campo_heredado": {"que": "nadie conoce"},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    for i, step in enumerate(executed, start=1):
        rows.append({"kind": "screenshot", "probe_id": probe_id, "test_id": "PT-D8-01",
                     "platform": PLATFORM, "run_index": 0, "url": "https://plataforma_a",
                     "artifact": f"a{i}.png", "sha256": "x",
                     "note": f"paso {i}: {step}",
                     "timestamp": f"2026-07-20T16:3{i}:00+00:00"})
    (d / "evidence" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return d


def test_it_reports_the_divergence_without_writing_anything(tmp_path):
    d = _job(tmp_path)
    before = (d / "job.json").read_bytes()

    report = backfill(d, apply=False)

    assert (d / "job.json").read_bytes() == before
    (entry,) = report["probes"]
    assert entry["diverged"] is True
    assert entry["approvedSteps"] == APPROVED
    assert entry["executedSteps"] == EXECUTED


def test_applying_adds_executed_steps_and_nothing_else(tmp_path):
    """The whole safety property: what a human approved is not edited."""
    d = _job(tmp_path)
    before = json.loads((d / "job.json").read_text(encoding="utf-8"))

    backfill(d, apply=True)

    after = json.loads((d / "job.json").read_text(encoding="utf-8"))
    assert after["executed_steps"] == {"PB-PT-D8-01": EXECUTED}
    assert after["probes"] == before["probes"], "los pasos aprobados no se tocan"
    assert after["approved_plan"] == before["approved_plan"]
    assert after["updated_at"] == before["updated_at"]
    assert after["un_campo_heredado"] == before["un_campo_heredado"], \
        "un campo que el dataclass no conoce no puede desaparecer en el viaje"
    assert set(after) - set(before) == {"executed_steps"}


def test_the_original_is_backed_up_before_the_first_write(tmp_path):
    d = _job(tmp_path)
    original = (d / "job.json").read_bytes()

    backfill(d, apply=True)

    assert (d / "job.json.bak_pre_backfill").read_bytes() == original


def test_applying_twice_changes_nothing_the_second_time(tmp_path):
    d = _job(tmp_path)
    backfill(d, apply=True)
    once = (d / "job.json").read_bytes()

    backfill(d, apply=True)

    assert (d / "job.json").read_bytes() == once


def test_a_probe_that_matches_its_recording_is_not_reported_as_diverged(tmp_path):
    d = _job(tmp_path, approved=EXECUTED, executed=EXECUTED)

    report = backfill(d, apply=False)

    (entry,) = report["probes"]
    assert entry["diverged"] is False


def test_a_probe_with_no_recording_is_left_alone(tmp_path):
    """A probe that never ran has nothing to write back, and that is not an error."""
    d = _job(tmp_path, executed=[])

    report = backfill(d, apply=True)

    (entry,) = report["probes"]
    assert entry["executedSteps"] == []
    assert entry["diverged"] is False
    after = json.loads((d / "job.json").read_text(encoding="utf-8"))
    assert "PB-PT-D8-01" not in after.get("executed_steps", {})


def test_the_ledger_is_never_written_to(tmp_path):
    d = _job(tmp_path)
    before = (d / "evidence" / "ledger.jsonl").read_bytes()

    backfill(d, apply=True)

    assert (d / "evidence" / "ledger.jsonl").read_bytes() == before


def test_divergence_is_a_pure_function_of_record_and_rows():
    record = {"probes": [{"probeId": "PB-1", "steps": [{"n": 1, "action": "aprobado"}]}]}
    rows = [{"kind": "screenshot", "probe_id": "PB-1", "run_index": 0,
             "note": "paso 1: ejecutado", "artifact": "a.png",
             "timestamp": "2026-07-20T16:00:00+00:00"}]

    (entry,) = divergence(record, rows)

    assert entry["approvedSteps"] == ["aprobado"]
    assert entry["executedSteps"] == ["ejecutado"]
    assert entry["diverged"] is True
