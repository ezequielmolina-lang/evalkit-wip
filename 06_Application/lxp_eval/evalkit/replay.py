"""Re-running a recorded probe through the real runner, with no browser.

WHY. The only loop that finds the bugs that matter runs against a live
platform: a login, a session, a role, a real Chromium, minutes per probe and
money per step. On 2026-07-20 that loop was exercised thirteen times in
twenty-one hours. Meanwhile the fast loop -- 900 tests in half a minute --
cannot see any of it, because a fake presents whatever shape the code expects.

But a finished run already left a perfect recording. Every step wrote a
screenshot, the full page text and a ledger row carrying the URL and the step
that produced it, each hashed. That is a high-fidelity fixture of the platform,
already paid for. This serves it back through the SAME `runner.run_probe`, so
the capture loop, the naming, the ledger writing and everything downstream run
exactly as they do live -- in milliseconds, for free, on every change.

WHAT IT CANNOT DO, stated plainly so nobody trusts it further than it goes. A
recording is downstream of the browser, so replay is blind to precisely the
layer that produced most of our incidents: it cannot catch a login page that is
a provider chooser rather than a form, a session token carrying the wrong role,
a selector that moved, or a stale process serving old code. It catches what
happens to evidence AFTER it is captured. Nothing before.

The drift guard is what keeps that honest. A recording describes the steps that
produced it; serve it for a probe whose steps were rewritten and the run goes
green while the evidence is about something else. That is the same lie as a
council reporting approval of a probe it never read, and it fails loudly here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import EvalkitError

#: `runner._note` writes "paso {n}: {step}" and appends a truncation admission
#: when the driver reports one. Both halves are parsed back out.
_NOTE = re.compile(r"^paso\s+(\d+):\s*(.*)$", re.DOTALL)
_TRUNCATED = re.compile(r"\s*\[TEXTO TRUNCADO:[^\]]*?(\d+)[^\]]*\]\s*$")


class ReplayError(EvalkitError):
    """Base for every way a recording fails to describe the probe asked of it."""


class ReplayDrift(ReplayError):
    """The probe's steps no longer match the ones that produced the recording."""


class RecordingExhausted(ReplayError):
    """The probe asks for a step the recording does not have."""


def frozen_readings(job_dir: Path | str) -> dict:
    """Las lecturas de la regla CONGELADAS en el plan aprobado de un trabajo, verificadas.

    Volver a juzgar una corrida grabada tiene que usar la lectura con la que se
    aprobó, no pedir otra: sobre evidencia congelada el juez cambió el
    veredicto en 23 de 43 criterios (2026-11-27), casi siempre por leer la
    regla distinto. Esto lee `evidence/approved_plan.json` (la instantánea que
    el ledger hashea) y devuelve criterio -> lectura. Una lectura cuya huella
    no coincide con sus campos fue tocada después de aprobar: no se usa, falla
    en voz alta, igual que una grabación que ya no describe la prueba.

    Un trabajo sin lecturas congeladas (anterior al contrato, o con el
    contrato apagado) devuelve {}: ahí no hay nada que reproducir.
    """
    import json

    from . import lectura as lectura_mod

    path = Path(job_dir) / "evidence" / "approved_plan.json"
    if not path.exists():
        return {}
    plan = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for cid, entry in (plan.get("lecturas") or {}).items():
        if not entry.get("lectura"):
            continue
        try:
            intact = lectura_mod.reading_hash(entry["lectura"]) == entry.get("hash_lectura")
        except lectura_mod.ReadingUnavailable:
            intact = False      # le falta la pregunta o un extremo: tampoco es la que se aprobó
        if not intact:
            raise ReplayError(
                f"la lectura congelada de {cid} no coincide con su huella: fue "
                "cambiada después de aprobar el plan. No se puede reproducir el "
                "juicio con ella."
            )
        out[cid] = lectura_mod.RuleReading.from_dict(entry["lectura"])
    return out


def steps_as_run(rows: list[dict], probe_id: str, *, since: str = "", until: str = "") -> list[str]:
    """The steps a probe ACTUALLY executed, read back out of the ledger.

    Every capture row carries "paso {n}: {step}" in its note, so a finished run
    describes itself. That is the only surviving record of what was driven when
    a probe was rewritten by hand and never saved back to the job.
    """
    driver = ReplayDriver(rows, probe_id=probe_id, since=since, until=until)
    return driver.recorded_steps


@dataclass
class _Capture:
    run_index: int
    step_index: int
    step_text: str
    url: str = ""
    screenshot: Path | None = None
    page_text: Path | None = None
    truncated: bool = False
    full_chars: int = 0


@dataclass
class ReplayDriver:
    """The runner's driver seam, served from a ledger instead of a browser.

    Implements exactly the surface `runner._run_once` touches -- `do`,
    `assert_session_alive`, `is_logged_out`, `url`, `screenshot`, `read_page`,
    and the two truncation properties -- and nothing else. The seam is small on
    purpose; it is the whole reason this is possible.
    """

    rows: list[dict]
    probe_id: str
    root: Path | str = "."
    logged_out: bool = False
    #: Name WHICH recording to replay. The ledger is append-only, so a probe
    #: re-run after a fix has two captures of the same step under the same
    #: run_index, and picking between them is not a decision this class gets to
    #: make silently -- PB-PT-D8-01 has 27 truncated captures and 27 healthy
    #: ones, and defaulting to the wrong half replays a session that was thrown
    #: away. Same instrument as `ledger.rows_since`, aimed by hand.
    since: str = ""
    until: str = ""

    captures: list[_Capture] = field(default_factory=list, init=False)
    cursor: int = field(default=-1, init=False)
    steps_run: int = field(default=0, init=False)
    #: How many captures were superseded by a later one of the same step. Never
    #: zero silently: a non-zero count means the recording is ambiguous and the
    #: caller should pass `since` rather than trust what came back.
    shadowed: int = field(default=0, init=False)
    #: Replay costs nothing. That is not trivia -- a loop that bills is a loop
    #: nobody runs on every change, which is how we got here.
    spend_usd: float = field(default=0.0, init=False)
    cache_read_tokens: int = field(default=0, init=False)
    cache_write_tokens: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # Rows in ledger order, so a later capture of a step supersedes an
        # earlier one: re-capturing a step is what a person does after fixing
        # what was wrong with the first attempt. `shadowed` counts the ones
        # that lost, because the caller has to be able to see the ambiguity.
        by_step: dict[tuple[int, int], _Capture] = {}
        seen_at: dict[tuple[int, int], str] = {}
        for row in sorted(self.rows, key=lambda r: str(r.get("timestamp") or "")):
            if row.get("probe_id") != self.probe_id:
                continue
            stamp = str(row.get("timestamp") or "")
            if self.since and stamp < self.since:
                continue
            if self.until and stamp >= self.until:
                continue
            parsed = _NOTE.match(str(row.get("note") or ""))
            if not parsed:
                continue
            step_index = int(parsed.group(1))
            text = parsed.group(2)
            trunc = _TRUNCATED.search(text)
            text = _TRUNCATED.sub("", text).strip()

            run_index = int(row.get("run_index", 0) or 0)
            key = (run_index, step_index)
            # A new capture SESSION for a step already captured: start fresh so
            # a healthy re-capture does not inherit the discarded one's text.
            if key in by_step and stamp != seen_at.get(key):
                self.shadowed += 1
                del by_step[key]
            seen_at[key] = stamp
            cap = by_step.setdefault(
                key, _Capture(run_index=run_index, step_index=step_index, step_text=text)
            )
            cap.url = cap.url or str(row.get("url") or "")
            artifact = self._resolve(str(row.get("artifact") or ""))
            if row.get("kind") == "screenshot":
                cap.screenshot = artifact
            elif row.get("kind") == "page_text":
                cap.page_text = artifact
                cap.truncated = bool(trunc)
                cap.full_chars = int(trunc.group(1)) if trunc else 0
        self.captures = [by_step[k] for k in sorted(by_step)]

    def _resolve(self, artifact: str) -> Path:
        p = Path(artifact)
        return p if p.is_absolute() else Path(self.root) / artifact

    @classmethod
    def from_job_dir(cls, job_dir: Path | str, *, probe_id: str, **kw) -> "ReplayDriver":
        import json

        path = Path(job_dir) / "evidence" / "ledger.jsonl"
        rows = [
            json.loads(l)
            for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        return cls(rows, probe_id=probe_id, **kw)

    @property
    def recorded_steps(self) -> list[str]:
        """The steps this recording is a recording OF, in order, first run."""
        return [c.step_text for c in self.captures if c.run_index == self.captures[0].run_index]

    @property
    def _current(self) -> _Capture:
        if not 0 <= self.cursor < len(self.captures):
            raise RecordingExhausted(
                f"la grabación de {self.probe_id} no tiene un paso en la posición "
                f"{self.cursor + 1}"
            )
        return self.captures[self.cursor]

    # --- the seam ----------------------------------------------------------

    async def do(self, step: str) -> None:
        """Advance to the next recorded capture, refusing if it is not this step."""
        self.cursor += 1
        if self.cursor >= len(self.captures):
            raise RecordingExhausted(
                f"el probe {self.probe_id} pide el paso {self.cursor + 1} "
                f"({step!r}) y la grabación tiene {len(self.captures)}. Volvé a "
                "grabar contra la plataforma: una grabación más corta que el "
                "probe no puede decir qué habría pasado."
            )
        recorded = self.captures[self.cursor]
        if recorded.step_text != step.strip():
            raise ReplayDrift(
                f"el probe {self.probe_id} cambió respecto de su grabación. "
                f"En el paso {recorded.step_index} la grabación hizo "
                f"{recorded.step_text!r} y el probe ahora pide {step.strip()!r}. "
                "La evidencia grabada no es sobre este probe: volvé a grabar."
            )
        self.steps_run += 1

    async def assert_session_alive(self) -> None:
        return None

    async def is_logged_out(self) -> bool:
        return self.logged_out

    async def url(self) -> str:
        return self._current.url

    async def screenshot(self, label: str = "snap", *, full_page: bool = True) -> Path:
        cap = self._current
        if cap.screenshot is None:
            raise RecordingExhausted(
                f"la grabación de {self.probe_id} no tiene captura para el paso "
                f"{cap.step_index}"
            )
        return cap.screenshot

    async def read_page(self, max_chars: int | None = 8000) -> str:
        """The recorded text, verbatim.

        `max_chars` is accepted and ignored: the runner passes None here
        precisely so the RECORD is never truncated, and re-applying a cap on
        replay would manufacture a defect the recording does not contain.
        """
        cap = self._current
        if cap.page_text is None:
            raise RecordingExhausted(
                f"la grabación de {self.probe_id} no tiene texto para el paso "
                f"{cap.step_index}"
            )
        return cap.page_text.read_text(encoding="utf-8", errors="replace")

    @property
    def last_read_truncated(self) -> bool:
        """Reproduce the defect, never launder it.

        A recording whose capture was cut short replays as cut short, so the
        new ledger row admits it exactly as the original did.
        """
        return self.captures[self.cursor].truncated if 0 <= self.cursor < len(self.captures) else False

    @property
    def last_read_full_chars(self) -> int:
        return self.captures[self.cursor].full_chars if 0 <= self.cursor < len(self.captures) else 0
