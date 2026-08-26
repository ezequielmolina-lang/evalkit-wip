"""Base error type and the shared author-facing message shape for evalkit.

Every author-facing evalkit error subclasses EvalkitError, so the CLI can catch
one type and render one friendly message instead of chasing a growing tuple as
new modules (probes, ledger, runner, report) land. Raise it with the pieces --

    raise ProbeError("el paso 3 no existe", path=p, line=12, hint="(pista)")

-- so that no module has to copy a formatter.

The error carries path/line/text/hint as structured fields rather than a
pre-formatted blob, because:

* the CLI owns presentation. Hand-aligning a hint under its prefix here would
  assume a fixed-width, non-wrapping terminal, and rich (already a dependency)
  re-wraps and fights that padding.
* ledger/report can serialize an error instead of scraping a string.
* tests can assert on facts instead of exact strings.

str() still renders location + text (+ hint on its own line) so the exception
reads correctly in a traceback or anywhere it is printed standalone.

Author-facing text is Spanish (voseo); code and docstrings stay English. Keep
scaffolding punctuation ASCII: an em-dash is unencodable on cp850/cp437, still
the default on Spanish-locale Windows consoles, so it would turn "you have a
typo" into a UnicodeEncodeError traceback. Accented Spanish words are fine.
"""
from __future__ import annotations

from pathlib import Path


class EvalkitError(ValueError):
    """Base for every evalkit error that is meant to be shown to an author.

    Subclasses ValueError to preserve the behaviour callers already rely on.
    """

    def __init__(
        self,
        text: str,
        *,
        path: Path | str | None = None,
        line: int | None = None,
        hint: str | None = None,
    ) -> None:
        self.text = text
        self.path = Path(path) if path is not None else None
        self.line = line
        self.hint = hint
        super().__init__(self.render())

    @property
    def location(self) -> str:
        """'criterios.yaml, linea 7: ', or '' when the error has no position."""
        if self.path is None:
            return ""
        if self.line is None:
            return f"{self.path.name}: "
        return f"{self.path.name}, línea {self.line}: "

    def render(self) -> str:
        """A sensible standalone rendering. The CLI is free to do better."""
        out = f"{self.location}{self.text}"
        if self.hint:
            out += f"\n{self.hint}"
        return out
