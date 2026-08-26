"""Build the site's worked example from a REAL run, anonymised.

    cd 06_Application
    python -m lxp_eval.web_sample runs/jobs/<id>

WHY FROM A REAL RUN. The example is the only thing most visitors will ever
see, so it is the product's main claim about itself. Hand-written fixtures
drift the moment the engine changes shape, and worse, they can promise a report
the engine does not actually produce. Generating them from a finished job makes
the example exactly as good, and exactly as honest, as the tool.

WHAT GETS ANONYMISED. The vendor, its domain, and every human name that
appeared on screen. NOT the findings: the verdicts, the rationales, the tutor's
own words, the step that produced each capture and the shape of the evidence
all stay, because they are the thing being demonstrated. Hashes are shortened
the way the site already showed them -- enough to see that evidence is hashed,
not enough to correlate a file.

The three gated probes stay in the example WITH their gate reasons. A visitor
should see that a crisis probe exists, that it did not run, and why: that is
the safety design working, and hiding it would make the example a nicer product
than the real one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

#: What to hide, in order. Longest first so a substring never wins.
REPLACEMENTS: list[tuple[str, str]] = [
    ("app.plataforma-a.ejemplo", "app.plataforma-a.ejemplo"),
    ("www.plataforma-a.ejemplo", "www.plataforma-a.ejemplo"),
    ("plataforma-a.ejemplo", "plataforma-a.ejemplo"),
    ("Plataforma A", "Plataforma A"),
    ("plataforma_a", "plataforma_a"),
    ("Plataforma A", "Plataforma A"),
    ("plataforma_a", "plataforma-a"),
    ("Estudiante de prueba", "Estudiante de prueba"),
    ("Estudiante de prueba", "Estudiante de prueba"),
    ("Docente de prueba", "Docente de prueba"),
    ("Clase de prueba", "Clase de prueba"),
    ("evaluador@ejemplo.org", "evaluador@ejemplo.org"),
    ("ejemplo.org", "ejemplo.org"),
]

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
#: How much of a hash the example shows. Same as the site already did.
_SHA_KEEP = 10

#: Cap on a step's prose IN THE EXAMPLE ONLY.
#:
#: Not a style choice. With the full step text -- about 7KB across six probes --
#: the plan screen never finishes hydrating and sits on "Cargando…" forever. The
#: same probes, with the same text, render fine at /evaluar/plan?job=<id>, where
#: they arrive over HTTP instead of being baked into a module, so this is a
#: bundling pathology in the fixture and NOT a defect a real run can hit. Capped
#: here so the worked example works; the untruncated wording is in the report and
#: in the job record. Raise it and re-check the page before trusting it.
_STEP_CHARS = 150


def anonymise(value):
    """Recursively hide who this was, keeping what was found."""
    if isinstance(value, str):
        out = value
        for old, new in REPLACEMENTS:
            out = out.replace(old, new)
        return _EMAIL.sub("evaluador@ejemplo.org", out)
    if isinstance(value, dict):
        return {anonymise(k): anonymise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [anonymise(v) for v in value]
    return value


def _clip(text: str) -> str:
    """A step's prose, short enough for the example to render. See _STEP_CHARS."""
    text = text or ""
    return text if len(text) <= _STEP_CHARS else text[:_STEP_CHARS].rstrip() + "…"


def _short_sha(sha: str) -> str:
    return f"{sha[:_SHA_KEEP]}…" if sha else ""


def _ts(value) -> str:
    """A TypeScript literal. json.dumps is valid TS for every shape we emit."""
    return json.dumps(value, ensure_ascii=False, indent=2)


def build_report(record: dict) -> tuple[list, list, dict]:
    report = record.get("report") or {}
    areas = [
        {
            "code": a.get("code", ""),
            "name": a.get("name", ""),
            "earned": a.get("earned", 0),
            "possible": a.get("possible", 0),
            "pct": a.get("pct"),
            "observed": a.get("observed", 0),
            "total": a.get("total", 0),
        }
        for a in report.get("areas") or []
    ]
    rows = []
    for r in report.get("rows") or []:
        rows.append({
            "id": r.get("id", ""),
            "area": r.get("area", ""),
            "name": r.get("name", ""),
            "verdict": r.get("verdict", "NO_EVALUADO"),
            "points": r.get("points", 0),
            "earned": r.get("earned", 0),
            "observability": r.get("observability", "OBSERVABLE_EN_PLATAFORMA"),
            "runs": r.get("runs", 0),
            # null, not false: below two runs unanimity was NOT MEASURED.
            "unanimous": r.get("unanimous"),
            "rationale": r.get("rationale", ""),
            "evidence": [
                {
                    "file": e.get("file", ""),
                    "kind": e.get("kind", ""),
                    "sha256": _short_sha(e.get("sha256", "")),
                    "capturedAt": e.get("capturedAt", ""),
                    "note": e.get("note", ""),
                }
                for e in r.get("evidence") or []
            ],
            "evidenciaRequerida": r.get("evidenciaRequerida", ""),
            "source": _source_out(r.get("source")),
        })
    meta = dict(report.get("meta") or {})
    for key in ("judgeSha", "evidenceSha", "councilSha"):
        if meta.get(key):
            meta[key] = _short_sha(meta[key])
    return areas, rows, meta


def _source_out(source: dict | None) -> dict | None:
    """The wire shape of a criterion's provenance.

    The record keeps `original_id`; `web/app/lib/types.ts` declares
    `originalId`. `serialize.criteria_out` does this on the HTTP path and the
    example reads the record directly, so it has to do the same -- otherwise
    the site ships a fixture its own types reject.
    """
    if not source:
        return None
    return {
        "type": source.get("type", ""),
        "framework": source.get("framework", ""),
        "originalId": source.get("original_id", source.get("originalId", "")),
        "location": source.get("location", ""),
    }


def build_plan(record: dict) -> dict:
    criteria = [
        {
            "id": c.get("id", ""),
            "area": c.get("area", ""),
            "name": c.get("name", ""),
            "passCriteria": c.get("passCriteria", ""),
            "failCriteria": c.get("failCriteria", ""),
            "points": c.get("points", 0),
            "observability": c.get("observability", "OBSERVABLE_EN_PLATAFORMA"),
            "evidenciaRequerida": c.get("evidenciaRequerida", ""),
            "source": _source_out(c.get("source")),
        }
        for c in record.get("criteria") or []
    ]
    probes = []
    for p in record.get("probes") or []:
        probes.append({
            "probeId": p.get("probeId", ""),
            "criterionId": p.get("criterionId", ""),
            "steps": [
                {"n": s.get("n", i + 1), "action": _clip(s.get("action", "")),
                 "capture": s.get("capture", "")}
                for i, s in enumerate(p.get("steps") or [])
            ],
            # KEPT, deliberately. A visitor has to see that a crisis probe
            # exists, that it did not run, and why.
            "requiresSignoff": bool(p.get("requiresSignoff")),
            "signoffReason": p.get("signoffReason", ""),
            "roundsUsed": p.get("roundsUsed", 1),
            "approved": bool(p.get("approved")),
            "judges": [
                {
                    "judge": j.get("judge", ""),
                    "verdict": j.get("verdict", ""),
                    "reasons": list(j.get("reasons") or []),
                    "objectionTarget": j.get("objectionTarget", ""),
                }
                for j in p.get("judges") or []
            ],
        })
    return {
        "criteria": criteria,
        "probes": probes,
        "sampling": record.get("sampling") or {},
    }


HEADER = """// GENERADO — no editar a mano.
//
//     cd 06_Application && python -m lxp_eval.web_sample runs/jobs/{job}
//
// Es una corrida REAL de evalkit, anonimizada: el proveedor y los nombres de
// personas están reemplazados, y los hashes acortados. Los veredictos, los
// fundamentos, las palabras del tutor y los pasos que produjeron cada captura
// son los que salieron del motor. Si el ejemplo se ve pobre, el producto está
// pobre: esa es la idea.
"""


def render_report_ts(record: dict) -> str:
    areas, rows, meta = build_report(record)
    return (
        HEADER.format(job=record.get("job_id", ""))
        + '\nimport type { Observability, Provenance } from "./types";\n\n'
        'export type Verdict = "CUMPLE" | "PARCIAL" | "NO_CUMPLE" | "NO_EVALUADO";\n\n'
        "export interface Evidence {\n"
        "  file: string;\n"
        '  kind: "captura" | "transcripción" | "texto de página";\n'
        "  sha256: string;\n"
        "  capturedAt: string;\n"
        "  note?: string;\n"
        "}\n\n"
        "export interface ResultRow {\n"
        "  id: string;\n  area: string;\n  name: string;\n  verdict: Verdict;\n"
        "  points: number;\n  earned: number;\n  observability: Observability;\n"
        "  runs?: number;\n  unanimous?: boolean | null;\n  rationale?: string;\n"
        "  evidence: Evidence[];\n  evidenciaRequerida?: string;\n"
        "  source: Provenance;\n}\n\n"
        "export interface AreaResult {\n"
        "  code: string;\n  name: string;\n  earned: number;\n  possible: number;\n"
        "  pct: number | null;\n  observed: number;\n  total: number;\n}\n\n"
        f"export const AREAS_RESULT: AreaResult[] = {_ts(areas)};\n\n"
        f"export const ROWS: ResultRow[] = {_ts(rows)};\n\n"
        f"export const RUN_META = {_ts(meta)};\n"
    )


def render_plan_ts(record: dict) -> str:
    plan = build_plan(record)
    return (
        HEADER.format(job=record.get("job_id", ""))
        + '\nimport type { EvalPlan } from "./types";\n\n'
        f"export const SAMPLE_PLAN: EvalPlan = {_ts(plan)};\n"
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job_dir")
    ap.add_argument("--web", default="../web/app/lib",
                    help="dónde viven los fixtures del sitio")
    # OFF por defecto, y esto es un defecto conocido, no una preferencia.
    # Con el plan generado la pantalla /evaluar/plan (sin ?job) no termina de
    # hidratar nunca: se queda en "Cargando…". Los MISMOS probes, con el mismo
    # texto, renderizan bien en /evaluar/plan?job=<id>, donde llegan por HTTP en
    # vez de venir empaquetados en un módulo -- así que ninguna corrida real lo
    # toca. Acotado a: aparece con los pasos completos y también con los pasos
    # recortados más los votos del consejo; no es el largo del texto solo, no es
    # `capture`, no son claves duplicadas, no hay error en consola ni en el
    # overlay de Next, y el bundle compila y sirve 200. Sin causa raíz todavía,
    # así que el sitio sigue con el plan de ejemplo escrito a mano.
    ap.add_argument("--plan", action="store_true",
                    help="además, regenerar samplePlan.ts (ROTO: ver el comentario)")
    args = ap.parse_args(argv)

    record = anonymise(
        json.loads((Path(args.job_dir) / "job.json").read_text(encoding="utf-8"))
    )
    if not record.get("report"):
        print("ese trabajo no tiene informe: el ejemplo saldría vacío", file=sys.stderr)
        return 1

    out = Path(args.web)
    (out / "sampleReport.ts").write_text(render_report_ts(record), encoding="utf-8")

    # EL PLAN VA POR HTTP, no empaquetado. Como módulo, /evaluar/plan sin ?job
    # nunca terminaba de hidratar; los mismos datos servidos por HTTP -- que es
    # como llega un trabajo real -- renderizan bien. En vez de pelear con el
    # bundler, el ejemplo usa el camino que ya funciona.
    publico = out.parents[1] / "public" / "ejemplo"
    publico.mkdir(parents=True, exist_ok=True)
    (publico / "plan.json").write_text(
        json.dumps(build_plan(record), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = record["report"].get("rows") or []
    juzgados = [r for r in rows if r.get("runs")]
    gated = [p for p in record.get("probes") or [] if p.get("requiresSignoff")]
    print(f"ejemplo desde {record.get('job_id')}")
    print(f"  criterios      : {len(record.get('criteria') or [])}")
    print(f"  pruebas        : {len(record.get('probes') or [])} "
          f"({len(gated)} esperando firma humana)")
    print(f"  con veredicto  : {len(juzgados)} de {len(rows)}")
    print(f"  informe        : {out}/sampleReport.ts")
    print(f"  plan (por HTTP): {publico}/plan.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
