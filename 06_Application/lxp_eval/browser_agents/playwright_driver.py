"""Low-level Playwright driver — async browser primitives.

Wraps Playwright with the small set of operations our Claude tool-use loop
needs: launch, navigate, click, type, screenshot, read accessibility tree.

Design notes:
- Headful by default (visible browser) — `headless=False` looks like a real user
  and avoids triggering common bot-detection heuristics.
- We pace ourselves with `asyncio.sleep` between actions so the trace looks
  natural and the rendered DOM has time to settle.
- The accessibility tree is preferred over raw HTML for Claude's `read_page`
  because it is far smaller and labels interactive elements with their roles.
- Clicks are resolved through Playwright's `get_by_role` / `get_by_text` /
  `get_by_label` accessor stack rather than CSS selectors — these are more
  stable across the JS-heavy SPAs we're testing.

Usage:
    driver = PlaywrightDriver(screenshots_dir=Path('./shots'))
    await driver.launch()
    await driver.navigate('https://app.plataforma-b.ejemplo/')
    text = await driver.read_page()
    await driver.click_by_text('Iniciar sesión')
    await driver.type_into_focused('evaluador@ejemplo.org')
    await driver.screenshot('login_filled')
    await driver.close()
"""
from __future__ import annotations

import asyncio
import os
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..evalkit.errors import EvalkitError
from ..evalkit.session import (
    PlatformAuth,
    SessionExpired,
    is_logged_out_page,
    is_logged_out_url,
    load_platform_auth,
)

logger = logging.getLogger(__name__)


# Default per-action pacing — small enough to be productive, large enough to
# look like a human and let the SPA settle before the next read.
DEFAULT_ACTION_DELAY_S = 2.0

# Default timeout for a single Playwright operation (ms).
DEFAULT_TIMEOUT_MS = 30_000

#: Timeout for ONE strategy inside the click chain. Much shorter than
#: DEFAULT_TIMEOUT_MS on purpose: `click_element_description` tries six
#: locators in turn, so at the full timeout a label that matches NOTHING costs
#: three minutes of wall clock before the agent is told. It is a speculative
#: probe, not a wait for a slow page -- if the element is already rendered the
#: locator resolves in milliseconds, and if it is not there no amount of
#: waiting will conjure it. Observed live: the agent hunting for a
#: "new conversation" control burned minutes per attempt at 30s a strategy.
CLICK_STRATEGY_TIMEOUT_MS = int(os.getenv("LXP_CLICK_STRATEGY_TIMEOUT_MS", "4000"))

#: Un aviso de que la plataforma todavía está respondiendo: un verbo de espera
#: seguido de puntos suspensivos ("Gallito está pensando…", "Cargando...",
#: "Typing…"). Los puntos son obligatorios para no tomar por espera un texto
#: fijo ("Generando hábitos de estudio"). Ver `PlaywrightDriver.settle`. No
#: nombra a ningún proveedor; una plataforma que avise de otro modo lo declara
#: con `LXP_BUSY_PATTERN`.
BUSY_PATTERN = re.compile(
    os.getenv("LXP_BUSY_PATTERN")
    or (
        r"\b(pensando|escribiendo|cargando|generando|preparando|procesando|"
        r"analizando|buscando|redactando|thinking|typing|loading|generating|"
        r"processing|working)\b[^\n]{0,40}?(\.{3}|…)"
    ),
    re.IGNORECASE,
)
#: Cuánto se espera, como mucho, a que ese aviso desaparezca antes de capturar.
SETTLE_TIMEOUT_S = float(os.getenv("LXP_SETTLE_TIMEOUT_S", "45"))


# --- el navegador no sale de la plataforma -----------------------------------
#
# LO QUE PASÓ EL 2026-11-25. Una prueba decía "abrir la plataforma del tutor en
# el navegador" sin dar la dirección. El agente inventó una (tutor.io), que
# redirige a un sitio de venta de dominios detrás de una verificación de humano
# de Cloudflare, y le hizo clic cinco veces a la verificación. Las pruebas
# siguientes arrancaron paradas en ese sitio. Nada de eso es evaluar la
# plataforma: es tráfico automatizado contra un tercero que no aceptó nada, con
# el riesgo de archivar su pantalla como evidencia de otro.
#
# Pedirle al agente que no lo haga no alcanza (ver `read_only_tools`: el modelo
# puede decidir ignorarlo y para cuando se nota ya ocurrió). Así que la salida
# se cierra en dos lugares: `navigate()` se niega antes de tocar el navegador, y
# una ruta del contexto aborta cualquier navegación de la página principal hacia
# un host que la plataforma no declara -- que es el camino de un clic, que
# `navigate()` nunca ve.


class OffPlatformNavigation(EvalkitError):
    """Se intentó llevar el navegador a un sitio que no es la plataforma."""


class HumanVerificationRequired(EvalkitError):
    """La página pide demostrar que hay una persona. evalkit no lo resuelve."""


def normalize_host(host: str | None) -> str:
    host = (host or "").strip().lower()
    return host[4:] if host.startswith("www.") else host


def host_allowed(url: str, allowed: "frozenset[str] | None") -> bool:
    """¿Esta dirección es de la plataforma bajo evaluación?

    Coincidencia EXACTA de host (sin `www.`), no de dominio padre. Un prototipo
    en un hosting compartido vive en `algo.vercel.app`, y aceptar el dominio
    padre sería aceptar todos los sitios de ese hosting. El host de un proveedor
    de identidad, o cualquier otro que haga falta, se declara en el config de la
    plataforma (`allowed_hosts:`), no aquí.

    Sin lista no se restringe nada: es el comportamiento de los llamadores que
    no declaran plataforma. Lo que no es un sitio (about:, data:, blob:) pasa
    siempre: no lleva a ninguna parte.
    """
    if allowed is None:
        return True
    from urllib.parse import urlparse

    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https"):
        return True
    return normalize_host(parts.hostname) in allowed


#: Títulos de las páginas intermedias que frenan a un navegador automatizado.
_CHALLENGE_TITLE = re.compile(
    r"^\s*(just a moment|un momento|attention required|security check"
    r"|verificaci[oó]n de seguridad|verifying you are human"
    r"|vercel security checkpoint)",
    re.IGNORECASE,
)
#: Frases del cuerpo de esas páginas. Sólo cuentan en una página CORTA: una
#: lección que explica qué es un captcha tiene estas palabras y no es un desafío,
#: y un falso positivo aquí frena una evaluación entera por nada.
_CHALLENGE_TEXT = re.compile(
    r"verify (that )?you are human|verifique que es un ser humano"
    r"|verifica que eres humano|checking your browser"
    r"|comprobando (su|tu) navegador|verificaci[oó]n de seguridad en curso",
    re.IGNORECASE,
)
_CHALLENGE_MAX_CHARS = 2000

#: Lo que un agente escribiría para apuntarle a la casilla de una verificación.
_CHALLENGE_CLICK = re.compile(
    r"captcha|no soy un robot|not a robot|(ser|eres|soy) humano"
    r"|(you are|i am|i'm) human|verif\w+.{0,20}human",
    re.IGNORECASE,
)


def looks_like_human_challenge(*, title: str, text: str) -> bool:
    """¿La página entera es una verificación de humano?"""
    if _CHALLENGE_TITLE.search(title or ""):
        return True
    body = text or ""
    return len(body) < _CHALLENGE_MAX_CHARS and bool(_CHALLENGE_TEXT.search(body))


def names_a_human_challenge(description: str) -> bool:
    """¿Este clic apunta a la casilla de una verificación de humano?"""
    return bool(_CHALLENGE_CLICK.search(description or ""))


@dataclass
class DriverState:
    """Bookkeeping for the current browser session."""
    screenshots_taken: list[Path] = field(default_factory=list)
    actions_log: list[dict[str, Any]] = field(default_factory=list)

    def log(self, action: str, **payload: Any) -> None:
        self.actions_log.append({"ts": datetime.utcnow().isoformat(), "action": action, **payload})


class PlaywrightDriver:
    """Async wrapper over Playwright Chromium for Claude tool-use control.

    Lazily imports `playwright.async_api` so that the rest of the package can
    be imported (e.g., by smoke tests or doc generators) without Playwright
    being installed.
    """

    def __init__(
        self,
        screenshots_dir: Path,
        *,
        headless: bool = True,
        storage_state: Path | None = None,
        action_delay_s: float = DEFAULT_ACTION_DELAY_S,
        viewport_width: int = 1440,
        viewport_height: int = 900,
        user_agent: str | None = None,
        locale: str = "es-SV",
        platform: str | None = None,
        platform_auth: PlatformAuth | None = None,
        allowed_hosts: "Any | None" = None,
    ) -> None:
        # Los hosts de la plataforma bajo evaluación. Con lista, el navegador no
        # puede salir de ellos (ver `host_allowed`); sin lista no se restringe.
        self.allowed_hosts = (
            None if allowed_hosts is None
            else frozenset(h for h in (normalize_host(x) for x in allowed_hosts) if h)
        )
        #: Las salidas que se frenaron, para que el informe las pueda citar.
        self.blocked_navigations: list[str] = []
        self.screenshots_dir = Path(screenshots_dir)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        # Headless by default: the product is an automated run, not a human
        # driving Chrome. Pass headless=False only for the interactive login.
        self.headless = headless
        self.storage_state = Path(storage_state) if storage_state else None
        self.action_delay_s = action_delay_s
        self.viewport = {"width": viewport_width, "height": viewport_height}
        self.user_agent = user_agent
        self.locale = locale

        # How THIS platform proves you are logged in. Loaded from its
        # config.yaml, never hardcoded here -- see evalkit.session.PlatformAuth.
        if platform_auth is None and platform:
            platform_auth = load_platform_auth(platform)
        self.platform_auth = platform_auth

        # Set by read_page: whether the last read was cut short, and how long
        # the page really was. The runner puts this in the ledger note so a
        # partial capture cannot pass as the whole page.
        self.last_read_truncated = False
        self.last_read_full_chars = 0

        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None  # type: ignore[assignment]
        self.state = DriverState()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def launch(self) -> None:
        from playwright.async_api import async_playwright  # lazy import

        # BEFORE starting anything. This check used to sit after the browser was
        # already up, and because the raise happens inside __aenter__, __aexit__
        # never runs -- so `async with PlaywrightDriver(...)` leaked a whole
        # Chromium process on every missing session, the most ordinary failure
        # there is.
        if self.storage_state is not None and not self.storage_state.exists():
            raise SessionExpired(
                f"no existe el storage_state {self.storage_state.name} — "
                "hay que iniciar sesión primero (ver evalkit.session)"
            )

        logger.info("Launching Chromium (headless=%s, locale=%s)", self.headless, self.locale)
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                ],
            )
            if self.storage_state is not None:
                logger.info("reusando sesión guardada: %s", self.storage_state.name)
            self._context, self.page = await self._open_context(
                str(self.storage_state) if self.storage_state is not None else None
            )
        except BaseException:
            # Every OTHER way launch can die (a bad context arg, a crashed page)
            # would leak the same way. Tear down whatever got started, then let
            # the original exception through untouched.
            await self.close()
            raise
        self.state.log("launch", headless=self.headless)

    async def _open_context(self, storage_state: Any | None) -> tuple[Any, Any]:
        """Un contexto nuevo con su página, con el mismo cerco que el primero.

        Un solo lugar para crearlo, porque `start_over` abre otro a mitad de la
        corrida y un contexto sin el cerco de hosts sería justo el agujero que
        el cerco vino a cerrar.
        """
        context_kwargs: dict[str, Any] = dict(viewport=self.viewport, locale=self.locale)
        if self.user_agent:
            context_kwargs["user_agent"] = self.user_agent
        if storage_state is not None:
            context_kwargs["storage_state"] = storage_state
        context = await self._browser.new_context(**context_kwargs)
        try:
            if self.allowed_hosts is not None:
                # Sólo se interceptan las direcciones AJENAS (el predicado), así
                # que el tráfico de la propia plataforma no pasa por Python.
                await context.route(
                    lambda url: not host_allowed(url, self.allowed_hosts),
                    self._stop_leaving_the_platform,
                )
            page = await context.new_page()
            page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        except BaseException:
            await context.close()
            raise
        return context, page

    async def remember_start(self) -> None:
        """Guarda, en memoria, el estado con el que la corrida ENTRÓ.

        Se llama una vez, ya adentro (después del login si lo hubo). Es lo que
        `start_over` restaura antes de cada prueba. En memoria y no en disco: es
        una sesión viva de una cuenta ajena, y el archivo de sesión guardado
        tiene su propio dueño (evalkit.session).
        """
        assert self._context is not None
        self._start_state = await self._context.storage_state()

    async def start_over(self, url: str) -> bool:
        """Deja el navegador como al entrar: contexto nuevo, en `url`.

        POR QUÉ EXISTE. En el piloto del 2026-11-25 todas las pruebas de una
        corrida compartían un contexto, así que cada una arrancaba donde la
        anterior había dejado la plataforma. La segunda prueba pedía "tocar una
        opción incorrecta" y encontró la opción ya marcada y los botones
        deshabilitados; las siguientes tocaban "Temas" y la plataforma respondía
        "antes de seguir, terminemos la pregunta que quedó pendiente". Trece de
        catorce pruebas tuvieron un paso que no se pudo ejecutar, con un plan
        que estaba bien escrito para una plataforma recién abierta.

        Devuelve False si no se pudo y se siguió en el contexto anterior: pasa
        cuando la sesión vive en algo que el estado guardado no lleva
        (sessionStorage), y entonces un contexto nuevo es una pantalla de login.
        En ese caso sólo se vuelve a `url`, que es lo que había antes.

        LO QUE NO HACE: el progreso que la plataforma guarda en su SERVIDOR
        sigue ahí. Para eso hace falta una cuenta por prueba, no un navegador.
        """
        assert self._browser is not None and self.page is not None
        state = getattr(self, "_start_state", None)
        if state is None and self.storage_state is not None:
            state = str(self.storage_state)
        old_context, old_page = self._context, self.page
        try:
            self._context, self.page = await self._open_context(state)
            await self.navigate(url)
            lost = await self.is_logged_out()
        except Exception as exc:  # noqa: BLE001 -- se vuelve al contexto anterior
            logger.warning("no se pudo abrir un contexto nuevo: %s", exc)
            lost = True
        if lost:
            fresh = self._context
            self._context, self.page = old_context, old_page
            if fresh is not None and fresh is not old_context:
                try:
                    await fresh.close()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Error closing context: %s", exc)
            logger.warning(
                "el contexto nuevo no quedó con la sesión abierta; la prueba "
                "arranca en el contexto anterior, vuelta a %s", url,
            )
            await self.navigate(url)
            self.state.log("start_over", fresh_context=False)
            return False
        try:
            await old_context.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing context: %s", exc)
        self.state.log("start_over", fresh_context=True)
        return True

    async def new_session(self, url: str) -> bool:
        """El mismo estudiante vuelve otro día: página nueva en el MISMO contexto.

        Lo contrario de `start_over`. Allá se tira lo que la plataforma guardó,
        para que una prueba no herede a la anterior; acá se conserva, porque la
        pregunta del recorrido es qué recuerda la plataforma cuando alguien
        vuelve. Cerrar la página y abrir otra es lo más parecido a "cerré la
        pestaña y volví mañana" que se puede hacer sin esperar a mañana: se va
        el estado en memoria de la página y queda lo que la plataforma persistió
        (cookies, almacenamiento local, y lo que tenga en su servidor).
        """
        assert self._context is not None and self.page is not None
        old = self.page
        self.page = await self._context.new_page()
        self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            await old.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error closing page: %s", exc)
        await self.navigate(url)
        self.state.log("new_session")
        return True

    async def close(self) -> None:
        if self._context:
            try:
                await self._context.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing context: %s", e)
        if self._browser:
            try:
                await self._browser.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error closing browser: %s", e)
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("Error stopping playwright: %s", e)
        self.state.log("close")

    async def __aenter__(self) -> "PlaywrightDriver":
        await self.launch()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> str:
        """Navigate to a URL. Returns the URL the browser ended up at."""
        assert self.page is not None
        allowed = getattr(self, "allowed_hosts", None)
        if not host_allowed(url, allowed):
            raise OffPlatformNavigation(
                f'no se navega a "{url}": no es una dirección de la plataforma '
                f"bajo evaluación ({', '.join(sorted(allowed or ())) or 'sin hosts'})",
                hint=(
                    "(si la plataforma usa otro host, por ejemplo un proveedor de "
                    "identidad, declaralo en allowed_hosts: de su config.yaml)"
                ),
            )
        logger.info("navigate -> %s", url)
        await self.page.goto(url, wait_until=wait_until)
        await asyncio.sleep(self.action_delay_s)
        final_url = self.page.url
        self.state.log("navigate", url=url, final_url=final_url)
        return final_url

    async def _stop_leaving_the_platform(self, route: Any) -> None:
        """Aborta una navegación de la página principal hacia un host ajeno.

        Sólo la navegación de la página: las fuentes, los scripts y las llamadas
        a servicios de terceros que la plataforma usa para funcionar siguen de
        largo, porque cortarlas rompería la plataforma que se quiere evaluar.
        Un iframe ajeno tampoco se corta: no saca al navegador de la plataforma.
        """
        request = route.request
        try:
            leaving = (
                request.is_navigation_request()
                and request.frame.parent_frame is None
            )
        except Exception:  # noqa: BLE001 -- un frame ya cerrado no es una salida
            leaving = False
        if not leaving:
            await route.continue_()
            return
        self.blocked_navigations.append(request.url)
        self.state.log("blocked_navigation", url=request.url)
        logger.warning(
            "navegación frenada: %s no es un host de la plataforma", request.url
        )
        # 204 y no `abort`: un abort deja al navegador en una página de error de
        # Chromium (lo mostró la prueba con navegador real), y la prueba
        # siguiente arrancaría desde ahí. Con "sin contenido" la navegación se
        # cancela y la página de la plataforma queda donde estaba. El sitio
        # ajeno nunca recibe el pedido en ninguno de los dos casos.
        await route.fulfill(status=204, body="")

    async def is_human_challenge(self) -> bool:
        """Si la página actual es una verificación de humano. Nunca levanta."""
        if getattr(self, "page", None) is None:
            return False
        try:
            title = await self.page.title()
            text = await self.page.inner_text("body", timeout=2000)
        except Exception:  # noqa: BLE001 -- no poder leer no es una verificación
            return False
        return looks_like_human_challenge(title=title, text=text)

    async def screenshot(self, label: str = "snap", *, full_page: bool = True) -> Path:
        """Save a screenshot and return its path."""
        assert self.page is not None
        safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:60] or "snap"
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        out = self.screenshots_dir / f"{ts}_{safe_label}.png"
        await self.page.screenshot(path=str(out), full_page=full_page)
        self.state.screenshots_taken.append(out)
        self.state.log("screenshot", path=str(out), label=label)
        logger.info("screenshot -> %s", out.name)
        return out

    async def _page_text(self) -> str:
        """The page as an accessibility tree, or the best thing still available.

        `page.accessibility` was REMOVED from Playwright (gone by 1.61, which is
        what we run), so the call raised on every read and the driver quietly
        fell back to `inner_text("body")`. Nothing broke loudly and the whole
        agent got worse: instead of a structured role+name tree it received raw
        page text -- on Plataforma A's dashboard that is 20414 characters of noise
        against a 3000-character budget, so 85% of every page was thrown away
        before the agent could reason about it. Navigation steps that "could not
        find the course" were often just steps whose page never arrived.

        `aria_snapshot` is the supported replacement and returns YAML directly.
        The legacy path stays for older Playwright, and inner_text remains the
        last resort so a driver on an unknown version still runs.
        """
        page = self.page
        snap = getattr(page, "aria_snapshot", None)
        if snap is not None:
            try:
                text = await snap()
                if text and text.strip():
                    return text
            except Exception as e:  # noqa: BLE001
                logger.warning("aria_snapshot falló (%s), sigo con el respaldo", e)

        legacy = getattr(page, "accessibility", None)
        if legacy is not None:
            try:
                tree = await legacy.snapshot(interesting_only=True)
                if tree:
                    return _flatten_a11y(tree)
            except Exception as e:  # noqa: BLE001
                logger.warning("a11y snapshot falló (%s), sigo con el respaldo", e)

        return await page.inner_text("body")

    async def read_page(self, max_chars: int | None = 8000) -> str:
        """Return a text summary of the page (accessibility tree, snipped).

        `max_chars=None` returns the WHOLE page, untruncated. That is what an
        evidence capture must use, and the distinction is not cosmetic: this
        one number used to serve two unrelated masters -- the agent's token
        budget and the evidence file -- and conflating them cost a probe.
        PB-PT-D8-01 wrote 27 captures, each cut at 8000 of ~25000 characters,
        and because truncation keeps the HEAD what survived was the course
        sidebar while the tutor conversation -- an overlay rendered late in the
        DOM -- sat entirely inside the discarded 17000. The judge scored the
        criterion NO_EVALUADO for citing no valid artifact, and it was right:
        the artifacts did not contain the thing being evaluated.

        Capping the agent's own reads is still correct; capping the record is
        not. The agent can go back and look again. Evidence is written once.

        The accessibility tree includes only meaningful elements with their
        role + name, which is roughly what a screen-reader user perceives —
        ideal for Claude to reason over without choking on raw HTML.

        A cut-short read SAYS SO, both in the returned text and on
        `last_read_truncated` / `last_read_full_chars`, which the runner copies
        into the ledger note. Otherwise evidence can be silently incomplete
        while its hash certifies the truncated bytes as "what was seen" -- and
        the thing that got cut is exactly what tends to sit at the bottom of a
        long page.
        """
        assert self.page is not None
        text = await self._page_text()

        text = text.strip()
        full_chars = len(text)
        # Reset every read: a stale flag would put a false truncation note on
        # the NEXT piece of evidence.
        self.last_read_full_chars = full_chars
        self.last_read_truncated = max_chars is not None and full_chars > max_chars

        if self.last_read_truncated:
            dropped = full_chars - max_chars
            text = (
                text[:max_chars]
                + f"\n…[TEXTO TRUNCADO: se omitieron {dropped} de {full_chars} caracteres]"
            )
            logger.warning(
                "read_page truncó la página: %d de %d caracteres guardados",
                max_chars, full_chars,
            )
        self.state.log(
            "read_page",
            chars=len(text),
            full_chars=full_chars,
            truncated=self.last_read_truncated,
        )
        return text

    async def get_url(self) -> str:
        assert self.page is not None
        return self.page.url

    async def url(self) -> str:
        """Alias used by the probe runner's narrow driver interface."""
        return await self.get_url()

    @property
    def context(self) -> Any:
        """The live browser context. `auto_login` reads its cookies to prove
        that a login really succeeded."""
        return self._context

    async def is_logged_out(self) -> bool:
        """Whether the current PAGE is a login wall, per the platform config.

        The URL alone cannot answer this. PLATAFORMA_B serves its login form at the site
        ROOT, so no URL marker matches it and the old check never fired for that
        platform at all; and any SPA can render an auth modal over /dashboard/12
        without navigating, so the URL stays pristine while the session is gone.
        With no platform config we degrade to the URL heuristic rather than
        claiming the page is fine.
        """
        if self.page is None:
            return False
        if self.platform_auth is None:
            return is_logged_out_url(await self.get_url())
        # `require_positive=False`: this runs DURING a probe, on whatever page
        # the run has reached. The authenticated markers come from one page (the
        # dashboard), so demanding them here declares a lesson -- an immersive
        # view with no side-nav -- a login wall. A real logout redirects to
        # /login/, which the URL pass still catches. See is_logged_out_page.
        return await is_logged_out_page(
            self.page, self.platform_auth, require_positive=False
        )

    async def assert_session_alive(self) -> None:
        """Raise SessionExpired if we are looking at a login wall.

        The runner calls this before capturing anything AND AFTER EVERY STEP: a
        logged-out page must never become evidence, or a verdict gets rendered
        on a login screen and attributed to the vendor.

        Lo mismo vale para una verificación de humano: evalkit no la resuelve,
        y una captura de esa pantalla no es evidencia de nada sobre la
        plataforma. Se detiene con su propio error, para que el motivo que queda
        en el informe diga qué pasó y no "la sesión expiró".
        """
        if await self.is_human_challenge():
            current = await self.get_url()
            raise HumanVerificationRequired(
                f"la página en {current} pide verificar que hay una persona "
                "(captcha o control de seguridad): evalkit no la resuelve, así "
                "que la prueba se detiene acá",
                hint=(
                    "(si es la propia plataforma, pedile al proveedor una cuenta "
                    "o un entorno de prueba sin esa verificación)"
                ),
            )
        if await self.is_logged_out():
            current = await self.get_url()
            raise SessionExpired(
                f"la sesión expiró: la página en {current} es una pantalla de "
                "login — hay que volver a iniciar sesión (ver evalkit.session)"
            )

    async def save_storage_state(self, path: Path) -> Path:
        """Persist the live context's cookies/localStorage for later headless runs."""
        assert self._context is not None
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(path))
        logger.info("storage_state guardado: %s", path.name)
        return path

    async def do(self, step: str) -> None:
        """Execute ONE scripted step.

        A deliberately small, deterministic verb set -- no LLM in the loop, so a
        run is reproducible and headless. An unrecognized step RAISES rather
        than being skipped: silently not performing a step would leave the page
        in the wrong state and turn the next screenshot into false evidence.

            goto <url>            click <visible text>
            fill <selector> = <value>       press <key>
            wait <seconds>        expect_text <substring>
        """
        assert self.page is not None
        raw = (step or "").strip()
        verb, _, rest = raw.partition(" ")
        verb = verb.lower()
        rest = rest.strip()

        if verb == "goto":
            await self.navigate(rest)
        elif verb == "click":
            if not await self.click_by_text(rest):
                raise EvalkitError(f"no encontré nada clickeable con el texto: {rest!r}")
        elif verb == "fill":
            selector, _, value = rest.partition("=")
            if not selector.strip() or not value.strip():
                raise EvalkitError(f"paso 'fill' mal formado: {step!r} (usá: fill <selector> = <valor>)")
            if not await self.fill_by_selector(selector.strip(), value.strip()):
                raise EvalkitError(f"no pude completar el campo: {selector.strip()!r}")
        elif verb == "press":
            await self.press_key(rest)
        elif verb == "wait":
            try:
                await self.wait(float(rest))
            except ValueError:
                raise EvalkitError(f"paso 'wait' mal formado: {step!r} (usá: wait <segundos>)") from None
        elif verb == "expect_text":
            text = await self.read_page()
            if rest not in text:
                raise EvalkitError(f"no encontré el texto esperado en la página: {rest!r}")
        else:
            raise EvalkitError(
                f"no sé ejecutar el paso: {step!r}",
                hint="(verbos: goto, click, fill, press, wait, expect_text)",
            )
        self.state.log("do", step=step)

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
        self.state.log("wait", seconds=seconds)

    async def settle(self, *, timeout_s: float | None = None) -> str:
        """Espera a que la plataforma termine de responder. Devuelve "" si terminó.

        POR QUÉ. En la corrida completa del 2026-11-26 un criterio de seguridad
        del tutor (PT-D9-H1) quedó sin veredicto por "evidencia insuficiente":
        la captura decía "Gallito está pensando…" y nada más. Quien ejecuta
        envió el mensaje, cerró el paso, y el runner capturó en ese instante: la
        respuesta del tutor, que era lo único que el criterio preguntaba, llegó
        después de la foto. Que el agente espere es una regla de su prompt; que
        la captura no se tome con la plataforma a medio responder lo garantiza
        esto, que corre en el runner justo antes de guardar.

        Barato a propósito: una lectura de la página. Sólo si esa lectura
        muestra un aviso de espera (`BUSY_PATTERN`) se sigue mirando, una vez
        por segundo, hasta que desaparezca o se cumpla el plazo.

        Si el plazo se cumple devuelve el aviso que seguía en pantalla, para que
        la nota de la captura lo diga: una captura tomada durante la espera no
        se presenta como la respuesta.
        """
        deadline = float(SETTLE_TIMEOUT_S if timeout_s is None else timeout_s)
        waited = 0.0
        still = ""
        while True:
            try:
                text = await self._page_text()
            except Exception as exc:  # noqa: BLE001 -- no poder mirar no frena la captura
                logger.debug("settle: no pude leer la página (%s)", exc)
                return ""
            found = BUSY_PATTERN.search(text or "")
            if not found:
                if waited:
                    self.state.log("settle", waited_s=waited)
                return ""
            still = found.group(0).strip()
            if waited >= deadline:
                self.state.log("settle_timeout", waited_s=waited, busy=still)
                logger.warning("la plataforma seguía respondiendo tras %.0f s: %r", waited, still)
                return still
            await asyncio.sleep(1.0)
            waited += 1.0

    # ------------------------------------------------------------------
    # Click strategies — multi-strategy fallback for fragile SPAs
    # ------------------------------------------------------------------

    async def click_by_text(self, text: str, *, exact: bool = False, nth: int = 0) -> bool:
        """Click the element CALLED `text`, whatever makes it called that.

        Visible text first, then the accessible name, then the label. The
        agent's intent is "click the thing named X"; which attribute the page
        happens to use to name it is not something the agent can know, and
        making it guess wastes a turn per attempt.

        This got worse the moment `read_page` started returning a real
        accessibility tree: the agent began seeing names that come from
        `aria-label` -- `button "Navigate to step 1"` -- and asking for them by
        name. Against the live platform that button gives `get_by_text` 0 hits
        and `get_by_role(name=...)` exactly 1, so the agent could see it and
        never touch it. Reading the page better surfaced elements the click
        tool could not reach.

        Still fails closed: if nothing matches, this returns False rather than
        clicking something near enough.
        """
        assert self.page is not None
        intentos = (
            ("text", lambda: self.page.get_by_text(text, exact=exact)),
            ("role:button", lambda: self.page.get_by_role("button", name=text)),
            ("label", lambda: self.page.get_by_label(text)),
            ("role:link", lambda: self.page.get_by_role("link", name=text)),
            ("role:tab", lambda: self.page.get_by_role("tab", name=text)),
        )
        ultimo: Exception | None = None
        for como, construir in intentos:
            try:
                await construir().nth(nth).click(timeout=CLICK_STRATEGY_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001 -- se prueba la estrategia siguiente
                ultimo = e
                continue
            await asyncio.sleep(self.action_delay_s)
            self.state.log("click_by_text", text=text, via=como)
            return True

        logger.warning("click_by_text(%r) failed: %s", text, ultimo)
        self.state.log("click_by_text_failed", text=text, error=str(ultimo))
        return False

    async def click_by_role(self, role: str, name: str | None = None, nth: int = 0) -> bool:
        """Click via accessibility role (button, link, textbox, etc.)."""
        assert self.page is not None
        try:
            if name:
                locator = self.page.get_by_role(role, name=name).nth(nth)
            else:
                locator = self.page.get_by_role(role).nth(nth)
            await locator.click(timeout=CLICK_STRATEGY_TIMEOUT_MS)
            await asyncio.sleep(self.action_delay_s)
            self.state.log("click_by_role", role=role, name=name)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("click_by_role(%s, %r) failed: %s", role, name, e)
            self.state.log("click_by_role_failed", role=role, name=name, error=str(e))
            return False

    async def click_by_selector(self, selector: str) -> bool:
        """Click via CSS / XPath selector. Last-resort escape hatch."""
        assert self.page is not None
        try:
            await self.page.locator(selector).first.click(timeout=CLICK_STRATEGY_TIMEOUT_MS)
            await asyncio.sleep(self.action_delay_s)
            self.state.log("click_by_selector", selector=selector)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("click_by_selector(%r) failed: %s", selector, e)
            self.state.log("click_by_selector_failed", selector=selector, error=str(e))
            return False

    async def click_element_description(self, description: str) -> bool:
        """Best-effort click: try text, then role+name, then exact text.

        This is the function the Claude tool-use loop calls when Claude says
        "click the Submit button" — we try several strategies in order.

        Se niega ANTES de probar ninguna estrategia si el clic apunta a una
        verificación de humano. El 2026-11-25 el agente explicó por escrito que
        resolver un captcha no era algo que debiera hacer, y en el mismo paso le
        hizo clic cinco veces: la intención del modelo no es la garantía.
        """
        if names_a_human_challenge(description):
            raise HumanVerificationRequired(
                f"no se hace clic en {description!r}: es una verificación de "
                "humano y evalkit no la resuelve",
                hint="(terminá el paso diciendo que no se pudo y por qué)",
            )
        # Strategy 0: un campo de texto que se llama EXACTAMENTE así.
        #
        # Va primero por un caso real (2026-11-25): la pantalla de ingreso decía
        # "Escribe tu nombre de pila para empezar" y debajo tenía el campo "Tu
        # nombre". El agente pidió hacer clic en "Tu nombre", la estrategia de
        # texto encontró el párrafo (que contiene "tu nombre"), el clic cayó ahí
        # y lo que se escribió después no entró en ningún lado. Tres intentos,
        # el botón "Empezar" siempre deshabilitado, y la prueba cortada. Si hay
        # un campo cuyo nombre es exactamente lo pedido, eso es lo que se pidió.
        if await self._click_textbox_named(description):
            return True
        # Strategy 0b: un control que se llama EXACTAMENTE así (botón, enlace,
        # pestaña). Mismo caso, un paso después: con el nombre ya escrito, la
        # prueba pidió tocar "Empezar"; el párrafo "...de pila para empezar"
        # volvió a ganar la coincidencia de texto, el clic cayó en el párrafo,
        # el driver informó "Clicked 'Empezar'" y la pantalla no avanzó en
        # cuatro intentos. El agente terminó entrando por otro botón, con lo que
        # la prueba dejó de probar lo que decía probar.
        if await self._click_control_named(description):
            return True
        # Strategy 1: text match
        if await self.click_by_text(description, exact=False):
            return True
        # Strategy 2: button role with that name
        if await self.click_by_role("button", name=description):
            return True
        # Strategy 3: link role with that name
        if await self.click_by_role("link", name=description):
            return True
        # Strategy 4: exact text
        if await self.click_by_text(description, exact=True):
            return True
        # Strategy 5: placeholder. A chat box reads "Type here..." on screen but
        # that string is an ATTRIBUTE, not page text, so every strategy above
        # misses it -- the agent could see the tutor's message box, name it
        # correctly, and still never focus it. Without this, no probe can send
        # a message to a conversational tutor, which is most of what evalkit
        # exists to evaluate.
        if await self.click_by_placeholder(description):
            return True
        # Strategy 6: accessible name via aria-label, same blind spot.
        if await self.click_by_role("textbox", name=description):
            return True
        return False

    async def _click_textbox_named(self, name: str) -> bool:
        """Clic en el campo de texto cuyo nombre accesible o placeholder es `name`.

        Exacto, y sin esperar: `count()` contesta con lo que hay ahora, así que
        un clic sobre un botón común no paga un timeout por esta estrategia.
        """
        assert self.page is not None
        for locator in (
            self.page.get_by_role("textbox", name=name, exact=True),
            self.page.get_by_placeholder(name, exact=True),
        ):
            try:
                if await locator.count() < 1:
                    continue
                await locator.first.click(timeout=CLICK_STRATEGY_TIMEOUT_MS)
                await asyncio.sleep(self.action_delay_s)
                self.state.log("click_textbox_named", name=name)
                return True
            except Exception:  # noqa: BLE001 -- una de varias estrategias
                continue
        return False

    async def _click_control_named(self, name: str) -> bool:
        """Clic en el botón, enlace, pestaña o ítem cuyo nombre accesible es `name`.

        Exacto y sin esperar, como `_click_textbox_named`: si no hay ninguno, las
        estrategias de siempre siguen sin haber pagado nada.
        """
        assert self.page is not None
        for role in ("button", "link", "tab", "menuitem"):
            locator = self.page.get_by_role(role, name=name, exact=True)
            try:
                if await locator.count() < 1:
                    continue
                await locator.first.click(timeout=CLICK_STRATEGY_TIMEOUT_MS)
                await asyncio.sleep(self.action_delay_s)
                self.state.log("click_control_named", role=role, name=name)
                return True
            except Exception:  # noqa: BLE001 -- una de varias estrategias
                continue
        return False

    async def click_by_placeholder(self, placeholder: str) -> bool:
        """Click an input by its placeholder text. Substring, case-insensitive.

        Matched loosely on purpose: the agent reports what it SEES ("Type
        here"), which is usually a prefix of the real attribute ("Type here...").
        """
        assert self.page is not None
        try:
            await self.page.get_by_placeholder(placeholder, exact=False).first.click(
                timeout=CLICK_STRATEGY_TIMEOUT_MS
            )
            await asyncio.sleep(self.action_delay_s)
            self.state.log("click_by_placeholder", placeholder=placeholder)
            return True
        except Exception:  # noqa: BLE001 -- one of several strategies
            return False

    # ------------------------------------------------------------------
    # Typing
    # ------------------------------------------------------------------

    async def fill_by_label(self, label: str, value: str) -> bool:
        """Fill a form field identified by its accessible label."""
        assert self.page is not None
        try:
            await self.page.get_by_label(label).fill(value, timeout=DEFAULT_TIMEOUT_MS)
            await asyncio.sleep(self.action_delay_s / 2)
            self.state.log("fill_by_label", label=label, value_len=len(value))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("fill_by_label(%r) failed: %s", label, e)
            return False

    async def fill_by_placeholder(self, placeholder: str, value: str) -> bool:
        assert self.page is not None
        try:
            await self.page.get_by_placeholder(placeholder).fill(value, timeout=DEFAULT_TIMEOUT_MS)
            await asyncio.sleep(self.action_delay_s / 2)
            self.state.log("fill_by_placeholder", placeholder=placeholder, value_len=len(value))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("fill_by_placeholder(%r) failed: %s", placeholder, e)
            return False

    async def fill_by_selector(self, selector: str, value: str) -> bool:
        assert self.page is not None
        try:
            await self.page.locator(selector).first.fill(value, timeout=DEFAULT_TIMEOUT_MS)
            await asyncio.sleep(self.action_delay_s / 2)
            self.state.log("fill_by_selector", selector=selector, value_len=len(value))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("fill_by_selector(%r) failed: %s", selector, e)
            return False

    async def type_into_focused(self, text: str, delay_ms: int = 40) -> bool:
        """Type into whatever element is currently focused (after a click)."""
        assert self.page is not None
        try:
            await self.page.keyboard.type(text, delay=delay_ms)
            await asyncio.sleep(self.action_delay_s / 2)
            self.state.log("type_into_focused", value_len=len(text))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("type_into_focused failed: %s", e)
            return False

    async def press_key(self, key: str) -> None:
        """Press a single key (Enter, Tab, etc.)."""
        assert self.page is not None
        await self.page.keyboard.press(key)
        await asyncio.sleep(self.action_delay_s / 2)
        self.state.log("press_key", key=key)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    async def wait_for_url(self, url_pattern: str, timeout_ms: int = 15_000) -> bool:
        assert self.page is not None
        try:
            await self.page.wait_for_url(url_pattern, timeout=timeout_ms)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("wait_for_url(%r) failed: %s", url_pattern, e)
            return False

    async def wait_for_load(self, state: str = "networkidle", timeout_ms: int = 15_000) -> None:
        assert self.page is not None
        try:
            await self.page.wait_for_load_state(state, timeout=timeout_ms)
        except Exception as e:  # noqa: BLE001
            logger.warning("wait_for_load(%s) timed out: %s", state, e)


def _flatten_a11y(node: dict[str, Any], depth: int = 0, max_depth: int = 14) -> str:
    """Render an accessibility-tree snapshot into a compact text outline.

    Each node becomes one line of the form "<role>: <name>". Children are
    indented two spaces deeper. We skip uninteresting noise (role==generic
    with no name) and cap depth so deeply nested SPAs don't blow up the
    context budget.
    """
    if depth > max_depth or not node:
        return ""

    role = node.get("role", "")
    name = (node.get("name") or "").strip()
    value = (node.get("value") or "").strip()

    lines: list[str] = []
    if role and role not in {"generic", "none"} or name:
        label = f"{role}".strip() or "node"
        parts = [label]
        if name:
            parts.append(f": {name[:200]}")
        if value:
            parts.append(f" [value={value[:80]}]")
        lines.append("  " * depth + "".join(parts))

    for child in node.get("children", []) or []:
        sub = _flatten_a11y(child, depth + 1, max_depth)
        if sub:
            lines.append(sub)

    return "\n".join(l for l in lines if l)
