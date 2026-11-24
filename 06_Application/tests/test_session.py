"""Session tests. No real browser is launched — pages/contexts are faked.

The load-bearing property here is that a password read from the environment is
typed into the login form and NOWHERE else: not into a log, an exception, a
repr, or an artifact.
"""
import json
import logging
from pathlib import Path

import pytest

from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.session import (
    Credentials,
    PlatformAuth,
    SessionExpired,
    SessionStore,
    auto_login,
    credentials_from_env,
    ensure_session,
    interactive_login,
    is_logged_out_page,
    is_logged_out_url,
    load_platform_auth,
    login_url_for,
)

SECRET = "sup3r-s3cr3t-pw"


class FakePage:
    """Minimal stand-in for a Playwright Page.

    `present` is the set of CSS selectors the page currently matches — that is
    what the DOM-level logged-out / authenticated predicates read.
    """

    def __init__(
        self,
        *,
        url="https://app.test/login/",
        after_submit=None,
        fields=("input[type='email']", "input[type='password']"),
        present=("input[type='password']",),
        present_after_submit=None,
    ):
        self.url = url
        self._after_submit = after_submit
        self._fields = set(fields)
        self.present = set(present)
        self._present_after_submit = present_after_submit
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.goto_urls: list[str] = []

    async def goto(self, url, **kw):
        self.goto_urls.append(url)
        self.url = url

    async def fill(self, selector, value, **kw):
        if selector not in self._fields:
            raise RuntimeError(f"no such field: {selector}")
        self.filled[selector] = value

    async def query_selector(self, selector):
        return object() if selector in self.present else None

    def _submitted(self):
        if self._after_submit:
            self.url = self._after_submit
        if self._present_after_submit is not None:
            self.present = set(self._present_after_submit)

    async def click(self, selector, **kw):
        self.clicked.append(selector)
        self._submitted()

    async def press(self, selector, key, **kw):
        self.clicked.append(f"{selector}:{key}")
        self._submitted()


class FakeContext:
    def __init__(self, state=None):
        self._state = state or {"cookies": [{"name": "sid", "value": "abc"}]}

    async def storage_state(self, **kw):
        return self._state

    async def cookies(self, *a, **kw):
        return self._state.get("cookies", [])


#: A platform whose authenticated state is provable by a DOM selector.
AUTH_STRONG = PlatformAuth(
    platform="plataforma_a",
    login_url="https://app.test/login/",
    logged_out_url_markers=("/login", "/signin"),
    logged_out_selectors=("input[type='password']",),
    authenticated_selectors=("nav[aria-label='cuenta']",),
    session_cookies=(),
)

#: A platform (PLATAFORMA_B-shaped) that serves its login form at the ROOT, so the URL
#: heuristic can never fire and only the DOM check can tell us anything.
AUTH_ROOT_LOGIN = PlatformAuth(
    platform="plataforma_b",
    login_url="https://app.plataforma-b.ejemplo/",
    logged_out_url_markers=("/login",),
    logged_out_selectors=("input[type='password']",),
    authenticated_selectors=(),
    session_cookies=(),
)


# --- is_logged_out_url -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://app.plataforma-a.ejemplo/login/",
        "https://app.test/signin",
        "https://app.test/sign-in?next=/x",
        "https://accounts.google.com/o/oauth2/auth",
        "https://login.microsoftonline.com/common/oauth2",
        "https://app.test/auth/login",
    ],
)
def test_logged_out_urls_are_detected(url):
    assert is_logged_out_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://app.plataforma-a.ejemplo/classes",
        "https://app.plataforma-b.ejemplo/dashboard",
        "https://app.test/lesson/5",
        "",
    ],
)
def test_logged_in_urls_are_not_flagged(url):
    assert is_logged_out_url(url) is False


# --- SessionStore ----------------------------------------------------------


def test_store_path_is_per_platform_and_role(tmp_path: Path):
    s1 = SessionStore("plataforma_a", "student", directory=tmp_path)
    s2 = SessionStore("plataforma_a", "teacher", directory=tmp_path)
    assert s1.path != s2.path
    assert s1.path.name == "storage_state_plataforma_a_student.json"
    assert s2.path.name == "storage_state_plataforma_a_teacher.json"


def test_store_round_trip(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    assert store.exists() is False
    state = {"cookies": [{"name": "sid", "value": "abc"}], "origins": []}
    store.save(state)
    assert store.exists() is True
    assert store.load() == state


def test_store_rejects_an_unknown_role(tmp_path: Path):
    with pytest.raises(Exception):
        SessionStore("plataforma_a", "principal", directory=tmp_path)


def test_load_missing_state_raises_session_expired(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    with pytest.raises(SessionExpired):
        store.load()


# --- credentials -----------------------------------------------------------


def test_credentials_read_per_platform_and_role():
    env = {
        "PLATAFORMA_A_STUDENT_EMAIL": "s@test", "PLATAFORMA_A_STUDENT_PASSWORD": SECRET,
        "PLATAFORMA_A_TEACHER_EMAIL": "t@test", "PLATAFORMA_A_TEACHER_PASSWORD": "other",
    }
    student = credentials_from_env("plataforma_a", "student", env=env)
    teacher = credentials_from_env("plataforma_a", "teacher", env=env)
    assert student.email == "s@test"
    assert teacher.email == "t@test"
    assert credentials_from_env("plataforma_b", "student", env=env) is None  # not configured


# --- THE secret-never-leaks property --------------------------------------


@pytest.mark.asyncio
async def test_password_never_appears_in_logs_reprs_or_errors(tmp_path: Path, caplog):
    """The password is typed into the form and nowhere else."""
    caplog.set_level(logging.DEBUG)
    creds = Credentials(email="s@test", password=SECRET)

    # 1. repr/str of the credentials object must not carry it
    assert SECRET not in repr(creds)
    assert SECRET not in str(creds)

    # 2. a SUCCESSFUL login must not log it
    page = FakePage(
        after_submit="https://app.test/classes",
        present_after_submit=("nav[aria-label='cuenta']",),
    )
    ok = await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_STRONG)
    assert ok is True
    assert page.filled["input[type='password']"] == SECRET  # it DID reach the form
    assert SECRET not in caplog.text

    # 3. a FAILED login (no password field) must not leak it into the message
    caplog.clear()
    broken = FakePage(fields=("input[type='email']",))
    ok = await auto_login("plataforma_a", "student", broken, credentials=creds, auth=AUTH_STRONG)
    assert ok is False
    assert SECRET not in caplog.text

    # 4. nor into any exception text raised by the store/orchestrator
    caplog.clear()
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    with pytest.raises(SessionExpired) as e:
        await ensure_session("plataforma_a", "student", url="https://app.test/", store=store)
    assert SECRET not in str(e.value)
    assert SECRET not in caplog.text


# --- auto_login ------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_login_fills_both_fields_and_reports_success():
    page = FakePage(
        after_submit="https://app.test/classes",
        present_after_submit=("nav[aria-label='cuenta']",),
    )
    creds = Credentials(email="s@test", password=SECRET)
    assert await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_STRONG) is True
    assert page.filled["input[type='email']"] == "s@test"
    assert page.goto_urls  # navigated to the login page


@pytest.mark.asyncio
async def test_auto_login_fails_when_still_on_a_login_url():
    # form filled but we bounce back to /login -> not authenticated
    page = FakePage(after_submit="https://app.test/login?error=1")
    creds = Credentials(email="s@test", password=SECRET)
    assert await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_STRONG) is False


@pytest.mark.asyncio
async def test_auto_login_returns_false_without_credentials():
    page = FakePage()
    assert await auto_login("plataforma_a", "student", page, credentials=None, env={}) is False


# --- #3: a WRONG password must not report success -------------------------


@pytest.mark.asyncio
async def test_auto_login_rejects_a_wrong_password_on_a_root_login_page():
    """The live-verified defect: PLATAFORMA_B serves its login form at the ROOT, so the
    URL never matches a login marker and `not is_logged_out_url(url)` reported a
    bad password as success. The password field is still on screen — that is the
    signal that must decide it."""
    page = FakePage(
        url="https://app.plataforma-b.ejemplo/",
        after_submit="https://app.plataforma-b.ejemplo/",   # SPA: URL does not move
        present=("input[type='password']",),
        present_after_submit=("input[type='password']",),  # rejected, form still up
    )
    creds = Credentials(email="s@test", password="wrong")
    ok = await auto_login("plataforma_b", "student", page, credentials=creds, auth=AUTH_ROOT_LOGIN)
    assert ok is False


@pytest.mark.asyncio
async def test_auto_login_accepts_a_good_password_on_a_root_login_page():
    page = FakePage(
        url="https://app.plataforma-b.ejemplo/",
        after_submit="https://app.plataforma-b.ejemplo/",   # still no URL change
        present=("input[type='password']",),
        present_after_submit=(),                       # form gone -> authenticated
    )
    creds = Credentials(email="s@test", password=SECRET)
    ok = await auto_login("plataforma_b", "student", page, credentials=creds, auth=AUTH_ROOT_LOGIN)
    assert ok is True


@pytest.mark.asyncio
async def test_auto_login_refuses_to_claim_success_with_no_positive_signal():
    """Fail closed: a platform config with no way to prove authentication must
    not produce a `True` that downstream code reads as 'we are logged in'."""
    blind = PlatformAuth(
        platform="nueva",
        login_url="https://nueva.test/",
        logged_out_url_markers=(),
        logged_out_selectors=(),
        authenticated_selectors=(),
        session_cookies=(),
    )
    page = FakePage(url="https://nueva.test/", after_submit="https://nueva.test/home")
    creds = Credentials(email="s@test", password=SECRET)
    assert await auto_login("nueva", "student", page, credentials=creds, auth=blind) is False


@pytest.mark.asyncio
async def test_auto_login_can_prove_authentication_with_a_session_cookie():
    cookie_auth = PlatformAuth(
        platform="plataforma_a",
        login_url="https://app.test/login/",
        logged_out_url_markers=("/login",),
        logged_out_selectors=(),
        authenticated_selectors=(),
        session_cookies=("sessionid",),
    )
    page = FakePage(after_submit="https://app.test/classes", present=())
    context = FakeContext({"cookies": [{"name": "sessionid", "value": "x"}]})
    creds = Credentials(email="s@test", password=SECRET)
    assert await auto_login(
        "plataforma_a", "student", page, credentials=creds, auth=cookie_auth, context=context
    ) is True

    empty = FakeContext({"cookies": [{"name": "consent", "value": "1"}]})
    assert await auto_login(
        "plataforma_a", "student", FakePage(after_submit="https://app.test/classes", present=()),
        credentials=creds, auth=cookie_auth, context=empty,
    ) is False


# --- #2: the logged-out predicate must see the DOM, not just the URL ------


@pytest.mark.asyncio
async def test_root_login_page_is_logged_out_even_though_the_url_looks_fine():
    # is_logged_out_url can never fire here — this is why PLATAFORMA_B's guard was inert.
    assert is_logged_out_url("https://app.plataforma-b.ejemplo/") is False
    page = FakePage(url="https://app.plataforma-b.ejemplo/", present=("input[type='password']",))
    assert await is_logged_out_page(page, AUTH_ROOT_LOGIN) is True


@pytest.mark.asyncio
async def test_spa_auth_modal_over_a_real_route_is_logged_out():
    # The session expired and an auth modal rendered over /dashboard/12 without
    # navigating: the URL is pristine, the DOM is not.
    page = FakePage(url="https://app.test/dashboard/12", present=("input[type='password']",))
    assert await is_logged_out_page(page, AUTH_STRONG) is True


@pytest.mark.asyncio
async def test_missing_authenticated_marker_counts_as_logged_out():
    page = FakePage(url="https://app.test/dashboard/12", present=())
    assert await is_logged_out_page(page, AUTH_STRONG) is True
    page.present = {"nav[aria-label='cuenta']"}
    assert await is_logged_out_page(page, AUTH_STRONG) is False


@pytest.mark.asyncio
async def test_url_heuristic_still_fires_as_the_cheap_first_pass():
    page = FakePage(url="https://app.test/login/", present=("nav[aria-label='cuenta']",))
    assert await is_logged_out_page(page, AUTH_STRONG) is True


# --- #6: platform constants come from config, not from Python -------------


def _platform_config(tmp_path: Path, name: str, body: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(body, encoding="utf-8")
    return tmp_path


CONFIG_OK = """name: Nueva
vendor: Nueva Ed
auth:
  login_url: "https://nueva.test/entrar"
  logged_out_url_markers:
    - "/entrar"
  logged_out_selectors:
    - "input[type='password']"
  authenticated_selectors:
    - "nav#cuenta"
  session_cookies:
    - "nsid"
"""


def test_platform_auth_is_read_from_the_platform_config(tmp_path: Path):
    root = _platform_config(tmp_path, "nueva", CONFIG_OK)
    auth = load_platform_auth("nueva", platforms_dir=root)
    assert auth.login_url == "https://nueva.test/entrar"
    assert auth.logged_out_selectors == ("input[type='password']",)
    assert auth.authenticated_selectors == ("nav#cuenta",)
    assert auth.session_cookies == ("nsid",)
    assert login_url_for("nueva", platforms_dir=root) == "https://nueva.test/entrar"


def test_a_third_platform_needs_no_python_change(tmp_path: Path):
    # The whole point of #6: adding a platform is a config edit by the client.
    root = _platform_config(tmp_path, "tercera", CONFIG_OK.replace("nueva.test", "tercera.test"))
    assert load_platform_auth("tercera", platforms_dir=root).login_url == "https://tercera.test/entrar"


def test_unknown_platform_gets_a_clear_error(tmp_path: Path):
    with pytest.raises(EvalkitError) as e:
        load_platform_auth("fantasma", platforms_dir=tmp_path)
    assert "fantasma" in str(e.value)
    assert "config.yaml" in str(e.value)


def test_config_without_an_auth_block_gets_a_clear_error(tmp_path: Path):
    root = _platform_config(tmp_path, "muda", "name: Muda\nvendor: X\n")
    with pytest.raises(EvalkitError) as e:
        load_platform_auth("muda", platforms_dir=root)
    assert "auth" in str(e.value)
    assert e.value.path is not None  # standard file+line treatment


def test_config_with_a_broken_auth_block_points_at_the_line(tmp_path: Path):
    root = _platform_config(
        tmp_path, "rota", "name: Rota\nvendor: X\nauth:\n  login_url: 12\n"
    )
    with pytest.raises(EvalkitError) as e:
        load_platform_auth("rota", platforms_dir=root)
    assert e.value.line == 4  # points inside the auth block, at login_url


@pytest.mark.needs_real_platform
def test_the_shipped_platform_configs_carry_their_auth_block():
    # plataforma_a and plataforma_b must be config-driven now, not hardcoded in session.py
    for platform in ("plataforma_a", "plataforma_b"):
        auth = load_platform_auth(platform)
        assert auth.login_url.startswith("https://")
        assert auth.logged_out_selectors  # a real DOM check exists


# --- interactive_login -----------------------------------------------------


@pytest.mark.asyncio
async def test_interactive_login_polls_until_logged_in_then_saves(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    page = FakePage(url="https://app.test/login/")
    context = FakeContext()

    ticks = {"n": 0}

    async def fake_sleep(_s):
        # after two polls the human has finished logging in
        ticks["n"] += 1
        if ticks["n"] >= 2:
            page.url = "https://app.test/classes"

    path = await interactive_login(
        "https://app.test/login/", store, page=page, context=context,
        timeout_s=30, poll_s=0.01, sleep=fake_sleep,
    )
    assert path == store.path
    assert store.exists()
    assert store.load()["cookies"][0]["name"] == "sid"


@pytest.mark.asyncio
async def test_interactive_login_times_out_if_never_logged_in(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    page = FakePage(url="https://app.test/login/")  # never changes

    elapsed = {"t": 0.0}

    async def fake_sleep(s):
        elapsed["t"] += s

    def clock():
        return elapsed["t"]

    with pytest.raises(SessionExpired):
        await interactive_login(
            "https://app.test/login/", store, page=page, context=FakeContext(),
            timeout_s=1, poll_s=0.5, sleep=fake_sleep, now=clock,
        )
    assert not store.exists()


# --- ensure_session orchestration -----------------------------------------


@pytest.mark.asyncio
async def test_reuses_a_live_saved_session_without_logging_in(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    store.save({"cookies": []})
    calls = []

    async def verify(_s):
        return True

    async def auto():
        calls.append("auto")
        return True

    path = await ensure_session(
        "plataforma_a", "student", url="https://app.test/", store=store,
        verify_alive=verify, auto_login_fn=auto,
    )
    assert path == store.path
    assert calls == []  # never attempted a login


@pytest.mark.asyncio
async def test_stale_session_falls_through_to_auto_login(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    store.save({"cookies": []})
    calls = []

    async def verify(_s):
        return False  # saved state is dead

    async def auto():
        calls.append("auto")
        store.save({"cookies": [{"name": "fresh", "value": "1"}]})
        return True

    async def interactive():
        calls.append("interactive")
        return store.path

    path = await ensure_session(
        "plataforma_a", "student", url="https://app.test/", store=store,
        verify_alive=verify, auto_login_fn=auto, interactive_login_fn=interactive,
    )
    assert path == store.path
    assert calls == ["auto"]  # auto succeeded, human never bothered


@pytest.mark.asyncio
async def test_falls_back_to_interactive_when_auto_login_fails(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    calls = []

    async def auto():
        calls.append("auto")
        return False  # e.g. SSO wall

    async def interactive():
        calls.append("interactive")
        store.save({"cookies": [{"name": "human", "value": "1"}]})
        return store.path

    path = await ensure_session(
        "plataforma_a", "student", url="https://app.test/", store=store,
        auto_login_fn=auto, interactive_login_fn=interactive,
    )
    assert path == store.path
    assert calls == ["auto", "interactive"]  # tried automatic FIRST


# --- #4: ensure_session on a CLEAN machine --------------------------------


@pytest.mark.asyncio
async def test_auto_login_success_saves_the_state_on_a_clean_store(tmp_path: Path):
    """The clean-machine path. auto_login authenticates the live page but the
    storage state was never persisted, so store.exists() was False and the code
    fell through to the 300-second human prompt anyway."""
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    assert not store.exists()  # clean machine
    calls = []

    async def auto():
        calls.append("auto")
        return True

    async def save_state():
        calls.append("save")
        return {"cookies": [{"name": "sid", "value": "fresh"}]}

    async def interactive():
        calls.append("interactive")   # must NEVER happen
        return store.path

    path = await ensure_session(
        "plataforma_a", "student", url="https://app.test/", store=store,
        auto_login_fn=auto, save_state_fn=save_state, interactive_login_fn=interactive,
    )

    assert path == store.path
    assert store.exists()                       # the state is actually on disk
    assert store.load()["cookies"][0]["value"] == "fresh"   # ...and usable
    assert "interactive" not in calls           # zero interactive calls
    assert calls == ["auto", "save"]


@pytest.mark.asyncio
async def test_a_stale_file_is_not_handed_back_after_a_failed_auto_login(tmp_path: Path):
    """The other half of the bug: the branch above already rejected this file as
    dead, so `store.exists()` must not resurrect it."""
    store = SessionStore("plataforma_a", "student", directory=tmp_path)
    store.save({"cookies": [{"name": "dead", "value": "1"}]})
    calls = []

    async def verify(_s):
        return False  # the saved state is dead

    async def auto():
        calls.append("auto")
        return True   # claims success but saves nothing

    async def interactive():
        calls.append("interactive")
        store.save({"cookies": [{"name": "human", "value": "1"}]})
        return store.path

    path = await ensure_session(
        "plataforma_a", "student", url="https://app.test/", store=store,
        verify_alive=verify, auto_login_fn=auto, interactive_login_fn=interactive,
    )
    assert calls == ["auto", "interactive"]     # did NOT return the dead file
    assert store.load()["cookies"][0]["name"] == "human"
    assert path == store.path


@pytest.mark.asyncio
async def test_raises_when_every_path_fails(tmp_path: Path):
    store = SessionStore("plataforma_a", "student", directory=tmp_path)

    async def auto():
        return False

    with pytest.raises(SessionExpired):
        await ensure_session(
            "plataforma_a", "student", url="https://app.test/", store=store, auto_login_fn=auto
        )


@pytest.mark.asyncio
async def test_a_lesson_page_is_not_a_login_wall(monkeypatch):
    """Absence of the dashboard chrome is not evidence of a dead session.

    Plataforma A's `authenticated_selectors` were captured from the Dashboard
    (`side-nav`, `user-profile`). A lesson is an immersive view that renders
    neither -- so mid-run, on exactly the pages a probe must reach, the gate
    read "no positive markers" as "logged out" and killed the run one step
    after it successfully opened the course.

    The real logged-out case is not ambiguous on Plataforma A: the app REDIRECTS to
    /login/?callbackUrl=..., which the URL pass already catches. So at run
    time a page is a login wall only on POSITIVE evidence. At login time the
    opposite still holds -- see the test below.
    """
    auth = PlatformAuth(
        platform="plataforma_a",
        login_url="https://app.plataforma-a.ejemplo/login/",
        logged_out_selectors=("input[type='password']",),
        authenticated_selectors=("[data-testid='side-nav']",),
    )
    lesson = FakePage(
        url="https://app.plataforma-a.ejemplo/assignment/019e/?type=LESSON",
        present=(),          # immersive view: no side-nav, and no password field
    )

    assert await is_logged_out_page(lesson, auth, require_positive=False) is False
    # ...but a real redirect to the login wall is still caught, by URL alone
    wall = FakePage(url="https://app.plataforma-a.ejemplo/login/?callbackUrl=%2F", present=())
    assert await is_logged_out_page(wall, auth, require_positive=False) is True
    # ...and so is a password field rendered over an in-app URL
    modal = FakePage(
        url="https://app.plataforma-a.ejemplo/dashboard/12",
        present=("input[type='password']",),
    )
    assert await is_logged_out_page(modal, auth, require_positive=False) is True


@pytest.mark.asyncio
async def test_login_still_demands_positive_proof(monkeypatch):
    """The strict rule stays where it was earned.

    A wrong password once reported success because "the login form is gone"
    was treated as proof. At LOGIN time an ambiguous page is still a failure.
    """
    auth = PlatformAuth(
        platform="plataforma_a",
        login_url="https://app.plataforma-a.ejemplo/login/",
        logged_out_selectors=("input[type='password']",),
        authenticated_selectors=("[data-testid='side-nav']",),
    )
    ambiguous = FakePage(url="https://app.plataforma-a.ejemplo/whatever", present=())

    assert await is_logged_out_page(ambiguous, auth) is True


@pytest.mark.needs_real_platform
def test_plataforma_a_can_tell_it_is_logged_out_on_a_provider_chooser():
    """Plataforma A's /login/ has no password field until a provider is chosen.

    Declaring only `input[type='password']` made `logged_out_selectors` match
    nothing on the actual login wall, so `is_logged_out()` answered False while
    logged out and `runner._refuse_if_logged_out` -- the second, independent
    gate that stops a login screen being filed as evidence -- was inert on the
    one platform being evaluated. Verified live on 2026-08-10: password = 0,
    form = 0, email = 0, "Continue with email" = 1.
    """
    from pathlib import Path

    from lxp_eval.evalkit.session import load_platform_auth

    auth = load_platform_auth(
        "plataforma_a", platforms_dir=Path(__file__).resolve().parents[1] / "platforms"
    )
    joined = " ".join(auth.logged_out_selectors)
    assert "Continue with email" in joined, (
        "sin un marcador del selector de proveedor, el muro de login de Plataforma A "
        "no se reconoce como deslogueado"
    )


# --- login en varios pasos (selector de proveedor) ---------------------------


class StepwisePage(FakePage):
    """A login that reveals its fields one click at a time.

    Plataforma A: /login/ shows "Continue with email / Google / Google Classroom" and
    no form at all. The email box appears after the first click, and the
    password box only after "Next". Verified live 2026-08-10.
    """

    def __init__(self, *, unlocks: dict[str, str], **kw):
        super().__init__(fields=(), **kw)
        self._unlocks = unlocks

    async def click(self, selector, **kw):
        self.clicked.append(selector)
        if selector in self._unlocks:
            self._fields.add(self._unlocks[selector])
        else:
            self._submitted()


AUTH_CHOOSER = PlatformAuth(
    platform="plataforma_a",
    login_url="https://app.plataforma-a.ejemplo/login/",
    authenticated_selectors=("[data-testid='side-nav']",),
    before_email_clicks=("button:has-text('Continue with email')",),
    before_password_clicks=("button:has-text('Next')",),
    submit_selectors=("button:has-text('Log in')",),
)


@pytest.mark.asyncio
async def test_auto_login_clicks_through_a_provider_chooser():
    """The whole reason a human had to sit in front of Plataforma A's login.

    auto_login went straight to filling an email box that does not exist yet,
    found nothing, and gave up -- so every run needed a person to capture the
    session by hand, which is not a product a client can be handed.
    """
    page = StepwisePage(
        unlocks={
            "button:has-text('Continue with email')": "input[type='email']",
            "button:has-text('Next')": "input[type='password']",
        },
        present=(),
        after_submit="https://app.plataforma-a.ejemplo/studentV2/",
        present_after_submit=("[data-testid='side-nav']",),
    )
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")

    ok = await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_CHOOSER)

    assert ok is True
    assert page.clicked[:2] == [
        "button:has-text('Continue with email')",
        "button:has-text('Next')",
    ]
    assert page.filled["input[type='email']"] == "evaluador@ejemplo.org"
    assert page.filled["input[type='password']"] == "secreto"
    assert "button:has-text('Log in')" in page.clicked


@pytest.mark.asyncio
async def test_a_platform_with_a_plain_form_is_untouched():
    """The new keys are optional; a one-page form must behave exactly as before."""
    page = FakePage(after_submit="https://app.test/classes",
                    present_after_submit=("nav[aria-label='cuenta']",))
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")

    ok = await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_STRONG)

    assert ok is True
    assert page.filled["input[type='email']"] == "evaluador@ejemplo.org"


@pytest.mark.asyncio
async def test_a_missing_pre_click_does_not_abort_the_login():
    """Best effort: a chooser already dismissed is not a failure."""
    page = FakePage(after_submit="https://app.test/classes",
                    present_after_submit=("[data-testid='side-nav']",))
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")

    auth = PlatformAuth(
        platform="plataforma_a",
        login_url="https://app.test/login/",
        authenticated_selectors=("[data-testid='side-nav']",),
        before_email_clicks=("button:has-text('Un boton que no esta')",),
    )
    assert await auto_login("plataforma_a", "student", page, credentials=creds, auth=auth) is True


@pytest.mark.needs_real_platform
def test_the_chooser_steps_are_read_from_the_plataforma_a_config():
    from pathlib import Path

    from lxp_eval.evalkit.session import load_platform_auth

    auth = load_platform_auth(
        "plataforma_a", platforms_dir=Path(__file__).resolve().parents[1] / "platforms"
    )
    assert "Continue with email" in " ".join(auth.before_email_clicks)
    assert "Next" in " ".join(auth.before_password_clicks)


# --- el rol, que viaja dentro del token de sesión ----------------------------


ROLE_SWITCH = {
    "open": "[data-testid='user-profile']",
    "choose": {"student": "Student", "teacher": "Teacher"},
    "lands_on": {
        "student": "https://app.plataforma-a.ejemplo/student/home/",
        "teacher": "https://app.plataforma-a.ejemplo/home/",
    },
}


class RolePage(FakePage):
    """Login lands on the teacher home; choosing a role navigates."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.menu_open = False

    async def click(self, selector, **kw):
        if selector == ROLE_SWITCH["open"]:
            self.clicked.append(selector)
            self.menu_open = True
            return
        for role, label in ROLE_SWITCH["choose"].items():
            if self.menu_open and label in selector:
                self.clicked.append(selector)
                self.url = ROLE_SWITCH["lands_on"][role]
                return
        # Like Playwright: a selector that matches nothing RAISES, so
        # `_click_first` can fall through to the next candidate.
        if self.menu_open:
            raise RuntimeError(f"no such element: {selector}")
        self.clicked.append(selector)
        self._submitted()


@pytest.mark.asyncio
async def test_a_fresh_login_is_put_into_the_role_that_was_asked_for():
    """Bug #4, documented in config since 2026-07-19 and never implemented.

    One account, two roles, and the role travels INSIDE the session token, so
    /login/ always lands on Teacher. Walking the teacher UI makes every
    student-tutor criterion (PT-D9-*, PT-D8-*) come back NO_EVALUADO -- the
    report saying the tutor could not be reached while it was one click away.
    """
    auth = PlatformAuth(
        platform="plataforma_a",
        login_url="https://app.plataforma-a.ejemplo/login/",
        authenticated_selectors=("[data-testid='side-nav']",),
        role_switch=ROLE_SWITCH,
    )
    page = RolePage(
        after_submit="https://app.plataforma-a.ejemplo/home/",
        present_after_submit=("[data-testid='side-nav']",),
    )
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")

    ok = await auto_login("plataforma_a", "student", page, credentials=creds, auth=auth)

    assert ok is True
    assert page.url == "https://app.plataforma-a.ejemplo/student/home/"
    assert "[data-testid='user-profile']" in page.clicked


@pytest.mark.asyncio
async def test_a_session_already_in_the_right_role_is_left_alone():
    auth = PlatformAuth(
        platform="plataforma_a", login_url="https://app.plataforma-a.ejemplo/login/",
        authenticated_selectors=("[data-testid='side-nav']",), role_switch=ROLE_SWITCH,
    )
    page = RolePage(
        after_submit="https://app.plataforma-a.ejemplo/student/home/",
        present_after_submit=("[data-testid='side-nav']",),
    )
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")

    assert await auto_login("plataforma_a", "student", page, credentials=creds, auth=auth) is True
    assert "[data-testid='user-profile']" not in page.clicked


@pytest.mark.asyncio
async def test_a_platform_with_one_interface_needs_no_role_switch():
    page = FakePage(after_submit="https://app.test/classes",
                    present_after_submit=("nav[aria-label='cuenta']",))
    creds = Credentials(email="evaluador@ejemplo.org", password="secreto")
    assert await auto_login("plataforma_a", "student", page, credentials=creds, auth=AUTH_STRONG) is True


@pytest.mark.needs_real_platform
def test_the_role_switch_is_read_from_the_plataforma_a_config():
    from pathlib import Path

    from lxp_eval.evalkit.session import load_platform_auth

    auth = load_platform_auth(
        "plataforma_a", platforms_dir=Path(__file__).resolve().parents[1] / "platforms"
    )
    assert auth.role_switch["choose"]["student"] == "Student"
    assert "student/home" in auth.role_switch["lands_on"]["student"]
