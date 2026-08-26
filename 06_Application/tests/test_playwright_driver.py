"""PlaywrightDriver tests — no real browser is ever launched.

`launch()` lazily imports `playwright.async_api.async_playwright`, so these
tests monkeypatch that one symbol with a fake that records what would have been
started and closed. Nothing here opens Chromium, hits a network, or calls an API.

Two properties are load-bearing:

* the missing-session path must not leak a Chromium process. The raise happened
  inside `__aenter__`, so `__aexit__` never ran and every `async with` on an
  absent storage_state leaked a whole browser;
* what the driver reports about the page must be true -- a truncated page read
  must announce itself, and the logged-out check must look at the DOM, because
  the URL heuristic is structurally blind to a root-served login form.
"""
from pathlib import Path

import pytest

from lxp_eval.browser_agents.playwright_driver import PlaywrightDriver
from lxp_eval.evalkit.session import PlatformAuth, SessionExpired

AUTH_ROOT_LOGIN = PlatformAuth(
    platform="plataforma_b",
    login_url="https://app.plataforma-b.ejemplo/",
    logged_out_url_markers=("/login",),
    logged_out_selectors=("input[type='password']",),
    authenticated_selectors=(),
    session_cookies=(),
)


# --- the fake playwright stack --------------------------------------------


class _FakeLocator:
    """One localiser result. `.first` returns itself, as Playwright's does."""

    def __init__(self, page, kind, value, hits):
        self._page, self._kind, self._value, self._hits = page, kind, value, hits

    @property
    def first(self):
        return self

    async def click(self, **kw):
        if not any(self._value in h for h in self._hits):
            raise RuntimeError(f"no {self._kind} matching {self._value!r}")
        self._page.clicked.setdefault(self._kind, []).append(self._value)


class FakePage:
    def __init__(self, url="https://app.test/lesson/5", present=()):
        self.url = url
        self.present = set(present)
        #: What each localiser can find. Empty by default: a page where the
        #: agent's label matches nothing is the case worth testing.
        self.text_hits: set[str] = set()
        self.placeholder_hits: set[str] = set()
        self.role_hits: set[str] = set()
        self.clicked: dict[str, list[str]] = {}

    @property
    def clicked_placeholders(self):
        return self.clicked.get("placeholder", [])

    def set_default_timeout(self, ms):
        self.timeout = ms

    async def query_selector(self, selector):
        return object() if selector in self.present else None

    def get_by_text(self, value, **kw):
        return _FakeLocator(self, "text", value, self.text_hits)

    def get_by_placeholder(self, value, **kw):
        return _FakeLocator(self, "placeholder", value, self.placeholder_hits)

    def get_by_role(self, role, name=None, **kw):
        return _FakeLocator(self, "role", name or role, self.role_hits)


class FakeContext:
    def __init__(self, page):
        self._page = page
        self.closed = False

    async def new_page(self):
        return self._page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, page):
        self._page = page
        self.closed = False
        self.contexts: list[FakeContext] = []

    async def new_context(self, **kw):
        ctx = FakeContext(self._page)
        self.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, browser):
        self._browser = browser
        self.launches = 0

    async def launch(self, **kw):
        self.launches += 1
        return self._browser


class FakePlaywright:
    def __init__(self, page):
        self.chromium = FakeChromium(FakeBrowser(page))
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1
        return self

    async def stop(self):
        self.stopped += 1

    def __call__(self):
        return self


@pytest.fixture
def fake_pw(monkeypatch):
    """Swap the lazily imported `async_playwright` for the fake stack."""
    import playwright.async_api as api

    pw = FakePlaywright(FakePage())
    monkeypatch.setattr(api, "async_playwright", pw)
    return pw


# --- #8: the missing-session path must not leak a browser -----------------


@pytest.mark.asyncio
async def test_missing_storage_state_never_starts_a_browser(tmp_path: Path, fake_pw):
    """The leak: launch() started playwright AND Chromium, then raised because
    the storage_state file was absent. Inside __aenter__ that means __aexit__
    never runs, so `async with PlaywrightDriver(...)` leaked a whole Chromium
    process EVERY time -- on the most ordinary failure there is."""
    driver = PlaywrightDriver(
        tmp_path / "shots", storage_state=tmp_path / "no_such_state.json"
    )
    with pytest.raises(SessionExpired):
        await driver.launch()

    assert fake_pw.started == 0                    # never even started playwright
    assert fake_pw.chromium.launches == 0          # ...so no Chromium to leak


@pytest.mark.asyncio
async def test_async_with_leaks_nothing_when_the_session_is_missing(tmp_path: Path, fake_pw):
    driver = PlaywrightDriver(
        tmp_path / "shots", storage_state=tmp_path / "no_such_state.json"
    )
    with pytest.raises(SessionExpired):
        async with driver:
            pass
    assert fake_pw.chromium.launches == 0
    assert fake_pw.chromium._browser.closed is False  # nothing was opened to close


@pytest.mark.asyncio
async def test_a_failure_after_launch_still_closes_the_browser(tmp_path: Path, fake_pw, monkeypatch):
    """Belt and braces for every OTHER way launch() can die (a bad context arg,
    a crashed page): whatever started must be torn down before the raise."""
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")

    boom = RuntimeError("new_context falló")

    async def explode(**kw):
        raise boom

    monkeypatch.setattr(fake_pw.chromium._browser, "new_context", explode)

    driver = PlaywrightDriver(tmp_path / "shots", storage_state=state)
    with pytest.raises(RuntimeError):
        await driver.launch()

    assert fake_pw.chromium._browser.closed is True
    assert fake_pw.stopped == 1


@pytest.mark.asyncio
async def test_a_present_storage_state_launches_normally(tmp_path: Path, fake_pw):
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    driver = PlaywrightDriver(tmp_path / "shots", storage_state=state)
    await driver.launch()
    assert fake_pw.chromium.launches == 1
    assert driver.page is not None
    await driver.close()


# --- #9: a truncated read must announce itself ----------------------------


@pytest.mark.asyncio
async def test_read_page_records_that_it_truncated(tmp_path: Path):
    driver = PlaywrightDriver(tmp_path / "shots")
    driver.page = _StubTextPage("x" * 12_000)

    text = await driver.read_page(max_chars=100)

    assert driver.last_read_truncated is True
    assert driver.last_read_full_chars == 12_000   # the ORIGINAL length
    assert "trunc" in text.lower()                 # and the artifact says so too


@pytest.mark.asyncio
async def test_read_page_reports_no_truncation_when_it_fits(tmp_path: Path):
    driver = PlaywrightDriver(tmp_path / "shots")
    driver.page = _StubTextPage("corto")
    await driver.read_page(max_chars=100)
    assert driver.last_read_truncated is False
    assert driver.last_read_full_chars == len("corto")


@pytest.mark.asyncio
async def test_truncation_flag_resets_between_reads(tmp_path: Path):
    # A stale True would put a false "TEXTO TRUNCADO" note on later evidence.
    driver = PlaywrightDriver(tmp_path / "shots")
    driver.page = _StubTextPage("x" * 500)
    await driver.read_page(max_chars=10)
    assert driver.last_read_truncated is True
    driver.page = _StubTextPage("corto")
    await driver.read_page(max_chars=10)
    assert driver.last_read_truncated is False


class _StubTextPage:
    """A page with no usable a11y tree, so read_page falls back to inner_text
    and the text under test reaches the truncator verbatim."""

    def __init__(self, text):
        self.accessibility = self
        self._text = text

    async def snapshot(self, **kw):
        raise RuntimeError("sin árbol de accesibilidad")

    async def inner_text(self, selector):
        return self._text


# --- #2: assert_session_alive must consult the DOM ------------------------


@pytest.mark.asyncio
async def test_assert_session_alive_catches_a_root_served_login_form(tmp_path: Path):
    """PLATAFORMA_B's login form lives at the site ROOT, so no URL marker can match and
    the URL-only guard NEVER FIRED for that platform at all."""
    driver = PlaywrightDriver(tmp_path / "shots", platform_auth=AUTH_ROOT_LOGIN)
    driver.page = FakePage(
        url="https://app.plataforma-b.ejemplo/", present=("input[type='password']",)
    )
    with pytest.raises(SessionExpired):
        await driver.assert_session_alive()
    assert await driver.is_logged_out() is True


@pytest.mark.asyncio
async def test_assert_session_alive_catches_an_spa_auth_modal(tmp_path: Path):
    driver = PlaywrightDriver(tmp_path / "shots", platform_auth=AUTH_ROOT_LOGIN)
    driver.page = FakePage(
        url="https://app.plataforma-b.ejemplo/dashboard/12", present=("input[type='password']",)
    )
    with pytest.raises(SessionExpired):
        await driver.assert_session_alive()


@pytest.mark.asyncio
async def test_a_real_authenticated_page_passes(tmp_path: Path):
    driver = PlaywrightDriver(tmp_path / "shots", platform_auth=AUTH_ROOT_LOGIN)
    driver.page = FakePage(url="https://app.plataforma-b.ejemplo/dashboard/12", present=())
    await driver.assert_session_alive()      # does not raise
    assert await driver.is_logged_out() is False


@pytest.mark.asyncio
async def test_without_a_platform_config_the_url_heuristic_still_applies(tmp_path: Path):
    driver = PlaywrightDriver(tmp_path / "shots")   # no platform_auth
    driver.page = FakePage(url="https://app.test/login/")
    with pytest.raises(SessionExpired):
        await driver.assert_session_alive()


@pytest.mark.asyncio
async def test_a_chat_box_is_reachable_by_its_placeholder(fake_pw, tmp_path):
    """"Type here..." is an attribute, not page text.

    Plataforma A's tutor message box shows "Type here..." on screen. The agent saw it,
    named it correctly, and every click strategy missed, because none of them
    look at the placeholder attribute -- so the run hung waiting on
    get_by_text("Type here"). Without this strategy no probe can send a message
    to a conversational tutor at all.
    """
    driver = PlaywrightDriver(tmp_path / "shots")
    await driver.launch()
    page = driver.page
    page.text_hits = set()          # nothing matches by text, as on the real page
    page.placeholder_hits = {"Type here"}

    assert await driver.click_element_description("Type here") is True
    assert page.clicked_placeholders == ["Type here"]


@pytest.mark.asyncio
async def test_evidence_capture_is_never_truncated(tmp_path: Path):
    """`max_chars=None` captures the whole page. The evidence needs all of it.

    read_page served two masters with one number: the AGENT's token budget and
    the EVIDENCE file. They are not the same requirement, and conflating them
    cost a whole probe. PB-PT-D8-01 wrote 27 page_text captures, every one cut
    at 8000 of ~25000 characters -- and because truncation keeps the HEAD, what
    survived was the course sidebar while the tutor conversation, an overlay
    rendered late in the DOM, was entirely inside the discarded 17000. The
    judge then scored the criterion NO_EVALUADO for citing no valid artifact,
    which was correct: the artifacts did not contain the thing under
    evaluation. Two hours of live probing recorded as nothing.
    """
    long_page = "".join(f"linea {i}\n" for i in range(4000))
    driver = PlaywrightDriver(tmp_path / "shots")
    driver.page = _StubTextPage(long_page)

    full = await driver.read_page(max_chars=None)

    assert full == long_page.strip(), "evidence must be the whole page, verbatim"
    assert "TRUNCADO" not in full
    assert driver.last_read_truncated is False
    assert driver.last_read_full_chars == len(long_page.strip())

    # ...while the agent's own view still gets capped, which is what the
    # number was always for.
    capped = await driver.read_page(max_chars=200)
    assert len(capped) < len(long_page)
    assert driver.last_read_truncated is True


# --- el árbol de accesibilidad, tras su desaparición de Playwright ----------


class _AriaPage:
    """Playwright >= 1.5x: aria_snapshot existe, accessibility no."""

    def __init__(self, aria="- heading \"Morning, Estudiante de prueba\"\n- button \"Math help\""):
        self._aria = aria
        self.inner_text_calls = 0

    async def aria_snapshot(self, **kw):
        return self._aria

    async def inner_text(self, sel):
        self.inner_text_calls += 1
        return "texto crudo larguísimo con todo el ruido de la página"


class _LegacyPage:
    """Playwright viejo: sin aria_snapshot, con accessibility."""

    class _A11y:
        async def snapshot(self, **kw):
            return {"role": "heading", "name": "Morning, Estudiante de prueba"}

    def __init__(self):
        self.accessibility = self._A11y()
        self.inner_text_calls = 0

    async def inner_text(self, sel):
        self.inner_text_calls += 1
        return "texto crudo"


class _BarePage:
    """Ni una cosa ni la otra: sólo queda el texto."""

    def __init__(self):
        self.inner_text_calls = 0

    async def inner_text(self, sel):
        self.inner_text_calls += 1
        return "texto crudo"


def _driver_with(page):
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d.page = page
    return d


@pytest.mark.asyncio
async def test_the_accessibility_tree_is_read_through_aria_snapshot():
    """`page.accessibility` está ELIMINADO en Playwright 1.61.

    La llamada fallaba en cada lectura y el driver caía a inner_text sin que
    nada se rompiera ruidosamente: el agente pasó de un árbol de roles a texto
    crudo -- 20414 caracteres contra un presupuesto de 3000 en el tablero de
    Plataforma A, o sea 85% de cada página descartado antes de poder razonarla.
    """
    page = _AriaPage()
    text = await _driver_with(page)._page_text()
    assert "Math help" in text
    assert page.inner_text_calls == 0, "no hace falta el respaldo si el árbol está"


@pytest.mark.asyncio
async def test_an_older_playwright_still_uses_its_accessibility_tree():
    page = _LegacyPage()
    text = await _driver_with(page)._page_text()
    assert "Morning, Estudiante de prueba" in text
    assert page.inner_text_calls == 0


@pytest.mark.asyncio
async def test_without_either_api_the_page_text_is_still_read():
    page = _BarePage()
    text = await _driver_with(page)._page_text()
    assert text == "texto crudo"
    assert page.inner_text_calls == 1


# --- clicar lo que el agente VE, no sólo lo que está escrito -----------------


class _Loc:
    def __init__(self, count, clicked, etiqueta):
        self._count, self._clicked, self._etiqueta = count, clicked, etiqueta

    def nth(self, n):
        return self

    async def click(self, **kw):
        if not self._count:
            raise RuntimeError("no such element")
        self._clicked.append(self._etiqueta)


class _NamedPage:
    """Una página donde el nombre accesible NO es texto visible.

    Es el caso de un botón con `aria-label`: aparece en el árbol ARIA como
    `button "Navigate to step 1"` y `get_by_text` no lo encuentra jamás.
    """

    def __init__(self, *, texto=(), roles=(), labels=()):
        self.texto, self.roles, self.labels = set(texto), set(roles), set(labels)
        self.clicked: list[str] = []

    def get_by_text(self, text, exact=False):
        return _Loc(text in self.texto, self.clicked, f"text:{text}")

    def get_by_role(self, role, name=None):
        return _Loc((role, name) in self.roles, self.clicked, f"role:{role}:{name}")

    def get_by_label(self, text):
        return _Loc(text in self.labels, self.clicked, f"label:{text}")


def _driver_page(page):
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d.page = page
    d.action_delay_s = 0
    d.state = type("S", (), {"log": lambda self, *a, **k: None})()
    return d


@pytest.mark.asyncio
async def test_a_button_named_by_aria_label_is_clickable():
    """Regresión de hacer BIEN otra cosa.

    Al leer la página como árbol de accesibilidad, el agente empezó a ver
    nombres que vienen de `aria-label` -- `button "Navigate to step 1"` -- y a
    pedirlos por su nombre. `click_by_text` busca texto VISIBLE, así que ese
    elemento no existe para ella: contra la plataforma en vivo,
    get_by_text da 0 y get_by_role(name=...) da 1. El agente veía algo que no
    podía tocar, y gastaba un intento en cada uno.
    """
    page = _NamedPage(roles={("button", "Navigate to step 1")})
    ok = await _driver_page(page).click_by_text("Navigate to step 1")
    assert ok is True
    assert page.clicked == ["role:button:Navigate to step 1"]


@pytest.mark.asyncio
async def test_visible_text_still_wins():
    """Sin cambiar lo que ya funcionaba: el texto visible se intenta primero."""
    page = _NamedPage(texto={"Continuar"}, roles={("button", "Continuar")})
    assert await _driver_page(page).click_by_text("Continuar") is True
    assert page.clicked == ["text:Continuar"]


@pytest.mark.asyncio
async def test_a_label_only_element_is_reachable():
    page = _NamedPage(labels={"Buscar"})
    assert await _driver_page(page).click_by_text("Buscar") is True
    assert page.clicked == ["label:Buscar"]


@pytest.mark.asyncio
async def test_something_that_is_not_there_still_fails():
    """Fallar cerrado: inventar un clic sería peor que no hacerlo."""
    page = _NamedPage()
    assert await _driver_page(page).click_by_text("No existe") is False
    assert page.clicked == []
