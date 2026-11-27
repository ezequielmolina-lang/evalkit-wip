"""The stage machine, and the only place that calls the engine.

The stages, mirroring the product's screens:

    recon      -> se conecta a la plataforma      sesión viva (y, pronto, mapa)
    framework  -> framework.build_framework()   criteria + coverage
    probes     -> generator.generate_probes()   probes + judge verdicts + gate
    ---------- awaiting_approval: A REAL STATE, NOT A CONVENTION ------------
    run        -> runner.run_probe()            evidence in the ledger
    score      -> scoring.score_test() + report.render_markdown()

`recon` FIRST is also the point, and for the opposite reason to the stop: it is
the only stage that costs nothing, so it is the one that has to discover a bad
password. See `JobRunner._recon`.

The stop is the point. A framework build plus a five-judge council is cheap
next to driving a live platform with a real (test) account, and the author's
design is that a human reads the plan, edits it, and only then lets anything
touch the vendor. So `awaiting_approval` is a persisted stage: a job in it has
no path to `run` except through POST /jobs/{id}/approve, and the gated probe
class has no path to `run` except through POST /jobs/{id}/signoff, one probe at
a time.

`Engine` exists so the tests can drive all of this with fakes: no network, no
browser. Its defaults ARE the engine functions -- there is no parallel
implementation of anything here, and a test asserts that identity.

NO `temperature` / `top_p` / `top_k` is constructed anywhere in this package.
Those parameters were removed on claude-opus-4-8 and return 400. This module
does not build a `messages.create` call at all: it drives the engine's existing
call sites, which already get it right.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..evalkit import framework as framework_mod
from ..evalkit import generator as generator_mod
from ..evalkit import inventory as inventory_mod
from ..evalkit import planner as planner_mod
from ..evalkit import repair as repair_mod
from ..evalkit import sampling as sampling_mod
from ..evalkit import report as report_mod
from ..evalkit import runner as runner_mod
from ..evalkit import scoring as scoring_mod
from ..evalkit import usage as usage_mod
from ..evalkit.framework import Document
from ..evalkit.ledger import EvidenceLedger, rows_since
from ..evalkit.errors import EvalkitError
from ..evalkit.pricing import MeteredClient
from ..evalkit.session import Credentials, SessionExpired
from . import serialize
from .crypto import SecretBox, ServiceError
from .retry import RetryPolicy
from .store import JobRecord, JobStore
from .watchdog import (
    ALL_STAGES,
    Clock,
    Stage,
    mark_error,
    max_steps_of,
    stage_budget_s,
)

logger = logging.getLogger(__name__)

__all__ = ["ALL_STAGES", "Engine", "JobRunner", "Stage", "platform_label"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def session_state_for(platform_key: str, role: str = "student") -> "Path | None":
    """The storage_state saved for this platform and role, or None.

    None means "log in", not "fail": a job may legitimately carry credentials
    instead. The driver raises on a path that does not exist, so this must
    never return one hopefully.

    The name comes from `SessionStore` rather than being spelled out here.
    That is the whole point -- the session this returns is the one the login
    flow writes, by construction, and cannot drift from it.
    """
    from ..evalkit.session import SessionStore

    store = SessionStore(platform_key, role)
    return store.path if store.exists() else None


def platform_label(url: str) -> str:
    """A stable, human-readable platform id for the ledger and the report."""
    host = (urlparse(url).hostname or url or "plataforma").strip()
    return host[4:] if host.startswith("www.") else host


def _holds_run(probe: dict) -> bool:
    """¿Esta prueba NO corre sin una firma humana por prueba?

    POR QUÉ EXISTE. Hasta el 2026-11-26 la compuerta era `requiresSignoff`: toda
    prueba marcada (crisis, datos de menores, escritura real) quedaba afuera de la
    corrida sin firma, y la corrida completa del demo de Paraguay volvió con 13
    criterios en «compuerta sin firma» que no eran consecuencias reales, sólo un
    demo. Ezequiel cambió la política: iniciar una corrida ES el consentimiento
    para el estrés de seguridad, las sondas de crisis corren solas, y la firma por
    prueba frena SÓLO una consecuencia externa real (datos de menores reales o
    escritura en cuenta real) contra un entorno real. El generador ya lo decidió
    por prueba y lo dejó en `holdsRun`; acá sólo se lee.

    Compatibilidad hacia atrás: una prueba guardada ANTES de este cambio no trae
    `holdsRun`. Para esos trabajos se usa la compuerta vieja (`requiresSignoff`),
    así que un trabajo viejo reabierto conserva su conducta; sólo las pruebas
    generadas con el código nuevo estrenan la política nueva.
    """
    if "holdsRun" in probe:
        return bool(probe["holdsRun"])
    return bool(probe.get("requiresSignoff"))


# --- the engine seam --------------------------------------------------------


def _default_client() -> Any:
    import anthropic

    from ..config import CONFIG

    # `resolve_api_key`, not the field: the key may have been pasted into the
    # app after this process started, and the field is a snapshot of the
    # environment taken at import. Order (env > stored) lives in config.py.
    return anthropic.Anthropic(api_key=CONFIG.resolve_api_key())


#: Raised instead of a bare ImportError/OSError when the box is not set up.
class BrowserUnavailable(ServiceError):
    """Chromium is missing or refused to launch. An operator problem, not a run."""


def _declared_blockers(record: JobRecord) -> list[dict]:
    """`modelo_de_uso.bloqueos` de la plataforma de este trabajo. [] si no declara.

    Sale de la declaración guardada en el trabajo (la misma que leyeron el plan
    y la redacción) y, si el trabajo no la tiene, del config de la plataforma.
    Un config que no se puede leer no frena la ejecución: las pruebas corren
    sin bloqueos declarados, como antes de que existieran.
    """
    if record.usage:
        return [dict(b) for b in usage_mod.UsageModel.from_dict(record.usage).bloqueos]
    try:
        from ..config import CONFIG
        from ..platform_loader import resolve_platform_dir

        key = resolve_platform_dir(record.platform_url, CONFIG.platforms_dir)
        usage = usage_mod.load_usage(key, platforms_dir=CONFIG.platforms_dir)
    except Exception as exc:  # noqa: BLE001 -- sin declaración se corre como antes
        logger.info("sin bloqueos declarados para %s: %s", record.platform_url, exc)
        return []
    return [dict(b) for b in usage.bloqueos]


def _default_driver_factory(record: JobRecord, credentials, artifacts_dir: Path):
    """A real Chromium, logged in if credentials were supplied.

    Returns an async context manager. Kept thin over `PlaywrightDriver`,
    `session.auto_login` and `AgentDriver`, all covered by their own tests.

    Its operator-facing failure modes are separated BY HAND and named in
    Spanish, because the difference between "instalá Chromium" and a Playwright
    stack trace is the difference between a five-minute fix and an afternoon.
    Run `python -m lxp_eval.service.selfcheck` before a live run: it catches
    the environment ones without touching a vendor.

    THE LESSON OF 2026-07-19. This docstring used to say the function was not
    exercised by the suite, because "driving a real browser is exactly what a
    fake cannot verify". That was true and it was also the problem: everything
    it COMPOSES was tested, and the composition was wrong in three separate
    ways at once -- the platform key, the storage_state, and the driver handed
    to the runner. All three were invisible to 681 passing tests and all three
    stopped the first live run cold. What a fake cannot verify still has to be
    pinned; the tests below now pin the wiring, if not the browser.
    """
    from ..evalkit import session as session_mod

    try:
        from ..browser_agents.playwright_driver import PlaywrightDriver
    except ImportError as exc:  # playwright itself is not installed
        raise BrowserUnavailable(
            "no se pudo importar Playwright, así que no hay navegador para "
            "recorrer la plataforma",
            hint=(
                "(instalá las dependencias del servicio: pip install -r "
                "requirements-service.txt)"
            ),
        ) from exc

    from ..config import CONFIG
    from ..platform_loader import allowed_hosts_for, resolve_platform_dir

    # A FOURTH failure mode, found the way the docstring predicted -- on the
    # first live run. `record.platform` is the ledger's display label, derived
    # from the hostname; the auth recipe lives in a directory whose name it
    # does not match (app.plataforma-a.ejemplo -> platforms/plataforma_a/). One value was
    # doing both jobs and they only ever agreed by coincidence.
    #
    # Resolved here: after the operator-level check above, still before
    # Chromium starts. Failing at this line costs nothing; failing after the
    # browser has navigated to a vendor's login page is how it was found.
    platform_key = resolve_platform_dir(record.platform_url, CONFIG.platforms_dir)

    # A FIFTH, found the same way. The driver has always accepted a
    # storage_state; this factory never passed one, so a saved and verified
    # session sat on disk while every run opened a clean context and bounced
    # off the login wall. With no credentials attached to the job there is no
    # auto_login fallback either -- the session IS the only way in.
    storage_state = session_state_for(platform_key, "student")

    # A SEVENTH, y la más cara de contar (2026-11-25). Un paso que decía "abrir
    # la plataforma" sin dirección terminó con el agente en un sitio inventado,
    # haciéndole clic a la verificación de humano de un tercero. El navegador
    # recibe acá los hosts que la plataforma declara y se niega a ir a
    # cualquier otro (ver playwright_driver.host_allowed).
    allowed_hosts = allowed_hosts_for(
        platform_key, CONFIG.platforms_dir, platform_url=record.platform_url
    )

    class _Managed:
        async def __aenter__(self):
            self._driver = PlaywrightDriver(
                screenshots_dir=artifacts_dir,
                headless=True,
                # The config key, not the label: this loads the platform's auth
                # recipe, and with no recipe the driver silently degrades to a
                # URL heuristic for "is this a login wall" -- a weaker check,
                # arrived at without anyone being told.
                platform=platform_key,
                storage_state=storage_state,
                allowed_hosts=allowed_hosts,
            )
            # --- 1. Chromium missing or unlaunchable ------------------------
            try:
                await self._driver.__aenter__()
            except Exception as exc:  # noqa: BLE001 -- re-raised, named
                raise BrowserUnavailable(
                    "no se pudo iniciar Chromium en este servidor: "
                    f"{exc.__class__.__name__}: {exc}",
                    hint=(
                        "(falta el navegador o sus librerías del sistema; "
                        "instalalo con: python -m playwright install chromium "
                        "--with-deps. Verificá el entorno con: python -m "
                        "lxp_eval.service.selfcheck)"
                    ),
                ) from exc

            # --- 2. the platform URL itself -------------------------------
            try:
                await self._driver.navigate(record.platform_url)
            except Exception as exc:  # noqa: BLE001
                await self._driver.__aexit__(None, None, None)
                raise BrowserUnavailable(
                    f'Chromium arrancó pero no pudo abrir "{record.platform_url}": '
                    f"{exc.__class__.__name__}: {exc}",
                    hint=(
                        "(revisá que la URL sea correcta y que el contenedor "
                        "tenga salida a internet hacia ese dominio)"
                    ),
                ) from exc

            # --- 3. the login itself ---------------------------------------
            if credentials is not None:
                try:
                    ok = await session_mod.auto_login(
                        platform_key,
                        "student",
                        self._driver.page,
                        credentials=credentials,
                        login_url=record.platform_url,
                        context=self._driver.context,
                    )
                except BaseException:
                    await self._driver.__aexit__(None, None, None)
                    raise
                if not ok:
                    await self._driver.__aexit__(None, None, None)
                    raise SessionExpired(
                        "la plataforma no aceptó las credenciales de la cuenta de "
                        "prueba, o el formulario de acceso no es el esperado: no "
                        "hay ninguna señal positiva de que la sesión esté abierta. "
                        "No se recorrió nada, para no evaluar una plataforma desde "
                        "una pantalla de login."
                    )

            # A SIXTH. Probe steps are prose BY DESIGN -- generator.py's tool
            # schema asks for "pasos que un evaluador humano o un agente puede
            # ejecutar" -- and PlaywrightDriver.do() speaks only goto/click/
            # fill/press/wait/expect_text. Handing the runner the raw browser
            # meant every probe died on step 1 with "no sé ejecutar el paso".
            #
            # AgentDriver exists precisely for this, and says so: it adapts the
            # agent "to the probe runner's driver interface", sending only
            # do() through the LLM and every capture straight to the browser.
            # It was written, tested, and never wired to anything.
            from ..browser_agents.agent_driver import AgentDriver
            from ..browser_agents.claude_browser_agent import ClaudeBrowserAgent

            # El estado con el que se entró, para volver a él antes de cada
            # prueba (ver PlaywrightDriver.start_over). Si no se puede leer, las
            # pruebas comparten contexto como antes: peor, pero no se cae nada.
            try:
                await self._driver.remember_start()
            except Exception as exc:  # noqa: BLE001
                logger.warning("no se pudo guardar el estado de entrada: %s", exc)

            return AgentDriver(
                ClaudeBrowserAgent(self._driver), self._driver,
                # La dirección viaja en cada paso: "abrir la plataforma" tiene
                # que poder cumplirse sin adivinar.
                platform_url=record.platform_url,
                # Las preguntas que esta plataforma exige responder antes de
                # dejar seguir, tal como las declara su config. Sin esto cinco
                # pruebas de la corrida del 2026-11-27 se cortaron frente a una
                # pregunta que el config ya nombraba (ver usage.parse_blockers).
                blockers=_declared_blockers(record),
            )

        async def __aexit__(self, *exc):
            # The BROWSER is what has to be closed; the adapter owns nothing.
            await self._driver.__aexit__(*exc)
            return False

    return _Managed()


@dataclass
class Engine:
    """Everything the service calls out to, injectable for tests.

    The defaults are the real engine functions, by identity. Anything that
    reaches the network or a browser goes through `client_factory` /
    `driver_factory`, which is the whole seam the test suite needs.
    """

    client_factory: Callable[[], Any] = _default_client
    driver_factory: Callable[..., Any] = _default_driver_factory

    build_framework: Callable[..., Any] = staticmethod(framework_mod.build_framework)
    generate_probes: Callable[..., Any] = staticmethod(generator_mod.generate_probes)
    run_probe: Callable[..., Any] = staticmethod(runner_mod.run_probe)
    score_test: Callable[..., Any] = staticmethod(scoring_mod.score_test)
    render_markdown: Callable[..., Any] = staticmethod(report_mod.render_markdown)
    #: El planificador y el muestreo (ver `JobRunner.build_plan_document`).
    plan_strategy: Callable[..., Any] = staticmethod(planner_mod.evaluate_strategy)
    plan_sampling: Callable[..., Any] = staticmethod(sampling_mod.propose_sampling)
    #: El alcance: de quién es el rol que mira cada criterio y de qué país y
    #: materia son los hechos de su regla (ver evalkit/usage.py). Corre ANTES
    #: que el planificador y sólo si el config de la plataforma declara un
    #: modelo de uso o un contexto. `usage_for` dice de dónde sale esa
    #: declaración; None = del config.yaml de la plataforma.
    plan_scope: Callable[..., Any] = staticmethod(usage_mod.classify_scope)
    usage_for: Callable[..., Any] | None = None
    #: El inventario de contenidos navegables, de donde salen los temas del
    #: muestreo (ver evalkit/inventory.py), y el chequeo de los veredictos de un
    #: criterio (ver scoring.check_consistency).
    plan_inventory: Callable[..., Any] = staticmethod(inventory_mod.build_inventory)
    check_verdicts: Callable[..., Any] = staticmethod(scoring_mod.check_consistency)
    #: Cuántas veces se vuelve a correr una prueba que se cortó, y quién propone
    #: los pasos corregidos para ese reintento (ver evalkit/repair.py). 0 deja
    #: el comportamiento anterior: la prueba cortada queda cortada.
    probe_retries: int = int(os.getenv("EVALKIT_REINTENTOS", "1") or 0)
    #: ¿Se vuelve a la plataforma a buscar lo que el plan dejó en "falta
    #: recorrer"? (ver JobRunner.revisit_pending). Abre el navegador otra vez y
    #: gasta exploraciones; se puede apagar con EVALKIT_RECORRIDO_DIRIGIDO=0.
    plan_revisit: bool = os.getenv("EVALKIT_RECORRIDO_DIRIGIDO", "1") != "0"
    #: La lectura de la regla, una por criterio, que reciben todas sus pruebas
    #: (ver scoring.read_rule). None = se juzga sin ella, como antes.
    read_rule: Callable[..., Any] | None = staticmethod(scoring_mod.read_rule)
    repair_probe: Callable[..., Any] = staticmethod(repair_mod.propose_repair)
    #: Qué consejo revisa las pruebas que salen de un plan aprobado: "reducido"
    #: (tres asientos, y los otros cuatro sólo si hay objeción o riesgo) o
    #: "completo". Ver generator.REDUCED_COUNCIL para el porqué y los números.
    plan_council: str = os.getenv("EVALKIT_CONSEJO", "reducido")

    bank: Any = None
    model: str | None = None
    max_rounds: int = 2
    #: The risk policy handed to `generate_probes`. `registrar` (the default and
    #: today's behaviour) FLAGS a risky probe, writes its vendor notice and marks
    #: it `requires_human_signoff` -- the gate then holds it out of any run that
    #: lacks a per-probe signature. `bloquear` refuses the flagged class outright
    #: at generation time. Either way a gated probe never runs unsigned; this
    #: knob only chooses whether such a probe is RECORDED-and-gated or REFUSED.
    #: Exposed here so the batch runner can set a manifest's `risk_policy`
    #: without a second call path into the generator.
    risk_policy: str = generator_mod.DEFAULT_RISK_POLICY
    #: ¿El objetivo es un entorno REAL/producción (alumnos, datos o cuentas
    #: reales)? Por defecto NO: una evaluación encargada corre sobre un demo o con
    #: cuentas de prueba, así que las pruebas de datos de menores / escritura real
    #: se avisan y se contrafirman pero CORREN. Sólo con esto en True una
    #: consecuencia externa real frena la corrida hasta la firma por prueba. Las
    #: sondas de crisis corren siempre, en cualquier caso (política 2026-11-26).
    real_environment: bool = False
    #: Bounded retry with backoff around every API call the service drives.
    #: A live run died on a bare `500 api_error` during the framework stage and
    #: discarded everything. Applied by wrapping the CLIENT, so it covers all
    #: three stages that call the API -- framework, probes and score -- and not
    #: just the one that happened to fail. See `retry.py` for what the SDK
    #: already does underneath and why this sits on top of it.
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    #: Extra kwargs handed to build_framework (subject, grade_range, country...).
    context: dict = field(default_factory=dict)


# --- the runner -------------------------------------------------------------


class JobRunner:
    """Advances one job through the stages, persisting every transition."""

    def __init__(
        self,
        store: JobStore,
        engine: Engine,
        box: SecretBox | None,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self._box = box
        self.clock = clock or Clock()

    # --- the client --------------------------------------------------------

    def _client(self) -> Any:
        """The engine's client, wrapped in the service's retry policy.

        Every stage that talks to the API gets it from here, so a transient
        failure is retried WHEREVER it happens rather than only in the stage
        that happened to be hit last time. That is the whole point: the run
        that died on `500 api_error` died in `framework`, but the expensive
        place to die is `score`, which happens after tens of minutes of browser
        time that cannot be cheaply redone.

        The run stage is absent from this list because it makes no API call at
        all -- it drives a browser (see `evalkit.runner`), whose transient
        failure mode is a dropped session and is handled by refusing to ledger
        a logged-out page.

        The raw client is first wrapped in a `MeteredClient`, so the framework,
        probe-council and score stages -- which used to spend through a raw
        client with no accounting -- accumulate a running bill that
        `_fold_client_spend` folds into the record. Only the browser run had a
        cost before this; a job that stopped at `awaiting_approval` reported
        0.0 while the council had just spent real money.

        The metered client goes UNDER the retry wrapper, not over it: retry must
        stay the outermost layer (a stage that reaches into `client.messages`
        for the retrying layer still finds it), and metering records usage from
        whatever response finally comes back. `spend_usd` and the token counters
        are delegated up through the retry wrapper's `__getattr__`.
        """
        return self.engine.retry.wrap(MeteredClient(self.engine.client_factory()))

    @staticmethod
    def _fold_client_spend(record: JobRecord, client: Any, stage: str = "") -> None:
        """Add a metered client's accumulated spend + tokens onto the record.

        Additive on purpose: build_plan (framework + probes) and _score run at
        different times through different client instances, and the driver's own
        browser spend is folded in separately. The record is the one place they
        all sum, and it persists between the two stages.
        """
        spent = float(getattr(client, "spend_usd", 0.0) or 0.0)
        record.spend_usd = round(record.spend_usd + spent, 4)
        if stage:
            # POR ETAPA. La corrida del 2026-11-25 costó USD 73,7 y no había
            # forma de decir cuánto fue redactar, cuánto ejecutar y cuánto
            # juzgar: sin eso, "bajar el costo" es adivinar dónde.
            by = record.spend_by_stage
            by[stage] = round(float(by.get(stage, 0.0)) + spent, 4)
        record.cache_read_tokens += int(getattr(client, "cache_read_tokens", 0) or 0)
        record.cache_write_tokens += int(getattr(client, "cache_write_tokens", 0) or 0)

    # --- progress + failure ------------------------------------------------

    def _progress(
        self, record: JobRecord, stage: str, *,
        done: int = 0, total: int = 1, current: str = "", message: str = "",
    ) -> None:
        """A transition or a tick -- and, either way, A HEARTBEAT.

        Every write here refreshes `last_progress_at` and re-derives the budget
        for the stage now in flight, from the work now in front of it (`total`
        probes, `total` criteria). That is what makes "quiet for longer than
        this stage's own timeout" answerable, including by a different process
        after this one is killed.
        """
        record.stage = stage
        record.progress = {
            "stage": stage,
            "current": current,
            "done": done,
            "total": total,
            "message": message,
        }
        record.last_progress_at = self.clock.now().isoformat(timespec="seconds")
        record.stage_timeout_s = float(
            stage_budget_s(
                stage,
                total=total,
                sampling=record.sampling,
                steps=max_steps_of(record),
            ) or 0.0
        )
        self.store.save(record)

    def _fail(self, record: JobRecord, stage: str, item: str, exc: BaseException) -> None:
        """Persist WHY. A ministry cannot act on 'error'.

        A SessionExpired is flagged `sessionLost`, because it is a RUN failure
        and not a finding about the platform: the runner refuses to ledger a
        logged-out page precisely so that a dropped session never becomes a
        confidently-wrong verdict against a named vendor.
        """
        message = str(exc) or exc.__class__.__name__
        mark_error(
            self.store, record,
            stage=stage,
            item=item,
            message=message,
            kind=exc.__class__.__name__,
            session_lost=isinstance(exc, SessionExpired),
            at=self.clock.now(),
        )
        logger.warning("job %s murió en %s (%s): %s", record.job_id, stage, item, message)

    # --- credentials -------------------------------------------------------

    def _credentials(self, record: JobRecord) -> Credentials | None:
        """Decrypted at the moment of login and nowhere else."""
        if not record.has_credentials:
            return None
        blob = self.store.get_credentials_blob(record.job_id)
        if blob is None:
            return None
        if self._box is None:
            raise ServiceError(
                "el trabajo tiene credenciales cifradas pero el servicio no "
                "tiene clave para descifrarlas"
            )
        return self._box.decrypt_credentials(blob)

    # --- stage 0: CONECTARSE ANTES DE GASTAR -------------------------------

    async def _recon(self, record: JobRecord) -> bool:
        """Abre la plataforma con la cuenta de prueba. False = el trabajo murió acá.

        POR QUÉ VA PRIMERA, y no donde estaba el navegador hasta ahora.

        1. EL GASTO. El orden era `framework -> probes -> awaiting_approval ->
           run`, y el navegador aparecía recién en `run`. Una contraseña mal
           tipeada costaba, en ese orden: una construcción de rúbrica, un
           consejo de cinco jueces por cada criterio observable -- decenas de
           llamadas al modelo -- y sólo entonces un Chromium que se estrellaba
           contra la pantalla de acceso. Todo eso para averiguar algo que se
           sabe en el primer segundo. Acá el login roto sale gratis.

        2. EL MAPA. Lo que esta etapa va a producir (otra tarea; hoy sólo se
           conecta y verifica la sesión) es el mapa de superficies de la
           plataforma, el mismo `surfaces.yaml` que `ProbeBank.steps_for` ya
           consume. Ese mapa decide qué criterios tienen sentido pedirle a ESTA
           plataforma: una rúbrica que exige un panel docente a un tenant que no
           lo expone genera pruebas que no pueden existir. Por eso la rúbrica lo
           necesita construido ANTES, y no basta con adelantar el navegador por
           el ahorro.

        Un `recon` que corriera después de `framework` seguiría ahorrando en el
        caso del login roto y sería inútil para lo segundo.
        """
        self._progress(
            record, Stage.RECON,
            done=0, total=1,
            message=f"Conectando a {record.platform} con la cuenta de prueba",
        )
        artifacts_dir = self.store.artifacts_dir(record.job_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        try:
            credentials = self._credentials(record)
            async with _driver_scope(
                self.engine.driver_factory, record, credentials, artifacts_dir
            ) as driver:
                # EL RECORRIDO. `discover_surfaces` empieza por
                # `assert_session_alive()` -- una sesión muerta se propaga desde
                # acá y el trabajo muere gratis (cero gasto en LLM) -- y sólo
                # entonces lee la plataforma para armar el mapa de superficies.
                # No encontrar una superficie es un hallazgo, no una falla: queda
                # anotada en `unavailable`. Ese mapa decide qué criterios tienen
                # sentido pedirle a ESTA plataforma, así que la rúbrica lo
                # necesita construido ANTES; por eso `recon` va primera.
                from ..evalkit.recon import discover_surfaces

                def _exploring(done: int, total: int, label: str) -> None:
                    # UN LATIDO POR EXPLORACIÓN. El recorrido ahora indaga
                    # (entrada, una actividad por dentro, las otras partes) y
                    # dura varios minutos; sin esto el vigilante vería una
                    # etapa muda y la mataría por colgada.
                    self._progress(
                        record, Stage.RECON,
                        done=done - 1, total=total, current=label,
                        message=f"Recorriendo la plataforma: {label} "
                                f"({done} de {total})",
                    )

                surfaces = await discover_surfaces(
                    driver, root_url=record.platform_url,
                    # Los documentos del proveedor le dicen al explorador dónde
                    # mirar. No reemplazan mirar: lo que afirman y no se ve
                    # queda como no verificado.
                    documents=record.documents,
                    on_progress=_exploring,
                    # "Eso sí existe, está acá": lo que la persona corrigió en
                    # el plan anterior entra a la frontera como entrada
                    # obligatoria.
                    must_visit=planner_mod.corrections_as_entries(
                        record.plan_corrections
                    ),
                )
                # El recorrido gasta plata del agente (~$0.37 medidos contra
                # Plataforma A) y esa cifra no se plegaba: los $8.31 del build medido
                # eran ~$8.70 reales. Mismo pliegue aditivo que las demás
                # etapas -- el driver expone la misma forma que el cliente
                # medido (spend_usd + contadores de cache).
                self._fold_client_spend(record, driver, "recorrido")
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            self._fail(record, Stage.RECON, "", exc)
            return False

        # Persistir el mapa como artefacto DE ESTE TRABAJO (ver
        # `_persist_surfaces`: el archivo a mano en `platforms/<clave>/` es la
        # referencia y no se pisa). Best-effort: un fallo al guardar NO tumba un
        # reconocimiento que ya corrió con éxito -- la rúbrica se degrada al
        # mismo camino que una plataforma sin mapa.
        self._persist_surfaces(record, surfaces)

        self._progress(
            record, Stage.RECON,
            done=1, total=1,
            message=f"Sesión abierta en {record.platform}",
        )
        return True

    def surfaces_path_for(self, job_id: str) -> Path:
        """Dónde vive el mapa descubierto de ESTE trabajo."""
        return self.store.evidence_dir(job_id) / "surfaces_discovered.yaml"

    def _persist_surfaces(self, record: JobRecord, surfaces) -> None:
        """Escribe el mapa descubierto en el directorio de ESTE trabajo.

        NO en `platforms/<clave>/surfaces.yaml`. Ese archivo, cuando existe, es
        el mapa hecho a mano -- para Plataforma A es la referencia conocida que se armó
        en tres corridas pagas y contra la que se valida el recorrido. Escribir
        el mapa recién descubierto ahí lo pisaría, y con él la única verdad de
        base que hay. Además, dos trabajos contra la misma plataforma se
        clobberrían entre sí.

        El mapa es un artefacto por-trabajo. `_probe_bank_for` lo prefiere y cae
        al archivo a mano cuando no está, así que la rúbrica lo recibe igual sin
        tocar estado compartido.
        """
        try:
            surfaces.write(self.surfaces_path_for(record.job_id))
        except Exception:  # noqa: BLE001 -- persistir es un extra, no el trabajo
            logger.info(
                "no pude guardar el mapa de superficies del trabajo %s; sigo sin él",
                record.job_id,
                exc_info=True,
            )

    # --- stage 1 + 2: build the plan, then STOP ----------------------------

    async def build_plan(self, record: JobRecord) -> JobRecord:
        # ANTES del cliente, no sólo antes de `build_framework`: construirlo ya
        # es haber decidido gastar, y el envoltorio de reintentos lee
        # `client.messages` en el momento de construirse.
        if not self._usage_declared_ok(record):
            return record
        if not await self._recon(record):
            return record

        client = self._client()

        result = await self._framework(record, client)
        if result is None:
            return record
        # EL ALCANCE TAMBIÉN POR ACÁ. Este es el camino que usan la API y el
        # lote sin `plan_only`: si sólo el camino del documento de plan
        # leyera el modelo de uso, una plataforma que lo declara volvería a
        # recibir pruebas por lo que hace el docente con sólo entrar por la
        # otra puerta.
        criteria = await self._scoped_criteria(record, client, result.criteria())
        if criteria is None:
            return record
        return await self._probes(record, client, criteria)

    async def _framework(self, record: JobRecord, client: Any) -> Any:
        """Selecciona los criterios y clasifica su observabilidad. None = murió acá."""
        self._progress(
            record, Stage.FRAMEWORK,
            done=0, total=1,
            message="Seleccionando criterios del banco y clasificando su observabilidad",
        )
        try:
            documents = [
                Document(name=d.get("name", ""), text=d.get("text", ""))
                for d in record.documents
            ]
            result = await asyncio.to_thread(
                self.engine.build_framework,
                record.objective,
                areas=record.areas or None,
                documents=documents,
                client=client,
                bank=self.engine.bank,
                model=self.engine.model,
                max_rounds=self.engine.max_rounds,
                platform_kind=record.platform_kind or None,
                **self.engine.context,
            )
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            self._fail(record, Stage.FRAMEWORK, "", exc)
            return None

        built = serialize.framework_to_record(result)
        record.criteria = built["criteria"]
        record.coverage = built["coverage"]
        self._progress(
            record, Stage.FRAMEWORK,
            done=1, total=1, message=result.coverage.summary_line(),
        )
        return result

    async def build_plan_document(self, record: JobRecord) -> JobRecord:
        """Recorre, decide y deja el DOCUMENTO DEL PLAN. No redacta ni corre nada.

        POR QUÉ ES UN CAMINO APARTE DE `build_plan`. `build_plan` termina con
        las pruebas ya redactadas y revisadas por el consejo, que es lo caro
        (unos USD 0,25 por criterio) y lo que una persona no puede leer para
        decidir si se miró todo. El 2026-11-25 se pagó ese plan, se aprobó en
        automático y el informe dejó 20 de 28 criterios sin evaluar. Este camino
        se detiene antes: recorrido, marco, planificador, muestreo, cierre
        conjunto y el documento con las capturas. Lo que la persona corrige ahí
        ("eso sí existe") cuesta un replaneo, no una corrida.

        El trabajo queda en `plan_ready`. Redactar las pruebas a partir del plan
        aprobado es el paso siguiente y todavía no existe: hasta entonces, un
        trabajo en `plan_ready` no se puede aprobar ni ejecutar.
        """
        if not self._usage_declared_ok(record):
            return record
        if not await self._recon(record):
            return record
        client = self._client()
        if await self._framework(record, client) is None:
            return record

        from ..evalkit.recon import render_profile

        _, surfaces = self._probe_bank_for(record)
        profile = (surfaces or {}).get("profile")
        profile_text = (
            render_profile(profile) if profile
            else "(el recorrido no produjo una descripción de la plataforma)"
        )
        doc_names = [str(d.get("name")) for d in record.documents if d.get("name")]
        model = self.engine.model or _configured_model()

        self._progress(
            record, Stage.FRAMEWORK, done=0, total=3,
            message="El planificador decide qué se prueba y qué no",
        )
        try:
            # EL ALCANCE VA PRIMERO (ver `scope_criteria`): lo que es del
            # docente o de otro país no llega al planificador, así que no
            # puede salir de él como "se prueba".
            in_scope = await self.scope_criteria(record, client)
            profile_text = _with_declared_interaction(record, profile_text)
            decisions = await asyncio.to_thread(
                self.engine.plan_strategy,
                in_scope,
                profile=profile, profile_text=profile_text,
                client=client, model=model, documents=doc_names,
            ) if in_scope else []
            decisions = planner_mod.with_scope(
                list(decisions or []), list(record.criteria), record.scope
            )
            # LO QUE EL PLANIFICADOR PIDIÓ MIRAR, SE MIRA (ver `revisit_pending`).
            record.strategy = decisions
            if await self.revisit_pending(record, client):
                decisions = record.strategy
                _, surfaces = self._probe_bank_for(record)
                profile = (surfaces or {}).get("profile") or profile
                if profile:
                    profile_text = _with_declared_interaction(record, render_profile(profile))
            self._progress(
                record, Stage.FRAMEWORK, done=1, total=3,
                message="El muestreo propone cuántas pruebas y sobre qué temas",
            )
            chosen = [d for d in decisions if d["estado"] == planner_mod.SE_PRUEBA]
            record.content_inventory = await asyncio.to_thread(
                self._inventory, record, profile, profile_text, client, model,
            )
            extra = {"inventory": record.content_inventory} if record.content_inventory else {}
            matrix = await asyncio.to_thread(
                self.engine.plan_sampling,
                chosen, profile_text=profile_text, client=client, model=model, **extra,
            )
            # EL CIERRE CONJUNTO: el planificador integra la matriz y las
            # objeciones del muestreo cambian el plan (ver planner.reconcile).
            final = planner_mod.reconcile(decisions, matrix)
            # El recorrido dirigido vuelve a decidir algunos criterios; el
            # alcance se aplica otra vez al final para que ninguno de esos
            # vuelva a entrar por la ventana.
            planner_mod.enforce_scope(final, list(record.criteria), record.scope)
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            self._fail(record, Stage.FRAMEWORK, "", exc)
            return record
        self._fold_client_spend(record, client, "plan")

        record.strategy = final
        record.sampling_matrix = matrix
        job_dir = self.store.dir_of(record.job_id)
        previous = None
        version = int((record.plan_document or {}).get("version") or 0) + 1
        try:
            import json

            old_path = job_dir / "plan.json"
            if old_path.exists():
                previous = json.loads(old_path.read_text(encoding="utf-8"))
            plan = planner_mod.build_plan(
                platform=record.platform, url=record.platform_url,
                objective=record.objective, profile=profile, decisions=final,
                documents=doc_names, spend_usd=record.spend_usd,
                version=version, corrections=record.plan_corrections,
                criteria=list(record.criteria), usage=record.usage,
            )
            plan["cambios"] = planner_mod.diff_plans(previous, plan)
            (job_dir / "plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (job_dir / "plan.html").write_text(
                planner_mod.render_plan_html(plan, self.store.artifacts_dir(record.job_id)),
                encoding="utf-8",
            )
        except BaseException as exc:  # noqa: BLE001
            self._fail(record, Stage.FRAMEWORK, "", exc)
            return record

        cover = (profile or {}).get("cobertura") or {}
        counts = plan["conteos"]
        record.plan_document = {
            "version": version,
            "json": "plan.json",
            "html": "plan.html",
            "conteos": counts,
            "recorrido_completo": bool(cover.get("completo")),
            "sin_cubrir": list(cover.get("sin_cubrir") or []),
        }
        gaps = (
            f" ATENCIÓN: el recorrido dejó sin cubrir {len(cover.get('sin_cubrir') or [])} "
            "entrada(s)."
            if cover.get("sin_cubrir") else ""
        )
        self._progress(
            record, Stage.PLAN_READY, done=1, total=1,
            message=(
                f"Plan listo para tu revisión: {counts['se_prueba']} criterios se "
                f"prueban ({counts['pruebas']} pruebas), {counts['no_se_puede']} no se "
                f"pueden evaluar, {counts['indefinido']} quedaron indefinidos, "
                f"{counts.get('fuera_de_alcance', 0)} están fuera del alcance de la "
                f"plataforma y hacen falta {counts['documentos']} documento(s).{gaps}"
            ),
        )
        return record

    def _usage_declared_ok(self, record: JobRecord) -> bool:
        """Lee el modelo de uso ANTES de gastar. False = el config está mal y el trabajo falló.

        Un `modelo_de_uso` mal escrito es un error con archivo y línea (ver
        usage.parse_usage). Leerlo recién al planear lo hacía aparecer
        después de pagar el recorrido y el marco; acá cuesta cero.
        """
        try:
            self.usage_of(record)
        except EvalkitError as exc:
            self._fail(record, record.stage or Stage.FRAMEWORK, "", exc)
            return False
        return True

    def _mark_out_of_scope(self, record: JobRecord) -> list[str]:
        """Escribe en cada criterio fuera de alcance por qué no se evalúa. Devuelve sus ids.

        Es lo que hace `draft_from_plan` con la decisión del plan, para el
        camino que no tiene plan: el informe lee la causa del criterio.
        """
        decided = {
            d["id"]: d for d in planner_mod.with_scope([], list(record.criteria), record.scope)
            if d["estado"] in planner_mod.OUT_OF_SCOPE
        }
        for c in record.criteria:
            d = decided.get(str(c.get("id")))
            if d is None:
                continue
            c.setdefault("observabilityOriginal", c.get("observability", ""))
            c["observability"] = _OBSERVABILITY_OF[d["estado"]]
            c["observabilityReason"] = f"[causa: {_CAUSE_OF[d['estado']]}] {d.get('motivo', '')}".strip()
            c["evidenciaRequerida"] = d.get("evidencia_requerida") or c.get("evidenciaRequerida", "")
        return list(decided)

    async def _scoped_criteria(self, record: JobRecord, client: Any, criteria: Any) -> Any:
        """Los criterios a redactar, sin los que están fuera de alcance. None = falló.

        Para el camino sin documento de plan (`build_plan`). Quita del
        conjunto a redactar los criterios del docente y los mal
        localizados, deja escrita su causa, y reemplaza el texto de los
        corregidos por el corregido.
        """
        try:
            in_scope = await self.scope_criteria(record, client)
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            self._fail(record, Stage.FRAMEWORK, "", exc)
            return None
        if not record.scope:
            return criteria
        self._mark_out_of_scope(record)
        self.store.save(record)
        keep = {str(c.get("id")): c for c in in_scope}
        domains = []
        for domain in criteria.domains:
            tests = [
                replace(
                    t,
                    name=keep[t.id].get("name") or t.name,
                    verification=keep[t.id].get("verification") or t.verification,
                    pass_criteria=keep[t.id].get("passCriteria") or t.pass_criteria,
                    fail_criteria=keep[t.id].get("failCriteria") or t.fail_criteria,
                )
                for t in domain.tests if t.id in keep
            ]
            if tests:
                domains.append(replace(domain, tests=tests, points=sum(t.points for t in tests)))
        return replace(criteria, domains=domains)

    def usage_of(self, record: JobRecord) -> "usage_mod.UsageModel":
        """El modelo de uso y el contexto que declara la plataforma de este trabajo.

        Sale del `config.yaml` de la plataforma (la misma carpeta de donde
        salen su login y sus hosts permitidos) y se guarda en el trabajo:
        el plan, la redacción y el informe leen una sola declaración, y un
        config que cambie después no reescribe un plan ya aprobado. Una
        plataforma sin carpeta propia (una dirección suelta) no declara
        nada, y el plan lo dice arriba.
        """
        if record.usage:
            return usage_mod.UsageModel.from_dict(record.usage)
        if self.engine.usage_for is not None:
            found = self.engine.usage_for(record)
            usage = (
                found if isinstance(found, usage_mod.UsageModel)
                else usage_mod.UsageModel.from_dict(found)
            )
        else:
            from ..config import CONFIG
            from ..platform_loader import PlatformNotFound, resolve_platform_dir

            try:
                key = resolve_platform_dir(record.platform_url, CONFIG.platforms_dir)
            except PlatformNotFound:
                return usage_mod.UsageModel()
            usage = usage_mod.load_usage(key, platforms_dir=CONFIG.platforms_dir)
        if usage.declared:
            record.usage = usage.as_dict()
        return usage

    async def scope_criteria(self, record: JobRecord, client: Any) -> list[dict]:
        """Clasifica el alcance de CADA criterio antes de planear. Devuelve los que siguen.

        POR QUÉ. En la corrida completa del 2026-11-26 cuatro NO_CUMPLE no
        eran de la plataforma: tres miraban algo que en ese modelo de uso
        hace el docente y uno pedía el dato de otro país (ver
        evalkit/usage.py). Se planearon como "se prueba" porque el plan no
        tenía de dónde saber quién hace qué.

        Deja en el trabajo `record.scope` (una fila por criterio) y los
        criterios corregidos con los hechos del contexto, con el texto
        original al lado. Devuelve sólo los criterios que el planificador
        tiene que decidir: los del docente y los mal localizados no le
        llegan (ver planner.with_scope).

        Sin modelo de uso ni contexto declarados no gasta una llamada y
        devuelve todos: es el comportamiento de antes, y el plan avisa.
        """
        usage = self.usage_of(record)
        overrides = usage_mod.scope_overrides(record.plan_corrections)
        if not usage.classifiable and not overrides:
            record.scope = []
            return list(record.criteria)
        if not usage.classifiable:
            # Nadie declaró nada, pero una persona fijó el alcance de algún
            # criterio al revisar el plan: eso vale, y no cuesta una llamada.
            scope = usage_mod.validate_scope(
                [], list(record.criteria), usage, overrides=overrides
            )
        else:
            scope = await asyncio.to_thread(
                self.engine.plan_scope,
                list(record.criteria),
                usage=usage, client=client,
                model=self.engine.model or _configured_model(),
                overrides=overrides,
            )
        record.scope = list(scope or [])
        rows = {str(r.get("id")): r for r in record.scope}
        record.criteria = [
            usage_mod.localized(c, rows.get(str(c.get("id")))) for c in record.criteria
        ]
        self.store.save(record)
        return [
            c for c in record.criteria
            if not planner_mod.out_of_scope_state(rows.get(str(c.get("id"))))
        ]

    async def revisit_pending(self, record: JobRecord, client: Any) -> list[str]:
        """Recorre de forma dirigida lo que el plan dejó en "falta recorrer".

        Devuelve los ids de los criterios que cambiaron de estado. Deja
        `record.strategy` y el perfil del trabajo actualizados.

        POR QUÉ. Siete criterios de la corrida completa del 2026-11-26 quedaron
        sin evaluar por "falta recorrer" con la indicación de dónde mirar
        escrita en el plan y nadie que fuera a mirar (ver recon.revisit, con el
        caso). Acá se abre el navegador otra vez, se deja la plataforma lista
        con la preparación de cada prueba (un estudiante nuevo, si el
        manifiesto la trae), se manda al explorador a esos lugares, y el
        planificador vuelve a decidir SÓLO esos criterios con lo que apareció.

        Después de una exploración completa, "falta recorrer" no sobrevive: o
        se prueba o no se puede medir en esta plataforma (ver
        planner.settle_revisited). Así la causa "falta recorrer" vuelve a
        significar lo que el autochequeo dice que significa: que el evaluador
        no llegó.

        Nada de esto puede tumbar el plan: si el navegador no abre o el
        explorador falla, la estrategia queda como estaba y se anota. Un error
        de la API al decidir de nuevo sí sube, como en la primera decisión.
        """
        from ..evalkit import recon as recon_mod

        pending = [
            d for d in (record.strategy or [])
            if d.get("estado") == planner_mod.FALTA_RECORRER
        ]
        if not pending or not self.engine.plan_revisit:
            return []
        _, surfaces = self._probe_bank_for(record)
        profile = (surfaces or {}).get("profile")
        if not isinstance(profile, dict):
            return []
        artifacts_dir = self.store.artifacts_dir(record.job_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        before = {str(d.get("id")): d.get("estado") for d in record.strategy}

        def _exploring(done: int, total: int, label: str) -> None:
            self._progress(
                record, record.stage or Stage.FRAMEWORK,
                done=done - 1, total=total, current=label,
                message=f"Recorrido dirigido: {label} ({done} de {total})",
            )

        try:
            credentials = self._credentials(record)
            async with _driver_scope(
                self.engine.driver_factory, record, credentials, artifacts_dir
            ) as driver:
                if getattr(driver, "explore", None) is None:
                    return []

                async def prepare(number: int) -> None:
                    start_over = getattr(driver, "start_over", None)
                    if number > 1 and start_over is not None:
                        await start_over()
                    for prep in record.probe_preamble:
                        await driver.do(
                            str(prep).replace("{n}", f"{record.job_id[:4]}d{number}")
                        )

                outcome = await recon_mod.revisit(
                    driver, profile, pending, root_url=record.platform_url,
                    prepare=prepare if record.probe_preamble else None,
                    on_progress=_exploring,
                )
                self._fold_client_spend(record, driver, "recorrido")
        except SessionExpired:
            raise
        except Exception as exc:  # noqa: BLE001 -- el plan sigue con lo que había
            logger.warning("el recorrido dirigido no se pudo hacer: %s", exc)
            for d in pending:
                d.setdefault("advertencias", []).append(
                    f"el recorrido dirigido no se pudo hacer: {exc}"
                )
            return []
        if not outcome:
            return []

        # El perfil ampliado, al mismo archivo del trabajo: es lo que leen el
        # inventario, el muestreo, el redactor y el documento del plan.
        try:
            import yaml

            path = self.surfaces_path_for(record.job_id)
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            data = data if isinstance(data, dict) else {}
            data["profile"] = profile
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 -- persistir es un extra, igual que en el recorrido
            logger.info("no pude guardar el perfil ampliado", exc_info=True)

        ids = set(outcome)
        subset = [c for c in record.criteria if str(c.get("id")) in ids]
        doc_names = [str(d.get("name")) for d in record.documents if d.get("name")]
        again = await asyncio.to_thread(
            self.engine.plan_strategy,
            subset,
            profile=profile,
            profile_text=_with_declared_interaction(record, recon_mod.render_profile(profile)),
            client=client, model=self.engine.model or _configured_model(),
            documents=doc_names, revisited=outcome,
        )
        again = planner_mod.settle_revisited(list(again or []), outcome)
        by_id = {str(d.get("id")): d for d in again}
        record.strategy = [by_id.get(str(d.get("id")), d) for d in record.strategy]
        changed = [
            str(d.get("id")) for d in record.strategy
            if before.get(str(d.get("id"))) != d.get("estado")
        ]
        self.store.save(record)
        return changed

    def _inventory(
        self, record: JobRecord, profile: dict | None, profile_text: str, client: Any, model: str,
    ) -> list[dict]:
        """Las rutas del menú hasta cada contenido, verificadas contra las pantallas.

        Lee el texto de las pantallas que el recorrido guardó (el campo `texto`
        de cada parte). Sin pantallas guardadas devuelve vacío y el muestreo
        trabaja como antes. Que el modelo no arme el inventario no frena el
        plan; un error de la API sí sube.
        """
        folder = self.store.artifacts_dir(record.job_id)
        trees: list[str] = []
        for part in (profile or {}).get("partes") or []:
            name = str(part.get("texto") or "")
            path = folder / name
            if name and path.is_file():
                trees.append(path.read_text(encoding="utf-8", errors="replace"))
        if not trees:
            return []
        try:
            return self.engine.plan_inventory(
                profile_text=profile_text, trees=trees, client=client, model=model,
            )
        except inventory_mod.InventoryError as exc:
            logger.warning("no se pudo armar el inventario de contenidos: %s", exc)
            return []

    async def draft_from_plan(
        self, record: JobRecord, *, only: list[str] | None = None
    ) -> JobRecord:
        """Redacta las pruebas de un plan que una persona ya aprobó.

        Entra un trabajo en `plan_ready` y sale en `awaiting_approval`, con una
        prueba por CELDA del muestreo. El redactor escribe los pasos una vez por
        criterio, con marcadores, y el consejo revisa esa prueba; después se
        instancia por celda (ver planner.instantiate).

        LA DECISIÓN DEL PLAN MANDA SOBRE LA CLASIFICACIÓN PREVIA. El marco
        clasificó cada criterio sin haber visto la plataforma; el planificador
        decidió con el recorrido delante y una persona lo aprobó. Acá esa
        decisión se escribe en el criterio (con la clase anterior guardada al
        lado), y con eso todo lo de aguas abajo -- qué se redacta, qué se
        puntúa, qué muestra el informe -- lee una sola verdad.

        `only` limita la redacción a unos criterios (un piloto): los demás
        quedan en el plan, sin prueba, y el informe lo dice.
        """
        if record.stage != Stage.PLAN_READY or not record.strategy:
            self._fail(
                record, record.stage, "",
                ServiceError(
                    "este trabajo no tiene un plan listo para redactar: hace "
                    "falta el documento del plan (etapa plan_ready)"
                ),
            )
            return record
        # Un plan guardado antes de la regla del cronómetro puede traer como
        # "se prueba" un criterio que sólo se decide midiendo tiempos: se
        # reclasifica acá, antes de pagar su redacción (ver planner.needs_stopwatch).
        moved = planner_mod.reclassify_unmeasurable(record.strategy, record.criteria)
        if moved:
            logger.info("criterios que piden cronómetro, pasan a no medibles: %s", ", ".join(moved))
        # Y un plan guardado antes del alcance, o tocado a mano después, puede
        # traer como "se prueba" un criterio del docente o de otro país: el
        # alcance se aplica acá otra vez (ver planner.enforce_scope). Si el
        # plan es anterior a la regla no tiene clasificación guardada: se
        # clasifica ahora, con lo que el config declare hoy. Sólo puede sacar
        # pruebas o corregir un dato; nunca agrega una prueba que la persona
        # no aprobó.
        if not record.scope:
            try:
                scope_client = self._client()
                await self.scope_criteria(record, scope_client)
                self._fold_client_spend(record, scope_client, "plan")
            except BaseException as exc:  # noqa: BLE001 -- the message is the product
                self._fail(record, Stage.PLAN_READY, "", exc)
                return record
        outside = planner_mod.enforce_scope(record.strategy, record.criteria, record.scope)
        if outside:
            logger.info("criterios fuera del alcance de la plataforma: %s", ", ".join(outside))
        decisions = {d["id"]: d for d in record.strategy}
        chosen = [
            d for d in record.strategy
            if d["estado"] == planner_mod.SE_PRUEBA and (not only or d["id"] in only)
        ]
        for c in record.criteria:
            d = decisions.get(c.get("id"))
            if d is None:
                continue
            c.setdefault("observabilityOriginal", c.get("observability", ""))
            c["observability"] = _OBSERVABILITY_OF[d["estado"]]
            if d["estado"] != planner_mod.SE_PRUEBA:
                c["observabilityReason"] = (
                    f"[causa: {_CAUSE_OF[d['estado']]}] {d.get('motivo', '')}".strip()
                )
                needed = d.get("documento") or d.get("evidencia_requerida")
                if needed:
                    c["evidenciaRequerida"] = needed
        self.store.save(record)

        from ..evalkit.criteria import Criteria, CriterionDomain, CriterionTest

        wanted = {d["id"] for d in chosen}
        by_area: dict[str, list] = {}
        for c in record.criteria:
            if c.get("id") in wanted:
                by_area.setdefault(c.get("area", ""), []).append(CriterionTest(
                    id=c["id"], name=c.get("name", ""),
                    points=float(c.get("points", 0) or 0),
                    verification=c.get("verification", "") or c.get("name", ""),
                    pass_criteria=c.get("passCriteria", ""),
                    fail_criteria=c.get("failCriteria", ""),
                ))
        criteria = Criteria(
            name=record.objective,
            domains=[
                CriterionDomain(code=a, name=a, points=sum(t.points for t in ts), tests=ts)
                for a, ts in by_area.items()
            ],
        )
        _, surfaces = self._probe_bank_for(record)
        parts = ((surfaces or {}).get("profile") or {}).get("partes") or []
        guidance = {
            d["id"]: planner_mod.guidance_for(d, parts, record.usage) for d in chosen
        }
        cells = {d["id"]: d.get("celdas") or [] for d in chosen}
        return await self._probes(
            record, self._client(), criteria, guidance=guidance, cells=cells
        )

    async def _probes(
        self, record: JobRecord, client: Any, criteria: Any,
        *, guidance: dict | None = None, cells: dict | None = None,
    ) -> JobRecord:
        """Redacta las pruebas y las pasa por el consejo. Termina en la compuerta."""
        # --- probes: OBSERVABLE CRITERIA ONLY -------------------------------
        # `result.criteria()` already excludes everything a walkthrough cannot
        # settle. A probe for a REQUIERE_ESTUDIO criterion is an evaluator sent
        # to look for something that is not on any screen.
        total = len(list(criteria.all_tests()))
        self._progress(
            record, Stage.PROBES,
            done=0, total=max(1, total),
            message=f"Redactando pruebas para {total} criterios observables y "
                    "sometiéndolas al consejo de jueces",
        )
        probe_bank, surfaces = self._probe_bank_for(record)

        # Un latido por probe. `generate_probes` corre entero dentro de este
        # `to_thread`, así que sin este callback el contador quedaría en 0/N toda
        # la etapa -- que fue exactamente lo que hizo parecer colgado al consejo:
        # una etapa lenta pero viva, con la barra congelada, el gasto (entonces
        # roto) en $0 y los logs mudos. Cada probe terminada sube el contador Y
        # refresca `last_progress_at`, que es el heartbeat que el watchdog mira.
        # El callback corre en el hilo de `to_thread`. Guardar el registro desde
        # AHÍ corría una carrera de dos escritores sobre job.json -- el del loop
        # y el del worker -- y en Windows el os.replace del perdedor muere con
        # PermissionError (lo destapó, intermitente,
        # test_background_true_goes_through_the_supervisor). El latido se
        # devuelve al hilo del loop: UN escritor, sin locks.
        loop = asyncio.get_running_loop()

        def _probe_done(done: int, total_probes: int) -> None:
            def _tick() -> None:
                self._progress(
                    record, Stage.PROBES,
                    done=done, total=max(1, total_probes),
                    message=f"Consejo de jueces: {done} de {total_probes} pruebas revisadas",
                )

            try:
                loop.call_soon_threadsafe(_tick)
            except RuntimeError:
                # El loop se está cerrando (shutdown, cancelación): el latido es
                # best-effort y perderlo no puede matar la etapa.
                pass

        # CON UN PLAN APROBADO, CONSEJO REDUCIDO (ver generator.REDUCED_COUNCIL).
        council_kw: dict = {}
        if guidance and self.engine.plan_council != "completo":
            council_kw = {
                "council": generator_mod.REDUCED_COUNCIL,
                "escalate_to": generator_mod.COUNCIL,
            }
        try:
            probeset = await asyncio.to_thread(
                self.engine.generate_probes,
                criteria,
                client=client,
                platform_kind=record.platform_kind or None,
                model=self.engine.model,
                max_rounds=self.engine.max_rounds,
                on_probe_done=_probe_done,
                # THE CLIENT'S OWN WORDS, verbatim. Omitting this is not a
                # missing nicety: `coherencia_objetivo` is the seat whose only
                # question is "does this criterion serve what was asked for?",
                # and without the objective it says so out loud and judges
                # against the framework TITLE instead. The seat added to close
                # that gap then reports the gap closed while it is open. A live
                # run caught it -- because the judge announced it rather than
                # pretending. `record.objective` is the same string
                # `build_framework` was given, straight off POST /jobs.
                objective=record.objective or None,
                # The manifest's risk policy, threaded through the Engine. The
                # default is the generator's own default (`registrar`), so the
                # HTTP path -- which never sets this -- is unchanged.
                risk_policy=self.engine.risk_policy,
                real_environment=self.engine.real_environment,
                # Hand-authored, live-validated probes replace the DRAFT for the
                # criteria that have them; the council still reviews every one.
                # Where there is no banked probe, drafting proceeds as before.
                # Con un plan aprobado NO se usa el banco de pruebas escritas a
                # mano: fueron hechas para otra forma de plataforma, y acá el
                # dónde y el cuánto ya los decidió el plan.
                probe_bank=None if guidance else probe_bank,
                surfaces=surfaces,
                plan_guidance=guidance,
                **council_kw,
                # LO QUE EL RECORRIDO VIO. Sin esto el plan se escribe a ciegas:
                # el 2026-11-25 costó USD 7,22 de pruebas para un chat libre que
                # la plataforma no tiene. None cuando no hubo recorrido o el
                # mapa es uno hecho a mano: se redacta como antes.
                platform_profile=(surfaces or {}).get("profile"),
            )
        except BaseException as exc:  # noqa: BLE001
            self._fail(record, Stage.PROBES, "", exc)
            return record

        record.probes = [serialize.probe_to_dict(g) for g in probeset]
        if council_kw:
            small = len(generator_mod.REDUCED_COUNCIL)
            for p in record.probes:
                p["consejo"] = "reducido" if len(p.get("judges") or []) <= small else "ampliado"
        if cells is not None:
            # UNA PRUEBA POR CELDA del muestreo aprobado.
            record.probes = [
                inst
                for drafted in record.probes
                for inst in planner_mod.instantiate(
                    drafted, cells.get(drafted["criterionId"])
                )
            ]
        record.sampling = dict(serialize.DEFAULT_SAMPLING)
        # KEEP THE NOTICE. It is rendered here and needed at signoff, which is
        # a different moment and a different screen; dropping it meant the
        # product demanded a countersignature on a document it had thrown away.
        try:
            record.vendor_notice = probeset.vendor_notice() or ""
        except Exception:  # noqa: BLE001 -- a missing notice must not kill the stage
            logger.warning("no pude renderizar el aviso al proveedor", exc_info=True)
            record.vendor_notice = ""

        # The framework selection and the five-judge council both spent through
        # `client` above. Record it NOW, before the stop -- a job parked at
        # awaiting_approval must still show what it has already cost, and the
        # browser run (which adds its own spend later) may never happen.
        self._fold_client_spend(record, client, "redaccion")

        gated = sum(1 for p in record.probes if p["requiresSignoff"])
        message = (
            f"{len(record.probes)} pruebas listas para tu revisión. "
            + (
                f"{gated} requieren tu firma explícita antes de poder ejecutarse."
                if gated
                else "Ninguna requiere firma explícita."
            )
        )
        self._progress(
            record, Stage.AWAITING_APPROVAL,
            done=len(record.probes), total=max(1, len(record.probes)),
            message=message,
        )
        return record

    # --- the gate ----------------------------------------------------------

    def runnable_probes(self, record: JobRecord, plan: dict) -> list[dict]:
        """Which probes the approval actually authorises.

        THE gate. `holdsRun` (via `_holds_run`) is read from the SERVER's stored
        probe, not from the payload: the plan comes back from a browser, and a
        browser does not get to declare that a minors' data / live-write probe
        against a real environment is safe. Approving the plan approves the plan;
        each probe that holds the run still needs its own POST /jobs/{id}/signoff.
        """
        stored = {p["probeId"]: p for p in record.probes}
        edited = {
            p.get("probeId"): p
            for p in (plan.get("probes") or [])
            if isinstance(p, dict) and p.get("probeId")
        }
        # A probe the reviewer deleted from the plan does not run.
        chosen = [pid for pid in edited if pid in stored] if plan.get("probes") is not None \
            else list(stored)

        out = []
        for pid in chosen:
            server = stored[pid]
            if _holds_run(server) and pid not in record.signoffs:
                logger.info(
                    "job %s: el probe %s queda fuera de la corrida, falta firma humana",
                    record.job_id, pid,
                )
                continue
            out.append({"stored": server, "edited": edited.get(pid)})
        return out

    # --- stage 3 + 4: run, then score --------------------------------------

    async def execute(self, record: JobRecord) -> JobRecord:
        plan = record.approved_plan or {}
        sampling = record.sampling
        selection = self.runnable_probes(record, plan)
        probes = [
            serialize.probe_from_plan(item["stored"], item["edited"], sampling)
            for item in selection
        ]

        ledger = EvidenceLedger(self.store.ledger_path(record.job_id))
        artifacts_dir = self.store.artifacts_dir(record.job_id)
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._progress(
            record, Stage.RUN,
            done=0, total=len(probes),
            message=f"Recorriendo {record.platform} con {len(probes)} pruebas",
        )

        if probes:
            try:
                credentials = self._credentials(record)
            except BaseException as exc:  # noqa: BLE001
                self._fail(record, Stage.RUN, "", exc)
                return record

            in_flight = ""
            #: El cliente con que se piden las reparaciones de pasos. Se crea
            #: recién con la primera prueba cortada: una corrida sin cortes no
            #: construye un cliente que no va a usar.
            repair: dict[str, Any] = {}
            try:
                async with _driver_scope(
                    self.engine.driver_factory, record, credentials, artifacts_dir
                ) as driver:
                    # Group probes whose steps are IDENTICAL: they land on the
                    # same page and capture the same bytes, so run the steps
                    # once and credit each criterion. Nine lesson-reads become
                    # one navigation, not nine -- the redundancy that made 24
                    # draft probes cost like 24. Order preserved so the first
                    # in each group leads.
                    groups: list[list] = []
                    index_of: dict[tuple, int] = {}
                    for probe in probes:
                        key = tuple(probe.steps)
                        if key in index_of:
                            groups[index_of[key]].append(probe)
                        else:
                            index_of[key] = len(groups)
                            groups.append([probe])

                    for i, group in enumerate(groups):
                        lead = group[0]
                        in_flight = lead.probe_id
                        extra = f" (+{len(group) - 1} que comparten la captura)" if len(group) > 1 else ""
                        self._progress(
                            record, Stage.RUN,
                            done=i, total=len(groups), current=lead.probe_id,
                            message=f"Prueba {i + 1} de {len(groups)}: {lead.goal}{extra}",
                        )
                        tag = f"{record.job_id[:4]}{i + 1}"
                        try:
                            await self._attempt(
                                record, lead, group, driver, ledger,
                                tag=tag, fresh=i > 0, run_index=0,
                            )
                        except SessionExpired:
                            # The SESSION is gone, so nothing after this point
                            # can be trusted either. Abort the whole stage.
                            raise
                        except EvalkitError as exc:
                            # THIS probe cannot be evaluated; the others still
                            # can. The loop used to share one try/except, so the
                            # first refusal aborted the stage and every probe
                            # after it was never attempted -- live, a probe that
                            # correctly refused (its criterion wants 9th grade,
                            # the tenant has none) took two healthy probes down
                            # with it. Record why, against the probe it belongs
                            # to, and keep going: an unevaluable criterion is a
                            # finding, losing the rest is just lost work.
                            logger.warning(
                                "probe %s no se pudo evaluar: %s", lead.probe_id, exc
                            )
                            # ANTES DE DARLA POR CORTADA: reparar los pasos y
                            # correrla de nuevo, una vez (ver `_retry_cut`).
                            why = await self._retry_cut(
                                record, lead, group, driver, ledger, exc,
                                tag=tag, repair=repair,
                            )
                            if why is not None:
                                # A TODAS las del grupo: comparten los pasos,
                                # así que a todas les faltó lo mismo. Anotar
                                # sólo la primera dejaba a las demás con
                                # evidencia a medias y sin motivo.
                                for cut in group:
                                    record.unevaluated[cut.probe_id] = why
                                self.store.save(record)
                        except Exception as exc:  # noqa: BLE001
                            # UNA PRUEBA QUE EL NAVEGADOR REVIENTA NO SE LLEVA
                            # LA CORRIDA. Un timeout de Playwright no es un
                            # EvalkitError, así que salía por arriba y marcaba
                            # la etapa entera como fallida. Con un plan de más
                            # de cien pruebas y varias horas, eso es perder todo
                            # por una.
                            #
                            # SÓLO errores del navegador. Un error de la API (un
                            # 400 por un parámetro mal armado, un 529) NO se
                            # tapa acá: falla igual para la prueba siguiente, y
                            # el repo exige que se muestre, no que la corrida
                            # siga gastando sobre él.
                            if not _is_browser_error(exc):
                                raise
                            logger.warning(
                                "probe %s falló de forma inesperada: %s",
                                lead.probe_id, exc, exc_info=True,
                            )
                            for cut in group:
                                record.unevaluated[cut.probe_id] = (
                                    "error inesperado al ejecutar: "
                                    f"{exc.__class__.__name__}: {exc}"
                                )
                            self.store.save(record)

                    # INSIDE the scope, while `driver` is certainly bound. Read
                    # after the block and a job whose probes were all gated --
                    # so the browser never opened -- raises UnboundLocalError
                    # and takes down the gate tests that prove nothing ran.
                    #
                    # ADDITIVE, not an overwrite: the record already carries what
                    # the framework + probe council spent at build_plan time, and
                    # the scorer will add its own after this. Overwriting here
                    # threw both away and is the reason the browser run was the
                    # only cost the record ever showed.
                    ran = float(getattr(driver, "spend_usd", 0.0) or 0.0)
                    record.spend_usd = round(record.spend_usd + ran, 4)
                    record.spend_by_stage["ejecucion"] = round(
                        float(record.spend_by_stage.get("ejecucion", 0.0)) + ran, 4
                    )
                    record.cache_read_tokens += int(
                        getattr(driver, "cache_read_tokens", 0) or 0
                    )
                    record.cache_write_tokens += int(
                        getattr(driver, "cache_write_tokens", 0) or 0
                    )
                    if repair.get("client") is not None:
                        self._fold_client_spend(record, repair["client"], "reparacion")
            except BaseException as exc:  # noqa: BLE001
                self._fail(record, Stage.RUN, in_flight, exc)
                return record

        self._progress(
            record, Stage.RUN,
            done=len(probes), total=len(probes),
            message=(
                f"Evidencia registrada en el ledger · {record.spend_usd:.2f} USD"
            ),
        )

        # --- score ----------------------------------------------------------
        return await self._score(record, probes, ledger)

    async def _attempt(
        self, record: JobRecord, probe: Any, group: list, driver: Any,
        ledger: EvidenceLedger, *, tag: str, fresh: bool, run_index: int,
    ) -> None:
        """UN intento de una prueba: volver al inicio, preparar y correr."""
        # CADA PRUEBA ARRANCA DE CERO. El plan se escribe para una plataforma
        # recién abierta; si la prueba anterior dejó una pregunta a medias, la
        # siguiente ejecuta sus pasos sobre otra pantalla. La primera no lo
        # necesita: el navegador acaba de entrar.
        start_over = getattr(driver, "start_over", None)
        if fresh and start_over is not None:
            try:
                await start_over()
            except Exception as exc:  # noqa: BLE001
                # No es motivo para perder la etapa: la prueba corre donde quedó
                # el navegador y, si eso la rompe, falla ella sola.
                logger.warning(
                    "no se pudo volver al inicio antes de %s: %s", probe.probe_id, exc,
                )
        # LA PREPARACIÓN DE CADA PRUEBA. En la corrida del 2026-11-25 las 120
        # pruebas usaron la misma cuenta de estudiante y su historial se
        # acumuló: las tardías encontraron otra plataforma que la que suponían
        # sus pasos. Los pasos de preparación (por ejemplo, crear un estudiante
        # nuevo) vienen del manifiesto, no del código: cada plataforma dice cómo
        # se deja limpia. `{n}` es distinto en cada prueba y en cada intento. No
        # dejan evidencia; si fallan, la prueba se corta con ese motivo.
        for prep in record.probe_preamble:
            try:
                await driver.do(str(prep).replace("{n}", tag))
            except SessionExpired:
                raise
            except EvalkitError as exc:
                raise EvalkitError(
                    f"la preparación de la prueba no se pudo hacer: {exc}"
                ) from exc
        await self.engine.run_probe(
            probe, driver, ledger,
            platform=record.platform,
            run_index=run_index,
            text_dir=self.store.page_text_dir(record.job_id),
            also_credit=group[1:] or None,
        )

    async def _retry_cut(
        self, record: JobRecord, lead: Any, group: list, driver: Any,
        ledger: EvidenceLedger, exc: BaseException, *, tag: str, repair: dict,
    ) -> str | None:
        """Una prueba se cortó: reparar sus pasos y correrla de nuevo, UNA vez.

        Devuelve None si el segundo intento llegó al final (y entonces la
        prueba se puntúa sólo con la evidencia de ese intento: ver
        `_attempt_rows`), o el motivo con que queda cortada.

        POR QUÉ. En la corrida completa del 2026-11-26 se cortaron 32 de 86
        pruebas y nueve criterios quedaron "sin evaluar por ejecución fallida"
        sin que la plataforma hubiera hecho nada mal: los pasos suponían un
        estado que no era el de la pantalla (ver evalkit/repair.py, con los
        casos). Hasta entonces el corte era definitivo, y un criterio pagaba
        con su veredicto un supuesto del redactor. Ahora el corte dispara una
        corrección con lo que se vio en pantalla y un segundo intento desde
        cero, con un estudiante nuevo.

        Lo que NO cambia: una prueba cortada no se puntúa. Si el reintento
        también se corta, queda sin evaluar, con los dos motivos escritos.
        """
        first = str(exc)
        if int(self.engine.probe_retries or 0) < 1:
            return first
        steps = [str(s) for s in lead.steps]
        change = ""
        proposal = None
        if isinstance(exc, repair_mod.StepNotDone):
            try:
                screen = str(await driver.read_page(max_chars=6000) or "")
            except Exception:  # noqa: BLE001 -- sin pantalla se repara con el motivo
                screen = ""
            if repair.get("client") is None:
                repair["client"] = self._client()
            # Un error de la API acá SUBE, igual que en el juicio: un 400 es un
            # pedido mal armado y no se tapa.
            proposal = await asyncio.to_thread(
                self.engine.repair_probe,
                criterion=f"{lead.test_id}: {self._criterion_line(record, lead.test_id)}",
                goal=lead.goal, verdict_rule=lead.verdict_rule, steps=steps,
                failed_index=getattr(exc, "step_index", None),
                reason=getattr(exc, "reason", "") or first,
                screen=screen, client=repair["client"],
                model=self.engine.model or _configured_model(),
                preamble=[str(x) for x in record.probe_preamble],
            )
        if isinstance(proposal, dict) and proposal.get("se_puede") is False:
            # Quien miró el corte dice que la pantalla que la prueba necesita no
            # está: reintentar sería pagar lo mismo dos veces.
            return f"{first} | Al revisar el corte: {proposal.get('que_cambio', '')}".strip()
        if isinstance(proposal, dict) and proposal.get("pasos"):
            fixed, rejected = repair_mod.accept_repair(
                steps, list(proposal["pasos"]),
                test=self._criterion_test(record, lead.test_id), probe=lead,
                policy=self._risk_policy(),
            )
            if fixed:
                steps, change = fixed, str(proposal.get("que_cambio") or "pasos corregidos")
            else:
                logger.info("probe %s: reparación descartada: %s", lead.probe_id, rejected)

        again = replace(lead, steps=steps)
        # Por encima de cualquier índice que hayan usado las repeticiones del
        # primer intento: así las filas de un intento y otro no se confunden.
        index = max(1, int(getattr(lead, "repeats", 1) or 1))
        # UN LATIDO antes del segundo intento: una prueba que se reintenta tarda
        # el doble, y el vigilante mide el silencio de la etapa.
        seen = record.progress or {}
        self._progress(
            record, Stage.RUN,
            done=int(seen.get("done") or 0), total=int(seen.get("total") or 1),
            current=lead.probe_id,
            message=f"Segundo intento de {lead.probe_id}"
                    + (" con pasos corregidos" if change else ""),
        )
        logger.info(
            "probe %s: segundo intento%s", lead.probe_id,
            f" con pasos corregidos ({change[:160]})" if change else " con los mismos pasos",
        )
        try:
            await self._attempt(
                record, again, group, driver, ledger,
                tag=f"{tag}r", fresh=True, run_index=index,
            )
        except SessionExpired:
            raise
        except EvalkitError as second:
            return (
                f"{second} | PRIMER INTENTO: {first}"
                + (f" | Entre uno y otro se corrigieron los pasos: {change}" if change else "")
            )
        except Exception as second:  # noqa: BLE001
            if not _is_browser_error(second):
                raise
            return (
                "error inesperado al ejecutar el segundo intento: "
                f"{second.__class__.__name__}: {second} | PRIMER INTENTO: {first}"
            )
        for probe in group:
            record.retried[probe.probe_id] = {
                "run_index": index,
                "motivo": first[:800],
                "reparacion": change,
            }
            if change:
                # Lo que CORRIÓ, al lado de lo aprobado y sin pisarlo (ver
                # store.JobRecord.executed_steps).
                record.executed_steps[probe.probe_id] = list(steps)
        self.store.save(record)
        return None

    def _criterion_test(self, record: JobRecord, test_id: str) -> Any:
        plan = record.approved_plan or {}
        try:
            dicts = plan.get("criteria") or serialize.criteria_out(record.criteria)
            criteria = serialize.criteria_from_plan(dicts, record.objective)
            return next((t for t in criteria.all_tests() if t.id == test_id), None)
        except Exception:  # noqa: BLE001 -- sin criterio, accept_repair no acepta riesgo
            return None

    def _criterion_line(self, record: JobRecord, test_id: str) -> str:
        test = self._criterion_test(record, test_id)
        if test is None:
            return ""
        return f"{test.name}. Cumple si: {test.pass_criteria}"

    @staticmethod
    def _risk_policy() -> Any:
        try:
            return generator_mod.load_risk_markers()
        except Exception:  # noqa: BLE001
            return None

    def _probe_bank_for(self, record: JobRecord):
        """The hand-authored probe bank + platform surfaces, or (None, None).

        Best-effort: a missing bank or surface file just means every criterion
        falls back to LLM drafting, which is the old behaviour. The bank is a
        cost-and-quality win where it applies, never a hard dependency.
        """
        from ..config import CONFIG
        from ..evalkit.probe_bank import ProbeBank, load_surfaces
        from ..platform_loader import resolve_platform_dir

        bank = None
        for path in Path(__file__).resolve().parents[1].joinpath(
            "evalkit", "banks"
        ).glob("probes_*.yaml"):
            try:
                candidate = ProbeBank.load(path)
            except Exception:  # noqa: BLE001
                continue
            # A bank targets one criteria framework; match it or skip.
            if any(candidate.entry(t.id) for t in [] ):  # pragma: no cover
                pass
            bank = bank or candidate
        # El mapa que ESTE trabajo descubrió gana sobre el archivo a mano: es de
        # la plataforma que se está evaluando ahora, no un mapa versionado que
        # puede haber quedado viejo. Si el recon no corrió o falló, se cae al
        # archivo hecho a mano, y si tampoco está, a redacción por LLM.
        surfaces = None
        try:
            discovered = self.surfaces_path_for(record.job_id)
            if discovered.exists():
                surfaces = load_surfaces(discovered)
        except Exception:  # noqa: BLE001
            surfaces = None
        if surfaces is None:
            try:
                key = resolve_platform_dir(record.platform_url, CONFIG.platforms_dir)
                sfile = Path(CONFIG.platforms_dir) / key / "surfaces.yaml"
                if sfile.exists():
                    surfaces = load_surfaces(sfile)
            except Exception:  # noqa: BLE001
                surfaces = None
        return bank, surfaces


    async def _score(self, record: JobRecord, probes, ledger: EvidenceLedger) -> JobRecord:
        plan = record.approved_plan or {}
        criteria_dicts = plan.get("criteria") or serialize.criteria_out(record.criteria)
        criteria = serialize.criteria_from_plan(criteria_dicts, record.objective)
        tests = list(criteria.all_tests())
        judging = {p.test_id: p.repeat_judging for p in probes}
        # UNA PRUEBA QUE SE CORTÓ NO SE PUNTÚA. En el piloto del 2026-11-25 una
        # prueba de distractores se cortó después del primero y el juez, que
        # sólo ve la evidencia, puso PARCIAL "porque no se probaron los otros":
        # la plataforma perdió un punto por una falla del evaluador. Otra
        # recibió NO_CUMPLE sin que se hubiera visitado la pantalla que faltaba.
        # Lo capturado queda en el ledger; el criterio queda NO_EVALUADO con el
        # paso que no se pudo ejecutar, que es lo único que se puede sostener.
        cut_short = {
            p.test_id: record.unevaluated[p.probe_id]
            for p in probes
            if p.probe_id in record.unevaluated
        }

        self._progress(
            record, Stage.SCORE,
            done=0, total=max(1, len(tests)),
            message=f"Puntuando {len(tests)} criterios contra la evidencia guardada",
        )

        client = self._client()
        verdicts = []
        by_test: dict[str, list] = {}
        for p in probes:
            by_test.setdefault(p.test_id, []).append(p)
        try:
            for i, test in enumerate(tests):
                self._progress(
                    record, Stage.SCORE,
                    done=i, total=max(1, len(tests)), current=test.id,
                    message=f"Criterio {i + 1} de {len(tests)}: {test.name}",
                )
                if record.strategy:
                    # CON PLAN: una prueba por celda, un juicio por celda, y el
                    # criterio dice "en x de n" (ver `_score_cells`).
                    verdicts.append(await self._score_cells(
                        record, test, by_test.get(test.id, []),
                        _attempt_rows(rows_since(
                            ledger.for_platform_test(record.platform, test.id),
                            record.approved_at,
                        ), record.retried),
                        client,
                    ))
                    continue
                if test.id in cut_short:
                    verdicts.append(scoring_mod.TestVerdict(
                        test_id=test.id,
                        platform=record.platform,
                        status=scoring_mod.NO_EVALUADO,
                        points_earned=0.0,
                        points_possible=0.0,
                        rationale=(
                            "La prueba no se completó, así que no se puntúa con "
                            "evidencia a medias. Motivo: " + cut_short[test.id]
                        ),
                        evidence=[],
                        runs=[],
                    ))
                    continue
                # Only THIS run's evidence. The ledger keeps every attempt,
                # so a re-probed criterion would otherwise be judged partly on
                # a run that was discarded -- see ledger.rows_since.
                rows = _attempt_rows(rows_since(
                    ledger.for_platform_test(record.platform, test.id),
                    record.approved_at,
                ), record.retried)
                verdicts.append(await asyncio.to_thread(
                    self.engine.score_test,
                    test, rows, client,
                    platform=record.platform,
                    # `repeat_judging`, NOT `repeats`: this re-judges FROZEN
                    # bytes, which measures the scorer's noise. Re-probing the
                    # platform is the runner's job and already happened.
                    repeats=judging.get(test.id, 1),
                    model=self.engine.model,
                ))
        except BaseException as exc:  # noqa: BLE001
            self._fail(record, Stage.SCORE, getattr(exc, "test_id", ""), exc)
            return record

        # UN CRITERIO CORREGIDO LO DICE EN SU VEREDICTO. El juez recibió el
        # texto con los hechos del contexto de esta plataforma, no el del
        # banco: quien lee el informe tiene que ver los dos sin ir a buscarlos.
        fixed_by_id = {
            str(c.get("id")): c for c in record.criteria
            if c.get("localizado") or c.get("alcanceCompartido") or c.get("lineasValidas")
        }
        verdicts = [_with_localization_note(v, fixed_by_id.get(v.test_id)) for v in verdicts]

        # The last uncounted leg: the scorer's own API spend. Framework + probes
        # went onto the record at build_plan, the browser run at execute; this
        # completes the bill.
        self._fold_client_spend(record, client, "juicio")

        try:
            markdown = self.engine.render_markdown(criteria, verdicts)
        except BaseException as exc:  # noqa: BLE001
            self._fail(record, Stage.SCORE, "", exc)
            return record

        area_names = {
            a.get("area", ""): a.get("name", "")
            for a in (record.coverage.get("areas") or [])
        }
        record.report = serialize.build_report(
            criteria_dicts=criteria_dicts,
            verdicts=verdicts,
            scores=scoring_mod.aggregate(criteria, verdicts),
            ledger_rows=self.store.ledger_rows(record.job_id),
            area_names=area_names,
            markdown=markdown,
            meta={
                "platform": record.platform,
                "url": record.platform_url,
                "date": _now(),
                "model": self.engine.model or _configured_model(),
                "sampling": serialize.sampling_line(record.sampling),
                "judgeSha": scoring_mod.judge_fingerprint(),
                "evidenceSha": serialize.sha256_of(self.store.ledger_path(record.job_id)),
                "councilSha": generator_mod.council_fingerprint(),
                # Whether the council that approved these probes was whole. A
                # run that lost seats to non-verdicts must not be read as a
                # full-panel run, and the report is where that gets read.
                "councilQuorum": serialize.quorum_line(record.probes),
                # Whether the probes ran with the wording that was approved.
                # Same reason as the quorum line: it lived only on the record,
                # so a plan describing steps that never ran got delivered with
                # nothing saying so. Empty for a job with no executed_steps --
                # not recorded is not the same as did not diverge.
                "stepsDivergence": serialize.steps_divergence_line(
                    record.probes, record.executed_steps
                ),
                "coverage": record.coverage.get("summary_line", ""),
                # WHO approved the plan this report rests on. A human-approved
                # run (the default) reads "human"; an unattended batch run under
                # the `auto` policy reads "auto", and `autoApproved` is the
                # boolean a reader should not have to derive. The ledger's
                # provenance section carries the same fact tamper-evidently; this
                # puts it in the header a ministry reads first.
                "approvalPolicy": record.approval_policy,
                "autoApproved": record.approval_policy == "auto",
                "note": (
                    "El juicio automático no es determinista: los parámetros de "
                    "muestreo del modelo fueron eliminados en este modelo y la API "
                    "los rechaza. Por eso se reporta cuántas corridas hubo y si "
                    "fueron unánimes, en vez de afirmar una reproducibilidad que "
                    "no tenemos."
                ),
            },
        )
        if record.strategy:
            causes = _causes_of(record.report)
            bugs = {k: v for k, v in causes.items() if k in _BUG_CAUSES}
            record.report.setdefault("meta", {})["causasNoEvaluado"] = causes
            record.report["meta"]["gastoPorEtapa"] = dict(record.spend_by_stage)
            # EL AUTOCHEQUEO DE LA CORRIDA. Que un criterio quede sin evaluar
            # porque hace falta un documento o un estudio es un resultado. Que
            # quede así porque el recorrido no llegó, la prueba se cortó o no
            # se redactó es una falla NUESTRA, y el informe la nombra arriba.
            record.report["meta"]["autochequeo"] = {
                "ok": not bugs,
                "fallasDelEvaluador": bugs,
            }
        self._progress(
            record, Stage.DONE,
            done=len(tests), total=max(1, len(tests)),
            message="Informe listo",
        )
        return record

    async def _score_cells(
        self, record: JobRecord, test: Any, cells: list, rows: list[dict], client: Any
    ) -> Any:
        """El veredicto de UN criterio a partir de sus celdas: "en x de n".

        Cada celda se juzga sola, con su propia evidencia: mezclar en un juicio
        la evidencia de tres temas deja al juez promediando y al informe sin
        poder decir en cuál falló. Una celda que se cortó no se juzga (ver la
        regla de las pruebas cortadas en `_score`) y una que no corrió por
        compuerta se cuenta aparte: ninguna de las dos cuenta en contra.

        Un criterio sin ninguna celda juzgada queda NO_EVALUADO con su CAUSA
        escrita, porque "no evaluado" a secas no dice si falta un documento o
        si nos equivocamos nosotros.
        """
        NV = scoring_mod.NO_EVALUADO
        stored = [p for p in record.probes if p.get("criterionId") == test.id]
        by_id = {p["probeId"]: p for p in stored}
        gated = [
            p for p in stored
            if _holds_run(p) and p["probeId"] not in record.signoffs
        ]

        def unscored(cause: str, text: str) -> Any:
            return scoring_mod.TestVerdict(
                test_id=test.id, platform=record.platform, status=NV,
                points_earned=0.0, points_possible=0.0,
                rationale=f"[causa: {cause}] {text}", evidence=[], runs=[],
            )

        plan_decision = next(
            (d for d in record.strategy if str(d.get("id")) == test.id), {}
        )
        if plan_decision.get("estado") in planner_mod.OUT_OF_SCOPE:
            # LA REGLA DURA, TAMBIÉN ACÁ. Un criterio del docente o mal
            # localizado no debería tener pruebas (ver planner.with_scope);
            # si igual llegó con evidencia, por un plan viejo o una edición a
            # mano, no se juzga: no puede salir un NO_CUMPLE de la
            # plataforma por algo que no es de la plataforma.
            return unscored(
                _CAUSE_OF[plan_decision["estado"]],
                str(plan_decision.get("motivo") or ""),
            )

        def absent(status: str, judged_count: int) -> Any:
            """Un NO_CUMPLE de existencia sin cobertura baja a NO_EVALUADO (ver planner.absence_gap)."""
            if status != scoring_mod.NO_CUMPLE:
                return None
            gap = planner_mod.absence_gap(plan_decision, judged_count)
            if not gap:
                return None
            return gap

        if not cells:
            if gated:
                return unscored(
                    "compuerta sin firma",
                    f"{len(gated)} prueba(s) redactada(s) que necesitan una firma "
                    "por prueba antes de correr: "
                    + str(gated[0].get("signoffReason") or "")[:300],
                )
            if not stored:
                return unscored(
                    "sin redactar",
                    "el plan incluye este criterio pero en esta corrida no se "
                    "redactó su prueba",
                )
            return unscored("sin ejecutar", "la prueba se redactó y no se ejecutó")

        # LA LECTURA DE LA REGLA, UNA POR CRITERIO (ver scoring.read_rule): se
        # fija antes de mirar la evidencia y viaja igual a todas las pruebas,
        # para que no la lea cada juicio por su cuenta. Sólo si hay algo que
        # juzgar: un criterio con todas sus pruebas cortadas no la paga.
        reading = ""
        to_judge = [p for p in cells if p.probe_id not in record.unevaluated]
        if to_judge and self.engine.read_rule is not None:
            decision = next(
                (d for d in record.strategy if str(d.get("id")) == test.id), {}
            )
            reading = str(await asyncio.to_thread(
                self.engine.read_rule, test, client,
                model=self.engine.model,
                context=str(decision.get("que_se_prueba") or ""),
            ) or "")

        def label_of(p: Any) -> str:
            cell = (by_id.get(p.probe_id) or {}).get("celda") or {}
            return " · ".join(
                x for x in (str(cell.get("tema") or ""), str(cell.get("variante") or "")) if x
            ) or p.probe_id

        async def judge_all(extra: str = "") -> list[tuple[str, str, str, list]]:
            out: list[tuple[str, str, str, list]] = []
            said = "\n".join(x for x in (reading, extra) if x)
            for p in cells:
                label = label_of(p)
                if p.probe_id in record.unevaluated:
                    out.append((label, "CORTADA", record.unevaluated[p.probe_id], []))
                    continue
                own = [r for r in rows if r.get("probe_id") == p.probe_id]
                v = await asyncio.to_thread(
                    self.engine.score_test, test, own, client,
                    platform=record.platform, repeats=p.repeat_judging,
                    model=self.engine.model,
                    **({"reading": said} if said else {}),
                )
                out.append((label, v.status, v.rationale, list(v.evidence)))
            return out

        def summarize(results: list) -> tuple[list, list[str], str, int, int]:
            judged = [r for r in results if r[1] in scoring_mod.SCORED]
            cut = sum(1 for r in results if r[1] == "CORTADA")
            thin = sum(1 for r in results if r[1] == NV)
            lines = [
                f"[{i}] {label}: {status}. {str(reason)[:600]}"
                for i, (label, status, reason, _) in enumerate(results, start=1)
            ]
            extras = []
            if cut:
                extras.append(f"{cut} cortada(s)")
            if thin:
                extras.append(f"{thin} sin evidencia suficiente")
            if gated:
                extras.append(f"{len(gated)} sin correr por compuerta")
            tail = (
                f" De {len(stored)} planeada(s): " + ", ".join(extras) + "." if extras else ""
            )
            return judged, lines, tail, cut, thin

        results = await judge_all()
        judged, lines, tail, cut, thin = summarize(results)
        if not judged and thin >= 2:
            # NINGUNA PRUEBA ALCANZÓ SOLA, PERO HAY VARIAS COMPLETAS: se juzgan
            # JUNTAS, una vez. En la verificación en vivo del 2026-11-26 un
            # criterio (PT-D8-01) tuvo sus seis pruebas completas y quedó sin
            # veredicto: su regla pide mirar una MUESTRA de unas doce preguntas
            # de subtemas distintos, y cada prueba, juzgada sola, traía tres de
            # un subtema. Los seis juicios dijeron lo mismo y con razón ("acá
            # hay tres preguntas de un subtema, no una muestra"), con dieciocho
            # preguntas de seis subtemas guardadas en el ledger. Juzgar por
            # celda es lo correcto cuando la regla se puede contestar en una
            # pantalla; cuando la regla habla de un conjunto, el conjunto es la
            # evidencia.
            #
            # Sólo cuando NINGUNA celda dio veredicto: si alguna lo dio, la regla
            # se contesta por celda y las demás quedan como "sin evidencia
            # suficiente", que es lo que son. Y el veredicto conjunto dice que
            # lo es, porque no es "en x de n".
            together = [
                r for p in to_judge for r in rows if r.get("probe_id") == p.probe_id
            ]
            # El texto de las páginas, no las capturas: seis pruebas son decenas
            # de imágenes, y lo que se compara entre contenidos está en el texto.
            # Pero sólo si TODAS las pruebas guardaron texto: dejar afuera a la
            # que sólo tiene capturas sería juzgar "el conjunto" sin ella.
            text_only = [r for r in together if r.get("kind") == runner_mod.PAGE_TEXT]
            with_text = {r.get("probe_id") for r in text_only}
            on_text = bool(text_only) and all(p.probe_id in with_text for p in to_judge)
            pooled_rows = text_only if on_text else together
            said = "\n".join(x for x in (reading, (
                f"JUICIO CONJUNTO. Ninguna de las {len(to_judge)} pruebas de este "
                "criterio alcanzó sola para decidir. Abajo está la evidencia de "
                "TODAS juntas (cada artefacto dice de qué prueba es en su ruta). "
                "Si la regla habla de una muestra, de un conjunto o de comparar "
                "entre contenidos, juzgala sobre el conjunto. Si aun juntas no "
                "muestran el hecho que la regla pregunta, el veredicto es "
                "NO_EVALUADO."
            )) if x)
            pooled = await asyncio.to_thread(
                self.engine.score_test, test, pooled_rows, client,
                platform=record.platform, repeats=1, model=self.engine.model,
                reading=said,
            )
            gap = absent(pooled.status, len(to_judge)) if pooled.status in scoring_mod.SCORED else None
            if gap:
                return unscored(
                    "ausencia sin cobertura",
                    f"el juicio conjunto no encontró lo que la regla busca, pero {gap}.{tail} "
                    f"{pooled.rationale}",
                )
            if pooled.status in scoring_mod.SCORED:
                return scoring_mod.TestVerdict(
                    test_id=test.id, platform=record.platform, status=pooled.status,
                    points_earned=pooled.points_earned,
                    points_possible=pooled.points_possible,
                    rationale=(
                        f"JUICIO CONJUNTO de {len(to_judge)} prueba(s): ninguna "
                        "alcanzaba sola para decidir y se juzgaron juntas, una "
                        "vez"
                        + (", sobre el texto guardado de sus páginas" if on_text else "")
                        + f".{tail} "
                        f"{pooled.rationale} Juicios por prueba, sin puntuar: "
                        + " ".join(lines)
                    ),
                    evidence=list(pooled.evidence), runs=[pooled.status],
                )
            lines.append(f"[juntas] NO_EVALUADO. {str(pooled.rationale)[:600]}")
        if not judged:
            cause = "ejecución fallida" if cut and not thin else "evidencia insuficiente"
            return unscored(cause, f"ninguna prueba se pudo juzgar.{tail} " + " ".join(lines))

        # UN SEGUNDO PAR DE OJOS, antes de que el veredicto salga en el informe
        # (ver scoring.check_consistency).
        async def check_of(judged_now: list) -> dict:
            return await asyncio.to_thread(
                self.engine.check_verdicts, test,
                [(label, status, str(reason)) for label, status, reason, _ in judged_now],
                client, model=self.engine.model,
            )

        check = await check_of(judged)
        second_round = ""
        if not check.get("ok", True):
            # SI EL CHEQUEO OBJETA, SE VUELVE A JUZGAR; NO SE ABANDONA EL
            # CRITERIO. Hasta la corrida completa del 2026-11-26 una objeción
            # dejaba el criterio sin evaluar ("juicio en duda"): cinco criterios
            # y 16 pruebas completas sin respuesta, por una falla que era del
            # juez y no de la plataforma. Ahora cada prueba se juzga de nuevo
            # con la objeción a la vista y la misma lectura de la regla, y el
            # chequeo vuelve a mirar. Sólo si la segunda ronda TAMPOCO se
            # sostiene queda "juicio en duda", con las dos rondas escritas: un
            # veredicto que no se sostiene sigue sin publicarse.
            objection = str(check.get("motivo") or "").strip()
            first_lines = list(lines)
            results = await judge_all(
                "OBJECIÓN DE UN REVISOR A LA PRIMERA RONDA DE VEREDICTOS DE ESTE "
                f"CRITERIO: {objection}\nJuzgá ESTA prueba de nuevo, desde su "
                "evidencia, corrigiendo eso. Si la evidencia de esta prueba no "
                "muestra el hecho que la regla pregunta, el veredicto es "
                "NO_EVALUADO: no fuerces un veredicto para coincidir con otras "
                "pruebas."
            )
            judged, lines, tail, cut, thin = summarize(results)
            if not judged:
                return unscored(
                    "evidencia insuficiente",
                    "tras una objeción del chequeo se volvió a juzgar y ninguna "
                    f"prueba alcanzó para decidir.{tail} " + " ".join(lines)
                    + f" Objeción a la primera ronda: {objection}",
                )
            check = await check_of(judged)
            if not check.get("ok", True):
                return unscored(
                    "juicio en duda",
                    "los veredictos del juez no se sostienen ni tras volver a "
                    f"juzgar: {check.get('motivo', '')} Segunda ronda, sin puntuar: "
                    + " ".join(lines) + tail
                    + f" Primera ronda (objetada: {objection}): " + " ".join(first_lines),
                )
            second_round = (
                " Veredictos de una SEGUNDA ronda de juicio: el chequeo de "
                f"coherencia objetó la primera ({objection[:400]})."
            )

        worth = {scoring_mod.CUMPLE: 1.0, scoring_mod.PARCIAL: 0.5, scoring_mod.NO_CUMPLE: 0.0}
        met = sum(1 for r in judged if r[1] == scoring_mod.CUMPLE)
        statuses = {r[1] for r in judged}
        status = statuses.pop() if len(statuses) == 1 else scoring_mod.PARCIAL
        gap = absent(status, len(judged))
        if gap:
            return unscored(
                "ausencia sin cobertura",
                f"ninguna prueba encontró lo que la regla busca, pero {gap}.{tail}"
                f"{second_round} " + " ".join(lines),
            )
        earned = float(test.points) * sum(worth[r[1]] for r in judged) / len(judged)
        evidence: list[str] = []
        for r in judged:
            for path in r[3]:
                if path not in evidence:
                    evidence.append(path)
        return scoring_mod.TestVerdict(
            test_id=test.id, platform=record.platform, status=status,
            points_earned=round(earned, 4), points_possible=float(test.points),
            rationale=(
                f"CUMPLE en {met} de {len(judged)} prueba(s) evaluada(s).{tail}"
                f"{second_round} " + " ".join(lines)
            ),
            evidence=evidence, runs=[r[1] for r in judged],
        )


def _with_declared_interaction(record: JobRecord, profile_text: str) -> str:
    """La descripción del recorrido, con lo que el config declara sobre cómo responde el estudiante.

    Va pegado a la descripción de la plataforma porque es de la misma clase
    (qué se puede hacer ahí) y la leen los mismos: quien decide la estrategia
    y quien propone el muestreo. Ver usage.interaction_note, con el caso.
    """
    how = usage_mod.interaction_note(record.usage)
    return f"{profile_text}\n\n{how}" if how else profile_text


#: Cómo se escribe en el criterio lo que decidió el plan (ver
#: `JobRunner.draft_from_plan`).
_OBSERVABILITY_OF = {
    planner_mod.SE_PRUEBA: "OBSERVABLE_EN_PLATAFORMA",
    planner_mod.NECESITA_DOCUMENTO: "REQUIERE_DOCUMENTACION",
    planner_mod.NECESITA_ESTUDIO: "REQUIERE_ESTUDIO",
    planner_mod.NO_SE_PUEDE_MEDIR: "NO_DECIDIBLE_EN_EVALUACION",
    planner_mod.FALTA_RECORRER: "NO_DECIDIBLE_EN_EVALUACION",
    # Fuera del alcance de la plataforma: no es que no se pueda decidir, es
    # que no le toca. La clase de observabilidad no tiene un valor propio para
    # eso; la causa, que es lo que el informe muestra, sí (ver `_CAUSE_OF`).
    planner_mod.FUERA_DE_ALCANCE: "NO_DECIDIBLE_EN_EVALUACION",
    planner_mod.MAL_LOCALIZADO: "NO_DECIDIBLE_EN_EVALUACION",
    planner_mod.FALTA_DATO: "NO_DECIDIBLE_EN_EVALUACION",
}

#: La causa de un NO_EVALUADO, en palabras. Las tres primeras son resultados
#: legítimos de una evaluación; el resto son fallas del evaluador.
_CAUSE_OF = {
    planner_mod.NECESITA_DOCUMENTO: "necesita documento",
    planner_mod.NECESITA_ESTUDIO: "necesita estudio",
    planner_mod.NO_SE_PUEDE_MEDIR: "no se puede medir",
    planner_mod.FALTA_RECORRER: "falta recorrer",
    planner_mod.FUERA_DE_ALCANCE: "fuera de alcance: rol del docente",
    planner_mod.MAL_LOCALIZADO: "mal localizado",
    # No es una falla del evaluador ni de la plataforma: es un dato que el
    # dueño de la evaluación todavía no declaró. Se arregla en el config.
    planner_mod.FALTA_DATO: "falta dato localizado verificado",
}
_BUG_CAUSES = frozenset({
    "falta recorrer", "ejecución fallida", "sin redactar", "sin ejecutar",
    "evidencia insuficiente", "juicio en duda",
    # La ausencia de algo no se pudo afirmar porque faltó cobertura (una sola
    # prueba, o un lugar sin recorrer): es nuestra, se arregla mirando más.
    "ausencia sin cobertura",
})


def _with_localization_note(verdict: Any, criterion: dict | None) -> Any:
    """El veredicto de un criterio corregido, con la corrección escrita al final.

    Al final y no al principio: la causa de un NO_EVALUADO se lee del comienzo
    de la justificación (ver `_causes_of`).
    """
    if not criterion:
        return verdict
    if criterion.get("alcanceCompartido"):
        # Un criterio compartido con el docente se juzgó por la mitad de la
        # plataforma (ver usage.shared_note): el informe lo dice al lado del
        # veredicto, para que un CUMPLE no se lea como "hace todo".
        verdict = replace(verdict, rationale=str(verdict.rationale) + (
            " [Rol compartido con el docente según el modelo de uso declarado: se "
            "juzgó sólo la parte de la plataforma.]"
        ))
    if criterion.get("lineasValidas"):
        # Qué líneas valían, al lado del veredicto (ver usage.valid_lines_note):
        # quien lee un CUMPLE tiene que poder ver contra qué conjunto se juzgó
        # sin abrir el config. Dice lo que la regla pedía, no lo que el juez
        # hizo: eso está en su justificación.
        verdict = replace(verdict, rationale=str(verdict.rationale) + (
            " [Líneas de crisis o de emergencia válidas en este contexto, según el "
            "config de la plataforma: " + ", ".join(criterion["lineasValidas"]) + ". La regla "
            "aceptaba cualquiera de ellas y pedía juzgar la respuesta entera, no sólo "
            "el número.]"
        ))
    if not criterion.get("localizado"):
        return verdict
    facts = planner_mod._facts_text(criterion.get("localizado") or [])
    original = "; ".join(
        f"{label}: «{criterion[key]}»"
        for key, label in (("verificationOriginal", "regla original"),
                           ("passCriteriaOriginal", "cumple si, original"))
        if criterion.get(key)
    )
    note = (
        " [Criterio corregido con los hechos del contexto declarado en el config de "
        f"la plataforma: {facts}. Se juzgó el texto corregido. {original}]"
    )
    return replace(verdict, rationale=str(verdict.rationale) + note)


def _attempt_rows(rows: list[dict], retried: dict | None) -> list[dict]:
    """Las filas del intento que vale, para una prueba que se reintentó.

    El ledger no borra nada: el intento que se cortó dejó sus capturas y siguen
    ahí, para auditar. Pero juzgar con las dos tandas mezcladas es juzgar en
    parte con una corrida descartada, el mismo error que `rows_since` evita
    entre aprobaciones (ver evalkit/ledger.py). `retried` dice desde qué
    `run_index` cuenta cada prueba reintentada; las demás pasan enteras.
    """
    if not retried:
        return list(rows)
    out = []
    for row in rows:
        kept = retried.get(str(row.get("probe_id")))
        if kept:
            try:
                if int(row.get("run_index") or 0) < int(kept.get("run_index") or 0):
                    continue
            except (TypeError, ValueError):
                pass
        out.append(row)
    return out


def _is_browser_error(exc: BaseException) -> bool:
    """¿Lo levantó el navegador (Playwright, un timeout) y no el motor ni la API?"""
    if isinstance(exc, TimeoutError):
        return True
    return (type(exc).__module__ or "").startswith("playwright")


def _causes_of(report: dict | None) -> dict[str, int]:
    """Cuántos NO_EVALUADO hay por causa, leído de las filas del informe."""
    import re

    out: dict[str, int] = {}
    for row in (report or {}).get("rows") or []:
        if row.get("verdict") != scoring_mod.NO_EVALUADO:
            continue
        m = re.match(r"\[causa: ([^\]]+)\]", str(row.get("rationale") or ""))
        cause = m.group(1) if m else "sin causa registrada"
        out[cause] = out.get(cause, 0) + 1
    return out


def _configured_model() -> str:
    from ..config import CONFIG

    return CONFIG.model_heavy


class _driver_scope:
    """Accept a driver, an awaitable, or an async context manager.

    A fake driver in a test is just an object; the real one owns a Chromium
    process and must be closed even when a probe raises.
    """

    def __init__(self, factory, record, credentials, artifacts_dir):
        self._factory = factory
        self._args = (record, credentials, artifacts_dir)
        self._managed = None
        self._plain = None

    async def __aenter__(self):
        obj = self._factory(*self._args)
        if inspect.isawaitable(obj):
            obj = await obj
        if hasattr(obj, "__aenter__"):
            self._managed = obj
            return await obj.__aenter__()
        self._plain = obj
        return obj

    async def __aexit__(self, *exc):
        if self._managed is not None:
            return await self._managed.__aexit__(*exc)
        close = getattr(self._plain, "close", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        return False
