"""Durable job state. One directory per job, JSON on disk.

The container WILL be restarted -- a deploy, an OOM, a Fly.io host migration --
and a run takes minutes. An in-memory dict means every restart silently
abandons whatever a ministry was watching, with no way to tell an abandoned job
from a slow one. So every transition is written through to disk before the
response goes out, and a fresh worker rebuilds its whole view by reading the
directory.

Layout, one directory per job:

    <root>/<job_id>/job.json                  the record (NEVER a credential)
    <root>/<job_id>/credentials.enc           Fernet ciphertext, or absent
    <root>/<job_id>/evidence/ledger.jsonl     the evidence ledger
    <root>/<job_id>/evidence/approved_plan.json   the plan as approved, snapshotted
    <root>/<job_id>/evidence/signoff_<probe>.json one probe's signoff, snapshotted
    <root>/<job_id>/evidence/artifacts/       screenshots
    <root>/<job_id>/evidence/page_text/       page-text captures

SQLite would also have worked. JSON won because the artefacts an auditor needs
are already files -- the ledger is JSONL, the screenshots are PNGs -- so a job
directory you can zip and hand over is worth more here than a query planner,
and there is exactly one writer per job.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..textio import scrub_surrogates

JOB_FILE = "job.json"
CREDENTIALS_FILE = "credentials.enc"
EVIDENCE_DIR = "evidence"
LEDGER_FILE = "ledger.jsonl"
ARTIFACTS_DIR = "artifacts"
PAGE_TEXT_DIR = "page_text"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobRecord:
    """Everything about one job EXCEPT the credentials.

    The exclusion is structural, not a convention someone has to remember: there
    is no credential field to accidentally serialise. `has_credentials` is a
    boolean, and the ciphertext lives in its own file.
    """

    job_id: str
    created_at: str
    updated_at: str

    # --- the request -------------------------------------------------------
    objective: str = ""
    areas: list[str] = field(default_factory=list)
    platform_url: str = ""
    platform: str = ""            # the label used in the ledger and the report
    platform_kind: str = ""
    documents: list[dict] = field(default_factory=list)
    has_credentials: bool = False
    #: Who gets to approve the plan: "human" (the default, and today's whole
    #: behaviour -- the job parks at `awaiting_approval` until a person calls
    #: POST /approve) or "auto". "auto" is the unattended-batch policy: the
    #: UNGATED probes proceed without a human, each gated probe stays behind its
    #: own signoff and so scores NO_EVALUADO. Set by the batch runner; a job
    #: created through the HTTP surface keeps "human", which is why old job.json
    #: files -- which have no such field -- read back as "human" too.
    approval_policy: str = "human"

    # --- the stage machine -------------------------------------------------
    stage: str = "framework"
    progress: dict = field(default_factory=dict)
    error: dict | None = None
    #: THE HEARTBEAT. Written on every transition and every progress update, so
    #: "this stage has produced nothing for longer than its own budget" is a
    #: question the watchdog can answer -- from disk, after a restart, about a
    #: job whose worker is gone. Distinct from `updated_at`, which any write
    #: touches (a signoff is not progress on a stage).
    last_progress_at: str = ""
    #: The budget for the stage currently in flight, in seconds, as it was
    #: derived when the stage began. Persisted rather than recomputed so a
    #: restarted worker judges the dead one by the same clock it was running
    #: under. 0 means "no stage is running".
    stage_timeout_s: float = 0.0

    # --- artefacts ---------------------------------------------------------
    #: The framework stage's output: criteria + the two-axis coverage report.
    criteria: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)
    #: The probes stage's output. AUTHORITATIVE for the gate: whatever a client
    #: sends back at approval time, `requires_signoff` is read from here.
    probes: list[dict] = field(default_factory=list)
    sampling: dict = field(default_factory=dict)
    #: probe_id -> ISO timestamp. Written only by POST /jobs/{id}/signoff.
    signoffs: dict[str, str] = field(default_factory=dict)
    #: WHAT THE RUN COST. Accumulated from the driver as each probe finishes.
    #: The driver knew this per step and only logged it -- and the service
    #: emits no info-level logging at all, so the figure existed for
    #: microseconds and vanished. That is why a large live bill could not be
    #: attributed to any run.
    spend_usd: float = 0.0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: The vendor notice for the flagged set, rendered at probes time and kept.
    #: The gate's own wording is "se avisa al proveedor antes de ejecutarlo y una
    #: persona contrafirma ese aviso" -- so the signature screen needs the text
    #: being countersigned. It used to be generated and dropped, and had to be
    #: rewritten by hand the first time a crisis probe actually reached signoff.
    vendor_notice: str = ""
    #: probe_id -> why that probe could not be evaluated. A refusal is a
    #: finding about the platform (or the account), not an error of the run, so
    #: it is carried on the record and reported rather than thrown away.
    unevaluated: dict[str, str] = field(default_factory=dict)
    #: probe_id -> the steps that ACTUALLY ran, read back from the ledger.
    #:
    #: Kept BESIDE `probes` and `approved_plan`, never merged into them. On job
    #: d19ddd2bfdfc4a90 the two disagree completely: PB-PT-D8-01 was approved
    #: with 17 abstract steps ("ingresar con una cuenta de estudiante de
    #: prueba") and driven with 27 concrete ones ("hacer clic en el curso BM -
    #: Matematicas Grado 3"), rewritten by hand against the real UI and never
    #: written back. Every probe on that job is like this.
    #:
    #: Overwriting the approved steps with these would make the record claim a
    #: human approved wording they never saw -- the same lie `council_still_
    #: applies` exists to stop -- and would erase the divergence itself, which
    #: is the finding. So both are carried, and a reader can see the gap.
    executed_steps: dict[str, list[str]] = field(default_factory=dict)
    #: EL PLAN QUE UNA PERSONA APRUEBA ANTES DE QUE SE REDACTE UNA PRUEBA.
    #: `strategy` es la decisión del planificador por criterio, ya cerrada con el
    #: muestreo; `sampling_matrix`, la matriz tal como la propuso el muestreo;
    #: `plan_document`, dónde quedó el documento (versión, archivos, conteos); y
    #: `plan_corrections`, lo que la persona corrigió ("eso existe, está acá"),
    #: como datos que el replaneo consume. Ver evalkit/planner.py.
    strategy: list[dict] = field(default_factory=list)
    sampling_matrix: dict = field(default_factory=dict)
    plan_document: dict = field(default_factory=dict)
    plan_corrections: list[dict] = field(default_factory=list)
    #: The reviewed plan exactly as the human approved it.
    approved_plan: dict | None = None
    approved_at: str = ""
    report: dict | None = None

    def touch(self) -> None:
        self.updated_at = now()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class JobStore:
    """Reads and writes job directories under `root`. No caching, on purpose.

    `get` always re-reads the file, so two workers (or a worker and a test)
    looking at the same root cannot disagree about a job's stage.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # --- paths -------------------------------------------------------------

    def dir_of(self, job_id: str) -> Path:
        return self.root / job_id

    def evidence_dir(self, job_id: str) -> Path:
        return self.dir_of(job_id) / EVIDENCE_DIR

    def ledger_path(self, job_id: str) -> Path:
        return self.evidence_dir(job_id) / LEDGER_FILE

    def artifacts_dir(self, job_id: str) -> Path:
        return self.evidence_dir(job_id) / ARTIFACTS_DIR

    def page_text_dir(self, job_id: str) -> Path:
        return self.evidence_dir(job_id) / PAGE_TEXT_DIR

    def credentials_path(self, job_id: str) -> Path:
        return self.dir_of(job_id) / CREDENTIALS_FILE

    # --- records -----------------------------------------------------------

    def create(self, **fields) -> JobRecord:
        job_id = uuid.uuid4().hex[:16]
        stamp = now()
        record = JobRecord(
            job_id=job_id, created_at=stamp, updated_at=stamp,
            # A job is alive from the moment it is created: a task that hangs
            # BEFORE writing its first progress must still be stallable.
            last_progress_at=stamp,
            **fields,
        )
        self.dir_of(job_id).mkdir(parents=True, exist_ok=True)
        self.save(record)
        return record

    def save(self, record: JobRecord) -> JobRecord:
        """Write through, atomically. A half-written job.json is a lost job."""
        record.touch()
        d = self.dir_of(record.job_id)
        d.mkdir(parents=True, exist_ok=True)
        target = d / JOB_FILE
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                scrub_surrogates(record.to_dict()), indent=2, ensure_ascii=False
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return record

    def get(self, job_id: str) -> JobRecord | None:
        # A job id becomes a path segment. Anything that is not a plain name is
        # not a job id, whatever it might be on the filesystem.
        if not job_id or "/" in job_id or "\\" in job_id or job_id in (".", ".."):
            return None
        path = self.dir_of(job_id) / JOB_FILE
        if not path.exists():
            return None
        return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_ids(self) -> list[str]:
        return sorted(
            p.name for p in self.root.iterdir() if (p / JOB_FILE).exists()
        ) if self.root.exists() else []

    # --- credentials -------------------------------------------------------

    def put_credentials(self, job_id: str, blob: bytes) -> Path:
        path = self.credentials_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        return path

    def get_credentials_blob(self, job_id: str) -> bytes | None:
        path = self.credentials_path(job_id)
        return path.read_bytes() if path.exists() else None

    # --- evidence ----------------------------------------------------------

    def ledger_rows(self, job_id: str) -> list[dict]:
        path = self.ledger_path(job_id)
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
