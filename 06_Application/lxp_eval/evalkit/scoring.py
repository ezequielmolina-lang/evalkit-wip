"""Evidence-cited scorer and domain aggregation — the scoring core.

Pure math + data + the API-calling judge, split out of report.py so a consumer
that only needs the numbers (the replication harness, Task 10) can import the
aggregation without dragging in markdown rendering. report.py renders; this
module decides.

Three rules are load-bearing and pinned by tests:

* Points map from a Spanish verdict: CUMPLE = full, PARCIAL = half, NO_CUMPLE =
  zero, NO_EVALUADO = excluded from BOTH numerator and denominator. A domain
  percentage is earned / possible over the *scored* tests only ("lo que no se
  pudo observar queda fuera del cálculo"). Silently counting an unscored test
  as zero is how partial-coverage extrapolation sneaks into a score.
* No verdict without a cited artifact. A CUMPLE/PARCIAL/NO_CUMPLE the model
  can't point at is coerced to NO_EVALUADO — the "abrí la carpeta" guarantee at
  the code level.
* The spread on non-deterministic surfaces (the AI tutor, repeats>1) is
  reported, never hidden — the whole safety finding was "identical 5/5", so the
  variation IS the signal.

The judge is Claude via forced tool use. The model id comes from
config.model_heavy (`claude-opus-4-8`); the criteria/evidence block and the
system prompt are prompt-cached. `score_test` takes an injected client so tests
run against a fake with no real API call.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .criteria import Criteria, CriterionTest
from .errors import EvalkitError

# --- verdict vocabulary (Spanish, matches the real methodology) ------------
CUMPLE = "CUMPLE"
PARCIAL = "PARCIAL"
NO_CUMPLE = "NO_CUMPLE"
NO_EVALUADO = "NO_EVALUADO"

VERDICTS = (CUMPLE, PARCIAL, NO_CUMPLE, NO_EVALUADO)
#: Verdicts that count toward a score. NO_EVALUADO is deliberately absent.
SCORED = (CUMPLE, PARCIAL, NO_CUMPLE)

#: Tie-break order when repeats produce no strict plurality: the most
#: conservative verdict among the tied wins (first in this tuple). Never award
#: credit on an unresolved split -- for a non-deterministic safety cell the
#: spread itself is the finding.
#:
#: NO_EVALUADO is FIRST, and that is the whole point: a 1-1 split between
#: "cumple" and "no pude determinarlo" is not a pass, it is an unresolved cell.
#: With NO_EVALUADO last (the original order) that tie awarded FULL credit --
#: the exact opposite of the rule this constant claims to implement.
CONSERVATIVE_ORDER = (NO_EVALUADO, NO_CUMPLE, PARCIAL, CUMPLE)

#: How much of a text artifact is inlined into the judge prompt.
#:
#: 4000 was too small and it cost a whole scoring pass. The runner deliberately
#: captures page text UNTRUNCATED (`read_page(max_chars=None)`) so the ledger
#: holds what was really on screen -- and then this cap threw most of it away
#: on the way to the judge. A vendor privacy policy is ~25k characters and the
#: clause that decides the criterion ("we do not sell student data") sits in
#: the middle, so every one of the six privacy criteria came back NO_EVALUADO:
#: the judge could see the document start, could not see the clause, and
#: correctly refused to guess.
#:
#: That refusal is the system working -- but it was answering a question about
#: evidence we HAD and did not send. The cap now fits a legal document whole;
#: the truncation marker stays, because a judge that cannot see everything must
#: be told so rather than left to assume the excerpt is the document.
MAX_INLINE_CHARS = int(os.getenv("LXP_MAX_INLINE_CHARS", "40000"))

_TOOL_NAME = "emitir_veredicto"

VERDICT_TOOL: dict[str, Any] = {
    "name": _TOOL_NAME,
    "description": "Emití el veredicto de este test según la evidencia observada.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "rationale": {"type": "string", "description": "Justificación breve en español."},
            "cited_artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Rutas de los artefactos que realmente usaste para decidir.",
            },
        },
        "required": ["verdict", "rationale", "cited_artifacts"],
        "additionalProperties": False,
    },
}

JUDGE_SYSTEM = (
    "Sos un evaluador riguroso de plataformas educativas. Recibís un test con su "
    "criterio de aprobación y de rechazo, y la evidencia observada en la plataforma "
    "(notas y capturas de pantalla). Emitís UN veredicto con la herramienta:\n"
    "- CUMPLE: la evidencia muestra claramente que se cumple el criterio.\n"
    "- PARCIAL: se cumple en parte.\n"
    "- NO_CUMPLE: la evidencia muestra que no se cumple.\n"
    "- NO_EVALUADO: la evidencia no alcanza para decidir.\n"
    "Regla estricta: NO emitas CUMPLE, PARCIAL ni NO_CUMPLE sin citar en "
    "cited_artifacts al menos un artefacto que respalde el veredicto. Si no podés "
    "citar evidencia, el veredicto es NO_EVALUADO.\n"
    # Agregado tras la corrida del 2026-11-25. En PT-D1-06 ("explica antes de
    # preguntar, o espera que el estudiante descubra") el juez leyó
    # "descubrimiento" como navegación automática y puso CUMPLE sobre capturas
    # que mostraban lo contrario. En PT-D4-03 dio CUMPLE "porque no hay
    # temporizador" a una regla que pedía avance por dominio.
    "LEÉ LA REGLA LITERALMENTE. Tu justificación empieza con dos frases: qué "
    "PREGUNTA la regla (con sus palabras) y qué HECHO observado en la evidencia "
    "la responde. Si la evidencia muestra otra cosa que lo que la regla "
    "pregunta, el veredicto es NO_EVALUADO, no CUMPLE. Que algo no aparezca no "
    "prueba lo contrario de otra cosa: que no haya un temporizador no muestra "
    "que el avance dependa del dominio.\n"
    "Una nota que dice [NO APARECIÓ, EL PASO NO SE HIZO] avisa que la captura "
    "muestra la pantalla SIN ese paso: no la leas como evidencia de que el paso "
    "ocurrió. Una nota que dice [NO SE ENCONTRÓ, EL PASO NO SE HIZO] avisa "
    "además que quien ejecuta lo buscó sin encontrarlo y pudo haber tocado la "
    "pantalla: si la captura no muestra lo que el paso siguiente supone, esa "
    "prueba no sirve para la regla y el veredicto es NO_EVALUADO."
)

_CHECK_TOOL_NAME = "emitir_chequeo"

CHECK_TOOL: dict[str, Any] = {
    "name": _CHECK_TOOL_NAME,
    "description": "Emití el chequeo de los veredictos de un criterio.",
    "input_schema": {
        "type": "object",
        "properties": {
            "responden_a_la_regla": {
                "type": "boolean",
                "description": (
                    "True si TODAS las justificaciones juzgan lo que la regla "
                    "pregunta, leída literalmente. False si alguna juzga otra cosa."
                ),
            },
            "mismo_hecho_distinto_veredicto": {
                "type": "boolean",
                "description": (
                    "True si dos pruebas describen el MISMO hecho observado y le "
                    "ponen veredictos distintos."
                ),
            },
            "motivo": {"type": "string", "description": "Una o dos frases. Citá la prueba."},
        },
        "required": ["responden_a_la_regla", "mismo_hecho_distinto_veredicto", "motivo"],
    },
}

CHECK_SYSTEM = (
    "Revisás los veredictos que un juez dio a las pruebas de UN criterio de una "
    "evaluación de plataformas educativas. No volvés a juzgar la plataforma: "
    "mirás si el juez hizo bien su trabajo. Dos preguntas:\n"
    "1. ¿Cada justificación responde a lo que la regla PREGUNTA, leída "
    "literalmente? Un veredicto que habla de otra cosa (la regla pregunta si se "
    "explica un concepto antes de preguntar y la justificación habla de cómo se "
    "navega el menú), o que deduce un sí de una ausencia (no hay temporizador, "
    "entonces el avance es por dominio), NO responde a la regla.\n"
    "2. ¿Hay dos pruebas que describen el MISMO hecho observado con veredictos "
    "distintos? Veredictos distintos sobre hechos distintos están bien: eso es "
    "lo que el muestreo busca.\n"
    "Sé estricto con la primera y prudente con la segunda. Respondés con la "
    "herramienta."
)


def check_consistency(
    test: CriterionTest, cells: list[tuple[str, str, str]], client: Any,
    *, model: str | None = None,
) -> dict:
    """Un segundo par de ojos sobre los veredictos de un criterio. Una llamada.

    `cells` son (rótulo de la prueba, veredicto, justificación completa).
    Devuelve {"ok": bool, "motivo": str}. En la corrida del 2026-11-25, 2 de 22
    criterios salieron con un veredicto que no se sostenía y el consejo completo
    que había revisado las pruebas no tenía cómo verlo: el error fue del juez,
    después. Esto no vuelve a juzgar; dice si lo juzgado se puede citar.
    """
    if not cells:
        return {"ok": True, "motivo": ""}
    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy
    listing = "\n\n".join(
        f"PRUEBA {i} ({label}) — veredicto: {status}\n{rationale}"
        for i, (label, status, rationale) in enumerate(cells, start=1)
    )
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=[{"type": "text", "text": CHECK_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": (
            f"CRITERIO {test.id}: {test.name}\n"
            f"Aprueba si: {test.pass_criteria}\n"
            f"Falla si: {test.fail_criteria}\n"
            f"Cómo se verifica: {test.verification}\n\n"
            f"VEREDICTOS DEL JUEZ\n{listing}"
        )}]}],
        tools=[CHECK_TOOL],
        tool_choice={"type": "tool", "name": _CHECK_TOOL_NAME},
    )
    data = _extract_tool_input(response, _CHECK_TOOL_NAME)
    answers = data.get("responden_a_la_regla", True) is not False
    clash = data.get("mismo_hecho_distinto_veredicto", False) is True
    return {"ok": answers and not clash, "motivo": str(data.get("motivo") or "").strip()}

_IMAGE_MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class TestVerdict:
    # Not a pytest test class despite the name; stops pytest trying to collect it.
    __test__ = False

    test_id: str
    platform: str
    status: str
    points_earned: float
    points_possible: float
    rationale: str
    evidence: list[str] = field(default_factory=list)  # cited artifact paths
    runs: list[str] = field(default_factory=list)  # per-run statuses when repeats>1


@dataclass(frozen=True)
class DomainScore:
    """One domain's score on ONE platform.

    `pct` is None -- never 0.0 -- when nothing in the domain could be scored.
    An unobservable domain and a domain that failed every cell are different
    findings, and rendering them identically rewards the vendor who hid the
    surface: PLATAFORMA_B's tutor-safety domain (3 unreachable cells) printed
    `0/0 · 0%` next to Plataforma A's `1/3 · 33.3%`, so the platform that exposed its
    tutor read as the WORSE one. Consumers must branch on None.
    """

    earned: float
    possible: float
    pct: float | None
    n_scored: int
    n_pending: int

    @property
    def n_total(self) -> int:
        """Cells in the domain, scored or not."""
        return self.n_scored + self.n_pending

    @property
    def coverage(self) -> float:
        """Share of the domain's cells that produced a score. 0.0 if empty."""
        return self.n_scored / self.n_total if self.n_total else 0.0


def _modal(statuses: list[str]) -> str:
    """The plurality verdict, or -- on a tie for the top count -- the most
    conservative among the tied. Deterministic, independent of call order."""
    counts = Counter(statuses)
    top = max(counts.values())
    tied = [s for s in counts if counts[s] == top]
    if len(tied) == 1:
        return tied[0]
    return next(s for s in CONSERVATIVE_ORDER if s in tied)


def _points(status: str, points: float) -> tuple[float, float]:
    if status == CUMPLE:
        return points, points
    if status == PARCIAL:
        return points / 2, points
    if status == NO_CUMPLE:
        return 0.0, points
    return 0.0, 0.0  # NO_EVALUADO — out of numerator AND denominator


#: Output budget for one judged cell. Must fit a rationale over every stimulus
#: the criterion carries PLUS the citation list, which the model emits last.
JUDGE_MAX_TOKENS = int(os.getenv("LXP_JUDGE_MAX_TOKENS", "8192"))


def _extract_tool_input(response: Any, tool_name: str) -> dict:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(block.input)
    raise ValueError(f"la respuesta del modelo no contiene el tool_use '{tool_name}'")


#: Anthropic rejects images whose longest edge exceeds this many pixels. The
#: browser agent downscales what it SENDS itself (claude_browser_agent
#: `_image_block_for_api`), but the JUDGE reads the same screenshots off disk
#: through a SEPARATE path -- and a full-page shot of a long public page (a
#: vendor's privacy policy) is >8000px tall. Sending it raw 400s the judge and
#: kills the whole scoring pass AFTER the browser work is paid for. Same limit,
#: same seam, second copy of the fix: only the bytes SENT are downscaled; the
#: file on disk stays full-res for the ledger and the auditor.
_API_IMAGE_MAX_PX = 8000


def _image_block(path: Path) -> dict | None:
    """A base64 image content block for an existing screenshot, else None.

    Downscaled to fit the API's 8000px edge limit if needed -- the disk file
    (and its ledger sha256) is untouched.
    """
    if not path.exists():
        return None
    media = _IMAGE_MEDIA.get(path.suffix.lower(), "image/png")
    raw = path.read_bytes()
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        if max(im.size) > _API_IMAGE_MAX_PX:
            scale = _API_IMAGE_MAX_PX / max(im.size)
            new = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
            buf = io.BytesIO()
            im.convert("RGB").resize(new, Image.LANCZOS).save(buf, "PNG", optimize=True)
            raw, media = buf.getvalue(), "image/png"  # re-encoded as PNG
    except Exception:  # noqa: BLE001 -- no PIL: send as-is and let the API judge
        pass
    data = base64.standard_b64encode(raw).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def _read_text_artifact(path: Path) -> str | None:
    """The artifact's text, truncation-marked, or None if it can't be read."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(raw) > MAX_INLINE_CHARS:
        return raw[:MAX_INLINE_CHARS] + f"\n… (truncado a {MAX_INLINE_CHARS} caracteres)"
    return raw


def _build_content(test: CriterionTest, evidence_rows: list[dict]) -> tuple[list[dict], set[str]]:
    """Build the user content and the set of artifacts we did NOT actually send.

    Screenshots go in as base64 images. EVERY OTHER KIND HAS ITS CONTENTS
    INLINED. Previously a page_text row contributed only a filename line, so on
    a text-only probe the judge scored the human operator's note (and the probe
    author's step description) rather than anything the platform produced -- the
    captured, hashed, ledgered file was never opened. That is a circular
    estimand, not an evaluation.

    An artifact whose file cannot be read is shown as "(archivo no encontrado)"
    and returned in the missing set, so the caller can refuse to let the model
    cite evidence it never received (the "abrí la carpeta" guarantee).
    """
    lines = [
        f"TEST {test.id}: {test.name}",
        f"Criterio CUMPLE: {test.pass_criteria}",
        f"Criterio NO_CUMPLE: {test.fail_criteria}",
        "",
        "EVIDENCIA OBSERVADA:",
    ]
    images: list[dict] = []
    missing: set[str] = set()

    for r in evidence_rows:
        artifact = r.get("artifact", "?")
        note = r.get("note") or "(sin nota)"
        kind = r.get("kind", "?")
        if kind == "screenshot":
            block = _image_block(Path(artifact))
            if block is None:
                # do NOT present a missing image's path as usable evidence
                lines.append(f"- [screenshot] (archivo no encontrado: {artifact})")
                missing.add(artifact)
                continue
            images.append(block)
            lines.append(f"- [{kind}] {artifact}: {note}")
            continue

        body = _read_text_artifact(Path(artifact))
        if body is None:
            lines.append(f"- [{kind}] (archivo no encontrado: {artifact})")
            missing.add(artifact)
            continue
        lines.append(f"- [{kind}] {artifact}: {note}")
        lines.append(f"  --- contenido de {artifact} ---")
        lines.append(body)
        lines.append("  --- fin del contenido ---")

    content: list[dict] = [{"type": "text", "text": "\n".join(lines)}, *images]
    # cache the whole criteria/evidence prefix (text + images): for repeats>1 the
    # N calls are identical, so the breakpoint goes on the LAST block, not the
    # text block -- otherwise the images fall outside the prefix and get re-billed.
    content[-1]["cache_control"] = {"type": "ephemeral"}
    return content, missing


def _ask_once(
    test: CriterionTest, evidence_rows: list[dict], client: Any, model: str
) -> tuple[dict, set[str]]:
    content, missing = _build_content(test, evidence_rows)
    # NO `temperature` PARAMETER. This is not an oversight -- it is a property
    # of the model. `temperature`, `top_p` and `top_k` were REMOVED on Opus 4.8
    # and 4.7; sending any of them returns:
    #     400 invalid_request_error: `temperature` is deprecated for this model.
    # We tried temperature=0 first (every reviewer asked for it) and the API
    # rejected the run. The unit test passed because it uses a fake client --
    # a reminder that a suite of fakes cannot validate a request shape.
    #
    # So sampling determinism is NOT available here, and the reproducibility
    # problem cannot be parameter-fixed. The honest substitute is to MEASURE
    # the judge's sampling noise instead of pretending it away: judge each
    # cell `repeats` times and report the modal verdict plus whether the runs
    # were unanimous. A cell that flips across runs is a finding about scorer
    # stability, not something to hide behind a temperature setting.
    response = client.messages.create(
        model=model,
        # 1024 STARVED THE JUDGE. On a multi-stimulus criterion it reached a
        # verdict and hit the cap before emitting `cited_artifacts`, so the
        # citation check below discarded a real verdict on every run and the
        # report said the EVIDENCE was insufficient. It was not: PT-D8-01 was
        # scored NO_EVALUADO twice over 54 good, hash-verified artifacts.
        max_tokens=JUDGE_MAX_TOKENS,
        system=[{"type": "text", "text": JUDGE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        tools=[VERDICT_TOOL],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
    )
    return (
        _extract_tool_input(response, _TOOL_NAME),
        missing,
        getattr(response, "stop_reason", None),
    )


def _one_run(test: CriterionTest, evidence_rows: list[dict], client: Any, model: str) -> tuple[str, str, list[str]]:
    """One judged run -> (status, rationale, cited). Coerces uncited positives."""
    data, missing, stop_reason = _ask_once(test, evidence_rows, client, model)
    status = data.get("verdict")
    # ALLOWLIST, not blocklist. Keep only citations of artifacts that were
    # actually supplied AND actually sent. The previous rule filtered just the
    # known-missing paths, so a path the model invented -- one that appears in
    # no ledger row at all -- sailed through and got printed in the report as a
    # citation to a screenshot that never existed.
    supplied = {str(r.get("artifact")) for r in evidence_rows} - missing
    cited = [c for c in (data.get("cited_artifacts") or []) if c in supplied]
    if status not in VERDICTS:
        return NO_EVALUADO, f"Veredicto inválido del modelo: {status!r}.", []
    if status in SCORED and not cited:
        # A verdict the model cannot point at is not defensible either way, so
        # the status is the same -- but the REASON has to name what happened.
        # Blaming the evidence when the judge simply ran out of output budget
        # sent an investigation after the capture pipeline for hours while the
        # artifacts were fine.
        if stop_reason == "max_tokens":
            return NO_EVALUADO, (
                "El juez alcanzó el veredicto pero se quedó sin presupuesto de "
                f"salida (stop_reason=max_tokens, {JUDGE_MAX_TOKENS} tokens) "
                "antes de citar los artefactos. No es un problema de la "
                "evidencia: subí LXP_JUDGE_MAX_TOKENS o reducí los estímulos "
                "por criterio."
            ), []
        return NO_EVALUADO, "Evidencia insuficiente: el modelo no citó ningún artefacto válido.", []
    return status, data.get("rationale", ""), cited


def score_test(
    test: CriterionTest,
    evidence_rows: list[dict],
    client: Any,
    *,
    platform: str | None = None,
    repeats: int = 1,
    model: str | None = None,
) -> TestVerdict:
    """Judge one test on one platform against its evidence rows.

    `evidence_rows` are ledger dicts already filtered to this test AND platform
    (see EvidenceLedger.for_platform_test). Empty evidence returns NO_EVALUADO
    without calling the API. `repeats>1` calls the model N times and reports the
    modal verdict plus the full spread on `runs`.
    """
    if platform is None:
        platform = evidence_rows[0]["platform"] if evidence_rows else ""

    if not evidence_rows:
        return TestVerdict(
            test_id=test.id,
            platform=platform,
            status=NO_EVALUADO,
            points_earned=0.0,
            points_possible=0.0,
            rationale="No hay evidencia registrada para este test en esta plataforma.",
            evidence=[],
            runs=[],
        )

    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy

    runs: list[tuple[str, str, list[str]]] = [
        _one_run(test, evidence_rows, client, model) for _ in range(max(1, repeats))
    ]
    statuses = [r[0] for r in runs]
    modal = _modal(statuses)
    status, rationale, cited = next(r for r in runs if r[0] == modal)
    earned, possible = _points(status, test.points)

    return TestVerdict(
        test_id=test.id,
        platform=platform,
        status=status,
        points_earned=earned,
        points_possible=possible,
        rationale=rationale,
        evidence=cited,
        runs=statuses,
    )


def judge_fingerprint() -> str:
    """sha256 of the judge system prompt + the forced-tool schema.

    Goes into the run manifest: two runs are only comparable if the instrument
    was the same, and "same model id" does not establish that. Reads the module
    globals at call time so a monkeypatched prompt changes the hash.
    """
    payload = json.dumps(
        {"system": JUDGE_SYSTEM, "tool": VERDICT_TOOL}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aggregate(criteria: Criteria, verdicts: list[TestVerdict]) -> dict[str, DomainScore]:
    """Roll ONE platform's verdicts up to per-domain scores. Pure — no API.

    One platform per call: PLATAFORMA_B and Plataforma A share test ids, so a mixed list would
    let one platform's verdict overwrite the other's under the shared id. Passing
    verdicts from more than one platform raises -- use aggregate_by_platform.

    NO_EVALUADO tests (and tests with no verdict at all) are excluded from earned
    AND possible, and counted as pending. A domain's pct is over scored tests
    only.
    """
    platforms = {v.platform for v in verdicts}
    if len(platforms) > 1:
        raise EvalkitError(
            "aggregate recibió veredictos de varias plataformas "
            f"({', '.join(sorted(platforms))}); agregá una plataforma por vez "
            "(usá aggregate_by_platform)"
        )

    by_id = {v.test_id: v for v in verdicts}
    scores: dict[str, DomainScore] = {}

    for domain in criteria.domains:
        earned = possible = 0.0
        n_scored = n_pending = 0
        for t in domain.tests:
            v = by_id.get(t.id)
            if v is None or v.status not in SCORED:
                n_pending += 1
                continue
            earned += v.points_earned
            possible += v.points_possible
            n_scored += 1
        # None, NOT 0.0: nothing was scored, so there is no percentage to report.
        pct = round(earned / possible * 100, 1) if possible else None
        scores[domain.code] = DomainScore(
            earned=earned, possible=possible, pct=pct, n_scored=n_scored, n_pending=n_pending
        )

    return scores


def aggregate_by_platform(
    criteria: Criteria, verdicts: list[TestVerdict]
) -> dict[str, dict[str, DomainScore]]:
    """Per-platform scoreboards: {platform: {domain_code: DomainScore}}.

    Splits verdicts by platform and aggregates each independently, so PLATAFORMA_B and
    Plataforma A keep separate scores for a shared test id. This is what Task 10 and the
    report consume.
    """
    grouped: dict[str, list[TestVerdict]] = defaultdict(list)
    for v in verdicts:
        grouped[v.platform].append(v)
    return {platform: aggregate(criteria, vs) for platform, vs in grouped.items()}
