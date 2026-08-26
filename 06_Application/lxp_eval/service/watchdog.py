"""Making death observable.

A job that fails is fine: somebody reads the message and acts. A job that HANGS
is worse than one that fails, because nobody learns anything and the user waits
for a run that will never finish. The production path (`background=True`) hands
each stage to `asyncio.create_task`, and a bare `create_task`:

* drops the exception on the floor unless somebody awaits it or attaches a
  done-callback -- so a stage that crashes between two transitions leaves the
  job sitting in that stage forever, with a progress bar that is simply wrong;
* is held only by a weak reference inside the loop, so a task nobody keeps can
  be garbage-collected mid-flight;
* has no notion of "this has been quiet for too long".

This module closes all three, plus the fourth case no in-process supervisor can
see: the worker itself dying. Four parts:

1. `Clock` -- the seam. Staleness is wall-clock (it has to survive a restart, and
   a monotonic reading does not cross processes), so the tests get a fake clock
   rather than sleeping through a fifteen-minute budget;
2. `stage_budget_s` -- ONE budget per stage, derived from that stage's work.
   A single global timeout is either short enough to kill a healthy 40-probe
   browser run or long enough to be useless on a model call;
3. `Supervisor` -- keeps a strong reference to every task, attaches the
   done-callback, and runs one watcher per job that turns a stalled heartbeat
   into a recorded `error`;
4. `reconcile` -- run at startup. Anything left in flight by a killed worker,
   whose heartbeat is older than its own budget, becomes `error` saying so.

The rule throughout: DETECT AND RECORD. Never a generic failure, never a silent
resurrection, and never a job quietly moved back to a stage it already left.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

from .store import JobRecord, JobStore

logger = logging.getLogger(__name__)


class Stage:
    """The stages, as the web client's `Stage` union declares them."""

    #: PRIMERA. Se conecta a la plataforma y la recorre, antes de que se gaste
    #: una sola llamada al modelo. Ver el bloque de `recon` en `jobs.py`.
    RECON = "recon"
    FRAMEWORK = "framework"
    PROBES = "probes"
    AWAITING_APPROVAL = "awaiting_approval"
    RUN = "run"
    SCORE = "score"
    DONE = "done"
    ERROR = "error"


ALL_STAGES = (
    Stage.RECON, Stage.FRAMEWORK, Stage.PROBES, Stage.AWAITING_APPROVAL,
    Stage.RUN, Stage.SCORE, Stage.DONE, Stage.ERROR,
)

#: Nothing is running; a heartbeat here means nothing and must never be judged.
TERMINAL_STAGES = (Stage.DONE, Stage.ERROR)

#: `awaiting_approval` is deliberately NOT here. A job waiting for a human to
#: read the plan is not stalled, however long it waits -- that pause is the
#: product. A watchdog that "rescued" it would be killing the design.
#:
#: `recon` SÍ está: corre sola, sin nadie mirando, y lo que hace es levantar un
#: Chromium y esperar a una plataforma ajena -- exactamente la clase de trabajo
#: que se cuelga sin dar señal. Una etapa declarada a medias (en `Stage` pero no
#: acá) se queda sin timeout propio y el watchdog la deja colgada para siempre.
ACTIVE_STAGES = (
    Stage.RECON, Stage.FRAMEWORK, Stage.PROBES, Stage.RUN, Stage.SCORE,
)


# --- the clock seam ---------------------------------------------------------


class Clock:
    """Wall clock plus sleep. The only source of time the watchdog uses."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


# --- per-stage budgets ------------------------------------------------------
#
# These are HEARTBEAT budgets: the longest a stage may go without writing any
# progress. They are not stage totals. A run that ticks once per probe renews
# its budget on every probe, so a 40-probe run is bounded by one probe's
# allowance, not by the sum -- which is why the numbers can be generous without
# letting a genuinely dead run sit for hours.

#: Levantar Chromium, abrir la plataforma, iniciar sesión y mirar. Es tiempo de
#: navegador, no de modelo: lo que lo hace lento es una plataforma ajena que
#: tarda en responder, y por eso no alcanza con el presupuesto de una llamada.
RECON_BUDGET_S = 15 * 60
#: A framework build is a bank load plus a bounded council of model calls.
FRAMEWORK_BUDGET_S = 15 * 60
#: Probes drafts one probe per observable criterion and puts each to five
#: judges, with up to `max_rounds` revisions. Materially more work than the
#: framework stage, and it writes no progress in between.
PROBES_BUDGET_S = 30 * 60
#: Fixed margin on the run tick. The FIRST tick is written before the driver
#: exists, so launching Chromium and logging in happen inside that first
#: interval; carrying the margin on every tick is a few minutes of slack in
#: exchange for not special-casing tick zero.
RUN_BASE_S = 5 * 60
#: Fixed part of ONE probe: launching, navigating, settling.
RUN_PER_PROBE_S = 5 * 60
#: ...plus this much per STEP. The budget used to be flat per probe, written
#: when a step was a deterministic DSL verb (goto, click) costing ~2 seconds,
#: so any probe fit inside one allowance. Every step is now an agent loop
#: against a live page -- read, act, verify -- at one to two minutes each. A
#: healthy 7-step probe blew the flat 600s and the watchdog killed it at 605s
#: mid-run; PB-PT-D8-01, at 28 steps, could never have finished at all.
RUN_PER_STEP_S = 5 * 60
#: Fixed margin on the score tick: loading the ledger, preparing prompts.
SCORE_BASE_S = 5 * 60
#: ONE criterion judged once against frozen evidence.
SCORE_PER_TEST_S = 3 * 60
#: Used only when a record predates these fields, or names a stage we do not
#: recognise. Deliberately the largest of the real budgets: a wrong guess must
#: not kill healthy work.
DEFAULT_BUDGET_S = PROBES_BUDGET_S


def stage_budget_s(
    stage: str, *, total: int = 1, sampling: dict | None = None, steps: int = 1
) -> float | None:
    """The heartbeat budget for one stage, or None if the stage cannot stall.

    THE BUDGET MATCHES THE TICK. `run` and `score` both write progress once per
    ITEM -- one probe, one criterion -- so the budget is one item's allowance,
    NOT the whole stage's. Sizing it to the stage was the first version and it
    was wrong in a way worth recording: a 40-probe run would sit stalled for
    over three hours before the watchdog fired, which is close enough to
    "forever" to defeat the point of having one.

    `total` is therefore deliberately unused for these two. What DOES multiply
    is the work inside a single tick: `repeats` (the runner walks one probe
    that many times against the live platform before returning) and
    `repeatJudging` (the scorer re-judges one criterion that many times against
    the same frozen bytes). Both loop INSIDE the call the tick brackets.

    Healthy work is still safe: a probe that keeps reporting renews its budget
    on every tick, so a long run is bounded per probe and never in aggregate.
    """
    sampling = sampling or {}

    def _count(key: str) -> int:
        try:
            return max(1, int(sampling.get(key, 1) or 1))
        except (TypeError, ValueError):
            return 1

    total = max(1, int(total or 1))
    if stage == Stage.RECON:
        return float(RECON_BUDGET_S)
    if stage == Stage.FRAMEWORK:
        return float(FRAMEWORK_BUDGET_S)
    if stage == Stage.PROBES:
        return float(PROBES_BUDGET_S)
    if stage == Stage.RUN:
        per_probe = RUN_PER_PROBE_S + max(1, int(steps or 1)) * RUN_PER_STEP_S
        return float(RUN_BASE_S + _count("repeats") * per_probe)
    if stage == Stage.SCORE:
        return float(SCORE_BASE_S + _count("repeatJudging") * SCORE_PER_TEST_S)
    return None  # awaiting_approval, done, error: no clock runs on these


# --- heartbeat --------------------------------------------------------------


def max_steps_of(record: JobRecord) -> int:
    """Steps in the longest probe this job will walk, at least 1.

    The tick brackets ONE probe, so the clock has to cover the longest one --
    an average would kill the outlier, which is exactly the probe most likely
    to be slow.
    """
    plan = record.approved_plan or {}
    probes = plan.get("probes") or record.probes or []
    counts = [
        len(p.get("steps") or []) for p in probes if isinstance(p, dict)
    ]
    return max(counts) if counts else 1


def _parse(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def heartbeat_of(record: JobRecord) -> datetime | None:
    """When this job last showed a sign of life.

    Falls back through `updated_at` and `created_at` so a record written before
    this field existed is still judged by something real rather than treated as
    infinitely fresh (which would make the watchdog silently useless) or
    infinitely stale (which would kill every old job on the first boot after a
    deploy -- the reason `reconcile` also insists the stage be an active one).
    """
    for stamp in (record.last_progress_at, record.updated_at, record.created_at):
        parsed = _parse(stamp)
        if parsed is not None:
            return parsed
    return None


def budget_of(record: JobRecord) -> float | None:
    """The budget the record itself carries, or a re-derivation of it."""
    if record.stage not in ACTIVE_STAGES:
        return None
    stored = record.stage_timeout_s
    if stored and stored > 0:
        return float(stored)
    return stage_budget_s(
        record.stage,
        total=int((record.progress or {}).get("total") or 1),
        sampling=record.sampling,
        steps=max_steps_of(record),
    ) or float(DEFAULT_BUDGET_S)


def stalled_for(record: JobRecord, now: datetime) -> float | None:
    """Seconds past this stage's budget, or None if it is not stalled.

    None also covers "cannot stall": a terminal stage, `awaiting_approval`, or a
    record with no parseable timestamp at all.
    """
    budget = budget_of(record)
    if budget is None:
        return None
    beat = heartbeat_of(record)
    if beat is None:
        return None
    quiet = (now - beat).total_seconds()
    return quiet - budget if quiet > budget else None


# --- recording failure ------------------------------------------------------


def mark_error(
    store: JobStore,
    record: JobRecord,
    *,
    stage: str,
    item: str = "",
    message: str,
    kind: str,
    session_lost: bool = False,
    at: datetime | None = None,
) -> JobRecord:
    """THE one writer of `record.error`. Keeps where it died, not just that it did.

    `progress` keeps the failing stage and the item in flight so the UI can say
    WHERE; only `record.stage` becomes `error`.
    """
    stamp = (at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    record.error = {
        "stage": stage,
        "item": item,
        "message": message,
        "sessionLost": session_lost,
        "kind": kind,
        "at": stamp,
    }
    record.progress = {**(record.progress or {}), "stage": stage, "current": item}
    record.stage = Stage.ERROR
    record.last_progress_at = stamp
    record.stage_timeout_s = 0.0
    store.save(record)
    return record


def _stall_message(record: JobRecord, over: float, budget: float | None) -> str:
    item = (record.progress or {}).get("current") or ""
    where = f' mientras procesaba "{item}"' if item else ""
    minutes = int((budget or 0) // 60)
    return (
        f'el trabajo se quedó sin avanzar en la etapa "{record.stage}"{where}: '
        f"no dio ninguna señal de progreso durante más de {minutes} minutos "
        f"(se pasó por {int(over)} segundos del plazo de esa etapa). "
        "No se sabe si la tarea murió o quedó bloqueada, así que el trabajo se "
        "marca como fallido en vez de dejarlo colgado para siempre."
    )


def _dead_worker_message(record: JobRecord, budget: float | None) -> str:
    item = (record.progress or {}).get("current") or ""
    where = f' mientras procesaba "{item}"' if item else ""
    minutes = int((budget or 0) // 60)
    return (
        f'el proceso que ejecutaba este trabajo murió: quedó en la etapa '
        f'"{record.stage}"{where} y al reiniciarse el servicio su último latido '
        f"ya tenía más de {minutes} minutos. El trabajo no sigue corriendo en "
        "ninguna parte; hay que volver a lanzarlo."
    )


# --- startup reconciliation -------------------------------------------------


def reconcile(store: JobStore, *, clock: Clock | None = None) -> list[str]:
    """Anything a killed worker left in flight becomes `error`. Returns the ids.

    Deliberately conservative on both sides. A job whose heartbeat is still
    fresh is LEFT ALONE -- it may be running right now on a sibling worker, and
    stealing it would be worse than waiting for its own watchdog. A job in
    `awaiting_approval` is left alone forever, because that pause is a human
    reading a plan and not a fault.
    """
    clock = clock or Clock()
    now = clock.now()
    recovered: list[str] = []
    for job_id in store.list_ids():
        record = store.get(job_id)
        if record is None or record.stage not in ACTIVE_STAGES:
            continue
        over = stalled_for(record, now)
        if over is None:
            continue
        stage = record.stage
        mark_error(
            store, record,
            stage=stage,
            item=(record.progress or {}).get("current") or "",
            message=_dead_worker_message(record, budget_of(record)),
            kind="WorkerDied",
            at=now,
        )
        recovered.append(job_id)
        logger.warning("job %s recuperado al arrancar: murió en %s", job_id, stage)
    return recovered


# --- the supervisor ---------------------------------------------------------


class Supervisor:
    """Owns every background stage task: its reference, its death, its clock.

    One watcher task per job rather than one loop over all jobs. It costs a
    task, and it buys the property that a watcher cannot be starved by another
    job's watcher and stops existing the moment its job does.
    """

    def __init__(
        self,
        store: JobStore,
        *,
        clock: Clock | None = None,
        poll_interval: float = 15.0,
    ) -> None:
        self.store = store
        self.clock = clock or Clock()
        self.poll_interval = poll_interval
        #: STRONG references. `asyncio` keeps only a weak one, so a task nobody
        #: holds may be collected mid-flight -- the job would then stop with no
        #: exception anywhere. This dict is what stops that.
        self._tasks: dict[str, asyncio.Task] = {}
        self._watchers: dict[str, asyncio.Task] = {}
        self._reaper: asyncio.Task | None = None

    # --- lifecycle ---------------------------------------------------------

    def start(
        self,
        coro_fn: Callable[[JobRecord], Awaitable[Any]],
        record: JobRecord,
    ) -> asyncio.Task:
        """Schedule one stage, supervised. Returns immediately."""
        job_id = record.job_id
        task = asyncio.get_running_loop().create_task(
            self._guarded(coro_fn, record), name=f"evalkit-job-{job_id}"
        )
        self._tasks[job_id] = task
        task.add_done_callback(self._task_done)
        watcher = asyncio.get_running_loop().create_task(
            self._watch(job_id, task), name=f"evalkit-watch-{job_id}"
        )
        self._watchers[job_id] = watcher
        watcher.add_done_callback(lambda t: self._watchers.pop(job_id, None))
        return task

    async def _guarded(self, coro_fn, record: JobRecord):
        """Run the stage; never let its death go unrecorded.

        `BaseException`, not `Exception`: a `CancelledError` from the watchdog,
        or a `KeyboardInterrupt` during a shutdown, is exactly the case where a
        job would otherwise be left mid-stage forever.
        """
        job_id = record.job_id
        try:
            return await coro_fn(record)
        except asyncio.CancelledError:
            # The watchdog cancels a stalled task and has ALREADY written the
            # real reason. Do not paper over it with "cancelled".
            fresh = self.store.get(job_id)
            if fresh is not None and fresh.stage != Stage.ERROR:
                mark_error(
                    self.store, fresh,
                    stage=fresh.stage,
                    item=(fresh.progress or {}).get("current") or "",
                    message=(
                        "el trabajo fue cancelado antes de terminar la etapa "
                        f'"{fresh.stage}" (probablemente el servicio se está '
                        "apagando). No quedó corriendo en ninguna parte."
                    ),
                    kind="CancelledError",
                    at=self.clock.now(),
                )
            raise
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            fresh = self.store.get(job_id) or record
            if fresh.stage != Stage.ERROR:
                mark_error(
                    self.store, fresh,
                    stage=fresh.stage,
                    item=(fresh.progress or {}).get("current") or "",
                    message=str(exc) or exc.__class__.__name__,
                    kind=exc.__class__.__name__,
                    at=self.clock.now(),
                )
            logger.exception("job %s murió en segundo plano", job_id)
            # Record it, then let the interpreter keep dying. Swallowing these
            # would turn "the process is shutting down" into "one job failed",
            # and the shutdown would stall.
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return None

    def _task_done(self, task: asyncio.Task) -> None:
        """Retrieve the exception so the loop cannot swallow it silently.

        `_guarded` already catches everything, so reaching this with a live
        exception means the supervisor itself is broken -- which is worth a log
        rather than an "exception was never retrieved" warning at GC time.
        """
        for job_id, held in list(self._tasks.items()):
            if held is task:
                self._tasks.pop(job_id, None)
                break
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("tarea de trabajo terminó con excepción no manejada: %r", exc)

    # --- the watchdog ------------------------------------------------------

    async def _watch(self, job_id: str, task: asyncio.Task) -> None:
        while not task.done():
            await self.clock.sleep(self.poll_interval)
            if task.done():
                return
            record = self.store.get(job_id)
            if record is None or record.stage in TERMINAL_STAGES:
                return
            over = stalled_for(record, self.clock.now())
            if over is None:
                continue
            budget = budget_of(record)
            mark_error(
                self.store, record,
                stage=record.stage,
                item=(record.progress or {}).get("current") or "",
                message=_stall_message(record, over, budget),
                kind="StageStalled",
                at=self.clock.now(),
            )
            logger.warning(
                "job %s estancado en %s, se cancela la tarea", job_id, record.stage
            )
            task.cancel()
            return

    # --- the reaper: reconciliation on a timer, not only at boot ----------

    def start_reaper(self, interval: float = 60.0) -> asyncio.Task:
        """Re-run `reconcile` forever. Closes the multi-worker cliff.

        Startup-only reconciliation has a hole: a job orphaned by a worker that
        died stays `running` until THAT worker restarts. With one worker the
        hole closes on the next boot, which is usually soon. With two, worker B
        never reconciles A's orphan -- B booted while the heartbeat was still
        fresh, and nothing makes it look again. The job sits `running` forever
        and presents as a mystery outage.

        Safe to run against live jobs, including this worker's own, for the
        same reason `reconcile` is: a job being worked on has a fresh
        heartbeat, and only a stale one is touched.
        """
        task = asyncio.get_running_loop().create_task(
            self._reap(interval), name="evalkit-reaper"
        )
        self._reaper = task
        return task

    async def _reap(self, interval: float) -> None:
        while True:
            await self.clock.sleep(interval)
            try:
                recovered = reconcile(self.store, clock=self.clock)
            except Exception:  # noqa: BLE001 -- the reaper must never die
                # If it raised, nothing else would ever reconcile again, and
                # the failure would be invisible. Log and keep the loop.
                logger.exception("la reconciliación periódica falló; se reintenta")
                continue
            if recovered:
                logger.warning(
                    "reconciliación periódica: %d trabajo(s) huérfanos marcados "
                    "como fallidos: %s",
                    len(recovered), ", ".join(recovered),
                )

    # --- for tests and for shutdown ---------------------------------------

    def pending(self) -> Iterable[asyncio.Task]:
        return list(self._tasks.values())

    async def drain(self) -> None:
        """Wait for every scheduled stage to finish. Tests and shutdown only."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        for watcher in list(self._watchers.values()):
            watcher.cancel()
        await asyncio.gather(*list(self._watchers.values()), return_exceptions=True)

    async def shutdown(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
            self._reaper = None
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
        for watcher in list(self._watchers.values()):
            watcher.cancel()
        await asyncio.gather(*list(self._watchers.values()), return_exceptions=True)


__all__ = [
    "ACTIVE_STAGES",
    "ALL_STAGES",
    "Clock",
    "DEFAULT_BUDGET_S",
    "Stage",
    "Supervisor",
    "TERMINAL_STAGES",
    "budget_of",
    "heartbeat_of",
    "mark_error",
    "reconcile",
    "stage_budget_s",
    "stalled_for",
]
