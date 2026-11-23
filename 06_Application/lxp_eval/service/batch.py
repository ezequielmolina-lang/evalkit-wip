"""Correr muchas evaluaciones sin nadie mirando, por los mismos rieles que una.

    python -m lxp_eval.service.batch run <manifest.yaml>

Una corrida semanal de 50 a 100 herramientas es, hoy, 50 a 100 `POST /jobs`
hechos a mano y 50 a 100 pantallas de aprobación esperando una persona. Este
módulo cierra las dos brechas SIN motor nuevo: recorre un manifiesto y empuja
cada evaluación por las MISMAS costuras que usa el servicio HTTP --
`JobStore.create`, `JobRunner.build_plan`/`execute`, bajo `Supervisor.start`--
de modo que el heartbeat, el watchdog y la reconciliación de huérfanos cubren
los trabajos del lote sin tocarse. No hay un segundo recorrido del motor acá:
si fallara la supervisión del lote, fallaría la del servicio, porque es la misma.

DOS COSAS QUE EL LOTE NO NEGOCIA, porque son las invariantes del repo:

* UNA PRUEBA CON COMPUERTA NO CORRE SIN FIRMA HUMANA, y la firma es por prueba,
  no por plan. La política `auto` aprueba el plan y deja correr las pruebas SIN
  compuerta; cada prueba con compuerta sigue detrás de su propio signoff, que el
  lote nunca escribe, así que `runnable_probes` la deja afuera y su criterio
  vuelve NO_EVALUADO. Esto no es una cortesía del lote: es el mismo gate de
  `JobRunner.runnable_probes`, intacto. El lote no firma; sólo elige no esperar
  por lo que no tiene compuerta.

* NO SE AFIRMA UNA APROBACIÓN HUMANA QUE NO PASÓ. La fila de aprobación que el
  lote escribe en el ledger lleva el actor máquina (`APPROVAL_NOTE_AUTO`), y el
  informe lo registra en `meta.approvalPolicy`. Un ministerio que lea la
  procedencia ve "aprobado automáticamente", no "aprobado por el evaluador
  humano".

SIN PROGRAMADOR INTERNO. Una corrida semanal es este CLI dentro de una tarea
programada del sistema (cron, el Programador de tareas de Windows). Un scheduler
acá adentro sería una segunda fuente de verdad sobre cuándo corre algo, y la
primera -- el sistema operativo -- ya existe y ya se reinicia sola.

SECRETOS SÓLO DEL ENTORNO. El manifiesto lleva una REFERENCIA a credenciales
(`credentials_ref`), nunca una contraseña: un manifiesto se versiona, y una
contraseña en git es una contraseña quemada. La referencia se resuelve contra
`EVALKIT_CRED_<REF>_USER` / `EVALKIT_CRED_<REF>_PASS`. Sin esas variables el
trabajo corre sobre la sesión guardada (el diseño recon-first), no se inventa un
login.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, Sequence

from ..config import CONFIG
from ..evalkit.framework import NO_EVALUADO
from ..evalkit.generator import DEFAULT_RISK_POLICY, RISK_POLICIES
from . import serialize
from .app import _ledger_the_approval  # la MISMA fila de aprobación que /approve
from .crypto import SecretBox, ServiceError
from .jobs import Engine, JobRunner, Stage, platform_label
from .store import JobRecord, JobStore, now
from .watchdog import Clock, Supervisor

logger = logging.getLogger(__name__)

#: Mismo default que `app.DEFAULT_ROOT`: un lote y el servicio comparten el mismo
#: directorio de trabajos, para que un trabajo del lote se vea en la interfaz y
#: la reconciliación al arrancar el servicio lo cubra igual que a cualquier otro.
DEFAULT_ROOT = Path("runs") / "jobs"

VALID_APPROVAL_POLICIES = ("human", "auto")


__all__ = [
    "BatchSummary",
    "JobOutcome",
    "Manifest",
    "ManifestEntry",
    "load_manifest",
    "run_batch",
    "main",
]


# --- manifest ---------------------------------------------------------------


@dataclass
class ManifestEntry:
    """Una evaluación del lote. Mismos campos que un `POST /jobs`, más la política.

    `credentials_ref` es una REFERENCIA, no una credencial (ver el docstring del
    módulo). `documents` son `{name, text}` ya extraídos, o `{name, path}` para
    que el lote lea y extraiga el archivo -- lo mismo que `/extract` hace por la
    interfaz, para que un manifiesto pueda apuntar a un pliego en disco.
    """

    platform_url: str
    objective: str
    areas: list[str] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    platform_kind: str = ""
    credentials_ref: str = ""
    risk_policy: str = DEFAULT_RISK_POLICY
    #: Vacío = hereda el default del lote (`run_batch(default_approval_policy=...)`,
    #: a su vez el del manifiesto o "human"). `load_manifest` deja "" cuando la
    #: entrada no fija una, para que el default se resuelva en un solo lugar.
    approval_policy: str = ""


@dataclass
class Manifest:
    """El lote entero: las entradas y los topes que valen para todas.

    El manifiesto puede ser una LISTA de entradas (lo mínimo que pide la tarea) o
    un diccionario con `jobs:` y, opcionalmente, `max_spend_usd`, `max_parallel`,
    `approval_policy` y `root`. Las dos formas se aceptan para que lo simple siga
    siendo una lista y lo configurable no tenga que mudarse a banderas del CLI.
    """

    entries: list[ManifestEntry]
    root: str | None = None
    max_spend_usd: float | None = None
    max_parallel: int | None = None
    approval_policy: str | None = None


def _as_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    raise ValueError(f'"{field_name}" tiene que ser una lista o un texto')


def _entry_from(raw: dict, *, index: int, default_risk: str) -> ManifestEntry:
    if not isinstance(raw, dict):
        raise ValueError(
            f"la entrada {index} del manifiesto no es un objeto: cada trabajo es "
            "un mapa con al menos platform_url y objective"
        )
    url = str(raw.get("platform_url") or raw.get("url") or "").strip()
    objective = str(raw.get("objective") or "").strip()
    if not url:
        raise ValueError(f"la entrada {index} no tiene platform_url")
    if not objective:
        raise ValueError(f"la entrada {index} no tiene objective")

    risk = str(raw.get("risk_policy") or default_risk)
    if risk not in RISK_POLICIES:
        raise ValueError(
            f'la entrada {index} tiene risk_policy "{risk}", que no existe: '
            f"usá una de {', '.join(RISK_POLICIES)}"
        )
    policy = str(raw.get("approval_policy") or "").strip()
    if policy and policy not in VALID_APPROVAL_POLICIES:
        raise ValueError(
            f'la entrada {index} tiene approval_policy "{policy}", que no existe: '
            f"usá una de {', '.join(VALID_APPROVAL_POLICIES)}"
        )

    documents = raw.get("documents") or []
    if not isinstance(documents, list):
        raise ValueError(f'la entrada {index}: "documents" tiene que ser una lista')

    return ManifestEntry(
        platform_url=url,
        objective=objective,
        areas=_as_list(raw.get("areas"), "areas"),
        documents=[dict(d) for d in documents if isinstance(d, dict)],
        platform_kind=str(raw.get("platform_kind") or raw.get("kind") or ""),
        credentials_ref=str(raw.get("credentials_ref") or ""),
        risk_policy=risk,
        approval_policy=policy,
    )


def load_manifest(path: Path | str) -> Manifest:
    """Lee y VALIDA el manifiesto en el borde, no cuando ya se gastó plata.

    Un manifiesto malo tiene que fallar antes de abrir un solo Chromium: una
    política de riesgo inexistente o una entrada sin URL descubierta a mitad del
    lote ya costó N logins. Todo lo que se puede chequear leyendo el archivo se
    chequea acá.
    """
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if raw is None:
        raise ValueError("el manifiesto está vacío")

    settings: dict = {}
    if isinstance(raw, dict):
        jobs = raw.get("jobs")
        if jobs is None:
            raise ValueError(
                'el manifiesto es un objeto pero no tiene "jobs": poné la lista '
                "de trabajos bajo esa clave, o hacé que el manifiesto sea una "
                "lista directamente"
            )
        settings = raw
    elif isinstance(raw, list):
        jobs = raw
    else:
        raise ValueError(
            "el manifiesto tiene que ser una lista de trabajos, o un objeto con "
            '"jobs" adentro'
        )
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("el manifiesto no tiene ningún trabajo")

    default_policy = settings.get("approval_policy")
    if default_policy and default_policy not in VALID_APPROVAL_POLICIES:
        raise ValueError(
            f'approval_policy "{default_policy}" no existe: usá una de '
            f"{', '.join(VALID_APPROVAL_POLICIES)}"
        )

    entries = [
        _entry_from(job, index=i, default_risk=DEFAULT_RISK_POLICY)
        for i, job in enumerate(jobs)
    ]
    max_spend = settings.get("max_spend_usd")
    max_parallel = settings.get("max_parallel")
    return Manifest(
        entries=entries,
        root=settings.get("root"),
        max_spend_usd=None if max_spend is None else float(max_spend),
        max_parallel=None if max_parallel is None else int(max_parallel),
        approval_policy=default_policy,
    )


# --- credentials ------------------------------------------------------------


def resolve_credentials(ref: str) -> tuple[str, str] | None:
    """Una referencia del manifiesto -> (usuario, contraseña) DEL ENTORNO, o None.

    Nunca del manifiesto, nunca de un archivo del repo: `EVALKIT_CRED_<REF>_USER`
    y `EVALKIT_CRED_<REF>_PASS`, con `<REF>` en mayúsculas y lo no alfanumérico
    pasado a `_`. Faltando cualquiera de las dos, devuelve None y el trabajo
    corre sobre la sesión guardada -- el mismo camino que un `POST /jobs` sin
    credenciales. No se inventa un login a medias.
    """
    if not ref:
        return None
    key = "".join(c if c.isalnum() else "_" for c in ref).upper()
    user = os.getenv(f"EVALKIT_CRED_{key}_USER")
    password = os.getenv(f"EVALKIT_CRED_{key}_PASS")
    if not user or not password:
        return None
    return user, password


def _load_documents(documents: Sequence[dict]) -> list[dict]:
    """`{name, text}` tal cual, o `{name, path}` leído y extraído a texto.

    Extraer acá, en el borde, es lo mismo que hace `/extract` por la interfaz: el
    registro del trabajo guarda SIEMPRE `{name, text}`, nunca una ruta, así que
    el motor no gana un camino nuevo que pueda nombrar un archivo.
    """
    from ..evalkit.extract import extract_text

    out: list[dict] = []
    for doc in documents:
        text = doc.get("text")
        if text is not None:
            out.append({"name": doc.get("name", ""), "text": str(text)})
            continue
        path = doc.get("path")
        if not path:
            continue
        p = Path(path)
        name = doc.get("name") or p.name
        out.append({"name": name, "text": extract_text(name, p.read_bytes())})
    return out


# --- auto-approval ----------------------------------------------------------


def _auto_approve(store: JobStore, record: JobRecord) -> None:
    """Aprobar el plan sin humano, dejando el gate exactamente donde estaba.

    Es `POST /jobs/{id}/approve` menos la persona: el plan por defecto (sin
    ediciones, `probes=None` -> `runnable_probes` toma TODAS las pruebas
    guardadas) y el actor máquina en la fila del ledger. No se escribe ningún
    signoff: por eso cada prueba con compuerta queda fuera de la corrida y su
    criterio vuelve NO_EVALUADO. La firma sigue siendo por prueba, y acá no hay
    ninguna.

    Mismo orden que el endpoint: guardar el registro primero, poner la fila en el
    ledger después y ANTES de que `execute` corra sobre su autoridad. El helper
    es fail-closed a propósito (ver `app._ledger_decision`): una aprobación que
    no se puede registrar no debe surtir efecto.
    """
    record.sampling = record.sampling or dict(serialize.DEFAULT_SAMPLING)
    record.approved_plan = {
        "criteria": serialize.criteria_out(record.criteria),
        "probes": None,
        "sampling": record.sampling,
    }
    record.approved_at = now()
    store.save(record)
    _ledger_the_approval(store, record, note=serialize.APPROVAL_NOTE_AUTO)


# --- the runner seam --------------------------------------------------------


class _RunnerLike(Protocol):
    """Lo único que el lote le pide a un runner: las dos costuras del motor."""

    async def build_plan(self, record: JobRecord) -> JobRecord: ...
    async def execute(self, record: JobRecord) -> JobRecord: ...


RunnerFactory = Callable[[JobStore, Engine, "SecretBox | None", Clock], _RunnerLike]


def _default_runner_factory(
    store: JobStore, engine: Engine, box: "SecretBox | None", clock: Clock
) -> _RunnerLike:
    return JobRunner(store, engine, box, clock=clock)


# --- results ----------------------------------------------------------------


@dataclass
class JobOutcome:
    """Lo que pasó con un trabajo del lote, leído del store (no de la memoria)."""

    job_id: str
    platform: str
    stage: str
    approval_policy: str
    spend_usd: float
    no_evaluado: int
    scored: int
    error: str


@dataclass
class BatchSummary:
    jobs: list[JobOutcome]
    total_spend_usd: float
    halted: bool = False
    halted_reason: str = ""


def _outcome(store: JobStore, job_id: str) -> JobOutcome:
    record = store.get(job_id)
    if record is None:
        return JobOutcome(job_id, "", "missing", "", 0.0, 0, 0, "el trabajo desapareció")
    no_evaluado = scored = 0
    report = record.report or {}
    for row in report.get("rows", []):
        if row.get("verdict") == NO_EVALUADO:
            no_evaluado += 1
        else:
            scored += 1
    return JobOutcome(
        job_id=record.job_id,
        platform=record.platform,
        stage=record.stage,
        approval_policy=record.approval_policy,
        spend_usd=float(record.spend_usd or 0.0),
        no_evaluado=no_evaluado,
        scored=scored,
        error=(record.error or {}).get("message", "") if record.error else "",
    )


# --- the batch loop ---------------------------------------------------------


async def run_batch(
    entries: Sequence[ManifestEntry],
    *,
    store: JobStore,
    engine: Engine | None = None,
    box: "SecretBox | None" = None,
    clock: Clock | None = None,
    max_parallel: int | None = None,
    max_spend_usd: float | None = None,
    default_approval_policy: str = "human",
    runner_factory: RunnerFactory | None = None,
    supervisor: Supervisor | None = None,
) -> BatchSummary:
    """Recorre el manifiesto y maneja cada trabajo, acotado y supervisado.

    CONCURRENCIA. Un Chromium por trabajo es pesado, así que un semáforo del
    tamaño de `LXP_MAX_PARALLEL_AGENTS` acota cuántos corren a la vez. El semáforo
    se ADQUIERE en el lanzador, antes de crear el trabajo, y se LIBERA cuando el
    trabajo termina: así "dejar de lanzar" y "no pasar de N en vuelo" son la misma
    operación, y un trabajo no se crea hasta que hay lugar para su navegador.

    TOPE DE GASTO. Después de cada trabajo que termina se suma `record.spend_usd`
    (leído del store, la fuente de verdad) y, antes de lanzar el próximo, se
    compara contra el tope. Pasado el tope se deja de LANZAR; los que ya están en
    vuelo se dejan terminar -- matarlos tiraría trabajo ya pago. El tope cuenta
    gasto TERMINADO, que es lo que se puede medir sin adivinar.

    SUPERVISIÓN. Cada trabajo se agenda con `Supervisor.start`, así que el mismo
    watchdog que cubre al servicio cubre al lote: un trabajo que se cuelga se
    marca `error` por latido vencido en vez de colgar el lote entero.
    """
    engine = engine or Engine()
    clock = clock or Clock()
    runner_factory = runner_factory or _default_runner_factory
    owns_supervisor = supervisor is None
    supervisor = supervisor or Supervisor(store, clock=clock)
    limit = int(max_parallel if max_parallel is not None else CONFIG.max_parallel_agents)
    sem = asyncio.Semaphore(max(1, limit))

    total_spend = 0.0
    halted = False
    halted_reason = ""
    launched: list[str] = []

    def _over_cap() -> bool:
        return max_spend_usd is not None and total_spend >= float(max_spend_usd)

    async def _worker(record: JobRecord, entry: ManifestEntry, policy: str) -> JobRecord:
        nonlocal total_spend
        job_engine = replace(engine, risk_policy=entry.risk_policy)
        runner = runner_factory(store, job_engine, box, clock)
        try:
            await runner.build_plan(record)
            fresh = store.get(record.job_id) or record
            # Sólo un plan que llegó a la compuerta de aprobación se aprueba:
            # un build_plan que murió (recon fallido, etc.) quedó en `error` y no
            # se ejecuta. Bajo `human` no se aprueba nada: queda esperando, que es
            # el comportamiento de hoy.
            if policy == "auto" and fresh.stage == Stage.AWAITING_APPROVAL:
                _auto_approve(store, fresh)
                await runner.execute(fresh)
            return store.get(record.job_id) or fresh
        finally:
            # Sumar el gasto ANTES de soltar el semáforo: cuando el lanzador
            # despierte de su `acquire`, el total ya tiene que reflejar a este
            # trabajo, o el tope miraría una cifra vieja.
            final = store.get(record.job_id) or record
            total_spend += float(final.spend_usd or 0.0)
            sem.release()

    def _make_coro(entry: ManifestEntry, policy: str):
        async def _coro(record: JobRecord) -> JobRecord:
            return await _worker(record, entry, policy)
        return _coro

    tasks: list[asyncio.Task] = []
    for entry in entries:
        if _over_cap():
            halted = True
            break
        await sem.acquire()
        # Un trabajo pudo terminar mientras esperábamos lugar y hacer saltar el
        # tope: re-chequear antes de gastar en uno nuevo.
        if _over_cap():
            sem.release()
            halted = True
            break

        policy = entry.approval_policy or default_approval_policy or "human"
        try:
            record = store.create(
                objective=entry.objective,
                areas=list(entry.areas),
                platform_url=entry.platform_url,
                platform=platform_label(entry.platform_url),
                platform_kind=entry.platform_kind,
                documents=_load_documents(entry.documents),
                approval_policy=policy,
            )
            if entry.credentials_ref:
                creds = resolve_credentials(entry.credentials_ref)
                if creds and box is not None:
                    store.put_credentials(
                        record.job_id, box.encrypt_credentials(creds[0], creds[1])
                    )
                    record.has_credentials = True
                    store.save(record)
                elif creds and box is None:
                    logger.warning(
                        "job %s: hay credenciales para %s pero el servicio no "
                        "tiene EVALKIT_SECRET_KEY; corre sobre la sesión guardada",
                        record.job_id, entry.credentials_ref,
                    )
                else:
                    logger.info(
                        "job %s: credentials_ref %s sin variables de entorno; "
                        "corre sobre la sesión guardada",
                        record.job_id, entry.credentials_ref,
                    )
        except BaseException:
            # El trabajo no llegó a agendarse: soltar el lugar que tomamos, o el
            # semáforo se desangra y el lote se cuelga en el próximo acquire.
            sem.release()
            raise

        tasks.append(supervisor.start(_make_coro(entry, policy), record))
        launched.append(record.job_id)

    if halted:
        halted_reason = (
            f"se alcanzó el tope de gasto del lote: ${total_spend:.2f} de un "
            f"máximo de ${float(max_spend_usd):.2f}. Se dejaron de lanzar "
            f"trabajos nuevos; los {len(tasks)} ya lanzados terminan."
        )
        logger.warning(halted_reason)

    # Esperar a que termine todo lo lanzado. `_guarded` ya registró cualquier
    # muerte en el propio registro del trabajo, así que un error acá no es una
    # sorpresa: es un trabajo marcado `error` que el resumen va a listar.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if owns_supervisor:
        await supervisor.shutdown()

    outcomes = [_outcome(store, job_id) for job_id in launched]
    return BatchSummary(
        jobs=outcomes,
        total_spend_usd=round(total_spend, 4),
        halted=halted,
        halted_reason=halted_reason,
    )


# --- CLI --------------------------------------------------------------------


def _print_summary(summary: BatchSummary) -> None:
    print(f"\nLote terminado: {len(summary.jobs)} trabajo(s) lanzados.")
    for o in summary.jobs:
        linea = (
            f"  - {o.job_id}  {o.platform or '(sin plataforma)'}  "
            f"[{o.approval_policy}]  etapa={o.stage}  gasto=${o.spend_usd:.2f}"
        )
        if o.stage == Stage.DONE:
            linea += f"  puntuados={o.scored}  NO_EVALUADO={o.no_evaluado}"
        if o.error:
            linea += f"  error: {o.error}"
        print(linea)
    print(f"Gasto total del lote: ${summary.total_spend_usd:.2f}")
    if summary.halted:
        print(f"ATENCIÓN: {summary.halted_reason}")


def _cli_run(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError) as exc:
        print(f"no se pudo leer el manifiesto: {exc}", file=sys.stderr)
        return 2

    root = args.root or manifest.root or DEFAULT_ROOT
    store = JobStore(root)

    # El lote maneja credenciales ajenas igual que el servicio; sin clave de
    # cifrado puede correr, pero sólo trabajos sin credenciales (sobre sesión
    # guardada). No se cae por eso: se avisa.
    try:
        box: SecretBox | None = SecretBox.from_env()
    except ServiceError:
        logger.warning(
            "sin EVALKIT_SECRET_KEY: los trabajos con credentials_ref correrán "
            "sobre la sesión guardada, no con credenciales"
        )
        box = None

    max_spend = args.max_spend_usd if args.max_spend_usd is not None else manifest.max_spend_usd
    max_parallel = args.max_parallel if args.max_parallel is not None else manifest.max_parallel
    default_policy = args.approval_policy or manifest.approval_policy or "human"

    summary = asyncio.run(
        run_batch(
            manifest.entries,
            store=store,
            engine=Engine(),
            box=box,
            max_parallel=max_parallel,
            max_spend_usd=max_spend,
            default_approval_policy=default_policy,
        )
    )
    _print_summary(summary)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lxp_eval.service.batch",
        description=(
            "Corre un lote de evaluaciones sin intervención, por las mismas "
            "costuras que el servicio. Para una corrida semanal, poné este "
            "comando en una tarea programada del sistema."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    runp = sub.add_parser("run", help="correr el lote descrito en un manifiesto")
    runp.add_argument("manifest", help="ruta al manifiesto YAML")
    runp.add_argument(
        "--root", default=None,
        help="directorio de trabajos (default: runs/jobs, igual que el servicio)",
    )
    runp.add_argument(
        "--max-parallel", type=int, default=None,
        help="trabajos en vuelo a la vez (default: LXP_MAX_PARALLEL_AGENTS)",
    )
    runp.add_argument(
        "--max-spend-usd", type=float, default=None,
        help="tope de gasto del lote en USD; pasado el tope no se lanzan nuevos",
    )
    runp.add_argument(
        "--approval-policy", choices=list(VALID_APPROVAL_POLICIES), default=None,
        help="política por defecto para entradas que no la fijan (default: human)",
    )
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cli_run(args)
    parser.print_help(sys.stderr)  # pragma: no cover
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
