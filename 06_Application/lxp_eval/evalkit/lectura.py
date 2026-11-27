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


# --- escribir las lecturas de un plan ----------------------------------------


def _one(
    test: CriterionTest, client: Any, *, model: str | None, context: str,
    samples: int, reader: Callable[..., Any], compare: Callable[..., dict],
) -> dict:
    """El registro de lectura de UN criterio: sus muestras, si coinciden, y en qué estado queda."""
    entry: dict = {
        "version": SCHEMA_VERSION, "criterio": test.id, "estado": NO_DISPONIBLE,
        "lectura": None, "muestras": [], "acuerdo": None, "motivo": "",
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
    entry["acuerdo"] = {
        "equivalentes": bool(agreement.get("equivalentes")),
        "diferencias": str(agreement.get("diferencias") or ""),
    }
    if entry["acuerdo"]["equivalentes"]:
        entry["estado"] = ESTABLE
        entry["lectura"] = taken[0].as_dict()
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
    on_done: Callable[[int, int], None] | None = None,
    max_workers: int = WORKERS,
) -> dict[str, dict]:
    """La lectura de cada criterio que se va a probar, muestreada. Corre en la etapa de generación.

    Por criterio: `samples` lecturas escritas por separado (`reader`, por
    defecto `scoring.read_rule`) y una comparación (`compare`). Si coinciden,
    queda UNA lectura en estado ESTABLE; si no, quedan las muestras y el
    criterio espera a una persona (REQUIERE_ADJUDICACION); si una muestra no
    salió, NO_DISPONIBLE con el motivo.

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
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, total))) as pool:
        futures = {
            pool.submit(
                _one, t, client, model=model, context=str(contexts.get(t.id) or ""),
                samples=samples, reader=reader, compare=compare,
            ): t
            for t in tests
        }
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
