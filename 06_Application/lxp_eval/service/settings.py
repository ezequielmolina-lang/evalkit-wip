"""User settings, encrypted at rest. Today that means one thing: their key.

`lxp_eval/config.py` read `ANTHROPIC_API_KEY` from the environment and refused
to validate without it. That is a perfectly good arrangement for the person who
wrote the repo and an impossible one for everybody else: the project is going
open source, and "edit `06_Application/.env`, then restart the container" is not
a first-run experience. So the key becomes a setting the user pastes into the
app.

Which makes it OUR problem where it lands. Three properties, and each one is
here because the obvious implementation gets it wrong:

* ENCRYPTED, through the SAME `SecretBox` that already protects platform
  credentials. One key (`EVALKIT_SECRET_KEY`), one crypto path. A second one
  would be a second thing to review and the one nobody reviews;
* ATOMIC. `.tmp` + `os.replace`, like `JobStore.save`. A key the user pasted
  once and a truncated file is a support ticket that starts "it just stopped
  working";
* OWNER-ONLY on disk. See `_restrict_to_user` -- the interesting half.

Layout, in the SAME state directory the JobStore roots itself in, so a deploy
mounts one volume and a `docker run` names one path:

    <root>/settings.json      {"anthropicApiKey": "<Fernet ciphertext>"}
    <root>/<job_id>/...       the job directories

JSON with the ciphertext as a FIELD rather than a bare ciphertext file, so the
second setting -- a model choice, a default objective -- lands without a
migration. The plaintext is never a field, and the file is never logged.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from cryptography.fernet import InvalidToken

from .crypto import SecretBox, ServiceError

logger = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"

#: The field in `settings.json`. The VALUE is ciphertext; the name is not a
#: secret and being able to grep for it is worth more than the obscurity.
API_KEY_FIELD = "anthropicApiKey"

#: Every Anthropic key starts with this. Checked at paste time so a user who
#: pastes the wrong string -- an OpenAI key, the service token, a console URL --
#: learns now instead of when the first paid stage 401s twenty minutes in.
#: Deliberately a PREFIX check and nothing more: we do not know the length or
#: alphabet of keys Anthropic has not issued yet, and a validator that rejects
#: a valid key is worse than one that accepts an invalid one.
API_KEY_PREFIX = "sk-ant-"


def _icacls(path: Path, *args: str) -> tuple[int, str]:
    """Run one icacls invocation. Returns (returncode, message); never raises."""
    try:
        proc = subprocess.run(
            ["icacls", str(path), *args],
            capture_output=True, text=True, timeout=15,
            # No shell: the path is ours but the identity string comes from the
            # environment, and `cmd` metacharacters in it are not something we
            # want to have to think about.
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()


#: Memoised: the account running the process does not change mid-process, and
#: resolving it costs a subprocess.
_windows_identity: str | None = None


def _current_windows_identity() -> str:
    """Who to grant to, as icacls will actually resolve it.

    THE SECOND TRAP, found while writing this. The obvious `%USERNAME%` --
    "cosmo" here -- is NOT unambiguous to icacls: it resolved to a machine
    principal, printed a plausible `Cosmo\\:(F)` in the resulting ACL, returned
    0, and the granted SID was not the running user's. Combined with
    `/inheritance:r` that produces a file its owner can read but cannot rename,
    so the next atomic write dies with WinError 5. A silent no-op would have
    been the kinder failure.

    So: the SID, from `whoami`, which needs no name resolution at all. The
    `DOMINIO\\usuario` form is the fallback -- also unambiguous, and correct on
    the boxes where `whoami` is missing.
    """
    global _windows_identity
    if _windows_identity is not None:
        return _windows_identity
    sid = ""
    try:
        proc = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=15, shell=False,
        )
        if proc.returncode == 0:
            # "COSMO\cosmo","S-1-5-21-..." -- the SID is the last field.
            sid = proc.stdout.strip().split(",")[-1].strip().strip('"')
    except (OSError, subprocess.SubprocessError):
        sid = ""
    if sid.startswith("S-1-"):
        _windows_identity = "*" + sid          # `*` = "this is a SID, not a name"
        return _windows_identity
    domain = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME") or ""
    user = os.environ.get("USERNAME") or ""
    _windows_identity = f"{domain}\\{user}" if (domain and user) else user
    return _windows_identity


def _restrict_to_user(path: Path) -> None:
    """Make the file readable only by the account that owns it.

    THE TRAP, and it is a quiet one: `os.chmod(path, 0o600)` on Windows is a
    near no-op. It toggles the read-only attribute and nothing else -- there is
    no owner/group/other split in an NTFS ACL for it to write -- and yet
    `os.stat` afterwards happily reports `0o600`, fabricated by the CRT from
    that single attribute. So the naive implementation looks correct, a test on
    the mode bits goes green, and the file sits there inheriting whatever the
    parent directory grants, which on a shared or service-account box is
    everyone. Credit where it is due: this is documented in the openworker
    (MIT) audit, `coworker/secrets.py:_restrict_to_user`, which hit it first.

    What actually restricts a file on Windows is the ACL:

        icacls <path> /grant:r <identity>:F      # us, replacing any prior entry
        icacls <path> /inheritance:r             # ...and only us

    TWO INVOCATIONS, in that order, and not the one-liner it looks like. Both
    options in a single command are applied left to right, so a grant that
    fails to resolve (see `_current_windows_identity`) still gets its
    `/inheritance:r` executed -- leaving a file with no usable ACE at all,
    which its own owner cannot rename or delete. Granting first and only
    dropping inheritance once the grant is CONFIRMED means the failure mode is
    "still readable by the machine's other accounts", logged, instead of
    "unusable", crashing.

    Failure is logged, never raised. A user who cannot save their key because
    `icacls` was missing from PATH is worse off than one whose key is stored
    with loose permissions on a single-user laptop. But an unrestricted file
    nobody is told about is the actual disaster, so it goes to the log at
    WARNING with the path named.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    who = _current_windows_identity()
    if not who:
        logger.warning(
            "no se pudo determinar la cuenta de Windows: %s queda con los "
            "permisos heredados del directorio", path,
        )
        return

    code, message = _icacls(path, "/grant:r", f"{who}:F")
    if code != 0:
        logger.warning(
            "icacls no pudo otorgar permisos sobre %s a %s (código %s): %s -- "
            "el archivo queda legible para otras cuentas de esta máquina",
            path, who, code, message,
        )
        return          # NOT dropping inheritance: that would brick the file

    code, message = _icacls(path, "/inheritance:r")
    if code != 0:
        logger.warning(
            "icacls no pudo quitar la herencia de permisos de %s (código %s): "
            "%s -- el archivo queda legible para otras cuentas de esta máquina",
            path, code, message,
        )


def validate_api_key(value: str) -> str:
    """The pasted value, trimmed -- or a Spanish refusal naming what was wanted.

    Trimming is not politeness: people paste from a terminal and from the
    Anthropic console with a trailing newline, and a key with `\\n` in it fails
    as a 401 from the API, which reads like a bad key rather than a bad paste.
    """
    clean = (value or "").strip()
    if not clean:
        raise ServiceError(
            "la clave de API está vacía: pegá la clave de Anthropic, que "
            f"empieza con «{API_KEY_PREFIX}»",
            hint="(la conseguís en console.anthropic.com, en «API keys»)",
        )
    if not clean.startswith(API_KEY_PREFIX):
        # The refused value is NOT echoed back: it is a secret often enough --
        # somebody pasting the wrong key is still pasting A key -- and it would
        # then be in the response body, the browser console and the server log.
        raise ServiceError(
            "eso no parece una clave de Anthropic: se esperaba un valor que "
            f"empiece con «{API_KEY_PREFIX}»",
            hint="(la conseguís en console.anthropic.com, en «API keys»)",
        )
    return clean


class SettingsStore:
    """Reads and writes `<root>/settings.json`. No caching, like `JobStore`.

    Every read hits the file, so a key saved by the request handler is visible
    to the next request even if a second worker wrote it.
    """

    def __init__(self, root: Path | str, box: SecretBox) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # Underscored and never rendered: see __repr__.
        self._box = box

    def __repr__(self) -> str:  # a repr of the store must not carry the key
        return f"SettingsStore(root={str(self.root)!r}, box=***)"

    __str__ = __repr__

    @property
    def path(self) -> Path:
        return self.root / SETTINGS_FILE

    # --- the file ----------------------------------------------------------

    def _read(self) -> dict:
        """The settings dict, or {} for absent/corrupt.

        A truncated or hand-edited file reads as "nothing configured" rather
        than as a 500 on the settings screen: the screen's whole job is to let
        the user paste the key again, and it cannot do that if it cannot load.
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("no se pudo leer %s (%s): se ignora", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        """Atomic, then restricted. A half-written file must not lose the key."""
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        # Restrict the TEMP file: permissions travel with it through the
        # rename, so there is never a moment when the real path exists with the
        # directory's inherited ACL.
        _restrict_to_user(tmp)
        os.replace(tmp, self.path)

    # --- the API key -------------------------------------------------------

    def get_api_key(self) -> str | None:
        blob = self._read().get(API_KEY_FIELD)
        if not blob:
            return None
        try:
            return self._box.decrypt_text(str(blob).encode("ascii"))
        except (InvalidToken, ValueError) as exc:
            # EVALKIT_SECRET_KEY was rotated (or the file was tampered with).
            # For a CREDENTIAL that is fatal -- the job cannot log in and must
            # say so. For this, it is not: the user pastes their key again and
            # the next write is under the current box. Refusing to serve the
            # settings screen would take away the only way to fix it.
            logger.warning(
                "no se pudo descifrar la clave de API guardada (%s): se trata "
                "como si no hubiera clave; volvé a pegarla en la aplicación", exc,
            )
            return None

    def has_api_key(self) -> bool:
        return self.get_api_key() is not None

    def set_api_key(self, value: str) -> None:
        """Validate, trim, encrypt, write. Refusals leave the stored key alone."""
        clean = validate_api_key(value)      # raises ServiceError before any write
        data = self._read()
        data[API_KEY_FIELD] = self._box.encrypt_text(clean).decode("ascii")
        self._write(data)

    def clear_api_key(self) -> None:
        """Forget it. Clearing something that was never set is not an error."""
        data = self._read()
        if API_KEY_FIELD not in data:
            return
        data.pop(API_KEY_FIELD, None)
        self._write(data)
