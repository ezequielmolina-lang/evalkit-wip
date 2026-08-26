"""Rebuild verdicts for one job from evidence that is already on disk.

    cd 06_Application
    python -m lxp_eval.rescue_job runs/jobs/d19ddd2bfdfc4a90 --dry-run
    python -m lxp_eval.rescue_job runs/jobs/d19ddd2bfdfc4a90 --only PT-D9-M2

WHY THIS EXISTS. Job d19ddd2bfdfc4a90 was approved with a plan carrying one
probe of eight. The engine ran that one probe, scored all nine criteria against
the evidence of THAT run (`rows_since`, correctly), and reported eight of nine
as NO_EVALUADO. Meanwhile 223 captured, hashed, ledgered rows -- 21 hours of
probing -- sat on disk, verifiable and unread.

WHAT IT IS NOT. This is not a run and must never be mistaken for one. No human
approved these verdicts, no probe was executed, nothing was countersigned. So:

  * it writes OUTSIDE the job directory, and never edits job.json. That report
    is the audit record of what actually happened -- that we ran one probe of
    eight -- and overwriting it would destroy the evidence of the very failure
    that made this necessary.
  * it appends NO ledger row. Governance rows record human decisions; there is
    no decision here to put a signature on.
  * every artifact it emits says RECONSTRUCCIÓN in the first line, and carries
    the per-criterion note of what evidence was excluded and why.

A criterion with no usable evidence is reported as not reconstructed, WITH the
reason. It is never reported as a criterion the platform failed: PT-D9-H1 has
32 ledger rows and not one usable text, and rendering that as an unmet
criterion would blame a vendor for our own truncated capture.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .evalkit.rescue import select_archived_evidence
from .evalkit.scoring import NO_EVALUADO, score_test

BANNER = "RECONSTRUCCIÓN DESDE EVIDENCIA ARCHIVADA — NO ES UNA CORRIDA APROBADA"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_job(job_dir: Path) -> dict:
    return json.loads((Path(job_dir) / "job.json").read_text(encoding="utf-8"))


def load_ledger(job_dir: Path) -> list[dict]:
    path = Path(job_dir) / "evidence" / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _steps_divergence(record: dict) -> str:
    from .service import serialize

    return serialize.steps_divergence_line(
        record.get("probes") or [], record.get("executed_steps") or {}
    )


def _tests_of(record: dict):
    """The job's observable criteria as CriterionTest objects.

    Reuses the service's own reader so the observable-only invariant is the
    same one the pipeline enforces, rather than a second copy of the rule.
    """
    from .service import serialize

    plan = record.get("approved_plan") or {}
    criteria_dicts = plan.get("criteria") or serialize.criteria_out(record.get("criteria") or [])
    criteria = serialize.criteria_from_plan(criteria_dicts, record.get("objective", ""))
    return list(criteria.all_tests())


def reconstruct(
    job_dir: Path | str,
    *,
    client,
    only: tuple[str, ...] = (),
    model: str | None = None,
    root: Path | str = ".",
) -> dict:
    """Score every criterion that still has usable archived evidence."""
    job_dir = Path(job_dir)
    record = load_job(job_dir)
    rows = load_ledger(job_dir)
    platform = record.get("platform", "")

    entries: list[dict] = []
    for test in _tests_of(record):
        if only and test.id not in only:
            continue
        sel = select_archived_evidence(
            rows, test_id=test.id, platform=platform, root=root
        )
        entry = {"testId": test.id, "name": test.name, "selection": sel.to_dict()}
        if not sel.usable:
            entry["verdict"] = None
            entry["reason"] = (
                "No quedó evidencia utilizable en el ledger. " + sel.note
            )
            entries.append(entry)
            continue

        verdict = score_test(
            test, sel.rows, client, platform=platform, repeats=1, model=model
        )
        entry.update({
            "verdict": verdict.status,
            "pointsEarned": verdict.points_earned,
            "pointsPossible": verdict.points_possible,
            "rationale": verdict.rationale,
            "citedArtifacts": list(verdict.evidence),
            # One run. `unanimity_of` would call this NOT MEASURED and it is:
            # a reconstruction is not the place to claim stability.
            "runs": list(verdict.runs),
        })
        entries.append(entry)

    return {
        "kind": "reconstruction",
        "banner": BANNER,
        "jobId": record.get("job_id", ""),
        "platform": platform,
        "objective": record.get("objective", ""),
        "reconstructedAt": _now(),
        "sourceRun": {
            "approvedAt": record.get("approved_at", ""),
            "stage": record.get("stage", ""),
            "probesInApprovedPlan": len((record.get("approved_plan") or {}).get("probes") or []),
            "probesOnRecord": len(record.get("probes") or []),
            # Empty for a job whose executed steps were never recorded. The
            # reconstruction scores CRITERIA against evidence, so a rewritten
            # probe does not invalidate a verdict -- but a reader deciding how
            # much to trust the plan needs to know the plan is not what ran.
            "stepsDivergence": _steps_divergence(record),
        },
        "entries": entries,
    }


def render_markdown(result: dict) -> str:
    src = result["sourceRun"]
    out = [
        f"# {result['banner']}",
        "",
        f"Trabajo `{result['jobId']}` · plataforma **{result['platform']}** · "
        f"reconstruido el {result['reconstructedAt']}",
        "",
        "Estos veredictos se calcularon sobre evidencia que ya estaba en el ledger: "
        "capturas hasheadas y verificables, pero **anteriores a cualquier aprobación**. "
        "Nadie aprobó esta reconstrucción, no se ejecutó ninguna prueba para hacerla y "
        "no se escribió ninguna fila de gobernanza. No reemplaza al informe del trabajo "
        "y no debe presentarse como una evaluación de la plataforma.",
        "",
        f"La corrida original aprobó **{src['probesInApprovedPlan']} de "
        f"{src['probesOnRecord']} pruebas** del plan (aprobada el {src['approvedAt'] or '—'}), "
        "y por eso su informe dejó casi todo sin evaluar.",
        "",
    ]
    if src.get("stepsDivergence"):
        out += [f"**Pasos:** {src['stepsDivergence']}", ""]
    out += [
        "| Criterio | Veredicto | Puntos | Evidencia usada |",
        "|---|---|---|---|",
    ]
    for e in result["entries"]:
        v = e["verdict"] or "no reconstruido"
        pts = (f"{e.get('pointsEarned', 0):.1f} / {e.get('pointsPossible', 0):.1f}"
               if e["verdict"] else "—")
        out.append(f"| `{e['testId']}` {e['name']} | {v} | {pts} | "
                   f"{e['selection']['kept']} filas |")

    out += ["", "## Detalle por criterio", ""]
    for e in result["entries"]:
        out += [f"### `{e['testId']}` — {e['name']}", ""]
        if not e["verdict"]:
            out += [f"**No reconstruido.** {e['reason']}", ""]
            continue
        out += [
            f"**{e['verdict']}** · {e['pointsEarned']:.1f} de {e['pointsPossible']:.1f} puntos",
            "",
            e["rationale"] or "(sin fundamento)",
            "",
            f"*Selección de evidencia:* {e['selection']['note']}",
            "",
        ]
        if e["citedArtifacts"]:
            out.append("*Artefactos citados por el juez:*")
            out += [f"- `{a}`" for a in e["citedArtifacts"]]
            out.append("")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job_dir", help="p. ej. runs/jobs/d19ddd2bfdfc4a90")
    ap.add_argument("--only", action="append", default=[],
                    help="reconstruir sólo este criterio (repetible)")
    ap.add_argument("--out", default="runs/reconstructions",
                    help="dónde escribir; NUNCA dentro del trabajo")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostrar qué evidencia se usaría, sin llamar al juez")
    # Re-wording the artifact must not cost what the verdicts cost. The JSON
    # holds every judged result, so a rendering change re-renders; it does not
    # re-pay ~USD 3.69 of judge calls to change a sentence.
    ap.add_argument("--rerender", metavar="JSON",
                    help="volver a escribir el .md desde un .json ya juzgado, sin gastar")
    args = ap.parse_args(argv)

    if args.rerender:
        result = json.loads(Path(args.rerender).read_text(encoding="utf-8"))
        result["sourceRun"]["stepsDivergence"] = _steps_divergence(load_job(Path(args.job_dir)))
        Path(args.rerender).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        md = Path(args.rerender).with_suffix(".md")
        md.write_text(render_markdown(result), encoding="utf-8")
        print(f"re-renderizado sin gastar -> {md}")
        return 0

    job_dir = Path(args.job_dir)
    record = load_job(job_dir)
    rows = load_ledger(job_dir)
    platform = record.get("platform", "")

    if args.dry_run:
        for test in _tests_of(record):
            if args.only and test.id not in args.only:
                continue
            sel = select_archived_evidence(rows, test_id=test.id, platform=platform)
            print(f"{test.id:12} {'USABLE ' if sel.usable else 'VACIO  '} {sel.note}")
        return 0

    from .config import CONFIG
    import anthropic

    client = anthropic.Anthropic(api_key=CONFIG.resolve_api_key())
    result = reconstruct(job_dir, client=client, only=tuple(args.only))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result['jobId']}_{result['reconstructedAt'].replace(':', '').replace('-', '')}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = out_dir / f"{stem}.md"
    md.write_text(render_markdown(result), encoding="utf-8")

    done = sum(1 for e in result["entries"] if e["verdict"] and e["verdict"] != NO_EVALUADO)
    print(f"reconstruidos {done} de {len(result['entries'])} criterios -> {md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
