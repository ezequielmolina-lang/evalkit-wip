"""Browser sessions for headless, scriptable evaluation runs.

The point is automation without a human driving a browser: log in once, save the
storage state, and every later run reuses it headlessly. Two login paths:

* `auto_login`  — reads credentials from the environment and fills the
  platform's own email+password form. Works where the platform offers a direct
  login (Plataforma A's "Continue with email").
* `interactive_login` — headful fallback for SSO/2FA walls: a human logs in
  once, we poll until the page is no longer a login URL, then save the state.

Sessions are per (platform, role). The teacher panel is unreachable from a
student account -- that is exactly why PLATAFORMA_B's whole D9 came back NO_PUDE in the
blind run -- so a run must say which role it needs.

SECRETS: the password is read from the environment, typed into the form, and
goes nowhere else. It is never logged, never put in an exception, never written
to the ledger or an artifact. `Credentials` redacts it in repr/str so that even
an accidental log of the object cannot leak it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from .errors import EvalkitError
from .fileguard import restrict_to_user, write_private_text
from .yamlsrc import line_of, load_yaml_with_lines

logger = logging.getLogger(__name__)

Role = Literal["student", "teacher"]
ROLES = ("student", "teacher")

#: URL fragments that mean "not authenticated" (own login pages + SSO walls).
#: A cheap FIRST PASS only. It cannot see a login form served at a site root
#: (PLATAFORMA_B) nor an auth modal rendered over a real route without navigating (any
#: SPA), so a platform's real verdict comes from the DOM -- see
#: `is_logged_out_page` and the per-platform `auth:` block in config.yaml.
LOGGED_OUT_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/auth/login",
    "/logout",
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "auth0.com",
    "okta.com",
)

_EMAIL_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[name='username']",
    "input[id='email']",
)
_PASSWORD_SELECTORS = (
    "input[type='password']",
    "input[name='password']",
)
_SUBMIT_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
)


class SessionExpired(EvalkitError):
    """No usable session: never established, or bounced back to a login page."""


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str

    def __repr__(self) -> str:  # never leak the secret through a log of the object
        return f"Credentials(email={self.email!r}, password='***')"

    __str__ = __repr__


def credentials_from_env(
    platform: str, role: Role, *, env: dict[str, str] | None = None
) -> Credentials | None:
    """Read `<PLATFORM>_<ROLE>_EMAIL` / `_PASSWORD`. None if either is missing."""
    env = os.environ if env is None else env
    prefix = f"{platform.upper()}_{role.upper()}"
    email = env.get(f"{prefix}_EMAIL", "")
    password = env.get(f"{prefix}_PASSWORD", "")
    if not email or not password:
        logger.debug("no credentials configured for %s/%s", platform, role)
        return None
    return Credentials(email=email, password=password)


def is_logged_out_url(url: str, markers: tuple[str, ...] | None = None) -> bool:
    """True if this URL is a login/SSO page rather than the app.

    Substring test, so it is only ever a cheap first pass -- see the note on
    LOGGED_OUT_MARKERS for the two cases it structurally cannot catch.
    """
    if not url:
        return False
    lowered = url.lower()
    return any(marker in lowered for marker in (markers or LOGGED_OUT_MARKERS))


# ---------------------------------------------------------------------------
# Per-platform auth configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformAuth:
    """How to log into ONE platform, and how to tell whether we are logged in.

    Every field is read from `platforms/<name>/config.yaml`, never hardcoded:
    pointing evalkit at a third platform must be a config edit by the client,
    not a source edit by the author.

    * `logged_out_selectors`   -- present => definitely NOT authenticated
                                  (a password field, a login modal).
    * `authenticated_selectors`-- absent  => definitely NOT authenticated
                                  (an account menu, a logout link).
    Configure at least one of the two, or evalkit cannot tell a real page from
    a login wall and refuses to guess.
    """

    platform: str
    login_url: str
    logged_out_url_markers: tuple[str, ...] = LOGGED_OUT_MARKERS
    logged_out_selectors: tuple[str, ...] = ()
    authenticated_selectors: tuple[str, ...] = ()
    session_cookies: tuple[str, ...] = ()
    #: Clicks that REVEAL the form, for logins that arrive one field at a time.
    #:
    #: Plataforma A's /login/ is a provider chooser -- "Continue with email / Google /
    #: Google Classroom" -- with no form at all: the email box appears after
    #: the first click and the password box only after "Next". auto_login went
    #: straight to filling an email field that did not exist yet, found
    #: nothing, and returned False, so every run needed a person to sit in
    #: front of a browser and capture the session by hand. That is not
    #: something a client can be handed, and it is the whole reason this
    #: platform could never run unattended.
    #:
    #: Both default to empty, so a platform with an ordinary one-page form is
    #: unaffected. Best effort by design: a chooser that is already dismissed,
    #: or a step this tenant does not show, is not a failure.
    before_email_clicks: tuple[str, ...] = ()
    before_password_clicks: tuple[str, ...] = ()
    #: Overrides the built-in submit selectors. Plataforma A's login has no <form> and
    #: no button[type='submit'] -- the button is plain, with text -- so the
    #: defaults match nothing and the login fell back to pressing Enter.
    submit_selectors: tuple[str, ...] = ()
    #: How to put a session into a role, for a platform where one account holds
    #: several. Shape: {open: selector, choose: {role: label},
    #: lands_on: {role: url}}. Empty for a platform with one interface.
    role_switch: dict = field(default_factory=dict)


def _default_platforms_dir() -> Path:
    """Dónde viven las recetas de acceso de cada plataforma.

    Lee `LXP_PLATFORMS_DIR`, la MISMA variable que `config.py`. Antes resolvía
    la ruta contra su propio archivo y nada más, así que había dos fuentes de
    verdad para un solo directorio: quien apuntaba la variable a su carpeta
    -- la forma documentada de tener las configs de su plataforma FUERA del
    repo, que es como un proyecto abierto no publica datos de cliente -- la
    veía respetada en todos lados menos acá, y el login fallaba con "no conozco
    la plataforma" sobre una que el recorrido acababa de encontrar.

    El respaldo sigue siendo el directorio del paquete, que es adonde apunta
    también el default de `config.py` cuando el motor corre desde su raíz.
    """
    env = os.getenv("LXP_PLATFORMS_DIR")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[2] / "platforms"


def _str_list(
    block: dict, key: str, *, platform: str, path: Path, line: int
) -> tuple[str, ...]:
    value = block.get(key, [])
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list):
        raise EvalkitError(
            f'"{key}" de la plataforma {platform} debe ser una lista de textos',
            path=path,
            line=line,
            hint=f"(escribí {key}: [] si no hay ninguno)",
        )
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise EvalkitError(
                f'"{key}" de la plataforma {platform} tiene un elemento vacío '
                f"o que no es texto: {item!r}",
                path=path,
                line=line,
            )
    return tuple(value)


def load_platform_auth(platform: str, *, platforms_dir: Path | None = None) -> PlatformAuth:
    """Read the `auth:` block of `platforms/<platform>/config.yaml`.

    Raises EvalkitError with the standard file+line treatment when the platform
    has no config, or the config has no usable `auth:` block.
    """
    root = Path(platforms_dir) if platforms_dir is not None else _default_platforms_dir()
    path = root / platform / "config.yaml"
    if not path.exists():
        raise EvalkitError(
            f"no conozco la plataforma {platform!r}: falta {path}",
            hint=(
                "(cada plataforma necesita su platforms/<nombre>/config.yaml "
                "con un bloque auth:)"
            ),
        )

    data = load_yaml_with_lines(path, error_cls=EvalkitError)
    if not isinstance(data, dict):
        raise EvalkitError(
            f"el config de {platform} debe ser un mapa (clave: valor)", path=path, line=1
        )

    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise EvalkitError(
            f'al config de la plataforma {platform} le falta el bloque "auth"',
            path=path,
            line=line_of(data),
            hint=(
                "(necesita al menos login_url y uno de logged_out_selectors / "
                "authenticated_selectors)"
            ),
        )

    line = line_of(auth)
    login_url = auth.get("login_url")
    if not isinstance(login_url, str) or not login_url.strip():
        raise EvalkitError(
            f'"login_url" de la plataforma {platform} debe ser una URL entre comillas',
            path=path,
            line=line,
        )

    markers = _str_list(auth, "logged_out_url_markers", platform=platform, path=path, line=line)
    return PlatformAuth(
        platform=platform,
        login_url=login_url.strip(),
        logged_out_url_markers=markers or LOGGED_OUT_MARKERS,
        logged_out_selectors=_str_list(
            auth, "logged_out_selectors", platform=platform, path=path, line=line
        ),
        authenticated_selectors=_str_list(
            auth, "authenticated_selectors", platform=platform, path=path, line=line
        ),
        session_cookies=_str_list(
            auth, "session_cookies", platform=platform, path=path, line=line
        ),
        before_email_clicks=_str_list(
            auth, "before_email_clicks", platform=platform, path=path, line=line
        ),
        before_password_clicks=_str_list(
            auth, "before_password_clicks", platform=platform, path=path, line=line
        ),
        submit_selectors=_str_list(
            auth, "submit_selectors", platform=platform, path=path, line=line
        ),
        role_switch=auth.get("role_switch") or {},
    )


def known_platforms(*, platforms_dir: Path | None = None) -> list[str]:
    """Every platform that has a config.yaml, sorted. Discovery, not a constant."""
    root = Path(platforms_dir) if platforms_dir is not None else _default_platforms_dir()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if (d / "config.yaml").exists())


def login_url_for(platform: str, *, platforms_dir: Path | None = None) -> str:
    """Where this platform's own login form lives, per its config."""
    return load_platform_auth(platform, platforms_dir=platforms_dir).login_url


# ---------------------------------------------------------------------------
# The logged-out predicate that actually looks at the page
# ---------------------------------------------------------------------------


async def _selector_present(page: Any, selector: str) -> bool:
    """True if `selector` matches something on the page right now."""
    try:
        found = await page.query_selector(selector)
        return found is not None
    except Exception:  # noqa: BLE001 - a broken selector must not read as "logged in"
        logger.debug("no pude evaluar el selector %r", selector)
        return False


async def _any_present(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        if await _selector_present(page, selector):
            return True
    return False


async def is_logged_out_page(
    page: Any, auth: PlatformAuth, *, require_positive: bool = True
) -> bool:
    """Whether this PAGE (not just its URL) is a login wall.

    Three passes, cheapest first:

    1. the URL heuristic (free, but inert for a root-served login form);
    2. a logged-out selector is present (a password field over /dashboard/12);
    3. authenticated selectors are configured and NONE of them is present.

    With neither selector list configured this degrades to the old URL-only
    behaviour, which is why `load_platform_auth` pushes authors to set one.

    `require_positive` decides what an AMBIGUOUS page means -- one showing no
    login form and none of the authenticated markers either. The answer differs
    by moment, and conflating the two cost a whole run:

    * At LOGIN (the default, True) an ambiguous page is a FAILURE. This rule was
      earned: a wrong password once reported success because "the login form is
      gone" was accepted as proof, and an absence is equally what an error page
      or a half-loaded SPA looks like. Logging in demands positive proof.

    * DURING a run (False) an ambiguous page is fine. The authenticated markers
      are necessarily captured from one page -- on Plataforma A, the dashboard's
      `side-nav` and `user-profile` -- and a lesson is an immersive view that
      renders neither. Requiring them mid-run declares the session dead on
      exactly the pages a probe has to reach: the live run opened the course
      successfully and was killed at the next step, on a `type=LESSON` URL.

    Nothing is weakened by the distinction, because a genuinely dead session is
    not ambiguous: the platform redirects to `/login/?callbackUrl=...`, which
    pass 1 catches on the URL alone, and a session that dies behind a modal
    still renders the password field pass 2 catches.
    """
    url = getattr(page, "url", "") or ""
    if is_logged_out_url(url, auth.logged_out_url_markers):
        return True
    if await _any_present(page, auth.logged_out_selectors):
        return True
    if require_positive and auth.authenticated_selectors:
        return not await _any_present(page, auth.authenticated_selectors)
    return False


async def _has_session_cookie(context: Any, names: tuple[str, ...]) -> bool:
    if not names or context is None:
        return False
    try:
        cookies = await context.cookies()
    except Exception:  # noqa: BLE001
        logger.debug("no pude leer las cookies del contexto")
        return False
    present = {c.get("name") for c in cookies or [] if isinstance(c, dict)}
    return any(name in present for name in names)


class SessionStore:
    """A saved Playwright storage_state, per (platform, role)."""

    def __init__(self, platform: str, role: Role, *, directory: Path | None = None) -> None:
        if role not in ROLES:
            raise EvalkitError(
                f"rol desconocido: {role!r} — usá uno de: {', '.join(ROLES)}"
            )
        self.platform = platform
        self.role = role
        self.directory = Path(directory) if directory is not None else _default_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        # Owner-only, with inheritable grants so the state files created inside
        # stay openable -- see fileguard for why chmod alone does nothing here.
        restrict_to_user(self.directory, is_dir=True)
        self.path = self.directory / f"storage_state_{platform}_{role}.json"

    def exists(self) -> bool:
        return self.path.exists()

    def save(self, state: dict[str, Any]) -> Path:
        # Atomic + owner-only: this file is a bag of live session cookies.
        write_private_text(
            self.path, json.dumps(state, ensure_ascii=False, indent=2)
        )
        logger.info("sesión guardada: %s", self.path.name)
        return self.path

    def load(self) -> dict[str, Any]:
        if not self.exists():
            raise SessionExpired(
                f"no hay sesión guardada para {self.platform}/{self.role} "
                f"({self.path.name}) — hay que iniciar sesión primero"
            )
        return json.loads(self.path.read_text(encoding="utf-8"))


def _default_dir() -> Path:
    # 06_Application/runs/sessions — gitignored (runs/ is in .gitignore)
    return Path(__file__).resolve().parents[2] / "runs" / "sessions"


async def _fill_first(page: Any, selectors: tuple[str, ...], value: str) -> bool:
    """Fill the first selector that works. Never logs `value`."""
    for selector in selectors:
        try:
            await page.fill(selector, value)
            return True
        except Exception:
            continue
    return False


async def _click_first(page: Any, selectors: tuple[str, ...]) -> bool:
    for selector in selectors:
        try:
            await page.click(selector)
            return True
        except Exception:
            continue
    return False


async def auto_login(
    platform: str,
    role: Role,
    page: Any,
    *,
    credentials: Credentials | None = None,
    login_url: str | None = None,
    env: dict[str, str] | None = None,
    settle: Callable[[], Awaitable[None]] | None = None,
    auth: PlatformAuth | None = None,
    context: Any | None = None,
    platforms_dir: Path | None = None,
) -> bool:
    """Fill the platform's own login form. True ONLY if we can PROVE we are in.

    Proof means a positive signal -- an authenticated-only selector from the
    platform config, or a session cookie -- never merely "the URL does not look
    like a login page". That inverted test is what reported a wrong password as
    a successful login on PLATAFORMA_B, whose form is served at the site root and so can
    never match a URL marker.

    Returns False (rather than raising) when credentials are absent, the form
    cannot be found, or authentication cannot be proven -- the caller falls back
    to interactive_login. No log line or return value ever contains the password.
    """
    creds = credentials or credentials_from_env(platform, role, env=env)
    if creds is None:
        logger.info("sin credenciales para %s/%s: salteo el login automático", platform, role)
        return False

    if auth is None:
        try:
            auth = load_platform_auth(platform, platforms_dir=platforms_dir)
        except EvalkitError as e:
            logger.info("no puedo hacer login automático en %s: %s", platform, e.text)
            return False

    url = login_url or auth.login_url
    if not url:
        logger.info("no conozco la URL de login de %s", platform)
        return False

    logger.info("login automático en %s como %s (%s)", platform, role, creds.email)
    await page.goto(url)

    # Reveal the form, for a login that arrives one field at a time. Best
    # effort: a step this tenant does not show must not abort a login that
    # would otherwise work.
    for selector in auth.before_email_clicks:
        if not await _click_first(page, (selector,)):
            logger.info("no estaba %s en el login de %s, sigo", selector, platform)

    if not await _fill_first(page, _EMAIL_SELECTORS, creds.email):
        logger.warning("no encontré el campo de email en %s", platform)
        return False

    for selector in auth.before_password_clicks:
        if not await _click_first(page, (selector,)):
            logger.info("no estaba %s en el login de %s, sigo", selector, platform)

    if not await _fill_first(page, _PASSWORD_SELECTORS, creds.password):
        logger.warning("no encontré el campo de contraseña en %s", platform)
        return False

    if not await _click_first(page, auth.submit_selectors or _SUBMIT_SELECTORS):
        try:
            await page.press(_PASSWORD_SELECTORS[0], "Enter")
        except Exception:
            logger.warning("no pude enviar el formulario de login de %s", platform)
            return False

    if settle is not None:
        await settle()

    if not await _confirm_authenticated(platform, role, page, auth, context):
        return False

    # THE ROLE, before anyone saves this session. One account can hold two
    # roles and the role travels INSIDE the session token, so switching it
    # after the storage state is captured changes nothing for the run.
    return await switch_role(platform, role, page, auth, settle=settle)


async def switch_role(
    platform: str,
    role: Role,
    page: Any,
    auth: PlatformAuth,
    *,
    settle: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Put the freshly-authenticated session into the role we asked for.

    UNA cuenta, DOS roles. Plataforma A's role does not live on the account: it travels
    inside the session token, and /login/ always lands on the TEACHER view even
    when the same account is being used to evaluate the student experience.

    Without this the engine walks the wrong interface and every student-tutor
    criterion (PT-D9-*, PT-D8-*) comes back NO_EVALUADO -- the report saying
    "no se pudo llegar al tutor" while the tutor was one click away. The
    platform config has described this switch since 2026-07-19 and no code ever
    read it, which is the same class of bug as a gate that is configured and
    inert.

    Returns True when there is nothing to do: a platform with no `role_switch`
    block is a platform with one interface, and that is most of them.
    """
    switch = auth.role_switch or {}
    lands_on = (switch.get("lands_on") or {}).get(role, "")
    if not switch or not lands_on:
        return True

    # Already there: restoring a saved session usually lands in the right role.
    if lands_on and lands_on.rstrip("/") in (getattr(page, "url", "") or "").rstrip("/"):
        return True

    label = (switch.get("choose") or {}).get(role, "")
    opener = switch.get("open", "")
    if not label or not opener:
        logger.warning(
            "el role_switch de %s no dice cómo elegir el rol %s", platform, role
        )
        return False

    if not await _click_first(page, (opener,)):
        logger.warning("no pude abrir el menú de rol de %s (%s)", platform, opener)
        return False
    # Narrowest first. A bare `text=Student` matched TWICE in Plataforma A's menu and
    # Playwright's strict mode refuses an ambiguous selector, so the switch
    # failed on a menu that was open and correct. The nth=0 fallback is what
    # makes an unavoidable ambiguity clickable instead of fatal.
    candidates = (
        f"[role='menuitem']:has-text('{label}')",
        f"button:has-text('{label}')",
        f"text={label} >> nth=0",
        label,
    )
    if not await _click_first(page, candidates):
        logger.warning("no encontré el rol %s en el menú de %s", label, platform)
        return False
    if settle is not None:
        await settle()

    url = (getattr(page, "url", "") or "").rstrip("/")
    if lands_on.rstrip("/") not in url:
        logger.warning(
            "cambié al rol %s en %s pero la página quedó en %s, no en %s",
            role, platform, url or "(sin url)", lands_on,
        )
        return False
    logger.info("sesión de %s puesta en el rol %s", platform, role)
    return True


async def _confirm_authenticated(
    platform: str, role: Role, page: Any, auth: PlatformAuth, context: Any | None
) -> bool:
    """Positive proof that the login worked. Fails CLOSED when unprovable."""
    # A login page still on screen settles it immediately, whatever else says.
    if is_logged_out_url(getattr(page, "url", "") or "", auth.logged_out_url_markers):
        logger.warning(
            "login automático de %s/%s no prosperó (seguimos en una URL de login)",
            platform, role,
        )
        return False

    if auth.authenticated_selectors:
        if await _any_present(page, auth.authenticated_selectors):
            return True
        logger.warning(
            "login automático de %s/%s no prosperó (no aparece ninguna marca de "
            "sesión iniciada en la página)", platform, role,
        )
        return False

    if auth.session_cookies:
        if await _has_session_cookie(context, auth.session_cookies):
            return True
        logger.warning(
            "login automático de %s/%s no prosperó (no se emitió la cookie de sesión)",
            platform, role,
        )
        return False

    if auth.logged_out_selectors:
        # Weaker, but still DOM-grounded: a rejected password leaves the form up.
        if await _any_present(page, auth.logged_out_selectors):
            logger.warning(
                "login automático de %s/%s no prosperó (el formulario de login "
                "sigue en pantalla: contraseña rechazada)", platform, role,
            )
            return False
        logger.warning(
            "login de %s/%s confirmado sólo por AUSENCIA del formulario - agregá "
            "authenticated_selectors al config de la plataforma para una prueba "
            "positiva", platform, role,
        )
        return True

    logger.error(
        "no puedo confirmar el login de %s/%s: el config de la plataforma no "
        "define authenticated_selectors, session_cookies ni logged_out_selectors",
        platform, role,
    )
    return False


async def interactive_login(
    url: str,
    store: SessionStore,
    *,
    page: Any,
    context: Any,
    timeout_s: float = 300,
    poll_s: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], float] | None = None,
) -> Path:
    """Headful fallback: a human logs in once, then we save the storage state.

    Polls until the page is no longer on a login/SSO URL. Raises SessionExpired
    on timeout rather than saving a half-authenticated state.
    """
    sleep = sleep or asyncio.sleep
    now = now or time.monotonic

    print()
    print("=" * 72)
    print(f"INICIÁ SESIÓN A MANO en la ventana del navegador ({store.platform}/{store.role}).")
    print(f"Abriendo: {url}")
    print(f"Esperando hasta {int(timeout_s)}s. La sesión se guarda sola al entrar.")
    print("=" * 72)

    await page.goto(url)
    deadline = now() + timeout_s

    while now() < deadline:
        if not is_logged_out_url(page.url):
            state = await context.storage_state()
            saved = store.save(state)
            print(f"Sesión detectada y guardada: {saved.name}")
            return saved
        await sleep(poll_s)

    raise SessionExpired(
        f"no se completó el login de {store.platform}/{store.role} en {int(timeout_s)}s "
        f"(la página sigue en una URL de login)"
    )


def _store_is_fresh(store: SessionStore, rejected: bytes | None) -> bool:
    """True if the store holds a state we have NOT already judged dead."""
    if not store.exists():
        return False
    if rejected is None:
        return True
    try:
        return store.path.read_bytes() != rejected
    except OSError:
        return False


async def ensure_session(
    platform: str,
    role: Role,
    *,
    url: str,
    store: SessionStore | None = None,
    verify_alive: Callable[[SessionStore], Awaitable[bool]] | None = None,
    auto_login_fn: Callable[[], Awaitable[bool]] | None = None,
    save_state_fn: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    interactive_login_fn: Callable[[], Awaitable[Path]] | None = None,
) -> Path:
    """Get a usable storage_state path, cheapest path first.

    1. A saved session that verifies alive is reused as-is (no login at all).
    2. Otherwise try the automatic login (no human) and PERSIST what it got.
    3. Otherwise fall back to the interactive login (human, once).
    4. If nothing worked, raise rather than run against a logged-out page.

    `save_state_fn` is how step 2 persists: auto_login authenticates the live
    browser context, but without saving the storage state `store.exists()` stays
    False on a clean machine and the run drops into the 300-second human prompt
    it just avoided.
    """
    store = store or SessionStore(platform, role)

    # Fingerprint of a file we already REJECTED as dead, so a later branch
    # cannot hand the same dead path back just because it still exists.
    rejected: bytes | None = None

    if store.exists():
        if verify_alive is None or await verify_alive(store):
            logger.info("reuso la sesión guardada de %s/%s", platform, role)
            return store.path
        logger.info("la sesión guardada de %s/%s ya no sirve", platform, role)
        try:
            rejected = store.path.read_bytes()
        except OSError:
            rejected = None

    if auto_login_fn is not None and await auto_login_fn():
        logger.info("login automático de %s/%s OK", platform, role)
        if save_state_fn is not None:
            state = await save_state_fn()
            if isinstance(state, dict):
                store.save(state)
            else:
                logger.warning(
                    "save_state_fn de %s/%s no devolvió un storage_state utilizable",
                    platform, role,
                )
        if _store_is_fresh(store, rejected):
            return store.path
        logger.warning(
            "el login automático de %s/%s dijo que sí pero no quedó ninguna "
            "sesión nueva guardada: sigo con el login manual", platform, role,
        )

    if interactive_login_fn is not None:
        await interactive_login_fn()
        if _store_is_fresh(store, rejected):
            return store.path

    raise SessionExpired(
        f"no se pudo establecer una sesión para {platform}/{role}: "
        "ni la guardada sirve, ni el login automático, ni el manual"
    )
