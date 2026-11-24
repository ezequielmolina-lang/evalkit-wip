"""The job service: the HTTP layer the Next.js app drives.

Why a job and not a request: building a framework, generating probes past a
five-judge council and then driving a real browser takes MINUTES. A serverless
function cannot hold that. So the web POSTs to start an operation and polls for
structured progress, and the Python side lives in a container that owns
Playwright and the long-running work.

    from lxp_eval.service import create_app
    app = create_app()

Run it: see lxp_eval/service/README.md.
"""
from .crypto import SecretBox, ServiceError, generate_key
from .jobs import Engine, JobRunner, Stage
from .retry import RetryPolicy
from .store import JobRecord, JobStore
from .watchdog import Clock, Supervisor, reconcile


def __getattr__(name: str):
    """`create_app`, importado recién cuando alguien lo pide.

    `from lxp_eval.service import create_app` sigue funcionando igual (PEP 562).
    Lo que cambia es que importar el PAQUETE ya no carga `app.py`, y con él
    FastAPI y Starlette. Antes era `from .app import create_app` arriba de todo,
    así que cualquier submódulo -- en particular `batch`, el CLI que corre
    desatendido y sin servidor -- arrastraba el stack web sólo por vivir en este
    paquete. El motor no depende del front; el lote tampoco tiene por qué
    depender del servidor. Un test lo ata: importar `batch` en un proceso limpio
    no puede dejar `fastapi` en `sys.modules`.
    """
    if name == "create_app":
        from .app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Clock",
    "create_app",
    "Engine",
    "JobRunner",
    "JobRecord",
    "JobStore",
    "RetryPolicy",
    "SecretBox",
    "ServiceError",
    "Stage",
    "Supervisor",
    "generate_key",
    "reconcile",
]
