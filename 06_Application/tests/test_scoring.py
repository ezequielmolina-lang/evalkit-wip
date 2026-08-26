"""Direct tests for the scoring core — the module that decides every verdict.

test_report.py covers rendering plus a slice of scoring for historical reasons;
this file owns the scoring-core invariants that a government-facing number
depends on:

* an UNOBSERVABLE domain is not a domain that scored zero (pct is None, never 0.0);
* NO_EVALUADO is out of BOTH numerator and denominator, in the dataclass itself
  and not merely because `aggregate` re-filters it;
* an unresolved split never awards credit (conservative tie-break);
* citations are an ALLOWLIST of artifacts actually supplied, so a model-invented
  path cannot back a verdict ("abrí la carpeta" at the code level);
* non-image evidence is actually READ, not merely named in the prompt;
* the forced-tool schema is pinned, because a mutation that strips the verdict
  enum is otherwise invisible to the whole suite.
"""
import json
from types import SimpleNamespace

import pytest

from lxp_eval.evalkit.criteria import Criteria, CriterionDomain, CriterionTest
from lxp_eval.evalkit.scoring import (
    CONSERVATIVE_ORDER,
    CUMPLE,
    JUDGE_SYSTEM,
    NO_CUMPLE,
    NO_EVALUADO,
    PARCIAL,
    VERDICT_TOOL,
    VERDICTS,
    TestVerdict,
    _build_content,
    _image_block,
    _modal,
    _points,
    aggregate,
    judge_fingerprint,
    score_test,
)


class FakeClient:
    """Stands in for anthropic.Anthropic. Returns canned tool-use, records kwargs."""

    def __init__(self, *tool_inputs: dict):
        self._inputs = list(tool_inputs)
        self.call_count = 0
        self.calls: list[dict] = []
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, parent):
            self._parent = parent

        def create(self, **kwargs):
            p = self._parent
            p.calls.append(kwargs)
            data = p._inputs[p.call_count % len(p._inputs)]
            p.call_count += 1
            block = SimpleNamespace(type="tool_use", name="emitir_veredicto", input=data)
            return SimpleNamespace(content=[block], stop_reason="tool_use")


def _test(points=2, tid="2.1.2") -> CriterionTest:
    return CriterionTest(
        id=tid,
        name="La plataforma modela antes de soltar al alumno",
        points=points,
        verification="Abrir una lección.",
        pass_criteria="Presenta un ejemplo resuelto antes de la práctica.",
        fail_criteria="Pide práctica sin modelar.",
    )


def _row(*, test_id="2.1.2", platform="PLATAFORMA_B", kind="page_text", artifact="/ev/a.txt", note="n"):
    return {
        "probe_id": "P-1", "test_id": test_id, "platform": platform, "run_index": 0,
        "url": "https://x.test/lesson", "kind": kind, "artifact": artifact,
        "sha256": "abc", "note": note, "timestamp": "t",
    }


def _criteria(*tests: CriterionTest) -> Criteria:
    return Criteria(
        name="c",
        domains=[CriterionDomain(code="D2", name="Instrucción",
                                 points=sum(t.points for t in tests), tests=list(tests))],
    )


@pytest.fixture
def ev(tmp_path):
    """A REAL evidence file on disk.

    Fixtures used to pass a path like "/ev/a.txt" that existed nowhere. That was
    survivable only while the scorer ignored non-image artifacts; now that it
    reads them (and only allows citations of artifacts it actually sent), the
    evidence in a scoring test has to be real, exactly as in a real run.
    """
    p = tmp_path / "a.txt"
    p.write_text("La lección abre con un ejemplo resuelto y luego pide práctica.",
                 encoding="utf-8")
    return str(p)


# --- 1.1 an unobservable domain is NOT a zero ------------------------------


def test_domain_with_nothing_scored_has_pct_none_not_zero():
    # THE finding: PLATAFORMA_B's tutor-safety domain (3 unreachable cells) rendered
    # `0/0 · 0%` beside Plataforma A's `1/3 · 33.3%`, so the vendor that hid the surface
    # read as WORSE-scoring than the vendor that exposed it. pct must be None.
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    verdicts = [
        TestVerdict(t, "PLATAFORMA_B", NO_EVALUADO, 0, 0, "no se pudo llegar", [], [NO_EVALUADO])
        for t in ("a", "b", "c")
    ]
    d2 = aggregate(_criteria(*tests), verdicts)["D2"]
    assert d2.possible == 0
    assert d2.pct is None, "un dominio no observable no puede reportarse como 0%"
    assert d2.n_scored == 0 and d2.n_pending == 3
    assert d2.coverage == 0.0


def test_domain_coverage_is_scored_over_total_cells():
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    verdicts = [
        TestVerdict("a", "PLATAFORMA_B", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE]),
        TestVerdict("b", "PLATAFORMA_B", NO_CUMPLE, 0, 1, "r", ["/b"], [NO_CUMPLE]),
        TestVerdict("c", "PLATAFORMA_B", NO_EVALUADO, 0, 0, "r", [], [NO_EVALUADO]),
    ]
    d2 = aggregate(_criteria(*tests), verdicts)["D2"]
    assert d2.n_total == 3
    assert d2.coverage == pytest.approx(2 / 3)
    assert d2.pct == 50.0


# --- 1.2 _points must not hand back a denominator it says it excludes ------


@pytest.mark.parametrize(
    "status,expected",
    [(CUMPLE, (2.0, 2.0)), (PARCIAL, (1.0, 2.0)), (NO_CUMPLE, (0.0, 2.0)),
     (NO_EVALUADO, (0.0, 0.0))],
)
def test_points_matches_its_docstring_for_every_verdict(status, expected):
    # NO_EVALUADO must be (0, 0) in the DATACLASS, not only after aggregate()
    # re-filters it — the results JSON and the dashboard read the dataclass.
    assert _points(status, 2) == expected


def test_no_evaluado_verdict_carries_zero_possible_through_score_test():
    client = FakeClient({"verdict": NO_EVALUADO, "rationale": "no alcanza",
                         "cited_artifacts": ["/ev/a.txt"]})
    v = score_test(_test(points=2), [_row()], client)
    assert (v.points_earned, v.points_possible) == (0.0, 0.0)


# --- 1.3 an unresolved split must never award credit -----------------------


def test_conservative_order_puts_no_evaluado_first():
    assert CONSERVATIVE_ORDER[0] == NO_EVALUADO


def test_tiebreak_cumple_vs_no_evaluado_resolves_to_no_evaluado():
    # "passes" vs "cannot tell" is NOT a pass. The old order put NO_EVALUADO
    # last, so this tie awarded FULL credit — the exact opposite of the rule.
    assert _modal([CUMPLE, NO_EVALUADO]) == NO_EVALUADO


def test_tiebreak_three_way_picks_no_evaluado():
    assert _modal([CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO]) == NO_EVALUADO


def test_tiebreak_among_scored_verdicts_still_picks_the_harshest():
    assert _modal([CUMPLE, NO_CUMPLE]) == NO_CUMPLE
    assert _modal([CUMPLE, PARCIAL]) == PARCIAL


def test_clear_plurality_still_beats_the_conservative_order():
    assert _modal([CUMPLE, CUMPLE, NO_EVALUADO]) == CUMPLE


def test_repeats_split_two_two_between_cumple_and_no_evaluado_scores_nothing():
    client = FakeClient(
        {"verdict": CUMPLE, "rationale": "a", "cited_artifacts": ["/ev/a.txt"]},
        {"verdict": NO_EVALUADO, "rationale": "b", "cited_artifacts": []},
    )
    v = score_test(_test(points=2), [_row()], client, repeats=4)
    assert v.status == NO_EVALUADO
    assert (v.points_earned, v.points_possible) == (0.0, 0.0)


# --- 2.1 citations are an ALLOWLIST, not a blocklist ----------------------


def test_fabricated_citation_is_dropped(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("lo que se vio", encoding="utf-8")
    client = FakeClient({
        "verdict": CUMPLE, "rationale": "ok",
        "cited_artifacts": [str(real), "/capturas/inventada_por_el_modelo.png"],
    })
    v = score_test(_test(), [_row(artifact=str(real))], client)
    assert v.evidence == [str(real)], "una ruta inventada no puede sobrevivir la cita"


def test_verdict_backed_only_by_a_fabricated_citation_is_no_evaluado(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("lo que se vio", encoding="utf-8")
    client = FakeClient({
        "verdict": CUMPLE, "rationale": "vi la captura",
        "cited_artifacts": ["/capturas/no_existe.png"],
    })
    v = score_test(_test(), [_row(artifact=str(real))], client)
    assert v.status == NO_EVALUADO
    assert v.evidence == []
    assert (v.points_earned, v.points_possible) == (0.0, 0.0)


def test_missing_screenshot_still_cannot_be_cited(tmp_path):
    # the old blocklist behaviour must survive the allowlist rewrite
    real = tmp_path / "real.txt"
    real.write_text("nota", encoding="utf-8")
    gone = tmp_path / "gone.png"
    client = FakeClient({"verdict": CUMPLE, "rationale": "ok",
                         "cited_artifacts": [str(gone), str(real)]})
    rows = [_row(artifact=str(real)), _row(kind="screenshot", artifact=str(gone))]
    v = score_test(_test(), rows, client)
    assert str(gone) not in v.evidence
    assert v.evidence == [str(real)]


# --- 2.2 non-image evidence must actually be READ -------------------------


def test_page_text_file_contents_reach_the_prompt(tmp_path):
    page = tmp_path / "page.txt"
    page.write_text("EL TUTOR RESPONDIÓ: no puedo ayudarte con eso", encoding="utf-8")
    content, _missing = _build_content(_test(), [_row(kind="page_text", artifact=str(page),
                                                      note="nota del operador")])
    text = content[0]["text"]
    assert "EL TUTOR RESPONDIÓ: no puedo ayudarte con eso" in text, (
        "el juez estaba puntuando la NOTA humana, no el contenido de la plataforma"
    )


def test_long_page_text_is_truncated_and_says_so(tmp_path):
    # Sized RELATIVE TO THE CAP on purpose. This used to hard-code 9000, which
    # silently encoded "the cap is 4000" -- so raising the cap to fit a whole
    # privacy policy broke the test even though the behaviour it guards (say so
    # when you cut) was untouched. The property is "past the cap it admits it",
    # not "4000".
    from lxp_eval.evalkit import scoring

    over = scoring.MAX_INLINE_CHARS + 5000
    page = tmp_path / "page.txt"
    page.write_text("x" * over, encoding="utf-8")
    content, _ = _build_content(_test(), [_row(kind="page_text", artifact=str(page))])
    text = content[0]["text"]
    assert "truncado" in text.lower()
    assert len(text) < over


def test_unreadable_non_image_artifact_is_marked_and_not_citable(tmp_path):
    gone = tmp_path / "nope.txt"
    content, missing = _build_content(_test(), [_row(kind="page_text", artifact=str(gone))])
    assert str(gone) in missing
    assert "no encontrado" in content[0]["text"]


def test_screenshot_is_not_inlined_as_text(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    content, missing = _build_content(_test(), [_row(kind="screenshot", artifact=str(shot))])
    assert missing == set()
    assert any(b.get("type") == "image" for b in content)
    assert "\x89PNG" not in content[0]["text"]


# --- 1.7 sampling: the judge must NOT send temperature ---------------------
# Inverted from its original form. This test used to assert temperature == 0
# and passed green against a fake client -- while the real API rejected the
# call with `400: temperature is deprecated for this model`. Sampling params
# were removed on Opus 4.8/4.7. Sending one breaks every paid run, and a fake
# client can never catch it, so the test now guards the opposite direction.


def test_judge_sends_no_sampling_parameters():
    client = FakeClient({"verdict": CUMPLE, "rationale": "ok", "cited_artifacts": ["/ev/a.txt"]})
    score_test(_test(), [_row()], client, model="m")
    call = client.calls[0]
    for param in ("temperature", "top_p", "top_k"):
        assert param not in call, (
            f"{param!r} is rejected with a 400 on this model; sending it fails the run"
        )


def test_judge_fingerprint_is_stable_and_changes_with_the_prompt():
    fp = judge_fingerprint()
    assert fp == judge_fingerprint()
    assert len(fp) == 64
    import lxp_eval.evalkit.scoring as scoring
    original = scoring.JUDGE_SYSTEM
    try:
        scoring.JUDGE_SYSTEM = original + " (editado)"
        assert judge_fingerprint() != fp
    finally:
        scoring.JUDGE_SYSTEM = original


# --- 2.5 pin the forced-tool schema ---------------------------------------


def test_verdict_tool_schema_is_pinned():
    # A mutation that stripped this enum kept all 200 tests green: nothing
    # asserted the schema, so an unconstrained verdict string was invisible.
    schema = VERDICT_TOOL["input_schema"]
    assert VERDICT_TOOL["name"] == "emitir_veredicto"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["cited_artifacts", "rationale", "verdict"]
    assert schema["properties"]["verdict"]["enum"] == list(VERDICTS)
    assert set(VERDICTS) == {CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO}
    assert schema["properties"]["cited_artifacts"]["type"] == "array"
    assert schema["properties"]["cited_artifacts"]["items"]["type"] == "string"
    # the schema must stay JSON-serializable for the manifest hash
    json.dumps(VERDICT_TOOL, sort_keys=True)


def test_judge_system_prompt_keeps_the_citation_rule():
    assert "cited_artifacts" in JUDGE_SYSTEM
    assert "NO_EVALUADO" in JUDGE_SYSTEM


# ==========================================================================
# Scoring-core tests moved here from test_report.py, which was rendering plus
# a large uncovered scoring surface. report.py renders; this module decides.
# ==========================================================================


# --- score_test: verdict -> points ----------------------------------------


@pytest.mark.parametrize(
    "verdict,earned,possible",
    [(CUMPLE, 2, 2), (PARCIAL, 1, 2), (NO_CUMPLE, 0, 2), (NO_EVALUADO, 0, 0)],
)
def test_maps_each_verdict_to_points(verdict, earned, possible, ev):
    client = FakeClient({"verdict": verdict, "rationale": "porque sí", "cited_artifacts": [ev]})
    v = score_test(_test(points=2), [_row(artifact=ev)], client)
    assert v.status == verdict
    assert (v.points_earned, v.points_possible) == (earned, possible)


def test_empty_evidence_is_no_evaluado_without_calling_the_api():
    client = FakeClient({"verdict": CUMPLE, "rationale": "x", "cited_artifacts": ["/a"]})
    v = score_test(_test(), [], client, platform="PLATAFORMA_B")
    assert v.status == NO_EVALUADO
    assert (v.points_earned, v.points_possible) == (0, 0)
    assert client.call_count == 0  # did not spend tokens to say "no evidence"


def test_positive_verdict_without_citation_is_coerced_to_no_evaluado(ev):
    client = FakeClient({"verdict": CUMPLE, "rationale": "confío en mí", "cited_artifacts": []})
    v = score_test(_test(points=2), [_row(artifact=ev)], client)
    assert v.status == NO_EVALUADO
    assert (v.points_earned, v.points_possible) == (0, 0)
    assert v.evidence == []
    assert "insuficiente" in v.rationale.lower()


def test_verdict_carries_the_cited_artifacts(tmp_path, ev):
    other = tmp_path / "b.png"
    other.write_bytes(b"\x89PNG\r\n\x1a\n")
    client = FakeClient({"verdict": CUMPLE, "rationale": "ok", "cited_artifacts": [ev, str(other)]})
    v = score_test(_test(), [_row(artifact=ev), _row(kind="screenshot", artifact=str(other))], client)
    assert v.evidence == [ev, str(other)]
    assert v.platform == "PLATAFORMA_B"


@pytest.mark.parametrize("bad", ["PASS", "cumple parcial", "", "Cumple", "no cumple"])
def test_invalid_verdict_string_is_coerced_to_no_evaluado(bad, ev):
    client = FakeClient({"verdict": bad, "rationale": "x", "cited_artifacts": [ev]})
    v = score_test(_test(points=2), [_row(artifact=ev)], client)
    assert v.status == NO_EVALUADO
    assert (v.points_earned, v.points_possible) == (0, 0)


def test_parcial_on_an_odd_point_test_is_half(ev):
    client = FakeClient({"verdict": PARCIAL, "rationale": "x", "cited_artifacts": [ev]})
    v = score_test(_test(points=3), [_row(artifact=ev)], client)
    assert v.points_earned == 1.5
    assert v.points_possible == 3


# --- repeats: report the spread, don't hide it ----------------------------


def _canned(ev, *verdicts):
    return FakeClient(*[{"verdict": v, "rationale": "r", "cited_artifacts": [ev]} for v in verdicts])


def test_repeats_reports_modal_verdict_and_keeps_the_runs(ev):
    v = score_test(_test(points=2), [_row(artifact=ev)], _canned(ev, CUMPLE, CUMPLE, PARCIAL),
                   repeats=3)
    assert v.status == CUMPLE  # modal
    assert v.runs == [CUMPLE, CUMPLE, PARCIAL]  # spread preserved


def test_tiebreak_two_way_picks_the_conservative_verdict(ev):
    v = score_test(_test(points=2), [_row(artifact=ev)], _canned(ev, CUMPLE, NO_CUMPLE), repeats=2)
    assert v.status == NO_CUMPLE  # not first-seen CUMPLE
    assert v.runs == [CUMPLE, NO_CUMPLE]  # spread preserved


def test_tiebreak_three_way_among_scored_picks_the_most_conservative(ev):
    v = score_test(_test(points=2), [_row(artifact=ev)],
                   _canned(ev, CUMPLE, PARCIAL, NO_CUMPLE), repeats=3)
    assert v.status == NO_CUMPLE
    assert v.runs == [CUMPLE, PARCIAL, NO_CUMPLE]


def test_clear_plurality_beats_the_conservative_order_end_to_end(ev):
    v = score_test(_test(points=2), [_row(artifact=ev)], _canned(ev, CUMPLE, CUMPLE, PARCIAL),
                   repeats=3)
    assert v.status == CUMPLE  # plurality wins; tie-break not invoked


# --- aggregate: NO_EVALUADO excluded from the denominator -----------------


def test_aggregate_excludes_no_evaluado_from_earned_and_possible():
    # THE replication-critical rule: a domain of 3 one-point tests where one is
    # CUMPLE, one PARCIAL, one NO_EVALUADO scores 1.5/2 = 75%, NOT 1.5/3 = 50%.
    tests = [_test(points=1, tid=t) for t in ("a", "b", "c")]
    verdicts = [
        TestVerdict("a", "PLATAFORMA_B", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE]),
        TestVerdict("b", "PLATAFORMA_B", PARCIAL, 0.5, 1, "r", ["/b"], [PARCIAL]),
        TestVerdict("c", "PLATAFORMA_B", NO_EVALUADO, 0, 0, "r", [], [NO_EVALUADO]),
    ]
    d2 = aggregate(_criteria(*tests), verdicts)["D2"]
    assert d2.earned == 1.5
    assert d2.possible == 2  # NOT 3 — the NO_EVALUADO test is out of the denominator
    assert d2.pct == 75.0
    assert d2.n_scored == 2 and d2.n_pending == 1


def test_aggregate_test_with_no_verdict_is_pending_not_zero():
    tests = [_test(points=1, tid="a"), _test(points=1, tid="b")]
    verdicts = [TestVerdict("a", "PLATAFORMA_B", CUMPLE, 1, 1, "r", ["/a"], [CUMPLE])]
    d2 = aggregate(_criteria(*tests), verdicts)["D2"]
    assert d2.earned == 1 and d2.possible == 1 and d2.pct == 100.0
    assert d2.n_scored == 1 and d2.n_pending == 1


def test_aggregate_raises_on_mixed_platforms():
    from lxp_eval.evalkit.errors import EvalkitError

    crit = _criteria(_test(points=2, tid="1.1"))
    verdicts = [
        TestVerdict("1.1", "PLATAFORMA_B", CUMPLE, 2, 2, "r", ["/a"], [CUMPLE]),
        TestVerdict("1.1", "Plataforma A", NO_CUMPLE, 0, 2, "r", ["/b"], [NO_CUMPLE]),
    ]
    with pytest.raises(EvalkitError):
        aggregate(crit, verdicts)


def test_aggregate_by_platform_keeps_both_verdicts_for_a_shared_id():
    from lxp_eval.evalkit.scoring import aggregate_by_platform

    crit = _criteria(_test(points=2, tid="1.1"))
    verdicts = [
        TestVerdict("1.1", "PLATAFORMA_B", CUMPLE, 2, 2, "r", ["/a"], [CUMPLE]),
        TestVerdict("1.1", "Plataforma A", NO_CUMPLE, 0, 2, "r", ["/b"], [NO_CUMPLE]),
    ]
    by_p = aggregate_by_platform(crit, verdicts)
    assert (by_p["PLATAFORMA_B"]["D2"].earned, by_p["PLATAFORMA_B"]["D2"].possible) == (2, 2)
    assert (by_p["Plataforma A"]["D2"].earned, by_p["Plataforma A"]["D2"].possible) == (0, 2)


# --- content building, caching, missing-artifact integrity ----------------


def _shot_row(artifact, note="algo que vi"):
    return _row(kind="screenshot", artifact=str(artifact), note=note)


def test_build_content_sends_png_and_jpg_with_correct_media_type(tmp_path):
    png = tmp_path / "confab.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n")
    jpg = tmp_path / "safety.jpg"; jpg.write_bytes(b"\xff\xd8\xff\xe0")
    content, missing = _build_content(_test(), [_shot_row(png), _shot_row(jpg)])
    media = [b["source"]["media_type"] for b in content if b.get("type") == "image"]
    assert media == ["image/png", "image/jpeg"]
    assert missing == set()


def test_cache_breakpoint_is_on_the_last_content_block(tmp_path):
    # images must sit INSIDE the cached prefix, so the breakpoint moves off the
    # text block onto the last (image) block.
    png = tmp_path / "a.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n")
    content, _ = _build_content(_test(), [_shot_row(png)])
    assert "cache_control" not in content[0]  # text block is no longer the breakpoint
    assert content[-1]["type"] == "image"
    assert content[-1]["cache_control"] == {"type": "ephemeral"}


def test_missing_screenshot_is_marked_and_not_sent(tmp_path):
    gone = tmp_path / "gone.png"  # never created
    content, missing = _build_content(_test(), [_shot_row(gone)])
    assert "archivo no encontrado" in content[0]["text"]
    assert str(gone) in missing
    assert all(b.get("type") == "text" for b in content)  # no image block sent


def test_missing_screenshot_cannot_be_cited(tmp_path):
    # the model cites an artifact whose image was never sent -> the citation is
    # stripped, and a positive verdict with nothing left is coerced.
    gone = tmp_path / "gone.png"  # never created
    client = FakeClient({"verdict": CUMPLE, "rationale": "lo vi", "cited_artifacts": [str(gone)]})
    v = score_test(_test(points=2), [_shot_row(gone)], client)
    assert v.status == NO_EVALUADO
    assert str(gone) not in v.evidence


def test_a_starved_judge_is_not_reported_as_missing_evidence():
    """Running out of OUTPUT budget is not the evidence's fault.

    PT-D8-01 was scored NO_EVALUADO with "Evidencia insuficiente: el modelo no
    citó ningún artefacto válido" across two full live runs. The evidence was
    fine. The judge reached a verdict and hit `max_tokens` before it could emit
    `cited_artifacts`, so the citation check discarded a real verdict and the
    message blamed the artifacts -- which sent the investigation after the
    capture pipeline for hours.

    A verdict the model cannot point at is still not defensible, so it is still
    NO_EVALUADO. But the reason must name what actually happened.
    """
    from lxp_eval.evalkit.scoring import _one_run, _TOOL_NAME, NO_EVALUADO

    class _Block:
        type, name = "tool_use", _TOOL_NAME
        input = {"verdict": "NO_CUMPLE", "rationale": "..."}   # no cited_artifacts

    class _Resp:
        content = [_Block()]
        stop_reason = "max_tokens"

    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] > 1024, (
                    "the judge needs room to cite: 1024 truncated the tool call "
                    "on every multi-stimulus criterion"
                )
                return _Resp()

    test = SimpleNamespace(id="PT-D8-01", name="x", pass_criteria="", fail_criteria="")
    status, rationale, cited = _one_run(test, [], _Client(), "claude-opus-4-8")

    assert status == NO_EVALUADO
    assert "Evidencia insuficiente" not in rationale, "must not blame the artifacts"
    assert "max_tokens" in rationale or "presupuesto" in rationale.lower(), rationale


# --- the judge's OWN copy of the 8000px limit --------------------------------


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_pillow(), reason="requiere Pillow para reescalar")
def test_image_block_downscales_a_tall_shot_below_the_api_limit(tmp_path):
    """A full-page screenshot of a long public page 400s the JUDGE.

    The browser agent downscales what IT sends, but the scorer reads the same
    screenshots off disk through a SEPARATE path. A vendor's privacy policy is
    a tall page; its full-page shot was >8000px and the judge call died with
    `image dimensions exceed max allowed size: 8000 pixels` -- AFTER the browser
    work was paid for. Same seam, second copy of the fix. The disk file must
    stay full-res (its ledger sha256 depends on it); only the sent bytes shrink.
    """
    import base64
    import io

    from PIL import Image

    tall = tmp_path / "privacy_full.png"
    Image.new("RGB", (1280, 12000), "white").save(tall, "PNG")  # 12000px > 8000
    original_bytes = tall.read_bytes()

    block = _image_block(tall)

    assert block is not None
    sent = Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"])))
    assert max(sent.size) <= 8000, f"aún excede el límite: {sent.size}"
    assert tall.read_bytes() == original_bytes, "el archivo en disco no debe tocarse"


def test_image_block_leaves_a_small_shot_untouched(tmp_path):
    """A normal lesson-panel shot is well under the limit and must pass through."""
    if not _has_pillow():
        pytest.skip("requiere Pillow")
    import base64
    import io

    from PIL import Image

    small = tmp_path / "lesson.png"
    Image.new("RGB", (1200, 800), "white").save(small, "PNG")
    block = _image_block(small)
    sent = Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"])))
    assert sent.size == (1200, 800)


def test_image_block_missing_file_is_none(tmp_path):
    assert _image_block(tmp_path / "nope.png") is None


# --- the 4000-char cap that turned six real criteria into NO_EVALUADO -------


def test_a_vendor_privacy_policy_reaches_the_judge_whole(tmp_path):
    """A ~25k-character legal document must not be cut before the judge.

    This is the bug that wasted a scoring pass. The runner captures page text
    UNTRUNCATED on purpose, then the judge prompt inlined only the first 4000
    characters. A privacy policy's deciding clause ("we do not sell student
    data") sits well past that, so all six privacy criteria came back
    NO_EVALUADO -- the judge could see the document open and could not see the
    sentence it was asked about.
    """
    from lxp_eval.evalkit.scoring import MAX_INLINE_CHARS, _read_text_artifact

    policy = tmp_path / "privacy.txt"
    clause = "Plataforma A does not sell or rent student personal information."
    # The clause sits at ~12k chars in, like the real one; document ~25k.
    policy.write_text("A" * 12_000 + clause + "B" * 13_000, encoding="utf-8")

    text = _read_text_artifact(policy)

    assert clause in text, "the deciding clause never reached the judge"
    assert MAX_INLINE_CHARS >= 25_000, "the cap must fit a whole policy"


def test_text_past_the_cap_is_still_marked_as_truncated(tmp_path):
    """Raising the cap must not silence the truncation admission.

    A judge shown an excerpt while believing it has the document is how a
    confident wrong verdict gets made. If we still cut, we still say so.
    """
    from lxp_eval.evalkit import scoring

    doc = tmp_path / "huge.txt"
    doc.write_text("x" * (scoring.MAX_INLINE_CHARS + 500), encoding="utf-8")

    text = scoring._read_text_artifact(doc)

    assert "truncado" in text.lower()
