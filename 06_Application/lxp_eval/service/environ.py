"""Where each variable is allowed to come from. Two sources, on purpose.

The bug that started this: `ANTHROPIC_API_KEY` lives in `06_Application/.env`,
not the shell. `lxp_eval/config.py` calls `load_dotenv` at import, so anything
touching `CONFIG` sees it -- but `selfcheck` imported neither, read a bare
`os.environ`, and reported `[FALLA]` for a key the running service would have
had. A diagnostic that sends an operator to fix a working configuration, and a
deploy gate that blocks a healthy deploy.

That fix was right for that key and WRONG when I extended it to the other two.
This file now splits them:

    engine_environ()      os.environ + .env      ANTHROPIC_API_KEY
    deployment_environ()  os.environ ONLY        EVALKIT_SECRET_KEY
                                                 EVALKIT_SERVICE_TOKEN

WHY THE SPLIT.

`ANTHROPIC_API_KEY` is ENGINE config. `.env` is where this repo keeps it, the
engine reads it from there, and the service's whole job is to drive the engine
-- so the service must see what the engine sees. Anything else is the bug above.

The other two are DEPLOYMENT secrets for the container. They did not exist
before the service did; nothing has ever kept them in `.env`. (The earlier
version of this file argued `.env` was "the obvious place, since the Anthropic
key is already there". That was my inference, not a fact about the codebase,
and it does not survive contact with the failure modes.)

The failure modes are asymmetric, and that is what decides it:

* a stale `EVALKIT_SERVICE_TOKEN` in an image is LOUD -- every call 401s, the
  web app already says so in Spanish, and you find it in minutes;
* a stale `EVALKIT_SECRET_KEY` is QUIET. The service starts, encrypts new
  credentials happily, and fails only when it tries to DECRYPT credentials
  written under the real key -- surfacing as a confusing login failure on one
  job rather than as a configuration error at boot.

A container that means to take these from a platform secret manager but ships a
stale `.env` would silently start with the file's values. Since `override=False`
means a real environment variable always wins, `.env` could only ever take
effect when the injection FAILED -- precisely the moment to fail closed and
loud rather than quietly succeed with the wrong key.

Fail-closed is the load-bearing property of the token, and a file quietly
satisfying it undermines exactly that. The proof is that it made the guarantee
untestable: `test_the_service_refuses_to_build_without_a_token` started passing
a token it was trying to prove absent.

Cost of the decision: running the service locally needs a real export; dropping
the values in `.env` is not enough. `selfcheck` names both variables and prints
the command to generate each, so the friction is one message long.
"""
from __future__ import annotations

import os
from typing import Mapping

#: Read from `.env` as well as the environment. Engine configuration only.
ENGINE_VARS = ("ANTHROPIC_API_KEY",)

#: Environment ONLY. Deployment secrets -- see the module docstring.
DEPLOYMENT_VARS = ("EVALKIT_SECRET_KEY", "EVALKIT_SERVICE_TOKEN")


def dotenv_only() -> dict[str, str]:
    """Parse `.env` into a dict. DOES NOT TOUCH `os.environ`.

    `dotenv_values`, not `load_dotenv`, and the distinction is the whole split.
    The first version called `load_dotenv`, which MUTATES `os.environ` -- so the
    moment anything resolved the engine's key, every other `.env` line landed in
    the process environment and `deployment_environ` handed it straight back.
    The split silently did not hold, and `selfcheck` reported two deployment
    secrets as "present in the environment" when they were only ever in a file.
    Merging without mutating is what makes the two sources actually separate.
    """
    try:
        from dotenv import dotenv_values, find_dotenv

        found = find_dotenv(usecwd=True)
        return {k: v for k, v in dotenv_values(found).items() if v is not None}
    except Exception:  # noqa: BLE001 -- a missing/broken dotenv must not break auth
        # Falling back to the bare environment is strictly better than refusing
        # to read anything: a shell-exported value still works.
        return {}


#: Kept as the seam the tests neutralise to express "no `.env` at all".
load_dotenv_once = dotenv_only


def engine_environ(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """The environment as the ENGINE sees it: `.env` UNDER `os.environ`.

    The real environment wins, deliberately unlike `config.py`'s
    `override=True`: this fills GAPS. A value someone exported -- or that a test
    set -- must never be silently replaced by a stale line in a checked-out
    `.env`. For the case this exists to fix (a variable present only in `.env`)
    both orders agree.

    Returns a NEW mapping. Nothing here writes to `os.environ`.

    Pass `env` to bypass entirely -- that is what the tests do, and why none of
    them depend on a developer's `.env`.
    """
    if env is not None:
        return env
    return {**load_dotenv_once(), **os.environ}


def deployment_environ(env: Mapping[str, str] | None = None) -> Mapping[str, str]:
    """The environment ONLY. `.env` is deliberately not consulted.

    Used for the two secrets that gate the service itself, so that "unset"
    means unset -- and the fail-closed guarantee stays true, and testable.
    """
    if env is not None:
        return env
    return os.environ


def deployment_vars_in_dotenv() -> list[str]:
    """Deployment secrets found in `.env`, where they do not belong.

    An ambiguous configuration deserves to be named, not silently resolved.
    Nothing here reads them -- but `lxp_eval/config.py` calls
    `load_dotenv(override=True)` at import, so if a job ever imports `CONFIG`
    these values DO land in `os.environ` mid-process. Whether they take effect
    then depends on import order, which is not a thing anyone should have to
    reason about. `selfcheck` warns when this is the case.
    """
    return [name for name in DEPLOYMENT_VARS if name in load_dotenv_once()]
