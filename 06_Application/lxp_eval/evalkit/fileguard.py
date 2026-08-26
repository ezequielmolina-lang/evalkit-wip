"""Owner-only files, written atomically. For anything that holds a live secret.

The one consumer today is the session store: a Playwright storage_state is a
bag of live cookies for the vendor's production tenant, so it gets the same
treatment `session.py` gives the password itself -- except the password never
touches disk and the storage state must, which is why this module exists.

Two properties, each earned elsewhere the hard way:

* **Owner-only.** POSIX expresses this with mode bits (0700 dir / 0600 file).
  Windows does not: `os.chmod` there only toggles the read-only flag, so a
  0600 chmod is a silent no-op and the file keeps the broad inherited ACLs
  (SYSTEM, Administrators, Users). The fix is an icacls ACL -- strip inherited
  entries, grant the current user alone. A DIRECTORY grant must carry
  (OI)(CI) inherit flags, or files created inside end up with an EMPTY DACL
  and nothing -- including us -- can open them again.

* **Atomic.** Write to a `.tmp` sibling and `os.replace` into place, so a
  crash mid-write leaves either the old state or the new one, never a
  truncated JSON that a later run half-authenticates with.

Both are best-effort on the ACL side: a transient icacls/chmod failure must
never block saving a session (the run would lose a login a human just
performed). Failure is reported by return value, not exception.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


def restrict_to_user(
    path: Path,
    *,
    is_dir: bool,
    run: Callable[..., Any] = subprocess.run,
    chmod: Callable[[Path, int], None] = os.chmod,
    env: Mapping[str, str] | None = None,
    platform: str = sys.platform,
) -> bool:
    """Make `path` accessible to the current user alone. True iff applied.

    `run` / `chmod` / `env` / `platform` are injectable for tests; the
    defaults are the real thing.
    """
    if platform == "win32":
        env = os.environ if env is None else env
        user = env.get("USERNAME")
        if not user:
            return False
        domain = env.get("USERDOMAIN")
        account = f"{domain}\\{user}" if domain else user
        grant = f"{account}:(OI)(CI)F" if is_dir else f"{account}:F"
        try:
            run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                capture_output=True,
                check=False,
            )
        except OSError:
            return False
        return True

    try:
        chmod(Path(path), 0o700 if is_dir else 0o600)
    except OSError:
        return False
    return True


def write_private_text(
    path: Path,
    text: str,
    *,
    restrict: Callable[..., bool] = restrict_to_user,
) -> Path:
    """Write `text` to `path` atomically, owner-only. Returns `path`.

    The ACL goes on the `.tmp` BEFORE the replace, so the content is never on
    disk under the broad inherited ACLs, not even for an instant.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    restrict(tmp, is_dir=False)
    os.replace(tmp, path)
    return path
