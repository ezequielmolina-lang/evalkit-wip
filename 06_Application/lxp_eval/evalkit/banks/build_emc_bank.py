"""Extract the EMC Peru *AI Tutor Assessment Framework* into a criteria bank.

    python -m lxp_eval.evalkit.banks.build_emc_bank

Run it to regenerate `emc_peru_ai_tutor.yaml`; it is NOT imported at runtime.
The bank is committed so a clone has the asset without the .docx.

--------------------------------------------------------------------------
Why this instrument, and why it is worth more than the others
--------------------------------------------------------------------------
Source: `04_Reference_Frameworks/EMC_Peru/AI_Tutor_Assessment_Framework_Complete.docx`
(la organización Education Team, January 2025). It is externally authored AND it is
about AI tutors specifically, which is the use case the product is sold for.
Every other external instrument in the repo is about edtech-in-general.

--------------------------------------------------------------------------
The unit of assessment: Part 8, not Part 11
--------------------------------------------------------------------------
The document contains criteria at three grains, and they are the SAME criteria
restated:

  * Parts 4-7 -- test protocols, each with a verbatim "✓ Pass Criteria" /
    "✗ Failure Indicators" table. Finest grain, best anchors, no weights.
  * Part 8 -- the scoring rubric: six dimensions, 100 points, each with three
    verbatim score bands. Carries the weights.
  * Part 11 -- an integration CHECKLIST that restates Parts 4-8 as bullets,
    and 11.6 calls itself "Quick Reference".

Importing all three would triple-weight every dimension. So the bank takes
Part 8 as the unit -- it is the only grain the source itself weights -- and
folds each protocol into the dimension it scores, as the `verification` steps
and as ADDITIONAL verbatim anchors, attributed to their own section. Nothing is
paraphrased: pass/fail text is copied out of the .docx cells at build time, not
retyped here, so a transcription error is not possible in the direction that
matters. Part 11 is not imported; see NOT EXTRACTED below.

--------------------------------------------------------------------------
What is authored here rather than transcribed
--------------------------------------------------------------------------
Two things, both declared in the YAML so nobody mistakes them for source text:

  1. The `points` of EMC-6.1. Part 8 does NOT score engagement-illusion
     resistance, although Part 6 defines a full test for it with anchors. The
     entry carries `unscored_in_source: true` and says so in `evidence_base`.
     The six Part-8 entries keep the source's own weights, and they sum to the
     100 the source declares -- a test asserts it.
  2. Area assignment, and the two new areas (CAL, ENG). See AREAS below.

--------------------------------------------------------------------------
NOT EXTRACTED, on purpose
--------------------------------------------------------------------------
* Parts 2 and 3 (the diagnostic question battery, 8 questions with misconception
  tables). This is STIMULUS, not instrument: it is material you show a tutor in
  order to run the tests in Parts 4-8, not a criterion the tutor is judged
  against. It belongs in a probe library, not a criteria bank. The section says
  of itself that quadratics are a "Sample Domain".
* Part 9 (question-quality criteria). These judge the EVALUATOR's diagnostic
  questions, not the product under evaluation. Real criteria, wrong subject.
* Part 10 (student profile scenarios). Scenarios with "Expected:" prose, no
  pass/fail anchors, and each maps onto an adaptive behaviour already scored by
  8.3. Folded in as verification context for 8.3 rather than imported.
* Part 11 (integration checklist). A restatement of Parts 4-8, as above.
* The Appendix (extending to other curriculum areas). Guidance for authoring
  more questions; not assessable.
* The other four EMC_Peru .docx files -- see the report; they are meeting
  feedback, a testing protocol for two named platforms, and an essay.
"""
from __future__ import annotations

from pathlib import Path

import docx

APP_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = APP_ROOT.parent

DOCX = (
    REPO_ROOT / "04_Reference_Frameworks" / "EMC_Peru"
    / "AI_Tutor_Assessment_Framework_Complete.docx"
)
DOCX_REL = "04_Reference_Frameworks/EMC_Peru/AI_Tutor_Assessment_Framework_Complete.docx"
OUT = Path(__file__).with_name("emc_peru_ai_tutor.yaml")

FRAMEWORK = "emc_peru_ai_tutor_v1"

#: Areas. Four map onto the LXP bank's existing domains (same ids, so a merged
#: bank lines up). Two are new because the existing 14 genuinely do not cover
#: them: nothing in the LXP rubric scores metacognitive confidence calibration,
#: and nothing scores a system's willingness to refuse what the student prefers.
AREAS = [
    ("D1", "Gradual Release (I Do / We Do / You Do)", "Rosenshine 2012; EMC Peru Part 8.2"),
    ("D3", "Retrieval practice + spaced + interleaving", "Roediger, Karpicke; EMC Peru Part 8.4-8.5"),
    ("D5", "Misconception tracking + refutational teaching", "GEEAP + BEA; EMC Peru Part 8.1"),
    ("D14", "Adaptive Software / Personalization", "VanLehn 2011; EMC Peru Part 8.3"),
    ("CAL", "Calibración de confianza y metacognición",
     "EMC Peru Part 5 (hypercorrection effect, Barton) - área nueva: ninguno de los "
     "14 dominios del banco LXP puntúa calibración de confianza"),
    ("ENG", "Resistencia a la ilusión de engagement",
     "EMC Peru Part 6 - área nueva: ningún dominio del banco LXP puntúa si el sistema "
     "sostiene la práctica intercalada cuando el estudiante pide la bloqueada"),
]

#: One row per bank entry.
#:   id, area, name, points, severity, original_id,
#:   scoring_table  -- index of the Part 8 band table (None = unscored dimension)
#:   protocol_tables -- [(table index, section label)] whose Pass/Failure columns
#:                      are appended verbatim to the anchors
#:   protocol_sections -- headings whose paragraphs become `verification`
#:   evidence_base
#:   overlaps -- ids in the LXP bank that state a related criterion
ENTRIES = [
    dict(
        id="EMC-8.1", area="D5", name="Diagnostic Accuracy", points=25, severity="ALTA",
        original_id="8.1", location_anchor="Part 8.1 Diagnostic Accuracy (25 points), tabla de bandas",
        scoring_table=17,
        protocol_tables=[(10, "Part 4.4 Misconception-Targeted Response")],
        protocol_sections=["4.4 Misconception-Targeted Response"],
        evidence_base=(
            "EMC Peru Part 1.5 (LearnLM/Eedi RCT 2024): el factor crítico de éxito fue "
            "que las preguntas diagnósticas identificaran las concepciones erróneas ANTES "
            "de tutorizar."
        ),
        overlaps=["PT-D5-01", "PT-D5-03", "PT-D5-06", "PT-D14-02"],
    ),
    dict(
        id="EMC-8.2", area="D1", name="Instructional Sequencing", points=20, severity="ALTA",
        original_id="8.2", location_anchor="Part 8.2 Instructional Sequencing (20 points), tabla de bandas",
        scoring_table=18,
        protocol_tables=[(7, "Part 4.1 Instruction Before Testing")],
        protocol_sections=["4.1 Instruction Before Testing", "4.2 Worked Example Quality (Barton-Informed)"],
        evidence_base=(
            "EMC Peru Part 4.1: «Retrieval practice cannot create memories that don't "
            "exist. Testing before teaching wastes time and creates frustration.»"
        ),
        overlaps=["PT-D1-02", "PT-D1-05", "PT-D1-06"],
    ),
    dict(
        id="EMC-8.3", area="D14", name="Adaptive Scaffolding", points=20, severity="ALTA",
        original_id="8.3", location_anchor="Part 8.3 Adaptive Scaffolding (20 points), tabla de bandas",
        scoring_table=19,
        protocol_tables=[
            (9, "Part 4.3 Exit Ramps from Socratic Questioning"),
            (11, "Part 4.5 Disengagement Detection"),
        ],
        protocol_sections=[
            "4.3 Exit Ramps from Socratic Questioning",
            "4.5 Disengagement Detection",
        ],
        evidence_base="EMC Peru Part 1.3: «Exit ramp intelligence: Does it know when to stop Socratic questioning?»",
        overlaps=["PT-D14-05", "PT-D14-07", "PT-D4-06"],
    ),
    dict(
        id="EMC-8.4", area="D3", name="System Architecture", points=15, severity="MEDIA",
        original_id="8.4", location_anchor="Part 8.4 System Architecture (15 points), tabla de bandas",
        scoring_table=20,
        protocol_tables=[(15, "Part 7.4 Non-Interference Sequencing (Barton-Informed)")],
        protocol_sections=[
            "7.1 Knowledge Tracking", "7.2 Spaced Retrieval", "7.3 Problem Interleaving",
            "7.4 Non-Interference Sequencing (Barton-Informed)",
        ],
        evidence_base=(
            "EMC Peru Part 7.4: enseñar conceptos muy parecidos de forma consecutiva "
            "causa interferencia de codificación."
        ),
        overlaps=["PT-D3-02", "PT-D3-03", "PT-D14-03", "PT-D14-04"],
    ),
    dict(
        id="EMC-8.5", area="D3", name="Desirable Difficulty", points=10, severity="MEDIA",
        original_id="8.5", location_anchor="Part 8.5 Desirable Difficulty (10 points), tabla de bandas",
        scoring_table=21,
        protocol_tables=[(16, "Part 7.5 Desirable Difficulty: Progressive Scaffolding Removal")],
        protocol_sections=["7.5 Desirable Difficulty: Progressive Scaffolding Removal"],
        evidence_base=(
            "EMC Peru Part 7.5 (expertise reversal effect): lo que ayuda a un principiante "
            "perjudica a un experto; un buen sistema desvanece el apoyo."
        ),
        overlaps=["PT-D1-07", "PT-D14-06"],
    ),
    dict(
        id="EMC-8.6", area="CAL", name="Confidence Calibration", points=10, severity="MEDIA",
        original_id="8.6", location_anchor="Part 8.6 Confidence Calibration (10 points), tabla de bandas",
        scoring_table=22,
        protocol_tables=[(12, "Part 5.1 Test Protocol")],
        protocol_sections=["5.1 Test Protocol"],
        evidence_base=(
            "EMC Peru Part 5 (efecto de hipercorrección, Barton): los errores cometidos "
            "con alta confianza se corrigen con más eficacia que los de baja confianza."
        ),
    ),
    dict(
        id="EMC-6.1", area="ENG", name="Engagement Illusion Resistance", points=1, severity="ALTA",
        original_id="6.1", location_anchor="Part 6.1 Test Protocol: Student Requests Blocked Practice",
        scoring_table=None,
        protocol_tables=[(14, "Part 6.1 Test Protocol: Student Requests Blocked Practice")],
        protocol_sections=["6.1 Test Protocol: Student Requests Blocked Practice"],
        unscored_in_source=True,
        evidence_base=(
            "EMC Peru Part 6: los estudiantes prefieren consistentemente la práctica "
            "bloqueada, la relectura y la práctica masiva, y en todos los casos el método "
            "preferido produce peor aprendizaje. PESO PROPIO: la Part 8 del instrumento no "
            "le asigna puntaje a esta dimensión aunque define el test completo con anclas; "
            "el peso 1 lo pone este banco, el criterio y las anclas son de la fuente."
        ),
    ),
]


# --- lectura del .docx ------------------------------------------------------


def _doc():
    if not DOCX.exists():
        raise SystemExit(f"no encuentro el instrumento: {DOCX}")
    return docx.Document(str(DOCX))


def _pass_fail(doc, index: int, expect: tuple[str, str]) -> tuple[str, str]:
    """Las dos columnas de una tabla ✓/✗, verbatim, con el encabezado verificado."""
    t = doc.tables[index]
    head = [c.text.strip() for c in t.rows[0].cells]
    if head[0] != expect[0] or head[1] != expect[1]:
        raise SystemExit(
            f"la tabla {index} ya no es la esperada: encabezado {head!r}, esperaba {list(expect)!r}"
        )
    row = t.rows[1].cells
    return row[0].text.strip(), row[1].text.strip()


def _bands(doc, index: int) -> tuple[str, str]:
    """Banda superior e inferior de una tabla de puntaje de la Part 8, verbatim."""
    t = doc.tables[index]
    head = [c.text.strip() for c in t.rows[0].cells]
    if head != ["Score", "Criteria", "Evidence"]:
        raise SystemExit(f"la tabla de puntaje {index} tiene encabezado {head!r}")
    def band(r):
        c = [x.text.strip() for x in t.rows[r].cells]
        return f"{c[0]} puntos - {c[1]}. Evidencia: {c[2]}"
    return band(len(t.rows) - 1), band(1)


def _section(doc, heading: str) -> list[str]:
    """Párrafos bajo un encabezado, hasta el siguiente encabezado del mismo nivel o mayor."""
    paras = doc.paragraphs
    start = None
    for i, p in enumerate(paras):
        if p.text.strip() == heading and p.style.name.startswith("Heading"):
            start, level = i, int(p.style.name.split()[-1])
            break
    if start is None:
        raise SystemExit(f"no encuentro la sección {heading!r} en el instrumento")
    out = []
    for p in paras[start + 1:]:
        st = p.style.name
        if st.startswith("Heading") and int(st.split()[-1]) <= level:
            break
        txt = p.text.strip()
        if txt:
            out.append(txt)
    return out


def _bullets(text: str) -> list[str]:
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


# --- emisión ----------------------------------------------------------------


def _q(s) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return f'"{s.strip()}"'


def _num(x) -> str:
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


HEADER = f"""# Banco de criterios de evalkit -- NO editar a mano.
# Generado por lxp_eval/evalkit/banks/build_emc_bank.py
#
# Instrumento: AI Tutor Diagnostic Assessment Framework (la organización Education
# Team, enero 2025), Peru 5th Year Secondary Mathematics. Externo al autor de
# la rúbrica LXP y específico de tutores de IA.
#
# Las anclas pass/fail se COPIAN de las celdas del .docx en tiempo de build; no
# se reescriben acá. Cada ancla lleva entre paréntesis la sección de la que sale.
#
# Los ids van entre comillas a propósito: sin comillas YAML lee 1.10 como 1.1.

bank_id: "emc_peru_ai_tutor"
version: "1.0"

frameworks:
  - id: "{FRAMEWORK}"
    name: "EMC Peru -- AI Tutor Diagnostic Assessment Framework"
    location: "{DOCX_REL}"
    note: >-
      la organización Education Team, enero 2025. Unidad de evaluación: las seis
      dimensiones puntuadas de la Part 8 (100 puntos), con los protocolos de
      las Parts 4-7 plegados como verificación y anclas adicionales. Parts 2-3
      (batería de preguntas) son estímulo, no instrumento; Part 11 es un
      resumen de las Parts 4-8. Ver la cabecera del builder.
"""


def build() -> str:
    doc = _doc()
    out = [HEADER, "areas:"]
    for aid, name, ev in AREAS:
        out.append(f"  - id: {_q(aid)}")
        out.append(f"    name: {_q(name)}")
        out.append(f"    evidence_base: {_q(ev)}")
    out.append("")
    out.append("entries:")

    for spec in ENTRIES:
        passes: list[str] = []
        fails: list[str] = []

        if spec["scoring_table"] is not None:
            top, bottom = _bands(doc, spec["scoring_table"])
            passes.append(f"(Part {spec['original_id']}, banda superior) {top}")
            fails.append(f"(Part {spec['original_id']}, banda inferior) {bottom}")

        for idx, label in spec["protocol_tables"]:
            expect = (
                ("✓ Good Sequencing", "✗ Interference Risk") if idx == 15
                else ("✓ Pass Criteria", "✗ Failure Indicators")
            )
            p, f = _pass_fail(doc, idx, expect)
            passes.append(f"({label}) " + "; ".join(_bullets(p)))
            fails.append(f"({label}) " + "; ".join(_bullets(f)))

        steps: list[str] = []
        for heading in spec["protocol_sections"]:
            body = _section(doc, heading)
            steps.append(f"[{heading}] " + " ".join(body))
        verification = " ".join(steps)

        out.append(f"  - id: {_q(spec['id'])}")
        out.append(f"    area: {_q(spec['area'])}")
        out.append(f"    name: {_q(spec['name'])}")
        out.append(f"    points: {_num(spec['points'])}")
        out.append(f"    severity: {_q(spec['severity'])}")
        out.append(f"    verification: {_q(verification)}")
        out.append(f"    pass_criteria: {_q(' | '.join(passes))}")
        out.append(f"    fail_criteria: {_q(' | '.join(fails))}")
        out.append(f"    evidence_base: {_q(spec['evidence_base'])}")
        if spec.get("unscored_in_source"):
            out.append("    unscored_in_source: true")
        if spec.get("overlaps"):
            out.append("    overlaps:")
            for o in spec["overlaps"]:
                out.append(f"      - {_q(o)}")
        out.append("    source:")
        out.append('      type: "bank"')
        out.append(f"      framework: {_q(FRAMEWORK)}")
        out.append(f"      original_id: {_q(spec['original_id'])}")
        out.append(f"      location: {_q(DOCX_REL + '#' + spec['location_anchor'])}")

    return "\n".join(out) + "\n"


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")
