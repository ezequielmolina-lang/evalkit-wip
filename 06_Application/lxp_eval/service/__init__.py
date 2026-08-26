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
from .app import create_app
from .crypto import SecretBox, ServiceError, generate_key
from .jobs import Engine, JobRunner, Stage
from .retry import RetryPolicy
from .store import JobRecord, JobStore
from .watchdog import Clock, Supervisor, reconcile

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
