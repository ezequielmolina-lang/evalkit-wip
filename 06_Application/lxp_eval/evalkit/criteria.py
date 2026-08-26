"""Load an arbitrary evaluation rubric from YAML.

This is what makes the method reusable: the rubric is data, not code. Swap the
YAML and the whole engine re-aims at different criteria.

The people who write these files are education specialists, not programmers, so
every error raised here is author-facing: it names the file, the line, the
offending id and the bad value. Errors carry those as structured fields and let
the CLI render them -- see evalkit.errors.EvalkitError for the shape.

Messages are in Spanish (voseo) to match the criteria files themselves; code,
docstrings and tracebacks stay in English.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .errors import EvalkitError
from .msgs import blank_fields, is_blank, label, missing_fields
from .yamlsrc import line_of, load_yaml_with_lines, raw_text, require_quoted


class CriteriaError(EvalkitError):
    """Raised when a criteria file is structurally invalid."""


REQUIRED_TEST_FIELDS = ("id", "name", "points", "verification", "pass_criteria", "fail_criteria")
REQUIRED_DOMAIN_FIELDS = ("code", "name", "points", "tests")

# Required fields must also carry a value: `name:` with nothing after it parses
# to None, and would reach a Claude prompt as the literal string "None".
#
# `points: 0` is safe here and needs no exclusion: is_blank() tests for None or
# an all-whitespace string, NOT for falsiness. Do not "simplify" it to `not
# value` -- that is what would swallow a legitimate zero score.
#
# `tests` is absent from the domain list on purpose: an empty `tests` gets its
# own, clearer message ("no tiene tests") a few lines below.
NON_EMPTY_TEST_FIELDS = REQUIRED_TEST_FIELDS
NON_EMPTY_DOMAIN_FIELDS = ("code", "name", "points")

#: Fallback when the file declares no `name`. Ends up as a report title.
SIN_NOMBRE = "sin nombre"


def _num(value: float) -> str:
    """3.0 -> '3', 2.5 -> '2.5'. Authors write whole numbers; don't show floats."""
    return f"{value:g}"


@dataclass(frozen=True)
class CriterionTest:
    id: str
    name: str
    points: float
    verification: str
    pass_criteria: str
    fail_criteria: str


@dataclass(frozen=True)
class CriterionDomain:
    code: str
    name: str
    points: float
    tests: list[CriterionTest] = field(default_factory=list)


@dataclass(frozen=True)
class Criteria:
    name: str
    domains: list[CriterionDomain] = field(default_factory=list)

    def total_points(self) -> float:
        return sum(t.points for d in self.domains for t in d.tests)

    def all_tests(self) -> list[CriterionTest]:
        return [t for d in self.domains for t in d.tests]


def load_criteria(path: Path) -> Criteria:
    path = Path(path)
    raw = load_yaml_with_lines(path, error_cls=CriteriaError)

    if not isinstance(raw, dict) or "domains" not in raw:
        raise CriteriaError(
            "el archivo debe ser un mapa con una clave 'domains'",
            path=path,
            line=1,
        )

    seen_ids: dict[str, int] = {}
    seen_codes: dict[str, int] = {}
    domains: list[CriterionDomain] = []

    for i, d in enumerate(raw["domains"]):
        dline = line_of(d)
        dlabel = label(d, "code", i)

        missing = [k for k in REQUIRED_DOMAIN_FIELDS if k not in d]
        if missing:
            raise CriteriaError(
                missing_fields("dominio", dlabel, missing), path=path, line=dline
            )

        # before require_quoted: a blank `code:` is an empty field, not a
        # quoting mistake, and must be reported as one
        blank = [k for k in NON_EMPTY_DOMAIN_FIELDS if is_blank(d[k])]
        if blank:
            raise CriteriaError(
                blank_fields("dominio", dlabel, blank), path=path, line=dline
            )

        code = require_quoted(
            d, "code", noun="el código de dominio", path=path, error_cls=CriteriaError
        )

        if code in seen_codes:
            raise CriteriaError(
                f"el código de dominio {dlabel} está repetido "
                f"(ya aparece en la línea {seen_codes[code]})",
                path=path,
                line=dline,
            )
        seen_codes[code] = dline

        if not d["tests"]:
            raise CriteriaError(
                f"el dominio {dlabel} no tiene tests "
                "- necesita al menos uno para poder puntuarse",
                path=path,
                line=dline,
            )

        tests: list[CriterionTest] = []
        for j, t in enumerate(d["tests"]):
            tline = line_of(t)
            tlabel = label(t, "id", j)

            missing = [k for k in REQUIRED_TEST_FIELDS if k not in t]
            if missing:
                raise CriteriaError(
                    missing_fields("test", tlabel, missing), path=path, line=tline
                )

            blank = [k for k in NON_EMPTY_TEST_FIELDS if is_blank(t[k])]
            if blank:
                raise CriteriaError(
                    blank_fields("test", tlabel, blank), path=path, line=tline
                )

            tid = require_quoted(
                t, "id", noun="el id", path=path, error_cls=CriteriaError
            )
            if tid in seen_ids:
                raise CriteriaError(
                    f"el id de test {tlabel} está repetido "
                    f"(ya aparece en la línea {seen_ids[tid]})",
                    path=path,
                    line=tline,
                )
            seen_ids[tid] = tline

            try:
                points = float(t["points"])
            except (TypeError, ValueError) as e:
                raise CriteriaError(
                    f'el campo "points" del test {tlabel} debe ser un número, '
                    f"no {raw_text(t, 'points', t['points'])!r}",
                    path=path,
                    line=tline,
                ) from e

            tests.append(CriterionTest(
                id=tid,
                name=t["name"],
                points=points,
                verification=t["verification"],
                pass_criteria=t["pass_criteria"],
                fail_criteria=t["fail_criteria"],
            ))

        try:
            declared = float(d["points"])
        except (TypeError, ValueError) as e:
            raise CriteriaError(
                f'el campo "points" del dominio {dlabel} debe ser un número, '
                f"no {raw_text(d, 'points', d['points'])!r}",
                path=path,
                line=dline,
            ) from e

        actual = sum(t.points for t in tests)
        if abs(declared - actual) > 1e-6:
            raise CriteriaError(
                f"el dominio {dlabel} declara {_num(declared)} puntos "
                f"pero sus tests suman {_num(actual)}",
                path=path,
                line=dline,
            )

        domains.append(CriterionDomain(
            code=code,
            name=d["name"],
            points=declared,
            tests=tests,
        ))

    return Criteria(name=raw.get("name", SIN_NOMBRE), domains=domains)
