"""Extract the externally-authored instruments in `04_Reference_Frameworks/`.

    python -m lxp_eval.evalkit.banks.build_external_bank

Run it to regenerate `external_rubrics.yaml`; it is NOT imported at runtime.

--------------------------------------------------------------------------
Why
--------------------------------------------------------------------------
The bank's defensibility claim is that every criterion traces to a published
instrument. With only the author's own LXP rubric in it, a vendor's rebuttal
just moves from "an LLM wrote it" to "the la organización wrote it and grades us
with it". Independent authorship is what closes that. This file adds four
instruments nobody on this project wrote:

  BEA 2025    -- ACL/BEA shared task on Pedagogical Ability Assessment of
                 AI-powered Tutors (Kochmar et al., MBZUAI / KU Leuven / NAIST).
                 The LXP rubric ALREADY cites "BEA-MI/ML/PG/AC" as its evidence
                 base for six of its own criteria without the source being in
                 the bank. This puts the actual definitions in.
  EdReports   -- EdReports.org Quality Instructional Materials Tool: K-8
                 Mathematics. Same situation: LXP area D6 says "EdReports" and
                 the instrument was not in the bank.
  ESSA        -- the statutory evidence tiers, via US ED / IES / WWC. The only
                 instrument here with numeric, unarguable thresholds.
  UNICEF      -- EdTech for Good Framework, 1st Edition (Nov 2024), "Full
                 assessment" sheet. 14 scored sections across 5 pillars.

--------------------------------------------------------------------------
Anchors: transcribed, or declared missing. Never synthesised.
--------------------------------------------------------------------------
EdReports and ESSA state their thresholds, so the entries carry them verbatim
("Meets expectations (7-8 points)"; "At least 350 students"). BEA states a
3-point scale but illustrates it for only one of its four dimensions. UNICEF
states a per-section maximum score and nothing about what earns it.

Every entry that lacks a source-stated threshold carries `needs_anchors: true`
AND the literal marker "[SIN ANCLA EN LA FUENTE]" in the affected field. A test
asserts the two always travel together, so an entry cannot quietly acquire an
anchor it never had. Downstream, such an entry should read as weaker than an
anchored one -- that is the point of marking it rather than filling it in.

--------------------------------------------------------------------------
EXCLUDED, with reasons (see the report; this list is the honest part)
--------------------------------------------------------------------------
* ISTE_Standards_Educators_2017.pdf -- NOT the ISTE instrument. It is a saved
  Kansas State University web page reproducing the standards. Two independent
  reasons not to import it: it is a secondary reproduction, not the publisher's
  document, so "ISTE Standards for Educators p. 14" cannot be cited from it;
  and the standards judge EDUCATORS' professional practice ("Educators set
  professional learning goals..."), not a platform's behaviour. Right kind of
  document, wrong subject.
* EdTech_Genome_Project_Final_Report.pdf -- NOT the report. It is a saved
  InnovateEDU "Success Stories" web page: site navigation plus three paragraphs
  of project description. Zero criteria. The real final report would have to be
  re-downloaded.
* Washington_K12_EdTech_Standards_2018.pdf -- student technology-literacy
  standards. Judges students, not products.
* TPACK_Mishra_Koehler_2006.pdf -- a theoretical framework paper. TPACK is a
  lens for thinking about teacher knowledge; it states no assessable criterion
  and no threshold. Importing it as criteria would manufacture exactly the false
  authority this bank exists to avoid.
* UNESCO/UNICEF/ITU Charter (draft + final) and Gateways to Public Digital
  Learning -- PRINCIPLES and commitments ("states should ensure..."), addressed
  to governments. No assessable unit, no anchors.
* WWC_Procedures_Handbook_v50.pdf -- a methods handbook for rating STUDIES. It
  is the machinery behind the ESSA tiers, which are extracted; the handbook
  itself is not a product-evaluation instrument.
* GEEAP reading reports -- an evidence synthesis, not an instrument.
* Coach Teacher's Guide Diagnostic Tool -- a diagnostic for a national teacher
  guide policy, not for a learning platform.
* EdReports Rating Sheet 3.5 (Effective Technology Use) -- the section most
  relevant to an LXP, and deliberately NOT imported: the source says of it
  "This section is not scored" and gives no anchors at all. Giving it a weight
  would be this bank inventing a criterion EdReports declined to make one.
* UNICEF "Quick assessment" sheet -- a short-form subset of "Full assessment".
  Importing both would double-weight every dimension.
* The other four EMC_Peru .docx -- meeting feedback, a platform-specific test
  protocol, and an essay. Not instruments.
"""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pypdf

APP_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = APP_ROOT.parent
REFS = REPO_ROOT / "04_Reference_Frameworks" / "External_Rubrics"

OUT = Path(__file__).with_name("external_rubrics.yaml")

NO_ANCHOR = "[SIN ANCLA EN LA FUENTE]"

BEA_PDF_REL = "04_Reference_Frameworks/External_Rubrics/BEA_2025_AI_Tutor/BEA_2025_findings.pdf"
EDR_PDF_REL = "04_Reference_Frameworks/External_Rubrics/EdReports/EdReports_Math_K8_Review_Criteria.pdf"
ESSA_PDF_REL = (
    "04_Reference_Frameworks/External_Rubrics/ESSA_Evidence_Tiers/ESSA_finding_tier1-3_evidence.pdf"
)
ESSA_INFO_REL = (
    "04_Reference_Frameworks/External_Rubrics/ESSA_Evidence_Tiers/WWC_to_ESSA_Tiers_Infographic.pdf"
)
UNICEF_XLSX = REFS / "UNICEF_EdTech_for_Good" / "EdTechForGoodFramework_1stEdition_1_0.xlsx"
UNICEF_REL = (
    "04_Reference_Frameworks/External_Rubrics/UNICEF_EdTech_for_Good/"
    "EdTechForGoodFramework_1stEdition_1_0.xlsx"
)

AREAS = [
    ("D2", "Cognitive Load Theory + multimedia design", "Sweller / Mayer; EdReports Rating Sheet 3.1"),
    ("D4", "Formative assessment + mastery gating", "Rosenshine + BEA; EdReports Rating Sheet 3.3"),
    ("D5", "Misconception tracking + refutational teaching", "BEA 2025 mistake identification/location"),
    ("D6", "Curriculum coherence + EdReports criteria", "EdReports Gateways 1-2; UNICEF Pillar 3"),
    ("D7", "Teacher in the loop + dashboards", "EdReports Rating Sheet 3.2; UNICEF stakeholder feedback"),
    ("D12", "Evidence quality (ESSA Tier 1-4)", "ESSA Section 8101(21)(A) via US ED / IES / WWC"),
    ("D14", "Adaptive Software / Personalization", "EdReports Rating Sheet 3.4"),
    ("PRIV", "Privacidad y datos de menores", "UNICEF EdTech for Good, Pillar 1: Safety First"),
    ("INFRA", "Infraestructura, adaptabilidad local y asequibilidad",
     "UNICEF EdTech for Good, Pillar 4: Ready to Scale - área nueva: ninguno de los 14 "
     "dominios del banco LXP evalúa si el producto puede desplegarse y pagarse en el contexto"),
    ("INCL", "Accesibilidad, equidad de acceso y género",
     "UNICEF EdTech for Good, Pillar 5: Leave No One Behind - área nueva: el banco LXP no "
     "puntúa accesibilidad para personas con discapacidad ni equidad de género"),
]


# ---------------------------------------------------------------------------
# BEA 2025
# ---------------------------------------------------------------------------
# Las cuatro dimensiones, verbatim de la p.2 ("Below, we are reiterating the
# definitions for these dimensions from Maurya et al. (2025) for completeness").
# La escala de 3 puntos está en la p.3 y es GENÉRICA: el paper la ilustra sólo
# para mistake identification. Por eso MI lleva anclas y las otras tres las
# declaran faltantes en vez de extrapolar el ejemplo de MI a ellas.

BEA_SCALE = (
    'All aspects are annotated on a 3-point scale, where "No" denotes that the particular '
    'aspect of the tutor response is bad, "Yes" denotes that it is good, and "To some extent" '
    'denotes that the quality of the response, according to the particular aspect, is medium.'
)

BEA = [
    dict(
        id="BEA-MI", area="D5", name="Mistake identification", original_id="mistake_identification",
        page="p. 2, §2 (definiciones de Maurya et al. 2025)",
        definition=(
            "Since all dialogues in the dataset contain a mistake made by the student, we expect "
            "a good quality response from the tutor to include relevant and clear mistake "
            "identification."
        ),
        # p.3 ilustra la escala PARA ESTA dimensión, así que acá sí hay ancla.
        pass_anchor=(
            '(p. 3) "Yes" denotes that it is good (e.g., the mistake is identified clearly and '
            "correctly)."
        ),
        fail_anchor=(
            '(p. 3) "No" denotes that the particular aspect of the tutor response is bad (e.g., '
            "the mistake is not identified at all)."
        ),
        overlaps=["PT-D4-02", "PT-D5-01", "EMC-8.1"],
    ),
    dict(
        id="BEA-ML", area="D5", name="Mistake location", original_id="mistake_location",
        page="p. 2, §2 (definiciones de Maurya et al. 2025)",
        definition=(
            "In addition to notifying the student about the committed error, a good tutor response "
            "should also point to its location in the answer and explain what the error is to help "
            "the student remediate it."
        ),
        pass_anchor=None, fail_anchor=None,
        overlaps=["PT-D5-03", "PT-D5-06", "EMC-8.1"],
    ),
    dict(
        id="BEA-PG", area="D4", name="Providing guidance", original_id="providing_guidance",
        page="p. 2, §2 (definiciones de Maurya et al. 2025)",
        definition=(
            "Ideally, a good tutor should not reveal the answer immediately and instead should "
            "provide the student with relevant and helpful guidance, consisting, for example, of a "
            "hint, an explanation, or a supporting question."
        ),
        pass_anchor=None, fail_anchor=None,
        overlaps=["PT-D4-08", "PT-D1-06"],
    ),
    dict(
        id="BEA-AC", area="D4", name="Actionability", original_id="actionability",
        page="p. 2, §2 (definiciones de Maurya et al. 2025)",
        definition=(
            "Finally, once the guidance is provided to a student, a good tutor response should make "
            "it clear to the student what they are supposed to do next. I.e., the tutor's response "
            "should not be vague, unclear, or a conversation stopper."
        ),
        pass_anchor=None, fail_anchor=None,
        overlaps=["PT-D4-07"],
    ),
]


# ---------------------------------------------------------------------------
# EdReports K-8 Mathematics
# ---------------------------------------------------------------------------
# Unidad = la hoja de calificación (Rating Sheet). Cada una trae su Criterion,
# su Maximum Points y sus bandas ("Meets expectations (7-8 points)"), todo
# enunciado por el instrumento. Los indicadores individuales (1a, 2b, 3r...) NO
# entran por separado: no llevan ancla propia, sólo 0/1/2, y la banda que decide
# es la de la hoja. Importarlos duplicaría el peso de cada hoja.
#
# El texto se transcribe de la extracción del PDF corrigiendo ÚNICAMENTE los
# cortes de palabra que introduce el PDF ("coher ence" -> "coherence"). Ningún
# otro cambio.

EDREPORTS = [
    dict(
        id="EDR-1.1", area="D6", name="Focus on Major Work of the Grade", points=6, original_id="1.1",
        page="p. 5-6, Rating Sheet 1.1", severity="MEDIA",
        criterion=(
            "Materials do not assess topics before the grade level in which the topic should be "
            "introduced. (Maximum Points: 2) / Students and teachers using the materials as designed "
            "devote the large majority of class time in each grade K-8 to the major work of the "
            "grade. (Maximum Points: 4)"
        ),
        pass_anchor=(
            "Meets expectations (2 points) / Meets expectations (4 points). Nota al pie del "
            "instrumento: The materials should devote at least 65% and up to approximately 85% of "
            "class time to the major work of the grade with Grades K-2 nearer the upper end of that "
            "range, i.e., 85%."
        ),
        fail_anchor="Does not meet expectations (0 points).",
    ),
    dict(
        id="EDR-1.2", area="D6", name="Coherence", points=8, original_id="1.2",
        page="p. 6-7, Rating Sheet 1.2", severity="MEDIA",
        criterion=(
            "Each grade's instructional materials are coherent and consistent with the Standards. "
            "(Maximum Points: 8)"
        ),
        pass_anchor="Meets expectations (7-8 points).",
        fail_anchor="Partially meets expectations (5-6 points) / Does not meet expectations (<5 points).",
    ),
    dict(
        id="EDR-2.1", area="D6", name="Rigor and Balance", points=8, original_id="2.1",
        page="p. 9, Rating Sheet 2.1", severity="MEDIA",
        criterion=(
            "The instructional materials reflect the balances in the Standards and help students "
            "meet the Standards' rigorous expectations, by giving appropriate attention to: "
            "developing students' conceptual understanding; procedural skill and fluency; and "
            "engaging applications. (Maximum Points: 8)"
        ),
        pass_anchor=(
            "For 'Rigor and Balance' to attain a score of 'Meets Expectations,' materials must earn "
            "at least 7 points."
        ),
        fail_anchor="Does not meet expectations (<5 points).",
    ),
    dict(
        id="EDR-2.2", area="D6", name="Practice-Content Connection", points=10, original_id="2.2",
        page="p. 10, Rating Sheet 2.2", severity="MEDIA",
        criterion=(
            "Materials meaningfully connect the Standards for Mathematical Content and the Standards "
            "for Mathematical Practice. (Maximum Points: 10)"
        ),
        pass_anchor=(
            "For 'Practice-Content Connection' to attain a score of 'Meets Expectations,' materials "
            "must earn at least 9 points."
        ),
        fail_anchor="Does not meet expectations.",
    ),
    dict(
        id="EDR-3.1", area="D2", name="Use and Design to Facilitate Student Learning", points=8,
        original_id="3.1", page="p. 12, Rating Sheet 3.1", severity="MEDIA",
        criterion=(
            "Materials are well designed and take into account effective lesson structure and "
            "pacing. (Maximum Points: 8)"
        ),
        pass_anchor=(
            "For 'Use and design facilitate student learning' to attain a score of 'Meets "
            "Expectations,' material must earn at least 7 points."
        ),
        fail_anchor="Does not meet expectations (<5 points).",
    ),
    dict(
        id="EDR-3.2", area="D7", name="Teacher Planning and Learning for Success with CCSS", points=8,
        original_id="3.2", page="p. 13, Rating Sheet 3.2", severity="MEDIA",
        criterion="Materials support teacher learning and understanding of the Standards. (Maximum Points: 8)",
        pass_anchor=(
            "For 'Teacher Planning and Learning for Success with CCSS' to attain a score of 'Meets "
            "Expectations,' materials must earn at least 7 points. Meets expectations (7-8 points)."
        ),
        fail_anchor="Partially meets expectations (5-6 points) / Does not meet expectations (<5 points).",
    ),
    dict(
        id="EDR-3.3", area="D4", name="Assessment", points=10, original_id="3.3",
        page="p. 14, Rating Sheet 3.3", severity="ALTA",
        criterion=(
            "Materials offer teachers resources and tools to collect ongoing data about student "
            "progress on the Standards. (Maximum Points: 10)"
        ),
        pass_anchor=(
            "For 'Assessment' to attain a score of 'Meets Expectations,' materials must earn at "
            "least 9 points. Meets expectations (9-10 points)."
        ),
        fail_anchor="Partially meets expectations (6-8 points) / Does not meet expectations (<6 points).",
        overlaps=["PT-D4-05"],
    ),
    dict(
        id="EDR-3.4", area="D14", name="Differentiated Instruction", points=12, original_id="3.4",
        page="p. 15, Rating Sheet 3.4", severity="ALTA",
        criterion=(
            "Materials support teachers in differentiating instruction for diverse learners within "
            "and across grades. (Maximum Points: 12)"
        ),
        pass_anchor=(
            "For 'Differentiated Instruction' to attain a score of 'Meets Expectations,' materials "
            "must earn at least 11 points. Meets expectations (10-12 points)."
        ),
        fail_anchor="Partially meets expectations (8-9 points) / Does not meet expectations (<8 points).",
        overlaps=["PT-D14-05", "EMC-8.3"],
    ),
]


# ---------------------------------------------------------------------------
# ESSA evidence tiers
# ---------------------------------------------------------------------------
# Definiciones estatutarias, verbatim de la p.6 del PDF de IES/NCEE. Los umbrales
# numéricos (350 estudiantes, 2 sitios) salen del infográfico WWC->ESSA, que es
# el documento oficial que los enuncia; cada entrada cita el suyo.
#
# Los cuatro tiers son una ESCALERA, no cuatro criterios independientes: un
# producto alcanza el tier más alto que su evidencia soporta. `evidence_base` lo
# dice para que el puntaje no los sume como si fueran acumulables.

ESSA_LADDER = (
    "Los 4 tiers de ESSA son una escalera excluyente: aplica el más alto que la evidencia del "
    "producto alcance, no se suman."
)

ESSA = [
    dict(
        id="ESSA-T1", area="D12", name="Tier 1 - Strong Evidence", points=4, original_id="Tier 1",
        page="p. 6, tabla 'ESSA Definitions of Individual Studies Providing Tier 1, Tier 2, or Tier 3 Evidence'",
        criterion=(
            "Evidence includes a statistically significant and positive [favorable] finding from at "
            "least one \"well-designed and well-implemented experimental study\"."
        ),
        pass_anchor=(
            "(WWC_to_ESSA_Tiers_Infographic.pdf, p. 1) Meets WWC Standards Without Reservations "
            "under version 2.1 or later to meet Tier 1; Statistically significant positive effect to "
            "meet Tier 1; At least 350 students to meet Tier 1; At least two educational sites to "
            "meet Tier 1."
        ),
        fail_anchor=(
            "No alcanza alguno de los cuatro requisitos enunciados para Tier 1 (estándar WWC sin "
            "reservas, efecto positivo estadísticamente significativo, 350 estudiantes, 2 sitios)."
        ),
        overlaps=["PT-D12-01"],
    ),
    dict(
        id="ESSA-T2", area="D12", name="Tier 2 - Moderate Evidence", points=3, original_id="Tier 2",
        page="p. 6, tabla 'ESSA Definitions of Individual Studies Providing Tier 1, Tier 2, or Tier 3 Evidence'",
        criterion=(
            "Evidence includes a statistically significant and positive [favorable] finding from at "
            "least one \"well-designed and well-implemented quasi-experimental design study\"."
        ),
        pass_anchor=(
            "(WWC_to_ESSA_Tiers_Infographic.pdf, p. 1) Meets WWC Standards With or Without "
            "Reservations under version 2.1 or later to meet Tier 2; Statistically significant "
            "positive effect to meet Tier 2; At least 350 students to meet Tier 2; At least two "
            "educational sites to meet Tier 2."
        ),
        fail_anchor="No alcanza alguno de los cuatro requisitos enunciados para Tier 2.",
    ),
    dict(
        id="ESSA-T3", area="D12", name="Tier 3 - Promising Evidence", points=2, original_id="Tier 3",
        page="p. 6, tabla 'ESSA Definitions of Individual Studies Providing Tier 1, Tier 2, or Tier 3 Evidence'",
        criterion=(
            "Evidence includes a statistically significant and positive [favorable] finding from at "
            "least one \"well-designed and well-implemented correlational study with statistical "
            "controls for selection bias\"."
        ),
        pass_anchor=None,
        fail_anchor=None,
    ),
    dict(
        id="ESSA-T4", area="D12", name="Tier 4 - Demonstrates a Rationale", points=1,
        original_id="Tier 4", page="p. 6, nota al pie de la tabla de definiciones",
        criterion=(
            "Tier 4 is evidence that \"demonstrates a rationale, based on high-quality research "
            "findings or positive evaluation [findings], that [an] activity, strategy, or "
            "intervention is likely to improve student outcomes or other relevant outcomes\"."
        ),
        pass_anchor=None,
        fail_anchor=None,
    ),
]


# ---------------------------------------------------------------------------
# UNICEF EdTech for Good -- leído del .xlsx en tiempo de build
# ---------------------------------------------------------------------------
# Unidad = la sección puntuada de la hoja "Full assessment". Cada una trae su
# descripción y su puntaje máximo enunciados por el instrumento; las preguntas
# de la sección se copian como pasos de verificación. NINGUNA sección dice qué
# gana el máximo, así que las 14 declaran la falta de ancla.

UNICEF_MAP = {
    5: ("UNI-1.1", "PRIV", "Privacy and Transparency", "Pillar 1: Safety First", "CRITICA"),
    24: ("UNI-1.2", "PRIV", "Data Protection and Privacy - Data Sharing", "Pillar 1: Safety First", "CRITICA"),
    53: ("UNI-1.3", "PRIV", "Information Security", "Pillar 1: Safety First", "CRITICA"),
    74: ("UNI-1.4", "PRIV", "Safeguards", "Pillar 1: Safety First", "CRITICA"),
    106: ("UNI-2.1", "D12", "Evidence and Impact Assessment", "Pillar 2: Impact on Learning", "ALTA"),
    135: ("UNI-2.2", "D7", "Stakeholder Feedback", "Pillar 2: Impact on Learning", "MEDIA"),
    142: ("UNI-2.3", "D7", "Usage and Engagement", "Pillar 2: Impact on Learning", "MEDIA"),
    202: ("UNI-3.1", "D6", "Learning objectives", "Pillar 3: Designed for Children", "ALTA"),
    278: ("UNI-4.1", "INFRA", "Infrastructure", "Pillar 4: Ready to Scale", "ALTA"),
    322: ("UNI-4.2", "INFRA", "Local Adaptability", "Pillar 4: Ready to Scale", "ALTA"),
    402: ("UNI-4.3", "INFRA", "Affordability", "Pillar 4: Ready to Scale", "MEDIA"),
    458: ("UNI-5.1", "INCL", "Accessibility of Design", "Pillar 5: Leave No One Behind", "ALTA"),
    467: ("UNI-5.2", "INCL", "Equity of access", "Pillar 5: Leave No One Behind", "ALTA"),
    486: ("UNI-5.3", "INCL", "Gender", "Pillar 5: Leave No One Behind", "MEDIA"),
}

UNICEF_OVERLAPS = {
    "UNI-1.1": ["CSP-01", "CSP-02"],
    "UNI-2.1": ["ESSA-T1", "ESSA-T2"],
}


def _unicef_sections() -> list[dict]:
    if not UNICEF_XLSX.exists():
        raise SystemExit(f"no encuentro el instrumento UNICEF: {UNICEF_XLSX}")
    ws = openpyxl.load_workbook(UNICEF_XLSX, data_only=True)["Full assessment"]
    rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row, values_only=True))

    marks = sorted(UNICEF_MAP)
    out = []
    for n, start in enumerate(marks):
        r = rows[start - 1]
        desc, score = str(r[1] or "").strip(), str(r[6] or "").strip()
        if "Add score" not in score:
            raise SystemExit(f"la fila {start} de 'Full assessment' ya no es una sección puntuada")
        lo_hi = score.split("(")[-1].rstrip(")").strip()
        maximum = float(lo_hi.split("-")[-1])

        end = marks[n + 1] - 1 if n + 1 < len(marks) else len(rows)
        questions = []
        for rr in rows[start:end]:
            q = str(rr[1] or "").strip()
            if q and q != "\xa0" and not q.startswith("Pillar"):
                questions.append(q)

        eid, area, name, pillar, sev = UNICEF_MAP[start]
        out.append(dict(
            id=eid, area=area, name=name, points=maximum, severity=sev,
            original_id=name, page=f"hoja 'Full assessment', fila {start} ({pillar})",
            description=desc, questions=questions, pillar=pillar,
            overlaps=UNICEF_OVERLAPS.get(eid),
        ))
    return out


# ---------------------------------------------------------------------------


def _check_pdfs() -> None:
    """Falla ruidosamente si un PDF citado dejó de tener capa de texto."""
    for rel in (BEA_PDF_REL, EDR_PDF_REL, ESSA_PDF_REL, ESSA_INFO_REL):
        p = REPO_ROOT / rel
        if not p.exists():
            raise SystemExit(f"falta el instrumento citado: {rel}")
        text = "".join((pg.extract_text() or "") for pg in pypdf.PdfReader(str(p)).pages[:8])
        if len(text) < 1000:
            raise SystemExit(
                f"{rel} ya no tiene capa de texto utilizable ({len(text)} chars) "
                "- las ubicaciones de página dejaron de ser verificables"
            )


def _q(s) -> str:
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\n", " ").replace("\r", " ").replace("\xa0", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    return f'"{s.strip()}"'


def _num(x) -> str:
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


HEADER = """# Banco de criterios de evalkit -- NO editar a mano.
# Generado por lxp_eval/evalkit/banks/build_external_bank.py
#
# Cuatro instrumentos que NO escribió nadie de este proyecto. Las anclas que la
# fuente enuncia van verbatim; las que no enuncia se declaran faltantes con
# `needs_anchors: true` y la marca [SIN ANCLA EN LA FUENTE]. Nunca se sintetiza
# un umbral.
#
# Qué se dejó afuera y por qué está en la cabecera del builder. Leer eso antes
# de agregar nada acá.
#
# Los ids van entre comillas a propósito: sin comillas YAML lee 1.10 como 1.1.

bank_id: "external_rubrics"
version: "1.0"

frameworks:
  - id: "bea_2025_ai_tutor"
    name: "BEA 2025 Shared Task on Pedagogical Ability Assessment of AI-powered Tutors"
    location: "{bea}"
    note: >-
      Kochmar, Maurya, Petukhova, Srivatsa, Tack, Vasselli (MBZUAI / KU Leuven /
      NAIST). Cuatro dimensiones pedagógicas con definiciones verbatim y una
      escala de 3 puntos. La rúbrica LXP ya citaba "BEA-MI/ML/PG/AC" como base
      de evidencia sin que la fuente estuviera en el banco.
  - id: "edreports_math_k8"
    name: "EdReports.org Quality Instructional Materials Tool: K-8 Mathematics"
    location: "{edr}"
    note: >-
      Unidad: la hoja de calificación (Rating Sheet), que es lo que el
      instrumento puntúa y ancla. Los indicadores sueltos no entran: no llevan
      ancla propia y duplicarían el peso de su hoja. Rating Sheet 3.5 tampoco,
      porque la fuente dice "This section is not scored".
  - id: "essa_evidence_tiers"
    name: "ESSA Evidence Tiers (ESSA Section 8101(21)(A)) via US ED / IES / WWC"
    location: "{essa}"
    note: >-
      Escalera excluyente de 4 niveles. Definiciones verbatim del PDF de
      IES/NCEE; los umbrales numéricos (350 estudiantes, 2 sitios) del
      infográfico oficial WWC->ESSA, citado por entrada.
  - id: "unicef_edtech_for_good"
    name: "UNICEF EdTech for Good Framework, 1st Edition (Nov 2024)"
    location: "{uni}"
    note: >-
      Hoja "Full assessment": 14 secciones puntuadas en 5 pilares. Cada sección
      trae descripción y puntaje máximo de la fuente, pero el instrumento no
      dice qué gana el máximo: las 14 declaran la falta de ancla. La hoja
      "Quick assessment" es un subconjunto y no se importa.
""".format(bea=BEA_PDF_REL, edr=EDR_PDF_REL, essa=ESSA_PDF_REL, uni=UNICEF_REL)


def _emit(out: list[str], *, eid, area, name, points, severity, verification,
          passes, fails, evidence_base, framework, original_id, location,
          needs_anchors=False, overlaps=None) -> None:
    out.append(f"  - id: {_q(eid)}")
    out.append(f"    area: {_q(area)}")
    out.append(f"    name: {_q(name)}")
    out.append(f"    points: {_num(points)}")
    out.append(f"    severity: {_q(severity)}")
    out.append(f"    verification: {_q(verification)}")
    out.append(f"    pass_criteria: {_q(passes)}")
    out.append(f"    fail_criteria: {_q(fails)}")
    out.append(f"    evidence_base: {_q(evidence_base)}")
    if needs_anchors:
        out.append("    needs_anchors: true")
    if overlaps:
        out.append("    overlaps:")
        for o in overlaps:
            out.append(f"      - {_q(o)}")
    out.append("    source:")
    out.append('      type: "bank"')
    out.append(f"      framework: {_q(framework)}")
    out.append(f"      original_id: {_q(original_id)}")
    out.append(f"      location: {_q(location)}")


def build() -> str:
    _check_pdfs()
    out = [HEADER, "areas:"]
    for aid, name, ev in AREAS:
        out.append(f"  - id: {_q(aid)}")
        out.append(f"    name: {_q(name)}")
        out.append(f"    evidence_base: {_q(ev)}")
    out.append("")
    out.append("entries:")

    # BEA -------------------------------------------------------------------
    for c in BEA:
        anchored = c["pass_anchor"] is not None
        passes = c["pass_anchor"] if anchored else (
            f"{NO_ANCHOR} El instrumento define la dimensión pero ilustra la escala de 3 puntos "
            f'sólo para mistake identification. Escala genérica declarada (p. 3): {BEA_SCALE} '
            "Falta el umbral específico de esta dimensión; hay que anclarlo antes de puntuar."
        )
        fails = c["fail_anchor"] if anchored else (
            f"{NO_ANCHOR} Ver pass_criteria: la fuente no enuncia el umbral de esta dimensión."
        )
        _emit(
            out, eid=c["id"], area=c["area"], name=c["name"], points=1, severity="ALTA",
            verification=(
                f"Definición de la fuente: {c['definition']} Anotar la respuesta del tutor en la "
                f"escala de 3 puntos del instrumento. {BEA_SCALE}"
            ),
            passes=passes, fails=fails,
            evidence_base=(
                "BEA 2025 Shared Task, definiciones tomadas de Maurya et al. (2025); alineadas por "
                "el paper con los principios de las ciencias del aprendizaje (Chi & Wylie 2014; "
                "Mayer 2002). PESO PROPIO: el shared task no pondera las dimensiones (evalúa macro "
                "F1 por track), el peso 1 lo pone este banco."
            ),
            framework="bea_2025_ai_tutor", original_id=c["original_id"],
            location=f"{BEA_PDF_REL}#{c['page']}",
            needs_anchors=not anchored, overlaps=c.get("overlaps"),
        )

    # EdReports -------------------------------------------------------------
    for c in EDREPORTS:
        _emit(
            out, eid=c["id"], area=c["area"], name=c["name"], points=c["points"],
            severity=c["severity"],
            verification=(
                f"Criterio del instrumento: {c['criterion']} Calificar los indicadores de la hoja "
                "y totalizar contra la banda enunciada."
            ),
            passes=c["pass_anchor"], fails=c["fail_anchor"],
            evidence_base=(
                "EdReports.org Quality Instructional Materials Tool: K-8 Mathematics. El puntaje y "
                "las bandas son los de la hoja de calificación. ALCANCE: el instrumento evalúa "
                "materiales de matemática K-8 alineados a CCSS; las Gateways 1-2 son específicas "
                "de ese currículo y no se trasladan sin más a otro sistema educativo."
            ),
            framework="edreports_math_k8", original_id=c["original_id"],
            location=f"{EDR_PDF_REL}#{c['page']}", overlaps=c.get("overlaps"),
        )

    # ESSA ------------------------------------------------------------------
    for c in ESSA:
        anchored = c["pass_anchor"] is not None
        passes = c["pass_anchor"] if anchored else (
            f"{NO_ANCHOR} La definición estatutaria de este tier no trae los umbrales numéricos que "
            "el infográfico oficial sí enuncia para Tier 1 y Tier 2 (350 estudiantes, 2 sitios). "
            "Hay que anclarlo antes de puntuar."
        )
        fails = c["fail_anchor"] if anchored else (
            f"{NO_ANCHOR} Ver pass_criteria: la fuente no enuncia umbrales para este tier."
        )
        _emit(
            out, eid=c["id"], area=c["area"], name=c["name"], points=c["points"], severity="ALTA",
            verification=(
                f"Definición estatutaria: {c['criterion']} Buscar los estudios del producto en el "
                "What Works Clearinghouse (whatworks.ed.gov) y verificar si alguno cumple."
            ),
            passes=passes, fails=fails,
            evidence_base=f"ESSA Section 8101(21)(A), vía US ED / IES / WWC. {ESSA_LADDER}",
            framework="essa_evidence_tiers", original_id=c["original_id"],
            location=f"{ESSA_PDF_REL}#{c['page']}",
            needs_anchors=not anchored, overlaps=c.get("overlaps"),
        )

    # UNICEF ----------------------------------------------------------------
    for c in _unicef_sections():
        preguntas = " ".join(f"({i}) {q}" for i, q in enumerate(c["questions"], 1))
        _emit(
            out, eid=c["id"], area=c["area"], name=c["name"], points=c["points"],
            severity=c["severity"],
            verification=(
                f"Descripción de la sección: {c['description']} Preguntas del instrumento: {preguntas}"
            ),
            passes=(
                f"{NO_ANCHOR} El instrumento enuncia el puntaje máximo de la sección "
                f"({_num(c['points'])}) y sus preguntas, pero NO dice qué respuestas ganan el "
                "máximo ni cuál es el mínimo aceptable. Hay que anclarlo con el evaluador antes "
                "de puntuar."
            ),
            fails=f"{NO_ANCHOR} Ver pass_criteria: la fuente no enuncia umbral para esta sección.",
            evidence_base=(
                f"UNICEF EdTech for Good Framework, 1a edición (Nov 2024), {c['pillar']}. "
                "Puntaje máximo de la fuente. Formato de autoevaluación del proveedor con columna "
                "'Evaluator score': las respuestas hay que verificarlas, no aceptarlas."
            ),
            framework="unicef_edtech_for_good", original_id=c["original_id"],
            location=f"{UNICEF_REL}#{c['page']}",
            needs_anchors=True, overlaps=c.get("overlaps"),
        )

    return "\n".join(out) + "\n"


if __name__ == "__main__":
    OUT.write_text(build(), encoding="utf-8")
    print(f"wrote {OUT}")
