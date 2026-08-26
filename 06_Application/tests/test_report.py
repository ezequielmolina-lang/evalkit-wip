"""Rendering tests for the markdown report.

The scoring core lives in tests/test_scoring.py — report.py only turns verdicts
and DomainScores into text. What is pinned here is what a reader SEES:

* PLATAFORMA_B and Plataforma A are never blended (a column per platform, evidence kept apart);
* every cell states its coverage, so an unobservable domain can never be read
  as a domain that scored badly;
* a percentage is refused when coverage is too thin to support one.
"""
import pytest

from lxp_eval.evalkit.criteria import Criteria, CriterionDomain, CriterionTest
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.report import render_markdown
from lxp_eval.evalkit.scoring import (
    CUMPLE,
    NO_CUMPLE,
    NO_EVALUADO,
    PARCIAL,
    TestVerdict,
    aggregate,
    aggregate_by_platform,
)


def _test(points=2, tid="2.1.2") -> CriterionTest:
    return CriterionTest(
        id=tid,
        name="La plataforma modela antes de soltar al alumno",
        points=points,
        verification="Abrir una lección.",
        pass_criteria="Presenta un ejemplo resuelto antes de la práctica.",
        fail_criteria="Pide práctica sin modelar.",
    )


def _criteria(*tests: CriterionTest) -> Criteria:
    return Criteria(
        name="c",
        domains=[CriterionDomain(code="D2", name="Instrucción", points=sum(t.points for t in tests), tests=list(tests))],
    )


# --- render_markdown ------------------------------------------------------


def test_render_markdown_cites_evidence_lists_no_evaluados_and_shows_pct():
    tests = [_test(points=1, tid="a"), _test(points=1, tid="c")]
    crit = _criteria(*tests)
    verdicts = [
        TestVerdict("a", "PLATAFORMA_B", CUMPLE, 1, 1, "modeló primero", ["/ev/lesson.png"], [CUMPLE]),
        TestVerdict("c", "PLATAFORMA_B", NO_EVALUADO, 0, 0, "sin evidencia suficiente", [], [NO_EVALUADO]),
    ]
    md = render_markdown(crit, verdicts)
    assert "/ev/lesson.png" in md  # every scored verdict cites its artifact
    # 1 of 2 cells scored = 50% coverage, so NO percentage is printed: a "100%"
    # off a single observable cell is a coverage artifact, not a domain score.
    assert "COBERTURA INSUFICIENTE" in md
    assert "1 de 2 evaluados" in md
    assert "no evaluados" in md.lower()  # the NO_EVALUADO section exists
    # the uncovered test id appears in that section
    section = md.lower().split("no evaluados", 1)[1]
    assert '"c"' in section or "c" in section


def test_render_markdown_shows_a_column_per_platform():
    crit = _criteria(_test(points=2, tid="1.1"))
    verdicts = [
        TestVerdict("1.1", "PLATAFORMA_B", CUMPLE, 2, 2, "r", ["/plataforma_b.png"], [CUMPLE]),
        TestVerdict("1.1", "Plataforma A", NO_CUMPLE, 0, 2, "r", ["/plataforma_a.png"], [NO_CUMPLE]),
    ]
    md = render_markdown(crit, verdicts)
    # both platforms appear, and both platforms' evidence survives (no blend)
    assert "PLATAFORMA_B" in md and "Plataforma A" in md
    assert "/plataforma_b.png" in md and "/plataforma_a.png" in md


# --- I1: per-platform aggregation (no blend of PLATAFORMA_B and Plataforma A) --------------


def test_aggregate_raises_on_mixed_platforms():
    crit = _criteria(_test(points=2, tid="1.1"))
    verdicts = [
        TestVerdict("1.1", "PLATAFORMA_B", CUMPLE, 2, 2, "r", ["/a"], [CUMPLE]),
        TestVerdict("1.1", "Plataforma A", NO_CUMPLE, 0, 2, "r", ["/b"], [NO_CUMPLE]),
    ]
    with pytest.raises(EvalkitError):
        aggregate(crit, verdicts)


def test_aggregate_by_platform_keeps_both_verdicts_for_a_shared_id():
    # the reviewer's exact case: 1.1 CUMPLE on PLATAFORMA_B, 1.1 NO_CUMPLE on Plataforma A.
    # A single blended aggregate silently dropped one; this must keep both.
    crit = _criteria(_test(points=2, tid="1.1"))
    verdicts = [
        TestVerdict("1.1", "PLATAFORMA_B", CUMPLE, 2, 2, "r", ["/a"], [CUMPLE]),
        TestVerdict("1.1", "Plataforma A", NO_CUMPLE, 0, 2, "r", ["/b"], [NO_CUMPLE]),
    ]
    by_p = aggregate_by_platform(crit, verdicts)
    assert by_p["PLATAFORMA_B"]["D2"].earned == 2
    assert by_p["PLATAFORMA_B"]["D2"].possible == 2
    assert by_p["Plataforma A"]["D2"].earned == 0
    assert by_p["Plataforma A"]["D2"].possible == 2


# --- coverage is stated on EVERY cell -------------------------------------


def _md(*verdicts, tests):
    return render_markdown(_criteria(*tests), list(verdicts))


def test_unobservable_domain_never_renders_a_percentage():
    # THE finding, at the rendering layer. PLATAFORMA_B's 3 unreachable tutor-safety
    # cells printed "0/0 · 0%" beside Plataforma A's "1/3 · 33.3%", so a reader
    # concluded PLATAFORMA_B was WORSE on tutor safety when it was never measured.
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    md = _md(*[TestVerdict(t, "PLATAFORMA_B", NO_EVALUADO, 0, 0, "no se pudo llegar", [], [NO_EVALUADO])
               for t in ("a", "b", "c")], tests=tests)
    assert "0%" not in md
    assert "no evaluado" in md.lower()
    assert "0 de 3 celdas" in md


def test_scored_domain_states_its_coverage_next_to_the_percentage():
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    md = _md(
        TestVerdict("a", "Plataforma A", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE]),
        TestVerdict("b", "Plataforma A", NO_CUMPLE, 0, 1, "r", ["/b"], [NO_CUMPLE]),
        TestVerdict("c", "Plataforma A", NO_CUMPLE, 0, 1, "r", ["/c"], [NO_CUMPLE]),
        tests=tests,
    )
    assert "1/3 · 33.3% (3 de 3 evaluados)" in md


def test_thin_coverage_refuses_to_print_a_percentage():
    # 1 of 3 cells scored = 33% coverage. A "100%" off one cell is not a domain
    # score; print the raw points and say the coverage is insufficient.
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    md = _md(TestVerdict("a", "Plataforma A", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE]), tests=tests)
    assert "COBERTURA INSUFICIENTE" in md
    assert "100%" not in md
    assert "1/1" in md
    assert "1 de 3 evaluados" in md


def test_coverage_at_the_threshold_still_prints_a_percentage():
    # exactly 70% coverage is allowed; below it is not.
    tests = [_test(points=1, tid=str(i)) for i in range(10)]
    verdicts = [TestVerdict(str(i), "Plataforma A", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE]) for i in range(7)]
    md = _md(*verdicts, tests=tests)
    assert "100% (7 de 10 evaluados)" in md
    assert "COBERTURA INSUFICIENTE" not in md


def test_summary_table_marks_the_unobservable_domain_differently_from_a_zero():
    # the two-vendor case, side by side: they must not render the same.
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    md = render_markdown(_criteria(*tests), [
        *[TestVerdict(t, "PLATAFORMA_B", NO_EVALUADO, 0, 0, "sin acceso", [], [NO_EVALUADO])
          for t in ("a", "b", "c")],
        *[TestVerdict(t, "Plataforma A", NO_CUMPLE, 0, 1, "falló", ["/k"], [NO_CUMPLE])
          for t in ("a", "b", "c")],
    ])
    table = md.split("## Plataforma", 1)[0]
    plataforma_b_cell, plataforma_a_cell = table.strip().splitlines()[-1].split("|")[2:4]
    assert plataforma_b_cell != plataforma_a_cell
    assert "no evaluado" in plataforma_b_cell.lower()
    assert "0%" in plataforma_a_cell  # Plataforma A really did score zero, and says so
