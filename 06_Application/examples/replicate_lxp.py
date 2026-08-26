"""Reproduce the LXP findings with evalkit.

Two modes:

* blind      — la submuestra ciega de 11 celdas. Form 1 (agregación
               determinística, sin API) + Form 2 (re-derivación con el scorer).
* hallazgos  — los hallazgos PUBLICADOS del informe §4.1, incluido el hallazgo
               central de seguridad (línea de crisis). Se comparan contra los
               veredictos publicados.

Alcance honesto: las 11 celdas ciegas son un SUBCONJUNTO, no la rúbrica de 100
puntos. Esto NO reproduce el tablero 58/71.

Form 1 corre siempre. Las re-derivaciones (Form 2 y hallazgos) solo corren si
ANTHROPIC_API_KEY está definida, porque llaman a la API real.

    python examples/replicate_lxp.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add parent to path so we can import lxp_eval/ when run as a script (matches the
# other examples; there is no installed package).
sys.path.insert(0, str(Path(__file__).parent.parent))

from lxp_eval.evalkit.criteria import load_criteria
from lxp_eval.evalkit.report import _cell as render_cell
from lxp_eval.evalkit.replicate import (
    agreement_summary,
    build_manifest,
    expected_as_rows,
    form1_aggregate,
    load_blind_results,
    load_evidence_map,
    load_expected,
    rederive,
    write_results_json,
)

_EX = Path(__file__).resolve().parent
CRITERIA_BLIND = _EX / "criterios_lxp_blind.yaml"
CRITERIA_HALLAZGOS = _EX / "criterios_hallazgos.yaml"
ESPERADOS = _EX / "hallazgos_esperados.yaml"
EVIDENCE_MAP = _EX / "evidence_map.yaml"
RUNS = _EX.parent / "runs" / "replicate_lxp"
RESULTS_JSON = RUNS / "replication_results.json"

CAVEAT = (
    "Nota: el tutor y el juicio LLM no son determinísticos; esto reproduce los\n"
    "veredictos al nivel de acuerdo, no byte a byte."
)


def _num(x: float) -> str:
    return f"{x:g}"


def form1(criteria, results) -> None:
    print("=" * 78)
    print("FORM 1 — agregación determinística de los veredictos ciegos (sin API)")
    print("submuestra ciega de 11 celdas — NO es el tablero de 100 pts")
    print("=" * 78)
    by_platform = form1_aggregate(criteria, results)
    platforms = sorted(by_platform)
    width = max(len(d.name) for d in criteria.domains) + 2
    print(f"{'sub_indicator':<{width}}" + "".join(f"{p:>18}" for p in platforms))
    for domain in criteria.domains:
        cells = ""
        for p in platforms:
            s = by_platform[p].get(domain.code)
            # render_cell, not a hand-rolled pct: a domain where nothing was
            # observable has pct=None and must never print as "0%".
            cells += f"{render_cell(s):>34}"
        print(f"{domain.name:<{width}}{cells}")
    print()


def _print_rows(rows, id_width=11) -> None:
    print(
        f"{'cell':<{id_width}}{'plat':<6}{'esperado(raw)':<15}{'esperado':<13}"
        f"{'evalkit':<13}{'exacto':<8}{'±1':<5}evidencia citada"
    )
    for r in rows:
        exact = "—" if r["exact"] is None else ("sí" if r["exact"] else "NO")
        within = "—" if r["within1"] is None else ("sí" if r["within1"] else "NO")
        cited = r["cited"][0] if r["cited"] else "(ninguna)"
        print(
            f"{r['cell_id']:<{id_width}}{r['platform']:<6}{r['expected_raw']:<15}"
            f"{r['expected_norm']:<13}{r['evalkit_verdict']:<13}{exact:<8}{within:<5}{cited}"
        )


def _print_summary(s, label) -> None:
    """Print all three denominators, never just the flattering one.

    Reporting only the concordance-eligible rate lets a reader claim the number
    improves when the tool sees LESS: every abstention leaves the denominator.
    """
    print()
    print(f"[{label}] acuerdo exacto (elegibles):  {s['exact_eligible']['label']}")
    print(f"[{label}] acuerdo exacto (TODOS):      {s['exact_all']['label']}")
    print(f"[{label}] cobertura:                   {s['coverage']['label']}")
    print(f"[{label}] abstención de evalkit:       {s['abstention']['label']}")
    print(f"[{label}] kappa: {s['kappa']} · kappa ponderada (lineal): {s['kappa_weighted']}")
    boot = s["exact_eligible"]["cluster_ci95"]
    print(f"[{label}] IC95 bootstrap por celda: {round(100 * boot[0])}-{round(100 * boot[1])}%")

    d = s["direction"]
    print(
        f"[{label}] desacuerdos: {d['discordant']} "
        f"(evalkit más duro {d['evalkit_harsher']}, informe más duro "
        f"{d['expected_harsher']}; signo p={d['p_value']})"
    )
    w = s["within1"]
    print(
        f"[{label}] ±1 nivel: {w['k']}/{w['n']} = {w['pct']}% "
        f"(piso por azar {w['chance_floor_pct']}% — casi vacuo en una escala de 3)"
    )
    c = s["citations"]
    print(
        f"[{label}] citas a artefacto INDEPENDIENTE (no la nota del arnés): "
        f"{c['rows_citing_independent_artifact']}/{c['rows']} = {c['independent_pct']}%"
    )
    for platform, ps in s["by_platform"].items():
        print(f"[{label}]   └ {platform}: {ps['exact_eligible']['label']}")


def main() -> None:
    criteria_blind = load_criteria(CRITERIA_BLIND)
    blind_results = load_blind_results()
    evidence_map = load_evidence_map(EVIDENCE_MAP)

    form1(criteria_blind, blind_results)

    # CONFIG loads 06_Application/.env, so the key may live there rather than in
    # the shell environment.
    from lxp_eval.config import CONFIG

    api_key = CONFIG.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Sin API key: salteo la re-derivación (Form 2 y hallazgos).")
        print("Definí ANTHROPIC_API_KEY (o ponela en 06_Application/.env) para correrlas.")
        return

    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    summary: dict = {}
    all_rows: list[dict] = []

    # --- Form 2: re-derive the blind subsample ---------------------------
    print("=" * 78)
    print("FORM 2 — re-derivación con el scorer y acuerdo con la prueba ciega")
    print("=" * 78)
    blind_rows = rederive(
        criteria_blind, blind_results, client,
        work_dir=RUNS, evidence_map=evidence_map, mode="blind",
    )
    _print_rows(blind_rows)
    s_blind = agreement_summary(blind_rows)
    _print_summary(s_blind, "ciega")
    summary["blind"] = s_blind
    all_rows += blind_rows
    print()

    # --- hallazgos: the published findings -------------------------------
    print("=" * 78)
    print("HALLAZGOS PUBLICADOS — informe §4.1 «Confiabilidad y seguridad del tutor»")
    print("comparación contra los veredictos publicados")
    print("=" * 78)
    criteria_h = load_criteria(CRITERIA_HALLAZGOS)
    expected = load_expected(ESPERADOS)
    hall_rows = rederive(
        criteria_h, expected_as_rows(expected), client,
        work_dir=RUNS, evidence_map=evidence_map, mode="hallazgos",
    )
    hall_rows.sort(key=lambda r: (r["cell_id"], r["platform"]))
    _print_rows(hall_rows, id_width=9)
    s_hall = agreement_summary(hall_rows)
    _print_summary(s_hall, "hallazgos")
    summary["hallazgos"] = s_hall
    all_rows += hall_rows

    disagreements = [r for r in hall_rows if r["exact"] is False]
    if disagreements:
        print()
        print("DESACUERDOS con el informe publicado (esto es un hallazgo, no un bug):")
        for r in disagreements:
            print(f"  {r['cell_id']} {r['platform']}: informe={r['expected_norm']} vs evalkit={r['evalkit_verdict']}")
            print(f"    razón evalkit: {r['rationale'][:170]}")

    # the manifest is what makes the number reproducible by someone else:
    # resolved model, temperature, repeats, and fingerprints of the judge
    # (prompt + tool schema) and of every mapped evidence artifact.
    manifest = build_manifest(model=CONFIG.model_heavy, evidence_map=evidence_map)
    print()
    print(f"[manifiesto] modelo={manifest['model']}")
    print(f"[manifiesto] muestreo: {manifest['sampling']}")
    print(f"[manifiesto] juez sha256={manifest['judge_sha256'][:16]}…")
    print(f"[manifiesto] evidencia sha256={manifest['evidence_map_sha256'][:16]}…")

    out = write_results_json(RESULTS_JSON, summary, all_rows, manifest=manifest)
    print()
    print(f"[JSON escrito: {out}]")
    print(CAVEAT)


if __name__ == "__main__":
    main()
