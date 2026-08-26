"""An uploaded file -> the text the framework builder extracts criteria from.

The setup screen promises: "Adjuntar una norma, un pliego de licitación o tu
propia rúbrica -- PDF, Word o Excel. Los criterios se extraen citando el
documento y la página. Una norma tuya pesa más que cualquier cosa que
generemos." `framework._extract` already keeps the second half of that promise
-- it quotes the document and verifies the quote is really in it -- but it takes
`Document(name, text)`, and nothing turned a PDF into that text. This module is
the missing half, and nothing more: it parses bytes, it stores nothing, it
opens no path the caller names, and it never talks to a model.

Two design choices carry the weight:

* **PROVENANCE IS IN THE TEXT.** A citation the reader cannot walk back to a
  place is a gesture, not a citation. PDF pages get a `[página N]` line and xlsx
  sheets a `## <hoja>` heading, so the extractor -- which sees only this string
  -- can say WHERE in a 300-page pliego a requirement lives. `Document.locate`
  then resolves the quote to a line, and the line lands between the markers that
  name the page.

* **NO SILENT EMPTY.** Every failure raises. A scanned PDF that returned "" would
  produce a framework that ignored the ministry's own norm while looking like it
  had read it -- the same class of failure as a fabricated citation, arrived at
  from the other side. The user uploaded that file precisely because it outranks
  everything we generate; dropping it quietly is the one outcome that must be
  impossible.

Spanish (voseo) for anything a user reads; ASCII scaffolding, no em-dash (see
`errors.py` for the cp850 console reason). Accented words are fine.
"""
from __future__ import annotations

import io
from pathlib import Path

from .errors import EvalkitError


class ExtractError(EvalkitError):
    """A file the product cannot turn into text, said out loud.

    An `EvalkitError`, so the service's existing exception handler renders it as
    a 400 with the Spanish message and no second error shape has to exist.
    """


#: Hard ceiling on the returned text. A national procurement pliego runs to
#: hundreds of pages; the whole thing would ride into every job payload, every
#: prompt and every job.json on disk. 400k chars is roughly 100k tokens -- more
#: than a careful extraction pass should ever need -- and the cut is ANNOUNCED
#: in the text rather than performed quietly, because a criterion silently
#: missing from page 700 looks exactly like a criterion the document never had.
MAX_CHARS = 400_000

TRUNCATION_MARK = (
    "\n\n[... documento truncado: se leyeron los primeros {n:,} caracteres. "
    "Si lo que te importa está más adelante, subí sólo esa sección.]"
)

#: What the dropzone accepts, in the order a user thinks about them. Named in
#: the rejection message so the fix is IN the error and nobody has to go back to
#: the screen to find out what would have worked.
SUPPORTED = (".pdf", ".docx", ".xlsx", ".xlsm", ".txt", ".md", ".csv")

#: The 1997 binary formats. `python-docx` and `openpyxl` read neither, and the
#: user is one "Guardar como" away from a file that works -- so say THAT instead
#: of "no soportado", which sends them hunting for a converter they already have.
LEGACY = {".doc": ".docx", ".xls": ".xlsx"}


def extract_text(filename: str, data: bytes) -> str:
    """The text of `data`, dispatching on `filename`'s extension.

    `filename` is used for its EXTENSION and for error messages only. Nothing is
    opened, resolved or written: the bytes are already in hand, so a client
    cannot steer this at a path.
    """
    name = Path(str(filename or "")).name
    ext = Path(name).suffix.lower()

    if not ext:
        raise ExtractError(
            f'el archivo "{name or "(sin nombre)"}" no tiene extensión, así que '
            "no se sabe cómo leerlo - los formatos aceptados son: "
            + ", ".join(SUPPORTED)
        )
    if ext in LEGACY:
        raise ExtractError(
            f'"{name}" está en el formato binario antiguo de Office ({ext}), que '
            f"no se puede leer. Guardá el archivo como {LEGACY[ext]} "
            f'("Archivo > Guardar como" y elegí {LEGACY[ext]}) y volvé a subirlo.'
        )
    if ext not in SUPPORTED:
        raise ExtractError(
            f'no se puede leer "{name}": la extensión {ext} no está soportada - '
            "los formatos aceptados son: " + ", ".join(SUPPORTED)
        )

    if ext == ".pdf":
        text = _pdf(name, data)
    elif ext == ".docx":
        text = _docx(name, data)
    elif ext in (".xlsx", ".xlsm"):
        text = _xlsx(name, data)
    else:
        # The decode cannot fail: a ministry's CSV exported as latin-1 is a real
        # thing, and a replacement char in one cell beats refusing the file.
        text = data.decode("utf-8", errors="replace")

    text = text.strip("\n")
    if not text.strip():
        raise ExtractError(
            f'el archivo "{name}" no contiene texto extraíble - se abrió bien, '
            "pero está vacío"
        )
    return _cap(text)


def _cap(text: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return text[:MAX_CHARS] + TRUNCATION_MARK.format(n=MAX_CHARS)


# --- PDF --------------------------------------------------------------------


def _pdf(name: str, data: bytes) -> str:
    """Page text with `[página N]` markers, pypdf first, pdfplumber second.

    Two readers because they fail differently and a norm is worth a second
    attempt: pypdf is fast and handles most producers, pdfplumber reconstructs
    from character positions and gets text out of layouts pypdf returns blank
    for. Only the SECOND blank is a scan.
    """
    pages = _pdf_pypdf(name, data)
    if not any(p.strip() for p in pages):
        pages = _pdf_plumber(name, data) or pages
    if not any(p.strip() for p in pages):
        # A PDF that opened and paginated but has no text layer at all. Saying
        # "vacío" would be true and useless; the user needs to know it is a
        # PHOTOGRAPH of a document and what to do about it.
        raise ExtractError(
            f'"{name}" parece un PDF escaneado: tiene {len(pages)} página(s) pero '
            "ningún texto seleccionable, así que no hay nada que citar. Pasalo por "
            "OCR (reconocimiento de texto) o subí una copia con texto -- la que "
            "generó el documento original, no una impresión escaneada."
        )
    return "\n".join(f"\n[página {i}]\n{p}" for i, p in enumerate(pages, start=1))


def _pdf_pypdf(name: str, data: bytes) -> list[str]:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        return [(page.extract_text() or "") for page in reader.pages]
    except ExtractError:
        raise
    except Exception as exc:  # noqa: BLE001 -- pypdf raises a zoo of types
        raise ExtractError(
            f'no se pudo abrir "{name}" como PDF: el archivo está dañado, cifrado '
            f"o no es realmente un PDF ({type(exc).__name__})"
        ) from exc


def _pdf_plumber(name: str, data: bytes) -> list[str]:
    """The fallback. Best-effort by design: pypdf already reported the real error.

    Returns [] if it cannot help, so the caller falls back to the pypdf pages and
    the scanned-PDF message -- a second traceback here would replace an accurate
    diagnosis with a library name.
    """
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [(page.extract_text() or "") for page in pdf.pages]
    except Exception:  # noqa: BLE001
        return []


# --- Word -------------------------------------------------------------------


def _docx(name: str, data: bytes) -> str:
    """Paragraphs, then table cells. A rúbrica is usually a TABLE.

    `document.paragraphs` skips everything inside a table, so a rubric uploaded
    as the grid it is drawn as would extract to its title and nothing else. The
    tables are appended row by row with the same ` | ` separator the spreadsheet
    path uses, so a downstream reader sees one shape for tabular data.
    """
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 -- a bad zip raises from deep inside
        raise ExtractError(
            f'no se pudo abrir "{name}" como documento de Word: el archivo está '
            f"dañado o no es realmente un .docx ({type(exc).__name__})"
        ) from exc

    parts = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))
    return "\n".join(parts)


# --- Excel ------------------------------------------------------------------


def _xlsx(name: str, data: bytes) -> str:
    """One `## <hoja>` heading per sheet, rows joined by ` | `.

    `data_only=True` on purpose: a matriz de evaluación is full of formulas, and
    `=SUMA(B2:B9)` is not a requirement anybody can cite. What that costs is
    stated rather than hidden -- a workbook Excel never opened has no cached
    result, so those cells come out empty and the row still carries its labels.

    The sheet heading is the xlsx analogue of `[página N]`: it is the place a
    reader can be sent back to.
    """
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractError(
            f'no se pudo abrir "{name}" como libro de Excel: el archivo está '
            f"dañado o no es realmente un .xlsx ({type(exc).__name__})"
        ) from exc

    try:
        out: list[str] = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v).strip() for v in row]
                if not any(cells):
                    continue      # a spacer row is layout, not content
                rows.append(" | ".join(cells).strip(" |"))
            # An empty sheet contributes NO heading. Emitting one would make a
            # workbook of nothing but blank sheets extract to a page of "## Hoja1
            # ## Hoja2" -- non-empty text, so the empty-file check would pass it,
            # and the user would be told nothing while their file was ignored.
            if rows:
                out.append(f"## {ws.title}")
                out.extend(rows)
        return "\n".join(out)
    finally:
        wb.close()
