"""Run a probe against a live platform and record what it saw.

This is the automated path: a headless browser driven by a script, no human in
the loop after login. The runner walks a probe's steps, captures the configured
evidence at each one, and appends it to the ledger with full provenance
(platform, probe, test, url, timestamp, and the step that produced it).

The session is checked before the probe starts AND AGAIN AFTER EVERY STEP. Once
per probe is not enough and the failure is not benign: a 12-step probe that
loses its session at step 4 used to walk on to step 12, and steps 5-12 each
screenshotted the login wall, hashed it, and filed it under a real URL with a
confident note ("paso 7: abrir el panel del docente"). Scored later, that is a
confidently wrong verdict against a NAMED VENDOR rather than a missing one.

Belt and braces on top of that: a page that trips the driver's logged-out
predicate never gets a ledger row, whatever the session check believed.

The driver interface is deliberately narrow so tests can fake it:

    await driver.assert_session_alive()
    await driver.do(step)
    await driver.screenshot(name) -> Path
    await driver.read_page()      -> str
    await driver.url()            -> str
    await driver.is_logged_out()  -> bool   (optional)

and two optional attributes let a driver admit it truncated what it read:
`last_read_truncated` / `last_read_full_chars`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .steps import split_marks
from .ledger import EvidenceLedger
from .probes import Probe
from .session import SessionExpired

logger = logging.getLogger(__name__)

SCREENSHOT = "screenshot"
PAGE_TEXT = "page_text"


@dataclass(frozen=True)
class ProbeRun:
    probe_id: str
    test_id: str
    platform: str
    run_index: int
    steps: list[str] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    #: How many times the probe was actually executed (probe.repeats).
    repeats: int = 1
    #: One entry per execution, when repeats > 1. Empty for a single run.
    runs: list["ProbeRun"] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in value)


async def _refuse_if_logged_out(driver: Any, probe: Probe, index: int, step: str) -> None:
    """The belt-and-braces gate: never ledger a row for a login wall.

    `assert_session_alive` is the primary check; this second, independent one
    means a driver whose session check is weak (or whose platform config has no
    authenticated-only selector yet) still cannot file a login screen as
    evidence.
    """
    predicate = getattr(driver, "is_logged_out", None)
    if predicate is None:
        return
    if await predicate():
        raise SessionExpired(
            f"la sesión expiró durante el probe {probe.probe_id}, en el paso "
            f"{index} ({step!r}): la página es una pantalla de login, "
            "así que no se guarda como evidencia"
        )


def _note(
    index: int, step: str, driver: Any = None, *, kind: str = "", skipped: str = ""
) -> str:
    """The ledger note for one capture, including any truncation admission.

    `skipped` es lo que el agente vio cuando un paso [SI APARECE] no aplicó. Va
    en la nota porque la captura que sigue NO muestra el paso hecho: muestra la
    pantalla tal como estaba, y el juez tiene que saberlo para no leerla como
    evidencia de algo que no pasó.
    """
    note = f"paso {index}: {split_marks(step)[0]}"
    if skipped:
        note += f" [NO APARECIÓ, EL PASO NO SE HIZO: {skipped[:400]}]"
    if kind == PAGE_TEXT and getattr(driver, "last_read_truncated", False):
        full = getattr(driver, "last_read_full_chars", 0)
        note += f" [TEXTO TRUNCADO: se guardaron los primeros caracteres de {full} en total]"
    return note


async def _run_once(
    probe: Probe,
    driver: Any,
    ledger: EvidenceLedger,
    *,
    platform: str,
    run_index: int,
    text_dir: Path,
    also_credit: list[Probe] | None = None,
) -> ProbeRun:
    """One execution of the probe. Raises SessionExpired the moment it is unsafe."""
    # BEFORE anything: never let a logged-out page become evidence -- UNLESS
    # the probe is meant to leave the app. A public-policy probe has no session
    # to be alive, and the gate would read the public page as logged-out.
    gated = getattr(probe, "needs_session", True)
    if gated:
        await driver.assert_session_alive()
        await _refuse_if_logged_out(driver, probe, 0, "(antes del primer paso)")

    capture = set(probe.capture)
    artifacts: list[Path] = []
    # Criteria that share this exact capture. Their steps are identical, so the
    # browser is on the same page; each gets a ledger row pointing at the SAME
    # artifact (same sha256) instead of a re-navigation and a re-capture.
    shared = list(also_credit or [])

    def _credit_shared(artifact: Path, url: str, kind: str, index: int, step: str) -> None:
        for other in shared:
            ledger.append(
                probe_id=other.probe_id,
                test_id=other.test_id,
                platform=platform,
                run_index=run_index,
                url=url,
                artifact=artifact,
                kind=kind,
                note=_note(index, step, kind=kind, skipped=skipped.get(index, "")),
                timestamp=_now(),
            )

    #: Pasos [SI APARECE] que no aplicaron: índice -> lo que el agente vio.
    skipped: dict[int, str] = {}

    for index, step in enumerate(probe.steps, start=1):
        outcome = await driver.do(step)
        if isinstance(outcome, str) and outcome:
            skipped[index] = outcome

        # AFTER the step and BEFORE the capture. A step can be what kills the
        # session (a timeout, an idle logout, a role-gated page bouncing us),
        # so checking only at the top of the probe checks the wrong moment.
        # Skipped for a probe that never had a session to lose.
        if gated:
            await driver.assert_session_alive()
            await _refuse_if_logged_out(driver, probe, index, step)

        url = await driver.url()
        stem = f"{_slug(probe.probe_id)}_r{run_index}_s{index}"

        if SCREENSHOT in capture:
            shot = Path(await driver.screenshot(stem))
            ledger.append(
                probe_id=probe.probe_id,
                test_id=probe.test_id,
                platform=platform,
                run_index=run_index,
                url=url,
                artifact=shot,
                kind=SCREENSHOT,
                note=_note(index, step, skipped=skipped.get(index, "")),
                timestamp=_now(),
            )
            _credit_shared(shot, url, SCREENSHOT, index, step)
            artifacts.append(shot)

        if PAGE_TEXT in capture:
            # max_chars=None: the RECORD is never truncated. The cap exists
            # for the agent's context budget, and applying it here silently
            # filed 8000 characters of course sidebar as the evidence for a
            # tutor conversation that had been cut away.
            text = await driver.read_page(max_chars=None)
            text_dir.mkdir(parents=True, exist_ok=True)
            txt = text_dir / page_text_name(
                probe.probe_id, run_index, index, timestamp=_now()
            )
            txt.write_text(text, encoding="utf-8")
            ledger.append(
                probe_id=probe.probe_id,
                test_id=probe.test_id,
                platform=platform,
                run_index=run_index,
                url=url,
                artifact=txt,
                kind=PAGE_TEXT,
                note=_note(index, step, driver, kind=PAGE_TEXT, skipped=skipped.get(index, "")),
                timestamp=_now(),
            )
            _credit_shared(txt, url, PAGE_TEXT, index, step)
            artifacts.append(txt)

    logger.info(
        "probe %s (%s, run %d): %d pasos, %d artefactos",
        probe.probe_id, platform, run_index, len(probe.steps), len(artifacts),
    )
    return ProbeRun(
        probe_id=probe.probe_id,
        test_id=probe.test_id,
        platform=platform,
        run_index=run_index,
        steps=list(probe.steps),
        artifacts=artifacts,
    )


def page_text_name(probe_id: str, run_index: int, step: int, *, timestamp: str) -> str:
    """Filename for one page-text capture. UNIQUE PER CAPTURE, on purpose.

    Screenshots have always carried a timestamp; page text did not, so it was
    `{probe}_r{run}_s{step}.txt` and a re-run of the same probe overwrote the
    previous one in place. The earlier ledger row -- the sentence that says
    "this is what we saw" -- then pointed at bytes from a different run, while
    still carrying the old sha256. Re-hashing a real job caught it: 45 of 46
    rows verified, and the odd one out was text from a run the watchdog killed.

    An append-only ledger over mutable files is not append-only. The timestamp
    is sanitised into the name so two captures of the same step can coexist and
    each stays exactly what its row claims.
    """
    stamp = "".join(c for c in timestamp if c.isalnum())
    return f"{_slug(probe_id)}_r{run_index}_s{step}_{stamp}.txt"


async def run_probe(
    probe: Probe,
    driver: Any,
    ledger: EvidenceLedger,
    *,
    platform: str,
    run_index: int = 0,
    text_dir: Path | None = None,
    also_credit: list[Probe] | None = None,
) -> ProbeRun:
    """Execute one probe `probe.repeats` times, recording evidence per step.

    `repeats` is REAL re-probing: the whole probe runs again, against the live
    platform, with a fresh capture and its own ledger rows under a distinct
    `run_index`. That is what measures the PLATFORM's non-determinism -- a tutor
    that answers differently to the same prompt. (Re-running a judge over frozen
    evidence measures the SCORER's sampling noise instead; different question,
    different module.)

    Raises SessionExpired without writing a row for the step that lost the
    session, or for any step after it.
    """
    text_dir = Path(text_dir) if text_dir else ledger.path.parent / "page_text"
    repeats = max(1, getattr(probe, "repeats", 1) or 1)

    runs: list[ProbeRun] = []
    artifacts: list[Path] = []
    for offset in range(repeats):
        index = run_index + offset
        if repeats > 1:
            logger.info("probe %s: corrida %d de %d", probe.probe_id, offset + 1, repeats)
        one = await _run_once(
            probe, driver, ledger,
            platform=platform, run_index=index, text_dir=text_dir,
            also_credit=also_credit,
        )
        runs.append(one)
        artifacts.extend(one.artifacts)

    return ProbeRun(
        probe_id=probe.probe_id,
        test_id=probe.test_id,
        platform=platform,
        run_index=run_index,
        steps=list(probe.steps),
        artifacts=artifacts,
        repeats=repeats,
        runs=runs if repeats > 1 else [],
    )
