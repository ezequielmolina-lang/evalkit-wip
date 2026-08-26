"""Document extraction tests: an uploaded file becomes text a criterion can cite.

The setup screen promises "los criterios se extraen citando el documento y la
página". That promise has two halves, and both are pinned here:

* the TEXT actually comes out of the real file. Every fixture below is built in
  the test with the real library (openpyxl / python-docx / PyMuPDF), never
  committed as a binary blob: a checked-in .docx is a file nobody can review in
  a diff, and it rots silently when the library that reads it moves on;
* the PROVENANCE survives extraction. A PDF page and an xlsx sheet are the units
  a reader can be sent back to, so `[página N]` / `## <hoja>` markers have to be
  in the text the extractor sees. Without them the framework can cite a document
  but not a place inside it, which is the difference between a citation and a
  gesture.

And the failure modes are LOUD, because the silent versions are worse than the
error: a scanned PDF returning "" would produce a framework that quietly ignored
the ministry's own norm, and a .doc rejected as merely "unsupported" sends the
user hunting for a converter they already have (Word's Save As).
"""
from __future__ import annotations

import io

import pytest

from lxp_eval.evalkit.extract import MAX_CHARS, ExtractError, extract_text


# --- fixtures built with the real libraries ---------------------------------


def _pdf(pages: list[str]) -> bytes:
    """A real PDF with a text layer, one string per page."""
    import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text)
    out = doc.tobytes()
    doc.close()
    return out


def _pdf_without_text_layer() -> bytes:
    """A real PDF with pages but no text at all -- what a scan looks like."""
    import fitz

    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    out = doc.tobytes()
    doc.close()
    return out


def _docx(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    import docx

    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, cell in enumerate(row):
                t.cell(r, c).text = cell
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- each format round-trips its known text ---------------------------------


def test_pdf_round_trips_its_text():
    data = _pdf(["El proveedor debe publicar el modelo de lenguaje que utiliza."])
    out = extract_text("politica.pdf", data)
    assert "publicar el modelo de lenguaje" in out


def test_a_two_page_pdf_marks_every_page():
    """The citation unit for a PDF is the page, so the page has to be IN the text."""
    out = extract_text("pliego.pdf", _pdf(["primera hoja", "segunda hoja"]))
    assert "[página 1]" in out
    assert "[página 2]" in out
    # and the marker precedes the text it introduces, not the other way round
    assert out.index("[página 1]") < out.index("primera hoja") < out.index("[página 2]")
    assert out.index("[página 2]") < out.index("segunda hoja")


def test_docx_round_trips_paragraphs_and_table_cells():
    """A rúbrica is usually a TABLE. Dropping cells would drop the rubric."""
    data = _docx(
        ["Rúbrica de evaluación", "Aplica a toda la plataforma."],
        table=[["Criterio", "Puntaje"], ["Interfaz en español del país", "3"]],
    )
    out = extract_text("rubrica.docx", data)
    assert "Rúbrica de evaluación" in out
    assert "Aplica a toda la plataforma." in out
    assert "Interfaz en español del país" in out
    assert "Puntaje" in out


def test_xlsx_names_each_sheet_and_joins_the_row():
    out = extract_text("matriz.xlsx", _xlsx({
        "Requisitos": [["ID", "Requisito"], ["R1", "Publicar el modelo"]],
        "Puntajes": [["R1", 3]],
    }))
    assert "## Requisitos" in out
    assert "## Puntajes" in out
    assert "R1 | Publicar el modelo" in out
    assert out.index("## Requisitos") < out.index("## Puntajes")


def test_xlsx_skips_fully_empty_rows():
    out = extract_text("matriz.xlsx", _xlsx({
        "Hoja": [["a"], [None], ["", None], ["b"]],
    }))
    body = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("##")]
    assert body == ["a", "b"]


def test_xlsm_is_read_like_xlsx():
    out = extract_text("matriz.xlsm", _xlsx({"Hoja": [["valor conocido"]]}))
    assert "valor conocido" in out


@pytest.mark.parametrize("name", ["norma.txt", "norma.md", "norma.csv"])
def test_plain_text_formats_decode_as_utf8(name):
    out = extract_text(name, "acompañamiento pedagógico".encode("utf-8"))
    assert out == "acompañamiento pedagógico"


def test_undecodable_bytes_are_replaced_not_fatal():
    """A latin-1 CSV is a real thing a ministry sends. Mojibake beats a crash."""
    out = extract_text("norma.csv", b"norma \xff\xfe valida")
    assert "norma" in out and "valida" in out


# --- the loud failures ------------------------------------------------------


def test_a_scanned_pdf_says_so_instead_of_returning_nothing():
    """Silence here would drop the ministry's own norm from the framework."""
    with pytest.raises(ExtractError) as e:
        extract_text("escaneo.pdf", _pdf_without_text_layer())
    msg = str(e.value)
    assert "escaneado" in msg
    assert "OCR" in msg


def test_an_unsupported_extension_names_it_and_lists_what_works():
    with pytest.raises(ExtractError) as e:
        extract_text("presentacion.pptx", b"lo que sea")
    msg = str(e.value)
    assert ".pptx" in msg
    assert ".pdf" in msg and ".docx" in msg and ".xlsx" in msg


def test_a_file_with_no_extension_is_refused_by_name():
    with pytest.raises(ExtractError) as e:
        extract_text("norma", b"lo que sea")
    assert "norma" in str(e.value)


@pytest.mark.parametrize("name,save_as", [("norma.doc", ".docx"), ("matriz.xls", ".xlsx")])
def test_the_old_binary_office_formats_tell_you_to_save_as_the_new_one(name, save_as):
    """"No soportado" sends the user looking for a converter they already have."""
    with pytest.raises(ExtractError) as e:
        extract_text(name, b"\xd0\xcf\x11\xe0")
    msg = str(e.value)
    assert save_as in msg
    assert "Guardá" in msg or "guardá" in msg


def test_an_empty_file_raises_instead_of_returning_an_empty_string():
    with pytest.raises(ExtractError) as e:
        extract_text("norma.txt", b"")
    assert "no contiene texto extraíble" in str(e.value)


def test_a_whitespace_only_file_raises_too():
    with pytest.raises(ExtractError) as e:
        extract_text("norma.md", b"   \n\n\t  \n")
    assert "no contiene texto extraíble" in str(e.value)


def test_an_xlsx_with_only_empty_rows_raises():
    with pytest.raises(ExtractError) as e:
        extract_text("matriz.xlsx", _xlsx({"Hoja": [[None], ["", None]]}))
    assert "no contiene texto extraíble" in str(e.value)


def test_a_corrupt_docx_fails_in_spanish_naming_the_file():
    with pytest.raises(ExtractError) as e:
        extract_text("rubrica.docx", b"esto no es un docx")
    assert "rubrica.docx" in str(e.value)


def test_a_corrupt_pdf_fails_in_spanish_naming_the_file():
    with pytest.raises(ExtractError) as e:
        extract_text("pliego.pdf", b"esto no es un pdf")
    assert "pliego.pdf" in str(e.value)


# --- the cap ----------------------------------------------------------------


def test_an_enormous_document_is_truncated_with_a_visible_marker():
    """An 800-page procurement PDF must not become the job payload."""
    out = extract_text("gigante.txt", ("x" * (MAX_CHARS + 5_000)).encode("utf-8"))
    assert len(out) <= MAX_CHARS + 200      # the marker is allowed past the cap
    assert "truncado" in out


def test_a_document_under_the_cap_carries_no_truncation_marker():
    out = extract_text("chico.txt", b"norma breve")
    assert "truncado" not in out


# --- the extension is read from the NAME, case-insensitively ----------------


def test_the_extension_is_matched_case_insensitively():
    out = extract_text("NORMA.TXT", "texto conocido".encode("utf-8"))
    assert out == "texto conocido"


def test_extract_error_is_an_evalkit_error():
    """So the service's existing EvalkitError handler renders it as a 400."""
    from lxp_eval.evalkit.errors import EvalkitError

    assert issubclass(ExtractError, EvalkitError)
