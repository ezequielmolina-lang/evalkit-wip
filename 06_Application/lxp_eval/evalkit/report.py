"""Markdown rendering of a scored evaluation.

The scoring lives in evalkit.scoring; this module only turns verdicts + scores
into a human-readable, per-platform report. PLATAFORMA_B and Plataforma A are never blended: a
summary table gives one column per platform, and each platform gets its own
detail section. Every scored verdict cites its evidence; NO_EVALUADO tests are
listed apart so the reader sees what was not observed.
"""
from __future__ import annotations

from collections import defaultdict

from .criteria import Criteria
from .scoring import NO_EVALUADO, DomainScore, TestVerdict, aggregate_by_platform


#: Below this share of a domain's cells, a percentage is refused. A "100%"
#: computed off 1 of 3 cells is a coverage artifact, not a domain score.
MIN_COVERAGE_FOR_PCT = 0.7


def _num(value: float) -> str:
    return f"{value:g}"


def _cell(score: DomainScore | None) -> str:
    """One domain cell, ALWAYS carrying its coverage.

    Three shapes, and the distinction between the first two is the whole point:

    * nothing scored   -> ``no evaluado (0 de 3 celdas)``  — never a percentage
    * thin coverage    -> ``1/1 · COBERTURA INSUFICIENTE (1 de 3 evaluados)``
    * normal           -> ``1/3 · 33.3% (3 de 3 evaluados)``

    A domain nobody could observe used to render as ``0/0 · 0%``, identical in
    shape to a domain that was measured and failed. Side by side in the real
    run, PLATAFORMA_B's unreachable tutor-safety domain therefore read as WORSE than
    Plataforma A's measured ``1/3 · 33.3%`` — the vendor that hid the surface scored as
    if it had failed, and looked better than the one that exposed it.
    """
    if score is None:
        return "—"
    if score.pct is None:
        return f"no evaluado ({score.n_scored} de {score.n_total} celdas)"
    cov = f"({score.n_scored} de {score.n_total} evaluados)"
    points = f"{_num(score.earned)}/{_num(score.possible)}"
    if score.coverage < MIN_COVERAGE_FOR_PCT:
        return f"{points} · COBERTURA INSUFICIENTE {cov}"
    return f"{points} · {_num(score.pct)}% {cov}"


def render_markdown(criteria: Criteria, verdicts: list[TestVerdict]) -> str:
    """A human-readable, per-platform report."""
    by_platform = aggregate_by_platform(criteria, verdicts)
    platforms = sorted(by_platform)

    verdicts_by_platform: dict[str, dict[str, TestVerdict]] = defaultdict(dict)
    for v in verdicts:
        verdicts_by_platform[v.platform][v.test_id] = v

    out: list[str] = [f"# Informe de evaluación — {criteria.name}", ""]

    # summary: one column per platform
    if platforms:
        out += ["## Puntajes por dominio", ""]
        out.append("| Dominio | " + " | ".join(platforms) + " |")
        out.append("|" + "---|" * (len(platforms) + 1))
        for domain in criteria.domains:
            cells = " | ".join(_cell(by_platform[p].get(domain.code)) for p in platforms)
            out.append(f"| {domain.code} — {domain.name} | {cells} |")
        out.append("")

    # per-platform detail
    for platform in platforms:
        scores = by_platform[platform]
        vmap = verdicts_by_platform[platform]
        out += [f"## Plataforma: {platform}", ""]
        no_evaluados: list[str] = []

        for domain in criteria.domains:
            score = scores.get(domain.code)
            head = f"### {domain.code} — {domain.name}"
            if score is not None:
                head += f" — {_cell(score)}"
            out += [head, ""]
            for t in domain.tests:
                v = vmap.get(t.id)
                if v is None or v.status == NO_EVALUADO:
                    no_evaluados.append(t.id)
                    continue
                out.append(
                    f"- [{v.status}] {t.id} {t.name} — "
                    f"{_num(v.points_earned)}/{_num(v.points_possible)}"
                )
                if v.rationale:
                    out.append(f"  - {v.rationale}")
                for path in v.evidence:
                    out.append(f"  - evidencia: {path}")
            out.append("")

        if no_evaluados:
            out += ["### No evaluados (sin evidencia suficiente)", ""]
            for tid in no_evaluados:
                out.append(f'- "{tid}"')
            out.append("")

    return "\n".join(out)
