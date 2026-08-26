"""Rescuing evidence that is on disk but belongs to no approved run."""
from __future__ import annotations

import json

import pytest

from lxp_eval.evalkit.rescue import (
    CAPTURE_TRUNCATION_MARKER,
    RescueSelection,
    is_capture_truncated,
    select_archived_evidence,
)

PLATFORM = "app.plataforma-a.ejemplo"


def _row(tmp_path, *, test_id, kind, stamp, body="contenido completo de la pagina"):
    """A ledger row whose artifact actually exists on disk."""
    name = f"{test_id}_{kind}_{stamp.replace(':', '')}.{'png' if kind == 'screenshot' else 'txt'}"
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return {
        "kind": kind,
        "test_id": test_id,
        "probe_id": f"PB-{test_id}",
        "platform": PLATFORM,
        "timestamp": stamp,
        "artifact": str(path),
        "sha256": "irrelevante-para-la-seleccion",
        "note": f"paso de {test_id}",
    }


def test_a_capture_the_pipeline_marked_as_truncated_is_recognised():
    """The marker is written INTO the file at capture time, not at judge time.

    `_read_text_artifact` adds its own marker when it trims for the prompt; this
    one is baked into the artifact by the capture, so the missing characters are
    gone from disk and no judge will ever see them.
    """
    assert is_capture_truncated(
        "Home BM - Matematicas Grado 3 ... "
        f"{CAPTURE_TRUNCATION_MARKER}: se omitieron 16213 de 24213 caracteres]"
    )
    assert not is_capture_truncated("Home BM - Matematicas Grado 3, leccion completa")


def test_truncated_captures_are_excluded_and_counted(tmp_path):
    """A truncated capture kept only the course sidebar.

    On Plataforma A the lesson body sits at the END of a 24k-character page, so a
    capture cut to its first 8k holds the navigation list and none of the
    content the criterion is about. Judging on it is not a weaker verdict, it
    is a verdict about the wrong thing -- so it is dropped, and SAID.
    """
    rows = [
        _row(tmp_path, test_id="PT-D8-01", kind="page_text", stamp="2026-07-20T13:09:48+00:00",
             body=f"indice lateral {CAPTURE_TRUNCATION_MARKER}: se omitieron 16213 de 24213 caracteres]"),
        _row(tmp_path, test_id="PT-D8-01", kind="page_text", stamp="2026-07-20T16:35:09+00:00",
             body="indice lateral y ademas el contenido de la leccion"),
    ]

    sel = select_archived_evidence(rows, test_id="PT-D8-01", platform=PLATFORM)

    assert [r["timestamp"] for r in sel.rows] == ["2026-07-20T16:35:09+00:00"]
    assert sel.dropped_truncated == 1
    assert "1 captura" in sel.note and "truncada" in sel.note


def test_a_window_keeps_only_the_session_the_caller_asked_for(tmp_path):
    """`since` is the same instrument `rows_since` uses, aimed by hand.

    The ledger holds every attempt on purpose. Rescuing means naming WHICH
    attempt is being judged, not handing the judge all of them.
    """
    rows = [
        _row(tmp_path, test_id="PT-D8-01", kind="screenshot", stamp="2026-07-20T13:09:48+00:00"),
        _row(tmp_path, test_id="PT-D8-01", kind="screenshot", stamp="2026-07-20T16:35:09+00:00"),
    ]

    sel = select_archived_evidence(
        rows, test_id="PT-D8-01", platform=PLATFORM, since="2026-07-20T16:00"
    )

    assert len(sel.rows) == 1
    assert sel.dropped_outside_window == 1


def test_rows_of_other_tests_and_platforms_never_leak_in(tmp_path):
    rows = [
        _row(tmp_path, test_id="PT-D8-01", kind="screenshot", stamp="2026-07-20T16:35:09+00:00"),
        _row(tmp_path, test_id="PT-D9-H1", kind="screenshot", stamp="2026-07-20T16:35:10+00:00"),
    ]
    rows.append({**_row(tmp_path, test_id="PT-D8-01", kind="screenshot",
                        stamp="2026-07-20T16:35:11+00:00"), "platform": "otra.plataforma"})

    sel = select_archived_evidence(rows, test_id="PT-D8-01", platform=PLATFORM)

    assert len(sel.rows) == 1
    assert sel.rows[0]["test_id"] == "PT-D8-01"


def test_governance_rows_are_not_evidence(tmp_path):
    """An approval row shares the file and can share a test_id. It is not evidence."""
    rows = [
        _row(tmp_path, test_id="PT-D8-01", kind="screenshot", stamp="2026-07-20T16:35:09+00:00"),
        {**_row(tmp_path, test_id="PT-D8-01", kind="page_text", stamp="2026-07-20T16:35:20+00:00"),
         "kind": "approval"},
    ]

    sel = select_archived_evidence(rows, test_id="PT-D8-01", platform=PLATFORM)

    assert [r["kind"] for r in sel.rows] == ["screenshot"]


def test_identical_captures_are_deduped_by_content(tmp_path):
    """Twenty-seven reads of one page are one piece of evidence recorded 27 times."""
    same = "la misma pagina exacta"
    rows = [
        _row(tmp_path, test_id="PT-D8-01", kind="page_text",
             stamp=f"2026-07-20T16:3{i}:09+00:00", body=same)
        for i in range(3)
    ]

    sel = select_archived_evidence(rows, test_id="PT-D8-01", platform=PLATFORM)

    assert len(sel.rows) == 1
    assert sel.dropped_duplicate == 2


def test_an_artifact_that_is_gone_is_dropped_not_guessed(tmp_path):
    """The 'abrí la carpeta' guarantee: never cite what was not sent."""
    row = _row(tmp_path, test_id="PT-D8-01", kind="page_text", stamp="2026-07-20T16:35:09+00:00")
    (tmp_path / "borrado.txt").write_text("x", encoding="utf-8")
    gone = {**row, "artifact": str(tmp_path / "no-existe.txt"),
            "timestamp": "2026-07-20T16:36:09+00:00"}

    sel = select_archived_evidence([row, gone], test_id="PT-D8-01", platform=PLATFORM)

    assert len(sel.rows) == 1
    assert sel.dropped_missing == 1


def test_nothing_usable_is_an_empty_selection_that_explains_itself(tmp_path):
    """Empty must never read as 'the platform failed', which is the whole bug.

    PT-D9-H1 has 32 ledger rows and not one usable text. A report that renders
    that as an unmet criterion says the vendor hid something; the truth is that
    our capture truncated the page.
    """
    rows = [
        _row(tmp_path, test_id="PT-D9-H1", kind="page_text", stamp="2026-07-20T12:49:00+00:00",
             body=f"solo indice {CAPTURE_TRUNCATION_MARKER}: se omitieron 16213 de 24213 caracteres]"),
    ]

    sel = select_archived_evidence(rows, test_id="PT-D9-H1", platform=PLATFORM)

    assert sel.rows == []
    assert not sel.usable
    assert "truncada" in sel.note


def test_the_selection_serialises_for_the_artifact(tmp_path):
    """Whatever the rescue prints has to be machine-readable too."""
    rows = [_row(tmp_path, test_id="PT-D8-01", kind="screenshot",
                 stamp="2026-07-20T16:35:09+00:00")]

    sel = select_archived_evidence(rows, test_id="PT-D8-01", platform=PLATFORM,
                                   since="2026-07-20T16:00")

    d = sel.to_dict()
    assert json.loads(json.dumps(d))["testId"] == "PT-D8-01"
    assert d["window"]["since"] == "2026-07-20T16:00"
    assert d["kept"] == 1
