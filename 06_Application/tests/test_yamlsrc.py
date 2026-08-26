"""Tests for the shared YAML-with-source reader.

Two callers rely on this (criteria now, probes next), so the reader is tested on
its own: line numbers for nested structures, raw source text for values YAML
would coerce, and consistent wrapping of malformed input.
"""
from pathlib import Path

import pytest

from lxp_eval.evalkit.criteria import CriteriaError
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.yamlsrc import (
    LINE_KEY,
    MARKER_KEYS,
    line_of,
    load_yaml_with_lines,
    raw_text,
    require_quoted,
)

NESTED = """name: raíz
items:
  - id: a
    child:
      grandchild: hola
  - id: b
"""


def _write(tmp_path: Path, text: str, name: str = "f.yaml") -> Path:
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


# --- line numbers ----------------------------------------------------------


def test_line_of_top_level_mapping(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, NESTED))
    assert line_of(data) == 1


def test_line_of_nested_list_items(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, NESTED))
    first, second = data["items"]
    assert line_of(first) == 3
    assert line_of(second) == 6


def test_line_of_deeply_nested_mapping(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, NESTED))
    assert line_of(data["items"][0]["child"]) == 5


def test_line_of_non_mapping_is_one():
    # a bare scalar or list carries no position; callers get a safe default
    assert line_of("just a string") == 1
    assert line_of(["a", "list"]) == 1


# --- raw source text -------------------------------------------------------


def test_raw_text_preserves_a_value_yaml_would_coerce(tmp_path: Path):
    # `id: 1.10` parses to the float 1.1; the trailing zero survives only in the
    # source text. This is the whole reason the reader exists.
    data = load_yaml_with_lines(_write(tmp_path, "id: 1.10\n"))
    assert data["id"] == 1.1  # YAML already coerced it
    assert raw_text(data, "id", data["id"]) == "1.10"  # source is intact


def test_raw_text_preserves_leading_zero_and_boolean_like(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, "code: 07\nflag: no\n"))
    assert raw_text(data, "code", data["code"]) == "07"
    assert raw_text(data, "flag", data["flag"]) == "no"


def test_raw_text_falls_back_to_str_when_unknown(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, "id: a\n"))
    assert raw_text(data, "missing", 42) == "42"
    assert raw_text("not a mapping", "id", 42) == "42"


# --- the markers are an implementation detail ------------------------------


def test_markers_are_present_in_the_raw_mapping_but_named(tmp_path: Path):
    # callers that iterate ALL keys must be able to skip them; callers that read
    # named fields (criteria, probes) never see them.
    data = load_yaml_with_lines(_write(tmp_path, "id: a\n"))
    assert LINE_KEY in data
    real = {k: v for k, v in data.items() if k not in MARKER_KEYS}
    assert real == {"id": "a"}


# --- malformed input -------------------------------------------------------


def test_malformed_yaml_wraps_into_evalkit_error(tmp_path: Path):
    f = _write(tmp_path, "items: [unclosed\n")
    with pytest.raises(EvalkitError) as e:
        load_yaml_with_lines(f)
    msg = str(e.value)
    assert msg.startswith("f.yaml, línea ")
    assert "no se pudo interpretar el YAML" in msg
    assert e.value.__cause__ is not None


def test_malformed_yaml_uses_the_callers_error_class(tmp_path: Path):
    # the point of error_cls: `except CriteriaError` keeps working, and probes
    # will get ProbeError, while the CLI's `except EvalkitError` catches either.
    f = _write(tmp_path, "items: [unclosed\n")
    with pytest.raises(CriteriaError):
        load_yaml_with_lines(f, error_cls=CriteriaError)


# --- the shared quote rule -------------------------------------------------


def test_require_quoted_returns_a_quoted_string(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, 'id: "2.1.2"\n'))
    got = require_quoted(data, "id", noun="el id", path=Path("f.yaml"), error_cls=EvalkitError)
    assert got == "2.1.2"


def test_require_quoted_rejects_coerced_value_with_the_callers_error(tmp_path: Path):
    data = load_yaml_with_lines(_write(tmp_path, "id: 1.10\n"))
    with pytest.raises(CriteriaError) as e:
        require_quoted(data, "id", noun="el id", path=Path("f.yaml"), error_cls=CriteriaError)
    msg = str(e.value)
    # quotes the id as WRITTEN (1.10), never the coerced 1.1
    assert 'escribí id: "1.10", no id: 1.10' in msg
    assert "1.1" in e.value.hint
