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
import json
from pathlib import Path

import pytest

from lxp_eval.evalkit.framework import NO_EVALUADO
from lxp_eval.evalkit.generator import REVISE
from lxp_eval.evalkit.ledger import EvidenceEntry, EvidenceLedger
from lxp_eval.service import batch as batch_mod
from lxp_eval.service.batch import (
    EXIT_BAD_MANIFEST,
    EXIT_HALTED,
    EXIT_JOB_FAILED,
    EXIT_OK,
    SETUP_ERROR,
    BatchSummary,
    JobOutcome,
    ManifestEntry,
    exit_code_for,
    job_failed,
    load_manifest,
    resolve_credentials,
    run_batch,
)
from lxp_eval.service.jobs import Stage
from lxp_eval.service.store import JobStore

from tests.test_service import FakeAnthropic, make_engine, real_consequence_framework


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


def test_the_halt_reason_quotes_the_final_spend(tmp_path):
    """El motivo del corte cita el gasto FINAL, con los que estaban en vuelo.

    Dos a la vez, tope=$10, $10 por trabajo, y el segundo tarda: cuando el
    primero termina el tope ya saltó y el segundo sigue en vuelo. El motivo se
    armaba en ese instante y decía $10 mientras el total del mismo resumen decía
    $20. Tiene que decir lo mismo que el total.
    """
    calls: list = []
    made = []

    class SlowSecond(FakeRunner):
        async def build_plan(self, record):
            if self.slow:
                await asyncio.sleep(0.05)
            return await super().build_plan(record)

    def factory(store, engine, box, clock):
        runner = SlowSecond(store, engine, box, clock, calls=calls, spend=10.0)
        runner.slow = len(made) == 1          # the second job launched
        made.append(runner)
        return runner

    store = JobStore(tmp_path / "jobs")
    entries = [_entry(f"https://p{i}.test") for i in range(4)]
    summary = asyncio.run(run_batch(
        entries, store=store, runner_factory=factory,
        default_approval_policy="auto", max_parallel=2, max_spend_usd=10.0,
    ))
    assert summary.halted is True
    assert len(summary.jobs) == 2
    assert summary.total_spend_usd == pytest.approx(20.0)
    assert "$20.00" in summary.halted_reason
    assert "2 de 4" in summary.halted_reason      # how many were never launched


# --- one bad entry does not take the batch down ------------------------------
#
# Found by code review, before a real run could: per-entry setup used to
# re-raise, so a missing document path in ONE entry killed a 100-job weekly run
# with a traceback and no summary. Each of these would have been that crash.


def _run(coro):
    """Run with a ceiling: a leaked semaphore permit shows up as a HANG, and a
    hung test suite is the worst way to learn about it."""
    async def bounded():
        return await asyncio.wait_for(coro, timeout=20)
    return asyncio.run(bounded())


def test_a_missing_document_fails_that_entry_not_the_batch(tmp_path):
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    entries = [
        _entry("https://a.test"),
        _entry("https://b.test", documents=[
            {"name": "Pliego", "path": str(tmp_path / "no-existe.pdf")},
        ]),
        _entry("https://c.test"),
    ]
    summary = _run(run_batch(
        entries, store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="auto", max_parallel=1,
    ))
    # manifest order is kept, and the bad entry is LISTED, with its reason
    assert [o.platform for o in summary.jobs] == ["a.test", "b.test", "c.test"]
    bad = summary.jobs[1]
    assert bad.stage == SETUP_ERROR
    assert bad.job_id == ""
    assert "Pliego" in bad.error and "no existe" in bad.error
    # the other two ran to the end -- including the one AFTER the failure,
    # which with max_parallel=1 proves the permit was given back
    assert summary.jobs[0].stage == Stage.DONE
    assert summary.jobs[2].stage == Stage.DONE
    # no orphan record was left behind for the entry that never started
    assert len(store.list_ids()) == 2


def test_a_failure_after_the_record_exists_is_written_on_the_record(
    tmp_path, monkeypatch
):
    """Si el registro ya existe, el motivo queda EN el registro.

    Sin eso quedaría en su etapa inicial con latido fresco, y la reconciliación
    del servicio lo reportaría después como "murió el worker" -- un motivo falso.
    """
    monkeypatch.setenv("EVALKIT_CRED_ROTA_USER", "cuenta")
    monkeypatch.setenv("EVALKIT_CRED_ROTA_PASS", "clave-de-prueba")

    class FakeBox:
        def encrypt_credentials(self, user, password):
            return b"cifrado"

    class DiskFull(JobStore):
        def put_credentials(self, job_id, blob):
            raise OSError("no queda espacio en el disco")

    calls: list = []
    store = DiskFull(tmp_path / "jobs")
    entries = [
        _entry("https://a.test", credentials_ref="rota"),
        _entry("https://b.test"),
    ]
    summary = _run(run_batch(
        entries, store=store, box=FakeBox(),
        runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="auto", max_parallel=1,
    ))
    failed, ok = summary.jobs
    assert failed.job_id, "el registro existía: tiene que listarse con su id"
    record = store.get(failed.job_id)
    assert record.stage == Stage.ERROR
    assert "no se pudo preparar" in record.error["message"]
    assert "no queda espacio" in record.error["message"]
    assert record.error["kind"] == "OSError"
    # never started, so the runner never saw it -- and the batch went on
    assert ("build_plan", failed.job_id) not in calls
    assert ok.stage == Stage.DONE
    # the credential itself is nowhere in what the summary or the record carry
    assert "clave-de-prueba" not in failed.error
    assert "clave-de-prueba" not in str(record.error)


def test_an_oversized_document_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_mod, "MAX_DOCUMENT_BYTES", 10)
    big = tmp_path / "volcado.txt"
    big.write_text("x" * 500, encoding="utf-8")
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    summary = _run(run_batch(
        [_entry(documents=[{"name": "Volcado", "path": str(big)}])],
        store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="auto", max_parallel=1,
    ))
    assert summary.jobs[0].stage == SETUP_ERROR
    assert "Volcado" in summary.jobs[0].error
    assert "supera el límite" in summary.jobs[0].error
    assert calls == []


def test_a_document_path_is_read_by_its_extension_not_its_title(tmp_path):
    """Un título sin extensión más una ruta: el caso del manifiesto de ejemplo.

    `extract_text` elige el lector por la extensión del nombre que recibe. Pasarle
    el título ("Lineamiento pedagógico") rechazaba el archivo como formato no
    soportado; tiene que recibir el nombre del ARCHIVO.
    """
    doc = tmp_path / "lineamiento.txt"
    doc.write_text("Una idea por segmento.", encoding="utf-8")
    calls: list = []
    store = JobStore(tmp_path / "jobs")
    summary = _run(run_batch(
        [_entry(documents=[{"name": "Lineamiento pedagógico", "path": str(doc)}])],
        store=store, runner_factory=_fake_factory(calls, spend=1.0),
        default_approval_policy="auto", max_parallel=1,
    ))
    assert summary.jobs[0].stage == Stage.DONE, summary.jobs[0].error
    record = store.get(summary.jobs[0].job_id)
    # the record keeps {name, text} -- the title, the extracted text, no path
    assert record.documents == [
        {"name": "Lineamiento pedagógico", "text": "Una idea por segmento."}
    ]


@pytest.mark.parametrize(
    "n_entries",
    [
        # every entry launched: the cancel lands while the batch WAITS for jobs
        pytest.param(1, id="while-draining"),
        # the second entry is still queued behind the one slot: the cancel lands
        # in the LAUNCH loop, and the queued entry must never be created
        pytest.param(2, id="while-launching"),
    ],
)
def test_cancelling_the_batch_cancels_what_is_in_flight(tmp_path, n_entries):
    """Frenar el lote no deja trabajos corriendo sin nadie que los espere."""

    class Hangs(FakeRunner):
        async def build_plan(self, record):
            self.started.set()
            await asyncio.Event().wait()          # never returns on its own

    store = JobStore(tmp_path / "jobs")

    async def scenario():
        started = asyncio.Event()

        def factory(store_, engine, box, clock):
            runner = Hangs(store_, engine, box, clock, calls=[], spend=0.0)
            runner.started = started
            return runner

        task = asyncio.ensure_future(run_batch(
            [_entry(f"https://p{i}.test") for i in range(n_entries)],
            store=store, runner_factory=factory,
            default_approval_policy="auto", max_parallel=1,
        ))
        await asyncio.wait_for(started.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        # nothing of the batch is left running behind the cancelled launcher
        leftovers = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        assert leftovers == [], [t.get_name() for t in leftovers]

    asyncio.run(scenario())
    (job_id,) = store.list_ids()                  # the queued entry never existed
    record = store.get(job_id)
    # the supervisor wrote WHY on the job itself: cancelled, not left hanging
    assert record.stage == Stage.ERROR
    assert record.error["kind"] == "CancelledError"


# --- the exit code: the only thing a scheduler reads -------------------------
#
# The CLI used to return 0 whenever it could read the manifest, whatever became
# of the jobs. Under a scheduled task nobody reads the summary; the exit code IS
# the report. A week of failed jobs looked green.


def _outcome_of(stage, policy="auto", **kw):
    return JobOutcome(
        job_id=kw.pop("job_id", "abc"), platform="p.test", stage=stage,
        approval_policy=policy, spend_usd=0.0,
        no_evaluado=kw.pop("no_evaluado", 0), scored=kw.pop("scored", 0),
        error=kw.pop("error", ""),
    )


@pytest.mark.parametrize(
    "stage, policy, failed",
    [
        (Stage.DONE, "auto", False),
        (Stage.DONE, "human", False),
        # the end a `human` job was ASKED to reach: waiting for a person
        (Stage.AWAITING_APPROVAL, "human", False),
        # ...and not an end at all for an `auto` job: it should have gone on
        (Stage.AWAITING_APPROVAL, "auto", True),
        (Stage.ERROR, "auto", True),
        (Stage.ERROR, "human", True),
        (SETUP_ERROR, "auto", True),
        ("missing", "auto", True),
        # a stage that is not a resting place: counted as failed, not ignored
        (Stage.RUN, "auto", True),
        ("una-etapa-que-no-existe", "auto", True),
    ],
)
def test_what_counts_as_a_failed_job(stage, policy, failed):
    assert job_failed(_outcome_of(stage, policy)) is failed


def test_all_no_evaluado_is_a_result_not_a_failure():
    """No haber podido observar es un resultado válido; la corrida funcionó."""
    outcome = _outcome_of(Stage.DONE, no_evaluado=3, scored=0)
    assert job_failed(outcome) is False
    assert exit_code_for(BatchSummary(jobs=[outcome], total_spend_usd=0.0)) == EXIT_OK


def test_exit_codes_tell_the_causes_apart():
    ok = _outcome_of(Stage.DONE)
    parked = _outcome_of(Stage.AWAITING_APPROVAL, "human")
    bad = _outcome_of(Stage.ERROR, error="la sesión expiró")

    def code(jobs, halted=False):
        return exit_code_for(
            BatchSummary(jobs=jobs, total_spend_usd=0.0, halted=halted)
        )

    assert code([ok, parked]) == EXIT_OK == 0
    assert code([ok, bad]) == EXIT_JOB_FAILED == 1
    assert code([ok], halted=True) == EXIT_HALTED == 3
    # both happened: the failure is what to look at first
    assert code([ok, bad], halted=True) == EXIT_JOB_FAILED
    # the cap cut the batch before anything launched: still not "done"
    assert code([], halted=True) == EXIT_HALTED
    assert EXIT_BAD_MANIFEST == 2


def _manifest(tmp_path, body="- platform_url: https://a.test\n  objective: evaluar\n"):
    path = tmp_path / "m.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "jobs, halted, expected, says",
    [
        ([("done", "auto")], False, 0, "RESULTADO: OK"),
        ([("done", "auto"), ("error", "auto")], False, 1, "FALLARON 1 de 2"),
        ([("done", "auto")], True, 3, "RESULTADO: INCOMPLETO"),
    ],
)
def test_the_cli_returns_the_batch_outcome(
    tmp_path, monkeypatch, capsys, jobs, halted, expected, says
):
    """`main()` devuelve lo que pasó con el lote, y lo dice en la última línea."""
    summary = BatchSummary(
        jobs=[_outcome_of(stage, policy) for stage, policy in jobs],
        total_spend_usd=0.0, halted=halted,
        halted_reason="se alcanzó el tope de gasto" if halted else "",
    )

    async def fake_run_batch(entries, **kwargs):
        return summary

    monkeypatch.setattr(batch_mod, "run_batch", fake_run_batch)
    code = batch_mod.main(
        ["run", str(_manifest(tmp_path)), "--root", str(tmp_path / "jobs")]
    )
    assert code == expected
    out = capsys.readouterr().out
    assert says in out
    assert f"código {expected}" in out.strip().splitlines()[-1]


def test_an_unreadable_manifest_is_exit_2_and_runs_nothing(tmp_path, monkeypatch):
    async def must_not_run(entries, **kwargs):
        raise AssertionError("corrió un lote con un manifiesto inválido")

    monkeypatch.setattr(batch_mod, "run_batch", must_not_run)
    bad = _manifest(tmp_path, "- objective: sin url\n")
    assert batch_mod.main(["run", str(bad)]) == EXIT_BAD_MANIFEST
    assert batch_mod.main(["run", str(tmp_path / "no-existe.yaml")]) == EXIT_BAD_MANIFEST


def test_the_exit_code_reaches_the_operating_system(tmp_path):
    """De punta a punta, por `python -m`, sin dobles: lo que ve el programador.

    La única entrada apunta a un documento que no existe, así que falla al
    prepararse -- antes de que el motor abra un navegador o llame al modelo -- y
    el comando REAL tiene que salir con 1. Esto prueba la última línea del
    módulo (`raise SystemExit(main())`), que ningún otro test ejecuta.
    """
    import subprocess
    import sys

    manifest = _manifest(
        tmp_path,
        "- platform_url: https://a.test\n"
        "  objective: evaluar\n"
        "  documents:\n"
        "    - name: Pliego\n"
        f"      path: {json.dumps(str(tmp_path / 'no-existe.pdf'))}\n",
    )
    done = subprocess.run(
        [sys.executable, "-m", "lxp_eval.service.batch", "run", str(manifest),
         "--root", str(tmp_path / "jobs")],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert done.returncode == EXIT_JOB_FAILED, done.stdout[-600:] + done.stderr[-600:]
    assert "FALLARON 1 de 1" in done.stdout
    assert "Pliego" in done.stdout


# --- the CLI path stays free of the web stack --------------------------------


def test_importing_the_batch_runner_does_not_load_the_web_server():
    """El lote corre desatendido y sin servidor: no tiene por qué cargar FastAPI.

    En un proceso LIMPIO, porque en éste `test_service` ya lo importó. Antes el
    paquete hacía `from .app import create_app` arriba de todo, así que cualquier
    submódulo arrastraba el stack web sólo por vivir ahí.
    """
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys; import lxp_eval.service.batch; "
        "bad = [m for m in ('fastapi', 'starlette', 'lxp_eval.service.app') "
        "if m in sys.modules]; "
        "print(','.join(bad)); sys.exit(1 if bad else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, f"se cargó: {done.stdout.strip()} {done.stderr[-400:]}"


def test_create_app_is_still_importable_from_the_package():
    """El export perezoso no cambia el contrato: `from lxp_eval.service import create_app`."""
    from lxp_eval.service import create_app
    from lxp_eval.service.app import create_app as real

    assert create_app is real


def test_the_document_cap_is_the_upload_cap():
    """Repetido en `batch` para no importar el servidor; atado acá para que no derive."""
    from lxp_eval.service.app import MAX_UPLOAD_BYTES

    assert batch_mod.MAX_DOCUMENT_BYTES == MAX_UPLOAD_BYTES


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
    """Consecuencia externa real contra un entorno real: sin firma no corre, y el
    criterio vuelve NO_EVALUADO.

    Es la invariante dura del repo bajo política `auto`: el lote no firma nada,
    así que el gate de `runnable_probes` deja afuera toda prueba que frena la
    corrida. Desde 2026-11-26 lo que frena es una consecuencia externa real (acá,
    exportar datos de alumnos) contra un entorno real, no la duda del clasificador.
    """
    engine = make_engine(tmp_path, build=real_consequence_framework,
                         real_environment=True)
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


# --- the approval snapshot says WHAT was approved ----------------------------
#
# The first version snapshotted `probes: None` ("run every stored probe"). Same
# run, weaker record: the hashed artefact named the criteria and not the probes,
# which lived only in job.json -- an editable file. A machine approval is the one
# nobody watched, so its audit record cannot carry less than a human one.


def _approval_snapshot(store, job_id):
    row = next(r for r in store.ledger_rows(job_id) if r["kind"] == "approval")
    return row, json.loads(Path(row["artifact"]).read_text(encoding="utf-8"))


def test_the_auto_approval_snapshot_carries_the_probes_it_approved(tmp_path):
    store, record = _auto_run(tmp_path, make_engine(tmp_path))
    row, snapshot = _approval_snapshot(store, record.job_id)

    assert snapshot["probes"], "la instantánea no dice qué pruebas se aprobaron"
    assert [p["probeId"] for p in snapshot["probes"]] == [
        p["probeId"] for p in record.probes
    ]
    for approved, stored in zip(snapshot["probes"], record.probes):
        # the wording that ran, and the review it rests on, are in the hashed file
        assert approved["steps"] == stored["steps"]
        assert approved["verdictRule"] == stored["verdictRule"]
        assert approved["judges"] == stored["judges"]
        assert approved["requiresSignoff"] is False

    # ...and that file is the one the ledger row hashes
    ledger = EvidenceLedger(store.ledger_path(record.job_id))
    assert ledger.verify(EvidenceEntry(**row)) is True


def test_the_snapshot_is_what_was_approved_not_what_job_json_says_later(tmp_path):
    """Editar job.json después no cambia lo que el archivo con hash dice."""
    store, record = _auto_run(tmp_path, make_engine(tmp_path))
    row, before = _approval_snapshot(store, record.job_id)
    original_steps = before["probes"][0]["steps"]

    record.probes[0]["steps"] = [{"n": 1, "action": "pasos reescritos", "capture": ""}]
    store.save(record)

    _, after = _approval_snapshot(store, record.job_id)
    assert after["probes"][0]["steps"] == original_steps
    ledger = EvidenceLedger(store.ledger_path(record.job_id))
    assert ledger.verify(EvidenceEntry(**row)) is True


def test_a_gated_probe_in_the_snapshot_is_recorded_unsigned_and_does_not_run(tmp_path):
    """Las pruebas con compuerta VAN en la instantánea, y aun así no corren.

    Llevarlas en el plan aprobado no las autoriza: el gate lee `holdsRun` de la
    copia del servidor y la firma de `record.signoffs`. La instantánea deja
    constancia de lo contrario -- frena la corrida, sin firma -- que es la razón
    por la que el criterio volvió NO_EVALUADO.
    """
    engine = make_engine(tmp_path, build=real_consequence_framework,
                         real_environment=True)
    store, record = _auto_run(tmp_path, engine)
    _, snapshot = _approval_snapshot(store, record.job_id)

    assert snapshot["probes"]
    for p in snapshot["probes"]:
        assert p["requiresSignoff"] is True
        assert p["holdsRun"] is True
        assert p["signoffReason"]
        assert p["signedOff"] is False
    assert record.signoffs == {}
    # recon is the first driver; anything after it would be a probe that ran
    assert [s for d in engine.test_drivers[1:] for s in d.steps] == []
    assert all(r["verdict"] == NO_EVALUADO for r in record.report["rows"])
    # no signoff row was ever written on the ledger either
    assert not [r for r in store.ledger_rows(record.job_id) if r["kind"] == "signoff"]


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
