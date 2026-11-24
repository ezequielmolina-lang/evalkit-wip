"""Replication-harness tests. No real API — Form 2 is exercised with a fake."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lxp_eval.evalkit.criteria import Criteria, CriterionDomain, CriterionTest, load_criteria
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.replicate import (
    agreement,
    agreement_summary,
    blind_as_verdicts,
    expected_as_rows,
    form1_aggregate,
    load_evidence_map,
    load_expected,
    normalize_verdict,
    rederive,
    write_results_json,
)
from lxp_eval.evalkit.scoring import CUMPLE, NO_CUMPLE, NO_EVALUADO, PARCIAL

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
EXAMPLE_YAML = _EXAMPLES / "criterios_lxp_blind.yaml"
HALLAZGOS_YAML = _EXAMPLES / "criterios_hallazgos.yaml"
ESPERADOS_YAML = _EXAMPLES / "hallazgos_esperados.yaml"
EVIDENCE_MAP = _EXAMPLES / "evidence_map.yaml"


class FakeClient:
    def __init__(self, *tool_inputs: dict):
        self._inputs = list(tool_inputs)
        self.call_count = 0
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent):
            self._parent = parent

        def create(self, **kwargs):
            p = self._parent
            data = p._inputs[p.call_count % len(p._inputs)]
            p.call_count += 1
            block = SimpleNamespace(type="tool_use", name="emitir_veredicto", input=data)
            return SimpleNamespace(content=[block], stop_reason="tool_use")


def _test(tid, points=1):
    return CriterionTest(
        id=tid, name=f"n {tid}", points=points,
        verification="v", pass_criteria="p", fail_criteria="f",
    )


def _criteria(*tests):
    return Criteria(
        name="c",
        domains=[CriterionDomain(code="D9", name="Seg", points=sum(t.points for t in tests), tests=list(tests))],
    )


# --- normalize_verdict -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,norm",
    [
        ("CUMPLE", CUMPLE),
        ("PARCIAL", PARCIAL),
        ("NO CUMPLE", NO_CUMPLE),   # space, not underscore
        ("NO_PUDE", NO_EVALUADO),   # lossy collapse
        ("NO_OBSERVADO", NO_EVALUADO),
    ],
)
def test_normalize_maps_all_five_and_preserves_raw(raw, norm):
    got_norm, got_raw = normalize_verdict(raw)
    assert got_norm == norm
    assert got_raw == raw  # the raw label survives the lossy collapse


def test_normalize_unknown_raises_rather_than_failing_closed():
    # see test_unmapped_verdict_token_raises_instead_of_failing_closed: the
    # old fail-closed behaviour removed the pair from the concordance
    # denominator, which RAISED the published rate.
    with pytest.raises(EvalkitError):
        normalize_verdict("???")


# --- Form 1: deterministic aggregation on real verdicts --------------------


def test_deterministic_aggregation_matches_hand_computed_points():
    crit = _criteria(_test("PT-D9-H1"), _test("PT-D9-H2"), _test("PT-D9-M1"))
    results = [
        {"cell_id": "PT-D9-H1", "platform": "Plataforma A", "verdict": "CUMPLE", "lo_que_vi": "", "evidencia": "e"},
        {"cell_id": "PT-D9-H2", "platform": "Plataforma A", "verdict": "NO CUMPLE", "lo_que_vi": "", "evidencia": "e"},
        {"cell_id": "PT-D9-M1", "platform": "Plataforma A", "verdict": "PARCIAL", "lo_que_vi": "", "evidencia": "e"},
        # PLATAFORMA_B: all three could not be evaluated -> excluded from the denominator
        {"cell_id": "PT-D9-H1", "platform": "PLATAFORMA_B", "verdict": "NO_PUDE", "lo_que_vi": "", "evidencia": "e"},
        {"cell_id": "PT-D9-H2", "platform": "PLATAFORMA_B", "verdict": "NO_PUDE", "lo_que_vi": "", "evidencia": "e"},
        {"cell_id": "PT-D9-M1", "platform": "PLATAFORMA_B", "verdict": "NO_OBSERVADO", "lo_que_vi": "", "evidencia": "e"},
    ]
    by_platform = form1_aggregate(crit, results)
    # Plataforma A: CUMPLE(1) + NO_CUMPLE(0) + PARCIAL(0.5) over 3 scored = 1.5/3
    plataforma_a = by_platform["Plataforma A"]["D9"]
    assert (plataforma_a.earned, plataforma_a.possible, plataforma_a.n_scored) == (1.5, 3, 3)
    # PLATAFORMA_B: all NO_EVALUADO -> nothing scored, denominator zero
    plataforma_b = by_platform["PLATAFORMA_B"]["D9"]
    assert (plataforma_b.earned, plataforma_b.possible, plataforma_b.n_scored, plataforma_b.n_pending) == (0, 0, 0, 3)
    # NOT 0.0: nothing was observable, so there is no percentage. Rendering this
    # as 0% put PLATAFORMA_B's unreachable tutor below Plataforma A's measured-and-partly-failing
    # one, rewarding the vendor that hid the surface.
    assert plataforma_b.pct is None


def test_blind_as_verdicts_does_not_blend_platforms():
    crit = _criteria(_test("PT-D9-H1"))
    results = [
        {"cell_id": "PT-D9-H1", "platform": "PLATAFORMA_B", "verdict": "CUMPLE", "lo_que_vi": "", "evidencia": "e"},
        {"cell_id": "PT-D9-H1", "platform": "Plataforma A", "verdict": "NO CUMPLE", "lo_que_vi": "", "evidencia": "e"},
    ]
    verdicts = blind_as_verdicts(crit, results)
    assert {(v.platform, v.status) for v in verdicts} == {("PLATAFORMA_B", CUMPLE), ("Plataforma A", NO_CUMPLE)}


# --- the shipped criteria file --------------------------------------------


def test_generated_blind_criteria_loads_with_11_tests():
    crit = load_criteria(EXAMPLE_YAML)
    assert len(crit.all_tests()) == 11
    codes = {d.code for d in crit.domains}
    # D9 (3 confab/safety cells) and D8 (2 pedagogy cells) are the multi-cell groups
    assert {"D9", "D8"} <= codes
    d9 = next(d for d in crit.domains if d.code == "D9")
    assert len(d9.tests) == 3
    # ids survive exactly as the cell ids
    assert "PT-D9-H1" in {t.id for t in crit.all_tests()}


# --- agreement ladder ------------------------------------------------------


def test_agreement_exact_and_within_one():
    assert agreement(CUMPLE, CUMPLE) == (True, True)
    assert agreement(CUMPLE, PARCIAL) == (False, True)   # one level apart
    assert agreement(CUMPLE, NO_CUMPLE) == (False, False)  # two levels apart


def test_agreement_excludes_no_evaluado_either_side():
    assert agreement(NO_EVALUADO, CUMPLE) == (None, None)
    assert agreement(CUMPLE, NO_EVALUADO) == (None, None)


def test_agreement_summary_counts_and_excludes():
    rows = [
        {"exact": True, "within1": True},
        {"exact": False, "within1": True},
        {"exact": False, "within1": False},
        {"exact": None, "within1": None},  # excluded
    ]
    s = agreement_summary(rows)
    assert s["compared"] == 3
    assert s["excluded"] == 1
    assert s["exact"] == 1
    assert s["within1"]["k"] == 2
    # integer percentage: one decimal at this n is false precision
    assert s["exact_pct"] == 33


# --- Form 2 wiring (fake client, no network) ------------------------------


def test_rederive_produces_a_comparison_row_without_network(tmp_path: Path):
    crit = _criteria(_test("PT-D8-03"))  # non-confab -> repeats=1
    results = [{
        "cell_id": "PT-D8-03", "platform": "Plataforma A", "verdict": "CUMPLE",
        "lo_que_vi": "vi que la plataforma modela", "evidencia": "shot.txt",
    }]
    obs = str(tmp_path / "blind_Plataforma A_PT-D8-03_obs.txt")  # what rederive actually writes
    client = FakeClient({"verdict": CUMPLE, "rationale": "ok", "cited_artifacts": [obs]})
    rows = rederive(crit, results, client, work_dir=tmp_path, evidence_map={})
    assert client.call_count == 1  # repeats=1 for a non-confab cell
    assert len(rows) == 1
    row = rows[0]
    assert row["cell_id"] == "PT-D8-03"
    assert row["expected_norm"] == CUMPLE
    assert row["evalkit_verdict"] == CUMPLE
    assert row["exact"] is True


def test_rederive_runs_confab_cells_three_times(tmp_path: Path):
    # PT-D9-H1 is a confabulation cell -> repeats=3 even without a screenshot
    crit = _criteria(_test("PT-D9-H1"))
    results = [{
        "cell_id": "PT-D9-H1", "platform": "Plataforma A", "verdict": "NO CUMPLE",
        "lo_que_vi": "el tutor confirmó el recuerdo falso", "evidencia": "run1.txt",
    }]
    obs = str(tmp_path / "blind_Plataforma A_PT-D9-H1_obs.txt")
    client = FakeClient({"verdict": NO_CUMPLE, "rationale": "confirmó", "cited_artifacts": [obs]})
    rows = rederive(crit, results, client, work_dir=tmp_path, evidence_map={})
    assert client.call_count == 3
    assert rows[0]["runs"] == [NO_CUMPLE, NO_CUMPLE, NO_CUMPLE]
    assert rows[0]["exact"] is True


# --- explicit evidence map (no guessed images) ----------------------------


def test_unmapped_pair_gets_no_image_blocks(tmp_path: Path):
    # THE regression guard: the old heuristic attached a screenshot of a
    # DIFFERENT probe. A pair with no map entry must get text-only evidence.
    crit = _criteria(_test("PT-D9-H2"))
    results = [{
        "cell_id": "PT-D9-H2", "platform": "PLATAFORMA_B", "verdict": "NO_PUDE",
        "lo_que_vi": "sin tutor accesible", "evidencia": "notes.txt",
    }]
    client = FakeClient({"verdict": NO_CUMPLE, "rationale": "r", "cited_artifacts": ["x"]})
    rows = rederive(crit, results, client, work_dir=tmp_path, evidence_map={})
    kinds = [e["kind"] for e in rows[0]["evidence"]]
    assert "screenshot" not in kinds  # zero image blocks
    assert kinds == ["observation"]


def test_mapped_pair_attaches_exactly_the_mapped_artifact(tmp_path: Path):
    shot = tmp_path / "real_probe.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    crit = _criteria(_test("PT-D9-H1"))
    results = [{
        "cell_id": "PT-D9-H1", "platform": "Plataforma A", "verdict": "NO CUMPLE",
        "lo_que_vi": "confirmó el recuerdo", "evidencia": "run1.txt",
    }]
    client = FakeClient({"verdict": NO_CUMPLE, "rationale": "r", "cited_artifacts": ["x"]})
    rows = rederive(
        crit, results, client, work_dir=tmp_path,
        evidence_map={("PT-D9-H1", "Plataforma A"): [shot]},
    )
    shots = [e for e in rows[0]["evidence"] if e["kind"] == "screenshot"]
    assert len(shots) == 1
    assert shots[0]["exists"] is True
    assert shots[0]["path"].endswith("real_probe.png")


def test_evidence_map_fails_loud_on_missing_artifact(tmp_path: Path):
    bad = tmp_path / "map.yaml"
    bad.write_text(
        'entries:\n'
        '  - cell_id: "4.1.1"\n'
        '    platform: "PLATAFORMA_B"\n'
        '    artifacts:\n'
        '      - no/existe/jamas.png\n',
        encoding="utf-8",
    )
    with pytest.raises(EvalkitError) as e:
        load_evidence_map(bad, repo_root=tmp_path)
    msg = str(e.value)
    assert "no existe" in msg.lower() or "no se encontró" in msg.lower()
    assert "map.yaml" in msg  # names the file
    assert "línea" in msg  # and the line


@pytest.mark.needs_real_platform
def test_shipped_evidence_map_loads_and_every_artifact_exists():
    m = load_evidence_map(EVIDENCE_MAP)
    assert m  # non-empty
    for paths in m.values():
        for p in paths:
            assert p.exists(), p
    # the mis-evidence case must NOT be mapped: PLATAFORMA_B blind D9 cells get no image
    assert ("PT-D9-H1", "PLATAFORMA_B") not in m
    assert ("PT-D9-H2", "PLATAFORMA_B") not in m


# --- the published-findings (hallazgos) set -------------------------------


def test_hallazgos_criteria_and_expected_load_and_match():
    crit = load_criteria(HALLAZGOS_YAML)
    ids = {t.id for t in crit.all_tests()}
    assert ids == {"4.1.1", "4.1.2", "4.1.3", "4.1.5"}

    expected = load_expected(ESPERADOS_YAML)
    # one expected verdict per (id, platform), both platforms for all four
    assert set(expected) == {(i, p) for i in ids for p in ("PLATAFORMA_B", "Plataforma A")}
    # spot-check the published values (report §4.1)
    assert expected[("4.1.1", "PLATAFORMA_B")]["verdict"] == "CUMPLE"
    assert expected[("4.1.1", "Plataforma A")]["verdict"] == "NO CUMPLE"
    assert expected[("4.1.5", "PLATAFORMA_B")]["verdict"] == "NO CUMPLE"
    assert expected[("4.1.5", "Plataforma A")]["verdict"] == "PARCIAL"


def test_expected_as_rows_normalizes_to_the_common_shape():
    expected = load_expected(ESPERADOS_YAML)
    rows = expected_as_rows(expected)
    assert len(rows) == 8
    r = next(r for r in rows if r["cell_id"] == "4.1.5" and r["platform"] == "PLATAFORMA_B")
    assert r["verdict"] == "NO CUMPLE"
    assert "Línea 132" in r["lo_que_vi"] or "132" in r["lo_que_vi"]


# --- machine-readable output ----------------------------------------------


def test_writes_valid_json_with_repo_relative_paths(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    crit = _criteria(_test("4.1.1"))
    results = [{
        "cell_id": "4.1.1", "platform": "Plataforma A", "verdict": "NO CUMPLE",
        "lo_que_vi": "confirmó", "evidencia": "informe §4.1",
    }]
    client = FakeClient({"verdict": NO_CUMPLE, "rationale": "r", "cited_artifacts": [str(shot)]})
    rows = rederive(
        crit, results, client, work_dir=tmp_path, mode="hallazgos",
        evidence_map={("4.1.1", "Plataforma A"): [shot]},
    )
    out = tmp_path / "replication_results.json"
    write_results_json(out, {"hallazgos": agreement_summary(rows)}, rows)

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["generated_at"]
    assert "hallazgos" in data["summary"]
    row = data["rows"][0]
    assert row["mode"] == "hallazgos"
    assert row["criterion_name"]
    assert row["pass_criteria"] and row["fail_criteria"]
    assert row["expected_raw"] == "NO CUMPLE"
    assert row["expected_norm"] == NO_CUMPLE
    assert row["evalkit_verdict"] == NO_CUMPLE
    # paths must be repo-relative, never absolute C:\... or /home/...
    for e in row["evidence"]:
        assert not Path(e["path"]).is_absolute(), e["path"]
        assert ":" not in e["path"][:3]


# ==========================================================================
# Statistics. Raw agreement is the most challengeable number in the package:
# it hides its own denominator, its uncertainty, its direction, and the fact
# that 26 comparisons are 11 cells x 2 platforms rather than 26 independent
# draws. Everything below exists to stop a single percentage doing that work.
# ==========================================================================

from lxp_eval.evalkit.replicate import (  # noqa: E402
    build_manifest,
    cluster_bootstrap_ci,
    cohens_kappa,
    confusion_matrix,
    format_proportion,
    sign_test,
    wilson_ci,
)


# --- Wilson interval -------------------------------------------------------


def test_wilson_ci_on_15_of_18_matches_the_published_interval():
    lo, hi = wilson_ci(15, 18)
    assert round(100 * lo, 1) == 60.8
    assert round(100 * hi, 1) == 94.2


def test_wilson_ci_is_bounded_and_handles_the_degenerate_ends():
    assert wilson_ci(0, 10)[0] == 0.0
    assert wilson_ci(10, 10)[1] == 1.0
    assert wilson_ci(0, 0) == (0.0, 1.0)  # no data is not a point estimate


def test_format_proportion_reports_k_n_pct_and_ci_without_false_precision():
    # one decimal place at n=18 is false precision; the interval is 33 points wide.
    assert format_proportion(15, 18) == "15/18 (83%, IC95 61-94)"


# --- Cohen's kappa ---------------------------------------------------------


def test_cohens_kappa_matches_the_textbook_2x2():
    # classic worked example: po=0.70, pe=0.50 -> kappa=0.40
    pairs = [("A", "A")] * 20 + [("A", "B")] * 5 + [("B", "A")] * 10 + [("B", "B")] * 15
    assert round(cohens_kappa(pairs), 4) == 0.4


def test_kappa_is_one_on_perfect_agreement_and_zero_at_chance():
    assert cohens_kappa([("A", "A")] * 5 + [("B", "B")] * 5) == 1.0
    # independent raters with identical 50/50 marginals -> kappa 0
    pairs = [("A", "A")] * 25 + [("A", "B")] * 25 + [("B", "A")] * 25 + [("B", "B")] * 25
    assert round(cohens_kappa(pairs), 10) == 0.0


def test_kappa_is_not_the_raw_agreement_rate():
    # 90% raw agreement on a skewed marginal is a poor kappa. This is exactly
    # why the headline percentage cannot stand alone.
    pairs = [("A", "A")] * 90 + [("A", "B")] * 5 + [("B", "A")] * 5
    raw = 90 / 100
    assert raw == 0.9
    assert cohens_kappa(pairs) < 0.5


def test_linear_weighted_kappa_credits_adjacent_disagreements():
    # on an ORDERED 3-level scale, CUMPLE-vs-PARCIAL is a near miss and
    # CUMPLE-vs-NO_CUMPLE is not. Unweighted kappa cannot tell them apart.
    order = [NO_CUMPLE, PARCIAL, CUMPLE]
    adjacent = [(CUMPLE, CUMPLE)] * 8 + [(CUMPLE, PARCIAL)] * 2 + [(NO_CUMPLE, NO_CUMPLE)] * 10
    k = cohens_kappa(adjacent, categories=order)
    kw = cohens_kappa(adjacent, categories=order, weights="linear")
    assert kw > k


def test_weighted_kappa_equals_unweighted_on_a_two_level_scale():
    pairs = [("A", "A")] * 20 + [("A", "B")] * 5 + [("B", "A")] * 10 + [("B", "B")] * 15
    cats = ["A", "B"]
    assert round(cohens_kappa(pairs, categories=cats), 10) == round(
        cohens_kappa(pairs, categories=cats, weights="linear"), 10
    )


def test_kappa_is_none_when_undefined():
    assert cohens_kappa([]) is None
    # every rating in one category on both sides: pe == 1, kappa undefined
    assert cohens_kappa([("A", "A")] * 10, categories=["A", "B"]) is None


# --- confusion matrix (4x4, NO_EVALUADO included) --------------------------


def test_confusion_matrix_includes_no_evaluado_as_a_fourth_category():
    rows = [
        _row(CUMPLE, CUMPLE), _row(CUMPLE, PARCIAL),
        _row(NO_EVALUADO, NO_EVALUADO), _row(NO_CUMPLE, NO_EVALUADO),
    ]
    m = confusion_matrix(rows)
    assert set(m) == {CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO}
    assert m[CUMPLE][CUMPLE] == 1
    assert m[CUMPLE][PARCIAL] == 1
    assert m[NO_EVALUADO][NO_EVALUADO] == 1
    assert m[NO_CUMPLE][NO_EVALUADO] == 1
    assert sum(sum(r.values()) for r in m.values()) == 4


# --- sign test on discordant pairs ----------------------------------------


def test_sign_test_two_sided_exact_binomial():
    assert round(sign_test(5, 0), 4) == 0.0625   # all 5 one direction
    assert sign_test(0, 0) is None               # no discordant pairs
    assert sign_test(3, 3) == 1.0                # perfectly balanced


# --- cluster bootstrap by cell_id -----------------------------------------


def test_cluster_bootstrap_is_deterministic_and_wider_than_a_naive_interval():
    # 26 comparisons are 11 cells x 2 platforms. Resampling ROWS pretends they
    # are independent draws; resampling CELLS keeps both platforms together.
    rows = []
    for i in range(10):
        agree = i < 8
        for plat in ("PLATAFORMA_B", "Plataforma A"):
            rows.append(_row(CUMPLE, CUMPLE if agree else PARCIAL, cell=f"c{i}", platform=plat))
    a = cluster_bootstrap_ci(rows, n_boot=500, seed=7)
    b = cluster_bootstrap_ci(rows, n_boot=500, seed=7)
    assert a == b                       # same seed -> same interval
    assert a[0] < 0.8 < a[1]
    naive = wilson_ci(16, 20)
    assert (a[1] - a[0]) > (naive[1] - naive[0]), "clustering must not shrink the interval"


def test_cluster_bootstrap_is_none_without_eligible_rows():
    assert cluster_bootstrap_ci([_row(NO_EVALUADO, CUMPLE)], n_boot=50, seed=1) is None


# ==========================================================================
# Statistics. Raw agreement is the most challengeable number in the package:
# it hides its own denominator, its uncertainty, its direction, and the fact
# that 26 comparisons are 11 cells x 2 platforms rather than 26 independent
# draws. Everything below exists to stop a single percentage doing that work.
# ==========================================================================

from lxp_eval.evalkit.replicate import (  # noqa: E402
    CONFAB_CELLS,
    build_manifest,
    cluster_bootstrap_ci,
    cohens_kappa,
    confusion_matrix,
    format_proportion,
    sign_test,
    wilson_ci,
)


def _row(expected, evalkit, *, cell="c1", platform="Plataforma A", cited=None, evidence=None):
    """A rederive() output row, trimmed to the fields the statistics read."""
    exact, within1 = agreement(expected, evalkit)
    return {
        "cell_id": cell,
        "platform": platform,
        "expected_norm": expected,
        "evalkit_verdict": evalkit,
        "exact": exact,
        "within1": within1,
        "cited": cited if cited is not None else ["evidence_ledger/x.png"],
        "evidence": evidence if evidence is not None else [
            {"path": "evidence_ledger/x.png", "kind": "screenshot", "exists": True}
        ],
    }


# --- Wilson interval -------------------------------------------------------


def test_wilson_ci_on_15_of_18_matches_the_published_interval():
    lo, hi = wilson_ci(15, 18)
    assert round(100 * lo, 1) == 60.8
    assert round(100 * hi, 1) == 94.2


def test_wilson_ci_is_bounded_and_handles_the_degenerate_ends():
    assert wilson_ci(0, 10)[0] == 0.0
    assert wilson_ci(10, 10)[1] == 1.0
    assert wilson_ci(0, 0) == (0.0, 1.0)  # no data is not a point estimate


def test_format_proportion_reports_k_n_pct_and_ci_without_false_precision():
    # one decimal place at n=18 is false precision; the interval is 33 points wide.
    assert format_proportion(15, 18) == "15/18 (83%, IC95 61-94)"


# --- Cohen's kappa ---------------------------------------------------------


def test_cohens_kappa_matches_the_textbook_2x2():
    # classic worked example: po=0.70, pe=0.50 -> kappa=0.40
    pairs = [("A", "A")] * 20 + [("A", "B")] * 5 + [("B", "A")] * 10 + [("B", "B")] * 15
    assert round(cohens_kappa(pairs), 4) == 0.4


def test_kappa_is_one_on_perfect_agreement_and_zero_at_chance():
    assert cohens_kappa([("A", "A")] * 5 + [("B", "B")] * 5) == 1.0
    # independent raters with identical 50/50 marginals -> kappa 0
    pairs = [("A", "A")] * 25 + [("A", "B")] * 25 + [("B", "A")] * 25 + [("B", "B")] * 25
    assert round(cohens_kappa(pairs), 10) == 0.0


def test_kappa_is_not_the_raw_agreement_rate():
    # 90% raw agreement on a skewed marginal is a poor kappa. This is exactly
    # why the headline percentage cannot stand alone.
    pairs = [("A", "A")] * 90 + [("A", "B")] * 5 + [("B", "A")] * 5
    assert cohens_kappa(pairs) < 0.5


def test_linear_weighted_kappa_credits_adjacent_disagreements():
    # on an ORDERED 3-level scale, CUMPLE-vs-PARCIAL is a near miss and
    # CUMPLE-vs-NO_CUMPLE is not. Unweighted kappa cannot tell them apart.
    order = [NO_CUMPLE, PARCIAL, CUMPLE]
    adjacent = ([(CUMPLE, CUMPLE)] * 8 + [(CUMPLE, PARCIAL)] * 2
                + [(NO_CUMPLE, NO_CUMPLE)] * 10)
    k = cohens_kappa(adjacent, categories=order)
    kw = cohens_kappa(adjacent, categories=order, weights="linear")
    assert kw > k


def test_weighted_kappa_equals_unweighted_on_a_two_level_scale():
    pairs = [("A", "A")] * 20 + [("A", "B")] * 5 + [("B", "A")] * 10 + [("B", "B")] * 15
    cats = ["A", "B"]
    assert round(cohens_kappa(pairs, categories=cats), 10) == round(
        cohens_kappa(pairs, categories=cats, weights="linear"), 10
    )


def test_kappa_is_none_when_undefined():
    assert cohens_kappa([]) is None
    # every rating in one category on both sides: pe == 1, kappa undefined
    assert cohens_kappa([("A", "A")] * 10, categories=["A", "B"]) is None


# --- confusion matrix (4x4, NO_EVALUADO included) --------------------------


def test_confusion_matrix_includes_no_evaluado_as_a_fourth_category():
    rows = [
        _row(CUMPLE, CUMPLE), _row(CUMPLE, PARCIAL),
        _row(NO_EVALUADO, NO_EVALUADO), _row(NO_CUMPLE, NO_EVALUADO),
    ]
    m = confusion_matrix(rows)
    assert set(m) == {CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO}
    assert m[CUMPLE][CUMPLE] == 1
    assert m[CUMPLE][PARCIAL] == 1
    assert m[NO_EVALUADO][NO_EVALUADO] == 1
    assert m[NO_CUMPLE][NO_EVALUADO] == 1
    assert sum(sum(r.values()) for r in m.values()) == 4


# --- sign test on discordant pairs ----------------------------------------


def test_sign_test_two_sided_exact_binomial():
    assert round(sign_test(5, 0), 4) == 0.0625   # all 5 one direction
    assert sign_test(0, 0) is None               # no discordant pairs
    assert sign_test(3, 3) == 1.0                # perfectly balanced


# --- cluster bootstrap by cell_id -----------------------------------------


def test_cluster_bootstrap_is_deterministic_and_wider_than_a_naive_interval():
    # 26 comparisons are 11 cells x 2 platforms. Resampling ROWS pretends they
    # are independent draws; resampling CELLS keeps both platforms together.
    rows = []
    for i in range(10):
        agree = i < 8
        for plat in ("PLATAFORMA_B", "Plataforma A"):
            rows.append(_row(CUMPLE, CUMPLE if agree else PARCIAL,
                             cell=f"c{i}", platform=plat))
    a = cluster_bootstrap_ci(rows, n_boot=500, seed=7)
    b = cluster_bootstrap_ci(rows, n_boot=500, seed=7)
    assert a == b                       # same seed -> same interval
    assert a[0] < 0.8 < a[1]
    naive = wilson_ci(16, 20)
    assert (a[1] - a[0]) > (naive[1] - naive[0]), "clustering must not shrink the interval"


def test_cluster_bootstrap_is_none_without_eligible_rows():
    assert cluster_bootstrap_ci([_row(NO_EVALUADO, CUMPLE)], n_boot=50, seed=1) is None


# --- 1.4 an unrecognized verdict token must not silently inflate the rate --


def test_unmapped_verdict_token_raises_instead_of_failing_closed():
    # Failing closed to NO_EVALUADO removed the pair from the concordance
    # DENOMINATOR, so a typo in an input label quietly RAISED the headline
    # agreement rate. Loud is the only safe direction here.
    with pytest.raises(EvalkitError) as e:
        normalize_verdict("CUMPLEE")
    assert "CUMPLEE" in str(e.value)


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_empty_verdict_raises_too(raw):
    with pytest.raises(EvalkitError):
        normalize_verdict(raw)


@pytest.mark.parametrize("raw", ["NO_PUDE", "NO_OBSERVADO", "no pude"])
def test_declared_abstention_tokens_still_collapse_to_no_evaluado(raw):
    # the fail-closed path survives ONLY for the genuinely declared vocabulary
    assert normalize_verdict(raw)[0] == NO_EVALUADO


# --- 1.5 three denominators, so nobody can claim the number improves when
#         the tool sees less ------------------------------------------------


def _mixed_rows():
    """4 cells x 2 platforms: 4 exact, 1 near miss, 1 far miss, 2 abstentions."""
    return [
        _row(CUMPLE, CUMPLE, cell="c1", platform="PLATAFORMA_B"),
        _row(CUMPLE, CUMPLE, cell="c1", platform="Plataforma A"),
        _row(NO_CUMPLE, NO_CUMPLE, cell="c2", platform="PLATAFORMA_B"),
        _row(CUMPLE, PARCIAL, cell="c2", platform="Plataforma A"),          # near miss
        _row(PARCIAL, PARCIAL, cell="c3", platform="PLATAFORMA_B"),
        _row(CUMPLE, NO_CUMPLE, cell="c3", platform="Plataforma A"),        # far miss
        _row(NO_EVALUADO, NO_EVALUADO, cell="c4", platform="PLATAFORMA_B"),  # agrees, ineligible
        _row(CUMPLE, NO_EVALUADO, cell="c4", platform="Plataforma A"),      # evalkit abstains
    ]


def test_summary_reports_all_three_denominators():
    s = agreement_summary(_mixed_rows())
    assert s["exact_eligible"]["k"] == 4 and s["exact_eligible"]["n"] == 6
    # exact_all treats NO_EVALUADO as a fourth category: the c4/PLATAFORMA_B pair AGREES
    assert s["exact_all"]["k"] == 5 and s["exact_all"]["n"] == 8
    assert s["coverage"]["k"] == 6 and s["coverage"]["n"] == 8
    assert s["exact_eligible"]["pct"] == 67       # integer, no false precision
    assert len(s["exact_eligible"]["ci95"]) == 2


def test_summary_reports_the_abstention_rate():
    s = agreement_summary(_mixed_rows())
    assert s["abstention"]["k"] == 2 and s["abstention"]["n"] == 8


def test_summary_emits_the_full_four_by_four_confusion_matrix():
    m = agreement_summary(_mixed_rows())["confusion"]
    assert set(m) == {CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO}
    assert m[NO_EVALUADO][NO_EVALUADO] == 1
    assert m[CUMPLE][NO_EVALUADO] == 1


# --- 1.6 the statistics that make the headline defensible ------------------


def test_summary_carries_kappa_and_weighted_kappa():
    s = agreement_summary(_mixed_rows())
    assert -1.0 <= s["kappa"] <= 1.0
    assert -1.0 <= s["kappa_weighted"] <= 1.0
    # NOTE: weighted kappa is not always >= unweighted -- it depends on the
    # marginals, and on this fixture it is lower. The property that linear
    # weighting credits ADJACENT disagreements is pinned separately, in
    # test_linear_weighted_kappa_credits_adjacent_disagreements.


def test_summary_surfaces_directional_bias_that_raw_agreement_is_blind_to():
    # In the real run ALL FIVE disagreements ran the same way (evalkit harsher).
    # Raw agreement cannot see that; the sign test is what surfaces it.
    rows = [_row(CUMPLE, CUMPLE, cell=f"a{i}") for i in range(10)]
    rows += [_row(CUMPLE, PARCIAL, cell=f"b{i}") for i in range(5)]
    s = agreement_summary(rows)
    assert s["direction"]["evalkit_harsher"] == 5
    assert s["direction"]["expected_harsher"] == 0
    assert round(s["direction"]["p_value"], 4) == 0.0625


def test_summary_disaggregates_by_platform():
    # Pooled 83.3% concealed PLATAFORMA_B 12/12 = 100% vs Plataforma A 9/14 = 64.3%, with every
    # disagreement on Plataforma A. In a two-vendor procurement that MUST be split.
    s = agreement_summary(_mixed_rows())
    assert s["by_platform"]["PLATAFORMA_B"]["exact_eligible"]["k"] == 3
    assert s["by_platform"]["PLATAFORMA_B"]["exact_eligible"]["n"] == 3
    assert s["by_platform"]["Plataforma A"]["exact_eligible"]["k"] == 1
    assert s["by_platform"]["Plataforma A"]["exact_eligible"]["n"] == 3
    assert "by_platform" not in s["by_platform"]["Plataforma A"]  # no infinite nesting


def test_within1_is_never_reported_without_its_chance_floor():
    # on a 3-point ladder "within one level" is near-vacuous: the observed
    # marginals alone put the floor around 73%.
    s = agreement_summary(_mixed_rows())
    assert "within1_pct" not in s, "within1 must not stand alone as a headline"
    assert s["within1"]["pct"] is not None
    assert s["within1"]["chance_floor_pct"] is not None


def test_summary_carries_a_cluster_bootstrap_interval():
    lo, hi = agreement_summary(_mixed_rows())["exact_eligible"]["cluster_ci95"]
    assert 0.0 <= lo <= hi <= 1.0


# --- 2.3 the citation guarantee is vacuous if it cites our own note --------


def test_independent_artifact_citation_rate_is_measured_and_exposed():
    # 24 of 30 rows cited ONLY the harness-written _obs.txt, so the "abri la
    # carpeta" guarantee was satisfied by our own note in 80% of the sample.
    # The coercion rule is unchanged (that would move the estimand mid-flight);
    # this only measures and exposes it.
    obs = {"path": "runs/blind_Plataforma A_c1_obs.txt", "kind": "observation", "exists": True}
    shot = {"path": "evidence_ledger/real.png", "kind": "screenshot", "exists": True}
    rows = [
        _row(CUMPLE, CUMPLE, cell="c1", cited=[obs["path"]], evidence=[obs]),
        _row(CUMPLE, CUMPLE, cell="c2", cited=[obs["path"]], evidence=[obs]),
        _row(CUMPLE, CUMPLE, cell="c3", cited=[obs["path"], shot["path"]],
             evidence=[obs, shot]),
        _row(CUMPLE, NO_EVALUADO, cell="c4", cited=[], evidence=[obs]),
    ]
    c = agreement_summary(rows)["citations"]
    assert c["rows"] == 4
    assert c["rows_with_any_citation"] == 3
    assert c["rows_citing_independent_artifact"] == 1
    assert c["independent_pct"] == 25


# --- 1.7 reproducibility manifest -----------------------------------------


def test_manifest_pins_everything_an_outsider_needs_to_reproduce(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG-evidence")
    m = build_manifest(model="claude-opus-4-8", evidence_map={("c1", "Plataforma A"): [shot]})
    assert m["model"] == "claude-opus-4-8"
    # No temperature field: sampling params are rejected with a 400 on this
    # model, so a manifest asserting temperature=0 would claim a determinism
    # guarantee we do not have. State the limitation instead of faking it.
    assert "temperature" not in m
    assert "no determinista" in m["sampling"]
    assert m["repeats_per_cell"]["default"] == 1
    assert m["repeats_per_cell"]["confab"] == 3
    assert set(m["repeats_per_cell"]["confab_cells"]) == set(CONFAB_CELLS)
    assert len(m["judge_sha256"]) == 64
    assert len(m["evidence_map_sha256"]) == 64


def test_manifest_evidence_hash_changes_when_the_evidence_changes(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"original")
    before = build_manifest(model="m", evidence_map={("c1", "Plataforma A"): [shot]})
    shot.write_bytes(b"swapped")
    after = build_manifest(model="m", evidence_map={("c1", "Plataforma A"): [shot]})
    assert before["evidence_map_sha256"] != after["evidence_map_sha256"]
    assert before["judge_sha256"] == after["judge_sha256"]


def test_results_json_carries_the_manifest(tmp_path: Path):
    out = tmp_path / "r.json"
    m = build_manifest(model="claude-opus-4-8", evidence_map={})
    write_results_json(out, {"blind": {}}, [], manifest=m)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["manifest"]["model"] == "claude-opus-4-8"
    assert "temperature" not in data["manifest"]


# --- the reporting entry point must survive the summary it is handed -------


def test_entry_point_can_print_a_real_summary(capsys):
    """The CLI's own summary printer, against a real agreement_summary().

    This is a regression test with a specific origin: restructuring the summary
    removed the flat `within1_pct` key, and `examples/replicate_lxp.py` read it
    directly. Nothing in the suite touched that module, so the break would only
    have surfaced during the live API run — after paying for every judge call.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_replicate_lxp_entry", Path(__file__).resolve().parents[1] / "examples" / "replicate_lxp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    s = agreement_summary(_mixed_rows())
    mod._print_summary(s, "prueba")
    out = capsys.readouterr().out
    assert "acuerdo exacto (elegibles)" in out
    assert "cobertura" in out
    assert "kappa" in out
    assert "PLATAFORMA_B" in out and "Plataforma A" in out  # per-platform split is printed


def test_entry_point_renders_an_unobservable_domain_without_a_percentage(capsys):
    """form1's console table must honour pct=None exactly as the markdown does."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_replicate_lxp_entry2", Path(__file__).resolve().parents[1] / "examples" / "replicate_lxp.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    crit = _criteria(_test("PT-D9-H1"), _test("PT-D9-H2"), _test("PT-D9-M1"))
    results = [
        {"cell_id": c, "platform": "PLATAFORMA_B", "verdict": "NO_PUDE", "lo_que_vi": "", "evidencia": "e"}
        for c in ("PT-D9-H1", "PT-D9-H2", "PT-D9-M1")
    ]
    mod.form1(crit, results)
    out = capsys.readouterr().out
    assert "no evaluado" in out
    assert "0%" not in out
