"""A shared service token. The only legitimate caller is the Next.js app.

Proportionality first: this service has exactly one client, and that client is
another piece of our own infrastructure. A user system here would be inventing
identities nobody has. What is actually needed is that a stranger who reaches
the container cannot read every job -- objectives, criteria, evidence paths,
whatever a ministry is evaluating -- or start paid runs against a vendor's
platform with our test account.

So: one bearer token, from the environment, on every endpoint but `/healthz`.

Three properties this file exists to hold:

* CONSTANT TIME. `hmac.compare_digest`, never `==`. A byte-by-byte comparison
  that returns early leaks the token's prefix to anyone willing to measure, and
  the token is the whole security boundary;
* FAIL CLOSED. If `EVALKIT_SERVICE_TOKEN` is unset, the service refuses to
  build. A missing secret is a misconfiguration, and the safe reading of a
  misconfiguration is "no" -- the alternative is a deploy that silently ships
  an open service and looks perfectly healthy;
* SILENCE. The token is never logged, never echoed in an error, never returned
  by `/healthz`. `/healthz` says WHETHER auth is configured, which is what an
  operator needs, and nothing about what the value is.
"""
from __future__ import annotations

import hmac
import logging
import os

from .crypto import ServiceError

logger = logging.getLogger(__name__)

#: Where the token is read from. Set the same value in the web app's env.
TOKEN_ENV = "EVALKIT_SERVICE_TOKEN"

#: The header, and the scheme inside it: `Authorization: Bearer <token>`.
HEADER = "authorization"
SCHEME = "Bearer"

#: Short enough to type, long enough not to be guessed. A 16-char minimum
#: rejects the "changeme" class of value at boot rather than at breach.
MIN_TOKEN_LENGTH = 16

#: `/healthz` is open: it exposes a boolean and a job count, and a liveness
#: probe that needs a secret is a liveness probe that fails during a rotation.
OPEN_PATHS = frozenset({"/healthz"})


def load_token(env: dict[str, str] | None = None) -> str:
    """The token, or refuse. Never returns an empty or obviously-placeholder value.

    The ENVIRONMENT only -- `.env` is deliberately not consulted. This is a
    deployment secret, not engine config, and a file that can quietly satisfy
    it destroys the fail-closed guarantee: "unset" has to mean unset, or the
    property is neither true nor testable. See `environ.py` for the full
    reasoning.
    """
    from .environ import deployment_environ

    env = deployment_environ(env)
    raw = (env.get(TOKEN_ENV) or "").strip()
    if not raw:
        raise ServiceError(
            f"falta la variable de entorno {TOKEN_ENV}: sin token compartido "
            "cualquiera que alcance el contenedor podría leer todos los trabajos "
            "y lanzar corridas pagas, así que el servicio no arranca",
            hint=(
                "(generá uno con: python -c \"import secrets; "
                'print(secrets.token_urlsafe(32))" y ponelo también en el '
                "entorno de la app web)"
            ),
        )
    if len(raw) < MIN_TOKEN_LENGTH:
        raise ServiceError(
            f"el token de {TOKEN_ENV} es demasiado corto "
            f"({len(raw)} caracteres, mínimo {MIN_TOKEN_LENGTH}): un token "
            "adivinable es lo mismo que no tener token",
            hint=(
                "(generá uno con: python -c \"import secrets; "
                'print(secrets.token_urlsafe(32))")'
            ),
        )
    return raw


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison. NEVER `==`.

    `compare_digest` on the encoded bytes: it does not return early on the first
    differing byte, so the time it takes says nothing about how much of the
    token the caller got right.
    """
    if not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))


def token_from_header(value: str | None) -> str | None:
    """Pull the token out of `Authorization: Bearer <token>`.

    Case-insensitive on the scheme only. Anything else -- Basic, a bare token
    with no scheme, a second word -- is not this protocol and gets nothing.
    """
    if not value:
        return None
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != SCHEME.lower():
        return None
    return parts[1]


#: What an unauthenticated caller is told. Deliberately identical for "no
#: header", "wrong scheme" and "wrong token": which of the three it was is
#: information the caller has not earned, and it is the same fix either way.
DENIED = (
    "no autorizado: este servicio sólo atiende a la aplicación web, que se "
    f"identifica con la cabecera '{SCHEME} <token>' en Authorization"
)
