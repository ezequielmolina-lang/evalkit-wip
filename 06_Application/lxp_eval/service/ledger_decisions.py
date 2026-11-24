"""Las decisiones humanas (y la aprobación-máquina) sobre el ledger de evidencia.

Vivían en `app.py`, que es la superficie HTTP. Pero ninguna de estas funciones
toca la red: escriben una instantánea y una fila en el ledger, nada más. El
corredor de lotes (`batch.py`) necesita `auto_approve` y, mientras estuvo en
`app.py`, el CLI desatendido arrastraba FastAPI/Starlette sólo para alcanzar una
función de store+ledger. Acá viven sin dependencia web: `app.py` las importa para
sus endpoints y `batch.py` las importa sin cargar el servidor.

Nada cambió en lo que hacen: es el mismo código, movido. Las invariantes del
ledger (append-only, instantánea antes de la fila, fail-closed) siguen escritas
en los docstrings de cada función.
"""
from __future__ import annotations

import copy
import json

from ..evalkit.ledger import EvidenceLedger
from . import serialize
from .store import JobRecord, JobStore, now


def _slug(value: str) -> str:
    """Same rule the runner applies to probe ids in artifact filenames."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


def _ledger_decision(
    store: JobStore,
    record: JobRecord,
    *,
    name_stem: str,
    kind: str,
    probe_id: str,
    test_id: str,
    note: str,
    timestamp: str,
    payload: object,
) -> None:
    """Put a human decision on the evidence ledger, snapshot first.

    The operating model's answer to "why was probing this platform defensible?"
    is that a human saw it and authorized it -- so the authorization has to
    live in the same tamper-evident file as the probes it authorized, backed by
    an artifact an auditor can re-hash: a byte-for-byte snapshot of the
    decision. Snapshot written first, THEN hashed into the row, the same
    capture-time rule every other ledger entry follows.

    The filename is versioned, never reused. `verify()` re-hashes the file a
    row points at, so overwriting an earlier snapshot on a repeat decision
    would retroactively break verification of the earlier row -- a rewritten
    artifact is exactly the tampering the ledger exists to expose. Append-only
    holds for decisions too.

    FAIL CLOSED, deliberately: no try/except here. This row IS the audit record
    that makes the run defensible, so a decision that cannot be recorded must
    not take effect downstream -- and each caller sequences its writes to keep
    that true. The approval saves the record first but only drives the runner
    after this returns. The signoff calls this BEFORE touching the record,
    because /approve consults `record.signoffs` and never the ledger: a saved
    signature whose append then failed would authorize a run with no row
    behind it.
    """
    evidence_dir = store.evidence_dir(record.job_id)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = f"{name_stem}.json" if n == 1 else f"{name_stem}_{n}.json"
        snapshot = evidence_dir / name
        if not snapshot.exists():
            break
        n += 1
    snapshot.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    EvidenceLedger(store.ledger_path(record.job_id)).append(
        probe_id=probe_id,
        test_id=test_id,
        platform=record.platform,
        run_index=0,
        url="",
        artifact=snapshot,
        kind=kind,
        note=note,
        timestamp=timestamp,
    )


def _ledger_the_approval(
    store: JobStore, record: JobRecord, *, note: str | None = None
) -> None:
    """The approval row: one per approval, artifact = the approved plan.

    `note` names the actor on the audit record and is rendered verbatim into the
    report's provenance line (`serialize.provenance_of`). It defaults to the
    human sentence, so POST /approve is unchanged. The unattended batch runner
    passes `serialize.APPROVAL_NOTE_AUTO`, so an auto-approved run is recorded
    as machine-approved rather than borrowing a human's signature -- the row is
    otherwise identical (same `kind`, same snapshot), only the actor differs.
    """
    _ledger_decision(
        store,
        record,
        name_stem="approved_plan",
        kind="approval",
        probe_id="__plan__",
        test_id="__plan__",
        note=note or serialize.APPROVAL_NOTE_HUMAN,
        timestamp=record.approved_at,
        payload=record.approved_plan,
    )


def _ledger_the_signoff(
    store: JobStore, record: JobRecord, probe: dict, signed_at: str
) -> None:
    """The signoff row: one per POST /signoff, never bulk.

    Mirrors the endpoint's own rule -- a signoff granted by approving a list
    of thirty probes is not a signoff -- so each signature gets its own row
    and its own snapshot. The snapshot is small and explicit: which probe,
    which criterion, when, and the gated class the probe itself declares
    (`signoffReason`), i.e. WHY a human signature was required at all.
    """
    probe_id = probe["probeId"]
    test_id = probe["criterionId"]
    _ledger_decision(
        store,
        record,
        name_stem=f"signoff_{_slug(probe_id)}",
        kind="signoff",
        probe_id=probe_id,
        test_id=test_id,
        note=f'firma humana para el probe "{probe_id}"',
        timestamp=signed_at,
        payload={
            "probe_id": probe_id,
            "test_id": test_id,
            "signed_at": signed_at,
            "gated_class": probe["signoffReason"],
        },
    )


def auto_approve(store: JobStore, record: JobRecord) -> None:
    """Aprobar el plan sin humano, dejando el gate exactamente donde estaba.

    Lo comparten el corredor de lotes y POST /jobs bajo `approval_policy="auto"`.
    Es `POST /jobs/{id}/approve` menos la persona: el plan por defecto, sin
    ediciones, y el actor MÁQUINA en la fila del ledger. No se escribe ningún
    signoff, así que cada prueba con compuerta la deja afuera
    `JobRunner.runnable_probes` y su criterio vuelve NO_EVALUADO. La firma sigue
    siendo por prueba, y acá no hay ninguna.

    LAS PRUEBAS VAN EN LA INSTANTÁNEA. El plan aprobado lleva una copia de las
    pruebas tal como el servidor las tenía guardadas -- pasos, regla de veredicto,
    el consejo que las revisó y `requiresSignoff` -- y esa copia es lo que se
    hashea en la fila de aprobación. La primera versión guardaba `probes: None`
    ("corré todas las guardadas"), que ejecutaba lo mismo pero dejaba el registro
    de auditoría sin decir QUÉ se aprobó: los criterios estaban en el archivo con
    hash y las pruebas sólo en `job.json`, que es editable. Una aprobación humana
    por la interfaz sí las guarda; una aprobación-máquina, que es justo la que
    nadie miró, no puede guardar menos.

    Guardarlas no mueve el gate: `runnable_probes` lee `requiresSignoff` de la
    copia del SERVIDOR y exige la firma en `record.signoffs`, nunca en el plan.
    Que la instantánea muestre una prueba con compuerta y sin firmar es, de paso,
    la evidencia de por qué su criterio volvió NO_EVALUADO.

    Mismo orden que el endpoint: guardar el registro primero, poner la fila en el
    ledger después y ANTES de que `execute` corra sobre su autoridad.
    `_ledger_the_approval` es fail-closed, así que una aprobación que no se puede
    registrar no surte efecto.
    """
    record.sampling = record.sampling or dict(serialize.DEFAULT_SAMPLING)
    record.approved_plan = {
        "criteria": serialize.criteria_out(record.criteria),
        # Copia profunda: la instantánea es lo que había AL APROBAR, y no debe
        # cambiar si después alguien toca `record.probes` en memoria.
        "probes": copy.deepcopy(record.probes),
        "sampling": record.sampling,
    }
    record.approved_at = now()
    store.save(record)
    _ledger_the_approval(store, record, note=serialize.APPROVAL_NOTE_AUTO)


__all__ = [
    "_ledger_decision",
    "_ledger_the_approval",
    "_ledger_the_signoff",
    "_slug",
    "auto_approve",
]
