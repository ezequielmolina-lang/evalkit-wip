"""The pre-authored probe bank, and how a platform's surfaces complete it."""
from pathlib import Path

import pytest

from lxp_eval.evalkit.probe_bank import (
    ProbeBank,
    SurfaceUnavailable,
    load_surfaces,
)

BANKS = Path(__file__).resolve().parents[1] / "lxp_eval/evalkit/banks"
PLATAFORMA_A = Path(__file__).resolve().parents[1] / "platforms/plataforma_a/surfaces.yaml"


def test_navigation_comes_from_the_platform_not_the_probe():
    """The same three navigation steps were hand-copied into eight probes.

    Navigation belongs to the platform and serves every criterion that needs
    that surface; the stimulus belongs to the criterion and travels between
    platforms. Composing them at load time is what keeps a UI change to one
    file instead of eight.
    """
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    surfaces = load_surfaces(PLATAFORMA_A)

    steps = bank.steps_for("PT-D9-H2", surfaces)

    assert any("BM - Matematicas Grado 3" in s for s in steps), "navigation missing"
    assert any("boton circular violeta" in s for s in steps), "surface path missing"
    assert any("89+47=126" in s for s in steps), "the stimulus must survive verbatim"
    assert steps[-1].startswith("Esperar"), "a probe ends by reading the answer"
    # nothing addressed to a person, and no precondition an agent cannot meet
    joined = " ".join(steps).lower()
    assert "antes de correr el probe" not in joined
    assert "anotar el grado" not in joined


def test_a_sequence_becomes_one_send_and_one_read_per_stimulus():
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    surfaces = load_surfaces(PLATAFORMA_A)

    steps = bank.steps_for("PT-D4-02", surfaces)

    for s in ("27 + 45 = 62", "6 x 4 = 28", "52 - 27 = 35"):
        assert any(s in step for step in steps), f"{s} missing"
    assert sum(1 for s in steps if s.startswith("Esperar")) == 3


def test_an_unavailable_surface_refuses_instead_of_substituting():
    """Declaring what the account cannot reach beats discovering it mid-run.

    PT-D9-M2 wants 9th grade and the tenant has only 3rd and 6th. The honest
    outcome is NO EVALUABLE with the reason, not a quiet substitution that
    scores the criterion against a level it was never about.
    """
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    surfaces = load_surfaces(PLATAFORMA_A)

    with pytest.raises(SurfaceUnavailable) as exc:
        bank.steps_for("PT-D9-M2", surfaces)
    assert "9" in str(exc.value)


def test_a_gated_probe_says_so_before_anyone_runs_it():
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    entry = bank.entry("PT-D9-S1")
    assert entry["requires_signoff"] is True
    assert entry["risk"] == "crisis"


def test_every_entry_has_countable_verdict_anchors():
    """«El tutor responde apropiadamente» is not a probe: nothing can fail it."""
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    for cid, e in bank.entries.items():
        assert e.get("expect", {}).get("cumple"), f"{cid} has no CUMPLE anchor"
        assert e.get("expect", {}).get("no_cumple"), f"{cid} has no NO_CUMPLE anchor"
        # A probe either sends a stimulus or reads a surface. One or the other.
        assert (
            e.get("stimulus") or e.get("stimulus_sequence") or e.get("read_only")
        ), f"{cid} neither sends nor reads"


def test_a_read_only_probe_still_reads_the_surface():
    """Not every probe sends a message. Reading a lesson is a probe too.

    `steps_for` appended the surface's `read` only inside the per-stimulus
    loop, so a criterion that just navigates and reads («does the lesson open
    with retrieval?») produced navigation steps and then stopped — it never
    captured the thing under evaluation. A surface with a top-level `read` must
    run it once even when nothing is sent.
    """
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    surfaces = load_surfaces(PLATAFORMA_A)

    steps = bank.steps_for("PT-D3-01", surfaces)

    assert any("primera leccion" in s.lower() for s in steps), "navigation missing"
    assert steps[-1].lower().startswith("leer"), "a read-only probe must end by reading"
    # and it sent nothing
    assert not any("escribir exactamente" in s for s in steps)


def test_a_privacy_probe_reads_the_published_policy_not_the_app():
    """CSP criteria are about what the published policy DECLARES, not app behaviour.

    The surface must not walk the logged-in app; it navigates to the vendor's
    public site and finds the policy. And it must not hardcode a deep URL — a
    guessed 404 read as if it were the policy is exactly the garbage-evidence
    failure this session kept hitting — so it enters at the known root and looks
    for the link, confirming the page is really a policy before reading.
    """
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    surfaces = load_surfaces(PLATAFORMA_A)

    steps = bank.steps_for("CSP-01", surfaces)

    assert any("plataforma-a.ejemplo" in s for s in steps), "must enter at the known root"
    assert any("politica de privacidad" in s.lower() for s in steps), "must find the policy"
    assert any("no continuar" in s.lower() for s in steps), "must confirm it is a policy first"
    assert steps[-1].lower().startswith("leer"), "ends by reading the policy"
    # read-only: it sends nothing
    assert not any("escribir exactamente" in s for s in steps)


def test_silence_in_a_policy_does_not_pass_a_privacy_criterion():
    """A policy that says nothing about selling data does not «declare» it won't.

    Absence must map to NO_CUMPLE, never CUMPLE — a passing verdict on silence
    would let a vendor score clean by simply not addressing the question.
    """
    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    for cid in [f"CSP-0{n}" for n in range(1, 7)]:
        e = bank.entry(cid)
        nc = (e.get("expect") or {}).get("no_cumple", "").lower()
        assert "no menciona" in nc or "silencio" in nc or "no aborda" in nc, (
            f"{cid} must fail on a silent policy, not pass"
        )
