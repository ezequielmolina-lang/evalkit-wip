"""Translation between the engine's dataclasses and the web's JSON.

This module is a serialisation job and nothing else. It must not decide
anything: no scoring, no gating, no filtering that the engine did not already
do. Where it looks like it decides something -- `criteria_from_plan` keeping
only observable criteria -- it is repeating an invariant the engine already
enforces (`FrameworkResult._by_area`), so that an EDITED plan coming back from
a browser cannot smuggle a REQUIERE_ESTUDIO criterion into the probe set.

Names are camelCase on the wire because that is what `web/app/lib/types.ts`
declares, and snake_case in Python. The mapping is explicit in both directions
rather than an automatic case-converter: an automatic one turns
`evidencia_requerida` into `evidenciaRequerida` and `repeat_judging` into
`repeatJudging` correctly, and then silently mangles the next Spanish field
somebody adds.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..evalkit.criteria import Criteria, CriterionDomain, CriterionTest
from ..evalkit.framework import (
    NO_EVALUADO,
    OBSERVABLE_EN_PLATAFORMA,
    FrameworkResult,
)
from ..evalkit.ledger import GOVERNANCE_KINDS
from ..evalkit.probes import DEFAULT_CAPTURE, Probe
from ..evalkit.scoring import DomainScore, TestVerdict

#: The note written on an approval ledger row, and rendered verbatim (only its
#: first letter capitalised) into the report's provenance line. Two values, so
#: the audit trail says WHO approved -- a person, or the unattended policy.
#:
#: These are not decoration. `provenance_of` reads the approval line from the
#: ledger (the tamper-evident source of truth), and before this it rendered
#: "Plan aprobado por el evaluador humano" for EVERY approval row. An
#: auto-approved batch run would then have claimed a human approval that never
#: happened -- exactly the class of false claim the ledger exists to prevent.
#: The human value is byte-for-byte the old sentence's stem, so the delivered
#: report for a human-approved job is unchanged.
APPROVAL_NOTE_HUMAN = "plan aprobado por el evaluador humano"
APPROVAL_NOTE_AUTO = (
    'plan aprobado automáticamente por la política de aprobación "auto": las '
    "pruebas con compuerta quedan sin firmar y no se ejecutan (NO_EVALUADO)"
)

#: The sampling plan the review screen gets when nobody has edited one yet.
#: `repeats` is 1 rather than 3 on purpose: re-probing a live platform three
#: times is a real cost decision, and the tool must not spend it by default.
DEFAULT_SAMPLING = {
    "subjects": [],
    "grades": [],
    "lessonsPerGrade": 1,
    "repeats": 1,
    "repeatJudging": 1,
    "rationale": "",
}


# --- criteria ---------------------------------------------------------------


def criterion_to_dict(c) -> dict:
    return {
        "id": c.id,
        "area": c.area,
        "name": c.name,
        "passCriteria": c.pass_criteria,
        "failCriteria": c.fail_criteria,
        "points": c.points,
        "verification": c.verification,
        "observability": c.observability,
        "evidenciaRequerida": c.evidencia_requerida,
        "observabilityReason": c.observability_reason,
        "rationale": c.rationale,
        # "" for an observable criterion; NO_EVALUADO for the rest, so the
        # review screen can grey them out without re-deriving the rule.
        "status": "" if c.is_observable else NO_EVALUADO,
        "source": c.source.to_dict() if c.source else None,
    }


def _source_out(source: dict | None) -> dict | None:
    if not source:
        return None
    return {
        "type": source.get("type", ""),
        "framework": source.get("framework", ""),
        "originalId": source.get("original_id", source.get("originalId", "")),
        "location": source.get("location", ""),
    }


def criteria_out(criteria: list[dict]) -> list[dict]:
    """Job-record criteria -> the wire shape (`source` in camelCase)."""
    return [{**c, "source": _source_out(c.get("source"))} for c in criteria]


def with_frozen_readings(plan: dict, record: Any) -> dict:
    """El plan aprobado, con las lecturas congeladas si el trabajo las tiene.

    Va en la misma instantánea que las pruebas y los criterios, que es la que
    se guarda y se hashea en el ledger al aprobar: lo que una persona aprobó
    incluye la lectura con la que se va a juzgar. Las huellas se calculan con
    el texto de los criterios DEL PLAN, que es el que después recibe el juez.
    Un trabajo sin lecturas (contrato apagado, o anterior a él) sale igual que
    entró, sin la clave: se puntúa con la lectura generada en el momento.
    """
    readings = getattr(record, "readings", None) or {}
    if not readings:
        return plan
    from ..evalkit import lectura as lectura_mod

    criteria = criteria_from_plan(
        plan.get("criteria") or criteria_out(record.criteria), record.objective
    )
    plan["lecturas"] = lectura_mod.freeze(readings, criteria.all_tests())
    return plan


def criteria_from_plan(plan_criteria: list[dict], name: str) -> Criteria:
    """The approved criteria as a `Criteria`, OBSERVABLE ONLY.

    Filtering here is not a second policy: it is the same rule
    `FrameworkResult.criteria()` applies, restated at the point where an edited
    plan re-enters the pipeline. Without it, a reviewer who flipped a
    criterion's observability in the browser would get a probe generated for
    something no walkthrough can settle.
    """
    order: dict[str, list[CriterionTest]] = {}
    for c in plan_criteria:
        if c.get("observability", OBSERVABLE_EN_PLATAFORMA) != OBSERVABLE_EN_PLATAFORMA:
            continue
        order.setdefault(c.get("area", ""), []).append(CriterionTest(
            id=c["id"],
            name=c.get("name", ""),
            points=float(c.get("points", 0) or 0),
            verification=c.get("verification", "") or c.get("name", ""),
            pass_criteria=c.get("passCriteria", "") or c.get("pass_criteria", ""),
            fail_criteria=c.get("failCriteria", "") or c.get("fail_criteria", ""),
        ))
    domains = [
        CriterionDomain(
            code=code,
            name=code,
            points=sum(t.points for t in tests),
            tests=tests,
        )
        for code, tests in order.items()
    ]
    return Criteria(name=name, domains=domains)


# --- probes -----------------------------------------------------------------


def probe_to_dict(generated) -> dict:
    """A `GeneratedProbe` -> the wire shape, with every judge's verdict.

    `requiresSignoff` and `signoffReason` come from the generator's provenance,
    never from anything a client sent. `signedOff` starts False and is only ever
    set by POST /jobs/{id}/signoff.
    """
    p = generated.probe
    prov = generated.provenance
    last = prov.rounds[-1] if prov.rounds else None
    return {
        "probeId": p.probe_id,
        "criterionId": p.test_id,
        "goal": p.goal,
        "verdictRule": p.verdict_rule,
        "steps": [
            {"n": i, "action": s, "capture": ", ".join(p.capture)}
            for i, s in enumerate(p.steps, start=1)
        ],
        "capture": list(p.capture),
        "repeats": p.repeats,
        "repeatJudging": p.repeat_judging,
        "approved": prov.approved,
        # WHICH PANEL. `fullCouncil` lists the seats, but reword a judge's
        # remit or add a seat and two plans look identical while having been
        # reviewed by different councils. A reviewer signs off on a review, so
        # the review has to be identifiable afterwards.
        "councilSha": prov.council_fingerprint,
        "drafterSha": prov.drafter_fingerprint,
        "requiresSignoff": prov.requires_human_signoff,
        # `requiresSignoff` pide la contrafirma del aviso al proveedor; `holdsRun`
        # dice, además, que la prueba NO corre sin esa firma. Crisis: requiresSignoff
        # True, holdsRun False. Y holdsRun sólo es True contra un entorno real: en un
        # demo o con cuentas de prueba, hasta datos/escritura corren (política 2026-11-26).
        "holdsRun": prov.hold_run_for_signoff,
        "signoffReason": " ".join(prov.gate_reasons),
        "riskMarkers": list(prov.risk_markers),
        "signedOff": False,
        "signedOffAt": "",
        "roundsUsed": prov.rounds_used,
        # THE PANEL AS IT ACTUALLY SAT. `judges` below has one row per SEAT,
        # and a seat that never returned a verdict still produces a row -- so
        # without this, a council that ran 5-of-7 renders as a 7-judge council
        # with two rejections. `quorum.voted` / `quorum.expected` is the honest
        # headline and `quorumNote` is the Spanish sentence for a reader who
        # should not have to do the arithmetic.
        "quorum": {
            "expected": prov.quorum["expected"],
            "voted": prov.quorum["voted"],
            "votingExpected": prov.quorum["voting_expected"],
            "votingVoted": prov.quorum["voting_voted"],
            "complete": prov.quorum["complete"],
            "silent": list(prov.quorum["silent"]),
        },
        "quorumNote": prov.quorum["note"],
        "fullCouncil": prov.full_council,
        "judges": [
            {
                "judge": v.judge,
                "responsibility": v.responsibility,
                "family": v.family,
                "verdict": v.verdict,
                "reasons": list(v.reasons),
                "suggestedFix": v.suggested_fix,
                # False = this seat returned nothing usable. Its `verdict` is
                # REJECT because we fail closed, NOT because it objected.
                "answered": v.answered,
                "attempts": v.attempts,
                # "criterio" = an instrument finding: the judge located the
                # defect in the CRITERION, so it never went back to the
                # drafter and never blocked approval. Carried here so the
                # plan screen can show the finding instead of losing it.
                "objectionTarget": v.objection_target,
            }
            for v in (last.verdicts if last else ())
        ],
    }


def quorum_line(probe_dicts: list[dict]) -> str:
    """The council-quorum sentence the delivered report header carries.

    Same fact as each probe's `quorumNote`, aggregated for the reader who only
    ever sees the report. It has to live in the report and not just the review
    screen: the review screen is transient, and the report is the artefact that
    gets forwarded, quoted, and disputed a year later.
    """
    short = [p for p in probe_dicts if not p.get("quorum", {}).get("complete", True)]
    reduced = sum(1 for p in probe_dicts if p.get("consejo") == "reducido")
    if not short and reduced:
        # No es el consejo completo y el informe no puede decir que lo fue.
        wider = sum(1 for p in probe_dicts if p.get("consejo") == "ampliado")
        return (
            f"{reduced} prueba(s) fueron revisadas por el consejo reducido "
            f"(coherencia con el criterio, realismo y riesgo) y {wider} por el "
            "consejo completo, que se convoca cuando hay una objeción o un riesgo."
        )
    if not short:
        return "Todas las pruebas fueron juzgadas por el consejo completo."
    detalle = "; ".join(
        f"{p.get('probeId', '?')} ({p['quorum']['voted']} de "
        f"{p['quorum']['expected']}, faltó "
        f"{', '.join(p['quorum'].get('silent') or []) or 'sin identificar'})"
        for p in short
    )
    return (
        f"CONSEJO INCOMPLETO en {len(short)} de {len(probe_dicts)} pruebas: "
        f"{detalle}. Esas pruebas no fueron juzgadas por el consejo entero y no "
        "deben presentarse como tales."
    )


def steps_divergence_line(probe_dicts: list[dict], executed_steps: dict | None) -> str:
    """Did the probes run with the wording that was approved?

    Sibling of `quorum_line`, and there for the same reason: a fact that only
    existed on the record gets read by nobody, and this one decides whether the
    plan a ministry is handed describes what actually happened.

    On job d19ddd2bfdfc4a90 five of eight probes were rewritten by hand against
    the real UI during a 21-hour session and never saved back. PB-PT-D8-01 was
    approved with 17 abstract steps and driven with 27 concrete ones. The
    report said nothing, so the delivered plan described steps that never ran.

    Returns "" when there is nothing recorded to compare. Absence of
    `executed_steps` means WE DID NOT RECORD IT -- most jobs predate the field
    -- and printing the reassuring sentence there would be an assertion nobody
    checked, which is the failure this whole line exists to prevent.
    """
    if not executed_steps:
        return ""
    rewritten = []
    for p in probe_dicts:
        pid = p.get("probeId")
        ran = executed_steps.get(pid)
        # A probe that never ran has nothing to diverge from: the three gated
        # probes on that job have no recording because the signoff gate held.
        if not ran:
            continue
        approved = [
            (s.get("action", "") if isinstance(s, dict) else str(s)).strip()
            for s in (p.get("steps") or [])
        ]
        approved = [s for s in approved if s]
        if approved != ran:
            rewritten.append((pid, len(approved), len(ran)))

    if not rewritten:
        return "Las pruebas se ejecutaron con los pasos aprobados."
    detalle = "; ".join(
        f"{pid} ({n_ap} aprobados, {n_ex} ejecutados)" for pid, n_ap, n_ex in rewritten
    )
    return (
        f"PASOS REESCRITOS en {len(rewritten)} de {len(probe_dicts)} pruebas: "
        f"{detalle}. Esas pruebas no corrieron con la redacción que se aprobó, "
        "así que el plan aprobado no describe lo que se ejecutó."
    )


def unanimity_of(runs) -> bool | None:
    """Did repeated judgements of the same frozen bytes agree?

    None means NOT MEASURED, and it is the honest answer below two runs. This
    used to be `len(set(runs)) <= 1`, which returns True for a single run: one
    judgement presented as agreement between judgements. Every criterion in a
    repeats:1 evaluation then printed «unánimes» on the report screen, claiming
    a stability check that never happened -- the same shape of error as a
    quorum satisfied by one voter.
    """
    runs = list(runs or [])
    if len(runs) < 2:
        return None
    return len(set(runs)) == 1


def _text_of(probe: dict) -> tuple:
    """The wording a council verdict is ABOUT: goal, rule, and the steps."""
    steps = probe.get("steps") or []
    actions = tuple(
        (s.get("action") or "").strip() if isinstance(s, dict) else str(s).strip()
        for s in steps
    )
    return (
        (probe.get("goal") or "").strip(),
        (probe.get("verdictRule") or "").strip(),
        actions,
    )


def council_still_applies(stored: dict, edited: dict | None) -> bool:
    """Does the council's approval still describe what will actually run?

    `approved` rode on the stored probe and survived any human edit, so a
    reviewer could rewrite every step and the probe still reported itself
    approved by five judges who never read the new wording. This project's own
    evaluation did exactly that: eight probes rewritten by hand against the
    real UI, all eight still claiming council approval.

    The GATE is unaffected -- `requires_signoff` is always re-read from the
    server copy, so an edit can never unlock a risky probe. This is about not
    claiming a review that did not happen.
    """
    if not edited:
        return True
    return _text_of(stored) == _text_of({**stored, **edited})


def probe_from_plan(stored: dict, edited: dict | None, sampling: dict) -> Probe:
    """Rebuild a `probes.Probe` from the reviewed plan.

    `stored` is the server's authoritative copy; `edited` is what came back
    from the browser. Fields the reviewer actually changed win; everything the
    payload omits falls back to `stored`, so a client that round-trips a
    trimmed object does not silently blank a probe's verdict rule.

    `repeats` / `repeat_judging` come from the SAMPLING PLAN, which is where the
    product puts them. They already exist on `probes.Probe` and are consumed by
    `runner.run_probe` and `scoring.score_test` respectively -- nothing is
    re-implemented here.
    """
    edited = edited or {}
    steps_raw = edited.get("steps") or stored.get("steps") or []
    steps = [
        s.get("action", "") if isinstance(s, dict) else str(s)
        for s in steps_raw
    ]
    steps = [s for s in steps if s.strip()]
    capture = edited.get("capture") or stored.get("capture") or list(DEFAULT_CAPTURE)
    return Probe(
        probe_id=stored["probeId"],
        test_id=stored["criterionId"],
        goal=edited.get("goal") or stored.get("goal") or "",
        steps=steps,
        verdict_rule=edited.get("verdictRule") or stored.get("verdictRule") or "",
        capture=list(capture),
        repeats=max(1, int(sampling.get("repeats", 1) or 1)),
        repeat_judging=max(1, int(sampling.get("repeatJudging", 1) or 1)),
    )


def sampling_from_plan(plan: dict) -> dict:
    """The sampling plan, defaults filled, counts coerced to sane integers."""
    raw = dict(DEFAULT_SAMPLING)
    raw.update({k: v for k, v in (plan.get("sampling") or {}).items() if v is not None})
    for key in ("lessonsPerGrade", "repeats", "repeatJudging"):
        try:
            raw[key] = max(1, int(raw.get(key, 1) or 1))
        except (TypeError, ValueError):
            raw[key] = 1
    raw["subjects"] = list(raw.get("subjects") or [])
    raw["grades"] = list(raw.get("grades") or [])
    raw["rationale"] = str(raw.get("rationale") or "")
    return raw


def sampling_line(sampling: dict) -> str:
    """The one-line scope statement the report header carries."""
    parts = []
    if sampling.get("subjects"):
        parts.append(" y ".join(sampling["subjects"]))
    if sampling.get("grades"):
        parts.append(" y ".join(sampling["grades"]) + " grado")
    parts.append(f"{sampling.get('lessonsPerGrade', 1)} lecciones por grado")
    parts.append(f"{sampling.get('repeats', 1)} sondeos por criterio")
    parts.append(f"{sampling.get('repeatJudging', 1)} juicios por evidencia")
    return " · ".join(parts)


# --- the framework stage ----------------------------------------------------


def framework_to_record(result: FrameworkResult) -> dict:
    """The pieces of a `FrameworkResult` the job record keeps."""
    return {
        "criteria": [criterion_to_dict(c) for c in result.criteria_list],
        "coverage": result.coverage.to_dict(),
    }


# --- the report -------------------------------------------------------------


def _evidence_rows_by_artifact(rows: list[dict]) -> dict[str, dict]:
    return {r["artifact"]: r for r in rows}


_KIND_LABEL = {"screenshot": "captura", "page_text": "texto de página"}

#: How much of a sha256 the human line carries. Twelve hex characters is
#: enough to find the row unambiguously in the ledger and short enough to read
#: to an auditor; the structured entry keeps the full hash for re-hashing.
_SHA_PREFIX = 12


def provenance_of(ledger_rows: list[dict]) -> list[dict]:
    """The report's provenance section: who authorized this run, row by row.

    Source of truth is the LEDGER, never `record.signoffs` or `approved_at`:
    the record is mutable, the ledger is append-only with content hashes, and
    the section exists so the auditor reads the stronger of the two. Rows come
    out in ledger order, ALL of them -- a revised-and-re-approved plan lists
    both approvals, because the history of the decision is the audit record,
    not just its latest state.

    Each entry renders a human `line` in Spanish with ASCII scaffolding (the
    cp850 console rule): timestamp, the first 12 hex chars of the artifact's
    sha256, and the artifact filename an auditor can fetch and re-hash.

    Approval entries carry `"__plan__"` as both probeId and testId. That is
    the ledger row's own sentinel -- the decision is about the whole plan, not
    any one probe -- and the structured entry mirrors the row verbatim rather
    than blanking it, so what the report JSON says is what the ledger says.

    A ledger with no governance rows renders as an empty list. The service
    cannot serve a report before the approval that authorized its run (409
    until done), but a pure function does not get to crash on the order of
    events -- it simply has nothing to list.
    """
    entries = []
    for row in ledger_rows:
        kind = row.get("kind")
        if kind not in GOVERNANCE_KINDS:
            continue
        sha = row.get("sha256", "")
        name = Path(row.get("artifact", "")).name
        stamp = row.get("timestamp", "")
        detail = f"(sha256 {sha[:_SHA_PREFIX]}, archivo {name})"
        if kind == "approval":
            # From the ROW's own note, not a hardcoded sentence: a machine
            # approval (the `auto` batch policy) and a human one write different
            # notes, and the provenance line has to say which actually happened.
            # The human note IS the old sentence, so its output is unchanged; an
            # empty note (a row written before the notes existed) falls back to
            # the human sentence rather than rendering a blank line.
            note = (row.get("note") or "").strip()
            sentence = (note[:1].upper() + note[1:]) if note else (
                "Plan aprobado por el evaluador humano"
            )
            line = f"{sentence} el {stamp} {detail}"
        elif kind == "signoff":
            line = (
                f'Firma humana para el probe "{row.get("probe_id", "")}" '
                f"el {stamp} {detail}"
            )
        else:
            # A governance kind without its own report line is a programming
            # error: borrowing another kind's sentence would misdescribe the
            # decision on the audit record. Internal, so English is fine.
            raise ValueError(
                f"governance kind {kind!r} has no report line; give it one "
                "here before adding it to GOVERNANCE_KINDS"
            )
        entries.append({
            "kind": kind,
            "probeId": row.get("probe_id", ""),
            "testId": row.get("test_id", ""),
            "timestamp": stamp,
            "sha256": sha,
            "file": name,
            "line": line,
        })
    return entries


def build_report(
    *,
    criteria_dicts: list[dict],
    verdicts: list[TestVerdict],
    scores: dict[str, DomainScore],
    ledger_rows: list[dict],
    area_names: dict[str, str],
    markdown: str,
    meta: dict,
) -> dict:
    """The diagnostic, in the shape the report screen renders.

    The one rule that matters: a criterion nobody could observe is NOT a zero.
    Its area carries `pct: null` and its row carries NO_EVALUADO plus the
    evidence a ministry would have to request. Rendering it as 0% is how the
    vendor who hid a surface ends up scoring better than the one who exposed it.
    """
    by_test = {v.test_id: v for v in verdicts}
    by_artifact = _evidence_rows_by_artifact(ledger_rows)

    rows = []
    for c in criteria_dicts:
        v = by_test.get(c["id"])
        observable = c.get("observability") == OBSERVABLE_EN_PLATAFORMA
        evidence = []
        for artifact in (v.evidence if v else []):
            row = by_artifact.get(artifact, {})
            evidence.append({
                "file": Path(artifact).name,
                "artifact": artifact,
                "kind": _KIND_LABEL.get(row.get("kind", ""), row.get("kind", "")),
                "sha256": row.get("sha256", ""),
                "capturedAt": row.get("timestamp", ""),
                "note": row.get("note", ""),
                "url": row.get("url", ""),
            })
        runs = list(v.runs) if v else []
        rows.append({
            "id": c["id"],
            "area": f"{c.get('area', '')} · {area_names.get(c.get('area', ''), '')}".strip(" ·"),
            "areaCode": c.get("area", ""),
            "name": c.get("name", ""),
            "verdict": v.status if v else NO_EVALUADO,
            "points": c.get("points", 0),
            "earned": v.points_earned if v else 0.0,
            "observability": c.get("observability", ""),
            "runs": len(runs),
            "unanimous": unanimity_of(runs),
            "rationale": (
                v.rationale if v
                else c.get("observabilityReason", "") or c.get("observability_reason", "")
            ),
            "evidence": evidence,
            "evidenciaRequerida": c.get("evidenciaRequerida", ""),
            "source": _source_out(c.get("source")),
        })
        if observable and v is None:
            rows[-1]["rationale"] = (
                "No se ejecutó ningún probe para este criterio, así que no hay "
                "evidencia sobre la que juzgarlo."
            )

    areas = []
    seen: list[str] = []
    for c in criteria_dicts:
        code = c.get("area", "")
        if code in seen:
            continue
        seen.append(code)
        score = scores.get(code)
        if score is not None:
            areas.append({
                "code": code,
                "name": area_names.get(code, code),
                "earned": score.earned,
                "possible": score.possible,
                "pct": score.pct,
                "observed": score.n_scored,
                "total": score.n_total,
            })
        else:
            # No scored cells at all in this area: it is UNMEASURED, and an
            # unmeasured area renders as null, never as 0%.
            total = sum(1 for x in criteria_dicts if x.get("area") == code)
            areas.append({
                "code": code,
                "name": area_names.get(code, code),
                "earned": 0.0,
                "possible": 0.0,
                "pct": None,
                "observed": 0,
                "total": total,
            })

    return {
        "areas": areas,
        "rows": rows,
        # Purely additive: the informe screen renders areas/rows/meta by name
        # and will simply not show this until it grows a provenance block.
        "provenance": provenance_of(ledger_rows),
        "meta": meta,
        "markdown": markdown,
    }


def sha256_of(path: Path) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()
