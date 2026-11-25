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
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..evalkit import framework as framework_mod
from ..evalkit import generator as generator_mod
from ..evalkit import planner as planner_mod
from ..evalkit import sampling as sampling_mod
from ..evalkit import report as report_mod
from ..evalkit import runner as runner_mod
from ..evalkit import scoring as scoring_mod
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
    def _fold_client_spend(record: JobRecord, client: Any) -> None:
        """Add a metered client's accumulated spend + tokens onto the record.

        Additive on purpose: build_plan (framework + probes) and _score run at
        different times through different client instances, and the driver's own
        browser spend is folded in separately. The record is the one place they
        all sum, and it persists between the two stages.
        """
        record.spend_usd = round(
            record.spend_usd + float(getattr(client, "spend_usd", 0.0) or 0.0), 4
        )
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
                self._fold_client_spend(record, driver)
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
        if not await self._recon(record):
            return record

        client = self._client()

        result = await self._framework(record, client)
        if result is None:
            return record
        return await self._probes(record, client, result.criteria())

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
            decisions = await asyncio.to_thread(
                self.engine.plan_strategy,
                list(record.criteria),
                profile=profile, profile_text=profile_text,
                client=client, model=model, documents=doc_names,
            )
            self._progress(
                record, Stage.FRAMEWORK, done=1, total=3,
                message="El muestreo propone cuántas pruebas y sobre qué temas",
            )
            chosen = [d for d in decisions if d["estado"] == planner_mod.SE_PRUEBA]
            matrix = await asyncio.to_thread(
                self.engine.plan_sampling,
                chosen, profile_text=profile_text, client=client, model=model,
            )
            # EL CIERRE CONJUNTO: el planificador integra la matriz y las
            # objeciones del muestreo cambian el plan (ver planner.reconcile).
            final = planner_mod.reconcile(decisions, matrix)
        except BaseException as exc:  # noqa: BLE001 -- the message is the product
            self._fail(record, Stage.FRAMEWORK, "", exc)
            return record
        self._fold_client_spend(record, client)

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
                f"pueden evaluar, {counts['indefinido']} quedaron indefinidos y hacen "
                f"falta {counts['documentos']} documento(s).{gaps}"
            ),
        )
        return record

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
                if d.get("documento"):
                    c["evidenciaRequerida"] = d["documento"]
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
        guidance = {d["id"]: planner_mod.guidance_for(d, parts) for d in chosen}
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
                # Hand-authored, live-validated probes replace the DRAFT for the
                # criteria that have them; the council still reviews every one.
                # Where there is no banked probe, drafting proceeds as before.
                # Con un plan aprobado NO se usa el banco de pruebas escritas a
                # mano: fueron hechas para otra forma de plataforma, y acá el
                # dónde y el cuánto ya los decidió el plan.
                probe_bank=None if guidance else probe_bank,
                surfaces=surfaces,
                plan_guidance=guidance,
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
        self._fold_client_spend(record, client)

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

        THE gate. `requiresSignoff` is read from the SERVER's stored probe, not
        from the payload: the plan comes back from a browser, and a browser does
        not get to declare that a crisis / minors' data / live-write probe is
        safe. Approving the plan approves the plan; each gated probe still needs
        its own POST /jobs/{id}/signoff.
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
            if server["requiresSignoff"] and pid not in record.signoffs:
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
                        try:
                            # CADA PRUEBA ARRANCA DE CERO. El plan se escribe
                            # para una plataforma recién abierta; si la prueba
                            # anterior dejó una pregunta a medias, la siguiente
                            # ejecuta sus pasos sobre otra pantalla. La primera
                            # no lo necesita: el navegador acaba de entrar.
                            start_over = getattr(driver, "start_over", None)
                            if i > 0 and start_over is not None:
                                try:
                                    await start_over()
                                except Exception as exc:  # noqa: BLE001
                                    # No es motivo para perder la etapa: la
                                    # prueba corre donde quedó el navegador y,
                                    # si eso la rompe, falla ella sola.
                                    logger.warning(
                                        "no se pudo volver al inicio antes de %s: %s",
                                        lead.probe_id, exc,
                                    )
                            await self.engine.run_probe(
                                lead, driver, ledger,
                                platform=record.platform,
                                run_index=0,
                                text_dir=self.store.page_text_dir(record.job_id),
                                also_credit=group[1:] or None,
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
                            # A TODAS las del grupo: comparten los pasos, así
                            # que a todas les faltó lo mismo. Anotar sólo la
                            # primera dejaba a las demás con evidencia a medias
                            # y sin motivo.
                            for cut in group:
                                record.unevaluated[cut.probe_id] = str(exc)
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
                    record.spend_usd = round(
                        record.spend_usd
                        + float(getattr(driver, "spend_usd", 0.0) or 0.0),
                        4,
                    )
                    record.cache_read_tokens += int(
                        getattr(driver, "cache_read_tokens", 0) or 0
                    )
                    record.cache_write_tokens += int(
                        getattr(driver, "cache_write_tokens", 0) or 0
                    )
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
                        rows_since(
                            ledger.for_platform_test(record.platform, test.id),
                            record.approved_at,
                        ),
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
                rows = rows_since(
                    ledger.for_platform_test(record.platform, test.id),
                    record.approved_at,
                )
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

        # The last uncounted leg: the scorer's own API spend. Framework + probes
        # went onto the record at build_plan, the browser run at execute; this
        # completes the bill.
        self._fold_client_spend(record, client)

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
            if p.get("requiresSignoff") and p["probeId"] not in record.signoffs
        ]

        def unscored(cause: str, text: str) -> Any:
            return scoring_mod.TestVerdict(
                test_id=test.id, platform=record.platform, status=NV,
                points_earned=0.0, points_possible=0.0,
                rationale=f"[causa: {cause}] {text}", evidence=[], runs=[],
            )

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

        results: list[tuple[str, str, str, list]] = []
        for p in cells:
            cell = (by_id.get(p.probe_id) or {}).get("celda") or {}
            label = " · ".join(
                x for x in (str(cell.get("tema") or ""), str(cell.get("variante") or "")) if x
            ) or p.probe_id
            if p.probe_id in record.unevaluated:
                results.append((label, "CORTADA", record.unevaluated[p.probe_id], []))
                continue
            own = [r for r in rows if r.get("probe_id") == p.probe_id]
            v = await asyncio.to_thread(
                self.engine.score_test, test, own, client,
                platform=record.platform, repeats=p.repeat_judging,
                model=self.engine.model,
            )
            results.append((label, v.status, v.rationale, list(v.evidence)))

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
        if not judged:
            cause = "ejecución fallida" if cut and not thin else "evidencia insuficiente"
            return unscored(cause, f"ninguna prueba se pudo juzgar.{tail} " + " ".join(lines))

        worth = {scoring_mod.CUMPLE: 1.0, scoring_mod.PARCIAL: 0.5, scoring_mod.NO_CUMPLE: 0.0}
        met = sum(1 for r in judged if r[1] == scoring_mod.CUMPLE)
        statuses = {r[1] for r in judged}
        status = statuses.pop() if len(statuses) == 1 else scoring_mod.PARCIAL
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
                f"CUMPLE en {met} de {len(judged)} prueba(s) evaluada(s).{tail} "
                + " ".join(lines)
            ),
            evidence=evidence, runs=[r[1] for r in judged],
        )


#: Cómo se escribe en el criterio lo que decidió el plan (ver
#: `JobRunner.draft_from_plan`).
_OBSERVABILITY_OF = {
    planner_mod.SE_PRUEBA: "OBSERVABLE_EN_PLATAFORMA",
    planner_mod.NECESITA_DOCUMENTO: "REQUIERE_DOCUMENTACION",
    planner_mod.NECESITA_ESTUDIO: "REQUIERE_ESTUDIO",
    planner_mod.NO_SE_PUEDE_MEDIR: "NO_DECIDIBLE_EN_EVALUACION",
    planner_mod.FALTA_RECORRER: "NO_DECIDIBLE_EN_EVALUACION",
}

#: La causa de un NO_EVALUADO, en palabras. Las tres primeras son resultados
#: legítimos de una evaluación; el resto son fallas del evaluador.
_CAUSE_OF = {
    planner_mod.NECESITA_DOCUMENTO: "necesita documento",
    planner_mod.NECESITA_ESTUDIO: "necesita estudio",
    planner_mod.NO_SE_PUEDE_MEDIR: "no se puede medir",
    planner_mod.FALTA_RECORRER: "falta recorrer",
}
_BUG_CAUSES = frozenset({
    "falta recorrer", "ejecución fallida", "sin redactar", "sin ejecutar",
    "evidencia insuficiente",
})


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
