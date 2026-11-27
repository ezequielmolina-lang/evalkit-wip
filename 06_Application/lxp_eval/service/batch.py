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

* UNA CONSECUENCIA EXTERNA REAL NO CORRE SIN FIRMA HUMANA, y la firma es por
  prueba, no por plan. Desde 2026-11-26 lo que FRENA la corrida es angosto: datos
  de menores reales o escritura en cuenta real, y sólo contra un entorno real
  (`real_environment`); las de crisis/seguridad se marcan y se avisan pero
  CORREN, y un demo o cuentas de prueba no frenan nada (ver `_holds_run`). La
  política `auto` aprueba el plan y deja correr todo lo que no frena; cada prueba
  que sí frena sigue detrás de su propio signoff, que el lote nunca escribe, así
  que `runnable_probes` la deja afuera y su criterio vuelve NO_EVALUADO. El lote
  no firma; sólo elige no esperar por lo que no frena.

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
from .crypto import SecretBox, ServiceError
from .jobs import Engine, JobRunner, Stage, platform_label
# La MISMA aprobación-máquina que POST /jobs bajo "auto". Se importa de
# `ledger_decisions` y NO de `app`: este CLI corre desatendido, sin servidor, y no
# tiene por qué cargar FastAPI para alcanzar una función de store + ledger.
from .ledger_decisions import auto_approve
from .store import JobRecord, JobStore
from .watchdog import Clock, Supervisor, mark_error

logger = logging.getLogger(__name__)

#: Mismo default que `app.DEFAULT_ROOT`: un lote y el servicio comparten el mismo
#: directorio de trabajos, para que un trabajo del lote se vea en la interfaz y
#: la reconciliación al arrancar el servicio lo cubra igual que a cualquier otro.
DEFAULT_ROOT = Path("runs") / "jobs"

VALID_APPROVAL_POLICIES = ("human", "auto")

#: Tope de un documento leído desde una ruta del manifiesto. El mismo que
#: `app.MAX_UPLOAD_BYTES` pone a una subida por la interfaz (un test los ata),
#: repetido acá en vez de importado para no traer el servidor web al CLI. Sin
#: tope, una ruta mal puesta -- un video, un volcado -- se lee entera a memoria
#: antes de que nadie pueda decir que no era un pliego.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
MAX_DOCUMENT_LABEL = "25 MB"

#: La "etapa" que el resumen muestra para una entrada que no llegó a ser un
#: trabajo: falló antes de crear su registro, así que no hay job_id ni etapa del
#: motor que citar. No es un valor de `Stage` a propósito -- no existe en el store.
SETUP_ERROR = "setup_error"


__all__ = [
    "BatchSummary",
    "EXIT_BAD_MANIFEST",
    "EXIT_HALTED",
    "EXIT_JOB_FAILED",
    "EXIT_OK",
    "JobOutcome",
    "Manifest",
    "ManifestEntry",
    "exit_code_for",
    "job_failed",
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
    #: Pasos que se ejecutan antes de CADA prueba, para que no herede el estado
    #: que dejó la anterior (por ejemplo: cambiar de estudiante y entrar con un
    #: nombre nuevo). `{n}` se reemplaza por algo distinto en cada prueba. Va
    #: acá y no en el código porque cada plataforma se deja limpia a su manera;
    #: en la corrida del 2026-11-25 las 120 pruebas compartieron una cuenta y
    #: las tardías encontraron un historial que sus pasos no suponían.
    antes_de_cada_prueba: list[str] = field(default_factory=list)


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
        antes_de_cada_prueba=_as_list(raw.get("antes_de_cada_prueba"), "antes_de_cada_prueba"),
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

    Una ruta que no existe o un archivo desmedido se rechazan ACÁ, con un mensaje
    que nombra el documento, y no con el `FileNotFoundError` pelado de
    `read_bytes`. Quien lee el resumen de un lote de cien trabajos tiene que
    poder saber cuál pliego faltaba sin abrir un traceback. `run_batch` convierte
    este rechazo en un trabajo fallido listado, no en un lote caído.
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
        if not p.is_file():
            raise ValueError(
                f'el documento "{name}" apunta a una ruta que no existe o no es '
                f"un archivo: {path}"
            )
        size = p.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            raise ValueError(
                f'el documento "{name}" pesa {size / (1024 * 1024):.1f} MB y '
                f"supera el límite de {MAX_DOCUMENT_LABEL}: poné sólo la sección "
                "que importa, o una versión sin las imágenes"
            )
        # `p.name`, NO `name`: `extract_text` elige el lector por la EXTENSIÓN
        # del nombre que recibe, y `name` es el título que el manifiesto le puso
        # al documento ("Pliego de la licitación"), que no tiene por qué llevar
        # una. Pasarle el título rechazaba como "formato no soportado" justo el
        # caso del manifiesto de ejemplo: un título más una ruta a un PDF.
        out.append({"name": name, "text": extract_text(p.name, p.read_bytes())})
    return out


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
    #: Dónde quedó el documento del plan, cuando el trabajo era "sólo el plan".
    plan_path: str = ""
    plan_line: str = ""


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
        plan_path=(
            str(store.dir_of(record.job_id) / record.plan_document["html"])
            if (record.plan_document or {}).get("html") else ""
        ),
        plan_line=(record.progress or {}).get("message", "")
        if record.stage == Stage.PLAN_READY else "",
    )


# --- ¿salió bien? El código de salida del CLI --------------------------------
#
# El CLI devolvía 0 SIEMPRE que lograba leer el manifiesto, hubiera pasado lo que
# hubiera pasado con los trabajos. Se vio al comprobar el lote de punta a punta:
# un lote con una entrada en `setup_error` terminaba con el mismo código que uno
# perfecto. Este comando existe para correr dentro de una tarea programada, sin
# nadie mirando, y lo ÚNICO que un programador de tareas lee es el código de
# salida: con 0 fijo, una semana entera de trabajos fallidos se ve verde.
#
# Códigos distintos para causas distintas, porque piden acciones distintas:

#: Todo lo que pedía el manifiesto se atendió y nada falló.
EXIT_OK = 0
#: Al menos un trabajo falló. El resumen dice cuál y por qué.
EXIT_JOB_FAILED = 1
#: El manifiesto no se pudo leer o es inválido: no corrió nada. Es también el
#: código con que argparse rechaza una invocación mal escrita, a propósito: en
#: los dos casos el problema está en CÓMO se llamó al lote, no en el lote.
EXIT_BAD_MANIFEST = 2
#: Ningún trabajo falló, pero el tope de gasto cortó el lote y quedaron entradas
#: sin lanzar. No es una falla de nadie, y tampoco es "listo": hay plataformas
#: del manifiesto que esta corrida no evaluó.
EXIT_HALTED = 3


def job_failed(outcome: JobOutcome) -> bool:
    """¿Este trabajo terminó como tenía que terminar? Estricto a propósito.

    Sólo hay dos finales buenos: `done`, o `awaiting_approval` bajo política
    `human` -- que es el final PEDIDO de ese trabajo, esperar a una persona. Todo
    lo demás cuenta como falla: `error`, una entrada que no se pudo lanzar, un
    registro que desapareció, y también cualquier etapa que no debería ser un
    final (un trabajo `auto` parado en la compuerta, uno que quedó en `run`). La
    lista se escribe por lo que está BIEN y no por lo que está mal para que una
    etapa nueva o un final raro caigan del lado de avisar, no del de callar.

    Un trabajo `done` con todos sus criterios en NO_EVALUADO NO es una falla: no
    haber podido observar es un resultado válido de la evaluación, y la corrida
    que lo produjo funcionó.
    """
    if outcome.stage == Stage.DONE:
        return False
    if outcome.stage == Stage.AWAITING_APPROVAL and outcome.approval_policy == "human":
        return False
    # El final PEDIDO de un trabajo "sólo el plan": el documento está listo y
    # espera a una persona. Sólo cuenta si de verdad hay un documento.
    if outcome.stage == Stage.PLAN_READY and outcome.plan_path:
        return False
    return True


def exit_code_for(summary: BatchSummary) -> int:
    """El código de salida que le corresponde a un lote terminado.

    Un trabajo fallido gana sobre el corte por tope: si pasaron las dos cosas, lo
    que hay que mirar primero es la falla.
    """
    if any(job_failed(o) for o in summary.jobs):
        return EXIT_JOB_FAILED
    if summary.halted:
        return EXIT_HALTED
    return EXIT_OK


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
    plan_only: bool = False,
    corrections: list[dict] | None = None,
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

    UNA ENTRADA MALA NO TUMBA EL LOTE. La preparación de cada entrada -- leer sus
    documentos, crear el registro, cifrar credenciales, agendar -- está aislada:
    si falla, ESA entrada queda listada como fallida con su motivo y el lote
    sigue. La primera versión re-lanzaba la excepción desde acá, y una revisión
    de código lo encontró antes que una corrida real: bastaba un `path:` de
    documento inexistente en la entrada 3 de 100 para que el lote muriera con un
    traceback, sin resumen, y sin esperar a los trabajos que ya estaban en vuelo.
    Para una corrida semanal sin nadie mirando, eso es perder la semana por un
    archivo mal nombrado.

    Lo que SÍ se propaga es lo que no es de una entrada: una cancelación (alguien
    frenó el lote) cancela los trabajos en vuelo -- `Supervisor._guarded` deja el
    motivo en cada uno -- y, pase lo que pase, lo lanzado se espera y el
    supervisor se cierra antes de salir.
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
    #: Una posición por entrada ATENDIDA, en el orden del manifiesto: el job_id
    #: de un trabajo que tiene registro (se lee del store al final), o el
    #: resultado ya armado de una entrada que falló antes de tenerlo.
    slots: list[str | JobOutcome] = []

    def _over_cap() -> bool:
        return max_spend_usd is not None and total_spend >= float(max_spend_usd)

    async def _worker(record: JobRecord, entry: ManifestEntry, policy: str) -> JobRecord:
        nonlocal total_spend
        job_engine = replace(engine, risk_policy=entry.risk_policy)
        runner = runner_factory(store, job_engine, box, clock)
        try:
            if plan_only:
                # SÓLO EL PLAN: recorrido, estrategia, muestreo y el documento
                # con capturas. No se redacta ni se corre ninguna prueba, con
                # cualquier política: aprobar ese documento es de una persona.
                await runner.build_plan_document(record)
                return store.get(record.job_id) or record
            await runner.build_plan(record)
            fresh = store.get(record.job_id) or record
            # Sólo un plan que llegó a la compuerta de aprobación se aprueba:
            # un build_plan que murió (recon fallido, etc.) quedó en `error` y no
            # se ejecuta. Bajo `human` no se aprueba nada: queda esperando, que es
            # el comportamiento de hoy.
            if policy == "auto" and fresh.stage == Stage.AWAITING_APPROVAL:
                auto_approve(store, fresh)
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

    def _credentials_blob(entry: ManifestEntry) -> bytes | None:
        """Las credenciales de la entrada, YA cifradas, o None. Nunca en un log.

        Se resuelven y cifran ANTES de crear el registro: si el cifrado falla, no
        queda un trabajo a medio armar en el disco.
        """
        if not entry.credentials_ref:
            return None
        creds = resolve_credentials(entry.credentials_ref)
        if creds is None:
            logger.info(
                "%s: credentials_ref %s sin variables de entorno; corre sobre la "
                "sesión guardada",
                entry.platform_url, entry.credentials_ref,
            )
            return None
        if box is None:
            logger.warning(
                "%s: hay credenciales para %s pero falta EVALKIT_SECRET_KEY para "
                "cifrarlas; corre sobre la sesión guardada",
                entry.platform_url, entry.credentials_ref,
            )
            return None
        return box.encrypt_credentials(creds[0], creds[1])

    tasks: list[asyncio.Task] = []
    try:
        for index, entry in enumerate(entries):
            if _over_cap():
                halted = True
                break
            await sem.acquire()
            # Un trabajo pudo terminar mientras esperábamos lugar y hacer saltar
            # el tope: re-chequear antes de gastar en uno nuevo.
            if _over_cap():
                sem.release()
                halted = True
                break

            policy = entry.approval_policy or default_approval_policy or "human"
            record: JobRecord | None = None
            try:
                # Lo que puede fallar por culpa del manifiesto -- un documento
                # que no está, credenciales que no cifran -- va ANTES de
                # `store.create`, para que ese fallo no deje un registro huérfano.
                documents = _load_documents(entry.documents)
                blob = _credentials_blob(entry)
                record = store.create(
                    objective=entry.objective,
                    areas=list(entry.areas),
                    platform_url=entry.platform_url,
                    platform=platform_label(entry.platform_url),
                    platform_kind=entry.platform_kind,
                    documents=documents,
                    approval_policy=policy,
                    plan_corrections=list(corrections or []),
                    probe_preamble=list(entry.antes_de_cada_prueba),
                )
                if blob is not None:
                    store.put_credentials(record.job_id, blob)
                    record.has_credentials = True
                    store.save(record)
                tasks.append(supervisor.start(_make_coro(entry, policy), record))
            except Exception as exc:  # noqa: BLE001 -- el motivo es el producto
                # ESTA entrada no se pudo lanzar; las demás sí. Soltar el lugar
                # (o el semáforo se desangra y el lote se cuelga en el próximo
                # acquire), dejar el motivo donde el resumen lo lee, y seguir.
                sem.release()
                motivo = (
                    "no se pudo preparar el trabajo: "
                    f"{exc.__class__.__name__}: {exc}"
                )
                logger.warning(
                    "entrada %d (%s) no se lanzó: %s", index, entry.platform_url, motivo
                )
                if record is not None:
                    # Ya tiene registro: que diga por qué murió. Sin esto queda
                    # en su etapa inicial con latido fresco, y la reconciliación
                    # del servicio lo reportaría más tarde como "murió el
                    # worker", que no es lo que pasó.
                    mark_error(
                        store, record,
                        stage=record.stage,
                        message=motivo,
                        kind=exc.__class__.__name__,
                        at=clock.now(),
                    )
                    slots.append(record.job_id)
                else:
                    slots.append(JobOutcome(
                        job_id="",
                        platform=platform_label(entry.platform_url),
                        stage=SETUP_ERROR,
                        approval_policy=policy,
                        spend_usd=0.0,
                        no_evaluado=0,
                        scored=0,
                        error=motivo,
                    ))
                continue
            except BaseException:
                # No es un fallo de la entrada (cancelación, apagado): soltar el
                # lugar y dejar que el `finally` de abajo cierre lo lanzado.
                sem.release()
                raise
            slots.append(record.job_id)
    except asyncio.CancelledError:
        # Alguien frenó el lote MIENTRAS se lanzaba. Dejar los trabajos corriendo
        # sin lanzador sería gastar sin nadie que lea el resultado: se cancelan,
        # y `_guarded` deja escrito en cada uno que fue cancelado y en qué etapa.
        for task in tasks:
            task.cancel()
        raise
    finally:
        # SIEMPRE: esperar lo lanzado y cerrar el supervisor. `_guarded` ya
        # registró cualquier muerte en el propio registro del trabajo, así que
        # un error acá no es una sorpresa: es un trabajo marcado `error` que el
        # resumen va a listar.
        #
        # El cierre del supervisor va en su PROPIO finally. Lo más común es que
        # la cancelación llegue acá mismo, con todo lanzado y el lote esperando:
        # `gather` se la pasa a los trabajos (que quedan marcados) y la relanza
        # al terminar -- y sin el finally anidado esa relanzada se saltaba el
        # cierre y dejaba a los vigías del supervisor sin dueño.
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if owns_supervisor:
                await supervisor.shutdown()

    if halted:
        # Armado DESPUÉS de esperar a los que estaban en vuelo: la cifra de
        # gasto es la final. Antes se armaba al cortar, y el motivo citaba un
        # total menor que el de la línea "Gasto total" del mismo resumen.
        halted_reason = (
            f"se alcanzó el tope de gasto del lote (máximo "
            f"${float(max_spend_usd):.2f}) y no se lanzaron más trabajos: "
            f"quedaron {len(entries) - len(slots)} de {len(entries)} entradas sin "
            f"lanzar. Gasto total al cierre, con los que estaban en vuelo ya "
            f"terminados: ${total_spend:.2f}."
        )
        logger.warning(halted_reason)

    outcomes = [
        slot if isinstance(slot, JobOutcome) else _outcome(store, slot)
        for slot in slots
    ]
    return BatchSummary(
        jobs=outcomes,
        total_spend_usd=round(total_spend, 4),
        halted=halted,
        halted_reason=halted_reason,
    )


# --- CLI --------------------------------------------------------------------


def _print_summary(summary: BatchSummary) -> None:
    sin_lanzar = sum(1 for o in summary.jobs if o.stage == SETUP_ERROR)
    print(
        f"\nLote terminado: {len(summary.jobs)} entrada(s) atendidas"
        + (f", {sin_lanzar} sin poder lanzarse." if sin_lanzar else ".")
    )
    for o in summary.jobs:
        linea = (
            f"  - {o.job_id or '(sin registro)'}  "
            f"{o.platform or '(sin plataforma)'}  "
            f"[{o.approval_policy}]  etapa={o.stage}  gasto=${o.spend_usd:.2f}"
        )
        if o.stage == Stage.DONE:
            linea += f"  puntuados={o.scored}  NO_EVALUADO={o.no_evaluado}"
        if o.plan_path:
            linea += f"\n      {o.plan_line}\n      Documento del plan: {o.plan_path}"
        if o.error:
            linea += f"  error: {o.error}"
        print(linea)
    print(f"Gasto total del lote: ${summary.total_spend_usd:.2f}")
    if summary.halted:
        print(f"ATENCIÓN: {summary.halted_reason}")
    # La última línea dice lo mismo que el código de salida, en palabras: quien
    # abre el log de la tarea programada no tiene por qué saber qué es un 3.
    fallidos = [o for o in summary.jobs if job_failed(o)]
    code = exit_code_for(summary)
    if code == EXIT_JOB_FAILED:
        print(
            f"RESULTADO: FALLARON {len(fallidos)} de {len(summary.jobs)} "
            f"trabajo(s). El comando termina con código {code}."
        )
    elif code == EXIT_HALTED:
        print(
            "RESULTADO: INCOMPLETO. Ningún trabajo falló, pero el tope de gasto "
            f"dejó entradas sin lanzar. El comando termina con código {code}."
        )
    else:
        print(f"RESULTADO: OK. El comando termina con código {code}.")


async def run_from_plan(
    store: JobStore,
    job_id: str,
    *,
    engine: Engine | None = None,
    box: "SecretBox | None" = None,
    approved_by: str,
    only: list[str] | None = None,
    runner_factory: RunnerFactory | None = None,
    preamble: list[str] | None = None,
) -> JobOutcome:
    """Del plan aprobado al informe: redacta por celda, ejecuta y puntúa.

    `preamble` son los pasos de preparación de cada prueba (ver
    ManifestEntry.antes_de_cada_prueba); si se pasa, reemplaza a los del
    trabajo, para poder agregarlos a un plan que se hizo sin ellos.

    Es el paso que le faltaba al camino "sólo el plan". `approved_by` va tal
    cual al ledger: quién aprobó el documento del plan. Las compuertas no se
    mueven: lo que requiera firma queda sin correr y el informe dice cuántas.
    """
    from .ledger_decisions import approve_from_plan

    record = store.get(job_id)
    if record is None:
        return JobOutcome(job_id, "", "missing", "", 0.0, 0, 0, "el trabajo no existe")
    if preamble is not None:
        record.probe_preamble = [str(s) for s in preamble if str(s).strip()]
        store.save(record)
    runner = (runner_factory or _default_runner_factory)(store, engine or Engine(), box, Clock())
    await runner.draft_from_plan(record, only=only)
    fresh = store.get(job_id) or record
    if fresh.stage == Stage.AWAITING_APPROVAL:
        approve_from_plan(store, fresh, approved_by=approved_by)
        await runner.execute(fresh)
    return _outcome(store, job_id)


def _cli_from_plan(args: argparse.Namespace) -> int:
    store = JobStore(args.root or DEFAULT_ROOT)
    try:
        box: SecretBox | None = SecretBox.from_env()
    except ServiceError:
        box = None
    only = [x.strip() for x in (args.solo or "").split(",") if x.strip()] or None
    preamble = None
    if getattr(args, "antes_de_cada_prueba", None):
        try:
            lines = Path(args.antes_de_cada_prueba).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"no se pudo leer la preparación de cada prueba: {exc}", file=sys.stderr)
            return EXIT_BAD_MANIFEST
        preamble = [x.strip() for x in lines if x.strip() and not x.lstrip().startswith("#")]
    outcome = asyncio.run(run_from_plan(
        store, args.job_id, box=box, approved_by=args.aprobado_por, only=only,
        preamble=preamble,
    ))
    summary = BatchSummary(jobs=[outcome], total_spend_usd=outcome.spend_usd)
    _print_summary(summary)
    return exit_code_for(summary)


def _cli_run(args: argparse.Namespace) -> int:
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError) as exc:
        print(f"no se pudo leer el manifiesto: {exc}", file=sys.stderr)
        return EXIT_BAD_MANIFEST

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

    corrections = None
    if getattr(args, "correcciones", None):
        import yaml

        try:
            loaded = yaml.safe_load(Path(args.correcciones).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            print(f"no se pudieron leer las correcciones: {exc}", file=sys.stderr)
            return EXIT_BAD_MANIFEST
        corrections = [c for c in (loaded or []) if isinstance(c, dict)]

    summary = asyncio.run(
        run_batch(
            manifest.entries,
            store=store,
            engine=Engine(),
            box=box,
            max_parallel=max_parallel,
            max_spend_usd=max_spend,
            default_approval_policy=default_policy,
            plan_only=getattr(args, "command", "run") == "plan",
            corrections=corrections,
        )
    )
    _print_summary(summary)
    return exit_code_for(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m lxp_eval.service.batch",
        description=(
            "Corre un lote de evaluaciones sin intervención, por las mismas "
            "costuras que el servicio. Para una corrida semanal, poné este "
            "comando en una tarea programada del sistema."
        ),
        epilog=(
            "Códigos de salida: 0 = todo se atendió y nada falló; 1 = falló al "
            "menos un trabajo; 2 = el manifiesto o la invocación son inválidos y "
            "no corrió nada; 3 = ningún trabajo falló, pero el tope de gasto dejó "
            "entradas sin lanzar."
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
    planp = sub.add_parser(
        "plan",
        help=(
            "recorrer la plataforma y dejar el documento del plan con capturas, "
            "sin redactar ni correr ninguna prueba"
        ),
    )
    planp.add_argument("manifest", help="ruta al manifiesto YAML")
    planp.add_argument("--root", default=None, help="directorio de trabajos")
    planp.add_argument("--max-parallel", type=int, default=None)
    planp.add_argument("--max-spend-usd", type=float, default=None)
    planp.add_argument(
        "--correcciones", default=None,
        help=(
            "YAML con las correcciones de una persona al plan anterior: una lista "
            "de {tipo: existe|falta, superficie, url, nota}"
        ),
    )
    planp.set_defaults(approval_policy=None)
    fromp = sub.add_parser(
        "desde-plan",
        help=(
            "redactar las pruebas de un plan ya aprobado por una persona, "
            "ejecutarlas (una por celda del muestreo) y dejar el informe"
        ),
    )
    fromp.add_argument("job_id", help="el trabajo que está en la etapa plan_ready")
    fromp.add_argument("--root", default=None, help="directorio de trabajos")
    fromp.add_argument(
        "--aprobado-por", required=True,
        help="quién aprobó el documento del plan; queda escrito en el ledger",
    )
    fromp.add_argument(
        "--solo", default=None,
        help="ids de criterios separados por coma, para un piloto",
    )
    fromp.add_argument(
        "--antes-de-cada-prueba", default=None,
        help=(
            "archivo de texto con un paso por línea, que se ejecuta antes de cada "
            "prueba (por ejemplo, entrar con un estudiante nuevo); {n} cambia en "
            "cada prueba"
        ),
    )
    args = parser.parse_args(argv)
    if args.command == "desde-plan":
        return _cli_from_plan(args)
    if args.command in ("run", "plan"):
        return _cli_run(args)
    parser.print_help(sys.stderr)  # pragma: no cover
    return EXIT_BAD_MANIFEST  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
