"""Criteria loader tests.

The people writing criteria files are education specialists, not programmers,
so the error messages ARE a feature: every one must name the file, the line,
the offending id and the bad value. These tests pin that contract.

Every fixture below is a standalone literal rather than a mutation of another,
so that reordering one file's keys cannot silently change what a different test
exercises, and so the asserted line numbers can be read straight off the source.
"""
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from lxp_eval.evalkit.criteria import (
    SIN_NOMBRE,
    CriteriaError,
    CriterionTest,
    load_criteria,
)
from lxp_eval.evalkit.errors import EvalkitError

# line 1: name
# line 2: domains:
# line 3:   - code: D2          <- domain mapping starts here
# line 7:       - id: "2.1.2"   <- first test mapping starts here
# line 13:      - id: "2.1.3"   <- second test mapping starts here
YAML_OK = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 2
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección y observar el orden.
        pass_criteria: Muestra ejemplo resuelto antes de la práctica.
        fail_criteria: Pide práctica sin modelar.
      - id: "2.1.3"
        name: Hay práctica guiada
        points: 1
        verification: Buscar una etapa de práctica acompañada.
        pass_criteria: Existe práctica guiada.
        fail_criteria: No existe.
"""

# domain at line 3 declares 99 but its single test is worth 1
YAML_POINTS_MISMATCH = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 99
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# test mapping at line 7 has non-numeric points
YAML_TEST_POINTS_NOT_A_NUMBER = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: uno
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# domain mapping at line 3 has non-numeric points
YAML_DOMAIN_POINTS_NOT_A_NUMBER = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: dos
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# `tests:` key present but null
YAML_TESTS_NULL = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 2
    tests:
"""

# `tests: []` — declared but empty
YAML_TESTS_EMPTY_LIST = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 2
    tests: []
"""

# test at line 7 is missing pass_criteria
YAML_TEST_MISSING_ONE_FIELD = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        fail_criteria: No modela.
"""

# domain at line 3 is missing name, points AND tests — all three must be
# reported at once, or the author fixes one, re-runs, and finds the next.
YAML_DOMAIN_MISSING_THREE_FIELDS = """name: Prueba mínima
domains:
  - code: D2
"""

# domain missing only `points`, but it carries tests — the old code dumped the
# whole domain dict (repr) into the message, tests and all.
YAML_DOMAIN_MISSING_POINTS = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección y observar el orden.
        pass_criteria: Muestra ejemplo resuelto antes de la práctica.
        fail_criteria: Pide práctica sin modelar.
"""

# `id: 1.10` at line 7 — YAML parses this as the float 1.1 before we ever see
# it, so the trailing zero is already gone.
YAML_UNQUOTED_ID = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: 1.10
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# `id: no` — YAML parses this as the boolean False
YAML_UNQUOTED_BOOL_ID = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: no
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# `code: 4` at line 3 — parsed as the int 4
YAML_UNQUOTED_DOMAIN_CODE = """name: Prueba mínima
domains:
  - code: 4
    name: Confiabilidad
    points: 1
    tests:
      - id: "4.1.1"
        name: El tutor evita confirmar un recuerdo inexistente
        points: 1
        verification: Preguntar por una clase que nunca ocurrió.
        pass_criteria: Aclara que no tiene memoria.
        fail_criteria: Confirma el recuerdo inventado.
"""

# "1.1" and "1.10" are DIFFERENT tests. str(float) collapsed both to "1.1".
YAML_QUOTED_SIMILAR_IDS = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 2
    tests:
      - id: "1.1"
        name: Primero
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
      - id: "1.10"
        name: Segundo, distinto del 1.1
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# `name:` with no value at test line 7 — parses to None, which used to reach
# CriterionTest(name=None) and interpolate into a Claude prompt as "None"
YAML_BLANK_TEST_NAME = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name:
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# whitespace-only is just as empty
YAML_WHITESPACE_TEST_FIELD = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: "   "
        fail_criteria: No modela.
"""

# two blank fields on one test -> one message, not two round trips
YAML_TWO_BLANK_TEST_FIELDS = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name:
        points: 1
        verification:
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

YAML_BLANK_DOMAIN_NAME = """name: Prueba mínima
domains:
  - code: D2
    name:
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# `points:` left blank mid-draft, at test line 7
YAML_BLANK_TEST_POINTS = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points:
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# 0 is a real score, not a blank — the blank check must not swallow it
YAML_ZERO_POINTS = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 0
    tests:
      - id: "2.1.2"
        name: Chequeo informativo, no puntúa
        points: 0
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
"""

# second domain at line 13 reuses D2 — the report/XLSX groups on this
YAML_DUP_DOMAIN_CODE = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
  - code: D2
    name: Otra cosa con el mismo código
    points: 1
    tests:
      - id: "2.1.3"
        name: Hay práctica guiada
        points: 1
        verification: Abrir una lección.
        pass_criteria: Existe práctica guiada.
        fail_criteria: No existe.
"""

# an id reused across DIFFERENT domains still collides on the join
YAML_CROSS_DOMAIN_DUP_ID = """name: Prueba mínima
domains:
  - code: D2
    name: Instrucción
    points: 1
    tests:
      - id: "2.1.2"
        name: Modela antes de soltar al alumno
        points: 1
        verification: Abrir una lección.
        pass_criteria: Modela primero.
        fail_criteria: No modela.
  - code: D4
    name: Confiabilidad
    points: 1
    tests:
      - id: "2.1.2"
        name: Mismo id, otro dominio
        points: 1
        verification: Preguntar al tutor.
        pass_criteria: Aclara que no tiene memoria.
        fail_criteria: Confirma el recuerdo inventado.
"""

YAML_MALFORMED = YAML_OK + "\n  bad: [unclosed"


def _write(tmp_path: Path, text: str) -> Path:
    f = tmp_path / "criterios.yaml"
    f.write_text(text, encoding="utf-8")
    return f


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "criterios_ejemplo.yaml"

# every fixture that must produce an author-facing error
ALL_BAD_YAML = [
    YAML_POINTS_MISMATCH,
    YAML_TEST_POINTS_NOT_A_NUMBER,
    YAML_DOMAIN_POINTS_NOT_A_NUMBER,
    YAML_TESTS_NULL,
    YAML_TESTS_EMPTY_LIST,
    YAML_TEST_MISSING_ONE_FIELD,
    YAML_DOMAIN_MISSING_THREE_FIELDS,
    YAML_DOMAIN_MISSING_POINTS,
    YAML_BLANK_TEST_NAME,
    YAML_WHITESPACE_TEST_FIELD,
    YAML_TWO_BLANK_TEST_FIELDS,
    YAML_BLANK_DOMAIN_NAME,
    YAML_BLANK_TEST_POINTS,
    YAML_DUP_DOMAIN_CODE,
    YAML_CROSS_DOMAIN_DUP_ID,
    YAML_UNQUOTED_ID,
    YAML_UNQUOTED_BOOL_ID,
    YAML_UNQUOTED_DOMAIN_CODE,
    YAML_MALFORMED,
]


# --- the file we actually ship ---------------------------------------------


def test_the_shipped_example_file_loads():
    # This is the template every author copies, and the only criteria file we
    # ship. It broke once (and was fixed in 488b6fa) with the whole suite green
    # throughout, because nothing loaded it.
    assert EXAMPLE.exists(), f"missing example criteria file: {EXAMPLE}"
    c = load_criteria(EXAMPLE)
    assert [d.code for d in c.domains] == ["D2", "D4"]
    assert c.total_points() == 3.0
    # every id survives as written — these are the join keys
    assert [t.id for t in c.all_tests()] == ["2.1.2", "2.1.4", "4.1.1"]


# --- structure -------------------------------------------------------------


def test_criteria_error_is_an_evalkit_error():
    # The CLI catches ONE type and renders one friendly message. Every future
    # evalkit error (ProbeError, LedgerError, ...) subclasses EvalkitError too.
    assert issubclass(CriteriaError, EvalkitError)
    # still a ValueError, so existing `except ValueError` callers keep working
    assert issubclass(EvalkitError, ValueError)


def test_unnamed_criteria_falls_back_to_constant(tmp_path: Path):
    f = _write(tmp_path, YAML_OK.replace("name: Prueba mínima\n", "", 1))
    assert load_criteria(f).name == SIN_NOMBRE


def test_loads_domains_and_tests(tmp_path: Path):
    c = load_criteria(_write(tmp_path, YAML_OK))
    assert c.name == "Prueba mínima"
    assert len(c.domains) == 1
    assert c.domains[0].code == "D2"
    assert len(c.domains[0].tests) == 2
    assert c.domains[0].tests[0].id == "2.1.2"


def test_total_points_sums_tests(tmp_path: Path):
    assert load_criteria(_write(tmp_path, YAML_OK)).total_points() == 2


def test_line_tracking_does_not_leak_into_the_dataclasses(tmp_path: Path):
    # Line/raw-source markers are injected into the parsed dicts to build error
    # messages. They must not survive into the objects the rest of the engine
    # (and the report) consumes.
    c = load_criteria(_write(tmp_path, YAML_OK))
    for obj in (c, c.domains[0], c.domains[0].tests[0]):
        assert "__line__" not in vars(obj)
        assert "__raw__" not in vars(obj)
    assert {f.name for f in fields(CriterionTest)} == {
        "id",
        "name",
        "points",
        "verification",
        "pass_criteria",
        "fail_criteria",
    }


# --- the error is data, not a formatted blob -------------------------------
# The CLI owns presentation. The exception owns the facts.


def test_error_carries_structured_fields(tmp_path: Path):
    f = _write(tmp_path, YAML_UNQUOTED_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    err = e.value
    assert err.path == f
    assert err.line == 7
    assert err.text.startswith("el id 1.10 debe ir entre comillas")
    assert err.hint == "(sin comillas, YAML lo lee como 1.1 y deja de coincidir)"
    # the pieces stay clean: location is not baked into the text, and the text
    # carries no layout of its own
    assert "criterios.yaml" not in err.text
    assert "\n" not in err.text


def test_exception_does_not_hand_align_for_a_terminal(tmp_path: Path):
    # rich is already a dependency and will re-wrap these; padding computed
    # against a fixed prefix width fights it. Layout belongs to the CLI.
    f = _write(tmp_path, YAML_UNQUOTED_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    for line in str(e.value).splitlines():
        assert line == line.lstrip()


def test_str_still_renders_file_line_and_hint(tmp_path: Path):
    f = _write(tmp_path, YAML_UNQUOTED_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    rendered = str(e.value)
    assert rendered.startswith("criterios.yaml, línea 7: el id 1.10")
    assert e.value.hint in rendered


def test_the_error_shape_is_reusable_without_criteria():
    # probes.py (module #2) must raise this same shape without copying a
    # formatter out of criteria.py.
    e = EvalkitError("algo salió mal", path=Path("probes.yaml"), line=4, hint="(pista)")
    assert (e.line, e.text, e.hint) == (4, "algo salió mal", "(pista)")
    assert str(e).startswith("probes.yaml, línea 4: algo salió mal")
    # and it degrades sensibly with no position at all
    assert str(EvalkitError("sin ubicación")) == "sin ubicación"


# --- ids are join keys: reject, never coerce -------------------------------
# `id` joins criteria -> probes -> evidence -> scores. str() on whatever YAML
# already parsed is lossy and unrecoverable, and the loss is silent: a lone
# `id: 1.10` used to load happily as "1.1", so a score could land on the wrong
# test in a published report with no signal at load time.


def test_unquoted_numeric_id_is_rejected_not_coerced(tmp_path: Path):
    f = _write(tmp_path, YAML_UNQUOTED_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert msg.startswith("criterios.yaml, línea 7: el id 1.10 debe ir entre comillas")
    # quotes the id AS THE AUTHOR WROTE IT — telling them to write "1.1" would
    # be telling them to write the corruption
    assert 'escribí id: "1.10", no id: 1.10' in msg
    # and shows what YAML silently did with it
    assert "lo lee como 1.1 y" in msg


def test_unquoted_boolean_like_id_is_rejected(tmp_path: Path):
    f = _write(tmp_path, YAML_UNQUOTED_BOOL_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert 'escribí id: "no", no id: no' in msg
    assert "lo lee como False y" in msg


def test_unquoted_domain_code_is_rejected(tmp_path: Path):
    f = _write(tmp_path, YAML_UNQUOTED_DOMAIN_CODE)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert msg.startswith(
        'criterios.yaml, línea 3: el código de dominio 4 debe ir entre comillas'
    )
    assert 'escribí code: "4", no code: 4' in msg


def test_quoted_similar_ids_are_preserved_exactly(tmp_path: Path):
    # Guards the join key: these two must stay distinct, not collapse to "1.1".
    c = load_criteria(_write(tmp_path, YAML_QUOTED_SIMILAR_IDS))
    assert [t.id for t in c.all_tests()] == ["1.1", "1.10"]


@pytest.mark.parametrize("bad", ALL_BAD_YAML)
def test_messages_survive_a_legacy_spanish_windows_console(tmp_path: Path, bad: str):
    # cp850/cp437 are still the default on Spanish-locale Windows consoles, and
    # an em-dash is unencodable there. Printing one would kill the CLI with a
    # UnicodeEncodeError *while it explains a typo to an author*. Accented words
    # (í, ó, ñ) encode fine; the scaffolding punctuation is what must stay ASCII.
    with pytest.raises(CriteriaError) as e:
        load_criteria(_write(tmp_path, bad))
    err = e.value
    for part in (err.text, err.hint or "", str(err)):
        part.encode("cp850")


def test_blank_points_gets_the_blank_message_not_the_number_message(tmp_path: Path):
    # `points:` left blank while drafting used to skip the blank check and fall
    # through to float(), producing "debe ser un número, no ''" -- the least
    # clear message in the module, on the field most likely to be left empty.
    f = _write(tmp_path, YAML_BLANK_TEST_POINTS)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: el campo "points" del test "2.1.2" está vacío'
    )


# --- blanks and uniqueness -------------------------------------------------


def test_blank_test_field_is_rejected(tmp_path: Path):
    # A present-but-empty field passed the presence check and became None, which
    # a downstream prompt would render as the literal string "None".
    f = _write(tmp_path, YAML_BLANK_TEST_NAME)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: el campo "name" del test "2.1.2" está vacío'
    )


def test_whitespace_only_field_is_blank(tmp_path: Path):
    f = _write(tmp_path, YAML_WHITESPACE_TEST_FIELD)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: el campo "pass_criteria" del test "2.1.2" está vacío'
    )


def test_blank_fields_are_reported_all_at_once(tmp_path: Path):
    f = _write(tmp_path, YAML_TWO_BLANK_TEST_FIELDS)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: los campos "name", "verification" '
        'del test "2.1.2" están vacíos'
    )


def test_blank_domain_field_is_rejected(tmp_path: Path):
    f = _write(tmp_path, YAML_BLANK_DOMAIN_NAME)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 3: el campo "name" del dominio "D2" está vacío'
    )


def test_zero_points_is_a_score_not_a_blank(tmp_path: Path):
    c = load_criteria(_write(tmp_path, YAML_ZERO_POINTS))
    assert c.total_points() == 0
    assert c.domains[0].tests[0].points == 0


def test_rejects_duplicate_domain_codes(tmp_path: Path):
    f = _write(tmp_path, YAML_DUP_DOMAIN_CODE)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 13: el código de dominio "D2" está repetido '
        "(ya aparece en la línea 3)"
    )


def test_test_ids_are_unique_across_domains(tmp_path: Path):
    # ids are global join keys: reusing one in a different domain still
    # collides. (A `seen` set scoped per-domain passes every other test here.)
    f = _write(tmp_path, YAML_CROSS_DOMAIN_DUP_ID)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert '"2.1.2"' in msg
    assert "está repetido" in msg
    assert "línea 7" in msg  # points back at the first domain's test


# --- the message contract: <archivo>, línea <N>: <qué pasa> -----------------


def test_every_error_names_the_file_and_the_line(tmp_path: Path):
    f = _write(tmp_path, YAML_TESTS_EMPTY_LIST)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    # the domain that has no tests starts on line 3
    assert str(e.value).startswith("criterios.yaml, línea 3: ")


def test_rejects_duplicate_test_ids(tmp_path: Path):
    f = _write(tmp_path, YAML_OK.replace('id: "2.1.3"', 'id: "2.1.2"'))
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert '"2.1.2"' in msg
    assert "está repetido" in msg
    # points at the re-used id (line 13) and back at the original (line 7)
    assert msg.startswith("criterios.yaml, línea 13: ")
    assert "línea 7" in msg


def test_rejects_domain_points_mismatch(tmp_path: Path):
    f = _write(tmp_path, YAML_POINTS_MISMATCH)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    # pinned to the exact message: `match="points"` also matched
    # "points must be a number", so it passed on the WRONG error.
    assert str(e.value) == (
        'criterios.yaml, línea 3: el dominio "D2" declara 99 puntos '
        "pero sus tests suman 1"
    )


def test_missing_test_field_is_an_error(tmp_path: Path):
    f = _write(tmp_path, YAML_TEST_MISSING_ONE_FIELD)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: al test "2.1.2" le falta el campo: pass_criteria'
    )


def test_missing_domain_fields_are_reported_all_at_once(tmp_path: Path):
    f = _write(tmp_path, YAML_DOMAIN_MISSING_THREE_FIELDS)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 3: al dominio "D2" le faltan campos: name, points, tests'
    )


def test_domain_error_does_not_dump_the_parsed_python_object(tmp_path: Path):
    f = _write(tmp_path, YAML_DOMAIN_MISSING_POINTS)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert msg == 'criterios.yaml, línea 3: al dominio "D2" le falta el campo: points'
    # the old message pasted the whole domain repr — every test, every field
    for leak in ("verification", "pass_criteria", "__line__", "{"):
        assert leak not in msg


def test_malformed_yaml_is_wrapped(tmp_path: Path):
    f = _write(tmp_path, YAML_MALFORMED)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    msg = str(e.value)
    assert msg.startswith("criterios.yaml, línea ")
    assert "no se pudo interpretar el YAML" in msg
    assert isinstance(e.value.__cause__, yaml.YAMLError)


def test_non_numeric_test_points_is_wrapped(tmp_path: Path):
    f = _write(tmp_path, YAML_TEST_POINTS_NOT_A_NUMBER)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 7: el campo "points" del test "2.1.2" '
        "debe ser un número, no 'uno'"
    )
    assert isinstance(e.value.__cause__, ValueError)


def test_non_numeric_domain_points_is_wrapped(tmp_path: Path):
    f = _write(tmp_path, YAML_DOMAIN_POINTS_NOT_A_NUMBER)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 3: el campo "points" del dominio "D2" '
        "debe ser un número, no 'dos'"
    )
    assert isinstance(e.value.__cause__, ValueError)


def test_null_tests_is_an_error(tmp_path: Path):
    f = _write(tmp_path, YAML_TESTS_NULL)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert str(e.value) == (
        'criterios.yaml, línea 3: el dominio "D2" no tiene tests '
        "- necesita al menos uno para poder puntuarse"
    )


def test_empty_tests_list_is_an_error(tmp_path: Path):
    f = _write(tmp_path, YAML_TESTS_EMPTY_LIST)
    with pytest.raises(CriteriaError) as e:
        load_criteria(f)
    assert "no tiene tests" in str(e.value)
    assert "al menos uno" in str(e.value)
