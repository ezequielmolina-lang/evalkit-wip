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

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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

#: Los estados con los que una lectura se puede congelar. Nunca una muestra
#: sola: o coincidieron dos, o la decidió una persona (ver `freezable`).
FREEZABLE = (ESTABLE,)


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
    return {
        "vaga": data["vaga"],
        "parcial_definido": bool(data.get("parcial_definido")),
        "anclas_contables": bool(data.get("anclas_contables")),
        "objeciones": [str(x).strip() for x in (raw if isinstance(raw, list) else []) if str(x).strip()],
        "sobre": sobre,
    }


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
            "la lectura de la regla no salió igual dos veces y hay que decidir cuál vale: "
            + str((entry.get("acuerdo") or {}).get("diferencias") or "sin detalle")
        )
    if state == NO_DISPONIBLE:
        return "no se pudo escribir la lectura de la regla: " + str(entry.get("motivo") or "sin detalle")
    vague = entry.get("vaguedad") or {}
    if vague.get("vaga"):
        return "la lectura de la regla deja algo para decidir a ojo: " + (
            "; ".join(vague.get("objeciones") or []) or "sin detalle"
        )
    return ""


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

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, total))) as pool:
        futures = {pool.submit(_guarded, t): t for t in tests}
        for fut in as_completed(futures):
            test = futures[fut]
            out[test.id] = fut.result()   # una excepción sube y corta la etapa
            with beat:
                done += 1
                now = done
            logger.info("lectura %d/%d (%s): %s", now, total, test.id, out[test.id]["estado"])
            if on_done is not None:
                on_done(now, total)
    # En el orden del instrumento, no en el que terminaron los hilos.
    return {t.id: out[t.id] for t in tests}
