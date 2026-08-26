"""Credential encryption at rest.

The service is handed a test account's username and password so it can log a
browser in. Test accounts are still accounts: a password in a job record is a
password in a backup, in a log aggregator, and in whatever the container's disk
gets snapshotted into.

So the rule here is narrow and absolute:

* the plaintext lives in memory only, between the request that carries it and
  the moment the driver logs in;
* on disk it is Fernet (AES-128-CBC + HMAC-SHA256) ciphertext in its own file,
  never a field of the job record the API serialises;
* the key comes from `EVALKIT_SECRET_KEY` in the environment. Never the repo,
  never a default, never derived from something guessable. A service without
  the key refuses to start rather than falling back to plaintext.

There is deliberately NO masked form ("clave-****-jamas"). A masked-and-
recoverable value is a leak with a decoration on it, and a masked-and-
irrecoverable one is just a lie about what we hold.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from ..evalkit.errors import EvalkitError
from ..evalkit.session import Credentials

#: Where the key is read from. Set it in the container's secret store.
KEY_ENV = "EVALKIT_SECRET_KEY"


class ServiceError(EvalkitError):
    """An author/operator-facing service error. Spanish, like the rest."""


def generate_key() -> str:
    """A fresh key, for `EVALKIT_SECRET_KEY`. Print it once, store it, forget it."""
    return Fernet.generate_key().decode("ascii")


@dataclass(frozen=True)
class SecretBox:
    """Encrypt/decrypt credentials. Holds the key; never logs or renders it."""

    _fernet: Fernet

    def __repr__(self) -> str:  # a repr of the box must not carry the key
        return "SecretBox(key=***)"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "SecretBox":
        # The ENVIRONMENT only -- `.env` is deliberately not consulted. A stale
        # key in a checked-out/shipped `.env` is the quiet failure: the service
        # starts, encrypts new credentials happily, and fails only when it
        # tries to DECRYPT ones written under the real key -- surfacing as a
        # confusing login failure rather than a config error. See `environ.py`.
        from .environ import deployment_environ

        env = deployment_environ(env)
        raw = (env.get(KEY_ENV) or "").strip()
        if not raw:
            raise ServiceError(
                f"falta la variable de entorno {KEY_ENV}: sin clave no se pueden "
                "guardar credenciales cifradas, y guardarlas en claro no es una opción",
                hint=(
                    "(generá una con: python -c \"from lxp_eval.service.crypto import "
                    'generate_key; print(generate_key())")'
                ),
            )
        try:
            fernet = Fernet(raw.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise ServiceError(
                f"la clave de {KEY_ENV} no tiene el formato esperado "
                "(debe ser una clave Fernet en base64 de 32 bytes)"
            ) from exc
        return cls(_fernet=fernet)

    # --- generic secrets ---------------------------------------------------
    #
    # The user's OWN Anthropic key is a secret of the same class as a test
    # account's password -- it bills them, and it is pasted into the app rather
    # than injected by an operator -- so it gets the SAME box and the same key
    # rather than a second crypto path nobody would remember to review. What it
    # does not get is `Credentials`, whose shape is email+password.

    def encrypt_text(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt_text(self, blob: bytes) -> str:
        """Back to plaintext. Raises `InvalidToken` under a rotated key.

        Deliberately NOT wrapped in a ServiceError here: what a failed decrypt
        means depends on what was encrypted. A credential that cannot be read
        is a job that must stop; a stored setting that cannot be read is a
        setting the user can simply paste again. The caller decides.
        """
        return self._fernet.decrypt(bytes(blob)).decode("utf-8")

    def encrypt_credentials(self, email: str, password: str) -> bytes:
        payload = json.dumps(
            {"email": email, "password": password}, ensure_ascii=False
        ).encode("utf-8")
        return self._fernet.encrypt(payload)

    def decrypt_credentials(self, blob: bytes) -> Credentials:
        """Back to plaintext, at the moment of login and nowhere else.

        Returns evalkit's `Credentials`, whose __repr__ already prints the
        password as '***' -- so an accidental log of the object is not a leak.
        """
        try:
            payload = self._fernet.decrypt(bytes(blob))
        except InvalidToken as exc:
            raise ServiceError(
                "no se pudieron descifrar las credenciales guardadas: la clave "
                f"de {KEY_ENV} no es la que se usó para cifrarlas, o el archivo "
                "está corrupto"
            ) from exc
        data = json.loads(payload.decode("utf-8"))
        return Credentials(email=data["email"], password=data["password"])
