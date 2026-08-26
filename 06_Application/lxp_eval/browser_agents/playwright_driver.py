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
    ) -> None:
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
            context_kwargs: dict[str, Any] = dict(
                viewport=self.viewport,
                locale=self.locale,
            )
            if self.user_agent:
                context_kwargs["user_agent"] = self.user_agent
            if self.storage_state is not None:
                context_kwargs["storage_state"] = str(self.storage_state)
                logger.info("reusando sesión guardada: %s", self.storage_state.name)
            self._context = await self._browser.new_context(**context_kwargs)
            self.page = await self._context.new_page()
            self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        except BaseException:
            # Every OTHER way launch can die (a bad context arg, a crashed page)
            # would leak the same way. Tear down whatever got started, then let
            # the original exception through untouched.
            await self.close()
            raise
        self.state.log("launch", headless=self.headless)

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
        logger.info("navigate -> %s", url)
        await self.page.goto(url, wait_until=wait_until)
        await asyncio.sleep(self.action_delay_s)
        final_url = self.page.url
        self.state.log("navigate", url=url, final_url=final_url)
        return final_url

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
        """
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
        """
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
