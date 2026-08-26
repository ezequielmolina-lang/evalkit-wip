"""Shared building blocks for author-facing validation messages.

criteria and probes (and later modules) all report the same kinds of author
mistake -- a missing field, a blank field, "which thing are we talking about" --
and must word them identically so an author sees one consistent voice. These
helpers are that shared vocabulary; each loader supplies its own `kind`
("dominio", "test", "probe").

Spanish text (voseo), ASCII scaffolding (no em-dash: it is unencodable on the
cp850/cp437 consoles still default on Spanish-locale Windows). See
evalkit.errors for why that matters.
"""
from __future__ import annotations

from typing import Any

from .yamlsrc import raw_text


def is_blank(value: Any) -> bool:
    """True for None or an all-whitespace string.

    Deliberately NOT falsiness: is_blank(0) and is_blank(0.0) are False, because
    0 is a legitimate score. Never "simplify" this to `not value` -- that would
    reject a valid zero. This property is pinned by test_msgs.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def label(item: Any, key: str, index: int) -> str:
    """Name a thing by its id/code (`"D2"`), or `#1` when that id is what's wrong.

    Uses the raw source text, so a coerced id still reads as the author wrote it.
    """
    if isinstance(item, dict) and key in item:
        return f'"{raw_text(item, key, item[key])}"'
    return f"#{index + 1}"


def missing_fields(kind: str, label: str, missing: list[str]) -> str:
    if len(missing) == 1:
        return f"al {kind} {label} le falta el campo: {missing[0]}"
    return f"al {kind} {label} le faltan campos: {', '.join(missing)}"


def blank_fields(kind: str, label: str, blanks: list[str]) -> str:
    if len(blanks) == 1:
        return f'el campo "{blanks[0]}" del {kind} {label} está vacío'
    campos = ", ".join(f'"{b}"' for b in blanks)
    return f"los campos {campos} del {kind} {label} están vacíos"
