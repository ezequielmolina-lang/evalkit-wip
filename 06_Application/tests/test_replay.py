"""Replaying a recorded run through the real runner, with no browser."""
from __future__ import annotations

import json

import pytest

from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.replay import RecordingExhausted, ReplayDrift, ReplayDriver
from lxp_eval.evalkit.runner import run_probe

PLATFORM = "app.plataforma-a.ejemplo"
STEPS = ["abrir el curso de matematica", "hacer clic en el tutor"]


def _recording(tmp_path, *, steps=STEPS, run_index=0, truncated=False, probe_id="PB-PT-D8-01"):
    """A ledger as a real run leaves it: one screenshot + one page_text per step."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, step in enumerate(steps, start=1):
        shot = tmp_path / f"{probe_id}_r{run_index}_s{i}.png"
        shot.write_bytes(b"\x89PNG-falso")
        txt = tmp_path / f"{probe_id}_r{run_index}_s{i}.txt"
        txt.write_text(f"texto de la pagina en el paso {i}", encoding="utf-8")
        note = f"paso {i}: {step}"
        for kind, artifact in (("screenshot", shot), ("page_text", txt)):
            rows.append({
                "kind": kind, "probe_id": probe_id, "test_id": probe_id.replace("PB-", ""),
                "platform": PLATFORM, "run_index": run_index,
                "url": f"https://plataforma_a/paso{i}", "artifact": str(artifact),
                "sha256": "x",
                "note": note + (
                    " [TEXTO TRUNCADO: se guardaron los primeros caracteres de 24213 en total]"
                    if truncated and kind == "page_text" else ""
                ),
                "timestamp": f"2026-07-20T16:3{i}:00+00:00",
            })
    return rows


def _probe(steps=STEPS, probe_id="PB-PT-D8-01"):
    return Probe(
        probe_id=probe_id, test_id=probe_id.replace("PB-", ""),
        goal="ver el tutor", steps=list(steps), verdict_rule="cumple si responde",
    )


@pytest.mark.asyncio
async def test_a_recorded_run_replays_through_the_real_runner(tmp_path):
    """The whole point: `run_probe` untouched, browser absent, seconds not hours."""
    rows = _recording(tmp_path)
    fresh = EvidenceLedger(tmp_path / "nuevo" / "ledger.jsonl")
    driver = ReplayDriver(rows, probe_id="PB-PT-D8-01")

    result = await run_probe(
        _probe(), driver, fresh,
        platform=PLATFORM, run_index=0, text_dir=tmp_path / "nuevo" / "page_text",
    )

    assert result.probe_id == "PB-PT-D8-01"
    written = fresh.read_all()
    assert [r["kind"] for r in written] == ["screenshot", "page_text"] * 2
    assert [r["url"] for r in written[:2]] == ["https://plataforma_a/paso1"] * 2
    # the replayed text is the RECORDED text, not an invention
    replayed = [r for r in written if r["kind"] == "page_text"]
    saved = (tmp_path / "nuevo" / "page_text")
    bodies = sorted(p.read_text(encoding="utf-8") for p in saved.iterdir())
    assert bodies == ["texto de la pagina en el paso 1", "texto de la pagina en el paso 2"]
    assert len(replayed) == 2


@pytest.mark.asyncio
async def test_a_rewritten_step_is_drift_not_stale_evidence(tmp_path):
    """The guard that makes replay honest.

    A recording describes the steps that produced it. Serve it for a probe
    whose steps were rewritten and the run looks green while the evidence is
    about something else -- the same lie as a council approving a probe it
    never read.
    """
    rows = _recording(tmp_path)
    driver = ReplayDriver(rows, probe_id="PB-PT-D8-01")
    rewritten = _probe(["abrir el curso de LENGUAJE", "hacer clic en el tutor"])

    with pytest.raises(ReplayDrift) as exc:
        await run_probe(
            rewritten, driver, EvidenceLedger(tmp_path / "n" / "l.jsonl"),
            platform=PLATFORM, text_dir=tmp_path / "n" / "t",
        )

    assert "paso 1" in str(exc.value)
    assert "LENGUAJE" in str(exc.value)


@pytest.mark.asyncio
async def test_a_probe_longer_than_its_recording_says_so(tmp_path):
    rows = _recording(tmp_path, steps=["un solo paso"])
    driver = ReplayDriver(rows, probe_id="PB-PT-D8-01")

    with pytest.raises(RecordingExhausted):
        await run_probe(
            _probe(["un solo paso", "un paso que nadie grabo"]), driver,
            EvidenceLedger(tmp_path / "n" / "l.jsonl"),
            platform=PLATFORM, text_dir=tmp_path / "n" / "t",
        )


@pytest.mark.asyncio
async def test_replay_is_free_and_says_so(tmp_path):
    """A loop that costs money is a loop nobody runs on every change."""
    driver = ReplayDriver(_recording(tmp_path), probe_id="PB-PT-D8-01")

    await run_probe(
        _probe(), driver, EvidenceLedger(tmp_path / "n" / "l.jsonl"),
        platform=PLATFORM, text_dir=tmp_path / "n" / "t",
    )

    assert driver.spend_usd == 0.0
    assert driver.steps_run == len(STEPS)


@pytest.mark.asyncio
async def test_a_truncated_recording_replays_as_truncated(tmp_path):
    """Replay must reproduce the defect, not launder it.

    64 of job d19ddd2bfdfc4a90's rows were captured truncated. A replay that
    served them as healthy text would hide the exact bug the recording proves.
    """
    rows = _recording(tmp_path, truncated=True)
    fresh = EvidenceLedger(tmp_path / "n" / "l.jsonl")
    driver = ReplayDriver(rows, probe_id="PB-PT-D8-01")

    await run_probe(_probe(), driver, fresh, platform=PLATFORM,
                    text_dir=tmp_path / "n" / "t")

    notes = [r["note"] for r in fresh.read_all() if r["kind"] == "page_text"]
    assert all("TEXTO TRUNCADO" in n for n in notes)
    assert driver.last_read_full_chars == 24213


@pytest.mark.asyncio
async def test_the_logged_out_gate_still_applies_in_replay(tmp_path):
    """A recording of a login wall must not become evidence on replay either."""
    rows = _recording(tmp_path)
    driver = ReplayDriver(rows, probe_id="PB-PT-D8-01", logged_out=True)

    from lxp_eval.evalkit.session import SessionExpired

    with pytest.raises(SessionExpired):
        await run_probe(
            _probe(), driver, EvidenceLedger(tmp_path / "n" / "l.jsonl"),
            platform=PLATFORM, text_dir=tmp_path / "n" / "t",
        )


def test_a_recaptured_step_supersedes_the_attempt_it_replaced(tmp_path):
    """PB-PT-D8-01 has two captures of every step: 27 truncated, then 27 healthy.

    Both sit under the same run_index because the re-capture was driven by hand,
    so the recording is genuinely ambiguous. The later capture wins -- that is
    what re-capturing a step means -- and `shadowed` is non-zero so the
    ambiguity is never invisible to the caller.
    """
    first = _recording(tmp_path / "a", truncated=True)
    (tmp_path / "b").mkdir()
    second = _recording(tmp_path / "b", truncated=False)
    for r in second:  # the re-capture happened later
        r["timestamp"] = r["timestamp"].replace("T16:", "T18:")

    driver = ReplayDriver(first + second, probe_id="PB-PT-D8-01")

    assert len(driver.captures) == len(STEPS)
    assert not any(c.truncated for c in driver.captures), "gana la recaptura sana"
    assert driver.shadowed == len(STEPS)


def test_a_window_picks_the_session_by_hand(tmp_path):
    first = _recording(tmp_path / "a", truncated=True)
    (tmp_path / "b").mkdir()
    second = _recording(tmp_path / "b", truncated=False)
    for r in second:
        r["timestamp"] = r["timestamp"].replace("T16:", "T18:")

    old = ReplayDriver(first + second, probe_id="PB-PT-D8-01", until="2026-07-20T17:00")

    assert all(c.truncated for c in old.captures)
    assert old.shadowed == 0


def test_it_can_be_built_straight_from_a_job_directory(tmp_path):
    """The ergonomics that decide whether anyone actually uses this."""
    rows = _recording(tmp_path)
    d = tmp_path / "job"
    (d / "evidence").mkdir(parents=True)
    (d / "evidence" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )

    driver = ReplayDriver.from_job_dir(d, probe_id="PB-PT-D8-01")

    assert driver.recorded_steps == STEPS
