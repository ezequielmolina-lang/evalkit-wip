"""La lectura de la regla como parte del plan: se escribe al generar, se revisa y se congela.

POR QUÉ EXISTE ESTE MÓDULO. El 2026-11-27 se volvió a juzgar cuatro veces la
evidencia congelada de una corrida completa. El juez cambió el veredicto en 23
de 43 criterios, y en 21 de esos 23 relataba los mismos hechos y los juzgaba
distinto: no veía otra cosa, leía la regla de otra forma. La lectura de la regla
(`scoring.read_rule`) se generaba de nuevo al puntuar, en cada corrida, y nadie
la veía antes de gastar. Arreglado el defecto que la dejaba vacía, 13 de 24
criterios seguían volcando con la lectura siempre presente.

Acá vive lo que hace falta para que esa lectura deje de improvisarse:

* se escribe en la ETAPA DE GENERACIÓN (la que tiene cliente y corre vigilada),
  no al armar el documento del plan, que es una función pura;
* se MUESTREA: una sola lectura no dice si la regla se lee siempre igual, así
  que se piden dos y otra llamada dice si darían el mismo veredicto. Sólo una
  lectura estable entre muestras, o decidida por una persona, se puede congelar;
* quien escribe la lectura NO es quien redacta los pasos: un mismo rol
  redactaría una prueba hecha para pasar su propia vara.

Nada de esto toca al consejo que revisa las pruebas ni al juez: los prompts de
acá son propios y no entran en `council_fingerprint` ni en `judge_fingerprint`.

EL CONTRATO ESTÁ APAGADO POR DEFECTO (`Engine.frozen_readings` en el servicio).
Que una lectura congelada sea reproducible no dice que sea la correcta: eso se
decide contra veredictos de personas, y hasta entonces se puntúa como siempre.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from . import scoring
from .criteria import CriterionTest
from .scoring import ReadingUnavailable, RuleReading

logger = logging.getLogger(__name__)

#: Versión del registro de lectura que se guarda en el trabajo y en el plan.
SCHEMA_VERSION = 1

#: Cuántas veces se escribe la lectura de un criterio al generar. Con una sola
#: no se puede saber si la regla se lee igual dos veces: "una corrida no afirma
#: unanimidad" vale también para la lectura.
SAMPLES = 2

#: Cuántos criterios se leen a la vez (cada uno hace SAMPLES + 1 llamadas chicas).
WORKERS = 4

# --- estados de una lectura --------------------------------------------------
#: Las muestras darían el mismo veredicto: hay UNA lectura y se puede congelar.
ESTABLE = "estable"
#: Las muestras no coinciden: una persona tiene que decidir cuál vale.
REQUIERE_ADJUDICACION = "requiere_adjudicacion"
#: Alguna muestra no salió (ver scoring.read_rule): no hay con qué comparar.
NO_DISPONIBLE = "no_disponible"

#: Una persona eligió una de las muestras que no coincidían.
ADJUDICADA = "adjudicada"
#: Una persona escribió o corrigió la lectura.
EDITADA = "editada"

#: Los estados con los que una lectura se puede congelar. Nunca una muestra
#: sola: o coincidieron dos, o la decidió una persona (ver `freezable`).
FREEZABLE = (ESTABLE, ADJUDICADA, EDITADA)


def freezable(entry: dict | None) -> bool:
    """¿Esta lectura se puede congelar? Hace falta una lectura Y un estado que la respalde."""
    return bool(entry) and entry.get("estado") in FREEZABLE and bool(entry.get("lectura"))


# --- ¿dos lecturas dicen lo mismo? -------------------------------------------

_COMPARE_TOOL_NAME = "comparar_lecturas"

COMPARE_TOOL: dict[str, Any] = {
    "name": _COMPARE_TOOL_NAME,
    "description": "Decí si dos lecturas de la misma regla darían el mismo veredicto.",
    "input_schema": {
        "type": "object",
        "properties": {
            "equivalentes": {
                "type": "boolean",
                "description": (
                    "true sólo si ante los MISMOS hechos observados las lecturas "
                    "darían siempre el mismo veredicto."
                ),
            },
            "diferencias": {
                "type": "string",
                "description": (
                    "Si no son equivalentes: en qué difieren, en una o dos frases, "
                    "con un hecho concreto que una lectura llamaría de una forma y "
                    "la otra de otra. Vacío si son equivalentes."
                ),
            },
        },
        "required": ["equivalentes", "diferencias"],
    },
}

COMPARE_SYSTEM = (
    "Te muestran lecturas de la MISMA regla de un criterio, escritas por "
    "separado, sin verse. Cada una dice qué pregunta la regla, cómo se definen "
    "sus términos, qué hecho observable cuenta como CUMPLE, cuál como PARCIAL, "
    "cuál como NO_CUMPLE y qué no alcanza para decidir.\n"
    "Tu única tarea: decir si, ante los MISMOS hechos observados en una "
    "plataforma, darían siempre el mismo veredicto. Que estén redactadas "
    "distinto no importa. Importa una diferencia que cambie un veredicto: otro "
    "umbral, otra unidad de conteo, un caso que una incluye y la otra excluye, "
    "un PARCIAL que una define y la otra no, algo que una manda a NO_EVALUADO y "
    "la otra juzga.\n"
    "Buscá un hecho concreto que reciba veredictos distintos. Si lo encontrás, "
    "no son equivalentes y lo decís. Si después de buscarlo no lo encontrás, "
    "son equivalentes. Ante la duda, no lo son: una persona va a decidir. "
    "Respondés con la herramienta."
)


def _reading_block(index: int, reading: RuleReading) -> str:
    return f"LECTURA {index}\n{reading.as_text()}"


def compare_readings(
    test: CriterionTest, readings: Sequence[RuleReading], client: Any,
    *, model: str | None = None,
) -> dict:
    """¿Estas lecturas de la misma regla darían el mismo veredicto? Una llamada.

    No se puede decidir comparando texto: dos lecturas de la misma regla salen
    siempre con otra redacción, y lo que importa es si un hecho recibiría otro
    veredicto. En PT-D2-01 (contar elementos nuevos en pantalla) todas las
    pasadas describían la misma pantalla y diferían en si cada opción de
    respuesta cuenta como un elemento o si el bloque de opciones cuenta como
    uno: eso es lo que hay que cazar antes de congelar.

    Devuelve {"equivalentes": bool, "diferencias": str}. Lo que no se pudo
    comparar (una respuesta sin la herramienta o sin el sí o no) cuenta como
    NO equivalentes: el costo de equivocarse para ese lado es que una persona
    mire dos lecturas; para el otro, congelar una lectura que no era estable.
    Un error de la API sube.
    """
    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy
    body = "\n\n".join(_reading_block(i, r) for i, r in enumerate(readings, start=1))
    response = client.messages.create(
        model=model,
        max_tokens=1000,
        system=[{"type": "text", "text": COMPARE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": (
            f"CRITERIO {test.id}: {test.name}\n"
            f"Aprueba si: {test.pass_criteria}\n"
            f"Falla si: {test.fail_criteria}\n\n{body}"
        )}]}],
        tools=[COMPARE_TOOL],
        tool_choice={"type": "tool", "name": _COMPARE_TOOL_NAME},
    )
    try:
        data = scoring._extract_tool_input(response, _COMPARE_TOOL_NAME)
    except (ValueError, TypeError, AttributeError):
        data = {}
    if not isinstance(data.get("equivalentes"), bool):
        return {
            "equivalentes": False,
            "diferencias": "no se pudo comparar las lecturas: la respuesta no dijo si eran equivalentes",
        }
    same = data["equivalentes"]
    return {"equivalentes": same, "diferencias": "" if same else str(data.get("diferencias") or "").strip()}


# --- ¿la lectura quedó vaga? -------------------------------------------------

#: Sobre qué texto corrió el chequeo de vaguedad. No es lo mismo revisar lo que
#: escribió el modelo que lo que escribió una persona, y el registro lo dice.
SOBRE_MODELO = "lectura escrita por el modelo"
SOBRE_PERSONA = "texto escrito por una persona"

_VAGUE_TOOL_NAME = "chequear_lectura"

VAGUE_TOOL: dict[str, Any] = {
    "name": _VAGUE_TOOL_NAME,
    "description": "Decí si esta lectura de una regla deja algo para decidir a ojo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "parcial_definido": {
                "type": "boolean",
                "description": (
                    "true si la lectura dice qué HECHO observable es PARCIAL, o "
                    "dice expresamente que la regla no tiene estado intermedio. "
                    "false si PARCIAL falta sin explicación o es 'algo intermedio'."
                ),
            },
            "anclas_contables": {
                "type": "boolean",
                "description": (
                    "true si cada umbral o conteo dice QUÉ se cuenta y DESDE "
                    "CUÁNTO (o si la regla no tiene conteos). false si hay un "
                    "'varios', 'la mayoría', 'suficiente' sin número ni unidad."
                ),
            },
            "objeciones": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Cada cosa que quedaría a criterio de quien juzga, en una "
                    "frase, con las palabras de la lectura que la causan. Vacío "
                    "si no hay ninguna."
                ),
            },
            "vaga": {
                "type": "boolean",
                "description": (
                    "true si dos personas cuidadosas, mirando la misma pantalla "
                    "con esta lectura, podrían dar veredictos distintos."
                ),
            },
        },
        "required": ["parcial_definido", "anclas_contables", "objeciones", "vaga"],
    },
}

VAGUE_SYSTEM = (
    "Revisás UNA lectura de la regla de un criterio, antes de que una persona "
    "la apruebe y quede fija para juzgar. No revisás una prueba ni una "
    "plataforma, y no te toca decir si la lectura es la correcta para el "
    "criterio: eso lo mira otro. Tu única pregunta es si la lectura deja algo "
    "para decidir a ojo.\n"
    "Mirá tres cosas. Uno: PARCIAL. ¿Dice qué hecho observable lo distingue de "
    "CUMPLE y de NO_CUMPLE, o dice expresamente que la regla no tiene estado "
    "intermedio? Un PARCIAL ausente sin explicación, o definido como 'algo "
    "intermedio', se decide a ojo. Dos: las anclas. Cada umbral o conteo tiene "
    "que decir qué se cuenta y desde cuánto ('cada opción de respuesta cuenta "
    "como un elemento; cinco o más es NO_CUMPLE'). 'Varios', 'la mayoría', "
    "'suficiente' sin número ni unidad se deciden a ojo. Tres: los términos. "
    "Si una palabra de la regla admite dos lecturas y la lectura no eligió una, "
    "queda a ojo.\n"
    "Sé concreto: cada objeción cita las palabras de la lectura que la causan. "
    "No objetes por estilo ni pidas más texto por pedir. Respondés con la "
    "herramienta."
)


def check_reading(
    test: CriterionTest, reading: RuleReading, client: Any,
    *, model: str | None = None, sobre: str = SOBRE_MODELO,
) -> dict:
    """¿Esta lectura deja algo para decidir a ojo? Una llamada, con su propio prompt.

    POR QUÉ ES UN CHEQUEO PROPIO Y NO UN ASIENTO DEL CONSEJO. El consejo tiene
    un asiento de falsabilidad, pero está escrito para revisar UNA PRUEBA
    ("Estás revisando UN probe"), y su prompt entra en `council_fingerprint`:
    tocarlo para que lea lecturas cambia el instrumento con el que se
    revisaron todas las pruebas anteriores. Esto pregunta otra cosa, sobre
    otro objeto, con otro prompt.

    Lo que busca es lo que hizo volcar al juez sobre la misma evidencia: en 20
    de 23 criterios la regla dejaba sin definir la unidad de conteo, el umbral
    o el caso intermedio, y cada lectura lo resolvía por su cuenta.

    Devuelve {"vaga", "parcial_definido", "anclas_contables", "objeciones",
    "sobre"}. `sobre` dice si el texto revisado lo escribió el modelo o una
    persona. Una respuesta que no dice si es vaga cuenta como objeción: lo que
    no se pudo chequear no se da por bueno. Un error de la API sube.
    """
    if model is None:
        from ..config import CONFIG

        model = CONFIG.model_heavy
    response = client.messages.create(
        model=model,
        max_tokens=1500,
        system=[{"type": "text", "text": VAGUE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": (
            f"CRITERIO {test.id}: {test.name}\n"
            f"Aprueba si: {test.pass_criteria}\n"
            f"Falla si: {test.fail_criteria}\n\n"
            f"LECTURA A REVISAR\n{reading.as_text()}"
        )}]}],
        tools=[VAGUE_TOOL],
        tool_choice={"type": "tool", "name": _VAGUE_TOOL_NAME},
    )
    try:
        data = scoring._extract_tool_input(response, _VAGUE_TOOL_NAME)
    except (ValueError, TypeError, AttributeError):
        data = {}
    if not isinstance(data.get("vaga"), bool):
        return {
            "vaga": True, "parcial_definido": False, "anclas_contables": False,
            "objeciones": ["no se pudo chequear la lectura: la respuesta no dijo si era vaga"],
            "sobre": sobre,
        }
    raw = data.get("objeciones")
    out = {
        "vaga": data["vaga"],
        "parcial_definido": data.get("parcial_definido") is True,
        "anclas_contables": data.get("anclas_contables") is True,
        "objeciones": [str(x).strip() for x in (raw if isinstance(raw, list) else []) if str(x).strip()],
        "sobre": sobre,
    }
    # UNA RESPUESTA QUE SE CONTRADICE NO ES UN VISTO BUENO. "No es vaga" junto
    # a un PARCIAL sin definir, a conteos sin ancla o a una objeción escrita es
    # una respuesta que no se pudo leer como aprobación (ver `is_vague`).
    out["vaga"] = is_vague(out)
    return out


def is_vague(check: dict | None) -> bool:
    """¿Este chequeo objeta la lectura? Mira todo lo que dijo, no sólo la bandera.

    Alcanza con cualquiera: dijo que es vaga, PARCIAL quedó sin definir, los
    conteos quedaron sin ancla, o escribió una objeción. El error para este
    lado es que una persona mire una lectura que estaba bien.
    """
    if not check:
        return False
    return bool(
        check.get("vaga") is not False
        or check.get("parcial_definido") is not True
        or check.get("anclas_contables") is not True
        or check.get("objeciones")
    )


#: Con qué empieza cada aviso de `review_note`: para poder sacar del plan el
#: aviso de una lectura cuando la lectura cambia (ver `without_review_notes`).
_NOTE_UNSTABLE = "la lectura de la regla no salió igual dos veces"
_NOTE_MISSING = "no se pudo escribir la lectura de la regla"
_NOTE_VAGUE = "la lectura de la regla deja algo para decidir a ojo"


def without_review_notes(notes: Iterable[str] | None) -> list[str]:
    """Las advertencias de un criterio, sin las que puso una lectura anterior."""
    return [
        n for n in (notes or [])
        if not str(n).startswith((_NOTE_UNSTABLE, _NOTE_MISSING, _NOTE_VAGUE))
    ]


def review_note(entry: dict | None) -> str:
    """Lo que una persona tiene que mirar de esta lectura antes de aprobar, o "" si nada.

    Va a las `advertencias` del criterio en el plan y lo marca para revisar:
    la lectura vaga, la que no salió igual dos veces y la que no salió.
    """
    if not entry:
        return ""
    state = entry.get("estado")
    if state == REQUIERE_ADJUDICACION:
        return (
            _NOTE_UNSTABLE + " y hay que decidir cuál vale: "
            + str((entry.get("acuerdo") or {}).get("diferencias") or "sin detalle")
        )
    if state == NO_DISPONIBLE:
        return _NOTE_MISSING + ": " + str(entry.get("motivo") or "sin detalle")
    vague = entry.get("vaguedad") or {}
    if is_vague(vague):
        said = list(vague.get("objeciones") or [])
        if vague.get("parcial_definido") is not True:
            said.append("PARCIAL quedó sin definir")
        if vague.get("anclas_contables") is not True:
            said.append("hay conteos o umbrales sin unidad ni número")
        return _NOTE_VAGUE + ": " + ("; ".join(said) or "sin detalle")
    return ""


# --- la huella de una lectura y su edición por una persona -------------------


def reading_hash(reading: dict | RuleReading | None) -> str:
    """sha256 de los campos de una lectura, en un orden fijo. "" si no hay lectura.

    Cambia si cambia un campo y no cambia si no cambió nada: es lo que deja
    decir, debajo de una lectura editada, cuál era la anterior.
    """
    if not reading:
        return ""
    fields = reading.as_dict() if isinstance(reading, RuleReading) else RuleReading.from_dict(reading).as_dict()
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decided_by_a_person(
    entry: dict, test: CriterionTest, reading: RuleReading, *, state: str, by: str,
    client: Any, model: str | None, check: Callable[..., dict] | None, sobre: str, now: str | None,
) -> dict:
    """El registro nuevo: la lectura decidida arriba, la anterior y su huella debajo."""
    who = str(by or "").strip()
    if not who:
        # Una lectura que decide una persona lleva su nombre: es quien responde
        # por la vara con la que se juzga.
        raise ValueError("la edición de una lectura necesita decir quién la hizo")
    out = copy.deepcopy(entry)
    history = list(out.get("historial") or [])
    before = {
        "lectura": out.get("lectura"),
        "hash_lectura": out.get("hash_lectura") or reading_hash(out.get("lectura")),
        "estado": out.get("estado"),
        "vaguedad": out.get("vaguedad"),
        "editada_por": out.get("editada_por", ""),
        "editada_en": out.get("editada_en", ""),
    }
    # Cuál muestra se había elegido es un dato de ESA decisión: queda en el
    # historial y no sobrevive a la siguiente (una lectura editada después ya
    # no es "la muestra 2").
    if "muestra_elegida" in out:
        before["muestra_elegida"] = out.pop("muestra_elegida")
    history.append(before)
    out.update({
        "historial": history,
        "lectura": reading.as_dict(),
        "hash_lectura": reading_hash(reading),
        "estado": state,
        "motivo": "",
        "editada_por": who,
        "editada_en": now or _now(),
        "vaguedad": check(test, reading, client, model=model, sobre=sobre) if check is not None else None,
    })
    return out


def edit_reading(
    entry: dict, test: CriterionTest, fields: dict, *, by: str, client: Any,
    model: str | None = None, check: Callable[..., dict] | None = check_reading,
    now: str | None = None,
) -> dict:
    """La lectura corregida o escrita por una persona. Devuelve un registro NUEVO.

    POR QUÉ UN CAMINO PROPIO. La edición humana que ya existía
    (`generator.with_human_edit`) edita una PRUEBA y se niega a propósito a
    volver a revisar texto escrito por una persona; y la lectura no es un
    campo de la prueba: es del criterio, y la comparten todas sus pruebas.
    Acá la persona corrige la vara, no los pasos.

    `fields` son los campos que cambia; los demás quedan como estaban. Si no
    había una lectura (las muestras no coincidieron, o no salió), `fields`
    tiene que traerla entera. La lectura anterior, su huella y su estado
    quedan en `historial`; la nueva se vuelve a hashear y queda en estado
    EDITADA, que se puede congelar.

    El chequeo de vaguedad corre DE NUEVO sobre el texto de la persona y el
    registro dice que corrió sobre ese texto. Si objeta, la objeción queda a
    la vista y la decisión sigue siendo de la persona: no se le devuelve la
    lectura para que la reescriba un modelo.

    Una edición que deja la lectura sin la pregunta o sin un extremo levanta
    `ReadingUnavailable` y no cambia nada. Sin nombre, `ValueError`.
    """
    known = [k for k, _ in scoring.READING_FIELDS]
    # LO QUE NO SE ENTIENDE SE RECHAZA, NO SE IGNORA. Un campo mal escrito
    # ("cumpel") o un valor que no es texto se descartaban en silencio, y el
    # registro quedaba como "editado por una persona" con la lectura del modelo
    # intacta: quien edita cree que corrigió algo y el registro afirma una
    # autoría que no existe.
    unknown = [k for k in (fields or {}) if k not in known]
    if unknown:
        raise ValueError(
            "la lectura no tiene estos campos: " + ", ".join(map(str, unknown))
            + " (los que tiene: " + ", ".join(known) + ")"
        )
    base = dict(entry.get("lectura") or {})
    for key, value in (fields or {}).items():
        if value is None:
            base[key] = ""              # borrar un campo; si era un extremo, se rechaza abajo
        elif isinstance(value, str):
            base[key] = value
        else:
            raise ValueError(
                f"el campo {key} de la lectura tiene que ser texto, no {type(value).__name__}"
            )
    reading = RuleReading.from_dict(base)
    if entry.get("lectura") and reading_hash(reading) == reading_hash(entry["lectura"]):
        raise ValueError(
            "la edición no cambia nada de la lectura: no se registra como editada "
            "por una persona una lectura que nadie tocó"
        )
    return _decided_by_a_person(
        entry, test, reading, state=EDITADA, by=by, client=client, model=model,
        check=check, sobre=SOBRE_PERSONA, now=now,
    )


def adjudicate_reading(
    entry: dict, test: CriterionTest, sample: int, *, by: str, client: Any,
    model: str | None = None, check: Callable[..., dict] | None = check_reading,
    now: str | None = None,
) -> dict:
    """Una persona elige cuál de las muestras vale (`sample` desde 1). Devuelve un registro NUEVO.

    Es la salida de REQUIERE_ADJUDICACION sin escribir nada: las dos lecturas
    no coincidían y alguien decide cuál se congela. El chequeo de vaguedad
    corre sobre la elegida, que es texto del modelo.
    """
    samples = entry.get("muestras") or []
    if len(samples) < 2:
        # Elegir entre muestras supone que hubo dos para comparar. Con una sola
        # (la otra no salió) no hay elección: la persona puede escribirla
        # (`edit_reading`), que dice lo que es.
        raise ValueError(
            f"para elegir hacen falta dos muestras y este criterio tiene {len(samples)}"
        )
    if type(sample) is not int or not 1 <= sample <= len(samples):
        raise ValueError(f"no hay una muestra {sample}: este criterio tiene {len(samples)}")
    out = _decided_by_a_person(
        entry, test, RuleReading.from_dict(samples[sample - 1]), state=ADJUDICADA, by=by,
        client=client, model=model, check=check, sobre=SOBRE_MODELO, now=now,
    )
    out["muestra_elegida"] = sample
    return out


# --- congelar: lo que va al plan aprobado, con sus huellas -------------------

#: Los tipos de artefacto que una prueba guarda si nadie dice otra cosa (el
#: valor por defecto de `Probe.capture`).
DEFAULT_ARTIFACTS = ("screenshot", "page_text")


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def rule_hash(test: CriterionTest) -> str:
    """La huella de la REGLA tal como está escrita: lo que el juez recibe de ella y nada más.

    Se guarda en la lectura cuando se la escribe, para saber después contra
    qué redacción se escribió. La huella del observable se calcula al congelar,
    con el texto del plan aprobado; si la regla se reescribió ANTES de aprobar
    (la persona la editó al aprobar, o el criterio se relocalizó entre una
    redacción y otra), esa huella saldría bien y una lectura hecha para la
    regla vieja juzgaría la nueva. Con esto, al congelar se nota.
    """
    return _sha({
        "id": test.id,
        "verificacion": test.verification,
        "cumple": test.pass_criteria,
        "falla": test.fail_criteria,
    })


def observable_hash(test: CriterionTest, reading: dict, artifacts: Iterable[str]) -> str:
    """La huella del OBSERVABLE: la regla tal como está escrita, su lectura y qué evidencia pide.

    Se vence si se reescribe la regla (la lectura se escribió contra esa
    redacción), si cambia un campo de la lectura o si cambia el tipo de
    evidencia que hace falta. No se vence por lo que no toca la regla: el
    título del criterio o su puntaje.
    """
    return _sha({
        "id": test.id,
        "verificacion": test.verification,
        "cumple": test.pass_criteria,
        "falla": test.fail_criteria,
        "lectura": RuleReading.from_dict(reading).as_dict(),
        "artefactos": sorted({str(a) for a in artifacts}),
    })


def recon_hash(surfaces: Iterable[dict]) -> str:
    """La huella del RECORRIDO: las superficies donde se prueba el criterio y la huella de su captura.

    Es aparte de la del observable a propósito: si la plataforma cambió una
    pantalla, se vence esto y no la lectura de la regla, que no tiene por qué
    cambiar porque cambió un botón.
    """
    return _sha(sorted(
        (str(s.get("id") or ""), str(s.get("sha256") or "")) for s in surfaces if isinstance(s, dict)
    ))


def set_hash(hashes: Iterable[str]) -> str:
    """Una huella para un conjunto de huellas, sin importar el orden: la que va al informe."""
    return _sha(sorted(str(h) for h in hashes))


def freeze(readings: dict[str, dict], tests: Iterable[CriterionTest]) -> dict[str, dict]:
    """Lo que queda en el plan aprobado: por criterio, la lectura congelada con sus huellas.

    Sólo se congela una lectura que se puede congelar (ver `freezable`): dos
    muestras que coincidieron, o la que decidió una persona. La que no, entra
    igual, con `lectura: None` y el motivo: así quien puntúa sabe que el
    contrato estaba encendido y que ese criterio quedó sin lectura decidida, y
    no la reemplaza por una generada en el momento.
    """
    by_id = {t.id: t for t in tests}
    out: dict[str, dict] = {}
    for cid, entry in (readings or {}).items():
        artifacts = [str(a) for a in (entry.get("artefactos_requeridos") or DEFAULT_ARTIFACTS)]
        frozen = {
            "version": SCHEMA_VERSION,
            "estado": entry.get("estado"),
            "lectura": None,
            "motivo": review_note(entry) or str(entry.get("motivo") or ""),
            "artefactos_requeridos": artifacts,
            "superficies": copy.deepcopy(entry.get("superficies") or []),
            "hash_recon": str(entry.get("hash_recon") or recon_hash(entry.get("superficies") or [])),
        }
        test = by_id.get(cid)
        written_for = str(entry.get("hash_regla") or "")
        if test is not None and freezable(entry) and written_for and written_for != rule_hash(test):
            # LA REGLA CAMBIÓ DESPUÉS DE ESCRIBIR SU LECTURA. No se congela: la
            # lectura dice cómo leer otra redacción (ver `rule_hash`).
            frozen["vencida"] = True
            frozen["motivo"] = (
                "la regla de este criterio se reescribió después de escribir su "
                "lectura: la lectura es de la redacción anterior"
            )
        elif test is not None and freezable(entry):
            reading = RuleReading.from_dict(entry["lectura"]).as_dict()
            frozen.update({
                "lectura": reading,
                "hash_lectura": reading_hash(reading),
                "hash_observable": observable_hash(test, reading, artifacts),
                "vaguedad": copy.deepcopy(entry.get("vaguedad")),
                "editada_por": str(entry.get("editada_por") or ""),
                "editada_en": str(entry.get("editada_en") or ""),
            })
        out[cid] = frozen
    return out


#: Por qué un criterio con el contrato encendido puede quedar sin puntuar.
SIN_ADJUDICAR = "lectura sin adjudicar"
VENCIDA = "lectura congelada vencida"
NO_LEIDA = "no se pudo leer la regla"
SIN_TIPO = "falta el tipo de evidencia"


# --- la compuerta por TIPO de artefacto --------------------------------------


def required_artifacts(probes: Iterable[dict]) -> list[str]:
    """Qué tipos de artefacto tiene que haber para juzgar un criterio: los que sus pruebas guardan.

    Es un dato que YA estaba declarado: cada prueba dice qué captura
    (`Probe.capture`, por defecto captura de pantalla y texto de página). No
    se reusa `evidencia_requerida`, que es una frase para que la lea una
    persona, ni se agrega nada por nombre de criterio: la compuerta de abajo
    mira TIPOS, y qué tipos hacen falta sale de las pruebas.
    """
    kinds: set[str] = set()
    for probe in probes:
        kinds.update(str(k) for k in (probe.get("capture") or DEFAULT_ARTIFACTS))
    return sorted(kinds or DEFAULT_ARTIFACTS)


def missing_artifacts(required: Iterable[str], rows: Iterable[dict]) -> list[str]:
    """Los tipos pedidos que no están entre las filas de evidencia de una prueba.

    ES TODO LO QUE ESTA COMPUERTA DECIDE: si el TIPO está o no. No abre el
    archivo ni lee su contenido. Que el archivo exista y se pueda leer lo
    sigue controlando el juez (`scoring._build_content` y la regla de "sin
    cita válida no hay veredicto"), y lo que exige LEER la pantalla ("¿se
    compararon dos distractores de la misma pregunta?") no es una compuerta:
    va en el `no_alcanza` de la lectura y lo decide el modelo, diciéndolo.
    """
    have = {str(r.get("kind")) for r in rows}
    return [k for k in sorted({str(x) for x in required}) if k not in have]


def stale_surfaces(entry: dict | None, parts: Iterable[dict]) -> list[str]:
    """Las superficies de una lectura congelada cuya captura ya no es la misma (o que ya no están).

    Compara lo que se guardó al congelar con un recorrido nuevo de la
    plataforma. Es la mitad "se vence si cambió la plataforma" de las dos
    huellas; hace falta un recorrido nuevo para preguntarlo, así que no corre
    al puntuar.
    """
    now = {str(p.get("id")): str(p.get("sha256") or "") for p in parts if isinstance(p, dict)}
    return [
        str(s.get("id")) for s in (entry or {}).get("superficies") or []
        if now.get(str(s.get("id"))) != str(s.get("sha256") or "")
    ]


def frozen_reading_for(frozen: dict | None, test: CriterionTest) -> tuple[RuleReading | None, str, str]:
    """La lectura congelada de un criterio para puntuar: (lectura, "", "") o (None, causa, motivo).

    `frozen` es `approved_plan["lecturas"]`. Nunca devuelve una lectura que no
    corresponde: si nadie la decidió, o si su huella ya no coincide con la
    regla que se va a juzgar, dice por qué y quien llama deja el criterio sin
    puntuar. No hay un tercer camino que la reemplace por otra.
    """
    entry = (frozen or {}).get(test.id) or {}
    if not entry.get("lectura"):
        why = str(entry.get("motivo") or "") or "este criterio no tiene una lectura en el plan aprobado"
        if entry.get("vencida"):
            return None, VENCIDA, why
        if entry.get("estado") == NO_DISPONIBLE:
            # El lector no devolvió una lectura al generar. Es la misma falla
            # que cuando no la devuelve al puntuar, y se llama igual: no es una
            # decisión pendiente de nadie, es nuestra.
            return None, NO_LEIDA, why
        return None, SIN_ADJUDICAR, why
    expected = observable_hash(test, entry["lectura"], entry.get("artefactos_requeridos") or DEFAULT_ARTIFACTS)
    if entry.get("hash_observable") != expected:
        return None, VENCIDA, (
            "la lectura se congeló contra otra redacción de la regla, otra lectura "
            "u otro tipo de evidencia: su huella ya no coincide"
        )
    return RuleReading.from_dict(entry["lectura"]), "", ""


# --- escribir las lecturas de un plan ----------------------------------------


def _one(
    test: CriterionTest, client: Any, *, model: str | None, context: str,
    samples: int, reader: Callable[..., Any], compare: Callable[..., dict],
    check: Callable[..., dict] | None = None,
) -> dict:
    """El registro de lectura de UN criterio: sus muestras, si coinciden, y en qué estado queda."""
    entry: dict = {
        "version": SCHEMA_VERSION, "criterio": test.id, "estado": NO_DISPONIBLE,
        "lectura": None, "muestras": [], "acuerdo": None, "motivo": "",
        "vaguedad": None,
        # Contra qué redacción de la regla se escribió esta lectura (ver `rule_hash`).
        "hash_regla": rule_hash(test),
    }
    taken: list[RuleReading] = []
    for _ in range(max(2, samples)):
        try:
            reading = reader(test, client, model=model, context=context)
        except ReadingUnavailable as exc:
            entry["motivo"] = str(exc)
            entry["muestras"] = [r.as_dict() for r in taken]
            return entry
        if not isinstance(reading, RuleReading):
            # Un lector que devuelve otra cosa (texto suelto, vacío) no dio una
            # lectura con campos: no hay qué mostrarle a una persona ni qué hashear.
            entry["motivo"] = "el lector no devolvió una lectura con campos"
            entry["muestras"] = [r.as_dict() for r in taken]
            return entry
        taken.append(reading)
    entry["muestras"] = [r.as_dict() for r in taken]
    agreement = compare(test, taken, client, model=model)
    # `is True`, no "algo verdadero": un comparador reemplazado que devuelva
    # "false" como texto no puede terminar en una lectura congelable.
    entry["acuerdo"] = {
        "equivalentes": agreement.get("equivalentes") is True,
        "diferencias": str(agreement.get("diferencias") or ""),
    }
    if entry["acuerdo"]["equivalentes"]:
        entry["estado"] = ESTABLE
        entry["lectura"] = taken[0].as_dict()
        entry["hash_lectura"] = reading_hash(taken[0])
        # El chequeo de vaguedad queda EN la lectura, como campo: quien la
        # aprueba, la edita o la congela lo tiene delante, no en un log.
        if check is not None:
            entry["vaguedad"] = check(test, taken[0], client, model=model)
    else:
        # SIN ACUERDO NO HAY UNA LECTURA. No se elige una de las muestras "por
        # ahora": si quedara puesta, tarde o temprano alguien la congelaría, y
        # eso es congelar n=1 con otro nombre.
        entry["estado"] = REQUIERE_ADJUDICACION
        entry["motivo"] = entry["acuerdo"]["diferencias"]
    return entry


def write_readings(
    tests: Iterable[CriterionTest],
    *,
    client: Any,
    model: str | None = None,
    contexts: dict[str, str] | None = None,
    samples: int = SAMPLES,
    reader: Callable[..., Any] | None = None,
    compare: Callable[..., dict] | None = None,
    check: Callable[..., dict] | None = None,
    on_done: Callable[[int, int], None] | None = None,
    max_workers: int = WORKERS,
) -> dict[str, dict]:
    """La lectura de cada criterio que se va a probar, muestreada. Corre en la etapa de generación.

    Por criterio: `samples` lecturas escritas por separado (`reader`, por
    defecto `scoring.read_rule`) y una comparación (`compare`). Si coinciden,
    queda UNA lectura en estado ESTABLE; si no, quedan las muestras y el
    criterio espera a una persona (REQUIERE_ADJUDICACION); si una muestra no
    salió, NO_DISPONIBLE con el motivo. Con `check` (ver `check_reading`), la
    lectura estable lleva además su chequeo de vaguedad en el campo `vaguedad`.

    `contexts` es lo que el plan dijo que se iba a hacer y mirar por criterio
    (lo mismo que recibe `read_rule` al puntuar). `on_done(hechos, total)` es
    el latido para quien vigila la etapa. Un error de la API sube y corta la
    etapa, igual que al redactar las pruebas.
    """
    tests = list(tests)
    reader = reader or scoring.read_rule
    compare = compare or compare_readings
    contexts = contexts or {}
    out: dict[str, dict] = {}
    total = len(tests)
    if not total:
        return out
    done = 0
    beat = threading.Lock()
    # SI UNO REVIENTA, LOS QUE NO EMPEZARON NO EMPIEZAN. Un 400 falla igual
    # para el criterio siguiente, y sin esto los que estaban en cola seguían
    # leyéndose (tres llamadas cada uno) después del error: gasto de más y un
    # fallo que tarda en aparecer. La marca la pone el hilo que falló, antes de
    # terminar, para que el siguiente de la cola la vea.
    stop = threading.Event()

    def _guarded(t: CriterionTest) -> dict | None:
        if stop.is_set():
            return None
        try:
            return _one(
                t, client, model=model, context=str(contexts.get(t.id) or ""),
                samples=samples, reader=reader, compare=compare, check=check,
            )
        except BaseException:
            stop.set()
            raise

    # El hilo que falla avisa ANTES de que su resultado quede listo, así que un
    # criterio salteado puede terminar primero. Por eso el error se guarda y se
    # levanta al final, y un resultado vacío se saltea: si se usara, un 400
    # llegaría a quien mira convertido en un error de tipos.
    failed: BaseException | None = None
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, total))) as pool:
        futures = {pool.submit(_guarded, t): t for t in tests}
        for fut in as_completed(futures):
            test = futures[fut]
            try:
                entry = fut.result()
            except BaseException as exc:  # noqa: BLE001 -- se levanta abajo, el primero
                failed = failed or exc
                continue
            if entry is None:
                continue
            out[test.id] = entry
            with beat:
                done += 1
                now = done
            logger.info("lectura %d/%d (%s): %s", now, total, test.id, out[test.id]["estado"])
            if on_done is not None:
                on_done(now, total)
    if failed is not None:
        raise failed   # corta la etapa, con el error de verdad
    # En el orden del instrumento, no en el que terminaron los hilos.
    return {t.id: out[t.id] for t in tests}
