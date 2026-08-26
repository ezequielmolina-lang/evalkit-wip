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
    from ..platform_loader import resolve_platform_dir

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

            return AgentDriver(ClaudeBrowserAgent(self._driver), self._driver)

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

    bank: Any = None
    model: str | None = None
    max_rounds: int = 2
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

                surfaces = await discover_surfaces(
                    driver, root_url=record.platform_url
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

        # --- framework -----------------------------------------------------
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
            return record

        built = serialize.framework_to_record(result)
        record.criteria = built["criteria"]
        record.coverage = built["coverage"]
        self._progress(
            record, Stage.FRAMEWORK,
            done=1, total=1, message=result.coverage.summary_line(),
        )

        # --- probes: OBSERVABLE CRITERIA ONLY -------------------------------
        # `result.criteria()` already excludes everything a walkthrough cannot
        # settle. A probe for a REQUIERE_ESTUDIO criterion is an evaluator sent
        # to look for something that is not on any screen.
        criteria = result.criteria()
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
                # Hand-authored, live-validated probes replace the DRAFT for the
                # criteria that have them; the council still reviews every one.
                # Where there is no banked probe, drafting proceeds as before.
                probe_bank=probe_bank,
                surfaces=surfaces,
            )
        except BaseException as exc:  # noqa: BLE001
            self._fail(record, Stage.PROBES, "", exc)
            return record

        record.probes = [serialize.probe_to_dict(g) for g in probeset]
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
                            record.unevaluated[lead.probe_id] = str(exc)
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

        self._progress(
            record, Stage.SCORE,
            done=0, total=max(1, len(tests)),
            message=f"Puntuando {len(tests)} criterios contra la evidencia guardada",
        )

        client = self._client()
        verdicts = []
        try:
            for i, test in enumerate(tests):
                self._progress(
                    record, Stage.SCORE,
                    done=i, total=max(1, len(tests)), current=test.id,
                    message=f"Criterio {i + 1} de {len(tests)}: {test.name}",
                )
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
                "note": (
                    "El juicio automático no es determinista: los parámetros de "
                    "muestreo del modelo fueron eliminados en este modelo y la API "
                    "los rechaza. Por eso se reporta cuántas corridas hubo y si "
                    "fueron unánimes, en vez de afirmar una reproducibilidad que "
                    "no tenemos."
                ),
            },
        )
        self._progress(
            record, Stage.DONE,
            done=len(tests), total=max(1, len(tests)),
            message="Informe listo",
        )
        return record


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
