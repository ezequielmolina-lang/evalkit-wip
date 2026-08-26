"""Tests for the append-only evidence ledger.

This is the provenance spine: every score in the final report points at a ledger
row, and every row points at an artifact plus its hash. So the tests care about
two things -- that appends accumulate and can be filtered, and that the hash
actually catches a swapped artifact.
"""
import json
from pathlib import Path

from lxp_eval.evalkit.ledger import EVIDENCE_KINDS, GOVERNANCE_KINDS, EvidenceLedger


def test_append_and_read(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"fake-png-bytes")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    e = led.append(
        probe_id="P-1",
        test_id="1.1",
        platform="PLATAFORMA_B", run_index=0,
        url="https://x.test/lesson",
        artifact=shot,
        kind="screenshot",
        note="abrió la lección",
        timestamp="2026-07-16T10:00:00Z",
    )
    assert e.sha256
    rows = led.read_all()
    assert len(rows) == 1
    assert rows[0]["probe_id"] == "P-1"
    assert rows[0]["platform"] == "PLATAFORMA_B"
    assert rows[0]["sha256"] == e.sha256


def test_hash_detects_tampering(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"original")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    e = led.append(
        probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
        artifact=shot, kind="screenshot", note="", timestamp="t",
    )
    assert led.verify(e) is True
    shot.write_bytes(b"tampered")
    assert led.verify(e) is False


def test_verify_false_when_artifact_missing(tmp_path: Path):
    shot = tmp_path / "s.png"
    shot.write_bytes(b"x")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    e = led.append(
        probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
        artifact=shot, kind="screenshot", note="", timestamp="t",
    )
    shot.unlink()
    assert led.verify(e) is False


def test_appends_are_cumulative(tmp_path: Path):
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    for i in range(3):
        led.append(
            probe_id=f"P-{i}", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
            artifact=a, kind="screenshot", note="", timestamp="t",
        )
    assert len(led.read_all()) == 3


def test_read_all_empty_when_no_file(tmp_path: Path):
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    assert led.read_all() == []


def test_for_test_filters_by_test_id(tmp_path: Path):
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    led.append(probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u", artifact=a, kind="screenshot", note="", timestamp="t")
    led.append(probe_id="P-2", test_id="2.2", platform="PLATAFORMA_B", run_index=0, url="u", artifact=a, kind="screenshot", note="", timestamp="t")
    assert [r["probe_id"] for r in led.for_test("1.1")] == ["P-1"]


def test_two_platforms_same_test_do_not_blend(tmp_path: Path):
    # PLATAFORMA_B and Plataforma A are scored against the SAME test_ids. Evidence for one must
    # never leak into the other's score.
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    led.append(probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u", artifact=a, kind="screenshot", note="", timestamp="t")
    led.append(probe_id="P-2", test_id="1.1", platform="Plataforma A", run_index=0, url="u", artifact=a, kind="screenshot", note="", timestamp="t")
    plataforma_b = led.for_platform_test("PLATAFORMA_B", "1.1")
    assert [r["probe_id"] for r in plataforma_b] == ["P-1"]
    assert [r["probe_id"] for r in led.for_platform_test("Plataforma A", "1.1")] == ["P-2"]


# --- encoding conventions (write-boundary lessons) -------------------------


def test_accented_and_em_dash_note_roundtrips(tmp_path: Path):
    # a bare open() would write cp1252 on Windows and mangle this. The ledger
    # must force utf-8 on BOTH append and read.
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    note = "abrió la lección — sí modeló antes (ñ, á, é, ü)"
    led.append(
        probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
        artifact=a, kind="screenshot", note=note, timestamp="t",
    )
    assert led.read_all()[0]["note"] == note


def test_jsonl_is_human_readable_not_escaped(tmp_path: Path):
    # ensure_ascii=False: the file is meant to be opened and read by a human
    # auditor, so accents must be literal, not \uXXXX.
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    ledger_path = tmp_path / "ledger.jsonl"
    led = EvidenceLedger(ledger_path)
    led.append(
        probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
        artifact=a, kind="screenshot", note="abrió la lección", timestamp="t",
    )
    raw = ledger_path.read_text(encoding="utf-8")
    assert "abrió la lección" in raw
    assert "\\u" not in raw
    # and it is one valid JSON object per line
    assert json.loads(raw.splitlines()[0])["note"] == "abrió la lección"


# --- idempotency at the SCORING accessor -----------------------------------


def test_scoring_accessor_dedupes_identical_artifacts(tmp_path: Path):
    # Verified defect: three invocations of rederive append the same screenshot
    # three times, and all three copies were fed to the judge in ONE prompt —
    # 3x image tokens plus a repetition that can itself shift a verdict. The raw
    # log stays append-only (tamper-evidence); the scoring accessor dedupes.
    a = tmp_path / "a.png"
    a.write_bytes(b"\x89PNG\r\n\x1a\n")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    for i in range(3):
        led.append(
            probe_id=f"P-{i}", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
            artifact=a, kind="screenshot", note="", timestamp="t",
        )
    assert len(led.read_all()) == 3          # the log is still append-only
    rows = led.for_platform_test("PLATAFORMA_B", "1.1")
    assert len(rows) == 1                    # the judge sees it once
    assert rows[0]["probe_id"] == "P-0"      # the FIRST capture is kept


def test_dedupe_keeps_distinct_content_and_does_not_cross_platforms(tmp_path: Path):
    a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG-a")
    b = tmp_path / "b.png"; b.write_bytes(b"\x89PNG-b")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    for art in (a, b, a):
        led.append(probe_id="P", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
                   artifact=art, kind="screenshot", note="", timestamp="t")
    led.append(probe_id="P", test_id="1.1", platform="Plataforma A", run_index=0, url="u",
               artifact=a, kind="screenshot", note="", timestamp="t")
    plataforma_b = led.for_platform_test("PLATAFORMA_B", "1.1")
    assert [r["artifact"] for r in plataforma_b] == [str(a), str(b)]
    # the same bytes on the OTHER platform are a separate observation, not a dupe
    assert len(led.for_platform_test("Plataforma A", "1.1")) == 1


def test_same_bytes_under_two_test_ids_are_not_deduped(tmp_path: Path):
    # one screenshot can legitimately be evidence for two different cells
    a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    led.append(probe_id="P-1", test_id="1.1", platform="PLATAFORMA_B", run_index=0, url="u",
               artifact=a, kind="screenshot", note="", timestamp="t")
    led.append(probe_id="P-2", test_id="2.2", platform="PLATAFORMA_B", run_index=0, url="u",
               artifact=a, kind="screenshot", note="", timestamp="t")
    assert len(led.for_platform_test("PLATAFORMA_B", "1.1")) == 1
    assert len(led.for_platform_test("PLATAFORMA_B", "2.2")) == 1


def test_an_undecodable_byte_in_a_note_does_not_kill_the_run(tmp_path: Path):
    """The ledger records notes scraped off a live page.

    An append that raises takes down the whole run, not just the row, so a
    stray byte in a note must never be able to do that -- and the artifact's
    hash, taken from the file's own bytes, must be untouched by the repair.
    """
    a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG")
    led = EvidenceLedger(tmp_path / "ledger.jsonl")

    entry = led.append(probe_id="P-1", test_id="1.1", platform="Plataforma A", run_index=0,
                       url="u", artifact=a, kind="page_text",
                       note="respuesta: ay\udc81uda", timestamp="t")

    rows = led.read_all()
    assert len(rows) == 1
    assert "\udc81" not in rows[0]["note"]
    assert rows[0]["note"].startswith("respuesta: ay")
    # provenance is unaffected: the hash is of the artifact, not the note
    assert rows[0]["sha256"] == entry.sha256
    assert led.verify(entry) is True
    # and the file an auditor opens is valid UTF-8
    (tmp_path / "ledger.jsonl").read_text(encoding="utf-8")


def test_a_rerun_must_not_overwrite_the_previous_run_s_page_text(tmp_path: Path):
    """Evidence is immutable, so its filename must be unique per capture.

    Screenshots carry a timestamp in their name; page_text captures were named
    `{probe}_r{run}_s{step}.txt` only. Re-running the same probe therefore
    overwrote the earlier text, and the FIRST run's ledger row -- which says
    "this is what we saw" -- silently came to point at bytes from a later run.
    Caught by re-hashing a real job's ledger: 45 of 46 rows verified, and the
    odd one out was a page_text from a run the watchdog had killed.
    """
    from lxp_eval.evalkit.runner import page_text_name

    first = page_text_name("PB-PT-D9-H2", 0, 3, timestamp="2026-07-20T12:14:36+00:00")
    second = page_text_name("PB-PT-D9-H2", 0, 3, timestamp="2026-07-20T12:28:12+00:00")

    assert first != second, "two captures of the same step must not share a filename"
    for name in (first, second):
        assert name.endswith(".txt")
        assert "PB-PT-D9-H2" in name or "pb-pt-d9-h2" in name.lower()
        assert "_r0_s3" in name, "the probe/run/step key stays readable"


def test_governance_and_evidence_kinds_are_disjoint():
    """The partition pin: the two sets split the ledger's kinds between the
    two consumers. Scoring keeps only EVIDENCE_KINDS (fail-closed), provenance
    keeps only GOVERNANCE_KINDS -- a kind in BOTH would put a human decision in
    front of the judge, so the sets must never intersect."""
    assert GOVERNANCE_KINDS & EVIDENCE_KINDS == frozenset()
    # Neither side of the partition is allowed to be empty by accident.
    assert GOVERNANCE_KINDS == frozenset({"approval", "signoff"})
    assert EVIDENCE_KINDS


def test_a_non_evidence_kind_never_reaches_the_scoring_accessors(tmp_path: Path):
    """A signoff row carries a REAL test_id, so (test_id, platform) matches it.

    The approval ledger appends governance rows (kind="approval", "signoff")
    to the same file. Without a kind filter they flow straight into the judge
    prompt and change the frozen instrument. The filter is an allowlist --
    EVIDENCE_KINDS -- so a row kind that does not exist yet is excluded the
    day it is born, not the day someone remembers to exclude it.
    """
    led = EvidenceLedger(tmp_path / "ledger.jsonl")
    for kind in ("screenshot", "page_text", "observation"):
        art = tmp_path / f"{kind}.bin"; art.write_bytes(kind.encode())
        led.append(probe_id=f"P-{kind}", test_id="2.1.2", platform="Plataforma A", run_index=0,
                   url="u", artifact=art, kind=kind, note="", timestamp="t")

    before_platform = json.dumps(led.for_platform_test("Plataforma A", "2.1.2"), sort_keys=True)
    before_test = json.dumps(led.for_test("2.1.2"), sort_keys=True)

    # governance rows for the SAME scored test_id, plus a kind nobody has
    # invented yet -- fail-closed means the unknown one is excluded too
    for kind in ("signoff", "approval", "some_future_kind"):
        art = tmp_path / f"{kind}.json"; art.write_bytes(kind.encode())
        led.append(probe_id=f"G-{kind}", test_id="2.1.2", platform="Plataforma A", run_index=0,
                   url="u", artifact=art, kind=kind, note="", timestamp="t")

    # byte-identical: the instrument did not move
    assert json.dumps(led.for_platform_test("Plataforma A", "2.1.2"), sort_keys=True) == before_platform
    assert json.dumps(led.for_test("2.1.2"), sort_keys=True) == before_test
    # the audit path still returns EVERYTHING -- the log is the record
    assert len(led.read_all()) == 6


def test_scoring_sees_only_the_current_run_s_evidence(tmp_path: Path):
    """The ledger is append-only, so a re-run leaves the failed attempt behind.

    PB-PT-D8-01 was probed twice: 54 rows from an attempt whose page text was
    truncated to the course sidebar, then 54 good ones after the fix. Both
    carry the same test_id, so `for_platform_test` hands the judge all 108 and
    it scores a criterion on evidence from a run that was thrown away.

    `approved_at` is the exact boundary — every approval stamps it, and the
    evidence of a run is what came after it. No timestamp-gap heuristics.
    """
    from lxp_eval.evalkit.ledger import rows_since

    old = [{"test_id": "PT-D8-01", "timestamp": "2026-07-20T13:00:00+00:00"}]
    new = [{"test_id": "PT-D8-01", "timestamp": "2026-07-20T17:05:00+00:00"}]
    rows = old + new

    assert rows_since(rows, "2026-07-20T16:34:26+00:00") == new
    # exactly at the boundary counts as part of the run it opened
    assert rows_since(rows, "2026-07-20T17:05:00+00:00") == new
    # never approved (or a record predating the field): fall back to everything
    assert rows_since(rows, "") == rows
    assert rows_since(rows, None) == rows
