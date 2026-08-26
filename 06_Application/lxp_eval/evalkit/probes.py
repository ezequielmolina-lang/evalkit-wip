"""Load probes and gate them against the criteria.

A probe is one concrete, executable check against a platform: open a lesson,
look for a worked example, decide pass/fail. Probes live in their own YAML file
so the same criteria can be re-probed as a platform changes.

Two entry points:

    probes = load_probes(path)                       # parse + validate one file
    validate_against_criteria(probes, criteria)      # the coverage gate

Errors are author-facing and reuse the shared shape (evalkit.errors.EvalkitError
+ evalkit.yamlsrc): Spanish text, ASCII scaffolding, file/line where a line
exists. ids are join keys, so they follow the same reject-don't-coerce rule as
criteria.

The coverage gate is the point of this module: if a criteria test has no probe
it cannot be scored, and silently skipping it is how partial-coverage
extrapolation ("we checked 8 of 13, call it 60%") sneaks into a report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .criteria import Criteria
from .errors import EvalkitError
from .msgs import blank_fields, is_blank, label, missing_fields
from .yamlsrc import line_of, load_yaml_with_lines, raw_text, require_quoted


class ProbeError(EvalkitError):
    """Raised when a probe file is invalid or fails the coverage gate.

    Subclasses EvalkitError (NOT CriteriaError): a wrong parent would be
    invisible, because the CLI's `except EvalkitError` would still catch it.
    """


# Present-and-non-empty in every probe. `capture`, `repeats` and
# `repeat_judging` have defaults.
REQUIRED_PROBE_FIELDS = ("probe_id", "test_id", "goal", "steps", "verdict_rule")
# Of those, the ones that must carry real text (steps is a list, checked apart).
TEXT_PROBE_FIELDS = ("probe_id", "test_id", "goal", "verdict_rule")

#: What the runner captures for a probe when the author does not say.
DEFAULT_CAPTURE = ("screenshot", "page_text")


@dataclass(frozen=True)
class Probe:
    probe_id: str
    test_id: str
    goal: str
    steps: list[str]
    verdict_rule: str
    capture: list[str] = field(default_factory=lambda: list(DEFAULT_CAPTURE))
    #: How many times to RE-PROBE THE PLATFORM. Consumed by runner.run_probe:
    #: the whole probe runs again live, with fresh evidence under its own
    #: run_index. This is what measures the platform's non-determinism.
    repeats: int = 1
    #: How many times to RE-JUDGE the evidence this probe produced. A different
    #: quantity: it measures the SCORER's sampling noise over frozen bytes, and
    #: it is what the scoring side should read instead of matching probe ids
    #: against a hardcoded list of el país rubric cells -- a list that
    #: matches nothing under another country's rubric, silently collapsing its
    #: repeats to 1. The probe author is who knows a test is non-deterministic.
    repeat_judging: int = 1
    #: Does this probe run INSIDE the logged-in app? Almost always yes. A
    #: privacy-policy probe reads the vendor's PUBLIC site, which carries none
    #: of the session markers -- so the login-wall gate would kill it as
    #: "logged out" the instant it navigated off the app, the same false
    #: positive a lesson page hit earlier. False turns the gate off for this
    #: probe only.
    needs_session: bool = True


def _positive_int(p: dict, key: str, plabel: str, path: Path, line: int) -> int:
    """A YAML count field: present or absent, but never a stealth 1."""
    if key not in p:
        return 1
    value = p[key]
    # bool is an int subclass, so `repeats: yes` (-> True) would slip past a
    # plain isinstance(int) and read as 1. Reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProbeError(
            f'el campo "{key}" del probe {plabel} debe ser un entero >= 1, '
            f"no {raw_text(p, key, value)!r}",
            path=path,
            line=line,
        )
    return value


def _capture_of(p: dict, plabel: str, path: Path, line: int) -> list[str]:
    if "capture" not in p:
        return list(DEFAULT_CAPTURE)
    value = p["capture"]
    if not isinstance(value, list) or not value:
        raise ProbeError(
            f'el campo "capture" del probe {plabel} debe ser una lista no vacía '
            "(por ejemplo: [screenshot, page_text])",
            path=path,
            line=line,
        )
    return value


def load_probes(path: Path) -> list[Probe]:
    path = Path(path)
    raw = load_yaml_with_lines(path, error_cls=ProbeError)

    if not isinstance(raw, list):
        raise ProbeError(
            "el archivo de probes debe ser una lista de probes", path=path, line=1
        )

    seen_ids: dict[str, int] = {}
    probes: list[Probe] = []

    for i, p in enumerate(raw):
        pline = line_of(p)
        plabel = label(p, "probe_id", i)

        # gate on isinstance(dict) BEFORE `k not in p`: `in` on a string is a
        # substring test, which would misdiagnose a bare-string entry.
        if not isinstance(p, dict):
            raise ProbeError(
                f"el probe {plabel} debe ser un mapa (clave: valor)",
                path=path,
                line=pline,
            )

        missing = [k for k in REQUIRED_PROBE_FIELDS if k not in p]
        if missing:
            raise ProbeError(
                missing_fields("probe", plabel, missing), path=path, line=pline
            )

        blank = [k for k in TEXT_PROBE_FIELDS if is_blank(p[k])]
        if blank:
            raise ProbeError(
                blank_fields("probe", plabel, blank), path=path, line=pline
            )

        pid = require_quoted(
            p, "probe_id", noun="el id de probe", path=path, error_cls=ProbeError
        )
        if pid in seen_ids:
            raise ProbeError(
                f"el id de probe {plabel} está repetido "
                f"(ya aparece en la línea {seen_ids[pid]})",
                path=path,
                line=pline,
            )
        seen_ids[pid] = pline

        tid = require_quoted(
            p, "test_id", noun="el test_id", path=path, error_cls=ProbeError
        )

        steps = p["steps"]
        if not isinstance(steps, list) or not steps:
            raise ProbeError(
                f"el probe {plabel} no tiene pasos "
                "- necesita al menos uno para poder ejecutarse",
                path=path,
                line=pline,
            )

        probes.append(Probe(
            probe_id=pid,
            test_id=tid,
            goal=p["goal"],
            steps=steps,
            verdict_rule=p["verdict_rule"],
            capture=_capture_of(p, plabel, path, pline),
            repeats=_positive_int(p, "repeats", plabel, path, pline),
            repeat_judging=_positive_int(p, "repeat_judging", plabel, path, pline),
        ))

    return probes


def validate_against_criteria(probes: list[Probe], criteria: Criteria) -> None:
    """The coverage gate. Raises ProbeError; returns None when the set is sound.

    Two failures, both fatal to a trustworthy score:

    * a probe points at a test_id the criteria never defines (typo, or a probe
      left behind after the rubric changed);
    * a criteria test has no probe at all -- it would silently go unscored.
    """
    test_ids = {t.id for t in criteria.all_tests()}

    for p in probes:
        if p.test_id not in test_ids:
            raise ProbeError(
                f'el probe "{p.probe_id}" apunta al test_id "{p.test_id}", '
                "que no existe en los criterios"
            )

    covered = {p.test_id for p in probes}
    uncovered = [t.id for t in criteria.all_tests() if t.id not in covered]
    if uncovered:
        ids = ", ".join(f'"{tid}"' for tid in uncovered)
        raise ProbeError(
            f"estos tests no tienen ningún probe: {ids} "
            "- un test sin probe no se puede puntuar"
        )
