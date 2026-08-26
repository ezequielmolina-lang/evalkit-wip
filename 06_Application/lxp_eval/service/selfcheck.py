"""`python -m lxp_eval.service.selfcheck` -- is this machine able to run a job?

There is one piece of this service that has never executed: the driver factory
that launches a real Chromium and logs a real (test) account into a vendor's
platform. It cannot be tested here -- no credentials, and driving a vendor's
platform to satisfy a test suite is not something to do casually -- so the next
best thing is to make its first live failure cheap to diagnose.

That is what this is. It checks the things that fail on a FRESH MACHINE, in the
order they would bite, and prints one line per item saying which. Everything it
checks is local:

    * `EVALKIT_SECRET_KEY`      set, and a valid Fernet key
    * `EVALKIT_SERVICE_TOKEN`   set, and long enough to mean anything
    * `ANTHROPIC_API_KEY`       present  (PRESENT -- never validated)
    * the job directory         exists and is actually writable
    * the criteria bank         parses
    * Chromium                  installed, and launches

Two things it deliberately does NOT do:

* CALL THE ANTHROPIC API. A preflight that costs money and can fail for reasons
  unrelated to this machine is not a preflight. "The key is present and
  well-formed" is what a fresh machine gets wrong; "the key is valid" is what
  the first job discovers, quickly and with a clear error;
* OPEN A VENDOR URL. Chromium is launched against nothing at all. Whether the
  platform accepts our login is a question for a run a human authorised, not
  for a diagnostic somebody ran out of curiosity.

Exit code is 0 if every check passed, 1 otherwise, so it can gate a deploy.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

OK = "OK"
FAIL = "FALLA"
WARN = "AVISO"


@dataclass(frozen=True)
class Check:
    """One verifiable fact about this machine."""

    name: str
    status: str
    detail: str = ""
    #: What to actually DO about it. A diagnostic that says only "falla" has
    #: moved the mystery, not solved it.
    fix: str = ""

    @property
    def passed(self) -> bool:
        return self.status != FAIL

    def render(self) -> str:
        line = f"[{self.status:5}] {self.name}"
        if self.detail:
            line += f"\n        {self.detail}"
        if self.status == FAIL and self.fix:
            line += f"\n        -> {self.fix}"
        return line


# --- the individual checks --------------------------------------------------


def check_secret_key(env: dict[str, str]) -> Check:
    from .crypto import KEY_ENV, SecretBox, ServiceError

    name = f"clave de cifrado ({KEY_ENV})"
    try:
        SecretBox.from_env(env)
    except ServiceError as exc:
        return Check(
            name, FAIL, str(exc),
            fix=(
                'generala con: python -c "from lxp_eval.service.crypto import '
                'generate_key; print(generate_key())" y EXPORTALA en el entorno. '
                "Ojo: esta clave no se lee de .env a propósito (una clave vieja "
                "en un .env arranca el servicio y después no puede descifrar las "
                "credenciales guardadas con la clave buena)"
            ),
        )
    return Check(name, OK, "presente en el entorno y con formato Fernet válido")


def check_service_token(env: dict[str, str]) -> Check:
    from .auth import TOKEN_ENV, load_token
    from .crypto import ServiceError

    name = f"token de servicio ({TOKEN_ENV})"
    try:
        token = load_token(env)
    except ServiceError as exc:
        return Check(
            name, FAIL, str(exc),
            fix=(
                'generalo con: python -c "import secrets; '
                'print(secrets.token_urlsafe(32))", EXPORTALO en el entorno y '
                "poné EL MISMO valor en el entorno de la app web. Ojo: este "
                "token no se lee de .env a propósito, para que 'sin token' "
                "signifique sin token y el servicio no arranque"
            ),
        )
    # The LENGTH, never the value. This output goes into deploy logs.
    return Check(name, OK, f"presente en el entorno ({len(token)} caracteres)")


def check_anthropic_key(env: dict[str, str]) -> Check:
    """PRESENT, not valid. This check never calls the API.

    Reads the ENVIRONMENT only, and deliberately does not consult the key a
    user may have saved in the app (`service/settings.py`): this runs as a
    standalone command, without a service, so it has no state directory to look
    in and no `EVALKIT_SECRET_KEY` guarantee to decrypt with. What it can do is
    stop sending a stranger to a dotfile as if it were the only option -- so the
    FIX names the app first. A FAIL here therefore means "not configured for
    HEADLESS use", which is what this command is for.
    """
    name = "clave de Anthropic (ANTHROPIC_API_KEY)"
    raw = (env.get("ANTHROPIC_API_KEY") or "").strip()
    if not raw:
        return Check(
            name, FAIL,
            "no está definida ni en el entorno ni en 06_Application/.env: sin "
            "ella no se puede construir ningún marco ni generar ninguna prueba",
            fix=(
                "cargala en la aplicación (Configuración -> Clave de API), que "
                "la guarda cifrada y no necesita reiniciar nada; o, si corrés "
                "sin interfaz, poné ANTHROPIC_API_KEY en 06_Application/.env o "
                "exportala en el entorno"
            ),
        )
    if not raw.startswith("sk-"):
        return Check(
            name, WARN,
            "está definida pero no empieza con 'sk-'. Puede ser válida; sólo no "
            "tiene la forma habitual. Esta verificación NO llama a la API.",
        )
    return Check(
        name, OK,
        "presente (no se valida contra la API: eso costaría dinero y no es una "
        "propiedad de esta máquina)",
    )


def check_dotenv_hygiene(names: list[str] | None = None) -> Check:
    """Are deployment secrets sitting in `.env`, where they are not read?

    A WARNING rather than a failure: the service works, and nothing here reads
    them. But it is genuinely ambiguous, and worth saying out loud, because
    `lxp_eval/config.py` calls `load_dotenv(override=True)` at import -- so the
    moment a job imports `CONFIG` those values DO appear in `os.environ`. Which
    key the service ends up using would then depend on import order, and a
    stale Fernet key that wins that race fails at DECRYPT time, one job at a
    time, looking like a login problem rather than a config error.
    """
    name = "higiene de .env"
    if names is None:
        from .environ import deployment_vars_in_dotenv

        names = deployment_vars_in_dotenv()
    if not names:
        return Check(name, OK, "ningún secreto de despliegue está en .env")
    return Check(
        name, WARN,
        f"{', '.join(names)} está(n) en .env, donde este servicio NO los lee. "
        "Si además los exportás en el entorno, manda el entorno y no pasa nada; "
        "si NO los exportás, el servicio no arranca aunque el archivo los tenga. "
        "Peor: config.py hace load_dotenv(override=True) al importarse, así que "
        "una clave vieja del archivo puede terminar en el proceso igual.",
        fix="sacalos de .env y exportalos en el entorno",
    )


def check_job_dir(root: Path) -> Check:
    """Exists AND writable. `os.access` lies on enough systems to be useless."""
    name = f"directorio de trabajos ({root})"
    probe = root / ".selfcheck.tmp"
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(
            name, FAIL,
            f"no se puede escribir: {exc.__class__.__name__}: {exc}",
            fix=(
                "montá un volumen con permiso de escritura en esa ruta, o "
                "cambiá el directorio raíz del servicio"
            ),
        )
    return Check(name, OK, "existe y se puede escribir")


def check_bank() -> Check:
    name = "banco de criterios"
    try:
        from ..evalkit.framework import load_bank

        bank = load_bank()
    except Exception as exc:  # noqa: BLE001 -- any parse failure is the answer
        return Check(
            name, FAIL,
            f"no se pudo cargar: {exc.__class__.__name__}: {exc}",
            fix=(
                "el archivo YAML del banco está corrupto o falta; revisá "
                "lxp_eval/evalkit/banks/"
            ),
        )
    entries = len(getattr(bank, "entries", ()) or ())
    return Check(name, OK, f"{getattr(bank, 'bank_id', '?')} cargado, {entries} criterios")


def _launch_chromium() -> str:
    """Launch and immediately close a headless Chromium. Opens NO page.

    Separated so the tests can substitute it: this is the one check that starts
    a real process, and a test suite must not.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            return browser.version
        finally:
            browser.close()


def check_chromium(launcher: Callable[[], str] | None = None) -> Check:
    """The check that matters most, because it is the one nothing else covers.

    Separates "Playwright is not installed" from "Playwright is installed but
    the browser binary is missing" from "the binary is there but the container
    lacks its system libraries" -- three different fixes that produce three
    almost identical stack traces.
    """
    name = "Chromium (Playwright)"
    launcher = launcher or _launch_chromium
    try:
        import playwright  # noqa: F401
    except ImportError:
        return Check(
            name, FAIL,
            "Playwright no está instalado, así que no hay navegador con el que "
            "recorrer ninguna plataforma",
            fix="pip install -r requirements-service.txt",
        )
    try:
        version = launcher()
    except Exception as exc:  # noqa: BLE001
        text = f"{exc.__class__.__name__}: {exc}"
        missing_binary = "executable doesn" in text.lower() or "install" in text.lower()
        return Check(
            name, FAIL,
            f"Playwright está instalado pero Chromium no arrancó: {text}",
            fix=(
                "python -m playwright install chromium"
                if missing_binary
                else "python -m playwright install chromium --with-deps  "
                     "(faltan librerías del sistema en este contenedor)"
            ),
        )
    return Check(name, OK, f"arranca correctamente ({version})")


# --- the runner -------------------------------------------------------------


def run_checks(
    env: dict[str, str] | None = None,
    root: Path | str | None = None,
    *,
    launcher: Callable[[], str] | None = None,
) -> list[Check]:
    """Every check, in the order a fresh machine fails them.

    Each variable is resolved FROM THE SOURCE THE SERVICE WILL USE, which is
    not the same source for all of them: `ANTHROPIC_API_KEY` includes `.env`
    (engine config), the two `EVALKIT_*` secrets do not (deployment secrets --
    see `environ.py`). Getting this wrong in either direction produces a
    diagnostic that disagrees with the running service, which is worse than no
    diagnostic: this tool reported `[FALLA]` for an Anthropic key the service
    would have had, and as a deploy gate it blocked a healthy deploy.

    An explicit `env` overrides both sources, which is what the tests pass.
    """
    from .environ import deployment_environ, engine_environ

    engine_env = engine_environ(env)
    deploy_env = deployment_environ(env)
    if root is None:
        from .app import DEFAULT_ROOT

        root = DEFAULT_ROOT
    return [
        check_secret_key(deploy_env),
        check_service_token(deploy_env),
        check_anthropic_key(engine_env),
        check_dotenv_hygiene(None if env is None else []),
        check_job_dir(Path(root)),
        check_bank(),
        check_chromium(launcher),
    ]


def render(checks: list[Check]) -> str:
    failed = [c for c in checks if not c.passed]
    lines = [
        "Verificación del entorno del servicio evalkit",
        "=" * 46,
        "",
        *[c.render() for c in checks],
        "",
    ]
    if failed:
        lines.append(
            f"{len(failed)} verificación(es) fallaron. El servicio NO está listo "
            "para una corrida en vivo."
        )
    else:
        lines.append(
            "Todo en orden. Esto NO prueba que la plataforma acepte las "
            "credenciales de la cuenta de prueba: eso sólo se sabe en la primera "
            "corrida real, que un humano tiene que aprobar."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # A diagnostic that dies on its own accents is worse than no diagnostic.
    # Windows consoles still default to cp1252, and every message here is
    # Spanish; the traceback that would otherwise replace the output is
    # exactly the mystery this tool exists to prevent.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # pragma: no cover -- exotic stdout
        pass
    checks = run_checks()
    print(render(checks))
    return 0 if all(c.passed for c in checks) else 1


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    sys.exit(main())
