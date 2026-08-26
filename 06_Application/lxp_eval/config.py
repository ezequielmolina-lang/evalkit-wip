"""Configuration loaded from environment + defaults.

WHERE THE ANTHROPIC KEY COMES FROM, in order:

    1. the ANTHROPIC_API_KEY environment variable (incl. `.env`, loaded below)
    2. what the user saved in the app  (see `service/settings.py`)
    3. nothing -- `validate()` says so, in Spanish, naming both ways to fix it

The environment wins deliberately. An operator running headless -- CI, a cron
replication, `examples/*.py` -- has no UI to paste into, and a key exported for
one run must not be silently overridden by something a browser stored weeks
ago. The stored key is the FALLBACK, which is the case that matters for a
stranger who just cloned this: they have no `.env` and no intention of writing
one.

DEPENDENCY DIRECTION. `lxp_eval/service/*` already imports this module
(`service/jobs.py` builds its Anthropic client from `CONFIG`), so importing the
service back from here would be a cycle. It would also be the wrong shape: the
engine has no business knowing an HTTP service exists. So the service INJECTS
-- `create_app` calls `set_stored_key_provider(settings_store.get_api_key)` at
startup -- and this module keeps ownership of the ORDER, which is the part that
has to be documented in exactly one place.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# override=True so the .env file wins over any stale/empty env var set at user/system scope
load_dotenv(override=True)


#: Installed by the service layer at startup; called with no arguments, returns
#: the user's stored key or None. Stays None for every non-service entrypoint
#: (the CLI examples, the smoke test), which is exactly the pre-existing
#: behaviour: environment or nothing.
_stored_key_provider: Callable[[], str | None] | None = None


def set_stored_key_provider(provider: Callable[[], str | None] | None) -> None:
    """Register where a user-saved key can be read from. Pass None to unregister.

    A CALLABLE, not a value: the user can paste a key while the process is
    running, and a snapshot taken at startup would mean "save the key, then
    restart the container", which is the friction this whole feature removes.
    """
    global _stored_key_provider
    _stored_key_provider = provider


def stored_api_key() -> str:
    """The user-saved key, or "". Never raises."""
    provider = _stored_key_provider
    if provider is None:
        return ""
    try:
        return (provider() or "").strip()
    except Exception as exc:  # noqa: BLE001
        # An unreadable settings file must not take down a run that has a
        # perfectly good key in the environment, nor turn `validate()` into a
        # crash. It is reported and treated as "no stored key".
        logger.warning("no se pudo leer la clave de API guardada: %s", exc)
        return ""


@dataclass
class Config:
    # API
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # Models — `claude-opus-4-8` is the current Opus-tier id (the prior
    # `claude-opus-4-7-20260101` default was a stale, date-suffixed placeholder
    # that would 404).
    model_heavy: str = field(default_factory=lambda: os.getenv("LXP_MODEL_HEAVY", "claude-opus-4-8"))
    model_light: str = field(default_factory=lambda: os.getenv("LXP_MODEL_LIGHT", "claude-opus-4-8"))

    # Behaviour
    dry_run: bool = field(default_factory=lambda: os.getenv("LXP_DRY_RUN", "false").lower() == "true")
    max_parallel_agents: int = field(default_factory=lambda: int(os.getenv("LXP_MAX_PARALLEL_AGENTS", "4")))
    log_level: str = field(default_factory=lambda: os.getenv("LXP_LOG_LEVEL", "INFO"))

    # Paths
    rubric_xlsx: Path = field(default_factory=lambda: Path(os.getenv(
        "LXP_RUBRIC_XLSX", "../00_Master_Documents/Logging_Rubrica_LXP_ElPais.xlsx"
    )).resolve())
    platforms_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "LXP_PLATFORMS_DIR", "./platforms"
    )).resolve())
    outputs_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "LXP_OUTPUTS_DIR", "./outputs"
    )).resolve())

    # API call limits (defensive)
    max_tokens_per_call: int = 8192
    max_retries: int = 2

    def resolve_api_key(self) -> str:
        """The key to call Anthropic with. See the module docstring for the order.

        Read LIVE rather than off `anthropic_api_key`, which is captured once at
        import: a user who pastes their key into the running app must not have
        to restart the container for it to take effect. `anthropic_api_key`
        stays as it was for the older entrypoints that read it directly.
        """
        from_env = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if from_env:
            return from_env
        return stored_api_key()

    def validate(self) -> list[str]:
        """Return list of validation errors (empty if OK)."""
        errors = []
        if not self.dry_run and not self.resolve_api_key():
            # NOT "set ANTHROPIC_API_KEY". Whoever reads this now is as likely
            # to be someone who cloned the repo as the person who wrote it, and
            # for them editing a dotfile is not a step, it is a dead end. The
            # app is named first; the variable stays named for headless use.
            errors.append(
                "falta la clave de API de Anthropic: cargala en la aplicación "
                "(Configuración -> Clave de API), o definí la variable de "
                "entorno ANTHROPIC_API_KEY si corrés sin interfaz "
                "(o LXP_DRY_RUN=true para no llamar a la API)"
            )
        if not self.rubric_xlsx.exists():
            errors.append(f"Rubric XLSX not found: {self.rubric_xlsx}")
        if not self.platforms_dir.exists():
            errors.append(f"Platforms dir not found: {self.platforms_dir}")
        return errors


# Singleton
CONFIG = Config()
