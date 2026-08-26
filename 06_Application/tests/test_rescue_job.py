"""Rebuilding verdicts from archived evidence, without pretending it was a run."""
from __future__ import annotations

import json
from types import SimpleNamespace

from lxp_eval.evalkit.rescue import CAPTURE_TRUNCATION_MARKER
from lxp_eval.evalkit.scoring import CUMPLE
from lxp_eval.rescue_job import BANNER, load_job, reconstruct, render_markdown

PLATFORM = "app.plataforma-a.ejemplo"


class FakeClient:
    """Stands in for anthropic.Anthropic. Returns canned tool-use."""

    def __init__(self, *tool_inputs: dict):
        self._inputs = list(tool_inputs)
        self.call_count = 0
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent):
            self._parent = parent

        def create(self, **kwargs):
            p = self._parent
            data = p._inputs[p.call_count % len(p._inputs)]
            p.call_count += 1
            block = SimpleNamespace(type="tool_use", name="emitir_veredicto", input=data)
            return SimpleNamespace(content=[block], stop_reason="tool_use")


def _job(tmp_path, *, rows, criteria):
    """A job directory shaped like a real one: job.json plus an evidence ledger."""
    d = tmp_path / "jobs" / "abc123"
    (d / "evidence").mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({
        "job_id": "abc123",
        "platform": PLATFORM,
        "objective": "Evaluar Plataforma A",
        "stage": "done",
        "approved_at": "2026-07-20T22:12:07+00:00",
        "probes": [{"probeId": f"PB-{i}"} for i in range(8)],
        "approved_plan": {"criteria": criteria, "probes": [{"probeId": "PB-0"}]},
    }, ensure_ascii=False), encoding="utf-8")
    (d / "evidence" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    return d


def _crit(cid, name="un criterio"):
    return {"id": cid, "area": "D9", "name": name, "points": 10,
            "verification": name, "passCriteria": "cumple", "failCriteria": "no cumple"}


def _row(tmp_path, *, test_id, stamp, body, kind="page_text"):
    p = tmp_path / f"{test_id}_{stamp.replace(':', '')}.txt"
    p.write_text(body, encoding="utf-8")
    return {"kind": kind, "test_id": test_id, "probe_id": f"PB-{test_id}",
            "platform": PLATFORM, "timestamp": stamp, "artifact": str(p),
            "sha256": "x", "note": "un paso"}


def test_a_criterion_with_usable_evidence_gets_a_verdict(tmp_path):
    ev = _row(tmp_path, test_id="PT-D8-01", stamp="2026-07-20T16:35:09+00:00",
              body="la leccion completa, con su indicador de logro")
    d = _job(tmp_path, rows=[ev], criteria=[_crit("PT-D8-01")])
    client = FakeClient({"verdict": CUMPLE, "rationale": "se ve la leccion",
                         "cited_artifacts": [ev["artifact"]]})

    result = reconstruct(d, client=client)

    (entry,) = result["entries"]
    assert entry["verdict"] == CUMPLE
    assert entry["citedArtifacts"] == [ev["artifact"]]
    assert entry["selection"]["kept"] == 1


def test_a_criterion_whose_evidence_was_truncated_is_not_a_failed_criterion(tmp_path):
    """The whole point. PT-D9-H1 has rows and no usable text.

    Reporting that as an unmet criterion blames the vendor for our own capture
    bug. It has to come back as NOT RECONSTRUCTED, with the reason.
    """
    ev = _row(tmp_path, test_id="PT-D9-H1", stamp="2026-07-20T12:49:00+00:00",
              body=f"solo el indice {CAPTURE_TRUNCATION_MARKER}: se omitieron 16213 de 24213 caracteres]")
    d = _job(tmp_path, rows=[ev], criteria=[_crit("PT-D9-H1")])
    client = FakeClient({"verdict": CUMPLE, "rationale": "no deberia llamarse",
                         "cited_artifacts": []})

    result = reconstruct(d, client=client)

    (entry,) = result["entries"]
    assert entry["verdict"] is None
    assert "truncada" in entry["reason"]
    assert client.call_count == 0, "no se gasta una llamada sobre evidencia inservible"


def test_the_job_record_is_never_touched(tmp_path):
    """That report is the audit record of what actually happened."""
    ev = _row(tmp_path, test_id="PT-D8-01", stamp="2026-07-20T16:35:09+00:00", body="contenido")
    d = _job(tmp_path, rows=[ev], criteria=[_crit("PT-D8-01")])
    before = (d / "job.json").read_bytes()
    ledger_before = (d / "evidence" / "ledger.jsonl").read_bytes()

    reconstruct(d, client=FakeClient({"verdict": CUMPLE, "rationale": "ok",
                                      "cited_artifacts": [ev["artifact"]]}))

    assert (d / "job.json").read_bytes() == before
    assert (d / "evidence" / "ledger.jsonl").read_bytes() == ledger_before, \
        "una reconstrucción no es una decisión humana: no lleva fila de gobernanza"


def test_only_restricts_which_criteria_are_judged(tmp_path):
    rows = [
        _row(tmp_path, test_id="PT-D8-01", stamp="2026-07-20T16:35:09+00:00", body="uno"),
        _row(tmp_path, test_id="PT-D9-M2", stamp="2026-07-20T12:53:23+00:00", body="dos"),
    ]
    d = _job(tmp_path, rows=rows, criteria=[_crit("PT-D8-01"), _crit("PT-D9-M2")])
    client = FakeClient({"verdict": CUMPLE, "rationale": "ok",
                         "cited_artifacts": [rows[1]["artifact"]]})

    result = reconstruct(d, client=client, only=("PT-D9-M2",))

    assert [e["testId"] for e in result["entries"]] == ["PT-D9-M2"]
    assert client.call_count == 1


def test_the_artifact_says_what_it_is_on_its_first_line(tmp_path):
    """A reader who sees only this file must not mistake it for an evaluation."""
    ev = _row(tmp_path, test_id="PT-D8-01", stamp="2026-07-20T16:35:09+00:00", body="contenido")
    d = _job(tmp_path, rows=[ev], criteria=[_crit("PT-D8-01")])

    result = reconstruct(d, client=FakeClient({"verdict": CUMPLE, "rationale": "ok",
                                               "cited_artifacts": [ev["artifact"]]}))
    md = render_markdown(result)

    assert md.splitlines()[0] == f"# {BANNER}"
    assert "Nadie aprobó esta reconstrucción" in md
    assert "1 de 8 pruebas" in md, "tiene que decir cuánto del plan corrió de verdad"
    assert json.loads(json.dumps(result))["kind"] == "reconstruction"


def test_the_source_run_is_reported_honestly(tmp_path):
    ev = _row(tmp_path, test_id="PT-D8-01", stamp="2026-07-20T16:35:09+00:00", body="contenido")
    d = _job(tmp_path, rows=[ev], criteria=[_crit("PT-D8-01")])

    result = reconstruct(d, client=FakeClient({"verdict": CUMPLE, "rationale": "ok",
                                               "cited_artifacts": [ev["artifact"]]}))

    assert result["sourceRun"]["probesInApprovedPlan"] == 1
    assert result["sourceRun"]["probesOnRecord"] == 8
    assert load_job(d)["stage"] == "done"
