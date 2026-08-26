"""Read YAML while remembering where each value came from.

Every module that loads an author-written YAML file (criteria today, probes next,
ledger/runner later) needs the same two things to build a useful error:

* the 1-based line a mapping started on, so the message can point at it;
* the *raw source text* of a value, because by the time we see the parsed data
  YAML has already turned `id: 1.10` into the float 1.1 -- echoing the parsed
  value back would tell the author to write the wrong thing.

This module owns that plumbing so no caller clones it. The public surface is:

    data = load_yaml_with_lines(path, error_cls=CriteriaError)  # parse + wrap
    line_of(node)                    # 1-based line, or 1 if unknown
    raw_text(node, key, parsed)      # source text as authored
    require_quoted(node, key, ...)   # reject-don't-coerce for join keys

`load_yaml_with_lines` raises the caller's own `error_cls` (an EvalkitError
subclass) on malformed YAML, so `except CriteriaError` / `except ProbeError`
both keep working and the CLI's `except EvalkitError` catches either.

Implementation note: position data is stashed under reserved keys inside each
parsed mapping. Callers that build their result objects field by field (as
criteria does) never carry the markers into those objects; a caller that
iterates *all* of a mapping's keys should skip `MARKER_KEYS`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import EvalkitError

# Reserved keys holding position metadata on every parsed mapping.
LINE_KEY = "__line__"
RAW_KEY = "__raw__"
MARKER_KEYS = frozenset({LINE_KEY, RAW_KEY})


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that records each mapping's start line and raw scalar text."""

    def construct_mapping(self, node, deep=False):  # noqa: D102 - see module docstring
        mapping = super().construct_mapping(node, deep=deep)
        mapping[LINE_KEY] = node.start_mark.line + 1
        mapping[RAW_KEY] = {
            k.value: v.value
            for k, v in node.value
            if isinstance(k, yaml.ScalarNode) and isinstance(v, yaml.ScalarNode)
        }
        return mapping


def load_yaml_with_lines(
    path: Path | str,
    *,
    error_cls: type[EvalkitError] = EvalkitError,
) -> Any:
    """Parse `path`, annotating every mapping with its source position.

    Malformed YAML is re-raised as `error_cls` carrying the file, the line the
    parser choked on, and the underlying detail, with the original YAMLError
    chained as `__cause__`.
    """
    path = Path(path)
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_LineLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        detail = getattr(e, "problem", None) or str(e)
        raise error_cls(
            f"no se pudo interpretar el YAML: {detail}",
            path=path,
            line=mark.line + 1 if mark else 1,
        ) from e


def line_of(node: Any) -> int:
    """The 1-based line where `node` (a parsed mapping) began, or 1 if unknown."""
    return node.get(LINE_KEY, 1) if isinstance(node, dict) else 1


def raw_text(node: Any, key: str, parsed: Any) -> str:
    """The source text of `key` as the author typed it, not as YAML parsed it."""
    if isinstance(node, dict):
        text = node.get(RAW_KEY, {}).get(key)
        if text is not None:
            return text
    return str(parsed)


def require_quoted(
    node: dict,
    key: str,
    *,
    noun: str,
    path: Path,
    error_cls: type[EvalkitError],
) -> str:
    """Return `node[key]`, insisting the author quoted it.

    `id` and `code` are join keys (criteria -> probes -> evidence -> scores), so
    coercing whatever YAML handed us is not an option: str() on the float YAML
    made of `1.10` yields "1.1", losing the trailing zero irreversibly and
    silently. Reject instead, and use the error to teach the quoting rule.

    `noun` names the thing in Spanish, e.g. "el id" or "el código de dominio".
    """
    value = node[key]
    if isinstance(value, str):
        return value
    src = raw_text(node, key, value)
    raise error_cls(
        f"{noun} {src} debe ir entre comillas "
        f'- escribí {key}: "{src}", no {key}: {src}',
        path=path,
        line=line_of(node),
        hint=f"(sin comillas, YAML lo lee como {value!r} y deja de coincidir)",
    )
