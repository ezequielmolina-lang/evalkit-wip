"""El corredor de lotes: el bucle, el tope de gasto, y auto vs humano / con y sin compuerta.

Dos niveles de prueba, a propósito:

* El BUCLE se prueba con un `JobRunner` de mentira (`FakeRunner`): nos importa a
  quién se llama, en qué orden, cuántos a la vez y cuándo el tope corta, no lo
  que el motor hace adentro. Un runner real acá sólo agregaría ruido y minutos.

* El SPLIT con compuerta se prueba con el motor real sobre los MISMOS fakes que
  `test_service` (sin red, sin navegador): así la invariante que importa --una
  prueba con compuerta vuelve NO_EVALUADO bajo `auto`, una sin compuerta se
  puntúa-- se verifica contra el gate de verdad (`runnable_probes` + scoring),
  no contra una imitación. Reusar esos fakes es la regla del repo: las pruebas
  construyen lo que leen.
"""
from __future__ import annotations

import asyncio

import pytest

from lxp_eval.evalkit.framework import NO_EVALUADO
from lxp_eval.evalkit.generator import REVISE
from lxp_eval.service import serialize
from lxp_eval.service.batch import (
    ManifestEntry,
    load_manifest,
    resolve_credentials,
    run_batch,
)
from lxp_eval.service.jobs import Stage
from lxp_eval.service.store import JobStore

from tests.test_service import FakeAnthropic, make_engine


# --- a fake runner: the two engine seams, no engine --------------------------


class FakeRunner:
    """`build_plan` lands at awaiting_approval; `execute` finishes. Records calls.

    `spend` is charged ONCE, in `build_plan`, so one job costs exactly `spend` --
    which keeps the spend-cap arithmetic in the tests obvious. Each instance
    shares the same `calls` list so a test can read the whole batch's call order.
    """

    def __init__(self, store, engine, box, clock, *, calls, spend):
        self.store = store
        self.calls = calls
        self.spend = spend

    async def build_plan(self, record):
        record.criteria = []
        record.probes = []
        record.spend_usd = self.spend
        record.stage = Stage.AWAITING_APPROVAL
        self.store.save(record)
        self.calls.append(("build_plan", record.job_id))
        return record

    async def execute(self, record):
        record.stage = Stage.DONE
        record.report = {"rows": [], "meta": {"approvalPolicy": record.approval_policy}}
        self.store.save(record)
        self.calls.append(("execute", record.job_id))
        return record


def _fake_factory(calls, *, spend):
    def factory(store, engine, box, clock):
        return FakeRunner(store, engine, box, clock, calls=calls, spend=spend)
    return factory


def _entry(url="https://plat.test", *, objective="un tutor de matemática", **kw):
    return ManifestEntry(platform_url=url, objective=objective, **kw)


# --- the loop ----------------------------------------------------------------


def test_the_loop_drives_each_entry_through_build_plan(tmp_path):
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    entries = [_entry(f"https://p{i}.test") for i in range(3)]
    summary = asyncio.run(run_batch(
        entries, store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="human", max_parallel=2,
    ))
    assert len(summary.jobs) == 3
    assert [k for k, _ in calls].count("build_plan") == 3
    # every launched job exists on disk, stage set by the fake runner
    for o in summary.jobs:
        assert store.get(o.job_id) is not None


def test_human_policy_parks_and_never_executes(tmp_path):
    """Default `human` = hoy: se arma el plan y se espera. No corre execute."""
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    summary = asyncio.run(run_batch(
        [_entry()], store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="human", max_parallel=1,
    ))
    assert ("build_plan", summary.jobs[0].job_id) in calls
    assert not any(k == "execute" for k, _ in calls)
    assert store.get(summary.jobs[0].job_id).stage == Stage.AWAITING_APPROVAL


def test_auto_policy_executes_after_building(tmp_path):
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    summary = asyncio.run(run_batch(
        [_entry()], store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="auto", max_parallel=1,
    ))
    job_id = summary.jobs[0].job_id
    assert ("build_plan", job_id) in calls
    assert ("execute", job_id) in calls
    record = store.get(job_id)
    assert record.stage == Stage.DONE
    # the auto-approval wrote the plan and its machine-actor ledger row
    assert record.approved_plan is not None
    assert record.approved_at


def test_per_entry_policy_overrides_the_batch_default(tmp_path):
    """Una entrada con su propia política gana sobre el default del lote."""
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    entries = [
        _entry("https://a.test", approval_policy="auto"),
        _entry("https://b.test", approval_policy="human"),
    ]
    summary = asyncio.run(run_batch(
        entries, store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="human", max_parallel=1,
    ))
    by_platform = {o.platform: o for o in summary.jobs}
    assert store.get(by_platform["a.test"].job_id).stage == Stage.DONE
    assert store.get(by_platform["b.test"].job_id).stage == Stage.AWAITING_APPROVAL


# --- the spend cap -----------------------------------------------------------


def test_the_spend_cap_halts_new_launches(tmp_path):
    """Pasado el tope no se LANZA nada nuevo; lo lanzado termina.

    spend=$10 por trabajo, tope=$25, serie (max_parallel=1): tras el 2.º el total
    es $20 (<25) y el 3.º se lanza y lo cruza a $30; en la cuarta vuelta el total
    ya pasó el tope y no se lanza nada más. Tres de cinco.
    """
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    entries = [_entry(f"https://p{i}.test") for i in range(5)]
    summary = asyncio.run(run_batch(
        entries, store=store, runner_factory=_fake_factory(calls, spend=10.0),
        default_approval_policy="auto", max_parallel=1, max_spend_usd=25.0,
    ))
    assert summary.halted is True
    assert "tope de gasto" in summary.halted_reason.lower()
    assert len(summary.jobs) == 3
    assert summary.total_spend_usd == pytest.approx(30.0)
    # the two entries past the cap were never created
    assert len(store.list_ids()) == 3


def test_no_cap_runs_everything(tmp_path):
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    entries = [_entry(f"https://p{i}.test") for i in range(4)]
    summary = asyncio.run(run_batch(
        entries, store=store, runner_factory=_fake_factory(calls, spend=5.0),
        default_approval_policy="auto", max_parallel=2,
    ))
    assert summary.halted is False
    assert len(summary.jobs) == 4


# --- auto vs human / gated vs ungated, against the REAL gate ------------------


def _auto_run(tmp_path, engine):
    store = JobStore(tmp_path / "jobs")
    entry = _entry(objective="Un tutor de IA de matemática para 1.º a 6.º.",
                   approval_policy="auto")
    summary = asyncio.run(run_batch(
        [entry], store=store, engine=engine, box=None, max_parallel=2,
    ))
    return store, store.get(summary.jobs[0].job_id)


def test_auto_scores_ungated_probes_and_records_the_policy(tmp_path):
    """Sin compuerta: las pruebas corren solas y el informe dice que fue auto."""
    store, record = _auto_run(tmp_path, make_engine(tmp_path))  # safety = APPROVE
    assert record.stage == Stage.DONE
    assert record.report["meta"]["approvalPolicy"] == "auto"
    assert record.report["meta"]["autoApproved"] is True
    # The two OBSERVABLE criteria were scored; the REQUIERE_DOCUMENTACION one is
    # NO_EVALUADO by construction, never because of the gate.
    scored = [r for r in record.report["rows"] if r["verdict"] != NO_EVALUADO]
    assert len(scored) == 2


def test_auto_leaves_gated_probes_unevaluated(tmp_path):
    """Con compuerta: sin firma humana no corren, y el criterio vuelve NO_EVALUADO.

    Es la invariante dura del repo bajo política `auto`: el lote no firma nada,
    así que el gate de `runnable_probes` deja afuera toda prueba con compuerta.
    """
    engine = make_engine(tmp_path, client=FakeAnthropic(safety_verdict=REVISE))
    store, record = _auto_run(tmp_path, engine)
    assert record.stage == Stage.DONE
    assert record.report["meta"]["autoApproved"] is True
    # NOTHING was scored: every observable probe needed a signature the batch
    # never gives, so every row is NO_EVALUADO.
    scored = [r for r in record.report["rows"] if r["verdict"] != NO_EVALUADO]
    assert scored == []
    assert all(r["verdict"] == NO_EVALUADO for r in record.report["rows"])


def test_auto_approval_row_names_the_machine_actor(tmp_path):
    """La fila de aprobación va al ledger con el actor máquina, no humano."""
    store, record = _auto_run(tmp_path, make_engine(tmp_path))
    approvals = [
        p for p in record.report["provenance"] if p["kind"] == "approval"
    ]
    assert approvals, "no se registró la aprobación en la procedencia"
    line = approvals[0]["line"].lower()
    assert "autom" in line  # "aprobado automáticamente..."
    assert "evaluador humano" not in line


# --- manifest ----------------------------------------------------------------


def test_load_manifest_accepts_a_bare_list(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "- platform_url: https://a.test\n"
        "  objective: evaluar el tutor\n"
        "  approval_policy: auto\n",
        encoding="utf-8",
    )
    m = load_manifest(p)
    assert len(m.entries) == 1
    assert m.entries[0].platform_url == "https://a.test"
    assert m.entries[0].approval_policy == "auto"


def test_load_manifest_accepts_a_dict_with_settings(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "max_spend_usd: 50\n"
        "max_parallel: 3\n"
        "approval_policy: auto\n"
        "jobs:\n"
        "  - platform_url: https://a.test\n"
        "    objective: evaluar\n",
        encoding="utf-8",
    )
    m = load_manifest(p)
    assert m.max_spend_usd == 50.0
    assert m.max_parallel == 3
    assert m.approval_policy == "auto"
    assert len(m.entries) == 1


def test_load_manifest_rejects_a_bad_risk_policy(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text(
        "- platform_url: https://a.test\n"
        "  objective: evaluar\n"
        "  risk_policy: inventada\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="risk_policy"):
        load_manifest(p)


def test_load_manifest_rejects_an_entry_without_a_url(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("- objective: evaluar\n", encoding="utf-8")
    with pytest.raises(ValueError, match="platform_url"):
        load_manifest(p)


# --- credentials: a reference, resolved from the environment -----------------


def test_credentials_come_from_the_environment_not_the_manifest(monkeypatch):
    monkeypatch.setenv("EVALKIT_CRED_PLAT_A_USER", "cuenta-de-prueba")
    monkeypatch.setenv("EVALKIT_CRED_PLAT_A_PASS", "clave-de-prueba")
    assert resolve_credentials("plat-a") == ("cuenta-de-prueba", "clave-de-prueba")


def test_a_missing_credential_reference_is_none_not_an_error(monkeypatch):
    monkeypatch.delenv("EVALKIT_CRED_NADA_USER", raising=False)
    monkeypatch.delenv("EVALKIT_CRED_NADA_PASS", raising=False)
    assert resolve_credentials("nada") is None
    assert resolve_credentials("") is None
