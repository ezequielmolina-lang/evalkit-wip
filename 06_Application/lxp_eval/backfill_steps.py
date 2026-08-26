"""Write the steps a job ACTUALLY ran back onto its record -- beside the approved ones.

    cd 06_Application
    python -m lxp_eval.backfill_steps runs/jobs/d19ddd2bfdfc4a90          # sólo informa
    python -m lxp_eval.backfill_steps runs/jobs/d19ddd2bfdfc4a90 --apply

WHY. On job d19ddd2bfdfc4a90 the probes on the record and the probes that ran
have nothing to do with each other. PB-PT-D8-01 was approved with 17 abstract
steps ("ingresar con una cuenta de estudiante de prueba") and driven with 27
concrete ones ("hacer clic en el curso BM - Matematicas Grado 3"). Every probe
on the job is like that: rewritten by hand against the real UI during a
21-hour session, and never saved back. The ledger is the only place the real
wording survives, in each capture's "paso {n}: {step}" note.

WHY BESIDE, NOT INSTEAD. `approved_plan` is, in the store's own words, "the
reviewed plan exactly as the human approved it". Overwriting its steps with
what actually ran would make the record assert that a person approved wording
they never saw -- the exact lie `serialize.council_still_applies` exists to
stop, committed to the audit trail this time -- and would erase the divergence,
which is the finding. So the executed steps land in their own field and both
versions stay readable side by side.

Everything else is left exactly as it was: the approved plan, the probes, the
ledger, and `updated_at`. The record is edited as JSON rather than round-tripped
through `JobRecord`, because `from_dict` drops any key the dataclass does not
declare and an audit artefact is the last place to silently lose a field.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .evalkit.replay import steps_as_run

BACKUP_SUFFIX = ".bak_pre_backfill"


def _approved_steps(probe: dict) -> list[str]:
    out = []
    for s in probe.get("steps") or []:
        action = s.get("action", "") if isinstance(s, dict) else str(s)
        if action.strip():
            out.append(action.strip())
    return out


def divergence(record: dict, rows: list[dict]) -> list[dict]:
    """Per probe: what was approved, what ran, and whether they differ."""
    out = []
    for probe in record.get("probes") or []:
        pid = probe.get("probeId")
        if not pid:
            continue
        approved = _approved_steps(probe)
        executed = steps_as_run(rows, pid)
        out.append({
            "probeId": pid,
            "criterionId": probe.get("criterionId", ""),
            "approvedSteps": approved,
            "executedSteps": executed,
            # A probe that never ran has nothing to compare and is not a finding.
            "diverged": bool(executed) and executed != approved,
        })
    return out


def backfill(job_dir: Path | str, *, apply: bool = False) -> dict:
    """Report the divergence, and optionally record the executed steps."""
    job_dir = Path(job_dir)
    job_path = job_dir / "job.json"
    record = json.loads(job_path.read_text(encoding="utf-8"))

    ledger_path = job_dir / "evidence" / "ledger.jsonl"
    rows = [
        json.loads(l)
        for l in (ledger_path.read_text(encoding="utf-8").splitlines() if ledger_path.exists() else [])
        if l.strip()
    ]

    probes = divergence(record, rows)
    report = {
        "jobId": record.get("job_id", ""),
        "platform": record.get("platform", ""),
        "applied": False,
        "diverged": sum(1 for p in probes if p["diverged"]),
        "probes": probes,
    }
    if not apply:
        return report

    executed = {p["probeId"]: p["executedSteps"] for p in probes if p["executedSteps"]}
    if record.get("executed_steps") == executed:
        report["applied"] = True
        return report

    backup = job_path.with_name(job_path.name + BACKUP_SUFFIX)
    if not backup.exists():
        backup.write_bytes(job_path.read_bytes())

    record["executed_steps"] = executed
    tmp = job_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(job_path)
    report["applied"] = True
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("job_dir")
    ap.add_argument("--apply", action="store_true",
                    help="escribir executed_steps (por defecto sólo informa)")
    args = ap.parse_args(argv)

    report = backfill(args.job_dir, apply=args.apply)
    for p in report["probes"]:
        mark = "DIVERGE" if p["diverged"] else ("sin grabación" if not p["executedSteps"] else "coincide")
        print(f"{p['probeId']:16} {mark:14} aprobados={len(p['approvedSteps']):>3}  "
              f"ejecutados={len(p['executedSteps']):>3}")
    print(f"\n{report['diverged']} de {len(report['probes'])} probes divergen; "
          f"{'escrito' if report['applied'] else 'no se escribió nada (--apply para escribir)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
