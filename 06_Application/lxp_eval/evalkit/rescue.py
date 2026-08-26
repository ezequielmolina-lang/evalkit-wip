"""Judging a criterion from evidence that is already on disk.

A run's evidence is what came after its approval -- that is `rows_since`, and
the rule is right: it stops a verdict resting partly on an attempt that was
thrown away. But it has a cost nobody accounted for. A job whose approved plan
carried one probe out of eight leaves hours of captured, hashed, ledgered
evidence unscored: present on disk, verifiable against the ledger, and
invisible to the report. Job d19ddd2bfdfc4a90 has 223 such rows.

This module scores that evidence WITHOUT pretending it was a run. It selects
rows by an explicit window, drops the captures the pipeline itself marked as
truncated, and returns -- alongside the rows -- a count of everything it
excluded and a Spanish sentence saying so. The caller is expected to print that
sentence. A verdict reconstructed from an archive is a weaker claim than a
verdict from an approved run, and the difference has to survive into the
artifact or this becomes one more number nobody can trace.

Two things it deliberately does NOT do:

  - write to a job record, or append to the ledger. There is no approval behind
    these verdicts, so there is no governance row to write, and inventing one
    would put a signature on a decision no human made.
  - decide anything about the platform. An empty selection means WE could not
    see, not that the vendor hid something -- which is exactly how PT-D9-H1
    would otherwise read: 32 ledger rows, not one usable text, rendered as an
    unmet criterion.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import EVIDENCE_KINDS

#: Written INTO the artifact by the capture, not by the scorer. `scoring.
#: _read_text_artifact` adds its own marker when it trims for the prompt, and
#: that one is recoverable -- raise LXP_MAX_INLINE_CHARS and the text comes
#: back. This one means the characters never reached disk.
#:
#: On Plataforma A it cut every page to its first 8058 characters, which is the course
#: sidebar: the lesson body sits at the END of a ~24k page. So a capture
#: carrying this marker holds the navigation list and none of the content the
#: criterion asks about. 64 of job d19ddd2bfdfc4a90's 223 rows carry it.
CAPTURE_TRUNCATION_MARKER = "[TEXTO TRUNCADO"

#: Kinds whose artifact is text we can inspect for the marker. A screenshot is
#: bytes: it cannot be truncated this way, and trying to read it as text would
#: turn a healthy PNG into a dropped row.
_TEXT_KINDS = frozenset({"page_text", "observation"})


@dataclass
class RescueSelection:
    """What will be judged, and everything that will not be -- with reasons.

    The counts are not diagnostics for a log. They are the provenance of a
    verdict that has no approval behind it, and they belong in the artifact.
    """

    test_id: str
    platform: str
    rows: list[dict] = field(default_factory=list)
    since: str = ""
    until: str = ""
    dropped_outside_window: int = 0
    dropped_missing: int = 0
    dropped_truncated: int = 0
    dropped_duplicate: int = 0

    @property
    def usable(self) -> bool:
        return bool(self.rows)

    @property
    def note(self) -> str:
        """One Spanish sentence a reader can act on. Never silently empty."""
        def plural(n: int, one: str, many: str) -> str:
            return f"{n} {one}" if n == 1 else f"{n} {many}"

        parts = []
        if self.dropped_outside_window:
            parts.append(
                plural(self.dropped_outside_window, "captura fuera", "capturas fuera")
                + " de la ventana elegida"
            )
        if self.dropped_truncated:
            parts.append(
                plural(self.dropped_truncated, "captura truncada", "capturas truncadas")
                + " en origen"
            )
        if self.dropped_duplicate:
            parts.append(
                plural(self.dropped_duplicate, "captura repetida", "capturas repetidas")
                + " (contenido identico)"
            )
        if self.dropped_missing:
            parts.append(
                plural(self.dropped_missing, "archivo que ya no esta",
                       "archivos que ya no estan") + " en disco"
            )

        kept = plural(len(self.rows), "fila de evidencia", "filas de evidencia")
        if not parts:
            return f"Se juzga con {kept}; no se descarto nada."
        return f"Se juzga con {kept}; se descarto: " + "; ".join(parts) + "."

    def to_dict(self) -> dict:
        return {
            "testId": self.test_id,
            "platform": self.platform,
            "kept": len(self.rows),
            "usable": self.usable,
            "window": {"since": self.since, "until": self.until},
            "dropped": {
                "outsideWindow": self.dropped_outside_window,
                "missing": self.dropped_missing,
                "truncatedAtCapture": self.dropped_truncated,
                "duplicate": self.dropped_duplicate,
            },
            "note": self.note,
            "artifacts": [r.get("artifact", "") for r in self.rows],
        }


def is_capture_truncated(text: str) -> bool:
    """Did the capture pipeline cut this page short before writing it?"""
    return CAPTURE_TRUNCATION_MARKER in (text or "")


def _content_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def select_archived_evidence(
    rows: list[dict],
    *,
    test_id: str,
    platform: str,
    since: str = "",
    until: str = "",
    root: Path | str = ".",
) -> RescueSelection:
    """The rows worth handing a judge, and a count of the ones that were not.

    Filtering order is deliberate, because each step's count has to mean
    something on its own: a row outside the window is never also reported as
    truncated, and a file that is gone is never reported as a duplicate of one
    that is still there.

    `since` / `until` are ISO timestamps compared as strings, the same way
    `ledger.rows_since` compares them -- the ledger writes a fixed-width
    timezone-qualified format, so lexical order is chronological order.
    """
    sel = RescueSelection(test_id=test_id, platform=platform, since=since, until=until)
    root = Path(root)

    mine = [
        r for r in rows
        if r.get("test_id") == test_id
        and r.get("platform") == platform
        and r.get("kind") in EVIDENCE_KINDS
    ]

    seen: set[str] = set()
    for r in sorted(mine, key=lambda x: str(x.get("timestamp") or "")):
        stamp = str(r.get("timestamp") or "")
        if since and stamp < since:
            sel.dropped_outside_window += 1
            continue
        if until and stamp >= until:
            sel.dropped_outside_window += 1
            continue

        raw = str(r.get("artifact") or "")
        path = Path(raw)
        if not path.is_absolute():
            path = root / raw
        digest = _content_digest(path)
        if digest is None:
            sel.dropped_missing += 1
            continue

        if r.get("kind") in _TEXT_KINDS:
            body = path.read_text(encoding="utf-8", errors="replace")
            if is_capture_truncated(body):
                sel.dropped_truncated += 1
                continue

        if digest in seen:
            sel.dropped_duplicate += 1
            continue
        seen.add(digest)
        sel.rows.append(r)

    return sel
