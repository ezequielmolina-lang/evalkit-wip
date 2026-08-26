"""Tests for the shared validation-message helpers.

These are consumed by both criteria and probes, so the wording and -- above all
-- the is_blank semantics are pinned here so neither can drift.
"""
import pytest

from lxp_eval.evalkit.msgs import blank_fields, is_blank, label, missing_fields


# --- is_blank: the load-bearing one ---------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_is_blank_true_for_none_and_whitespace(value):
    assert is_blank(value) is True


def test_is_blank_false_for_zero_the_falsy_but_valid_case():
    # 0 is a legitimate score and it is falsy. is_blank must NOT be `not value`,
    # or a valid zero would be rejected. This property must never regress.
    assert is_blank(0) is False
    assert is_blank(0.0) is False


@pytest.mark.parametrize("value", ["x", " a ", "0", 1, 3.5, False, []])
def test_is_blank_false_for_real_values(value):
    # note False and [] are falsy too, and empty list is a non-None non-str;
    # is_blank only treats None and whitespace-strings as blank.
    assert is_blank(value) is False


# --- label ----------------------------------------------------------------


def test_label_uses_the_id_when_present():
    assert label({"code": "D2"}, "code", 0) == '"D2"'


def test_label_falls_back_to_index_when_missing():
    assert label({"name": "x"}, "code", 0) == "#1"
    assert label({"name": "x"}, "code", 4) == "#5"


def test_label_shows_the_raw_source_of_a_coerced_id():
    # a mapping read through yamlsrc carries the source text; label must echo the
    # author's `1.10`, not the parsed 1.1. Simulate the raw marker yamlsrc adds.
    from lxp_eval.evalkit.yamlsrc import RAW_KEY

    item = {"id": 1.1, RAW_KEY: {"id": "1.10"}}
    assert label(item, "id", 0) == '"1.10"'


# --- missing_fields / blank_fields: singular vs plural, kind, wording ------


def test_missing_fields_singular_and_plural():
    assert missing_fields("dominio", '"D2"', ["name"]) == (
        'al dominio "D2" le falta el campo: name'
    )
    assert missing_fields("probe", '"P1"', ["goal", "steps"]) == (
        'al probe "P1" le faltan campos: goal, steps'
    )


def test_blank_fields_singular_and_plural():
    assert blank_fields("test", '"2.1.2"', ["name"]) == (
        'el campo "name" del test "2.1.2" está vacío'
    )
    assert blank_fields("probe", '"P1"', ["goal", "verdict_rule"]) == (
        'los campos "goal", "verdict_rule" del probe "P1" están vacíos'
    )


def test_message_scaffolding_survives_a_legacy_spanish_console():
    # accented words are fine (cp850 encodes them); the scaffolding must carry no
    # em-dash, which would crash the CLI on a cp850/cp437 console.
    for msg in (
        missing_fields("dominio", '"D2"', ["a", "b"]),
        blank_fields("dominio", '"D2"', ["a", "b"]),
    ):
        msg.encode("cp850")
