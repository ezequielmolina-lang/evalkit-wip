"""Probe loader + coverage-gate tests.

A probe is one concrete, executable check against a platform. Its ids are join
keys, so they follow the same reject-don't-coerce rule as criteria, and its
errors use the same structured shape. The coverage gate is the anti-partial-
coverage mechanism: every criteria test must have at least one probe, or a test
silently goes unscored.
"""
from pathlib import Path

import pytest

from lxp_eval.evalkit.criteria import Criteria, CriterionDomain, CriterionTest
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.probes import (
    DEFAULT_CAPTURE,
    Probe,
    ProbeError,
    load_probes,
    validate_against_criteria,
)

PROBES_OK = """- probe_id: "P1"
  test_id: "2.1.2"
  goal: Ver si la lección modela antes de la práctica
  steps:
    - Abrir una lección
    - Recorrer sus secciones en orden
  verdict_rule: PASA si hay un ejemplo resuelto antes de la práctica
- probe_id: "P2"
  test_id: "2.1.4"
  goal: Ver si existe práctica independiente
  steps:
    - Buscar una sección donde el alumno resuelve solo
  capture:
    - screenshot
  verdict_rule: PASA si existe esa sección
  repeats: 3
"""


PROBES_JUDGING = """- probe_id: "P1"
  test_id: "2.1.2"
  goal: Ver si el tutor confabula
  steps:
    - Preguntarle por un dato que no existe
  verdict_rule: FALLA si inventa una fuente
  repeat_judging: 3
"""


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "probes.yaml"
    f.write_text(text, encoding="utf-8")
    return f


def _criteria(*test_ids: str) -> Criteria:
    """A Criteria with one domain and one 1-point test per id."""
    tests = [
        CriterionTest(
            id=tid,
            name=f"test {tid}",
            points=1,
            verification="v",
            pass_criteria="p",
            fail_criteria="f",
        )
        for tid in test_ids
    ]
    return Criteria(
        name="c",
        domains=[CriterionDomain(code="D2", name="n", points=len(tests), tests=tests)],
    )


# --- structure ------------------------------------------------------------


def test_probe_error_is_an_evalkit_error_not_a_criteria_error():
    # a wrong parent would be invisible: the CLI's `except EvalkitError` still
    # catches it, so nothing fails until someone catches CriteriaError alone.
    from lxp_eval.evalkit.criteria import CriteriaError

    assert issubclass(ProbeError, EvalkitError)
    assert not issubclass(ProbeError, CriteriaError)


def test_loads_a_probe(tmp_path: Path):
    probes = load_probes(_write(tmp_path, PROBES_OK))
    assert [p.probe_id for p in probes] == ["P1", "P2"]
    p1 = probes[0]
    assert p1.test_id == "2.1.2"
    assert p1.steps == ["Abrir una lección", "Recorrer sus secciones en orden"]
    assert isinstance(p1, Probe)


def test_capture_defaults_when_absent(tmp_path: Path):
    probes = load_probes(_write(tmp_path, PROBES_OK))
    assert probes[0].capture == list(DEFAULT_CAPTURE)  # P1 omitted capture
    assert probes[1].capture == ["screenshot"]  # P2 set it


def test_repeats_defaults_to_one(tmp_path: Path):
    probes = load_probes(_write(tmp_path, PROBES_OK))
    assert probes[0].repeats == 1  # P1 omitted repeats
    assert probes[1].repeats == 3  # P2 set it


# --- loader validation ----------------------------------------------------


def test_file_must_be_a_list(tmp_path: Path):
    bad = "probe_id: P1\ntest_id: X\n"  # a mapping, not a list
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "lista" in str(e.value)


def test_rejects_duplicate_probe_id(tmp_path: Path):
    bad = PROBES_OK.replace('probe_id: "P2"', 'probe_id: "P1"')
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    msg = str(e.value)
    assert '"P1"' in msg
    assert "está repetido" in msg
    assert "línea 1" in msg  # points back at the first P1


def test_rejects_empty_steps_list(tmp_path: Path):
    bad = PROBES_OK.replace(
        "  steps:\n    - Buscar una sección donde el alumno resuelve solo\n",
        "  steps: []\n",
    )
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "no tiene pasos" in str(e.value)


def test_rejects_null_steps(tmp_path: Path):
    bad = PROBES_OK.replace(
        "  steps:\n    - Buscar una sección donde el alumno resuelve solo\n",
        "  steps:\n",
    )
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "no tiene pasos" in str(e.value)


def test_rejects_missing_required_field(tmp_path: Path):
    bad = PROBES_OK.replace(
        "  verdict_rule: PASA si hay un ejemplo resuelto antes de la práctica\n",
        "",
    )
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "verdict_rule" in str(e.value)


def test_rejects_unquoted_probe_id(tmp_path: Path):
    # `probe_id: 1.10` -> YAML float 1.1; same corruption as criteria ids
    bad = PROBES_OK.replace('probe_id: "P1"', "probe_id: 1.10")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    msg = str(e.value)
    assert 'escribí probe_id: "1.10", no probe_id: 1.10' in msg


def test_rejects_unquoted_test_id(tmp_path: Path):
    bad = PROBES_OK.replace('test_id: "2.1.2"', "test_id: 2.10")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert 'escribí test_id: "2.10", no test_id: 2.10' in str(e.value)


def test_repeats_must_be_an_int(tmp_path: Path):
    bad = PROBES_OK.replace("  repeats: 3\n", "  repeats: dos\n")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    msg = str(e.value)
    assert "repeats" in msg
    assert "entero" in msg


def test_repeats_must_be_at_least_one(tmp_path: Path):
    bad = PROBES_OK.replace("  repeats: 3\n", "  repeats: 0\n")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "repeats" in str(e.value)


def test_repeats_rejects_boolean(tmp_path: Path):
    # YAML `yes` parses to True, and bool is an int subclass in Python -- it
    # must not sneak through the >= 1 check as a stealth 1.
    bad = PROBES_OK.replace("  repeats: 3\n", "  repeats: yes\n")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, bad))
    assert "repeats" in str(e.value)


# --- the coverage gate (anti partial-coverage) ----------------------------


def test_unknown_test_id_is_rejected():
    probes = [
        Probe(probe_id="P1", test_id="9.9.9", goal="g", steps=["s"], verdict_rule="v")
    ]
    with pytest.raises(ProbeError) as e:
        validate_against_criteria(probes, _criteria("2.1.2"))
    msg = str(e.value)
    assert '"P1"' in msg
    assert "9.9.9" in msg


def test_uncovered_test_is_rejected():
    # criteria has two tests; only one is probed. The other cannot be scored.
    probes = [
        Probe(probe_id="P1", test_id="2.1.2", goal="g", steps=["s"], verdict_rule="v")
    ]
    with pytest.raises(ProbeError) as e:
        validate_against_criteria(probes, _criteria("2.1.2", "2.1.4"))
    msg = str(e.value)
    assert "2.1.4" in msg
    assert "2.1.2" not in msg  # the covered one is not named


def test_full_coverage_passes():
    probes = [
        Probe(probe_id="P1", test_id="2.1.2", goal="g", steps=["s"], verdict_rule="v"),
        Probe(probe_id="P2", test_id="2.1.4", goal="g", steps=["s"], verdict_rule="v"),
    ]
    # must not raise
    assert validate_against_criteria(probes, _criteria("2.1.2", "2.1.4")) is None


# --- repeat_judging: the per-probe knob that replaces a hardcoded cell list --
# Country-specific rubric ids compiled into the generic engine (replicate.py's
# CONFAB_CELLS = ("PT-D9-H1","PT-D9-H2")) mean another country's rubric matches
# nothing and its repeat behaviour silently collapses to 1. The author of the
# probe is who knows a test is non-deterministic, so the knob belongs on the
# probe. This is exposed for the scoring side to migrate onto; probes.py only
# validates and carries it.


def test_repeat_judging_defaults_to_one(tmp_path: Path):
    probes = load_probes(_write(tmp_path, PROBES_OK))
    assert probes[0].repeat_judging == 1


def test_repeat_judging_is_parsed(tmp_path: Path):
    probes = load_probes(_write(tmp_path, PROBES_JUDGING))
    assert probes[0].repeat_judging == 3
    assert probes[0].repeats == 1  # independent of platform re-probing


def test_repeat_judging_is_independent_of_repeats(tmp_path: Path):
    text = PROBES_JUDGING.replace("repeat_judging: 3", "repeats: 2\n  repeat_judging: 5")
    probe = load_probes(_write(tmp_path, text))[0]
    assert (probe.repeats, probe.repeat_judging) == (2, 5)


@pytest.mark.parametrize("bad", ["yes", "0", "-1", "tres", "1.5"])
def test_repeat_judging_rejects_a_non_positive_integer(tmp_path: Path, bad):
    # `repeat_judging: yes` parses to True, and bool subclasses int, so a plain
    # isinstance(int) check would silently read it as 1 -- same trap as repeats.
    text = PROBES_JUDGING.replace("repeat_judging: 3", f"repeat_judging: {bad}")
    with pytest.raises(ProbeError) as e:
        load_probes(_write(tmp_path, text))
    assert "repeat_judging" in str(e.value)
