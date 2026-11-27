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
import re
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
    "prueba no sirve para la regla y el veredicto es NO_EVALUADO. Una nota que "
    "dice [CAPTURA DIRECTA] avisa que la pantalla se guardó tal como estaba, "
    "sin que nadie comprobara que muestra lo que el paso nombra: lo que vale "
    "es el texto y la imagen guardados, no el nombre del paso. Una nota que "
    "dice [SE HIZO DISTINTO DE LO ESCRITO] cuenta qué hizo de verdad quien "
    "ejecuta: juzgá con eso y no con el nombre del paso. Una nota que dice [LA "
    "PLATAFORMA SEGUÍA RESPONDIENDO AL CAPTURAR] avisa que esa captura puede "
    "no mostrar la respuesta: no deduzcas de ella que la plataforma no "
    "respondió.\n"
    # Agregado tras la corrida completa del 2026-11-26: cinco criterios quedaron
    # sin veredicto porque cada prueba se juzgó con una lectura propia de la
    # regla (ver `read_rule`).
    "Si el mensaje trae una LECTURA DE LA REGLA, es la misma para todas las "
    "pruebas de este criterio y se fijó antes de mirar la evidencia: aplicala "
    "tal cual, con sus definiciones. No la reinterpretes ni le agregues "
    "excepciones tuyas. Describí el HECHO observado antes de decidir: dos "
    "pruebas que observan el mismo hecho reciben el mismo veredicto."
)

_READING_TOOL_NAME = "emitir_lectura"

READING_TOOL: dict[str, Any] = {
    "name": _READING_TOOL_NAME,
    "description": "Fijá cómo se lee la regla de este criterio, para todas sus pruebas.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pregunta": {
                "type": "string",
                "description": "Qué pregunta la regla, en una frase, con sus palabras.",
            },
            "definiciones": {
                "type": "string",
                "description": (
                    "Los términos de la regla que admiten más de una lectura, "
                    "cada uno fijado: qué incluye y qué no."
                ),
            },
            "cumple": {
                "type": "string",
                "description": "Qué HECHO observable en la plataforma cuenta como CUMPLE.",
            },
            "parcial": {
                "type": "string",
                "description": "Qué hecho observable cuenta como PARCIAL.",
            },
            "no_cumple": {
                "type": "string",
                "description": "Qué HECHO observable cuenta como NO_CUMPLE.",
            },
            "no_alcanza": {
                "type": "string",
                "description": (
                    "Qué evidencia NO responde a la regla aunque se le parezca "
                    "(un hecho vecino, una ausencia): con eso es NO_EVALUADO."
                ),
            },
        },
        "required": ["pregunta", "cumple", "no_cumple", "no_alcanza"],
    },
}

READING_SYSTEM = (
    "Fijás, UNA vez por criterio, cómo se lee su regla en una evaluación de "
    "plataformas educativas. Después, varias pruebas de ese criterio se juzgan "
    "por separado, cada una con su evidencia y todas con tu lectura: tu trabajo "
    "es que todas midan lo mismo. Todavía nadie miró la evidencia y vos no "
    "conocés la plataforma: no supongas qué se va a encontrar ni a favor ni en "
    "contra.\n"
    "Leé la regla LITERALMENTE. Decí qué pregunta, con sus palabras. Fijá los "
    "términos que admiten más de una lectura (qué cuenta como 'elemento nuevo', "
    "qué cuenta como 'explicar antes'): elegí la lectura más apegada al texto y "
    "decí qué incluye y qué no. Decí qué HECHO observable en pantalla cuenta "
    "como CUMPLE, cuál como PARCIAL y cuál como NO_CUMPLE. Y decí qué NO "
    "alcanza: los hechos vecinos que se confunden con la regla (que una "
    "actividad empiece con una pregunta no dice si hubo una explicación antes) "
    "y las ausencias (que no haya un temporizador no muestra que el avance sea "
    "por dominio). Sé breve y concreto. Respondés con la herramienta."
)


#: Cuántas veces se pide la lectura de una regla antes de darla por perdida.
READING_ATTEMPTS = 2

#: Las claves sin las cuales una lectura no sirve: la pregunta y los dos extremos.
_READING_NEEDS = ("pregunta", "cumple", "no_cumple")

#: Un campo de la herramienta pegado DENTRO del texto de otro: el cierre de un
#: parámetro y la apertura del siguiente, escritos como texto (ver
#: `_recover_glued_fields`, con el caso).
_GLUED_FIELD = re.compile(r'</parameter>\s*<parameter name="([A-Za-z_][A-Za-z0-9_]*)">')
_GLUED_TAIL = re.compile(r"\s*</parameter>(?:\s*</invoke>)?\s*$")


class ReadingUnavailable(EvalkitError):
    """No se pudo fijar la lectura de la regla de un criterio (ver `read_rule`)."""


#: Los campos de una lectura, en el orden en que el juez los lee, con su rótulo.
READING_FIELDS = (
    ("pregunta", "Qué pregunta la regla"),
    ("definiciones", "Definiciones"),
    ("cumple", "CUMPLE si se observa"),
    ("parcial", "PARCIAL si se observa"),
    ("no_cumple", "NO_CUMPLE si se observa"),
    ("no_alcanza", "NO alcanza para decidir (es NO_EVALUADO)"),
)


@dataclass(frozen=True)
class RuleReading:
    """La lectura de la regla de un criterio, CAMPO POR CAMPO.

    Hasta el 2026-11-27 `read_rule` devolvía el texto ya armado. Alcanzaba
    mientras la lectura sólo viajaba al juez. Dejó de alcanzar cuando hubo que
    mostrársela a una persona antes de gastar, dejarla corregir un campo,
    revisar si el PARCIAL quedó definido y guardar una huella que cambie si
    cambia un campo: nada de eso se hace sobre un texto pegado.

    `str(lectura)` da EXACTAMENTE el texto de antes, que es lo que recibe el
    juez (`score_test(reading=...)`): el instrumento no cambia. `parcial`,
    `definiciones` y `no_alcanza` pueden faltar (hay reglas sin estado
    intermedio); la pregunta y los dos extremos, nunca.
    """

    pregunta: str
    cumple: str
    no_cumple: str
    definiciones: str = ""
    parcial: str = ""
    no_alcanza: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k, _ in READING_FIELDS}

    @classmethod
    def from_dict(cls, data: Any) -> "RuleReading":
        """Arma la lectura desde sus campos; sin la pregunta o un extremo, no hay lectura."""
        data = data if isinstance(data, dict) else {}
        clean = {k: str(data.get(k) or "").strip() for k, _ in READING_FIELDS}
        missing = [k for k in _READING_NEEDS if not clean[k]]
        if missing:
            raise ReadingUnavailable("a la lectura le falta: " + ", ".join(missing))
        return cls(**clean)

    def as_text(self) -> str:
        return "\n".join(
            f"- {human}: {getattr(self, key)}" for key, human in READING_FIELDS if getattr(self, key)
        )

    __str__ = as_text


def _recover_glued_fields(data: dict, known: tuple[str, ...]) -> dict:
    """Devuelve a su clave cada campo que el modelo dejó pegado dentro de otro.

    EL CASO. Al volver a juzgar cuatro veces la evidencia congelada de una
    corrida (2026-11-27) la lectura de la regla llegó vacía en 48 de 172
    juicios de criterio. El modelo la había escrito entera. La respuesta cruda
    de siete llamadas mostró siempre lo mismo: el valor de un campo traía, como
    texto, el cierre de ese campo y la apertura del siguiente,

        "definiciones": "...no inferido de documentación.</parameter>\n<parameter name=\"cumple\">En 3 o más pares...",

    y a veces el cierre de la llamada al final (`</parameter>\n</invoke>`). La
    clave `cumple` no llegaba como clave, `read_rule` veía que faltaba un
    extremo y descartaba todo. En una de las siete sólo llegaron dos claves y
    las otras cuatro venían adentro de `definiciones`.

    Lo pegado vuelve a su clave sólo si esa clave es del esquema y NO llegó por
    su cuenta: una clave que sí llegó manda, y lo pegado que la repite se
    descarta. Lo pegado con un nombre que el esquema no conoce se descarta
    también: no hay dónde ponerlo y no es parte del campo donde apareció.
    """
    out = dict(data)
    for key, value in data.items():
        if not isinstance(value, str) or "</parameter>" not in value:
            continue
        parts = _GLUED_FIELD.split(value)
        out[key] = _GLUED_TAIL.sub("", parts[0]).strip()
        for name, text in zip(parts[1::2], parts[2::2]):
            if name not in known:
                continue
            if str(data.get(name) or "").strip() or str(out.get(name) or "").strip():
                continue  # la clave llegó por su cuenta, o ya se recuperó: la primera manda
            out[name] = _GLUED_TAIL.sub("", text).strip()
    return out


def _reading_of(response: Any) -> "tuple[RuleReading | None, str]":
    """(la lectura, "") o (None, por qué esta respuesta no es una lectura)."""
    try:
        data = _extract_tool_input(response, _READING_TOOL_NAME)
    except (ValueError, TypeError, AttributeError):
        return None, "la respuesta no trae la herramienta de lectura"
    if not isinstance(data, dict):
        return None, "la respuesta no trae la herramienta de lectura"
    data = _recover_glued_fields(data, tuple(k for k, _ in READING_FIELDS))
    # Sin la pregunta y los dos extremos no hay lectura que valga la pena mandar.
    missing = [k for k in _READING_NEEDS if not str(data.get(k) or "").strip()]
    if missing:
        return None, (
            "llegó sin: " + ", ".join(missing)
            + " (llegaron: " + (", ".join(sorted(str(k) for k in data)) or "ninguna clave") + ")"
        )
    return RuleReading.from_dict(data), ""


def read_rule(
    test: CriterionTest, client: Any, *, model: str | None = None, context: str = "",
) -> RuleReading:
    """Cómo se lee la regla de UN criterio, fijado antes de juzgar. Una llamada.

    Devuelve la lectura con sus campos (`RuleReading`); `str()` de eso es el
    texto que recibe el juez.

    POR QUÉ. En la corrida completa del 2026-11-26 cinco criterios (16 pruebas
    que habían llegado al final) quedaron sin veredicto por "juicio en duda".
    El chequeo de coherencia los frenó con razón, y en los cinco la causa era la
    misma: cada prueba se juzga sola, y cada juicio leía la regla por su cuenta.
    En PT-D2-01 dos pruebas decidieron que los botones de ayuda no contaban como
    "elemento nuevo" y la tercera los contó. En PT-D6-03 el mismo hecho recibió
    NO_CUMPLE en dos pruebas y CUMPLE en otra. En PT-D5-05 se juzgó la pregunta
    de reflexión que viene después, no si el ítem exige el principio.

    Degradar el veredicto evita publicar algo que no se sostiene, pero deja al
    criterio sin respuesta por una falla del evaluador. Esto ataca la causa: la
    lectura de la regla se fija UNA vez, antes de ver la evidencia, y viaja
    igual a todas las pruebas del criterio (`score_test(..., reading=...)`).

    `context` es lo que el plan dijo que se iba a hacer y mirar; ayuda a fijar
    las definiciones sobre lo que de verdad se va a observar.

    NUNCA devuelve una lectura vacía. Hasta el 2026-11-27 devolvía "" cuando a
    la respuesta le faltaba la pregunta o un extremo, y quien llamaba juzgaba
    "como antes", sin lectura y sin decirlo. Eso pasó en 48 de 172 juicios de
    criterio de una medición sobre evidencia congelada, y no era inocuo: dos
    pasadas del mismo criterio diferían en 21 % de los pares cuando las dos
    tenían (o no tenían) lectura y en 58 % cuando una la tenía y la otra no, y
    sin lectura el juez daba CUMPLE más seguido (56 % contra 44 %). Una
    instrucción que aparece y desaparece al azar es ruido que pone el evaluador.

    Ahora: lo que el modelo dejó pegado dentro de otro campo se recupera (ver
    `_recover_glued_fields`: era la causa de las 48); si igual queda
    incompleta se pide de nuevo, `READING_ATTEMPTS` veces en total; y si tampoco
    sale levanta `ReadingUnavailable` con lo que faltó, para que quien llama
    decida a la vista (el servicio deja el criterio NO_EVALUADO con esa causa,
    ver `JobRunner._score_cells`).

    El pedido repetido es por una RESPUESTA incompleta, no por un error: un
    error de la API (un 400, un 401) sube en el primer intento, sin reintento
    acá. Los transitorios ya los reintenta service/retry.py por debajo.
    """
    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy
    request = dict(
        model=model,
        max_tokens=2500,
        system=[{"type": "text", "text": READING_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": (
            f"CRITERIO {test.id}: {test.name}\n"
            f"Aprueba si: {test.pass_criteria}\n"
            f"Falla si: {test.fail_criteria}\n"
            f"Cómo se verifica: {test.verification}"
            + (f"\n\nLO QUE EL PLAN DICE QUE SE VA A HACER Y MIRAR: {context}" if context else "")
        )}]}],
        tools=[READING_TOOL],
        tool_choice={"type": "tool", "name": _READING_TOOL_NAME},
    )
    why = ""
    for _ in range(READING_ATTEMPTS):
        reading, why = _reading_of(client.messages.create(**request))
        if reading:
            return reading
    raise ReadingUnavailable(
        f"la lectura de la regla de {test.id} no se pudo fijar en "
        f"{READING_ATTEMPTS} intentos: {why}"
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


def _build_content(
    test: CriterionTest, evidence_rows: list[dict], reading: str = "",
) -> tuple[list[dict], set[str]]:
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
    ]
    if reading:
        # La misma para todas las pruebas del criterio (ver `read_rule`).
        lines += [
            "",
            "LECTURA DE LA REGLA (la misma para todas las pruebas de este criterio):",
            reading,
        ]
    lines += [
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
    test: CriterionTest, evidence_rows: list[dict], client: Any, model: str,
    reading: str = "",
) -> tuple[dict, set[str]]:
    content, missing = _build_content(test, evidence_rows, reading)
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


def _one_run(
    test: CriterionTest, evidence_rows: list[dict], client: Any, model: str,
    reading: str = "",
) -> tuple[str, str, list[str]]:
    """One judged run -> (status, rationale, cited). Coerces uncited positives."""
    data, missing, stop_reason = _ask_once(test, evidence_rows, client, model, reading)
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
    reading: str = "",
) -> TestVerdict:
    """Judge one test on one platform against its evidence rows.

    `reading` es la lectura de la regla fijada para el criterio (ver
    `read_rule`): viaja igual a todas sus pruebas. Vacía = se juzga como antes.

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
        _one_run(test, evidence_rows, client, model, reading) for _ in range(max(1, repeats))
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
        {"system": JUDGE_SYSTEM, "tool": VERDICT_TOOL, "reading": READING_SYSTEM},
        sort_keys=True, ensure_ascii=False
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
