"""Turn a plain-language objective into a defensible criteria framework.

This is the front of the pipeline: the step before `generate_probes`.

    result = build_framework(
        "un tutor de IA de matemática para 1º a 6º grado en escuelas públicas",
        areas=["D1", "D3", "D9"],
        documents=[Document(name="politica.md", text=...)],
        client=anthropic.Anthropic(),
    )
    result.write_yaml(Path("criterios.yaml"))       # loads through load_criteria
    result.write_provenance(Path("procedencia.json"))

--------------------------------------------------------------------------
The framework follows the OBJECTIVE; the bank corroborates it
--------------------------------------------------------------------------
The first build of this module derived the criteria set FROM the bank: the bank
was the ceiling, and anything it did not carry could not be evaluated. That was
backwards, and the author corrected it.

The reason it was backwards is specific. The LXP rubric in the bank was ALREADY
FILTERED BY OBSERVABILITY -- criteria were removed during the pilot precisely
because they could not be settled by walking the platform. Building the
product's framework from that bank inherited a pilot-specific filter as if it
were a principle, so the product could never evaluate what the pilot chose not
to evaluate. A ministry cannot buy a tool that is structurally blind to the
things the last evaluation happened to skip.

So the flow is inverted:

1. derive the criteria the OBJECTIVE calls for;
2. for each, look for bank support. If a published instrument states it, attach
   that provenance (`type: bank`) and prefer the expert's wording and anchors;
3. if nothing in the bank covers it, the criterion STILL BELONGS -- it comes out
   `type: generated` with the `SIN RESPALDO` marker, which is visible and
   arguable, instead of being dropped, which is neither;
4. wording is ADAPTED to the context (subject, grades, language, platform kind)
   with the original kept alongside, so an adaptation can be diffed against what
   the expert wrote. Adaptation may CLARIFY; it may not invent scope;
5. uploaded documents are a first-class source. A ministry's own policy outranks
   anything we generate -- which is exactly why a criterion citing a document
   must quote text that is ACTUALLY in it, checked here, not trusted.

Everything about provenance is unchanged and stays in full. It is the product's
defensibility: when the losing vendor asks *where does this criterion come from
and who says it matters*, "an LLM wrote it" ends the conversation. The change is
that the bank is now CORROBORATION rather than a gate.

The offered areas are no longer limited to `bank.offered_areas()` either. The
bank's areas remain suggestions; the objective can call for areas no instrument
we hold has ever heard of.

--------------------------------------------------------------------------
Not everything that matters can be seen on a platform
--------------------------------------------------------------------------
Some criteria matter for the objective and no walkthrough can settle them: does
the vendor have a signed DPA? does sustained use improve learning? Those are
carried, not dropped. Each criterion declares an OBSERVABILITY CLASS when it is
written -- observable / needs a document / needs data / needs a study -- and the
non-observable ones are reported `NO_EVALUADO` with a machine-readable
`evidencia_requerida` and kept out of the scored denominator. Only observable
criteria reach `generate_probes`.

That turns "we could not check this" into an actionable request the ministry can
send to the vendor, which is more useful than dropping the criterion, and is the
whole point of the change.

--------------------------------------------------------------------------
Under-coverage is surfaced, never filled -- on TWO axes
--------------------------------------------------------------------------
`FrameworkResult.coverage` reports, per requested area:

* PROVENANCE: whether the bank covers it well, thinly, only through generated
  criteria, or not at all;
* EVIDENCE KIND: how much of it is platform-observable versus needing a
  document, data, or a study.

Both exist because of one documented bug: the report rendered an UNMEASURABLE
domain (`0/0`) identically to one that scored zero, so the vendor that HID a
surface read better than the vendor that exposed it. An area full of
`REQUIERE_ESTUDIO` criteria reading like an area we walked end to end is that
same bug wearing a different hat. `coverage.summary_line()` is the sentence a
ministry reads: *of the N things you care about, X we can check by walking the
platform, Y need documents from the vendor, Z need an impact study.*

--------------------------------------------------------------------------
The review council
--------------------------------------------------------------------------
Five judges, five different jobs, run in parallel, never shown each other's
verdicts: cobertura, no_solapamiento, medibilidad, neutralidad,
proporcionalidad. Conventions, the `Judge`/`JudgeVerdict`/`ReviewRound` types,
the forced review tool and the API call site are IMPORTED from `generator`, not
re-implemented, so the two councils cannot drift apart.

A sixth seat, `OBSERVABILITY_JUDGE`, is not on that council: it runs once,
before review, and it CLASSIFIES rather than votes. It used to reject probes an
evaluator supposedly could not execute -- a plausibility call dressed as a
feasibility call, made by a judge with no platform access, whose effect was to
silently reproduce the pilot's filtering. Its only rejection now is genuine
incoherence: a criterion no evidence of any kind could settle. Lack of ACCESS is
never a rejection; it is a class. `medibilidad` was corrected to match, or it
would have voted back out what the classifier carried in.

One seam does not reach: `generator._ask` pins `output_config={"effort":
"high"}`, so per-judge effort is not settable from here without editing
generator.py (which this module is not allowed to touch). High is the right
setting for all five of these jobs, so nothing is lost today, but the knob is
absent rather than chosen. Noted rather than worked around by cloning `_ask`.

--------------------------------------------------------------------------
API contract
--------------------------------------------------------------------------
NO `temperature` / `top_p` / `top_k`: removed on this model, `400
invalid_request_error`. Inherited by reusing `generator._ask`, which is the
validated call site.

Author-facing errors are Spanish (voseo) with ASCII scaffolding.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from .criteria import Criteria, CriterionDomain, CriterionTest
from .errors import EvalkitError
from .generator import (
    APPROVE,
    REVIEW_TOOL,
    UNKNOWN_VERDICT,
    VERDICTS,
    Judge,
    JudgeVerdict,
    ReviewRound,
    _ask,
)
from .msgs import blank_fields, is_blank, label, missing_fields
#: The verdict a non-observable criterion is reported as. IMPORTED from the
#: scorer rather than restated, so the two cannot drift into two spellings of
#: the same verdict -- the join between them is a bare string.
from .scoring import NO_EVALUADO
from .yamlsrc import line_of, load_yaml_with_lines, raw_text, require_quoted


class FrameworkError(EvalkitError):
    """Raised when the bank is invalid or the framework build cannot stand up.

    Subclasses EvalkitError so the CLI's single `except EvalkitError` renders it
    like every other author-facing failure.
    """


# --- sources ----------------------------------------------------------------

SOURCE_BANK = "bank"
SOURCE_DOCUMENT = "document"
SOURCE_GENERATED = "generated"
#: A requirement lifted from the law of the country the platform will operate
#: in. It gets its own type rather than riding in as a `document` because a
#: reader must see the difference INSTANTLY: a legal duty is not a pedagogical
#: criterion, it is not arguable on pedagogical grounds, and the consequence of
#: getting it wrong is a ministry deploying a platform it may not lawfully
#: deploy. It carries an instrument, an article and a jurisdiction.
SOURCE_STATUTE = "statute"
#: The only four legal provenances. There is no fifth, and in particular no
#: "unknown": a criterion whose origin we cannot name does not get emitted.
SOURCE_TYPES = (SOURCE_BANK, SOURCE_DOCUMENT, SOURCE_GENERATED, SOURCE_STATUTE)

#: What the emitted YAML says next to a criterion nobody authored but us. Loud
#: on purpose: this is the one distinction a reader must never have to hunt for.
UNSOURCED_MARK = "SIN RESPALDO"


# --- observability ----------------------------------------------------------
#
# The correction that this axis exists to implement. The shipped LXP bank was
# ALREADY filtered by observability: the author removed criteria precisely
# because they could not be settled by walking the platform. Building the
# product's framework FROM that bank inherits a pilot-specific filter as if it
# were a principle -- and the product can then never evaluate what the pilot
# chose not to evaluate. That is backwards.
#
# So the framework follows the OBJECTIVE. If a criterion matters for the stated
# objective it belongs in the framework, EVEN WHEN no walkthrough can settle it.
# Those criteria are carried, reported NO_EVALUADO with a stated reason and a
# machine-readable request for the evidence that WOULD settle them, and kept out
# of the scored denominator. That is the same discipline the scorer already
# enforces one level down (NO_EVALUADO is never rendered as zero), extended one
# level up.
#
# "We could not check this" is a dead end. "Ask the vendor for the signed DPA"
# is an action a ministry can take. That difference is the point of the change.

OBSERVABLE_EN_PLATAFORMA = "OBSERVABLE_EN_PLATAFORMA"
REQUIERE_DOCUMENTACION = "REQUIERE_DOCUMENTACION"
REQUIERE_DATOS = "REQUIERE_DATOS"
REQUIERE_ESTUDIO = "REQUIERE_ESTUDIO"

#: The four kinds of evidence a criterion can require. Order is the reporting
#: order and the tool enum's order; it is not a ranking.
OBSERVABILITY_CLASSES = (
    OBSERVABLE_EN_PLATAFORMA,
    REQUIERE_DOCUMENTACION,
    REQUIERE_DATOS,
    REQUIERE_ESTUDIO,
)

#: The fifth outcome, and it is NOT a fifth kind of evidence -- it is the
#: statement that no evidence settles the question.
#:
#: The compliance path forced this. Some legal requirements cannot be decided by
#: any walkthrough and by no document the vendor could produce either: they turn
#: on a regulator's interpretation, on a court, or on a fact about the vendor's
#: internal operations that an evaluation has no standing to establish. Filing
#: those under REQUIERE_DOCUMENTACION would publish, inside a procurement file,
#: the promise that a document is coming -- and a ministry would go and ask the
#: vendor for a document that does not exist and would not settle it anyway.
#:
#: So it is its own outcome, it is reported as its own outcome, and the sentence
#: it renders says a lawyer decides. It is deliberately NOT offered to the
#: generic observability classifier: given to the judge that grades pedagogical
#: criteria it becomes a door to park anything difficult behind. Only the
#: statute extractor may declare it.
NO_DECIDIBLE_EN_EVALUACION = "NO_DECIDIBLE_EN_EVALUACION"

#: Every outcome a criterion may carry. `OBSERVABILITY_CLASSES` stays the four
#: EVIDENCE classes, because that is what the generic minting tools offer; this
#: is what a criterion is VALIDATED against and what every count is derived
#: from. Anything that counts criteria must iterate this tuple, or a criterion
#: would disappear from the totals instead of being reported -- which is the
#: exact failure this whole module exists to refuse.
ALL_OBSERVABILITY_CLASSES = OBSERVABILITY_CLASSES + (NO_DECIDIBLE_EN_EVALUACION,)

#: Everything that a walkthrough cannot settle. These never reach
#: `generate_probes` and never enter a score.
NON_OBSERVABLE_CLASSES = tuple(
    c for c in ALL_OBSERVABILITY_CLASSES if c != OBSERVABLE_EN_PLATAFORMA
)

OBSERVABILITY_LABEL = {
    OBSERVABLE_EN_PLATAFORMA: "se puede verificar recorriendo la plataforma",
    REQUIERE_DOCUMENTACION: "requiere un documento del proveedor",
    REQUIERE_DATOS: "requiere acceso a datos",
    REQUIERE_ESTUDIO: "requiere un estudio",
    NO_DECIDIBLE_EN_EVALUACION: (
        "ninguna evaluación lo puede zanjar: requiere interpretación jurídica"
    ),
}

#: Said on every artefact the compliance path emits, without exception. We
#: organise evidence against legal text; we do not conclude that a platform
#: complies. That conclusion belongs to the ministry's lawyer, and a tool that
#: blurs the line would be selling a legal opinion it is not qualified to give.
NO_ES_OPINION_JURIDICA = (
    "Esto es evidencia organizada contra el texto legal, NO una opinión jurídica. "
    "Quien decide si hay cumplimiento es el asesor legal del ministerio; acá sólo "
    "consta qué se observó, contra qué artículo, y qué no se pudo observar."
)


@dataclass(frozen=True)
class CriterionSource:
    """Where a criterion came from. Every criterion has exactly one."""

    type: str
    framework: str
    original_id: str
    location: str
    #: Legal locators. Empty for everything that is not a statute. They are
    #: separate fields rather than a formatted string because a compliance
    #: finding is grouped, filtered and argued BY INSTRUMENT and BY ARTICLE, and
    #: a lawyer checking the citation needs the article, not our prose about it.
    instrument: str = ""
    article: str = ""
    jurisdiction: str = ""

    @property
    def is_sourced(self) -> bool:
        """True when an institution, not this tool, authored the criterion."""
        return self.type in (SOURCE_BANK, SOURCE_DOCUMENT, SOURCE_STATUTE)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "framework": self.framework,
            "original_id": self.original_id,
            "location": self.location,
            "instrument": self.instrument,
            "article": self.article,
            "jurisdiction": self.jurisdiction,
        }

    def describe(self) -> str:
        """One human line for the YAML comment above the criterion."""
        if self.type == SOURCE_GENERATED:
            return f"{UNSOURCED_MARK} - generado para un vacío: {self.location}"
        if self.type == SOURCE_STATUTE:
            return (
                f"norma: {self.instrument or self.framework} / "
                f"{self.article or self.original_id} ({self.location})"
                f" - jurisdicción: {self.jurisdiction or 'sin declarar'}"
            )
        origen = "documento" if self.type == SOURCE_DOCUMENT else "banco"
        return f"{origen}: {self.framework} / {self.original_id} ({self.location})"


# --- the bank ---------------------------------------------------------------

DEFAULT_BANK_PATH = Path(__file__).parent / "banks" / "lxp_sv_v7_4.yaml"

REQUIRED_ENTRY_FIELDS = (
    "id", "area", "name", "points", "verification", "pass_criteria", "fail_criteria", "source",
)
NON_EMPTY_ENTRY_FIELDS = (
    "id", "area", "name", "points", "verification", "pass_criteria", "fail_criteria",
)
REQUIRED_SOURCE_FIELDS = ("type", "framework", "original_id", "location")
REQUIRED_AREA_FIELDS = ("id", "name")


@dataclass(frozen=True)
class BankArea:
    id: str
    name: str
    evidence_base: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "evidence_base": self.evidence_base}


@dataclass(frozen=True)
class BankEntry:
    id: str
    area: str
    name: str
    points: float
    verification: str
    pass_criteria: str
    fail_criteria: str
    source: CriterionSource
    severity: str = ""
    evidence_base: str = ""
    #: What KIND of evidence settles this criterion. A property of the criterion,
    #: DECIDED WHEN IT IS WRITTEN, not discovered at runtime by looking at some
    #: particular product's UI.
    observability: str = OBSERVABLE_EN_PLATAFORMA
    #: For a non-observable entry: exactly what to ask the vendor for.
    evidencia_requerida: str = ""
    #: False when the bank did not say. An UNDECLARED entry is put to the
    #: classifier; a declared one is an authoring decision and is left alone.
    observability_declared: bool = False

    def haystack(self) -> str:
        """Everything a lexical match should be able to see."""
        return " ".join([
            self.name, self.verification, self.pass_criteria,
            self.fail_criteria, self.evidence_base,
        ])


@dataclass(frozen=True)
class CriteriaBank:
    bank_id: str
    version: str
    frameworks: tuple[dict, ...]
    areas: tuple[BankArea, ...]
    entries: tuple[BankEntry, ...]
    path: Path | None = None

    def entry(self, entry_id: str) -> BankEntry:
        for e in self.entries:
            if e.id == entry_id:
                return e
        raise FrameworkError(
            f'el criterio "{entry_id}" no está en el banco '
            f"{self.bank_id} v{self.version} - un criterio sin respaldo no puede "
            "entrar por la puerta de la selección"
        )

    def has(self, entry_id: str) -> bool:
        return any(e.id == entry_id for e in self.entries)

    def offered_areas(self) -> tuple[BankArea, ...]:
        """The areas the product offers to check. DERIVED FROM THE BANK.

        Not a hardcoded list: swap the bank and the checkboxes change with it,
        which is the whole "it works with another rubric" claim.
        """
        return self.areas

    def area(self, area_id: str) -> BankArea:
        for a in self.areas:
            if a.id == area_id:
                return a
        offered = ", ".join(a.id for a in self.areas)
        raise FrameworkError(
            f'el área "{area_id}" no existe en el banco {self.bank_id} '
            f"- las áreas disponibles son: {offered}"
        )

    def knows_area(self, area_id: str) -> bool:
        """Whether the bank has anything to say about this area.

        `area()` still RAISES for an unknown id -- asking the bank for an area it
        does not have is a bank-level error and stays one. This is the question
        the framework builder asks instead, because an area the bank never heard
        of is no longer disqualifying up there.
        """
        return any(a.id == area_id for a in self.areas)

    def entries_in(self, area_id: str) -> list[BankEntry]:
        return [e for e in self.entries if e.area == area_id]

    def shortlist(
        self, objective: str, *, areas: Sequence[str] | None = None, limit: int | None = None
    ) -> list[BankEntry]:
        """Candidate entries for an objective, best first.

        Deterministic and offline: an area filter, then a lexical overlap score
        against the objective. This only ORDERS and TRIMS candidates -- the
        model still chooses from what survives, and an entry the score misses is
        a missed candidate, never an invented one. Ties keep bank order so the
        result is stable across runs.
        """
        if areas is not None:
            for a in areas:
                self.area(a)  # raises, naming the offered areas
            pool = [e for e in self.entries if e.area in set(areas)]
        else:
            pool = list(self.entries)

        words = _words(objective)
        if words:
            scored = sorted(
                enumerate(pool),
                key=lambda pair: (-_overlap(words, pair[1].haystack()), pair[0]),
            )
            pool = [e for _, e in scored]
        return pool[:limit] if limit else pool


def _area_or_new(bank: CriteriaBank, area_id: str) -> BankArea:
    """The bank's area, or a fresh one the bank has never heard of.

    The inversion, in one function. The bank used to be the ceiling on what
    could be evaluated; it is now corroboration. An objective that calls for an
    area no instrument we hold covers still gets that area -- its criteria come
    out `generated` and marked SIN RESPALDO, which is visible and arguable,
    instead of being dropped, which is neither.
    """
    try:
        return bank.area(area_id)
    except FrameworkError:
        return BankArea(id=area_id, name=area_id, evidence_base="")


def _fold(text: str) -> str:
    """Lowercase, accent-stripped. `matemática` and `MATEMATICA` are one word."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


#: Spanish function words carry no topical signal and would make every entry
#: look equally relevant to every objective.
_STOP = frozenset(
    "de la el los las un una unos unas y o en para por con que se su sus del al a "
    "es son ser como mas mas no si lo le les este esta estos estas".split()
)


def _words(text: str) -> set[str]:
    return {
        w for w in "".join(c if c.isalnum() else " " for c in _fold(text)).split()
        if len(w) > 2 and w not in _STOP
    }


def _overlap(words: set[str], text: str) -> int:
    return len(words & _words(text))


def load_bank(path: Path | str | None = None) -> CriteriaBank:
    """Load a criteria bank, insisting every entry names where it came from."""
    path = Path(path) if path is not None else DEFAULT_BANK_PATH
    raw = load_yaml_with_lines(path, error_cls=FrameworkError)

    if not isinstance(raw, dict) or "entries" not in raw:
        raise FrameworkError(
            "el banco debe ser un mapa con una clave 'entries'", path=path, line=1
        )

    areas: list[BankArea] = []
    seen_areas: dict[str, int] = {}
    for i, a in enumerate(raw.get("areas") or []):
        aline = line_of(a)
        alabel = label(a, "id", i)
        missing = [k for k in REQUIRED_AREA_FIELDS if k not in a]
        if missing:
            raise FrameworkError(missing_fields("área", alabel, missing), path=path, line=aline)
        blank = [k for k in REQUIRED_AREA_FIELDS if is_blank(a[k])]
        if blank:
            raise FrameworkError(blank_fields("área", alabel, blank), path=path, line=aline)
        aid = require_quoted(a, "id", noun="el id de área", path=path, error_cls=FrameworkError)
        if aid in seen_areas:
            raise FrameworkError(
                f"el id de área {alabel} está repetido "
                f"(ya aparece en la línea {seen_areas[aid]})",
                path=path, line=aline,
            )
        seen_areas[aid] = aline
        areas.append(BankArea(id=aid, name=a["name"], evidence_base=str(a.get("evidence_base") or "")))

    if not areas:
        raise FrameworkError(
            "el banco no declara ningún área - sin áreas no hay nada que ofrecer "
            "para tildar, y la lista de áreas sale del banco",
            path=path, line=1,
        )

    entries: list[BankEntry] = []
    seen_ids: dict[str, int] = {}
    for i, e in enumerate(raw["entries"]):
        eline = line_of(e)
        elabel = label(e, "id", i)

        missing = [k for k in REQUIRED_ENTRY_FIELDS if k not in e]
        if missing:
            raise FrameworkError(missing_fields("criterio", elabel, missing), path=path, line=eline)
        blank = [k for k in NON_EMPTY_ENTRY_FIELDS if is_blank(e[k])]
        if blank:
            raise FrameworkError(blank_fields("criterio", elabel, blank), path=path, line=eline)

        eid = require_quoted(e, "id", noun="el id", path=path, error_cls=FrameworkError)
        if eid in seen_ids:
            raise FrameworkError(
                f"el id de criterio {elabel} está repetido "
                f"(ya aparece en la línea {seen_ids[eid]})",
                path=path, line=eline,
            )
        seen_ids[eid] = eline

        area_id = require_quoted(e, "area", noun="el área", path=path, error_cls=FrameworkError)
        if area_id not in seen_areas:
            offered = ", ".join(a.id for a in areas)
            raise FrameworkError(
                f'el criterio {elabel} dice pertenecer al área "{area_id}", '
                f"que el banco no declara - las áreas son: {offered}",
                path=path, line=eline,
            )

        try:
            points = float(e["points"])
        except (TypeError, ValueError) as exc:
            raise FrameworkError(
                f'el campo "points" del criterio {elabel} debe ser un número, '
                f"no {raw_text(e, 'points', e['points'])!r}",
                path=path, line=eline,
            ) from exc

        declared = "observability" in e
        obs = str(e.get("observability") or OBSERVABLE_EN_PLATAFORMA)
        if obs not in OBSERVABILITY_CLASSES:
            legales = ", ".join(OBSERVABILITY_CLASSES)
            raise FrameworkError(
                f'el criterio {elabel} declara una observabilidad desconocida '
                f'("{obs}") - las únicas válidas son: {legales}',
                path=path, line=eline,
            )
        evidencia = str(e.get("evidencia_requerida") or "").strip()
        if obs in NON_OBSERVABLE_CLASSES and not evidencia:
            raise FrameworkError(
                f'el criterio {elabel} es "{obs}" pero no dice qué evidencia lo '
                'zanjaría: falta "evidencia_requerida"',
                path=path, line=eline,
                hint=(
                    "(un criterio que no se puede mirar en la plataforma sólo sirve si "
                    "convierte 'no lo pudimos verificar' en un pedido concreto al proveedor)"
                ),
            )

        entries.append(BankEntry(
            id=eid,
            area=area_id,
            observability=obs,
            evidencia_requerida=evidencia,
            observability_declared=declared,
            name=e["name"],
            points=points,
            verification=e["verification"],
            pass_criteria=e["pass_criteria"],
            fail_criteria=e["fail_criteria"],
            source=_source_from_yaml(e["source"], elabel, path, eline),
            severity=str(e.get("severity") or ""),
            evidence_base=str(e.get("evidence_base") or ""),
        ))

    if not entries:
        raise FrameworkError(
            "el banco no tiene ningún criterio - no habría de dónde seleccionar",
            path=path, line=1,
        )

    return CriteriaBank(
        bank_id=str(raw.get("bank_id") or path.stem),
        version=str(raw.get("version") or "sin versión"),
        frameworks=tuple(
            {k: v for k, v in f.items() if not k.startswith("__")}
            for f in (raw.get("frameworks") or [])
        ),
        areas=tuple(areas),
        entries=tuple(entries),
        path=path,
    )


def _source_from_yaml(node: Any, elabel: str, path: Path, line: int) -> CriterionSource:
    if not isinstance(node, dict):
        raise FrameworkError(
            f'el campo "source" del criterio {elabel} debe ser un mapa '
            "(type, framework, original_id, location)",
            path=path, line=line,
        )
    missing = [k for k in REQUIRED_SOURCE_FIELDS if k not in node or is_blank(node[k])]
    if missing:
        campos = ", ".join(missing)
        raise FrameworkError(
            f'a la procedencia ("source") del criterio {elabel} le falta: {campos} '
            "- un criterio sin procedencia no es defendible y no entra al banco",
            path=path, line=line,
        )
    return CriterionSource(
        type=str(node["type"]),
        framework=str(node["framework"]),
        original_id=str(node["original_id"]),
        location=str(node["location"]),
    )


# --- the criterion the builder emits ----------------------------------------


@dataclass(frozen=True)
class FrameworkCriterion:
    """One criterion in the built framework, with its provenance attached.

    `original` holds the pre-adaptation text of a bank entry, so a reviewer can
    diff what we said against what the expert wrote. It is None for criteria
    that were never adapted (document-sourced and generated ones).
    """

    id: str
    area: str
    name: str
    points: float
    verification: str
    pass_criteria: str
    fail_criteria: str
    source: CriterionSource | None
    original: dict | None = None
    rationale: str = ""
    #: Which of the four evidence classes settles this criterion. See the
    #: OBSERVABILITY block above for why this exists and what it fixes.
    observability: str = OBSERVABLE_EN_PLATAFORMA
    #: For a non-observable criterion: exactly what would settle it. Required,
    #: because the whole value of carrying an uncheckable criterion is that it
    #: becomes a request the ministry can send to the vendor.
    evidencia_requerida: str = ""
    #: Why it cannot be settled on the platform, in the classifier's words.
    observability_reason: str = ""
    #: True when an author (bank YAML, extraction, generation) named the class.
    observability_declared: bool = False

    def __post_init__(self) -> None:
        # The invariant, enforced at construction rather than at review time:
        # there is no code path anywhere that produces a criterion whose origin
        # nobody can name.
        if self.source is None:
            raise FrameworkError(
                f'el criterio "{self.id}" no tiene procedencia '
                "- ningún criterio puede emitirse sin decir de dónde salió"
            )
        if self.source.type not in SOURCE_TYPES:
            legales = ", ".join(SOURCE_TYPES)
            raise FrameworkError(
                f'el criterio "{self.id}" declara una procedencia desconocida '
                f'("{self.source.type}") - las únicas válidas son: {legales}'
            )
        if self.observability not in ALL_OBSERVABILITY_CLASSES:
            legales = ", ".join(ALL_OBSERVABILITY_CLASSES)
            raise FrameworkError(
                f'el criterio "{self.id}" declara una observabilidad desconocida '
                f'("{self.observability}") - las únicas válidas son: {legales}'
            )
        if self.observability in NON_OBSERVABLE_CLASSES and not self.evidencia_requerida.strip():
            raise FrameworkError(
                f'el criterio "{self.id}" es "{self.observability}" pero no dice qué '
                'evidencia lo zanjaría: falta "evidencia_requerida"',
                hint=(
                    "(sin eso el criterio sólo informa que no lo miramos; con eso es un "
                    "pedido concreto que el ministerio le puede mandar al proveedor)"
                ),
            )

    @property
    def is_sourced(self) -> bool:
        return self.source is not None and self.source.is_sourced

    @property
    def is_observable(self) -> bool:
        """True when a walkthrough can settle it -- and ONLY then does it score."""
        return self.observability == OBSERVABLE_EN_PLATAFORMA

    @property
    def is_legal(self) -> bool:
        """True when the criterion is a requirement lifted from statute."""
        return self.source is not None and self.source.type == SOURCE_STATUTE

    @property
    def is_undecidable(self) -> bool:
        """No evaluation settles this. It is not a request; it is a referral."""
        return self.observability == NO_DECIDIBLE_EN_EVALUACION

    def verdict_floor(self) -> str:
        """The verdict this criterion can reach at best without more evidence.

        Observable criteria have no floor (None of the four verdicts is ruled
        out). Everything else is NO_EVALUADO before a single probe runs, which
        is why it never reaches the probe generator at all.
        """
        return "" if self.is_observable else NO_EVALUADO

    def no_evaluado_reason(self) -> str:
        """One line a ministry can act on, or "" for an observable criterion."""
        if self.is_observable:
            return ""
        why = self.observability_reason.strip()
        if self.is_undecidable:
            # Deliberately NOT phrased as "evidencia requerida". Nothing the
            # vendor sends settles this, and a ministry that reads it as a
            # document request will chase a document that does not exist and
            # then believe the matter is closed when it arrives.
            return (
                f"{OBSERVABILITY_LABEL[NO_DECIDIBLE_EN_EVALUACION]}. "
                f"Cuestión a resolver por un asesor legal: "
                f"{self.evidencia_requerida.strip()}"
                + (f" ({why})" if why else "")
            )
        return (
            f"{OBSERVABILITY_LABEL[self.observability]}. "
            f"Evidencia requerida: {self.evidencia_requerida.strip()}"
            + (f" ({why})" if why else "")
        )

    def to_test(self) -> CriterionTest:
        return CriterionTest(
            id=self.id,
            name=self.name,
            points=self.points,
            verification=self.verification,
            pass_criteria=self.pass_criteria,
            fail_criteria=self.fail_criteria,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "area": self.area,
            "name": self.name,
            "points": self.points,
            "verification": self.verification,
            "pass_criteria": self.pass_criteria,
            "fail_criteria": self.fail_criteria,
            "source": self.source.to_dict() if self.source else None,
            "original": self.original,
            "rationale": self.rationale,
            "observability": self.observability,
            "evidencia_requerida": self.evidencia_requerida,
            "observability_reason": self.observability_reason,
            "observability_declared": self.observability_declared,
            "status": "" if self.is_observable else NO_EVALUADO,
        }


# --- coverage ---------------------------------------------------------------

STATUS_OK = "cubierta"
STATUS_THIN = "delgada"
STATUS_GENERATED_ONLY = "sin_respaldo"
STATUS_UNCOVERED = "sin_cobertura"

#: Below this many SOURCED criteria, a requested area is reported as thin. One
#: criterion is a spot check, not a measured dimension, and a report that reads
#: as if it were is the same lie as `0/0 -> 0%`.
THIN_BELOW = 2

_STATUS_NOTE = {
    STATUS_OK: "el banco cubre esta área con criterios de instrumentos existentes",
    STATUS_THIN: (
        "cobertura delgada: hay menos de {thin} criterios con respaldo. "
        "Alcanza para una señal, NO para afirmar que el área fue evaluada"
    ),
    STATUS_GENERATED_ONLY: (
        "esta área quedó cubierta SÓLO por criterios generados por la herramienta, "
        "sin respaldo institucional. Revisala antes de publicar cualquier puntaje"
    ),
    STATUS_UNCOVERED: (
        "el área fue pedida y quedó SIN NINGÚN criterio. No se rellenó sola: "
        "un puntaje de esta área no existe y no debe leerse como cero"
    ),
}


@dataclass(frozen=True)
class AreaCoverage:
    """One area on TWO axes.

    Axis 1 (provenance): how much of this area came from a published instrument,
    from an uploaded document, or from the tool itself. That is the
    defensibility axis and it is unchanged.

    Axis 2 (evidence kind): how much of this area a walkthrough can actually
    settle, versus how much needs a document, data, or a study. This is the axis
    the ministry reads: *"of the 14 things you care about, 9 we can check by
    walking the platform, 3 need documents from the vendor, 2 need an impact
    study."* Without it, an area full of REQUIERE_ESTUDIO criteria reads exactly
    like an area we walked end to end -- which is the audit's `0/0 -> 0%` bug
    wearing a different hat.
    """

    area: str
    name: str
    status: str
    bank_criteria: int
    document_criteria: int
    generated_criteria: int
    bank_available: int
    note: str = ""
    observable: int = 0
    requires_documentation: int = 0
    requires_data: int = 0
    requires_study: int = 0
    #: Requirements no evaluation settles. Counted separately from the three
    #: evidence classes because they are not a request anyone can fulfil.
    not_decidable: int = 0
    in_bank: bool = True
    statute_criteria: int = 0

    @property
    def sourced_criteria(self) -> int:
        return self.bank_criteria + self.document_criteria + self.statute_criteria

    @property
    def total_criteria(self) -> int:
        return self.sourced_criteria + self.generated_criteria

    def class_counts(self) -> dict[str, int]:
        """Every outcome, keyed by class. THE accessor for anything that counts.

        Derived over `ALL_OBSERVABILITY_CLASSES` rather than by adding up named
        fields, so that adding a class and forgetting to wire it here raises at
        `_coverage`'s invariant instead of quietly shrinking a total. A
        requirement that vanishes from a count is worse than one reported as
        unverifiable: the ministry never learns it was asked.
        """
        return {
            OBSERVABLE_EN_PLATAFORMA: self.observable,
            REQUIERE_DOCUMENTACION: self.requires_documentation,
            REQUIERE_DATOS: self.requires_data,
            REQUIERE_ESTUDIO: self.requires_study,
            NO_DECIDIBLE_EN_EVALUACION: self.not_decidable,
        }

    @property
    def not_evaluated(self) -> int:
        return sum(self.class_counts()[k] for k in NON_OBSERVABLE_CLASSES)

    @property
    def answers_nothing(self) -> bool:
        """The area has criteria, and this run can settle NONE of them.

        Worth its own flag: an area like this renders, in every other view,
        exactly like an area we walked end to end and found compliant. That is
        the audit's `0/0 -> 0%` bug one level up, and a ministry must see it
        before it signs anything on the strength of this report.
        """
        return bool(self.total_criteria) and self.observable == 0

    def evidence_note(self) -> str:
        """The area's second axis in one Spanish line."""
        if not self.total_criteria:
            return "sin criterios"
        n = self.total_criteria
        counts = self.class_counts()
        parts = [f"{self.observable} en la plataforma"]
        for klass in NON_OBSERVABLE_CLASSES:
            if counts[klass]:
                parts.append(f"{counts[klass]} {OBSERVABILITY_LABEL[klass]}")
        note = f"de {n} criterio{'s' if n != 1 else ''}: " + ", ".join(parts)
        if self.answers_nothing:
            note += (
                " -- ATENCIÓN: esta corrida NO contesta nada sobre esta área; "
                "todo lo que el objetivo pide acá depende de evidencia que hay que pedir"
            )
        return note

    def to_dict(self) -> dict:
        return {
            "area": self.area,
            "name": self.name,
            "status": self.status,
            "bank_criteria": self.bank_criteria,
            "document_criteria": self.document_criteria,
            "generated_criteria": self.generated_criteria,
            "bank_available": self.bank_available,
            "note": self.note,
            "in_bank": self.in_bank,
            "statute_criteria": self.statute_criteria,
            "observable": self.observable,
            "requires_documentation": self.requires_documentation,
            "requires_data": self.requires_data,
            "requires_study": self.requires_study,
            "not_decidable": self.not_decidable,
            "class_counts": self.class_counts(),
            "not_evaluated": self.not_evaluated,
            "answers_nothing": self.answers_nothing,
            "evidence_note": self.evidence_note(),
        }


@dataclass(frozen=True)
class CoverageReport:
    """Which requested areas the bank actually carries, and which it does not.

    The reason this is a first-class return value and not a log line: the audit
    found the report rendering an UNMEASURABLE domain (`possible == 0`) exactly
    like one that scored zero, so the vendor that HID a surface read better than
    the one that exposed it. Silence about coverage is not neutral -- it reads
    as coverage. So every requested area appears here, including the ones that
    came back empty.
    """

    areas: tuple[AreaCoverage, ...]
    requested: tuple[str, ...]
    gaps: tuple[str, ...] = ()

    def status_of(self, area_id: str) -> str:
        for a in self.areas:
            if a.area == area_id:
                return a.status
        raise FrameworkError(f'el área "{area_id}" no figura en el reporte de cobertura')

    def _with(self, *statuses: str) -> list[AreaCoverage]:
        return [a for a in self.areas if a.status in statuses]

    @property
    def well_covered(self) -> list[AreaCoverage]:
        return self._with(STATUS_OK)

    @property
    def thin(self) -> list[AreaCoverage]:
        return self._with(STATUS_THIN)

    @property
    def unsourced(self) -> list[AreaCoverage]:
        return self._with(STATUS_GENERATED_ONLY)

    @property
    def uncovered(self) -> list[AreaCoverage]:
        return self._with(STATUS_UNCOVERED)

    @property
    def answers_nothing(self) -> list[AreaCoverage]:
        """Requested areas this run cannot say a single thing about."""
        return [a for a in self.areas if a.answers_nothing]

    @property
    def has_under_coverage(self) -> bool:
        return bool(self._with(STATUS_THIN, STATUS_GENERATED_ONLY, STATUS_UNCOVERED))

    def evidence_totals(self) -> dict[str, int]:
        """How many criteria of each outcome the whole framework holds.

        Derived by iterating `ALL_OBSERVABILITY_CLASSES` over each area's
        `class_counts()`, never by naming fields: a class that exists but is not
        summed here would make criteria vanish from the ministry's headline
        sentence while still sitting in the framework.
        """
        return {
            klass: sum(a.class_counts()[klass] for a in self.areas)
            for klass in ALL_OBSERVABILITY_CLASSES
        }

    def summary_line(self) -> str:
        """The sentence the ministry actually reads, in one line.

        This framing IS the deliverable of the two-axis report: a reader must be
        able to see at a glance how much of what they care about a walkthrough
        can settle, and what to ask the vendor for to settle the rest.
        """
        totals = self.evidence_totals()
        total = sum(totals.values())
        if not total:
            return "El marco no tiene ningún criterio."
        parts = [
            f"{totals[OBSERVABLE_EN_PLATAFORMA]} "
            f"{OBSERVABILITY_LABEL[OBSERVABLE_EN_PLATAFORMA]}"
        ]
        for klass in NON_OBSERVABLE_CLASSES:
            if totals[klass]:
                parts.append(f"{totals[klass]} {OBSERVABILITY_LABEL[klass]}")
        return (
            f"De los {total} criterios que exige el objetivo: " + "; ".join(parts) + "."
        )

    def to_dict(self) -> dict:
        return {
            "requested": list(self.requested),
            "areas": [a.to_dict() for a in self.areas],
            "gaps": list(self.gaps),
            "has_under_coverage": self.has_under_coverage,
            "evidence_totals": self.evidence_totals(),
            "summary_line": self.summary_line(),
        }


def _coverage(
    bank: CriteriaBank,
    requested: Sequence[str],
    criteria: Sequence[FrameworkCriterion],
    gaps: Sequence[str],
) -> CoverageReport:
    rows: list[AreaCoverage] = []
    # every REQUESTED area, plus any area a criterion actually landed in, so an
    # area nobody asked for but that got criteria is still accounted for
    area_ids = list(dict.fromkeys([*requested, *(c.area for c in criteria)]))
    for area_id in area_ids:
        mine = [c for c in criteria if c.area == area_id]
        bank_n = sum(1 for c in mine if c.source.type == SOURCE_BANK)
        doc_n = sum(1 for c in mine if c.source.type == SOURCE_DOCUMENT)
        gen_n = sum(1 for c in mine if c.source.type == SOURCE_GENERATED)
        law_n = sum(1 for c in mine if c.source.type == SOURCE_STATUTE)
        sourced = bank_n + doc_n + law_n

        if not mine:
            status = STATUS_UNCOVERED
        elif sourced == 0:
            status = STATUS_GENERATED_ONLY
        elif sourced < THIN_BELOW:
            status = STATUS_THIN
        else:
            status = STATUS_OK

        try:
            name = bank.area(area_id).name
            in_bank = True
        except FrameworkError:
            # An area the bank has never heard of is NOT an error any more. The
            # bank's areas are suggestions; the objective can call for more.
            name = area_id
            in_bank = False

        row = AreaCoverage(
            area=area_id,
            name=name,
            status=status,
            bank_criteria=bank_n,
            document_criteria=doc_n,
            generated_criteria=gen_n,
            statute_criteria=law_n,
            bank_available=len(bank.entries_in(area_id)),
            note=_STATUS_NOTE[status].format(thin=THIN_BELOW),
            in_bank=in_bank,
            observable=sum(1 for c in mine if c.observability == OBSERVABLE_EN_PLATAFORMA),
            requires_documentation=sum(
                1 for c in mine if c.observability == REQUIERE_DOCUMENTACION),
            requires_data=sum(1 for c in mine if c.observability == REQUIERE_DATOS),
            requires_study=sum(1 for c in mine if c.observability == REQUIERE_ESTUDIO),
            not_decidable=sum(
                1 for c in mine if c.observability == NO_DECIDIBLE_EN_EVALUACION),
        )
        _assert_nothing_dropped(row, len(mine))
        rows.append(row)
    return CoverageReport(areas=tuple(rows), requested=tuple(requested), gaps=tuple(gaps))


def _assert_nothing_dropped(row: AreaCoverage, seen: int) -> None:
    """Every criterion in the area is counted under exactly one outcome.

    THE guard against this feature's worst failure, and it is worth stating why
    it is an exception and not a log line. A requirement that is reported as
    "we could not verify this" is a line a ministry acts on. A requirement that
    silently falls out of the totals is a legal duty nobody ever learns was
    asked about -- and the report still reads complete, because the arithmetic
    still adds up against a smaller denominator. That is the audit's `0/0 -> 0%`
    bug in its most expensive form: a procurement decision made on a compliance
    summary with a hole in it that no reader can see.

    It fires when a new outcome class is added without wiring it into both the
    per-area fields and `class_counts()`. There is no recovery path on purpose.
    """
    counted = sum(row.class_counts().values())
    if counted == seen:
        return
    sourced_total = row.total_criteria
    raise FrameworkError(
        f'el área "{row.area}" tiene {seen} criterios pero sólo {counted} quedaron '
        f"contados por clase de evidencia (procedencia cuenta {sourced_total})",
        hint=(
            "(esto significa que un criterio existe en el marco y NO aparece en "
            "ningún total: un requisito que se pierde en silencio es peor que uno "
            "reportado como no verificable, porque nadie se entera de que se pidió. "
            "Suele pasar al agregar una clase de observabilidad sin sumarla en "
            "AreaCoverage.class_counts)"
        ),
    )


# --- documents --------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    """An uploaded document, as text. `name` is what provenance will cite."""

    name: str
    text: str

    def locate(self, quote: str) -> int | None:
        """1-based line where `quote` appears, or None if it does not.

        Whitespace-folded on both sides: a model re-wrapping a quoted line is a
        formatting difference, not a fabrication. Anything beyond that IS one.
        """
        needle = _squeeze(quote)
        if not needle:
            return None
        for i, line in enumerate(self.text.splitlines(), start=1):
            if needle in _squeeze(line):
                return i
        # the quote may legitimately span lines
        if needle in _squeeze(self.text):
            return 1
        return None


def _squeeze(text: str) -> str:
    return " ".join(_fold(text).split())


@dataclass(frozen=True)
class Statute(Document):
    """The law of the country the platform will operate in, as text.

    A `Document` subclass on purpose: quote verification is the same discipline
    and must not fork into a second implementation that drifts laxer. What it
    adds is the JURISDICTION, which is required and not defaulted.

    Required because a requirement list built for one country is silently wrong
    in another, and silence is the dangerous part. That bug was just found live
    one layer down: the risk vocabulary carried Chile's `línea 113` while the
    rubric under evaluation was Salvadoran, and nothing in the artefact said so.
    A statute that cannot name whose law it is does not get ingested.
    """

    jurisdiction: str = ""

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise FrameworkError(
                "una norma subida no tiene nombre - una cita sin instrumento no se "
                "puede verificar contra nada"
            )
        if not str(self.jurisdiction or "").strip():
            raise FrameworkError(
                f'la norma "{self.name}" no declara jurisdicción',
                hint=(
                    "(una lista de requisitos armada para un país es silenciosamente "
                    "incorrecta en otro; sin jurisdicción declarada no hay forma de "
                    "que el informe avise)"
                ),
            )
        if not str(self.text or "").strip():
            raise FrameworkError(
                f'la norma "{self.name}" vino vacía - no hay texto contra el cual '
                "verificar ninguna cita"
            )


# --- the forced tools -------------------------------------------------------

_SELECT_TOOL_NAME = "seleccionar_criterios"
_ADAPT_TOOL_NAME = "adaptar_criterio"
_GENERATE_TOOL_NAME = "emitir_criterios_nuevos"
_EXTRACT_TOOL_NAME = "extraer_criterios_del_documento"
_REVISE_TOOL_NAME = "revisar_framework"
_CLASSIFY_TOOL_NAME = "clasificar_observabilidad"

#: The observability fields every criterion-minting tool must carry. Declaring
#: the class AT WRITING TIME is the whole design: it is a property of the
#: criterion, not a discovery about some product's UI.
_OBSERVABILITY_PROPS: dict[str, Any] = {
    "observability": {
        "type": "string",
        "enum": list(OBSERVABILITY_CLASSES),
        "description": (
            "Qué CLASE de evidencia zanja este criterio. "
            "OBSERVABLE_EN_PLATAFORMA: alcanza con recorrer la plataforma. "
            "REQUIERE_DOCUMENTACION: hace falta un documento del proveedor (DPA, "
            "política de seguridad, declaración de alineación curricular). "
            "REQUIERE_DATOS: hace falta acceso a datos (registros de uso, telemetría, "
            "una exportación de administrador). "
            "REQUIERE_ESTUDIO: hace falta un diseño de investigación (¿mejora el "
            "aprendizaje?). "
            "NO castigues al criterio por no ser observable: si importa para el "
            "objetivo, va igual, y se reporta NO_EVALUADO con lo que haría falta."
        ),
    },
    "evidencia_requerida": {
        "type": "string",
        "description": (
            "Si NO es observable: QUÉ exactamente habría que pedir o hacer para "
            "zanjarlo, en una frase accionable ('el DPA firmado', 'la exportación de "
            "uso por alumno de un trimestre', 'un ensayo con asignación aleatoria por "
            "escuela'). Vacío si es observable."
        ),
    },
}

#: The five fields any criterion must carry to be measurable at all. Shared so
#: the three tools that mint criteria cannot drift into different shapes.
_CRITERION_PROPS: dict[str, Any] = {
    "name": {"type": "string", "description": "El criterio en una frase, en español."},
    "points": {"type": "number", "description": "Peso del criterio dentro del framework."},
    "verification": {
        "type": "string",
        "description": "Cómo se verifica MIRANDO la plataforma. Una acción, no un juicio.",
    },
    "pass_criteria": {"type": "string", "description": "Qué se observa exactamente para CUMPLE."},
    "fail_criteria": {"type": "string", "description": "Qué se observa exactamente para NO_CUMPLE."},
}


def _select_tool(candidates: Sequence[BankEntry]) -> dict:
    """Selection tool whose `id` enum IS the candidate list.

    This is the structural lock on invention: a criterion outside the bank is
    not expressible in a valid tool call. `_selected_entries` re-checks anyway --
    a schema is a request, and the enum being SENT is what a fake client can
    prove, not the enum being obeyed.
    """
    return {
        "name": _SELECT_TOOL_NAME,
        "description": "Elegí, del banco, los criterios que corresponden a este objetivo.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "selected": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "enum": [e.id for e in candidates]},
                            "rationale": {
                                "type": "string",
                                "description": "Por qué este criterio aplica a ESTE objetivo.",
                            },
                        },
                        "required": ["id", "rationale"],
                        "additionalProperties": False,
                    },
                },
                "gaps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Qué exige el objetivo que NINGÚN criterio del banco cubre. "
                        "Sé conservador: un vacío declarado habilita inventar criterios."
                    ),
                },
            },
            "required": ["selected", "gaps"],
            "additionalProperties": False,
        },
    }


ADAPT_TOOL: dict[str, Any] = {
    "name": _ADAPT_TOOL_NAME,
    "description": "Reescribí este criterio del banco para el contexto, sin cambiar qué mide.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            **_CRITERION_PROPS,
            "notes": {"type": "string", "description": "Qué adaptaste y por qué."},
        },
        # `id`, `area` and `source` are ABSENT on purpose: they are join keys and
        # provenance. We carry them over from the bank entry ourselves. A model
        # that can rewrite its own provenance has no provenance.
        "required": ["name", "points", "verification", "pass_criteria", "fail_criteria", "notes"],
        "additionalProperties": False,
    },
}


def _generate_tool(areas: Sequence[BankArea]) -> dict:
    return {
        "name": _GENERATE_TOOL_NAME,
        "description": "Emití criterios nuevos SÓLO para lo que el banco no cubre.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "area": {"type": "string", "enum": [a.id for a in areas]},
                            **_CRITERION_PROPS,
                            **_OBSERVABILITY_PROPS,
                            "why_not_in_bank": {
                                "type": "string",
                                "description": (
                                    "Por qué ningún criterio del banco sirve. Si alguno sirve, "
                                    "no emitas éste: un criterio inventado pesa menos ante un "
                                    "proveedor que discute el hallazgo."
                                ),
                            },
                        },
                        "required": [
                            "area", "name", "points", "verification",
                            "pass_criteria", "fail_criteria", "why_not_in_bank",
                            "observability", "evidencia_requerida",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        },
    }


def _extract_tool(areas: Sequence[BankArea]) -> dict:
    return {
        "name": _EXTRACT_TOOL_NAME,
        "description": "Extraé del documento los criterios evaluables que declara.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "area": {"type": "string", "enum": [a.id for a in areas]},
                            **_CRITERION_PROPS,
                            **_OBSERVABILITY_PROPS,
                            "quote": {
                                "type": "string",
                                "description": (
                                    "La frase TEXTUAL del documento que exige esto. "
                                    "Se verifica contra el documento: si no está, se rechaza."
                                ),
                            },
                        },
                        "required": [
                            "area", "name", "points", "verification",
                            "pass_criteria", "fail_criteria", "quote",
                            "observability", "evidencia_requerida",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        },
    }


#: Stands in for an empty enum. A `strict` tool schema with `enum: []` describes
#: a field no value can satisfy, and the API is entitled to reject the tool
#: outright -- which would turn "the framework already holds every criterion the
#: bank has" into a crash. The placeholder is filtered back out in
#: `_selected_entries`, so it can never be mistaken for a criterion id.
NOTHING_TO_PICK = "__ninguno__"


def _revise_tool(current_ids: Sequence[str], bank_ids: Sequence[str]) -> dict:
    """Revision stays bank-first: what can be ADDED is still an enum of bank ids."""
    return {
        "name": _REVISE_TOOL_NAME,
        "description": "Corregí el framework según las objeciones del consejo.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "drop": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(current_ids) or [NOTHING_TO_PICK]},
                    "description": "Ids del framework actual que hay que sacar.",
                },
                "add": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "enum": list(bank_ids) or [NOTHING_TO_PICK]},
                            "rationale": {"type": "string"},
                        },
                        "required": ["id", "rationale"],
                        "additionalProperties": False,
                    },
                    "description": "Criterios del BANCO que hay que sumar.",
                },
                "notes": {"type": "string", "description": "Qué cambiaste y por qué."},
            },
            "required": ["drop", "add", "notes"],
            "additionalProperties": False,
        },
    }


def _classify_tool(ids: Sequence[str]) -> dict:
    """One batched call that classifies every criterion nobody classified yet.

    The `id` enum is the undeclared list, so the classifier cannot invent a
    criterion or silently re-open one whose class an author already decided.
    """
    return {
        "name": _CLASSIFY_TOOL_NAME,
        "description": "Decidí qué CLASE de evidencia hace falta para zanjar cada criterio.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "criteria": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "enum": list(ids) or [NOTHING_TO_PICK]},
                            **_OBSERVABILITY_PROPS,
                            "reason": {
                                "type": "string",
                                "description": "Por qué esa clase, en una frase.",
                            },
                            "no_evaluable": {
                                "type": "boolean",
                                "description": (
                                    "true SÓLO si el criterio no se puede zanjar por NINGÚN "
                                    "medio: no afirma nada que pudiera resultar falso. Eso es "
                                    "incoherencia del criterio, no falta de acceso. Si con "
                                    "un documento, con datos o con un estudio se zanjaría, "
                                    "es false."
                                ),
                            },
                        },
                        "required": [
                            "id", "observability", "evidencia_requerida",
                            "reason", "no_evaluable",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["criteria"],
            "additionalProperties": False,
        },
    }


# --- prompts ----------------------------------------------------------------

SELECT_SYSTEM = (
    "Sos quien arma el marco de criterios de una evaluación de plataformas educativas, "
    "para un ministerio que va a decidir contrataciones con el resultado.\n"
    "NO inventás criterios. Elegís, de un banco de criterios ya escritos por expertos, "
    "los que corresponden al objetivo declarado. Esa es toda tu tarea.\n"
    "Por qué: cuando el proveedor que pierde pregunta de dónde salió un criterio y quién "
    "dice que importa, 'lo escribió un modelo' termina la conversación. Un criterio del "
    "banco viene con instrumento, id original y dónde mirarlo.\n"
    "Reglas:\n"
    "- Elegí lo que el objetivo realmente exige, no todo lo que existe. Un marco de 80 "
    "criterios para un objetivo chico no se puede ejecutar y no se ejecuta.\n"
    "- Si dos criterios del banco miden lo mismo, elegí UNO: duplicar un criterio "
    "duplica en silencio el peso de esa dimensión.\n"
    "- Declará un vacío SÓLO si de verdad ningún criterio del banco sirve. Declarar un "
    "vacío habilita inventar, e inventar debilita el marco entero.\n"
    "Respondés con la herramienta."
)

ADAPT_SYSTEM = (
    "Adaptás un criterio ya escrito por expertos al contexto concreto de esta evaluación "
    "(materia, grados, idioma, tipo de plataforma).\n"
    "Adaptar es REDACTAR MÁS PRECISO, no cambiar qué se mide:\n"
    "- podés fijar la materia y el rango de grados, y volver la verificación más concreta;\n"
    "- NO podés ablandar ni endurecer el umbral, ni cambiar la dimensión evaluada;\n"
    "- las anclas de CUMPLE y NO_CUMPLE tienen que quedar igual de observables o más: "
    "qué exactamente se ve para cada una. Nada de adjetivos ('adecuado', 'apropiado');\n"
    "- describí QUÉ tiene que lograr la plataforma, nunca CÓMO lo implementa un producto: "
    "otra arquitectura puede cumplir el mismo criterio de otra manera.\n"
    "NO INVENTES ALCANCE. Adaptar es aclarar y volver contable lo que el experto escribió; "
    "no es agregar parámetros que el experto no escribió. Está PROHIBIDO introducir: "
    "tamaño de muestra ('en las 5 lecciones auditadas'), cantidad de pantallas, ventanas "
    "de tiempo, cantidad de sesiones, alumnos o intentos. Eso es una decisión metodológica "
    "de la corrida, y si de verdad hace falta va en la CONFIGURACIÓN DE LA CORRIDA, donde "
    "se ve, no adentro del texto del criterio, donde parece que lo escribió el experto y "
    "queda publicado bajo su procedencia.\n"
    "Los números que YA estaban en el original se conservan tal cual; los que no estaban, "
    "no se agregan.\n"
    "El texto original se conserva al lado del tuyo y alguien los va a comparar. "
    "Escribís en español rioplatense claro. Respondés con la herramienta."
)

GENERATE_SYSTEM = (
    "Emitís criterios NUEVOS, y sólo para lo que el banco de criterios no cubre.\n"
    "Estos criterios van a quedar marcados como SIN RESPALDO en el marco publicado, "
    "para que cualquiera vea de un vistazo cuáles tienen peso institucional y cuáles los "
    "inventó la herramienta. Emití pocos y buenos, o ninguno.\n"
    "NO descartes un criterio por no poder mirarse en la plataforma. Si el objetivo lo "
    "exige, va igual: declarás su observabilidad y qué evidencia lo zanjaría, y se reporta "
    "NO_EVALUADO en vez de puntuarse. Filtrar por observabilidad acá sería repetir el "
    "recorte del piloto, que es exactamente lo que esta herramienta corrige.\n"
    "Anclas concretas y contables para CUMPLE y NO_CUMPLE. Respondés con la herramienta."
)

EXTRACT_SYSTEM = (
    "Extraés de un documento subido (una política, una norma, un pliego) los criterios "
    "evaluables que ese documento EXIGE.\n"
    "La política de un ministerio pesa más que cualquier cosa que genere esta herramienta, "
    "y por eso mismo la cita se verifica: 'quote' tiene que ser una frase TEXTUAL del "
    "documento. Si citás algo que no está, se rechaza el criterio y se corta la corrida.\n"
    "Un principio general ('la educación debe ser inclusiva') no es un criterio; la "
    "obligación concreta que ese principio impone, sí.\n"
    "Extraé la obligación aunque NO se pueda verificar mirando la plataforma: declarás qué "
    "clase de evidencia la zanjaría (documento, datos, estudio) y qué habría que pedir. "
    "Una obligación de la política del ministerio que no reportamos es peor que una que "
    "reportamos como NO EVALUADA con el pedido concreto al lado.\n"
    "Respondés con la herramienta."
)

CLASSIFY_SYSTEM = (
    "Clasificás criterios de evaluación por la CLASE DE EVIDENCIA que hace falta para "
    "zanjarlos. No decidís si el criterio es bueno, ni si sobra, ni cuánto pesa.\n"
    "Esta es una pregunta SOBRE EL CRITERIO, no sobre una plataforma concreta: no ves la "
    "plataforma y no tenés que suponer cómo es su interfaz. Preguntate qué tipo de "
    "evidencia zanjaría la afirmación, no si tal producto tiene tal pantalla.\n"
    "NO estás filtrando. Un criterio que no se puede mirar en la plataforma NO se descarta: "
    "entra al marco igual, se reporta NO EVALUADO y se convierte en un pedido concreto al "
    "proveedor. Ese es el punto.\n"
    "Respondés con la herramienta."
)

REVISE_SYSTEM = (
    "Corregís un marco de criterios según las objeciones de un consejo de revisión.\n"
    "Corregís EXACTAMENTE lo objetado. Podés sacar criterios del marco y sumar criterios "
    "DEL BANCO; no podés inventar acá.\n"
    "Si una objeción te parece equivocada, no la apliques y decilo en las notas. "
    "Respondés con la herramienta."
)


# --- the council ------------------------------------------------------------

_SHARED = (
    "Estás revisando UN MARCO DE CRITERIOS completo: el conjunto de criterios con el que "
    "se va a evaluar una plataforma educativa, y con cuyo resultado un ministerio puede "
    "decidir una contratación.\n"
    "Juzgá SÓLO desde tu rol. Otro juez se ocupa de lo demás; no opines fuera de tu lente "
    "y no supongas qué habrán dicho los demás (no los ves, y es a propósito).\n"
    "Respondé siempre con la herramienta:\n"
    "- approve: el marco sirve como está desde tu lente.\n"
    "- revise: sirve, pero hay que corregir algo concreto (decí qué, y sobre qué criterio).\n"
    "- reject: no sirve desde tu lente ni con retoques.\n"
    "Los motivos van en español y citan el criterio (por id) que evaluaste."
)

FRAMEWORK_COUNCIL: tuple[Judge, ...] = (
    Judge(
        name="cobertura",
        responsibility="¿El conjunto cubre el objetivo declarado? ¿Qué falta?",
        system=(
            "[JUEZ: cobertura]\n"
            "Sos el juez de COBERTURA. Tu única pregunta: ¿este conjunto de criterios cubre "
            "el objetivo tal como fue declarado, o deja afuera algo que el objetivo exige?\n"
            f"{_SHARED}\n"
            "Reglas de tu rol:\n"
            "- Nombrá lo que FALTA, concretamente. 'Podría ser más completo' no es un motivo.\n"
            "- Mirá el reporte de cobertura que viene con el marco: un área pedida que quedó "
            "vacía, o cubierta por un solo criterio, o cubierta sólo por criterios inventados, "
            "es un hallazgo tuyo aunque el resto del marco esté bien.\n"
            "- Un criterio marcado NO_EVALUADO SÍ CUENTA como cobertura del objetivo: el "
            "marco lo pide y dice qué habría que conseguir. No lo trates como si faltara.\n"
            "- Al revés: si un área queda cubierta ENTERA por criterios NO_EVALUADO, decilo. "
            "Esta corrida no va a contestar nada sobre esa área, y quien lea el informe "
            "tiene que saberlo antes de firmar una contratación.\n"
            "- No te importa si sobran criterios, ni si están bien pesados, ni si se pueden "
            "observar: eso lo miran otros."
        ),
    ),
    Judge(
        name="no_solapamiento",
        responsibility="¿Hay dos criterios que miden lo mismo?",
        system=(
            "[JUEZ: no_solapamiento]\n"
            "Sos el juez de NO SOLAPAMIENTO. Tu única pregunta: ¿hay dos criterios que miden "
            "la misma cosa?\n"
            f"{_SHARED}\n"
            "Por qué importa: dos criterios que miden lo mismo DUPLICAN EN SILENCIO el peso "
            "de esa dimensión. Nadie lo decidió y nadie lo ve; el puntaje final queda inclinado "
            "hacia lo que se escribió dos veces.\n"
            "Reglas de tu rol:\n"
            "- Señalá pares concretos por id y decí qué comparten.\n"
            "- Que dos criterios pertenezcan a la misma área NO los hace duplicados; que una "
            "misma observación en la plataforma los resuelva a los dos, SÍ.\n"
            "- Cuidado con el solapamiento entre un criterio del banco y uno del documento "
            "subido: es el caso más frecuente y el más fácil de pasar por alto."
        ),
    ),
    Judge(
        name="medibilidad",
        responsibility=(
            "¿Cada criterio tiene anclas contables, y está en la clase de evidencia "
            "que le corresponde?"
        ),
        system=(
            "[JUEZ: medibilidad]\n"
            "Sos el juez de MEDIBILIDAD. Tu pregunta: ¿cada criterio tiene anclas "
            "concretas y contables, y está declarado en la clase de evidencia correcta?\n"
            f"{_SHARED}\n"
            "LO QUE NO HACÉS, y es importante: NO rechazás un criterio por no poder "
            "mirarse en la plataforma. Un criterio que necesita un documento del "
            "proveedor, acceso a datos o un ensayo controlado ES PARTE DEL MARCO: va "
            "declarado con su clase de evidencia y se reporta NO EVALUADO, con el pedido "
            "concreto al lado. Sacarlo por difícil de ver sería repetir el recorte del "
            "piloto, que es exactamente lo que este marco corrige. Si un criterio así "
            f"viene marcado {OBSERVABLE_EN_PLATAFORMA}, tu objeción es que está MAL "
            "CLASIFICADO -- decí en qué clase va --, no que sobre.\n"
            "Rechazá o mandá a revisar cuando un criterio:\n"
            "- no tenga ancla observable: falta el QUÉ exacto que se ve (o se lee, o se "
            "cuenta en los datos) para CUMPLE y el QUÉ exacto para NO_CUMPLE;\n"
            "- use fórmulas que no se pueden contradecir ('el tutor responde apropiadamente', "
            "'la experiencia es adecuada');\n"
            "- esté marcado observable pero en realidad exija documentación, datos o un "
            "estudio -- eso se termina puntuando con la impresión de quien evalúa;\n"
            "- no sea zanjable POR NINGÚN MEDIO: no afirma nada que pudiera resultar falso. "
            "Ese es el único caso en que un criterio de verdad sobra.\n"
            "Exigí anclas concretas y contables, no adjetivos."
        ),
    ),
    Judge(
        name="neutralidad",
        responsibility="¿El marco está inclinado hacia una arquitectura de producto?",
        system=(
            "[JUEZ: neutralidad]\n"
            "Sos el juez de NEUTRALIDAD ENTRE PROVEEDORES. Tu única pregunta: ¿este marco "
            "favorece o castiga una arquitectura de producto en particular?\n"
            f"{_SHARED}\n"
            "Esto NO es hipotético en esta herramienta. Una auditoría midió que el puntuador "
            "era sistemáticamente más duro con un proveedor que con otro (100% contra 64,3%; "
            "las cinco discrepancias eran del mismo proveedor y todas hacia abajo). El sesgo "
            "por proveedor es un modo de falla DEMOSTRADO acá adentro. Tratalo como tal.\n"
            "Rechazá o mandá a revisar cuando:\n"
            "- un criterio suponga UNA implementación (un chat, un panel con cierto nombre) "
            "para algo que otra plataforma puede lograr de otra manera;\n"
            "- el marco mida sobre todo lo que UN tipo de producto hace bien, de modo que el "
            "puntaje total premie la arquitectura y no la enseñanza;\n"
            "- una plataforma que resuelve el mismo problema pedagógico con otro diseño "
            "sacaría menos puntos sin ser peor.\n"
            "El marco debe decir QUÉ tiene que lograr la plataforma, no CÓMO."
        ),
    ),
    Judge(
        name="proporcionalidad",
        responsibility="¿El peso de cada criterio es sensato frente a lo que está en juego?",
        system=(
            "[JUEZ: proporcionalidad]\n"
            "Sos el juez de PROPORCIONALIDAD. Tu única pregunta: ¿los pesos son sensatos?\n"
            f"{_SHARED}\n"
            "Reglas de tu rol:\n"
            "- Mirá cuántos puntos suma cada área sobre el total, no criterio por criterio.\n"
            "- Un marco donde la seguridad del tutor vale 1 punto sobre 100 NO es una "
            "evaluación de seguridad, por más que el criterio exista. Decilo así.\n"
            "- Lo mismo al revés: un área con muchos criterios chicos puede dominar el total "
            "sin que nadie lo haya decidido.\n"
            "- Los pesos deben reflejar QUÉ ESTÁ EN JUEGO para un alumno de escuela pública, "
            "no cuán fácil es de medir cada cosa.\n"
            "- Proponé el reacomodo concreto (qué área sube, qué área baja) cuando objetes."
        ),
    ),
)


#: The observability judge, with its JOB CHANGED.
#:
#: It used to sit on the probe council and REJECT probes an evaluator supposedly
#: could not execute. A reviewer flagged the problem: this judge has no platform
#: access, so it was making a plausibility call dressed as a feasibility call.
#: Worse, rejecting on that basis silently reproduced the pilot's own filtering
#: -- criteria disappeared for being hard to see, which is the exact bug the
#: framework rework exists to fix.
#:
#: Its job now is CLASSIFICATION: given a criterion, which of the four evidence
#: classes settles it? That is a question about the criterion, which this judge
#: can answer well, and not a question about a particular product's UI, which it
#: cannot. The only rejection left is genuine incoherence -- a criterion that no
#: evidence of any kind could settle, because it asserts nothing that could turn
#: out to be false. Lack of ACCESS is never a rejection any more; it is a class.
OBSERVABILITY_JUDGE = Judge(
    name="observabilidad",
    responsibility=(
        "¿Qué CLASE de evidencia hace falta para zanjar este criterio: la plataforma, "
        "un documento, datos, o un estudio?"
    ),
    system=(
        "[JUEZ: observabilidad]\n"
        + CLASSIFY_SYSTEM
        + "\nLas cuatro clases, y cómo elegir:\n"
        f"- {OBSERVABLE_EN_PLATAFORMA}: un recorrido por la plataforma lo zanja. "
        "Alguien abre pantallas, escribe, mira lo que pasa, y con eso ya sabe.\n"
        f"- {REQUIERE_DOCUMENTACION}: hace falta un documento del proveedor -- un DPA, "
        "una política de seguridad, una declaración de alineación curricular, un "
        "certificado. Ninguna pantalla lo demuestra.\n"
        f"- {REQUIERE_DATOS}: hace falta acceso a datos -- registros de uso, telemetría "
        "de aprendizaje, una exportación de administrador. Un recorrido produce "
        "anécdota, no la distribución que el criterio pide.\n"
        f"- {REQUIERE_ESTUDIO}: hace falta un diseño de investigación. '¿Mejora el "
        "aprendizaje?' no se contesta mirando un producto, se contesta con un "
        "contrafactual.\n"
        "Reglas de tu rol:\n"
        "- Ante la duda entre plataforma y datos, mirá si el criterio pide una "
        "PROPORCIÓN o una TENDENCIA. Si la pide, son datos.\n"
        "- Si elegís algo que no es la plataforma, tenés que escribir qué pedir, "
        "concreto y accionable. 'Más información' no sirve.\n"
        "- `no_evaluable` es SÓLO para incoherencia: un criterio que no afirma nada que "
        "pudiera resultar falso. Que sea difícil o caro de zanjar NO es incoherencia."
    ),
)


def council_fingerprint(council: Sequence[Judge] = FRAMEWORK_COUNCIL) -> str:
    """sha256 of every judge's prompt + the shared review-tool schema.

    Two runs are only comparable if the instrument was the same, and "same model
    id" does not establish that.
    """
    payload = json.dumps(
        {
            "judges": [
                {"name": j.name, "responsibility": j.responsibility, "system": j.system}
                for j in council
            ],
            "tool": REVIEW_TOOL,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def builder_fingerprint() -> str:
    """sha256 of the four prompts + tool shapes that mint criteria."""
    from . import framework as _self

    payload = json.dumps(
        {
            "select": _self.SELECT_SYSTEM,
            "adapt": [_self.ADAPT_SYSTEM, _self.ADAPT_TOOL],
            "generate": _self.GENERATE_SYSTEM,
            "extract": _self.EXTRACT_SYSTEM,
            "revise": _self.REVISE_SYSTEM,
            "classify": [_self.CLASSIFY_SYSTEM, OBSERVABILITY_JUDGE.system],
            "criterion_props": _CRITERION_PROPS,
            "observability_props": _OBSERVABILITY_PROPS,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- the result -------------------------------------------------------------


@dataclass(frozen=True)
class FrameworkResult:
    """The criteria set, plus everything needed to defend it.

    `criteria()` is the in-memory `Criteria`; `write_yaml` emits the file the
    rest of the pipeline consumes. They agree by construction -- `criteria()`
    builds from the same list `to_yaml` serialises -- and a test round-trips the
    file through the real `load_criteria` to keep it that way.
    """

    name: str
    objective: str
    criteria_list: tuple[FrameworkCriterion, ...]
    coverage: CoverageReport
    rounds: tuple[ReviewRound, ...]
    approved: bool
    bank_id: str
    bank_version: str
    model: str
    context: dict = field(default_factory=dict)

    def __iter__(self):
        return iter(self.criteria_list)

    def __len__(self) -> int:
        return len(self.criteria_list)

    @property
    def observable_list(self) -> tuple[FrameworkCriterion, ...]:
        """The criteria a walkthrough can settle. ONLY these get probed and scored."""
        return tuple(c for c in self.criteria_list if c.is_observable)

    @property
    def not_evaluated_list(self) -> tuple[FrameworkCriterion, ...]:
        """Carried, reported, and kept out of the denominator. Never dropped."""
        return tuple(c for c in self.criteria_list if not c.is_observable)

    def total_points(self) -> float:
        """Points IN THE SCORED DENOMINATOR, i.e. observable criteria only.

        The non-observable ones are not worth zero -- they are not in the
        fraction at all. Counting them in the denominator would publish a low
        score for a platform whose only sin is that we could not look.
        """
        return sum(c.points for c in self.observable_list)

    def no_evaluado_points(self) -> float:
        """Points parked outside the score. Reported so the split is visible."""
        return sum(c.points for c in self.not_evaluated_list)

    def no_evaluados(self) -> list[dict]:
        """Every carried-but-uncheckable criterion, as an actionable request."""
        return [
            {
                "id": c.id,
                "area": c.area,
                "name": c.name,
                "points": c.points,
                "status": NO_EVALUADO,
                "observability": c.observability,
                "evidencia_requerida": c.evidencia_requerida,
                "reason": c.no_evaluado_reason(),
                "source": c.source.to_dict() if c.source else None,
            }
            for c in self.not_evaluated_list
        ]

    def write_no_evaluados(self, path: Path | str) -> Path:
        """The vendor-request artefact: what to ask for, and why it is missing."""
        path = Path(path)
        payload = {
            "objective": self.objective,
            "summary_line": self.coverage.summary_line(),
            "evidence_totals": self.coverage.evidence_totals(),
            "no_evaluados": self.no_evaluados(),
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path

    @property
    def sourced_count(self) -> int:
        return sum(1 for c in self.criteria_list if c.is_sourced)

    @property
    def generated_count(self) -> int:
        return sum(1 for c in self.criteria_list if c.source.type == SOURCE_GENERATED)

    def _by_area(self) -> list[tuple[str, list[FrameworkCriterion]]]:
        """Only the OBSERVABLE criteria, grouped. This feeds `criteria()` and the
        emitted YAML, and therefore `generate_probes`: a criterion no walkthrough
        can settle must never become a probe an evaluator is asked to run.
        """
        order: dict[str, list[FrameworkCriterion]] = {}
        for c in self.observable_list:
            order.setdefault(c.area, []).append(c)
        return list(order.items())

    def criteria(self) -> Criteria:
        """The same framework as a `Criteria`, without a file round trip."""
        domains = []
        for area_id, group in self._by_area():
            name = next((a.name for a in self.coverage.areas if a.area == area_id), area_id)
            domains.append(CriterionDomain(
                code=area_id,
                name=name,
                points=sum(c.points for c in group),
                tests=[c.to_test() for c in group],
            ))
        return Criteria(name=self.name, domains=domains)

    def to_yaml(self) -> str:
        # A framework built with a user-fixed scope has empty `rounds`: no
        # council sat, and the header saying one approved it would be exactly
        # the kind of fabricated claim this module exists to make impossible.
        if self.rounds:
            origin = "# no inventado, y revisado por un consejo de cinco jueces."
            council = f"# Aprobado por el consejo: {'sí' if self.approved else 'NO'}"
        else:
            origin = "# no inventado: alcance fijado por el usuario, área completa del banco."
            council = "# Consejo: no sesionó -- con alcance fijo la revisión del plan es humana."
        out = [
            "# Generado por evalkit.framework -- seleccionado de un banco de criterios,",
            origin,
            f"# Objetivo: {self.objective}",
            f"# Banco: {self.bank_id} v{self.bank_version}",
            f"# Criterios con respaldo: {self.sourced_count}/{len(self.criteria_list)}"
            f" · generados: {self.generated_count}",
            council,
            "#",
            f"# Los criterios marcados {UNSOURCED_MARK} los inventó la herramienta: no tienen",
            "# respaldo institucional. La procedencia completa va en el JSON que acompaña.",
            "#",
            "# Los ids van entre comillas a propósito: sin comillas YAML lee 1.10 como",
            "# 1.1 y `no` como False, y los ids son claves de unión.",
        ]
        out.append(f"# {self.coverage.summary_line()}")
        for row in self.coverage.areas:
            if row.status != STATUS_OK:
                out.append(f"# COBERTURA [{row.area}] {row.status}: {row.note}")
            if row.not_evaluated:
                out.append(f"# EVIDENCIA [{row.area}] {row.evidence_note()}")
        if self.not_evaluated_list:
            # Named here, in the scored file, on purpose. Silence about a
            # criterion we carried but could not settle reads as coverage; a
            # reader must not have to open a second artefact to find out that
            # the framework asked for something this run could not answer.
            out.append("#")
            out.append(
                f"# Estos criterios EXIGE el objetivo y NO se pudieron evaluar acá "
                f"({_numeric(self.no_evaluado_points())} puntos fuera del denominador)."
            )
            out.append("# No valen cero: están fuera de la fracción. Pedí esto:")
            for c in self.not_evaluated_list:
                out.append(
                    f"#   NO EVALUADO [{c.id}] ({c.area}) {c.name}"
                    f" -- {c.observability}: {c.evidencia_requerida}"
                )
        out.append(f"name: {_q(self.name)}")
        out.append("domains:")
        for area_id, group in self._by_area():
            name = next((a.name for a in self.coverage.areas if a.area == area_id), area_id)
            out.append(f"  - code: {_q(area_id)}")
            out.append(f"    name: {_q(name)}")
            out.append(f"    points: {_numeric(sum(c.points for c in group))}")
            out.append("    tests:")
            for c in group:
                out.append(f"      # procedencia -> {c.source.describe()}")
                out.append(f"      - id: {_q(c.id)}")
                out.append(f"        name: {_q(c.name)}")
                out.append(f"        points: {_numeric(c.points)}")
                out.append(f"        verification: {_q(c.verification)}")
                out.append(f"        pass_criteria: {_q(c.pass_criteria)}")
                out.append(f"        fail_criteria: {_q(c.fail_criteria)}")
        return "\n".join(out) + "\n"

    def write_yaml(self, path: Path | str) -> Path:
        path = Path(path)
        path.write_text(self.to_yaml(), encoding="utf-8")
        return path

    def write_provenance(self, path: Path | str) -> Path:
        path = Path(path)
        payload = {
            "objective": self.objective,
            "name": self.name,
            "model": self.model,
            "context": self.context,
            "bank": {"id": self.bank_id, "version": self.bank_version},
            "builder_fingerprint": builder_fingerprint(),
            "council_fingerprint": council_fingerprint(),
            "council": [
                {"judge": j.name, "responsibility": j.responsibility, "family": j.family}
                for j in FRAMEWORK_COUNCIL
            ],
            "approved": self.approved,
            "rounds": [r.to_dict() for r in self.rounds],
            "coverage": self.coverage.to_dict(),
            "criteria": [c.to_dict() for c in self.criteria_list],
            "no_evaluados": self.no_evaluados(),
            "scored_points": self.total_points(),
            "no_evaluado_points": self.no_evaluado_points(),
        }
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path


def _q(value: Any) -> str:
    """A double-quoted YAML scalar. JSON string syntax is valid YAML here."""
    return json.dumps(str(value), ensure_ascii=False)


def _numeric(value: float) -> str:
    return f"{float(value):g}"


# --- build steps ------------------------------------------------------------


def _context_block(objective: str, context: dict, cacheable: bool = True) -> dict:
    lines = [f"OBJETIVO DECLARADO: {objective}"]
    for key, human in (
        ("subject", "Materia"),
        ("grade_range", "Grados"),
        ("language", "Idioma"),
        ("platform_kind", "Tipo de plataforma"),
        ("country", "País / sistema"),
    ):
        if context.get(key):
            lines.append(f"{human}: {context[key]}")
    if context.get("platform_kind"):
        lines.append(
            "El tipo de plataforma es CONTEXTO, no una excusa para suponer su interfaz."
        )
    block = {"type": "text", "text": "\n".join(lines)}
    if cacheable:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def _entry_lines(e: BankEntry) -> str:
    return (
        f"[{e.id}] ({e.area}) {e.name} · {_numeric(e.points)} pts"
        f"{' · ' + e.severity if e.severity else ''}\n"
        f"    verificación: {e.verification}\n"
        f"    CUMPLE: {e.pass_criteria}\n"
        f"    NO_CUMPLE: {e.fail_criteria}\n"
        f"    base: {e.evidence_base or 'sin declarar'} "
        f"(procedencia: {e.source.framework} / {e.source.original_id})"
    )


def _select(
    client: Any, model: str, objective: str, context: dict,
    candidates: Sequence[BankEntry], areas: Sequence[BankArea],
) -> tuple[list[tuple[BankEntry, str]], list[str]]:
    catalogue = "\n\n".join(_entry_lines(e) for e in candidates)
    area_lines = "\n".join(f"- {a.id}: {a.name}" for a in areas)
    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": (
                f"ÁREAS PEDIDAS\n{area_lines}\n\n"
                f"BANCO DE CRITERIOS DISPONIBLES ({len(candidates)})\n\n{catalogue}"
            ),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "Elegí los criterios que corresponden a este objetivo."},
    ]
    data = _ask(client, model, SELECT_SYSTEM, blocks, _select_tool(candidates), max_tokens=8192)
    picks = _pairs(data.get("selected"), "selected")
    gaps = [str(g) for g in (data.get("gaps") or []) if str(g).strip()]
    return picks, gaps


def _pairs(raw: Any, field_name: str) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FrameworkError(
            f'el campo "{field_name}" de la respuesta del modelo debe ser una lista'
        )
    out = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise FrameworkError(
                f'una entrada de "{field_name}" no trae un id de criterio: {item!r}'
            )
        out.append((str(item["id"]), str(item.get("rationale") or "")))
    return out


def _selected_entries(bank: CriteriaBank, picks: Sequence[tuple[str, str]]) -> list[tuple[BankEntry, str]]:
    """Resolve ids against the bank. An id we do not recognise stops the run.

    The tool enum should make this unreachable. It is checked anyway: the enum
    is a REQUEST to the API, and the one thing we must never do is emit a
    criterion whose provenance we made up because the model named it.
    """
    resolved: list[tuple[BankEntry, str]] = []
    seen: set[str] = set()
    for entry_id, rationale in picks:
        if entry_id == NOTHING_TO_PICK:
            continue  # the empty-enum placeholder, never a criterion
        if entry_id in seen:
            continue  # a repeated pick is one criterion, not double weight
        seen.add(entry_id)
        resolved.append((bank.entry(entry_id), rationale))
    return resolved


def _adapt_one(
    client: Any, model: str, objective: str, context: dict,
    entry: BankEntry, rationale: str,
) -> FrameworkCriterion:
    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": (
                "CRITERIO DEL BANCO, TAL COMO LO ESCRIBIÓ EL EXPERTO\n"
                f"{_entry_lines(entry)}\n\n"
                f"Por qué se eligió para este objetivo: {rationale or 'sin motivo declarado'}\n\n"
                "Adaptá su redacción al contexto sin cambiar qué mide."
            ),
        },
    ]
    data = _ask(client, model, ADAPT_SYSTEM, blocks, ADAPT_TOOL)

    fields = {
        "name": entry.name,
        "verification": entry.verification,
        "pass_criteria": entry.pass_criteria,
        "fail_criteria": entry.fail_criteria,
    }
    adapted: dict[str, str] = {}
    reverted: list[str] = []
    for key, original_text in fields.items():
        candidate = _text(data, key, original_text)
        invented = _invented_scope(candidate, original_text)
        if invented:
            # Reverting to the expert's text, not to nothing: the field goes back
            # to the wording that HAS provenance. Recorded out loud, because a
            # silent rollback is how a reviewer stops being able to tell what the
            # adapter did.
            adapted[key] = original_text
            reverted.append(
                f"{key}: se revirtió al original porque la adaptación introdujo "
                f"alcance que el experto no escribió ({', '.join(invented)})"
            )
        else:
            adapted[key] = candidate

    return FrameworkCriterion(
        id=entry.id,
        area=entry.area,
        points=_points(data.get("points"), entry.points),
        source=entry.source,
        observability=entry.observability,
        evidencia_requerida=entry.evidencia_requerida,
        observability_declared=entry.observability_declared,
        original={
            "name": entry.name,
            "points": entry.points,
            "verification": entry.verification,
            "pass_criteria": entry.pass_criteria,
            "fail_criteria": entry.fail_criteria,
            "adaptation_notes": str(data.get("notes") or ""),
            "scope_reverted": reverted,
        },
        rationale=rationale,
        **adapted,
    )


#: Nouns that carry METHODOLOGICAL SCOPE. A number attached to one of these is a
#: decision about how much of the platform to look at -- a run parameter, not a
#: property of the criterion. `grado` is deliberately ABSENT: "3.º grado" is the
#: evaluation's context, which adaptation is explicitly allowed to fix.
_SCOPE_NOUNS = frozenset("""
leccion lecciones pantalla pantallas minuto minutos hora horas dia dias
semana semanas mes meses sesion sesiones alumno alumnos alumna alumnas
estudiante estudiantes caso casos unidad unidades actividad actividades
intento intentos ejercicio ejercicios muestra muestras item items itemes
modulo modulos curso cursos tarea tareas pregunta preguntas turno turnos
mensaje mensajes docente docentes escuela escuelas clase clases capitulo
capitulos ejemplo ejemplos video videos pagina paginas conversacion
conversaciones interaccion interacciones
""".split())

#: Spelled-out quantities count too: "en las cinco lecciones auditadas" invents
#: exactly as much scope as "en las 5".
_NUMBER_WORDS = {
    "un", "una", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete",
    "ocho", "nueve", "diez", "once", "doce", "quince", "veinte", "treinta",
    "varios", "varias", "algunos", "algunas",
}

#: How many words may sit between the number and the noun and still bind them
#: ("5 lecciones", "5 primeras lecciones", "5 de las lecciones").
_SCOPE_WINDOW = 3


def _scope_pairs(text: str) -> set[tuple[str, str]]:
    """(quantity, scope noun) pairs a text commits to."""
    tokens = "".join(c if c.isalnum() else " " for c in _fold(text)).split()
    pairs: set[tuple[str, str]] = set()
    for i, tok in enumerate(tokens):
        if not (tok.isdigit() or tok in _NUMBER_WORDS):
            continue
        for noun in tokens[i + 1: i + 1 + _SCOPE_WINDOW]:
            if noun in _SCOPE_NOUNS:
                pairs.add((tok, noun))
                break
    return pairs


def _invented_scope(adapted: str, original: str) -> list[str]:
    """Scope the adaptation added that the expert never wrote.

    The live failure this exists for: adapting `PT-D1-01` (`Cada chunk <=1
    idea`) produced "En LAS 5 LECCIONES AUDITADAS, cada segmento presenta 1 sola
    idea". The threshold survived intact -- but the sample size is a
    methodological decision the model made and then published under the original
    instrument's provenance, where it reads as the expert's.

    Deterministic and offline on purpose. A judge asked "did this invent scope?"
    is one more opinion; a (quantity, scope-noun) diff against the expert's own
    text is a fact. It is deliberately narrow: only quantities bound to scope
    NOUNS trip it, so ordinary clarification ("una lección de matemática de 3.º
    grado") and thresholds the expert already wrote pass straight through.
    """
    new = _scope_pairs(adapted) - _scope_pairs(original)
    return sorted(f"{n} {noun}" for n, noun in new)


def _text(data: dict, key: str, fallback: str) -> str:
    """The adapted text, or the expert's original when adaptation came back empty.

    Falling back to the ORIGINAL rather than to an empty string is the point: a
    criterion the adapter dropped on the floor reverts to the text that has
    provenance, instead of reaching a judge as the literal string "None".
    """
    value = str(data.get(key) or "").strip()
    return value or fallback


def _points(value: Any, fallback: float) -> float:
    try:
        points = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    return points if points > 0 else float(fallback)


def _observability_from(node: dict, cid: str) -> dict:
    """Read a minting tool's declared observability, refusing an unusable one.

    A tool that declares a non-observable class without saying what would settle
    the criterion has produced the useless half of this feature: the reader
    learns we did not check, and nothing about what to ask for. Rejected here
    rather than repaired, because a repaired `evidencia_requerida` would be this
    module inventing the request.
    """
    obs = str(node.get("observability") or OBSERVABLE_EN_PLATAFORMA).strip()
    if obs not in OBSERVABILITY_CLASSES:
        legales = ", ".join(OBSERVABILITY_CLASSES)
        raise FrameworkError(
            f'el criterio "{cid}" vino con una observabilidad desconocida ("{obs}") '
            f"- las únicas válidas son: {legales}"
        )
    return {
        "observability": obs,
        "evidencia_requerida": str(node.get("evidencia_requerida") or "").strip(),
        "observability_declared": True,
    }


def _classify_observability(
    client: Any, model: str, objective: str, context: dict,
    criteria: Sequence[FrameworkCriterion],
) -> list[FrameworkCriterion]:
    """Classify every criterion whose author did not declare a class.

    ONE batched call, not one per criterion: the classifier compares criteria
    against each other usefully, and the enum of ids keeps it inside the set.

    A criterion whose class an author DID declare is left alone. That is an
    authoring decision, and a judge with no platform access is not the right
    body to overrule it -- which is the same reason this judge stopped rejecting.

    Unclassified criteria stay OBSERVABLE_EN_PLATAFORMA. That is the direction
    that keeps evaluating: it means "we will try to settle this by looking", and
    the medibilidad judge and the probe council still sit downstream of it. The
    opposite default would silently move criteria out of the score, which is the
    failure this whole rework exists to stop.
    """
    pending = [c for c in criteria if not c.observability_declared]
    if not pending:
        return list(criteria)

    listing = "\n\n".join(
        f"[{c.id}] ({c.area}) {c.name}\n"
        f"    verificación: {c.verification}\n"
        f"    CUMPLE: {c.pass_criteria}\n"
        f"    NO_CUMPLE: {c.fail_criteria}"
        for c in pending
    )
    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": (
                f"CRITERIOS A CLASIFICAR ({len(pending)})\n\n{listing}\n\n"
                "Clasificá cada uno por la clase de evidencia que lo zanja."
            ),
        },
    ]
    data = _ask(
        client, model, OBSERVABILITY_JUDGE.system, blocks,
        _classify_tool([c.id for c in pending]), max_tokens=8192,
    )

    decided: dict[str, dict] = {}
    for row in data.get("criteria") or []:
        cid = str(row.get("id") or "")
        if cid == NOTHING_TO_PICK or not cid:
            continue
        if row.get("no_evaluable"):
            # The ONE rejection left, and it is about the criterion, not about
            # access: nothing that could ever be observed would settle it,
            # because it asserts nothing that could turn out false.
            raise FrameworkError(
                f'el criterio "{cid}" no se puede zanjar por ningún medio: '
                f"{str(row.get('reason') or 'sin motivo declarado')}",
                hint=(
                    "(esto NO es falta de acceso -- falta de acceso es una clase de "
                    "observabilidad y se reporta NO EVALUADO. Esto es un criterio que "
                    "no afirma nada que pudiera resultar falso: reescribilo o sacalo)"
                ),
            )
        decided[cid] = row

    out: list[FrameworkCriterion] = []
    for c in criteria:
        row = decided.get(c.id)
        if row is None or c.observability_declared:
            out.append(c)
            continue
        obs = str(row.get("observability") or OBSERVABLE_EN_PLATAFORMA)
        evidencia = str(row.get("evidencia_requerida") or "").strip()
        reason = str(row.get("reason") or "").strip()
        if obs in NON_OBSERVABLE_CLASSES and not evidencia:
            # The classifier said "you cannot see this" and then did not say what
            # to ask for. That is the useless half. Keep it evaluable rather than
            # park a criterion nobody can act on.
            obs, evidencia = OBSERVABLE_EN_PLATAFORMA, ""
            reason = (
                "el clasificador la marcó no observable pero no dijo qué evidencia "
                f"haría falta; se deja evaluable. ({reason})"
            )
        out.append(replace(
            c,
            observability=obs,
            evidencia_requerida=evidencia,
            observability_reason=reason,
            observability_declared=True,
        ))
    return out


def _generate(
    client: Any, model: str, objective: str, context: dict,
    gaps: Sequence[str], areas: Sequence[BankArea], selected: Sequence[FrameworkCriterion],
) -> list[FrameworkCriterion]:
    if not gaps:
        return []
    have = "\n".join(f"- [{c.id}] {c.name}" for c in selected) or "(ninguno)"
    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": (
                "CRITERIOS QUE YA TIENE EL MARCO (no los repitas)\n"
                f"{have}\n\n"
                "VACÍOS DECLARADOS\n"
                + "\n".join(f"- {g}" for g in gaps)
                + "\n\nEmití criterios nuevos SÓLO para estos vacíos. Pocos y buenos."
            ),
        },
    ]
    data = _ask(client, model, GENERATE_SYSTEM, blocks, _generate_tool(areas), max_tokens=8192)
    out = []
    for i, c in enumerate(data.get("criteria") or [], start=1):
        why = str(c.get("why_not_in_bank") or "vacío declarado sin detallar")
        out.append(FrameworkCriterion(
            id=f"GEN-{i:02d}",
            area=str(c.get("area") or areas[0].id),
            name=str(c.get("name") or "").strip() or f"criterio generado {i}",
            points=_points(c.get("points"), 1.0),
            verification=str(c.get("verification") or "").strip(),
            pass_criteria=str(c.get("pass_criteria") or "").strip(),
            fail_criteria=str(c.get("fail_criteria") or "").strip(),
            **_observability_from(c, f"GEN-{i:02d}"),
            source=CriterionSource(
                type=SOURCE_GENERATED,
                framework="evalkit.framework",
                original_id=f"GEN-{i:02d}",
                location=why,
            ),
            rationale=why,
        ))
    return out


def _extract(
    client: Any, model: str, objective: str, context: dict,
    doc: Document, areas: Sequence[BankArea], index: int,
) -> list[FrameworkCriterion]:
    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": f"DOCUMENTO: {doc.name}\n\n{doc.text}",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "Extraé los criterios evaluables que este documento exige."},
    ]
    data = _ask(client, model, EXTRACT_SYSTEM, blocks, _extract_tool(areas), max_tokens=8192)

    out = []
    for i, c in enumerate(data.get("criteria") or [], start=1):
        quote = str(c.get("quote") or "")
        line = doc.locate(quote)
        if line is None:
            # The loud failure, on purpose. A ministry's own policy outranks
            # everything else in this framework; a criterion that cites it
            # falsely is the worst artefact this tool could emit, and it must
            # not be recoverable into a warning nobody reads.
            raise FrameworkError(
                f'un criterio extraído de "{doc.name}" cita un texto que no está '
                f"en el documento: {quote[:120]!r}",
                hint=(
                    "(la cita se verifica contra el documento; sin cita verificable "
                    "el criterio no tiene procedencia)"
                ),
            )
        out.append(FrameworkCriterion(
            id=f"DOC{index}-{i:02d}",
            area=str(c.get("area") or areas[0].id),
            name=str(c.get("name") or "").strip() or f"criterio del documento {i}",
            points=_points(c.get("points"), 1.0),
            verification=str(c.get("verification") or "").strip(),
            pass_criteria=str(c.get("pass_criteria") or "").strip(),
            fail_criteria=str(c.get("fail_criteria") or "").strip(),
            **_observability_from(c, f"DOC{index}-{i:02d}"),
            source=CriterionSource(
                type=SOURCE_DOCUMENT,
                framework=doc.name,
                original_id=f"DOC{index}-{i:02d}",
                location=f"{doc.name}, línea {line}",
            ),
            rationale=quote,
        ))
    return out


# --- review -----------------------------------------------------------------


def _framework_block(
    criteria: Sequence[FrameworkCriterion], coverage: CoverageReport, objective: str
) -> dict:
    """The volatile half: the framework under review. After the cache breakpoint."""
    total = sum(c.points for c in criteria) or 1.0
    by_area: dict[str, list[FrameworkCriterion]] = {}
    for c in criteria:
        by_area.setdefault(c.area, []).append(c)

    lines = [f"MARCO PROPUESTO ({len(criteria)} criterios, {_numeric(total)} puntos)"]
    for area_id, group in by_area.items():
        pts = sum(c.points for c in group)
        lines.append(
            f"\nÁREA {area_id} - {_numeric(pts)} pts "
            f"({pts / total * 100:.0f}% del total), {len(group)} criterios"
        )
        for c in group:
            mark = f" [{UNSOURCED_MARK}]" if not c.is_sourced else ""
            no_eval = "" if c.is_observable else f" [{NO_EVALUADO}]"
            evidencia = (
                "" if c.is_observable
                else f"\n      evidencia requerida: {c.evidencia_requerida}"
            )
            lines.append(
                f"  [{c.id}]{mark}{no_eval} {c.name} · {_numeric(c.points)} pts\n"
                f"      procedencia: {c.source.describe()}\n"
                f"      evidencia: {c.observability}{evidencia}\n"
                f"      verificación: {c.verification}\n"
                f"      CUMPLE: {c.pass_criteria}\n"
                f"      NO_CUMPLE: {c.fail_criteria}"
            )

    lines.append("\nREPORTE DE COBERTURA")
    lines.append(f"  {coverage.summary_line()}")
    for row in coverage.areas:
        lines.append(
            f"  {row.area} ({row.name}): {row.status} - "
            f"{row.bank_criteria} del banco, {row.document_criteria} de documentos, "
            f"{row.generated_criteria} generados; el banco ofrece {row.bank_available}\n"
            f"      evidencia: {row.evidence_note()}"
        )
    if coverage.gaps:
        lines.append("  vacíos declarados: " + "; ".join(coverage.gaps))

    lines.append("\nEmití tu veredicto desde tu rol.")
    return {"type": "text", "text": "\n".join(lines)}


def _judge_one(
    client: Any, judge: Judge, default_model: str, objective: str, context: dict,
    criteria: Sequence[FrameworkCriterion], coverage: CoverageReport,
) -> JudgeVerdict:
    model = judge.model or default_model
    # This judge sees the objective, the framework and the coverage report. It
    # does NOT see any other judge's verdict, in this round or the previous one.
    # A panel that reads itself produces anchoring, not independence.
    data = _ask(
        client,
        model,
        judge.system,
        [_context_block(objective, context), _framework_block(criteria, coverage, objective)],
        REVIEW_TOOL,
        max_tokens=4096,
    )
    verdict = data.get("verdict")
    reasons = [str(r) for r in (data.get("reasons") or [])]
    if verdict not in VERDICTS:
        # Degrades to REJECT, never to APPROVE. The audit found the mirror bug in
        # the scorer, where an unknown token left the denominator and a typo
        # RAISED the published number.
        verdict = UNKNOWN_VERDICT
        reasons = [f"Veredicto inválido del modelo: {data.get('verdict')!r}.", *reasons]
    # `objection_target` is DELIBERATELY not plumbed here. The shared
    # `ReviewRound.objections` / `unanimous_approval` (generator.py) special-
    # case TARGET_CRITERIO for the PROBE council, where "the criterion is a
    # given" holds; in THIS council the criteria are exactly what a revise is
    # meant to change, so the field's semantics would be backwards. Leaving it
    # unplumbed pins every framework verdict to the default ("probe"), which
    # keeps this council's aggregation provably unchanged -- see
    # test_framework_verdicts_default_to_probe_so_aggregation_is_unchanged.
    # If you ever plumb it, reconsider the shared aggregation semantics FIRST.
    return JudgeVerdict(
        judge=judge.name,
        responsibility=judge.responsibility,
        model=model,
        family=judge.family,
        verdict=verdict,
        reasons=reasons,
        suggested_fix=data.get("suggested_fix"),
    )


def _review(
    client: Any, council: Sequence[Judge], model: str, objective: str, context: dict,
    criteria: Sequence[FrameworkCriterion], coverage: CoverageReport, index: int,
) -> ReviewRound:
    """Run the whole panel in parallel. Independent by construction."""
    with ThreadPoolExecutor(max_workers=max(1, len(council))) as pool:
        futures = [
            pool.submit(_judge_one, client, j, model, objective, context, criteria, coverage)
            for j in council
        ]
        verdicts = [f.result() for f in futures]
    return ReviewRound(index=index, verdicts=tuple(verdicts))


def _revise(
    client: Any, model: str, objective: str, context: dict, bank: CriteriaBank,
    criteria: Sequence[FrameworkCriterion], objections: Sequence[JudgeVerdict],
) -> tuple[list[str], list[tuple[str, str]], str]:
    lines = [
        "El consejo revisó este marco y objetó lo siguiente. Corregí exactamente esto:"
    ]
    for v in objections:
        lines.append(f"\n[{v.judge} - {v.responsibility}] veredicto: {v.verdict}")
        lines.extend(f"  - {r}" for r in v.reasons)
        if v.suggested_fix:
            lines.append(f"  sugerencia: {v.suggested_fix}")

    current = [c.id for c in criteria]
    have = "\n".join(f"- [{c.id}] ({c.area}) {c.name} · {_numeric(c.points)} pts" for c in criteria)
    available = [e for e in bank.entries if e.id not in set(current)]
    catalogue = "\n\n".join(_entry_lines(e) for e in available) or "(el banco no ofrece más)"

    blocks = [
        _context_block(objective, context),
        {
            "type": "text",
            "text": f"MARCO ACTUAL\n{have}\n\nBANCO DISPONIBLE PARA SUMAR\n\n{catalogue}",
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n".join(lines)},
    ]
    data = _ask(
        client, model, REVISE_SYSTEM, blocks,
        _revise_tool(current, [e.id for e in available]), max_tokens=8192,
    )
    drop = [str(d) for d in (data.get("drop") or [])]
    add = _pairs(data.get("add"), "add")
    return drop, add, str(data.get("notes") or "")


# --- entry point ------------------------------------------------------------

DEFAULT_MAX_ROUNDS = 2

#: How many bank entries reach the selector. The whole shipped bank fits, but a
#: bank three times the size would not, and truncation must be a stated policy
#: rather than an accident of the context window. Trimming only ever removes
#: CANDIDATES; it can never add one, so it cannot manufacture a criterion.
DEFAULT_CANDIDATE_LIMIT = 200


def _verbatim_criterion(entry: BankEntry) -> FrameworkCriterion:
    """A bank entry as a criterion, word for word.

    No adapter sits in between on purpose: the fixed-scope path's promise is
    that two identical requests produce identical criteria, and a model
    rewording each field breaks that promise even when it changes nothing that
    matters. `original` stays None -- that field exists to diff an adaptation
    against the expert's text, and here the expert's text IS the criterion.
    """
    return FrameworkCriterion(
        id=entry.id,
        area=entry.area,
        name=entry.name,
        points=entry.points,
        verification=entry.verification,
        pass_criteria=entry.pass_criteria,
        fail_criteria=entry.fail_criteria,
        source=entry.source,
        observability=entry.observability,
        evidencia_requerida=entry.evidencia_requerida,
        observability_declared=entry.observability_declared,
        rationale="alcance fijado por el usuario: entra el área completa del banco",
    )


def _fixed_scope_framework(
    objective: str,
    *,
    requested: Sequence[str],
    documents: Sequence[Document] | None,
    client: Any,
    bank: CriteriaBank,
    model: str,
    name: str | None,
    context: dict,
) -> FrameworkResult:
    """The deterministic path for a user-fixed scope (explicit `areas`).

    The audit of 2026-07-25 measured what the alternative costs: areas=["D9"]
    went through the LLM selector and came back with 18 criteria across SEVEN
    areas (five of them D9) -- most of an $8.31 build was scope nobody asked
    for -- and the same request produced 6 probes one day and 16 the next,
    because a model SAMPLES. A published instrument is applied whole, so here
    the criteria are ALL the bank's entries in the requested areas, by filter.
    Unwanted criteria get pruned by the human at plan review; that gate exists
    for exactly this. No selector, no gap scan, no adapter, no council: the
    only model calls this path may make are the observability classifier --
    evidence class is a property of each criterion, not of the scope -- and,
    when the user attached documents, the extractor (see below).
    """
    known = [a for a in requested if bank.knows_area(a)]
    unknown = [a for a in requested if not bank.knows_area(a)]
    if not known:
        # A typo must not produce an empty framework silently. Name the areas
        # that do exist so the fix is in the error message.
        offered = ", ".join(a.id for a in bank.offered_areas())
        raise FrameworkError(
            f"ninguna de las áreas pedidas ({', '.join(unknown)}) existe en el "
            f"banco {bank.bank_id} v{bank.version} - las áreas disponibles "
            f"son: {offered}"
        )

    criteria: list[FrameworkCriterion] = [
        _verbatim_criterion(entry)
        for area_id in known
        for entry in bank.entries_in(area_id)
    ]

    # Uploaded documents are ADDED to that, never substituted for it.
    #
    # DECISION REVERSED (2026-07-26). This path used to refuse `documents`
    # outright, on the reading that extraction is "a model choosing scope out of
    # a text". That is true of the SELECTOR, which this path exists to remove,
    # and false of an upload: the user attaching a norm IS the user fixing
    # scope, and the setup screen has been promising "una norma tuya pesa más
    # que cualquier cosa que generemos" the whole time. Refusing to read it was
    # the same failure the deterministic change was fixing, from the other side.
    #
    # What is NOT loosened: the bank half above stays verbatim and untouched
    # (the document cannot displace, reword or reorder an expert criterion), the
    # extractor may only assign a criterion to an area the user asked for, and
    # every quote is verified against the document -- a fabricated citation to a
    # ministry's own policy still stops the build. Replicability holds because
    # the model is bounded by a text the user supplied rather than sampling a
    # scope out of the whole bank.
    area_objs = [bank.area(a) for a in known]
    for i, doc in enumerate(documents or [], start=1):
        criteria.extend(_extract(client, model, objective, context, doc, area_objs, i))

    # Per-criterion, not scope: declared entries keep their author's class and
    # never reach the model; only undeclared ones are classified. Same rule as
    # the selection path.
    criteria = _classify_observability(client, model, objective, context, criteria)

    # The coverage report says OUT LOUD that the scope was fixed and that no
    # suggestion scan ran -- silence here would read as "nothing else applies",
    # which is not what happened: nobody looked, because nobody asked.
    notes = [
        f"Áreas fijadas por el usuario: {', '.join(known)}. "
        "No se exploraron sugerencias fuera de ese alcance."
    ]
    # ...and the documents get named for the same reason. The note above claims
    # nothing outside the scope was explored; a criterion that came out of an
    # upload nobody was told was read would make that claim false.
    if documents:
        notes.append(
            "Documentos subidos por el usuario, leídos y citados: "
            + ", ".join(d.name for d in documents)
        )
    notes.extend(
        f'el área "{a}" no existe en el banco {bank.bank_id} v{bank.version}; '
        "se siguió sólo con las áreas conocidas"
        for a in unknown
    )
    # `requested` (unknown areas included) goes to the coverage report, so a
    # misspelt area also shows up as an uncovered ROW, not only as a note.
    coverage = _coverage(bank, requested, criteria, notes)

    return FrameworkResult(
        name=name or f"Marco de evaluación - {objective}",
        objective=objective,
        criteria_list=tuple(criteria),
        coverage=coverage,
        rounds=(),        # no council sat; the artefacts must not claim one did
        approved=True,    # nothing objected -- the binding review is the human's
        bank_id=bank.bank_id,
        bank_version=bank.version,
        model=model,
        context={k: v for k, v in context.items() if v},
    )


def build_framework(
    objective: str,
    *,
    areas: Sequence[str] | None = None,
    documents: Sequence[Document] | None = None,
    client: Any,
    bank: CriteriaBank | Path | str | None = None,
    model: str | None = None,
    council: Sequence[Judge] | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    name: str | None = None,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    **context: Any,
) -> FrameworkResult:
    """Build a criteria framework for `objective`, selecting from a criteria bank.

    `areas` are ids from `bank.offered_areas()` -- the list the product shows as
    checkboxes, derived from the bank rather than hardcoded. None means "all
    areas the bank offers", and the model selects within them against the
    objective. EXPLICIT areas mean a user-fixed scope: the criteria are all the
    bank's entries in those areas, deterministically, and no model chooses
    (see `_fixed_scope_framework` for the audit that forced the split).

    `documents` are uploaded policy/spec texts. Criteria extracted from them are
    tagged with the document and the line, and their quote is verified against
    the document; a quote that is not there stops the build. They are read on
    BOTH paths: an attached norm is the user fixing scope, so a fixed scope adds
    the document's criteria to the bank's rather than refusing the upload.

    `context` takes `subject`, `grade_range`, `language`, `platform_kind`,
    `country`. They shape adaptation wording, nothing else.

    Returns the criteria AND a coverage report. Requested areas that the bank
    covers thinly, or only through generated criteria, or not at all, are
    reported as such -- never silently filled.

    `client` is injected so tests run against a fake with no API call.
    """
    if not str(objective or "").strip():
        raise FrameworkError(
            "el objetivo está vacío - sin objetivo no hay contra qué seleccionar criterios"
        )

    if bank is None or isinstance(bank, (str, Path)):
        bank = load_bank(bank)

    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy

    council = tuple(council if council is not None else FRAMEWORK_COUNCIL)
    max_rounds = max(1, int(max_rounds))
    requested = list(dict.fromkeys(
        areas if areas is not None else [a.id for a in bank.offered_areas()]
    ))
    if not requested:
        raise FrameworkError(
            "no se pidió ningún área - sin áreas no hay nada que evaluar"
        )

    # Explicit areas = deterministic scope. Everything below this line is the
    # model-driven path, and it must stay byte-for-byte what it was: `areas is
    # None` keeps selecting by objective exactly as before.
    if areas is not None:
        return _fixed_scope_framework(
            objective,
            requested=requested,
            documents=documents,
            client=client,
            bank=bank,
            model=model,
            name=name,
            context=context,
        )

    # An area the bank has never heard of is NO LONGER an error. The bank's areas
    # are SUGGESTIONS; the objective can call for areas no instrument we hold has
    # ever covered, and those criteria come out `generated` / SIN RESPALDO rather
    # than not coming out at all.
    area_objs = [_area_or_new(bank, a) for a in requested]

    # Only the areas the bank actually carries can produce candidates. The rest
    # are not an error -- they simply have no bank support to offer.
    in_bank = [a for a in requested if bank.knows_area(a)]
    candidates = (
        bank.shortlist(objective, areas=in_bank, limit=candidate_limit) if in_bank else []
    )

    # 1. select from the bank -- CORROBORATION, not a ceiling
    if candidates:
        picks, gaps = _select(client, model, objective, context, candidates, area_objs)
    else:
        # Nothing in the bank serves these areas. That is a declared GAP to fill,
        # not a crash: the objective still calls for these criteria and the tool
        # still owes them, marked SIN RESPALDO.
        picks = []
        gaps = [
            f"el banco no ofrece ningún criterio para el área {a.id} ({a.name})"
            for a in area_objs
        ]
    chosen = _selected_entries(bank, picks)

    # 2. adapt the wording, keeping the original alongside
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(chosen) or 1))) as pool:
        futures = [
            pool.submit(_adapt_one, client, model, objective, context, entry, rationale)
            for entry, rationale in chosen
        ]
        criteria: list[FrameworkCriterion] = [f.result() for f in futures]

    # 3. uploaded documents: first-class, and their citations verified
    for i, doc in enumerate(documents or [], start=1):
        criteria.extend(_extract(client, model, objective, context, doc, area_objs, i))

    # 4. net-new ONLY for declared gaps, marked unsourced
    criteria.extend(_generate(client, model, objective, context, gaps, area_objs, criteria))

    # 5. classify what nobody classified. The observability judge decides WHICH
    #    KIND of evidence each criterion needs; it does not decide whether the
    #    criterion survives. Nothing is dropped here.
    criteria = _classify_observability(client, model, objective, context, criteria)

    coverage = _coverage(bank, requested, criteria, gaps)

    # 6. the council, with revise rounds
    rounds: list[ReviewRound] = []
    panel: ReviewRound | None = None
    for index in range(max_rounds):
        panel = _review(client, council, model, objective, context, criteria, coverage, index)
        rounds.append(panel)
        if not panel.objections or index == max_rounds - 1:
            break
        drop, add, _notes = _revise(
            client, model, objective, context, bank, criteria, panel.objections
        )
        dropped = set(drop)
        criteria = [c for c in criteria if c.id not in dropped]
        for entry, rationale in _selected_entries(bank, add):
            if any(c.id == entry.id for c in criteria):
                continue
            criteria.append(
                _adapt_one(client, model, objective, context, entry, rationale)
            )
        coverage = _coverage(bank, requested, criteria, gaps)

    assert panel is not None  # max_rounds >= 1

    # The invariant, restated at the exit: nothing leaves here without a source.
    # `FrameworkCriterion.__post_init__` already guarantees it, so this can only
    # fire if someone adds a construction path that bypasses the dataclass.
    for c in criteria:
        if c.source is None or c.source.type not in SOURCE_TYPES:
            raise FrameworkError(
                f'el criterio "{c.id}" llegó a la salida sin procedencia válida'
            )

    return FrameworkResult(
        name=name or f"Marco de evaluación - {objective}",
        objective=objective,
        criteria_list=tuple(criteria),
        coverage=coverage,
        rounds=tuple(rounds),
        approved=panel.unanimous_approval,
        bank_id=bank.bank_id,
        bank_version=bank.version,
        model=model,
        context={k: v for k, v in context.items() if v},
    )
