"""The HTTP surface the Next.js app drives.

    POST /jobs                          start one -> {job_id}
    GET  /jobs/{id}                     stage, structured progress, artefacts
    POST /jobs/{id}/approve             the reviewed plan -> proceeds to run
    POST /jobs/{id}/signoff             ONE gated probe, explicitly
    GET  /jobs/{id}/report              the diagnostic
    GET  /jobs/{id}/ledger              the chronological trail, for the appendix
    GET  /jobs/{id}/evidence/{artifact} a captured screenshot / page text
    POST /extract                       an uploaded PDF/Word/Excel -> its text
    GET  /settings                      is there an API key, and from where
    POST /settings/api-key              the user's own Anthropic key -> stored
    DELETE /settings/api-key            forget it
    GET  /healthz                       the ONLY endpoint without auth

Every endpoint but `/healthz` requires `Authorization: Bearer <token>`, matched
in constant time against `EVALKIT_SERVICE_TOKEN`. The service refuses to build
without that variable: a missing secret is a misconfiguration, and shipping an
open service that looks healthy is the worst reading of one. See `auth.py`.

Two further properties are enforced HERE rather than trusted to callers:

* the response serialiser is a whitelist. `_public_state` can only emit the
  keys it names, so there is no field a credential could ride out on -- not
  even a future one somebody adds to `JobRecord`;
* `/evidence` serves only artifacts THIS job's ledger records, matched by
  basename against the ledger rather than resolved as a path. "Any file that
  exists under the job directory" would have served `credentials.enc`.

`/extract` is held to both: it answers with three named keys and nothing else,
it never opens a path (the bytes arrive in the body; the filename is used for
its extension and for the error message), and it writes nothing anywhere.

`/settings` is held to the first, and it is the reason the rule is worth
stating twice: the user's own Anthropic API key is a credential that arrives
through this API, and it must never come back out of it. `_settings_state`
emits three named keys -- `hasApiKey`, `keyHint`, `source` -- and the hint is a
stub (8 leading + 4 trailing characters) that identifies WHICH key is
configured without being usable as one. The key itself lives encrypted in
`settings.json` under the same box as the platform credentials.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from ..evalkit.errors import EvalkitError
from ..evalkit.extract import extract_text
from ..textio import first_lone_surrogate, scrub_surrogates
from . import auth as auth_mod
from . import serialize
from .crypto import SecretBox, ServiceError
from .jobs import Engine, JobRunner, Stage, platform_label
# The decision rows (approval, signoff) and the machine approval live in their
# own module because none of them touches the network: the batch CLI needs
# `auto_approve` and must not have to import a web server to reach it. They are
# imported BY NAME here so the endpoints below read as they always did, and so
# `app._slug` stays importable (a test pins it against the runner's rule).
from .ledger_decisions import (  # noqa: F401 -- _slug/_ledger_decision re-exported
    _ledger_decision,
    _ledger_the_approval,
    _ledger_the_signoff,
    _slug,
    auto_approve,
)
from .models import ApiKeyIn, ApproveIn, SignoffIn, StartJobIn
from .settings import SettingsStore
from .store import JobRecord, JobStore, now
from .watchdog import Clock, Supervisor, reconcile

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("runs") / "jobs"

#: Ceiling on one upload. A national pliego runs to a few MB; 25 is generous for
#: the real case and small enough that the parse cannot become the outage. The
#: extractor caps the TEXT separately (`extract.MAX_CHARS`) -- a 3 MB PDF can
#: still hold a million characters, so the two limits guard different things.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Rendered into the refusal, so the message says a size a human recognises
#: rather than a byte count they have to divide.
MAX_UPLOAD_LABEL = "25 MB"


# --- response building ------------------------------------------------------


#: Marker so a second app in the same process does not stack handlers.
_LOG_HANDLER_TAG = "evalkit-service"

#: How much of an API key `/settings` may show: enough to recognise WHICH key
#: is configured, never enough to use it. `sk-ant-a…kJ8s`.
KEY_HINT_HEAD = 8
KEY_HINT_TAIL = 4


def _key_hint(key: str) -> str | None:
    """A recognisable stub of a key, or None. NEVER the key.

    The head is the prefix everyone's key shares, so the tail is what actually
    distinguishes "the key I pasted" from "some other key" -- which is the only
    question the settings screen has to answer. A short value gets no tail at
    all rather than a hint that is most of it.
    """
    key = (key or "").strip()
    if not key:
        return None
    if len(key) <= KEY_HINT_HEAD + KEY_HINT_TAIL:
        return f"{key[:4]}…"
    return f"{key[:KEY_HINT_HEAD]}…{key[-KEY_HINT_TAIL:]}"


def _configure_package_logging() -> None:
    """Make the package audible. It was not.

    `lxp_eval` carries two dozen `logger.info` lines -- which probe is running,
    which saved session was reused, what a step cost -- and under uvicorn not
    one of them ever reached the output: root sits at WARNING, nothing is
    attached to the package logger, so every record propagated up and was
    dropped. An engine.log after a full live run held five uvicorn lines and
    nothing from this package at all.

    Attaches ONE handler to the package logger and stops propagation, so the
    level here is authoritative and records are not also re-filtered by root.
    Level comes from LXP_LOG_LEVEL (default INFO). An existing configuration is
    left alone: if someone already attached handlers, that is their choice.
    """
    pkg = logging.getLogger("lxp_eval")
    if any(getattr(h, "_tag", None) == _LOG_HANDLER_TAG for h in pkg.handlers):
        return
    if pkg.handlers:      # somebody configured this on purpose; do not fight them
        return
    level = os.getenv("LXP_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    handler._tag = _LOG_HANDLER_TAG  # type: ignore[attr-defined]
    pkg.addHandler(handler)
    pkg.setLevel(getattr(logging, level, logging.INFO))
    pkg.propagate = False


def _plan_of(record: JobRecord) -> dict | None:
    """The plan as the review screen reads it, with live signoff state.

    `requiresSignoff` is re-read from the stored probe every time. The browser
    receives the server's opinion, not its own from last request.
    """
    if not record.criteria:
        return None
    # What the human actually approved, if they have approved once already.
    edits = {
        e.get("probeId"): e
        for e in ((record.approved_plan or {}).get("probes") or [])
        if isinstance(e, dict) and e.get("probeId")
    }
    probes = []
    for p in record.probes:
        signed_at = record.signoffs.get(p["probeId"], "")
        # `approved` is the council's verdict on a TEXT. If a reviewer rewrote
        # the steps, five judges are still credited for wording they never
        # read. The gate is untouched -- requiresSignoff always comes from the
        # server copy -- but the claim of review has to follow the text.
        intact = serialize.council_still_applies(p, edits.get(p["probeId"]))
        probes.append({
            **p,
            "signedOff": bool(signed_at),
            "signedOffAt": signed_at,
            "approved": bool(p.get("approved")) and intact,
            "humanEdited": not intact,
        })
    return {
        "criteria": serialize.criteria_out(record.criteria),
        "probes": probes,
        "sampling": record.sampling or dict(serialize.DEFAULT_SAMPLING),
        "coverage": record.coverage,
    }


def _recon_of(record: JobRecord, store: JobStore) -> dict | None:
    """What the walk found — the surfaces map plus its screenshots.

    The walk photographed the platform from its very first live run and the
    product kept the proof to itself: the map and the shots sat on disk while
    the screens showed nothing. This is what lets the plan screen say "esto
    encontré, POR ESO propongo esta rúbrica" with the pictures to back it.

    `shots` carries basenames only; the bytes travel through /evidence, whose
    allow-list admits this job's artifact PNGs (see `get_evidence`).
    """
    import yaml

    out: dict = {"surfaces": None, "shots": []}
    sfile = store.evidence_dir(record.job_id) / "surfaces_discovered.yaml"
    try:
        if sfile.exists():
            out["surfaces"] = yaml.safe_load(sfile.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- un mapa ilegible no tumba el estado
        logger.warning("mapa de superficies ilegible para %s", record.job_id, exc_info=True)
    try:
        adir = store.artifacts_dir(record.job_id)
        if adir.exists():
            out["shots"] = sorted(p.name for p in adir.glob("*.png"))
    except OSError:
        pass
    if out["surfaces"] is None and not out["shots"]:
        return None
    return out


def _public_state(record: JobRecord, store: JobStore) -> dict:
    """EVERY key this endpoint can emit. A credential is not among them.

    Built by naming fields, never by dumping the record: `asdict(record)` would
    export whatever the next field added to `JobRecord` happens to be, which is
    exactly how a secret ends up in a browser.
    """
    return {
        "jobId": record.job_id,
        "recon": _recon_of(record, store),
        "job_id": record.job_id,          # both spellings; the web client takes either
        "stage": record.stage,
        "progress": record.progress or {
            "stage": record.stage, "current": "", "done": 0, "total": 1, "message": "",
        },
        "plan": _plan_of(record),
        "report": record.report,
        "error": record.error,
        "unevaluated": dict(record.unevaluated),
        "vendorNotice": record.vendor_notice,
        "spendUsd": record.spend_usd,
        "cacheReadTokens": record.cache_read_tokens,
        "cacheWriteTokens": record.cache_write_tokens,
        "platform": record.platform,
        "objective": record.objective,
        "areas": list(record.areas),
        "approvalPolicy": record.approval_policy,
        "hasCredentials": record.has_credentials,
        "evidencePath": str(store.ledger_path(record.job_id)),
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "approvedAt": record.approved_at,
    }


# --- the app ----------------------------------------------------------------


def create_app(
    *,
    root: Path | str | None = None,
    engine: Engine | None = None,
    box: SecretBox | None = None,
    background: bool = True,
    allow_origins: list[str] | None = None,
    token: str | None = None,
    clock: Clock | None = None,
    poll_interval: float = 15.0,
    reap_interval: float = 60.0,
) -> FastAPI:
    """Build the service.

    `background=False` runs each stage inside the request that triggers it.
    That is what most tests use, so a run is deterministic instead of a poll
    loop against a task that may or may not have been scheduled yet. In
    production it stays True: a POST must return a job id in milliseconds
    while the pipeline takes minutes. `background=True` is supervised --
    see `watchdog.Supervisor` -- because a bare `create_task` drops the
    exception and leaves the job in its stage forever.

    Raises `ServiceError` if there is no service token. That is deliberate: a
    misconfigured deploy must fail loudly at boot, not come up healthy and
    open.
    """
    _configure_package_logging()
    store = JobStore(root or DEFAULT_ROOT)
    engine = engine or Engine()
    clock = clock or Clock()

    # FAIL CLOSED. No token, no service -- not "no token, no auth".
    service_token = token if token is not None else auth_mod.load_token()
    if not service_token:
        raise ServiceError(
            f"el token de servicio ({auth_mod.TOKEN_ENV}) está vacío: el "
            "servicio no arranca sin autenticación"
        )
    if box is None:
        try:
            box = SecretBox.from_env()
        except ServiceError:
            # A service with no key can still build frameworks and probes; it
            # simply cannot accept credentials. Refusing to boot would take the
            # whole product down over an optional feature.
            logger.warning(
                "sin EVALKIT_SECRET_KEY: se aceptarán trabajos SIN credenciales"
            )
            box = None

    # The user's own Anthropic key, in the SAME state directory as the jobs --
    # one volume to mount, one path to name in a `docker run`. Needs the box:
    # a key we cannot encrypt is a key we do not accept (see POST /settings).
    settings_store = SettingsStore(store.root, box) if box is not None else None

    # THE INJECTION, and the direction is the point. `service` already imports
    # `config`; config importing back would be a cycle, and would also mean the
    # engine knowing an HTTP service exists. So the service hands config a
    # reader and config keeps ownership of the ORDER (env > stored > nothing).
    # See the module docstring of `lxp_eval/config.py`.
    from ..config import set_stored_key_provider

    set_stored_key_provider(settings_store.get_api_key if settings_store else None)

    runner = JobRunner(store, engine, box, clock=clock)
    supervisor = Supervisor(store, clock=clock, poll_interval=poll_interval)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # STARTUP: nothing stays `running` because a worker died. A job left in
        # flight by a killed container must not sit in its stage forever after
        # a restart -- and must not be silently resurrected either.
        recovered = reconcile(store, clock=clock)
        if recovered:
            logger.warning(
                "%d trabajo(s) quedaron en vuelo tras la muerte del worker "
                "anterior y se marcaron como fallidos: %s",
                len(recovered), ", ".join(recovered),
            )
        # ...and KEEP reconciling. Startup-only leaves a job orphaned by a
        # sibling worker `running` until that worker restarts, which with more
        # than one worker may be never.
        if reap_interval:
            supervisor.start_reaper(reap_interval)
        yield
        await supervisor.shutdown()

    app = FastAPI(title="evalkit", version="1", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins or ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store
    app.state.engine = engine
    app.state.runner = runner
    app.state.supervisor = supervisor
    app.state.clock = clock

    # --- auth --------------------------------------------------------------
    #
    # Middleware, not a per-route dependency: a dependency protects the routes
    # somebody remembered to decorate, and the failure mode of forgetting one
    # is an endpoint that is quietly public. This covers every path that
    # exists and every path anyone adds later.

    @app.middleware("http")
    async def _require_token(request, call_next):
        path = request.url.path
        # CORS preflight carries no Authorization header by definition; the
        # response says only which methods are allowed.
        if request.method == "OPTIONS" or path in auth_mod.OPEN_PATHS:
            return await call_next(request)
        presented = auth_mod.token_from_header(request.headers.get(auth_mod.HEADER))
        if not auth_mod.token_matches(service_token, presented):
            # No token in the log line, and no hint about how close it was.
            logger.warning("petición sin token válido a %s %s", request.method, path)
            return JSONResponse(status_code=401, content={"detail": auth_mod.DENIED})
        return await call_next(request)

    async def _drive(coro_fn, record: JobRecord):
        if background:
            # NOT a bare create_task: the supervisor keeps the reference, takes
            # the done-callback, and watches the heartbeat.
            supervisor.start(coro_fn, record)
            return record
        return await coro_fn(record)

    async def _build_then_maybe_run(record: JobRecord) -> JobRecord:
        """build_plan y, SÓLO bajo approval_policy="auto", aprobar y ejecutar.

        La única cadena que maneja POST /jobs, así que humano y auto comparten
        camino y el default humano queda intacto: arma el plan y PARA en
        awaiting_approval, el comportamiento de hoy. Bajo `auto` corre todo lo que
        no frena la corrida (crisis incluida); cada prueba que SÍ frena (`holdsRun`:
        consecuencia externa real contra un entorno real) queda detrás de su
        signoff -- que esto nunca escribe -- y vuelve NO_EVALUADO.

        Toda la cadena corre bajo la MISMA supervisión que build_plan solo: en
        segundo plano es una sola tarea supervisada, así que un cuelgue en la
        etapa de run lo atrapa el watchdog en vez de quedar colgado. Sólo se
        aprueba un plan que llegó a la compuerta: un build_plan que murió quedó en
        `error` y no se ejecuta.
        """
        await runner.build_plan(record)
        fresh = store.get(record.job_id) or record
        if fresh.approval_policy == "auto" and fresh.stage == Stage.AWAITING_APPROVAL:
            auto_approve(store, fresh)
            await runner.execute(fresh)
        return store.get(record.job_id) or fresh

    def _require(job_id: str) -> JobRecord:
        record = store.get(job_id)
        if record is None:
            raise HTTPException(404, f'el trabajo "{job_id}" no existe')
        return record

    # --- settings ----------------------------------------------------------

    def _settings_state() -> dict:
        """WHETHER there is a key, which one, and where it came from. Never it.

        Exactly three keys, by the same rule as `_public_state` and `/extract`:
        the serialiser can only emit what it names, so there is no field the
        key could ride out on.

        `source` is not decoration. The environment WINS over anything the user
        saved (an operator running headless has no UI to paste into), so a
        settings screen that did not know this would cheerfully offer to change
        a value that is being ignored, and the user would change it, and
        nothing would happen. The order below is `config.resolve_api_key`'s --
        the two are pinned together by
        `test_the_environment_wins_and_the_ui_is_told_so`.
        """
        from_env = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if from_env:
            return {"hasApiKey": True, "keyHint": _key_hint(from_env), "source": "env"}
        stored = settings_store.get_api_key() if settings_store else None
        if stored:
            return {"hasApiKey": True, "keyHint": _key_hint(stored), "source": "stored"}
        return {"hasApiKey": False, "keyHint": None, "source": None}

    def _require_settings() -> SettingsStore:
        if settings_store is None:
            raise HTTPException(
                503,
                "el servicio no tiene clave de cifrado (EVALKIT_SECRET_KEY), "
                "así que no puede guardar la clave de API; definí "
                "ANTHROPIC_API_KEY en el entorno mientras tanto",
            )
        return settings_store

    @app.get("/settings")
    async def get_settings():
        return _settings_state()

    @app.post("/settings/api-key")
    async def put_api_key(body: ApiKeyIn):
        """The key the user pasted. Validated here, stored encrypted, never echoed.

        A bad value is a 400 with the Spanish message from
        `settings.validate_api_key` (via the EvalkitError handler), so somebody
        who pasted an OpenAI key or their service token learns at paste time
        rather than when a paid stage 401s twenty minutes in.
        """
        _require_settings().set_api_key(body.apiKey)   # ServiceError -> 400
        return _settings_state()

    @app.delete("/settings/api-key")
    async def delete_api_key():
        _require_settings().clear_api_key()
        return _settings_state()

    # --- health ------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        """Open, and it stays open: a probe that needs a secret fails on rotation.

        It reports WHETHER auth is configured, never the value. `auth` is
        always true here -- the service cannot boot without a token -- and is
        reported anyway so an operator reading a health page sees the fact
        stated rather than having to know the invariant.

        `apiKey` was added because "ok": true was, for a fresh install, a lie a
        first-run UI would act on: the container is alive, and cannot evaluate
        anything, because nobody has given it a key. It stays a BOOLEAN -- this
        is the one endpoint without auth, so it says whether, never which.
        `ok` is unchanged: liveness and readiness are different questions and
        an orchestrator restarting the container over a missing key would fix
        nothing.
        """
        return {
            "ok": True,
            "jobs": len(store.list_ids()),
            "credentials": box is not None,
            "auth": bool(service_token),
            "apiKey": _settings_state()["hasApiKey"],
        }

    # --- create ------------------------------------------------------------

    @app.post("/jobs")
    async def start_job(body: StartJobIn):
        record = store.create(
            objective=body.objective,
            areas=list(body.areas),
            platform_url=body.platform.url,
            platform=platform_label(body.platform.url),
            platform_kind=body.platform.kind or "",
            documents=[d.model_dump() for d in body.documents],
            # "human" (el default) parquea en awaiting_approval, como siempre.
            # "auto" aprueba el plan y corre las pruebas SIN compuerta sin
            # esperar a nadie; las de compuerta quedan sin firmar y vuelven
            # NO_EVALUADO. La cadena entera (build -> aprobar -> run) va por
            # `_build_then_maybe_run`, supervisada igual que build_plan solo.
            approval_policy=body.approval_policy,
        )

        # Credentials: encrypted into their own file, immediately, and dropped
        # from memory with the request body. They never touch job.json.
        if body.platform.username and body.platform.password:
            if box is None:
                raise HTTPException(
                    503,
                    "el servicio no tiene clave de cifrado (EVALKIT_SECRET_KEY), "
                    "así que no puede recibir credenciales",
                )
            store.put_credentials(
                record.job_id,
                box.encrypt_credentials(body.platform.username, body.platform.password),
            )
            record.has_credentials = True
            store.save(record)

        await _drive(_build_then_maybe_run, record)
        return {"job_id": record.job_id, "jobId": record.job_id}

    # --- read --------------------------------------------------------------

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        return _public_state(_require(job_id), store)

    # --- approve -----------------------------------------------------------

    @app.post("/jobs/{job_id}/approve")
    async def approve(job_id: str, body: ApproveIn):
        record = _require(job_id)
        if record.stage != Stage.AWAITING_APPROVAL:
            raise HTTPException(
                409,
                f'no se puede aprobar el plan del trabajo "{job_id}": está en la '
                f'etapa "{record.stage}" y sólo se aprueba en '
                f'"{Stage.AWAITING_APPROVAL}"',
            )

        plan = dict(body.plan or {})

        # The plan IS the audit record, so a corrupted one is refused, not
        # repaired. Our JSON goes out as clean UTF-8; a client that decodes it
        # with the platform locale (cp1252 on Windows) turns "ESTÁ" into
        # "ESTÃ" + byte 0x81, which surrogateescape renders as an unencodable
        # lone surrogate, and then POSTs it back. Scrubbing that at the store
        # would let it through as "ESTÃ�" -- text no human approved,
        # inside the artefact a ministry is meant to be able to audit.
        bad = first_lone_surrogate(plan)
        if bad is not None:
            raise HTTPException(
                400,
                "el plan llegó con caracteres que no son UTF-8 válido, así que "
                "no se aprobó nada: es un problema de codificación en el "
                "cliente, no en el plan. El servicio responde siempre en "
                "utf-8; decodificá la respuesta como utf-8 en vez de usar la "
                "codificación por defecto del sistema (en Windows, cp1252) y "
                "volvé a enviarla.\n"
                # Scrubbed: the excerpt is the offending text, so quoting it
                # raw would make the error report itself unserialisable.
                f'(primer texto afectado: "{scrub_surrogates(bad[:80])}...")',
            )
        record.sampling = serialize.sampling_from_plan(plan)
        # The approved plan is stored as the human left it -- EXCEPT the gate.
        # `requiresSignoff` / `signedOff` are server state; a payload that
        # flipped them is recorded as received but never consulted (see
        # JobRunner.runnable_probes).
        record.approved_plan = serialize.with_frozen_readings({
            "criteria": plan.get("criteria") or serialize.criteria_out(record.criteria),
            "probes": plan.get("probes"),
            "sampling": record.sampling,
        }, record)
        record.approved_at = now()
        store.save(record)

        # The decision itself becomes a ledger row before anything runs on its
        # authority. Raises on failure, and that is the point -- see the helper.
        _ledger_the_approval(store, record)

        record = await _drive(runner.execute, record)
        return _public_state(store.get(job_id) or record, store)

    # --- signoff -----------------------------------------------------------

    @app.post("/jobs/{job_id}/signoff")
    async def signoff(job_id: str, body: SignoffIn):
        """One probe that needs a countersignature, on the record.

        Never bulk, and never implied by an approval. The signoff endpoint accepts
        any probe with `requiresSignoff` (crisis/self-harm, minors' data, live
        writes — the classes whose vendor notice a person countersigns). What
        HOLDS the run is narrower: `holdsRun` (a real external consequence against
        a real environment; crisis runs). A signoff that could be granted by
        approving a list of thirty probes is not a signoff.
        """
        record = _require(job_id)
        probe = next(
            (p for p in record.probes if p["probeId"] == body.probe_id), None
        )
        if probe is None:
            raise HTTPException(
                404,
                f'el trabajo "{job_id}" no tiene ningún probe "{body.probe_id}"',
            )
        if not probe["requiresSignoff"]:
            raise HTTPException(
                409,
                f'el probe "{body.probe_id}" no requiere firma humana: firmarlo '
                "no significaría nada y devaluaría las firmas que sí importan",
            )
        signed_at = now()

        # ROW FIRST, record second. /approve consults `record.signoffs` and
        # never the ledger, so a signoff saved before its row would -- on a
        # failed append -- leave a durable signature with no audit row behind
        # it, and the gated probe would run unrecorded. The inverted failure
        # (a row for a signoff the record lost) is the safe direction for an
        # audit log, and a re-signoff appends harmlessly.
        _ledger_the_signoff(store, record, probe, signed_at)

        record.signoffs[body.probe_id] = signed_at
        store.save(record)
        logger.info("job %s: firma humana para el probe %s", job_id, body.probe_id)
        return _public_state(record, store)

    # --- report ------------------------------------------------------------

    @app.get("/jobs/{job_id}/report")
    async def get_report(job_id: str):
        record = _require(job_id)
        if record.report is None:
            raise HTTPException(
                409,
                f'el trabajo "{job_id}" todavía no tiene informe (etapa '
                f'"{record.stage}")',
            )
        return record.report

    # --- evidence ----------------------------------------------------------

    @app.get("/jobs/{job_id}/ledger")
    async def get_ledger(job_id: str):
        """El rastro cronológico completo, para el apéndice del informe.

        Todo esto ya estaba en el ledger —paso, hash, hora, URL por artefacto—
        y no se podía leer. La evidencia por criterio que trae el informe no
        alcanza para un apéndice: sólo lista lo que sostuvo un veredicto, y
        omite los pasos que no produjeron ninguno. El apéndice existe para que
        un lector escéptico recorra el argumento ENTERO, no la parte que nos
        conviene.

        `artifact` sale como NOMBRE, nunca como ruta: la ruta absoluta cuenta
        cómo está armada la máquina y no le sirve de nada a quien lee el
        informe. El nombre es además lo que `/evidence` acepta.
        """
        _require(job_id)
        rows = []
        for row in store.ledger_rows(job_id):
            rows.append({
                "probeId": row.get("probe_id", ""),
                "testId": row.get("test_id", ""),
                "runIndex": row.get("run_index", 0),
                "kind": row.get("kind", ""),
                "artifact": Path(row.get("artifact", "")).name,
                "sha256": row.get("sha256", ""),
                "timestamp": row.get("timestamp", ""),
                "note": row.get("note", ""),
                "url": row.get("url", ""),
            })
        return {"rows": rows}

    @app.get("/jobs/{job_id}/evidence/{artifact}")
    async def get_evidence(job_id: str, artifact: str):
        record = _require(job_id)
        name = Path(artifact).name
        if not name or name != artifact or name in (".", ".."):
            raise HTTPException(400, "nombre de artefacto inválido")

        # The LEDGER is the allow-list, not the filesystem. Only bytes this job
        # actually captured and hashed can be served.
        for row in store.ledger_rows(job_id):
            path = Path(row["artifact"])
            if path.name != name:
                continue
            if not path.exists():
                raise HTTPException(
                    410,
                    f'el artefacto "{name}" figura en el ledger pero ya no está '
                    "en el disco",
                )
            return FileResponse(path)

        # Las capturas del RECONOCIMIENTO también son evidencia -- el producto
        # exploró la plataforma y tiene que poder MOSTRARLO. Nunca entran al
        # ledger (ninguna prueba corrió), así que la lista blanca gana
        # exactamente una forma más: PNGs dentro del directorio de artefactos
        # de ESTE trabajo, por basename. Nada más: credentials.enc no es un
        # .png, los page_text viven en otro directorio, y para todo lo demás
        # el ledger sigue siendo la autoridad.
        if name.endswith(".png"):
            candidate = store.artifacts_dir(job_id) / name
            if candidate.exists() and candidate.is_file():
                return FileResponse(candidate)

        raise HTTPException(
            404,
            f'el trabajo "{job_id}" no registró ningún artefacto llamado "{name}"',
        )

    # --- document extraction -----------------------------------------------

    @app.post("/extract")
    async def extract(file: UploadFile = File(...)):
        """An uploaded norm / pliego / rúbrica, as the text a criterion cites.

        The setup screen promised this from the first day and the dropzone
        faked it: clicking appended an invented filename and no file was ever
        read. The engine's framework stage has always known how to extract
        criteria from `Document(name, text)` and verify their quotes; what was
        missing was the text.

        STATELESS ON PURPOSE. No job, no directory, no bytes on disk: the text
        comes straight back and the browser sends it to POST /jobs like any
        other document. That keeps the two properties in the module docstring
        intact -- there is no new path a client can name and no new file for
        `/evidence` to have to refuse -- and it means an upload that the user
        then abandons leaves nothing behind to clean up or leak.

        The response is a whitelist of three keys, same discipline as
        `_public_state`. Notably absent: anything about where the upload was
        spooled.
        """
        # Bounded read: one byte past the limit is enough to know it is over,
        # and nothing larger is ever held. This does NOT stop a client from
        # sending a gigabyte -- Starlette has already spooled the body before
        # this runs -- it stops us from parsing one. A transport-level cap is
        # the reverse proxy's job and should be set there too.
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"el archivo supera el límite de {MAX_UPLOAD_LABEL}. Subí sólo "
                "la sección que importa, o una versión sin las imágenes.",
            )

        # Basename only, and only for its extension and the error text. Nothing
        # here is opened: the bytes came in the body.
        name = Path(str(file.filename or "")).name or "archivo"
        text = extract_text(name, data)   # ExtractError -> 400 via the handler
        return {"name": name, "text": text, "chars": len(text)}

    # --- errors ------------------------------------------------------------

    @app.exception_handler(EvalkitError)
    async def _evalkit_error(request, exc: EvalkitError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": exc.render()})

    return app
