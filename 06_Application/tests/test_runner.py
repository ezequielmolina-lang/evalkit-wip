"""Probe-runner tests. No real browser — the driver is faked.

The load-bearing property: a logged-out page must never produce evidence. The
session check runs BEFORE any capture, so an expired session writes zero ledger
rows rather than a screenshot of a login screen.
"""
from pathlib import Path

import pytest

from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.runner import ProbeRun, run_probe
from lxp_eval.evalkit.session import SessionExpired


class FakeDriver:
    """Implements the narrow interface the runner needs.

    `dies_at_step` makes the session drop partway through, the way a real one
    does: the first N-1 steps are fine and step N lands on a login wall.
    """

    def __init__(
        self,
        *,
        alive=True,
        page_text="contenido de la lección",
        shots_dir=None,
        dies_at_step=None,
        logged_out_at_step=None,
    ):
        self._alive = alive
        self._page_text = page_text
        self._shots_dir = Path(shots_dir) if shots_dir else None
        self._dies_at_step = dies_at_step
        self._logged_out_at_step = logged_out_at_step
        self.done_steps: list[str] = []
        self.session_checks = 0
        self.shots: list[str] = []
        self.reads = 0
        self.last_read_truncated = False
        self.last_read_full_chars = 0

    def _step_no(self):
        return len(self.done_steps)

    async def assert_session_alive(self):
        self.session_checks += 1
        dead = not self._alive or (
            self._dies_at_step is not None and self._step_no() >= self._dies_at_step
        )
        if dead:
            raise SessionExpired("la sesión expiró: la página está en /login")

    async def is_logged_out(self):
        return (
            self._logged_out_at_step is not None
            and self._step_no() >= self._logged_out_at_step
        )

    async def do(self, step):
        self.done_steps.append(step)

    async def screenshot(self, name):
        self.shots.append(name)
        path = self._shots_dir / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path

    async def read_page(self, max_chars=8000):
        self.reads += 1
        return self._page_text

    async def url(self):
        return "https://app.test/lesson/5"


def _probe(
    steps=("goto https://app.test/lesson/5", "click Tutor"),
    capture=("screenshot", "page_text"),
    repeats=1,
):
    return Probe(
        probe_id="P-1",
        test_id="2.1.2",
        goal="ver si modela antes de soltar",
        steps=list(steps),
        verdict_rule="PASA si hay ejemplo resuelto",
        capture=list(capture),
        repeats=repeats,
    )


def _ledger(tmp_path):
    return EvidenceLedger(tmp_path / "ledger.jsonl")


@pytest.mark.asyncio
async def test_expired_session_writes_no_evidence_at_all(tmp_path: Path):
    # THE guard: the check happens before any step or capture.
    driver = FakeDriver(alive=False, shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    with pytest.raises(SessionExpired):
        await run_probe(_probe(), driver, ledger, platform="Plataforma A")
    assert ledger.read_all() == []  # zero rows
    assert driver.done_steps == []  # never even ran a step
    assert driver.shots == []


@pytest.mark.asyncio
async def test_checks_session_before_running_steps(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    await run_probe(_probe(), driver, _ledger(tmp_path), platform="Plataforma A")
    assert driver.session_checks >= 1


# --- #1: the session is re-checked BETWEEN steps, not once per probe ------


@pytest.mark.asyncio
async def test_session_is_rechecked_after_every_step(tmp_path: Path):
    # Once-per-probe is the defect: a 12-step probe that loses its session at
    # step 4 kept going and screenshotted the login wall eleven more times.
    driver = FakeDriver(shots_dir=tmp_path)
    await run_probe(
        _probe(steps=("a", "b", "c")), driver, _ledger(tmp_path), platform="Plataforma A"
    )
    # once up front + once after each of the three steps
    assert driver.session_checks == 4


@pytest.mark.asyncio
async def test_a_session_lost_mid_probe_writes_no_evidence_from_that_step_on(tmp_path: Path):
    """THE confidently-wrong-evidence mechanism. Steps 1-3 are real; the session
    dies at step 4; steps 4-8 must produce nothing at all rather than five
    hashed, confidently-noted screenshots of a login screen."""
    driver = FakeDriver(shots_dir=tmp_path, dies_at_step=4)
    ledger = _ledger(tmp_path)
    steps = [f"paso {i}" for i in range(1, 9)]

    with pytest.raises(SessionExpired):
        await run_probe(_probe(steps=steps), driver, ledger, platform="Plataforma A")

    rows = ledger.read_all()
    notes = " | ".join(r["note"] for r in rows)
    assert len(rows) == 3 * 2          # only steps 1-3, screenshot + page_text
    for gone in ("paso 4", "paso 5", "paso 6", "paso 7", "paso 8"):
        assert gone not in notes
    # and it stopped driving the browser too
    assert driver.done_steps == ["paso 1", "paso 2", "paso 3", "paso 4"]


@pytest.mark.asyncio
async def test_a_logged_out_page_is_refused_even_if_the_session_check_passes(tmp_path: Path):
    """Belt and braces: if the page trips the logged-out predicate, no ledger
    row is written for it -- whatever assert_session_alive believed."""
    driver = FakeDriver(shots_dir=tmp_path, logged_out_at_step=2)
    ledger = _ledger(tmp_path)

    with pytest.raises(SessionExpired):
        await run_probe(
            _probe(steps=("paso 1", "paso 2", "paso 3")), driver, ledger, platform="Plataforma A"
        )

    rows = ledger.read_all()
    assert len(rows) == 2                       # step 1 only
    assert all("paso 1" in r["note"] for r in rows)


@pytest.mark.asyncio
async def test_a_driver_without_the_predicate_still_works(tmp_path: Path):
    # is_logged_out() is optional on the driver interface.
    class Minimal(FakeDriver):
        is_logged_out = None

    driver = Minimal(shots_dir=tmp_path)
    del Minimal.is_logged_out
    await run_probe(_probe(steps=("x",)), driver, _ledger(tmp_path), platform="Plataforma A")


# --- #7: probe.repeats means REAL re-probing ------------------------------


@pytest.mark.asyncio
async def test_repeats_produces_that_many_runs_with_distinct_run_index(tmp_path: Path):
    """An analyst who writes `repeats: 5` on a crisis probe got ONE run while
    the report implied five platform interactions."""
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)

    run = await run_probe(
        _probe(steps=("abrir el tutor",), capture=("screenshot",), repeats=3),
        driver, ledger, platform="Plataforma A",
    )

    rows = ledger.read_all()
    assert len(rows) == 3                                  # one per run
    assert sorted(r["run_index"] for r in rows) == [0, 1, 2]   # distinct
    assert driver.done_steps == ["abrir el tutor"] * 3      # really re-probed
    assert len(driver.shots) == 3                           # fresh capture each time
    assert len(set(driver.shots)) == 3                      # distinct artifacts
    assert run.repeats == 3
    assert len(run.runs) == 3
    assert len(run.artifacts) == 3


@pytest.mark.asyncio
async def test_repeats_start_from_the_given_run_index(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    await run_probe(
        _probe(steps=("x",), capture=("screenshot",), repeats=2),
        driver, ledger, platform="Plataforma A", run_index=5,
    )
    assert sorted(r["run_index"] for r in ledger.read_all()) == [5, 6]


@pytest.mark.asyncio
async def test_a_session_lost_during_a_later_repeat_keeps_the_earlier_ones(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path, dies_at_step=2)
    ledger = _ledger(tmp_path)
    with pytest.raises(SessionExpired):
        await run_probe(
            _probe(steps=("x",), capture=("screenshot",), repeats=4),
            driver, ledger, platform="Plataforma A",
        )
    rows = ledger.read_all()
    assert [r["run_index"] for r in rows] == [0]   # run 0 survived, 1-3 never happened


# --- #9: truncated page text must say so ----------------------------------


@pytest.mark.asyncio
async def test_truncated_page_text_is_recorded_in_the_ledger(tmp_path: Path):
    """Evidence can be silently incomplete while the hash certifies the
    truncated bytes as 'what was seen'. The row has to admit it."""
    driver = FakeDriver(shots_dir=tmp_path, page_text="recortado")
    driver.last_read_truncated = True
    driver.last_read_full_chars = 24_312
    ledger = _ledger(tmp_path)

    await run_probe(
        _probe(steps=("x",), capture=("page_text",)), driver, ledger, platform="Plataforma A"
    )

    row = ledger.read_all()[0]
    assert "truncado" in row["note"].lower()
    assert "24312" in row["note"] or "24 312" in row["note"]


@pytest.mark.asyncio
async def test_untruncated_page_text_says_nothing_about_truncation(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    await run_probe(
        _probe(steps=("x",), capture=("page_text",)), driver, ledger, platform="Plataforma A"
    )
    assert "truncado" not in ledger.read_all()[0]["note"].lower()


@pytest.mark.asyncio
async def test_walks_every_step_and_captures_both_kinds(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    run = await run_probe(_probe(), driver, ledger, platform="Plataforma A")

    assert driver.done_steps == ["goto https://app.test/lesson/5", "click Tutor"]
    rows = ledger.read_all()
    # 2 steps x (screenshot + page_text)
    assert len(rows) == 4
    assert {r["kind"] for r in rows} == {"screenshot", "page_text"}
    assert {r["platform"] for r in rows} == {"Plataforma A"}
    assert {r["probe_id"] for r in rows} == {"P-1"}
    assert {r["test_id"] for r in rows} == {"2.1.2"}
    assert all(r["url"] == "https://app.test/lesson/5" for r in rows)
    assert all(r["timestamp"] for r in rows)
    assert isinstance(run, ProbeRun)
    assert len(run.artifacts) == 4


@pytest.mark.asyncio
async def test_capture_setting_is_honoured(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    await run_probe(_probe(capture=("page_text",)), driver, ledger, platform="Plataforma A")
    rows = ledger.read_all()
    assert {r["kind"] for r in rows} == {"page_text"}
    assert driver.shots == []  # no screenshots taken at all


@pytest.mark.asyncio
async def test_page_text_artifact_holds_the_text(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path, page_text="el tutor dice hola")
    ledger = _ledger(tmp_path)
    await run_probe(_probe(steps=("goto x",), capture=("page_text",)), driver, ledger, platform="Plataforma A")
    row = ledger.read_all()[0]
    assert Path(row["artifact"]).read_text(encoding="utf-8") == "el tutor dice hola"


@pytest.mark.asyncio
async def test_run_index_is_recorded_for_repeat_runs(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    await run_probe(_probe(steps=("goto x",)), driver, ledger, platform="Plataforma A", run_index=2)
    assert {r["run_index"] for r in ledger.read_all()} == {2}


@pytest.mark.asyncio
async def test_step_note_is_recorded_so_evidence_says_what_produced_it(tmp_path: Path):
    driver = FakeDriver(shots_dir=tmp_path)
    ledger = _ledger(tmp_path)
    await run_probe(_probe(steps=("click Tutor",)), driver, ledger, platform="Plataforma A")
    assert all("click Tutor" in r["note"] for r in ledger.read_all())


@pytest.mark.asyncio
async def test_a_no_session_probe_skips_the_login_wall_check(tmp_path):
    """A public-policy probe leaves the logged-in app on purpose.

    The privacy criteria read the vendor's PUBLIC policy at plataforma_a.com, which has
    none of the dashboard markers that prove a live session — so the session
    gate would kill it as "logged out" the moment it navigated off the app.
    That is the same false-positive that killed a lesson-page probe earlier.
    A probe declared `needs_session=False` must not be session-checked.
    """
    from lxp_eval.evalkit.probes import Probe
    from lxp_eval.evalkit.ledger import EvidenceLedger

    checked = {"count": 0}

    class _Driver:
        async def assert_session_alive(self):
            checked["count"] += 1
        async def is_logged_out(self):
            checked["count"] += 1
            return True                      # a public page looks logged-out
        async def do(self, step): pass
        async def url(self): return "https://plataforma_a.com/privacy"
        async def read_page(self, max_chars=None): return "Privacy Policy ..."
        async def screenshot(self, label="snap", *, full_page=True):
            p = tmp_path / f"{label}.png"; p.write_bytes(b"\x89PNG"); return p

    probe = Probe(
        probe_id="PB-CSP-01", test_id="CSP-01", goal="g",
        steps=["Navegar a plataforma_a.com/privacy", "Leer la politica"],
        verdict_rule="v", capture=["page_text"], needs_session=False,
    )
    ledger = EvidenceLedger(tmp_path / "l.jsonl")
    await run_probe(
        probe, _Driver(), ledger, platform="plataforma_a", run_index=0,
        text_dir=tmp_path / "t",
    )
    assert checked["count"] == 0, "a no-session probe must not be session-gated"


@pytest.mark.asyncio
async def test_identical_probes_capture_once_and_credit_each(tmp_path):
    """Nine criteria that read the SAME lesson should read it once.

    A read-only probe's steps are its whole identity: two probes with identical
    steps land on identical pixels. Running them separately pays N navigations
    and N captures of the same screen — the redundancy that made validating 24
    draft probes cost like 24 when it is really a handful of distinct captures.

    One capture, one artifact file, but a ledger row per criterion pointing at
    it (same sha256). The judge then scores each criterion against the shared
    evidence, which is also more honest: all nine see the same lesson state.
    """
    from lxp_eval.evalkit.probes import Probe
    from lxp_eval.evalkit.ledger import EvidenceLedger

    caps = {"shots": 0, "reads": 0}

    class _Driver:
        async def assert_session_alive(self): pass
        async def is_logged_out(self): return False
        async def do(self, step): pass
        async def url(self): return "https://plataforma_a.com/lesson/1"
        async def read_page(self, max_chars=None):
            caps["reads"] += 1
            return "contenido de la leccion"
        async def screenshot(self, label="snap", *, full_page=True):
            caps["shots"] += 1
            p = tmp_path / f"{label}.png"; p.write_bytes(b"PNGX" + label.encode()); return p

    steps = ["Abrir el curso", "Abrir la primera leccion", "Leer el contenido"]
    a = Probe(probe_id="PB-D1-04", test_id="PT-D1-04", goal="g", steps=steps,
              verdict_rule="v", capture=["screenshot", "page_text"])
    b = Probe(probe_id="PB-D2-02", test_id="PT-D2-02", goal="g", steps=steps,
              verdict_rule="v", capture=["screenshot", "page_text"])
    c = Probe(probe_id="PB-D2-05", test_id="PT-D2-05", goal="g", steps=steps,
              verdict_rule="v", capture=["screenshot", "page_text"])

    ledger = EvidenceLedger(tmp_path / "l.jsonl")
    await run_probe(a, _Driver(), ledger, platform="plataforma_a", run_index=0,
                    text_dir=tmp_path / "t", also_credit=[b, c])

    # three navigation steps, captured once each — NOT three times over
    assert caps["shots"] == 3, "one capture per step, not per criterion"
    rows = ledger.read_all()
    tids = {r["test_id"] for r in rows}
    assert tids == {"PT-D1-04", "PT-D2-02", "PT-D2-05"}, "every criterion must be credited"
    # each credited criterion points at the SAME bytes
    shots = [r for r in rows if r["kind"] == "screenshot"]
    assert len({r["sha256"] for r in shots}) == 3, "3 distinct steps, shared across criteria"
    assert len(shots) == 9, "3 steps x 3 criteria = 9 rows, from 3 files"
