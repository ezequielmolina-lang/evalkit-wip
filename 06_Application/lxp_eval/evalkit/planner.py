"""El planificador: el dueño del plan que una persona aprueba ANTES de gastar.

POR QUÉ EXISTE. La etapa de aprobación existía desde el principio, pero lo que
ponía delante de la persona era una lista de pruebas ya redactadas: cuarenta
pasos por criterio, sin una sola imagen de la plataforma. Con eso nadie puede
contestar las dos preguntas que importan antes de pagar una corrida: "¿miraron
todo?" y "¿esto que dicen que no existe, de verdad no existe?". El 2026-11-25 se
aprobó (en automático) un plan cuyo informe dejó 20 de 28 criterios sin evaluar,
y recién leyendo el informe se pudo ver qué había faltado.

El plan que sale de acá es un documento corto que dice, con las capturas del
recorrido al lado:

  * qué se va a probar: cada criterio, en qué pantalla, cuántas veces y sobre
    qué temas;
  * qué NO se puede evaluar y por qué (no como un cero: como un hallazgo);
  * qué quedó indefinido: lo que el recorrido vio y no recorrió, o lo que el
    planificador no pudo ubicar en ninguna pantalla. Es el lugar donde la
    persona dice "eso sí existe, está acá";
  * qué documentos hacen falta, y qué criterios destraba cada uno.

EL ORDEN (el del encargo, no otro): el recorrido mira; el PLANIFICADOR decide la
estrategia (`evaluate_strategy`); el MUESTREO propone n, temas y variantes para
lo que el planificador eligió (`sampling.propose_sampling`); y los dos cierran
juntos (`reconcile`): el planificador integra la matriz y el muestreo confirma
que lo elegido se puede muestrear. Recién ahí se arma el documento.

LO QUE EL CÓDIGO NO LE CREE AL MODELO, que es la misma regla del juez ("sólo
cita artefactos que recibió") aplicada un paso antes:
  * una pantalla citada tiene que ser un id del recorrido; una inventada se
    descarta, y un "se prueba" que se queda sin pantallas pasa a indefinido;
  * un criterio sin decisión no desaparece: hereda la clasificación del marco.

LO QUE ESTE MÓDULO NO HACE: no redacta los pasos de las pruebas ni llama al
consejo de jueces. Eso cuesta la mayor parte de un plan (unos USD 0,25 por
criterio) y se hace después de que la persona aprobó QUÉ se prueba.
"""
from __future__ import annotations

import base64
import html
import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import usage as usage_mod
from .errors import EvalkitError
from .steps import LONG_MARK

#: Los cinco estados que el planificador (el modelo) puede darle a un criterio.
SE_PRUEBA = "se_prueba"
NECESITA_DOCUMENTO = "necesita_documento"
NECESITA_ESTUDIO = "necesita_estudio"
NO_SE_PUEDE_MEDIR = "no_se_puede_medir"
FALTA_RECORRER = "falta_recorrer"
MODEL_STATES = (SE_PRUEBA, NECESITA_DOCUMENTO, NECESITA_ESTUDIO, NO_SE_PUEDE_MEDIR, FALTA_RECORRER)

#: Los dos estados que sólo pone el CÓDIGO, a partir del alcance (ver
#: `with_scope` y evalkit/usage.py). El modelo que decide la estrategia no los
#: puede emitir: salen del modelo de uso y del contexto que declara el config.
#: `fuera_de_alcance`: el comportamiento lo hace el docente, no la plataforma.
#: `mal_localizado`: la regla depende de un hecho de otro país o de otra materia.
FUERA_DE_ALCANCE = "fuera_de_alcance"
MAL_LOCALIZADO = "mal_localizado"
OUT_OF_SCOPE = (FUERA_DE_ALCANCE, MAL_LOCALIZADO)
STATES = MODEL_STATES + OUT_OF_SCOPE

#: Las cubetas del documento. `indefinido` es a propósito una cubeta propia y
#: no una variante de "no se puede": es donde la persona corrige. Y
#: `fuera_de_alcance` tampoco es "no se puede": no es que no se pueda medir, es
#: que medirlo sobre la plataforma sería medir a otro.
BUCKET = {
    SE_PRUEBA: "se_prueba",
    NECESITA_DOCUMENTO: "documento",
    NECESITA_ESTUDIO: "no_se_puede",
    NO_SE_PUEDE_MEDIR: "no_se_puede",
    FALTA_RECORRER: "indefinido",
    FUERA_DE_ALCANCE: "fuera_de_alcance",
    MAL_LOCALIZADO: "fuera_de_alcance",
}

#: Los cuatro cubos del alcance, que es como el revisor lee el plan: qué es
#: trabajo de la plataforma, qué es del docente, qué está escrito para otro
#: lugar y qué pide un documento o un estudio.
CUBO_PLATAFORMA = "plataforma"
CUBO_DOCENTE = "docente"
CUBO_MAL_LOCALIZADO = "mal_localizado"
CUBO_DOC_ESTUDIO = "doc_estudio"
CUBOS = (CUBO_PLATAFORMA, CUBO_DOC_ESTUDIO, CUBO_DOCENTE, CUBO_MAL_LOCALIZADO)

#: Un criterio que pregunta si algo EXISTE ("¿hay un modo donde...?"), frente a
#: uno que pregunta cómo se comporta algo que está ahí.
EXISTENCIA = "existencia"
COMPORTAMIENTO = "comportamiento"

#: Cuántas pruebas juzgadas hacen falta para afirmar una ausencia.
MIN_PROBES_FOR_ABSENCE = 2

#: Qué estado hereda un criterio sin decisión, según cómo lo clasificó el marco.
_FROM_OBSERVABILITY = {
    "REQUIERE_DOCUMENTACION": NECESITA_DOCUMENTO,
    "REQUIERE_DATOS": NECESITA_DOCUMENTO,
    "REQUIERE_ESTUDIO": NECESITA_ESTUDIO,
    "NO_DECIDIBLE_EN_EVALUACION": NO_SE_PUEDE_MEDIR,
}

#: Cuántos criterios van en una llamada. El perfil se cachea y se reusa; lo que
#: se parte es la salida, que con cien criterios no entra en una respuesta.
_CHUNK = 25

# --- estimación de la corrida ------------------------------------------------
#
# Medidas, no supuestas. Reprueba en vivo del 2026-11-25: tres pruebas, 28
# pasos, USD 1,20 y 7 min 25 s. El plan (redacción + consejo) costó unos
# USD 0,25 por criterio en las corridas de ese día, y el juez unos centavos.
# Son para que la persona sepa el orden de magnitud de lo que autoriza; el
# documento las muestra como estimación.
USD_PER_PROBE = float(os.getenv("LXP_PLAN_USD_PER_PROBE", "0.40"))
USD_PER_CRITERION_DRAFT = float(os.getenv("LXP_PLAN_USD_PER_DRAFT", "0.25"))
USD_PER_CRITERION_JUDGE = float(os.getenv("LXP_PLAN_USD_PER_JUDGE", "0.05"))
MINUTES_PER_PROBE = float(os.getenv("LXP_PLAN_MIN_PER_PROBE", "2.5"))

STRATEGY_TOOL_NAME = "emitir_estrategia"

STRATEGY_TOOL: dict[str, Any] = {
    "name": STRATEGY_TOOL_NAME,
    "description": "Emití la decisión para cada criterio recibido.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decisiones": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "El id del criterio, tal cual."},
                        "estado": {"type": "string", "enum": list(MODEL_STATES)},
                        "partes": {
                            "type": "array", "items": {"type": "string"},
                            "description": (
                                "Los ids (P1, P2...) de las pantallas del "
                                "recorrido donde se prueba. Sólo ids que figuren."
                            ),
                        },
                        "comportamientos": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Los ids (C1...) de comportamientos que ya dan indicios.",
                        },
                        "que_se_prueba": {
                            "type": "string",
                            "description": (
                                "En una o dos frases: qué se va a hacer en esa "
                                "pantalla y qué se va a mirar."
                            ),
                        },
                        "estimulo": {
                            "type": "string",
                            "description": (
                                "Si se prueba: lo que se le pone a la plataforma, "
                                "CONCRETO. El texto literal que se escribe, la "
                                "respuesta que se elige, el botón que se toca. No "
                                "una etiqueta ('probar el feedback'): la acción."
                            ),
                        },
                        "tipo_de_regla": {
                            "type": "string", "enum": [EXISTENCIA, COMPORTAMIENTO],
                            "description": (
                                "existencia: la regla pregunta si algo existe en "
                                "la plataforma (un modo, una pantalla, un control). "
                                "comportamiento: pregunta cómo responde algo que está."
                            ),
                        },
                        "superficie_no_vista": {
                            "type": "string",
                            "description": (
                                "Sólo para reglas de existencia: si lo que se "
                                "busca podría vivir en un lugar que el recorrido "
                                "NO registró (lo que aparece al empezar una sesión, "
                                "al terminarla, una sección sin recorrer), decí "
                                "cuál. Vacío si todos los lugares donde viviría "
                                "están entre las pantallas vistas."
                            ),
                        },
                        "motivo": {
                            "type": "string",
                            "description": "Por qué este estado. Obligatorio si no se prueba.",
                        },
                        "documento": {
                            "type": "string",
                            "description": (
                                "Si necesita un documento: cuál, dicho de forma "
                                "que el proveedor sepa qué mandar."
                            ),
                        },
                    },
                    "required": ["id", "estado", "motivo"],
                },
            },
        },
        "required": ["decisiones"],
    },
}

STRATEGY_SYSTEM = (
    "Sos el PLANIFICADOR de una evaluación de plataformas educativas. Ya se "
    "recorrió la plataforma: tenés la lista de pantallas que se vieron (con un "
    "id cada una), los comportamientos que se averiguaron haciendo y lo que el "
    "recorrido NO pudo cubrir. Con eso decidís, criterio por criterio, la "
    "estrategia. Una persona va a leer tu plan con las capturas al lado y lo va "
    "a corregir antes de que se gaste en correr nada, así que lo que más vale es "
    "que diga la verdad sobre lo que se puede y lo que no.\n\n"
    "Para cada criterio elegí UN estado:\n"
    "- se_prueba: se puede comprobar operando la plataforma. Citá los ids de las "
    "pantallas donde se hace (obligatorio: sin una pantalla vista no hay "
    "prueba) y decí qué se va a hacer y qué se va a mirar.\n"
    "- necesita_documento: sólo lo zanja un documento del proveedor (una "
    "política, un registro, un contrato, una especificación). Decí cuál.\n"
    "- necesita_estudio: exige un relevamiento con personas o datos de uso; "
    "ninguna pantalla ni documento lo contesta.\n"
    "- no_se_puede_medir: operando el navegador no hay forma de observarlo "
    "(por ejemplo, un tiempo que la pantalla no muestra). Decí qué haría falta. "
    "Quien ejecuta las pruebas toca, escribe y guarda captura y texto de la "
    "página: NO mide tiempos ni latencias, no graba video ni audio. Un criterio "
    "que sólo se zanja cronometrando va acá, no a se_prueba.\n"
    "- falta_recorrer: el criterio se podría probar en una parte que el "
    "recorrido vio y NO recorrió, o que no encontró y podría existir. No lo "
    "des por inexistente: decí qué parte habría que mirar. Es lo que la persona "
    "va a corregir.\n\n"
    "Reglas:\n"
    "- Citá sólo ids que figuren en la lista. No completes con lo que una "
    "plataforma así 'suele tener'.\n"
    "- Si un comportamiento ya observado da indicios sobre el criterio, citalo: "
    "no reemplaza la prueba, pero dice dónde mirar.\n"
    "- Un documento del proveedor que se adjuntó sirve para criterios "
    "documentales; lo que el documento afirma sobre la plataforma y nadie vio "
    "en pantalla NO convierte un criterio en 'se_prueba'.\n"
    "- Lo que escribe en el chat de un tutor o cambia una clase real se puede "
    "probar; el riesgo lo clasifica otra etapa. No lo descartes por eso.\n"
    "- Para cada criterio que se prueba escribí el ESTÍMULO concreto: qué se "
    "escribe, qué se elige, qué se toca. Es lo que la persona lee para saber "
    "qué se le va a hacer a la plataforma.\n"
    "- Decí si la regla es de EXISTENCIA o de comportamiento. Encontrar algo "
    "una vez prueba que existe; no encontrarlo en un lugar no prueba que no "
    "exista. Si es de existencia y lo que se busca podría estar en un lugar que "
    "el recorrido no registró (el comienzo de una sesión, su cierre, una "
    "sección que quedó sin recorrer), nombralo en `superficie_no_vista`.\n"
    "Respondés con la herramienta, una decisión por criterio recibido."
)


class PlannerError(EvalkitError):
    """El plan no se pudo armar."""


def _tool_input(response: Any, tool_name: str) -> dict:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(block.input)
    raise PlannerError(
        f'el modelo no usó la herramienta "{tool_name}": no se acepta una '
        "respuesta libre como si fuera una estrategia"
    )


def _default_state(criterion: dict) -> tuple[str, str]:
    obs = str(criterion.get("observability") or "")
    state = _FROM_OBSERVABILITY.get(obs)
    if state:
        return state, str(
            criterion.get("observabilityReason") or criterion.get("evidenciaRequerida") or ""
        ).strip() or "clasificación del marco de criterios"
    return FALTA_RECORRER, "el planificador no emitió una decisión para este criterio"


#: Un criterio que pide un cronómetro: lo dice su verificación ("cronometrar")
#: o su umbral está en segundos o milisegundos ("<1s", "200 ms"). Un umbral en
#: minutos u horas NO entra: "el docente lo ve en menos de 5 minutos" se
#: comprueba recargando la pantalla, sin medir nada.
_STOPWATCH = re.compile(
    r"cronometr|milisegund|medir (el tiempo|la latencia)|"
    r"(?<![\w.,])\d+(?:[.,]\d+)?\s*(ms|s|seg|segundos?)\b",
    re.IGNORECASE,
)

STOPWATCH_REASON = (
    "pide cronometrar o un umbral en segundos, y quien ejecuta no mide tiempos: "
    "opera la página y guarda captura y texto"
)


def needs_stopwatch(criterion: dict) -> bool:
    """¿Este criterio sólo se puede decidir midiendo tiempos?

    POR QUÉ. En la corrida completa del 2026-11-25 el criterio "Latencia <1s"
    (verificación: "cronometrar respuesta a feedback") quedó como `se_prueba`,
    se redactaron dos pruebas que pedían grabar la pantalla y las dos se
    cortaron: gasto seguro, veredicto imposible. El planificador tiene la regla
    escrita en su prompt; acá va la parte que el código puede comprobar solo.
    """
    text = " | ".join(
        str(criterion.get(k) or "") for k in ("verification", "passCriteria", "failCriteria")
    )
    return bool(_STOPWATCH.search(text))


def reclassify_unmeasurable(decisions: list[dict], criteria: list[dict]) -> list[str]:
    """Pasa a `no_se_puede_medir` lo que `needs_stopwatch` detecta. En el lugar.

    Sirve para un plan que ya está guardado (hecho antes de esta regla): se
    llama al redactar desde el plan, así no se pagan pruebas que mueren seguro.
    Devuelve los ids que cambió.
    """
    by_id = {str(c.get("id")): c for c in criteria or []}
    changed: list[str] = []
    for d in decisions or []:
        if d.get("estado") != SE_PRUEBA or not needs_stopwatch(by_id.get(str(d.get("id")), {})):
            continue
        d["estado"] = NO_SE_PUEDE_MEDIR
        d["cubeta"] = BUCKET[NO_SE_PUEDE_MEDIR]
        d["motivo"] = STOPWATCH_REASON
        d.setdefault("advertencias", []).append(
            "decía 'se prueba', pero " + STOPWATCH_REASON + ": pasa a no se puede medir"
        )
        changed.append(str(d.get("id")))
    return changed


def validate_strategy(decisions: Any, criteria: list[dict], profile: dict | None) -> list[dict]:
    """Las decisiones del modelo, pasadas por lo que el código no le cree.

    Devuelve UNA decisión por criterio, en el orden de `criteria`.
    """
    profile = profile or {}
    part_ids = {str(p.get("id")) for p in profile.get("partes") or [] if p.get("id")}
    behavior_ids = {str(b.get("id")) for b in profile.get("comportamientos") or [] if b.get("id")}
    by_id: dict[str, dict] = {}
    for d in decisions or []:
        if isinstance(d, dict) and str(d.get("id") or "").strip():
            by_id.setdefault(str(d["id"]).strip(), d)

    out: list[dict] = []
    for criterion in criteria:
        cid = str(criterion.get("id"))
        raw = by_id.get(cid)
        notes: list[str] = []
        if raw is None or raw.get("estado") not in MODEL_STATES:
            state, reason = _default_state(criterion)
            decision = {"estado": state, "motivo": reason, "partes": [], "comportamientos": [],
                        "que_se_prueba": "", "documento": "", "estimulo": "",
                        "tipo_de_regla": "", "superficie_no_vista": ""}
            if raw is None:
                notes.append("sin decisión del planificador: hereda la clasificación del marco")
        else:
            cited = [str(x).strip() for x in (raw.get("partes") or [])]
            parts = [x for x in cited if x in part_ids]
            invented = [x for x in cited if x and x not in part_ids]
            if invented:
                notes.append(
                    "el planificador citó pantallas que el recorrido no registró ("
                    + ", ".join(invented) + "): se descartaron"
                )
            decision = {
                "estado": raw["estado"],
                "motivo": str(raw.get("motivo") or "").strip(),
                "partes": parts,
                "comportamientos": [
                    str(x).strip() for x in (raw.get("comportamientos") or [])
                    if str(x).strip() in behavior_ids
                ],
                "que_se_prueba": str(raw.get("que_se_prueba") or "").strip(),
                "documento": str(raw.get("documento") or "").strip(),
                "estimulo": str(raw.get("estimulo") or "").strip(),
                "tipo_de_regla": (
                    raw.get("tipo_de_regla")
                    if raw.get("tipo_de_regla") in (EXISTENCIA, COMPORTAMIENTO) else ""
                ),
                "superficie_no_vista": str(raw.get("superficie_no_vista") or "").strip(),
            }
            if decision["estado"] == SE_PRUEBA and not parts:
                # UNA PRUEBA SIN PANTALLA NO ES UNA PRUEBA. Es exactamente el
                # plan a ciegas que costó la corrida del 2026-11-25: se
                # redactaría un camino hacia algo que nadie vio.
                decision["estado"] = FALTA_RECORRER
                notes.append(
                    "decía 'se prueba' sin una pantalla del recorrido que lo "
                    "respalde: pasa a indefinido hasta que alguien diga dónde está"
                )
        if decision["estado"] == SE_PRUEBA and needs_stopwatch(criterion):
            decision["estado"] = NO_SE_PUEDE_MEDIR
            decision["motivo"] = STOPWATCH_REASON
            notes.append(
                "decía 'se prueba', pero " + STOPWATCH_REASON + ": pasa a no se puede medir"
            )
        # Un pedido de documento sin decir CUÁL no se le puede mandar a nadie.
        # Vale también para el criterio que heredó su estado del marco.
        if decision["estado"] == NECESITA_DOCUMENTO and not decision["documento"]:
            decision["documento"] = (
                str(criterion.get("evidenciaRequerida") or "").strip()
                or "documento del proveedor que respalde este criterio"
            )
        decision.update({
            "id": cid,
            "area": str(criterion.get("area") or ""),
            "nombre": str(criterion.get("name") or ""),
            "cubeta": BUCKET[decision["estado"]],
            "advertencias": notes,
        })
        out.append(decision)
    return out


def _criteria_block(criteria: list[dict]) -> str:
    lines = []
    for c in criteria:
        lines.append(
            f"- {c.get('id')} | área {c.get('area')} | {c.get('name')}\n"
            f"  cómo se verifica: {c.get('verification') or '(no dice)'}\n"
            f"  cumple si: {c.get('passCriteria') or '(no dice)'}\n"
            f"  clasificación previa del marco: {c.get('observability') or '(sin clasificar)'}"
        )
    return "\n".join(lines)


def evaluate_strategy(
    criteria: list[dict],
    *,
    profile: dict | None,
    profile_text: str,
    client: Any,
    model: str,
    documents: list[str] | None = None,
    on_progress: Any = None,
    max_tokens: int = 16000,
    revisited: dict[str, str] | None = None,
) -> list[dict]:
    """El planificador decide, criterio por criterio, qué se prueba y qué no.

    `revisited` se pasa en la SEGUNDA decisión, después del recorrido dirigido
    (ver recon.revisit y `settle_revisited`): dice, por criterio, cómo terminó
    la exploración que fue a buscar su pantalla.
    """
    if not criteria:
        return []
    second = ""
    if revisited:
        listed = "\n".join(f"- {cid}: {how}" for cid, how in revisited.items())
        second = (
            "\n\nSEGUNDA DECISIÓN. Estos criterios habían quedado en "
            "`falta_recorrer` y ya se hizo una exploración dirigida a buscar la "
            "pantalla que faltaba; lo que encontró está arriba, en las partes y "
            "comportamientos más nuevos y en lo que se buscó y no se encontró. "
            f"Cómo terminó cada exploración:\n{listed}\n"
            "Decidí de nuevo. Si la pantalla apareció, es `se_prueba` y citá su "
            "id. Quien ejecuta las pruebas SÍ puede escribir (el explorador no): "
            "si la pantalla existe y lo único que faltó fue escribir para "
            "avanzar, es `se_prueba`. Si la exploración terminó y la pantalla no "
            "existe, es `no_se_puede_medir` y el motivo dice qué se buscó, por "
            "dónde y qué había en su lugar. `falta_recorrer` sólo vale para un "
            "criterio cuya exploración no se completó."
        )
    docs = (
        "\n\nDOCUMENTOS DEL PROVEEDOR ADJUNTOS A ESTA EVALUACIÓN: "
        + "; ".join(documents) + "."
        if documents else
        "\n\nNo se adjuntó ningún documento del proveedor."
    )
    raw: list[dict] = []
    chunks = [criteria[i:i + _CHUNK] for i in range(0, len(criteria), _CHUNK)]
    for index, chunk in enumerate(chunks, start=1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": "high"},
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": STRATEGY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {
                    "type": "text",
                    "text": "LO QUE EL RECORRIDO VIO DE LA PLATAFORMA\n" + profile_text + docs,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "CRITERIOS A DECIDIR\n" + _criteria_block(chunk) + second},
            ]}],
            tools=[STRATEGY_TOOL],
            tool_choice={"type": "tool", "name": STRATEGY_TOOL_NAME},
        )
        raw += list(_tool_input(response, STRATEGY_TOOL_NAME).get("decisiones") or [])
        if on_progress is not None:
            try:
                on_progress(index, len(chunks))
            except Exception:  # noqa: BLE001 -- el aviso no puede tumbar el plan
                pass
    return validate_strategy(raw, criteria, profile)


def settle_revisited(decisions: list[dict], revisited: dict[str, str]) -> list[dict]:
    """Lo que el código no le cree a la segunda decisión. En el lugar.

    Después de una exploración dirigida COMPLETA, "falta recorrer" deja de ser
    una respuesta: o la pantalla apareció (y el criterio se prueba) o no existe
    (y el criterio no se puede medir en esta plataforma, que es un resultado y
    no una falla del evaluador). Si el planificador insiste con `falta_recorrer`
    para un criterio cuya exploración terminó, pasa a `no_se_puede_medir` con el
    motivo que él mismo dio y la aclaración de que se fue a buscar.

    Al revés no se toca: un criterio cuya exploración NO se completó sigue en
    `falta_recorrer`. Decir "no existe" de algo que no se terminó de buscar es
    exactamente la afirmación que este repo no hace.
    """
    for d in decisions or []:
        how = (revisited or {}).get(str(d.get("id")))
        if how is None:
            continue
        notes = d.setdefault("advertencias", [])
        if d.get("estado") == FALTA_RECORRER and how == "hecha":
            d["estado"] = NO_SE_PUEDE_MEDIR
            d["cubeta"] = BUCKET[NO_SE_PUEDE_MEDIR]
            d["motivo"] = (
                "Se fue a buscar con una exploración dirigida y la pantalla "
                "donde se probaría no apareció. " + str(d.get("motivo") or "")
            ).strip()
            notes.append(
                "seguía en 'falta recorrer' después de una exploración dirigida "
                "completa: pasa a no se puede medir en esta plataforma"
            )
        elif d.get("estado") == FALTA_RECORRER:
            notes.append(f"la exploración dirigida no se completó: {how}")
        else:
            notes.append("decidido de nuevo después de una exploración dirigida")
    return decisions


# --- el alcance: de quién es el rol y de qué lugar son los hechos --------------

def cube_of(decision: dict) -> str:
    """En cuál de los cuatro cubos del alcance cae una decisión."""
    state = decision.get("estado")
    if state == MAL_LOCALIZADO:
        return CUBO_MAL_LOCALIZADO
    if state == FUERA_DE_ALCANCE:
        return CUBO_DOCENTE
    if state in (NECESITA_DOCUMENTO, NECESITA_ESTUDIO):
        return CUBO_DOC_ESTUDIO
    return CUBO_PLATAFORMA


def out_of_scope_state(row: dict | None) -> str:
    """El estado que el alcance le impone a un criterio, o "" si no le impone ninguno.

    Mal localizado va primero: un criterio de otra materia que además sería
    del docente no aplica, antes de ser de nadie.
    """
    row = row or {}
    if row.get("localizacion") == usage_mod.LOC_MAL:
        return MAL_LOCALIZADO
    if row.get("rol") == usage_mod.ROL_DOCENTE:
        return FUERA_DE_ALCANCE
    return ""


def _scope_summary(row: dict) -> dict:
    return {
        "rol": row.get("rol") or usage_mod.ROL_PLATAFORMA,
        "rol_motivo": row.get("rol_motivo") or "",
        "hechos": list(row.get("hechos") or []),
        "localizacion": row.get("localizacion") or usage_mod.LOC_OK,
        "localizacion_motivo": row.get("localizacion_motivo") or "",
    }


def _facts_text(facts: list[dict]) -> str:
    out = []
    for f in facts or []:
        if f.get("coincide") == "si":
            out.append(f"{f.get('hecho')}: {f.get('en_el_criterio')} (coincide con el contexto)")
        elif f.get("coincide") == "no":
            there = f.get("en_el_contexto") or "el contexto declarado no lo trae"
            out.append(f"{f.get('hecho')}: el criterio dice «{f.get('en_el_criterio')}»; en este contexto: {there}")
        else:
            out.append(
                f"{f.get('hecho')}: el criterio dice «{f.get('en_el_criterio')}» "
                "(sin contrastar: el contexto declarado no trae este dato)"
            )
    return "; ".join(out)


def with_scope(decisions: list[dict], criteria: list[dict], scope: list[dict] | None) -> list[dict]:
    """LA REGLA DURA: un criterio del docente o mal localizado no se prueba sobre la plataforma.

    POR QUÉ. En la corrida completa del 2026-11-26 cuatro NO_CUMPLE no eran de
    la plataforma (ver evalkit/usage.py, con el caso). Los cuatro se habían
    planeado como "se prueba", se pagaron sus pruebas y el juez, con razón
    según lo que tenía delante, dijo que eso no aparecía en pantalla. El error
    no estaba en el juicio: estaba en haberle preguntado a la plataforma por el
    trabajo de otro, o con el dato de otro país.

    Acá se cierra antes de planear nada. `scope` es la clasificación ya
    validada (usage.validate_scope); `decisions` son las del planificador, que
    puede no haber recibido los criterios fuera de alcance. Sale UNA decisión
    por criterio, en el orden de `criteria`:

      * rol del docente -> `fuera_de_alcance`. No genera prueba. En el informe
        es NO_EVALUADO con "observación de aula" como evidencia requerida.
      * hecho de otro país o materia que no se pudo corregir con el contexto ->
        `mal_localizado`. NO_EVALUADO hasta que el dato esté en el config.
      * todo lo demás queda como lo decidió el planificador, con el alcance
        escrito al lado para que la persona lo vea y lo pueda vetar.

    Si el planificador igual decidió algo para un criterio fuera de alcance
    (un plan viejo, una corrección a mano), el alcance gana y queda anotado
    qué decía: el código lo hace imposible, no improbable.
    """
    rows = {str(r.get("id")): r for r in scope or []}
    have = {str(d.get("id")): d for d in decisions or []}
    out: list[dict] = []
    for criterion in criteria:
        cid = str(criterion.get("id"))
        row = rows.get(cid)
        forced = out_of_scope_state(row)
        current = have.get(cid)
        if not forced:
            if current is None:
                state, reason = _default_state(criterion)
                current = {
                    "id": cid, "area": str(criterion.get("area") or ""),
                    "nombre": str(criterion.get("name") or ""),
                    "estado": state, "motivo": reason, "partes": [], "comportamientos": [],
                    "que_se_prueba": "", "documento": "", "estimulo": "",
                    "tipo_de_regla": "", "superficie_no_vista": "",
                    "advertencias": ["sin decisión del planificador: hereda la clasificación del marco"],
                }
            d = dict(current)
            if d.get("estado") in OUT_OF_SCOPE:
                # Estaba fuera de alcance y ya no (cambió el config o una
                # persona lo vetó): vuelve a indefinido para que se decida de
                # nuevo, en vez de quedar con un estado que nadie sostiene.
                d["estado"] = FALTA_RECORRER
                d["motivo"] = "dejó de estar fuera de alcance y todavía no se decidió cómo se prueba"
            if row:
                d["alcance"] = _scope_summary(row)
                notes = list(d.get("advertencias") or [])
                notes += [n for n in row.get("advertencias") or [] if n not in notes]
                if row.get("localizacion") == usage_mod.LOC_CORREGIDO:
                    fixed_note = (
                        "criterio corregido con los hechos del contexto: "
                        + _facts_text([f for f in row.get("hechos") or [] if f.get("coincide") == "no"])
                    )
                    if fixed_note not in notes:
                        notes.append(fixed_note)
                d["advertencias"] = notes
            d["cubeta"] = BUCKET[d["estado"]]
            d["cubo"] = cube_of(d)
            out.append(d)
            continue

        notes = list((row or {}).get("advertencias") or [])
        if current is not None and current.get("estado") not in OUT_OF_SCOPE:
            notes.append(
                f"el planificador lo había dejado en '{current.get('estado')}': el "
                "alcance manda, no se prueba sobre la plataforma"
            )
        if forced == FUERA_DE_ALCANCE:
            reason = (
                "En el modelo de uso declarado este comportamiento lo hace el "
                "docente, no la plataforma. " + str(row.get("rol_motivo") or "")
                + " Que no aparezca en pantalla es el diseño, no una falla: "
                "probarlo sobre la plataforma sería medir a otro."
            ).replace("  ", " ").strip()
            needed = usage_mod.EVIDENCIA_DOCENTE
        else:
            facts = [f for f in row.get("hechos") or [] if f.get("coincide") == "no"]
            reason = (
                "La regla de este criterio depende de hechos de otro país o de "
                "otra materia"
                + (": " + _facts_text(facts) if facts else "")
                + ". " + str(row.get("localizacion_motivo") or "")
            ).strip()
            needed = (
                "el dato equivalente de este contexto, declarado en el config de "
                "la plataforma (bloque contexto), o un criterio escrito para esta "
                "materia y este país"
            )
        d = {
            "id": cid,
            "area": str(criterion.get("area") or ""),
            "nombre": str(criterion.get("name") or ""),
            "estado": forced,
            "motivo": reason,
            "partes": [], "comportamientos": [],
            "que_se_prueba": "", "documento": "", "estimulo": "",
            "tipo_de_regla": "", "superficie_no_vista": "",
            "evidencia_requerida": needed,
            "alcance": _scope_summary(row),
            "cubeta": BUCKET[forced],
            "advertencias": notes,
        }
        d["cubo"] = cube_of(d)
        out.append(d)
    return out


def enforce_scope(decisions: list[dict], criteria: list[dict], scope: list[dict] | None) -> list[str]:
    """Aplica `with_scope` sobre un plan ya guardado. En el lugar; devuelve qué cambió.

    Para el plan hecho antes de esta regla, o editado a mano después: se llama
    al redactar desde el plan, igual que `reclassify_unmeasurable`, así no se
    paga una prueba que no puede terminar en un veredicto válido.
    """
    if not scope:
        return []
    before = {str(d.get("id")): d.get("estado") for d in decisions or []}
    known = {str(c.get("id")) for c in criteria or []}
    fresh = {d["id"]: d for d in with_scope(
        [d for d in decisions or [] if str(d.get("id")) in known], criteria, scope
    )}
    changed = []
    for i, d in enumerate(decisions or []):
        new = fresh.get(str(d.get("id")))
        if new is None:
            continue
        if new["estado"] in OUT_OF_SCOPE:
            new.update({"n": 0, "celdas": [], "sensible_al_tema": None, "revisar": False})
        decisions[i] = new
        if before.get(new["id"]) != new["estado"]:
            changed.append(new["id"])
    return changed


def absence_gap(decision: dict | None, judged: int) -> str:
    """Por qué una ausencia NO alcanza para un NO_CUMPLE. "" si alcanza.

    POR QUÉ. Un criterio de la corrida del 2026-11-26 preguntaba si existe un
    modo de práctica sin ayuda. La prueba miró un lugar, no vio el rótulo y el
    veredicto fue NO_CUMPLE con una sola prueba. Pero para una regla de
    existencia las dos respuestas no pesan igual: encontrarlo una vez prueba
    que existe; no encontrarlo en un lugar no prueba que no exista. Si quedó
    sin mirar un lugar donde podría estar (el comienzo o el cierre de una
    sesión, que el recorrido no siempre registra), o si la ausencia se vio en
    una sola prueba, el veredicto honesto es NO_EVALUADO.

    Sólo aplica a reglas que el plan marcó como de existencia. Una regla de
    comportamiento ("¿el tutor nombra el error?") sí se contesta con un NO en
    la pantalla donde debía pasar.

    Y sólo cuando el veredicto del criterio entero es NO_CUMPLE, es decir,
    cuando NINGUNA prueba juzgada lo encontró. Si unas lo encontraron y otras
    no, el criterio sigue en "x de n" (PARCIAL): cada celda mira un contenido
    distinto y "existe en este tema y no en aquel" es un resultado, no una
    ausencia sin cobertura. `judged` cuenta las pruebas juzgadas; las cortadas
    no suman.
    """
    d = decision or {}
    if d.get("tipo_de_regla") != EXISTENCIA:
        return ""
    unseen = str(d.get("superficie_no_vista") or "").strip()
    if unseen:
        return (
            "la regla pregunta si algo existe y quedó sin alcanzar un lugar donde "
            f"podría estar ({unseen}): no encontrarlo donde se miró no prueba que no exista"
        )
    if judged < MIN_PROBES_FOR_ABSENCE:
        return (
            "la regla pregunta si algo existe y la ausencia se observó en "
            f"{judged} prueba: una presencia se muestra una vez, una ausencia "
            f"necesita cobertura (al menos {MIN_PROBES_FOR_ABSENCE} pruebas)"
        )
    return ""


_ROLE_TEXT = {
    usage_mod.ROL_PLATAFORMA: "La plataforma",
    usage_mod.ROL_DOCENTE: "El docente",
    usage_mod.ROL_COMPARTIDO: "Compartido entre docente y plataforma",
}


def ficha(decision: dict, criterion: dict | None, surfaces: list[dict] | None = None) -> dict:
    """Los cinco campos que el revisor lee, por criterio, ANTES de aprobar y gastar.

    Es prevención en el plan en vez de detección en el informe: con estos cinco
    campos a la vista, "esto lo hace el docente" o "ese número es de otro país"
    se ven leyendo, no después de pagar la corrida.

      1. stress_test: qué se le pone a la plataforma, concreto.
      2. como_se_juzga: la regla, qué aprueba, qué falla y qué evidencia decide.
      3. rol_y_superficie: de quién es el comportamiento y en qué pantalla
         debería aparecer.
      4. hechos_localizados: cada dato por país, materia o currículo del que
         depende la regla, contrastado con el contexto.
      5. que_falta: si no es observable, qué documento, estudio u observación
         lo zanjaría.

    Todo sale de datos que ya están en la decisión y en el criterio: no hay una
    llamada al modelo acá, y un campo sin dato lo dice.
    """
    c = criterion or {}
    state = decision.get("estado")
    scope = decision.get("alcance") or {}
    by_id = {s.get("id"): s for s in surfaces or []}
    tested = state == SE_PRUEBA

    if tested:
        stress = str(decision.get("estimulo") or "").strip() or (
            str(decision.get("que_se_prueba") or "").strip()
            or "(el planificador no escribió el estímulo: pídalo antes de aprobar)"
        )
        cells = [
            " · ".join(x for x in (str(cell.get("tema") or ""), str(cell.get("variante") or "")) if x)
            for cell in decision.get("celdas") or []
        ]
        cells = [x for x in cells if x]
        if cells:
            stress += " Se repite en: " + " | ".join(cells) + "."
    else:
        stress = "No se le pone nada a la plataforma: este criterio no se prueba recorriéndola."

    rule = str(c.get("verification") or c.get("name") or "").strip()
    judged = f"Regla: {rule or '(el criterio no la trae)'}"
    if c.get("passCriteria"):
        judged += f" Cumple si: {c['passCriteria']}"
    if c.get("failCriteria"):
        judged += f" No cumple si: {c['failCriteria']}"
    if tested:
        judged += " Decide la captura y el texto de la pantalla después del estímulo."
        if decision.get("tipo_de_regla") == EXISTENCIA:
            judged += (
                " Es una regla de existencia: un NO_CUMPLE exige al menos "
                f"{MIN_PROBES_FOR_ABSENCE} pruebas y haber alcanzado todo lugar "
                "donde podría estar; si no, queda sin evaluar."
            )
    else:
        judged += " En esta evaluación queda sin evaluar; no cuenta como un cero."

    role = _ROLE_TEXT.get(scope.get("rol") or usage_mod.ROL_PLATAFORMA, "La plataforma")
    if not scope:
        role += " (supuesto por defecto: el config no declara modelo de uso)"
    elif scope.get("rol_motivo"):
        role += f". {scope['rol_motivo']}"
    where = "; ".join(
        f"{pid} {by_id.get(pid, {}).get('nombre', '')}".strip()
        for pid in decision.get("partes") or []
    )
    role += f" Superficie: {where}." if where else " Superficie: ninguna pantalla del recorrido."
    if decision.get("superficie_no_vista"):
        role += f" Podría estar también en un lugar que no se recorrió: {decision['superficie_no_vista']}."

    facts = _facts_text(scope.get("hechos") or [])
    if not facts:
        facts = (
            "Ninguno: la regla no depende de datos por país, materia o currículo."
            if scope else
            "Sin contrastar: el config no declara el contexto (país, materia, líneas de crisis)."
        )

    if tested:
        missing = "Nada: es observable recorriendo la plataforma."
    else:
        missing = str(
            decision.get("documento") or decision.get("evidencia_requerida")
            or decision.get("motivo") or ""
        ).strip() or "(sin dato)"
    return {
        "stress_test": stress,
        "como_se_juzga": judged,
        "rol_y_superficie": role,
        "hechos_localizados": facts,
        "que_falta": missing,
    }


def reconcile(decisions: list[dict], matrix: dict[str, dict]) -> list[dict]:
    """El cierre conjunto: el planificador integra la matriz del muestreo.

    No es un pase de mano. El muestreo puede objetar un criterio ("no se puede
    muestrear como está planteado") y esa objeción cambia el plan: el criterio
    sigue en "se prueba" con una sola prueba y queda marcado para que la persona
    lo mire, con la objeción escrita. Lo que NO puede hacer el muestreo es sacar
    un criterio del plan ni agregar uno: eso es del planificador.
    """
    out = []
    for decision in decisions:
        d = dict(decision)
        if d["estado"] != SE_PRUEBA:
            d.update({"n": 0, "celdas": [], "sensible_al_tema": None, "revisar": False})
            out.append(d)
            continue
        row = matrix.get(d["id"])
        notes = list(d.get("advertencias") or [])
        if row is None:
            row = {"sensible_al_tema": False, "n": 1, "celdas": [{"tema": "", "variante": "única"}],
                   "motivo": "", "confirmado": True, "objecion": "", "advertencias": []}
            notes.append("sin propuesta de muestreo: queda una prueba")
        notes += row.get("advertencias") or []
        review = False
        cells = list(row["celdas"])
        if not row.get("confirmado", True):
            review = True
            cells = cells[:1]
            notes.append("el muestreo objetó este criterio: " + row.get("objecion", ""))
        if any(c.get("tema_no_visto") for c in cells):
            review = True
            notes.append("hay temas propuestos que el recorrido no vio")
        # Una regla de existencia que no puede terminar en NO_CUMPLE se dice
        # ANTES de pagar sus pruebas (ver `absence_gap`).
        gap = absence_gap(d, len(cells))
        if gap:
            review = True
            notes.append("así planeado, una ausencia quedaría sin evaluar: " + gap)
        d.update({
            "n": len(cells),
            "celdas": cells,
            "sensible_al_tema": bool(row.get("sensible_al_tema")),
            "motivo_muestreo": row.get("motivo", ""),
            "revisar": review,
            "advertencias": notes,
        })
        out.append(d)
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_plan(
    *,
    platform: str,
    url: str,
    objective: str,
    profile: dict | None,
    decisions: list[dict],
    documents: list[str] | None = None,
    spend_usd: float = 0.0,
    version: int = 1,
    corrections: list[dict] | None = None,
    criteria: list[dict] | None = None,
    usage: dict | None = None,
) -> dict:
    """El plan como DATOS: lo que el documento muestra y lo que la web va a editar.

    Lleva las tres cubetas, las referencias a la evidencia de cada superficie,
    las repeticiones y los temas (editables), el lugar para las correcciones de
    la persona y la versión, para poder mostrar la diferencia cuando se replanea.

    Con `criteria` y `usage`, cada criterio lleva además su FICHA de cinco
    campos (ver `ficha`) y el plan dice qué modelo de uso y qué contexto se
    declararon. Sin ellos el plan sale igual, y dice que nadie los declaró.
    """
    profile = profile or {}
    parts = [p for p in profile.get("partes") or [] if isinstance(p, dict)]
    cover = profile.get("cobertura") or {}
    by_criterion = {str(c.get("id")): c for c in criteria or []}
    declared = usage_mod.UsageModel.from_dict(usage)
    for d in decisions:
        d["cubo"] = cube_of(d)
        d["ficha"] = ficha(d, by_criterion.get(str(d.get("id"))), parts)

    surfaces = []
    for part in parts:
        feeding = [d for d in decisions if part.get("id") in (d.get("partes") or [])]
        testing = [d for d in feeding if d["estado"] == SE_PRUEBA]
        surfaces.append({
            "id": part.get("id"),
            "nombre": part.get("nombre", ""),
            "rol": part.get("rol", ""),
            "como_llegar": part.get("como_llegar", ""),
            "que_muestra": part.get("que_muestra", ""),
            "que_permite": part.get("que_permite", ""),
            "url": part.get("url", ""),
            "captura": part.get("captura", ""),
            "sha256": part.get("sha256", ""),
            "texto": part.get("texto", ""),
            "criterios": [d["id"] for d in feeding],
            "pruebas": sum(d.get("n", 0) for d in testing),
            # Sin captura es una señal de primera clase, no un detalle: una
            # superficie que nadie puede ver no respalda una prueba.
            "estado": "se_prueba" if testing else ("sin_captura" if not part.get("captura") else "sin_criterios"),
        })
    # Lo que el recorrido VIO y no recorrió también es una fila: es donde la
    # persona dice "eso existe, está acá".
    for text in cover.get("sin_cubrir") or []:
        surfaces.append({
            "id": "", "nombre": text, "rol": "", "captura": "", "criterios": [],
            "pruebas": 0, "estado": "indefinido",
            "que_muestra": "Se vio en el menú y no se recorrió.",
        })

    needed: dict[str, dict] = {}
    for d in decisions:
        if d["estado"] == NECESITA_DOCUMENTO:
            key = d["documento"].strip().casefold()
            item = needed.setdefault(key, {"documento": d["documento"], "criterios": [], "respuesta": None})
            item["criterios"].append(d["id"])

    tested = [d for d in decisions if d["estado"] == SE_PRUEBA]
    probes = sum(d.get("n", 0) for d in tested)
    counts = {
        "se_prueba": len(tested),
        "no_se_puede": sum(1 for d in decisions if d["cubeta"] == "no_se_puede"),
        "indefinido": sum(1 for d in decisions if d["cubeta"] == "indefinido"),
        "fuera_de_alcance": sum(1 for d in decisions if d["cubeta"] == "fuera_de_alcance"),
        "cubos": {cube: sum(1 for d in decisions if d["cubo"] == cube) for cube in CUBOS},
        "documentos": len(needed),
        "criterios_con_documento": sum(1 for d in decisions if d["cubeta"] == "documento"),
        "criterios": len(decisions),
        "pruebas": probes,
        "a_revisar": sum(1 for d in decisions if d.get("revisar")),
    }
    estimate = round(
        probes * USD_PER_PROBE
        + len(tested) * (USD_PER_CRITERION_DRAFT + USD_PER_CRITERION_JUDGE), 2
    )
    return {
        "version": version,
        "generado": _now(),
        "plataforma": {"nombre": platform, "url": url},
        "objetivo": objective,
        # QUÉ SE DECLARÓ SOBRE CÓMO SE USA. Va arriba porque cambia cómo se lee
        # todo lo demás: con "capa de práctica", que la plataforma no explique
        # antes de preguntar deja de ser un hallazgo.
        "modelo_de_uso": {
            "declarado": declared.declared,
            "modelo": declared.modelo,
            "quien_hace_que": declared.quien_hace_que,
            "documento": declared.documento,
            "contexto": usage_mod.context_lines(declared),
        },
        "conteos": counts,
        "recorrido": {
            "entrada": profile.get("entrada", ""),
            "roles": profile.get("roles") or [],
            "cobertura": cover,
            "entradas": profile.get("entradas") or [],
            "comportamientos": profile.get("comportamientos") or [],
            "experimentos": profile.get("experimentos") or [],
            "no_encontrado": profile.get("no_encontrado") or [],
            "documentado_sin_verificar": profile.get("documentado_sin_verificar") or [],
            "sin_explorar": profile.get("sin_explorar") or [],
        },
        "superficies": surfaces,
        "criterios": decisions,
        "documentos_necesarios": list(needed.values()),
        "documentos_recibidos": list(documents or []),
        "reglas": {
            "pruebas_por_criterio": "las de la matriz de muestreo (columna n)",
            "condiciones_de_corte": [
                "Una prueba que no puede completar un paso se corta y su criterio queda sin evaluar, con el paso que falló.",
                "Si la sesión se cae, se detiene la corrida entera.",
                "Una verificación de humano detiene la prueba; no se intenta resolver.",
                "El navegador no sale de las direcciones de la plataforma.",
            ],
            "costo_estimado_usd": estimate,
            "tiempo_estimado_min": round(probes * MINUTES_PER_PROBE),
            "base_de_la_estimacion": (
                f"USD {USD_PER_PROBE:.2f} y {MINUTES_PER_PROBE:g} min por prueba, "
                f"más USD {USD_PER_CRITERION_DRAFT + USD_PER_CRITERION_JUDGE:.2f} "
                "por criterio para redactar y juzgar (medido el 2026-11-25)"
            ),
            "con_compuerta": (
                "Se sabe al redactar los pasos: las pruebas que escriben en una "
                "clase con estudiantes reales, exponen datos de menores o tocan "
                "dinero necesitan una firma por prueba."
            ),
        },
        "gasto_hasta_ahora_usd": round(float(spend_usd or 0.0), 2),
        # Lo que la persona corrige, como datos: {tipo, superficie o criterio,
        # url, nota}. El replaneo las consume (ver `corrections_as_entries`).
        "correcciones": list(corrections or []),
    }


#: Los marcadores que el redactor deja en los pasos y que se reemplazan por
#: los valores de cada celda del muestreo.
MARK_TOPIC = "[TEMA]"
MARK_VARIANT = "[VARIANTE]"


_SOFT_VARIANT = (
    " (Es una orientación: si lo que aparece en pantalla no permite exactamente "
    "esa situación, hacé la más parecida que sí permita, seguí con el paso y "
    "decí en el cierre cuál hiciste. No lo des por no hecho sólo por eso. Y en "
    "ESTE paso respondé una sola pregunta (con todas sus partes, si las "
    "tiene): si la descripción habla de una secuencia entera, el resto lo "
    "cubren los otros pasos de la prueba.)"
)


def guidance_for(decision: dict, surfaces: list[dict] | None = None) -> str:
    """Lo que el plan aprobado decidió para UN criterio, dicho al redactor.

    El redactor escribe los pasos una sola vez; las celdas del muestreo los
    instancian (ver `instantiate`). Así el consejo revisa una prueba por
    criterio y no una por celda, que es lo que mantiene el costo del plan.
    """
    by_id = {s.get("id"): s for s in surfaces or []}
    where = "; ".join(
        f"{pid} ({by_id.get(pid, {}).get('nombre', '')}: {by_id.get(pid, {}).get('como_llegar', '')})"
        for pid in decision.get("partes") or []
    ) or "(el plan no citó pantallas)"
    cells = decision.get("celdas") or []
    listing = "\n".join(
        f"  {i}. tema: {c.get('tema') or '(no depende del tema)'} · variante: {c.get('variante')}"
        for i, c in enumerate(cells, start=1)
    )
    scope = decision.get("alcance") or {}
    extra = ""
    if decision.get("estimulo"):
        extra += f"Estímulo que la persona aprobó: {decision['estimulo']}\n"
    fixed = [f for f in scope.get("hechos") or [] if f.get("coincide") == "no" and f.get("en_el_contexto")]
    if fixed:
        # EL DATO DEL PAÍS VIAJA HASTA LOS PASOS. Un criterio corregido con la
        # línea de crisis de este contexto no sirve si la prueba se redacta
        # buscando la del país para el que se escribió el banco.
        extra += (
            "Hechos de ESTE contexto (usá estos, no los del criterio original): "
            + _facts_text(fixed) + "\n"
        )
    return (
        "EL PLAN APROBADO PARA ESTE CRITERIO (lo decidió el planificador, lo "
        "dimensionó el muestreo y lo aprobó una persona; no lo cambies).\n"
        f"Qué se prueba: {decision.get('que_se_prueba') or '(no dice)'}\n"
        f"{extra}"
        f"En qué pantallas del recorrido: {where}\n"
        f"Esta prueba se va a ejecutar {len(cells) or 1} vez/veces, una por celda:\n"
        f"{listing or '  1. única'}\n\n"
        "Escribí los pasos UNA sola vez, de modo que sirvan para todas las celdas, "
        "usando dos marcadores literales que el ejecutor reemplaza:\n"
        f"- {MARK_TOPIC}: el contenido al que hay que entrar. UN solo paso de "
        f"navegación: '{LONG_MARK} Ir al contenido {MARK_TOPIC} y abrir su "
        "actividad'. El ejecutor recibe la ruta completa del menú con el nombre, "
        "así que NO escribas un paso por nivel. Si el criterio no depende del "
        "tema, no lo uses.\n"
        f"- {MARK_VARIANT}: lo que distingue a la celda (qué error provocar, qué "
        "mensaje escribir, qué momento mirar). Ponelo en el paso que produce el "
        "estímulo, por ejemplo: 'responder de modo que ocurra esto: "
        f"{MARK_VARIANT}'.\n"
        "No escribas el nombre de un tema ni una variante concreta en los pasos: "
        "van los marcadores.\n"
        f"El paso que lleva {MARK_VARIANT} hace UNA acción, como todos. Si una "
        "variante describe varias acciones seguidas, escribí esas acciones como "
        "pasos separados y dejá el marcador sólo en el que cambia entre celdas: "
        "un paso que dice 'aplicar lo siguiente: [VARIANTE]' para una secuencia "
        "entera no entra en los turnos de un paso (en el piloto en vivo cortó la "
        "prueba)."
    )


def instantiate(probe: dict, cells: list[dict] | None) -> list[dict]:
    """Una prueba redactada con marcadores -> una instancia por celda.

    Cada instancia tiene su propio id (`<id>-c1`, `<id>-c2`...) y el mismo
    criterio: la evidencia se junta por criterio y el informe dice "en x de n".
    Dos celdas que dejan los pasos idénticos (el redactor no usó los
    marcadores) son la misma prueba: se ejecuta una sola vez y se dice, en vez
    de correr dos veces lo mismo y contarlo como dos.
    """
    cells = [c for c in (cells or []) if isinstance(c, dict)] or [{"tema": "", "variante": ""}]
    out: list[dict] = []
    seen: set[tuple] = set()
    for k, cell in enumerate(cells, start=1):
        topic = str(cell.get("tema") or "").strip()
        route = [str(x) for x in (cell.get("ruta") or []) if str(x).strip()]
        if topic and len(route) > 1:
            # LA RUTA VIAJA CON EL NOMBRE. Cinco pruebas del 2026-11-25 pidieron
            # un contenido real por el camino equivocado (estaba bajo otro eje);
            # quien ejecuta no tiene que adivinar dónde está.
            topic = f"{topic} (ruta en el menú: {' › '.join(route)})"
        elif not topic:
            topic = "el primer contenido disponible del menú"
        variant = str(cell.get("variante") or "").strip() or "la situación habitual"

        def fill(text: str) -> str:
            return str(text).replace(MARK_TOPIC, topic).replace(MARK_VARIANT, variant)

        def fill_step(text: str) -> str:
            # LA VARIANTE ORIENTA, NO EXIGE. En el piloto en vivo una celda
            # pedía provocar "el error de signo de potencia (-1)^5 = 1" y la
            # plataforma mostró una resta: el agente, con razón, dijo que no
            # podía, y la prueba se cortó. La plataforma elige la pregunta; lo
            # que el muestreo busca es dispersión, no ESE error. En un paso, la
            # variante va con la salida dicha: hacer lo más parecido y seguir.
            if MARK_VARIANT not in str(text):
                return fill(text)
            return fill(text) + _SOFT_VARIANT

        steps = [dict(s, action=fill_step(s.get("action", ""))) for s in probe.get("steps") or []]
        key = tuple(s["action"] for s in steps)
        if key in seen:
            continue
        seen.add(key)
        inst = dict(probe)
        inst.update({
            "probeId": f"{probe['probeId']}-c{k}",
            "templateId": probe["probeId"],
            "steps": steps,
            "goal": fill(probe.get("goal", "")),
            "verdictRule": fill(probe.get("verdictRule", "")),
            "celda": {
                "n": k, "tema": cell.get("tema", ""), "variante": cell.get("variante", ""),
                **({"ruta": route} if route else {}),
            },
        })
        out.append(inst)
    if len(out) < len(cells):
        for inst in out:
            inst["celdasFundidas"] = len(cells) - len(out)
    return out


def corrections_as_entries(corrections: list[dict] | None) -> list[dict]:
    """Las correcciones "eso existe, está acá" como entradas obligatorias del recorrido."""
    out = []
    for c in corrections or []:
        if not isinstance(c, dict) or c.get("tipo") not in ("existe", "falta"):
            continue
        text = str(c.get("superficie") or c.get("nota") or "").strip()
        if text:
            out.append({
                "texto": text, "tipo": "menu", "estado": "pendiente",
                "url": str(c.get("url") or "").strip(),
                "nota": str(c.get("nota") or "").strip(),
                # Lo dijo una persona: no se degrada a "mencionada" aunque el
                # árbol no lo muestre (ver frontier.build_frontier).
                "obligatoria": True,
            })
    return out


def diff_plans(old: dict | None, new: dict) -> dict:
    """Qué cambió entre dos versiones del plan: lo único que hay que volver a autorizar."""
    if not old:
        return {"primera_version": True}
    before = {c["id"]: c for c in old.get("criterios") or []}
    after = {c["id"]: c for c in new.get("criterios") or []}
    moved, resized = [], []
    for cid, c in after.items():
        prev = before.get(cid)
        if prev is None:
            continue
        if prev["estado"] != c["estado"]:
            moved.append({"id": cid, "antes": prev["estado"], "ahora": c["estado"]})
        elif prev.get("n", 0) != c.get("n", 0):
            resized.append({"id": cid, "antes": prev.get("n", 0), "ahora": c.get("n", 0)})
    names_before = {s["nombre"] for s in old.get("superficies") or [] if s.get("captura")}
    names_after = {s["nombre"] for s in new.get("superficies") or [] if s.get("captura")}
    return {
        "primera_version": False,
        "superficies_nuevas": sorted(names_after - names_before),
        "superficies_que_ya_no_estan": sorted(names_before - names_after),
        "criterios_que_cambiaron_de_estado": moved,
        "criterios_con_otro_n": resized,
        "criterios_nuevos": sorted(set(after) - set(before)),
        "criterios_quitados": sorted(set(before) - set(after)),
        "costo_antes_usd": (old.get("reglas") or {}).get("costo_estimado_usd"),
        "costo_ahora_usd": (new.get("reglas") or {}).get("costo_estimado_usd"),
    }


# --- el documento -------------------------------------------------------------

_FULL = (900, 1600, 70)
#: La miniatura de la tabla de alcance. Chica a propósito: es para reconocer la
#: pantalla, no para leerla; la captura entera está una sola vez, en el anexo.
_THUMB = (240, 420, 60)


def _image_data_uri(path: Path, size: tuple[int, int, int] = _FULL) -> str:
    """La captura embebida. Reducida si se puede: el documento viaja por correo."""
    width, max_height, quality = size
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            if im.width > width:
                im = im.resize((width, int(im.height * width / im.width)))
            # Una captura de página entera puede ser muy larga: se muestra el
            # comienzo, que es donde está el menú y lo que la identifica.
            if im.height > max_height:
                im = im.crop((0, 0, im.width, max_height))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 -- sin Pillow va el PNG tal cual
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


_ROLE_LABEL = {
    "estudiante": "Estudiante", "docente": "Docente", "administrador": "Administrador",
    "publico": "Público", "otro": "Otro",
}

_STATE_LABEL = {
    SE_PRUEBA: "Se prueba",
    NECESITA_DOCUMENTO: "Necesita un documento",
    NECESITA_ESTUDIO: "Necesita un estudio",
    NO_SE_PUEDE_MEDIR: "No se puede medir",
    FALTA_RECORRER: "Indefinido: falta recorrer",
    FUERA_DE_ALCANCE: "Fuera de alcance: rol del docente",
    MAL_LOCALIZADO: "Mal localizado: otro país o materia",
}

_FICHA_LABELS = (
    ("stress_test", "1. Qué se le pone a la plataforma"),
    ("como_se_juzga", "2. Cómo se juzga"),
    ("rol_y_superficie", "3. De quién es el rol y dónde aparece"),
    ("hechos_localizados", "4. Hechos por país, materia o currículo"),
    ("que_falta", "5. Qué falta si no es observable"),
)


def _ficha_table(c: dict) -> str:
    card = c.get("ficha") or {}
    if not card:
        return ""
    rows = "".join(
        f"<tr><th>{_e(label)}</th><td>{_e(card.get(key) or '(sin dato)')}</td></tr>"
        for key, label in _FICHA_LABELS
    )
    return f"<table class='ficha'>{rows}</table>"

_CSS = """
:root{--ink:#1c2430;--mute:#5b6675;--line:#d9dee5;--ok:#1f7a4d;--okbg:#e6f4ec;
--no:#8a3a2f;--nobg:#f7e9e6;--un:#8a6a12;--unbg:#fbf3d9;--doc:#2b5ea7;--docbg:#e7eef9;--bg:#f6f7f9}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 Segoe UI,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:14px 28px;z-index:5}
header h1{font-size:19px;margin:0 0 2px}header .meta{color:var(--mute);font-size:13px}
.counts{display:flex;gap:10px;flex-wrap:wrap;margin-top:10px}
.count{border-radius:8px;padding:8px 14px;min-width:150px}.count b{font-size:24px;display:block;line-height:1.1}
.c-ok{background:var(--okbg);color:var(--ok)}.c-no{background:var(--nobg);color:var(--no)}
.c-un{background:var(--unbg);color:var(--un)}.c-doc{background:var(--docbg);color:var(--doc)}
.c-cost{background:#eef0f3;color:var(--ink)}
main{max-width:1180px;margin:0 auto;padding:22px 28px 60px}
h2{font-size:17px;margin:34px 0 6px;border-bottom:2px solid var(--line);padding-bottom:6px}
h2 small{font-weight:400;color:var(--mute);font-size:13px;margin-left:8px}
p.lead{color:var(--mute);margin:4px 0 14px}
.alert{background:var(--unbg);border-left:4px solid var(--un);padding:10px 14px;border-radius:4px;margin:10px 0}
.alert.bad{background:var(--nobg);border-color:var(--no)}.alert.good{background:var(--okbg);border-color:var(--ok)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line)}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line);font-size:14px}
th{background:#eef0f3;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--mute)}
.tag{display:inline-block;border-radius:10px;padding:1px 9px;font-size:12px;font-weight:600;white-space:nowrap}
.t-ok{background:var(--okbg);color:var(--ok)}.t-no{background:var(--nobg);color:var(--no)}
.t-un{background:var(--unbg);color:var(--un)}.t-doc{background:var(--docbg);color:var(--doc)}
.t-role{background:#eef0f3;color:var(--mute)}
.t-out{background:#ece7f6;color:#4b3a86}.c-out{background:#ece7f6;color:#4b3a86}
table.ficha{margin:8px 0}table.ficha th{width:230px;text-transform:none;letter-spacing:0;font-size:13px;color:var(--ink)}
img.thumb{width:150px;border:1px solid var(--line);border-radius:4px;display:block}
.noshot{width:150px;height:84px;border:2px dashed var(--no);border-radius:4px;color:var(--no);
display:flex;align-items:center;justify-content:center;font-size:12px;text-align:center;padding:6px}
details{background:#fff;border:1px solid var(--line);border-radius:6px;margin:8px 0;padding:0 14px}
details>summary{cursor:pointer;padding:10px 0;font-weight:600}
details img.full{max-width:100%;border:1px solid var(--line);margin:8px 0}
.small{font-size:12px;color:var(--mute);word-break:break-all}
ul{margin:6px 0 10px 20px;padding:0}li{margin:2px 0}
.cells td{font-size:13px}.how{background:#fff;border:1px dashed var(--line);padding:12px 14px;border-radius:6px}
code{background:#eef0f3;padding:1px 5px;border-radius:3px;font-size:13px}
@media print{header{position:static}details{break-inside:avoid}}
"""


def _tag(state: str) -> str:
    cls = {"se_prueba": "t-ok", "no_se_puede": "t-no", "indefinido": "t-un", "documento": "t-doc",
           "fuera_de_alcance": "t-out"}
    return f'<span class="tag {cls.get(BUCKET.get(state, state), "t-un")}">{_e(_STATE_LABEL.get(state, state))}</span>'


def render_plan_html(plan: dict, artifacts_dir: Path | str | None = None) -> str:
    """El plan como un documento que se abre con doble clic.

    Autocontenido (las capturas van adentro) y sin scripts: se puede mandar por
    correo y se lee igual dentro de un año. La edición en línea (corregir una
    superficie, cambiar las repeticiones) es de la interfaz web; este documento
    dice cómo mandar las correcciones mientras tanto.
    """
    artifacts = Path(artifacts_dir) if artifacts_dir else None
    counts = plan.get("conteos") or {}
    rules = plan.get("reglas") or {}
    recon = plan.get("recorrido") or {}
    cover = recon.get("cobertura") or {}
    criteria = plan.get("criterios") or []
    by_id = {c["id"]: c for c in criteria}
    surfaces = plan.get("superficies") or []
    # CADA CAPTURA ENTRA UNA VEZ. El primer documento generado en vivo pesó
    # 9 MB porque repetía la captura de una pantalla en cada criterio que la
    # citaba (unas 150 imágenes para 30 pantallas). Ahora: una miniatura en la
    # tabla de alcance y la captura entera una sola vez en el anexo; los
    # criterios la enlazan.
    images: dict[str, str] = {}
    thumbs: dict[str, str] = {}
    if artifacts is not None:
        for s in surfaces:
            if s.get("captura") and s["captura"] not in images:
                images[s["captura"]] = _image_data_uri(artifacts / s["captura"])
                thumbs[s["captura"]] = _image_data_uri(artifacts / s["captura"], _THUMB)
        for b in recon.get("comportamientos") or []:
            if b.get("captura") and b["captura"] not in images:
                images[b["captura"]] = _image_data_uri(artifacts / b["captura"])

    def shot(name: str, cls: str = "thumb", anchor: str = "") -> str:
        uri = (thumbs if cls == "thumb" else images).get(name or "")
        if not uri:
            return '<div class="noshot">sin captura</div>'
        img = f'<img class="{cls}" src="{uri}" alt="captura">'
        return f'<a href="#cap-{_e(anchor)}">{img}</a>' if anchor else img

    o: list[str] = []
    w = o.append
    w("<!doctype html><html lang='es'><head><meta charset='utf-8'>")
    w(f"<title>Plan de evaluación: {_e(plan['plataforma']['nombre'])}</title>")
    w(f"<style>{_CSS}</style></head><body>")

    # --- banda de cabecera ---
    w("<header>")
    w(f"<h1>Plan de evaluación: {_e(plan['plataforma']['nombre'])}</h1>")
    w(
        f"<div class='meta'>{_e(plan['plataforma']['url'])} · versión {_e(plan.get('version'))} · "
        f"recorrido y plan del {_e(plan.get('generado'))} · gastado hasta ahora: "
        f"USD {plan.get('gasto_hasta_ahora_usd', 0):.2f}. Nada se ha ejecutado todavía.</div>"
    )
    w("<div class='counts'>")
    w(f"<div class='count c-ok'><b>{counts.get('se_prueba', 0)}</b>criterios se prueban</div>")
    w(f"<div class='count c-no'><b>{counts.get('no_se_puede', 0)}</b>no se pueden evaluar</div>")
    w(f"<div class='count c-un'><b>{counts.get('indefinido', 0)}</b>indefinidos (para corregir)</div>")
    if counts.get("fuera_de_alcance"):
        w(
            f"<div class='count c-out'><b>{counts['fuera_de_alcance']}</b>fuera del alcance "
            "de la plataforma (rol del docente o de otro contexto)</div>"
        )
    w(
        f"<div class='count c-doc'><b>{counts.get('documentos', 0)}</b>documentos necesarios "
        f"({counts.get('criterios_con_documento', 0)} criterios)</div>"
    )
    w(
        f"<div class='count c-cost'><b>USD {rules.get('costo_estimado_usd', 0):.0f}</b>"
        f"estimado para correr {counts.get('pruebas', 0)} pruebas, "
        f"unos {rules.get('tiempo_estimado_min', 0)} min</div>"
    )
    w("</div></header><main>")

    # --- el supuesto de toda la evaluación: cómo se usa la plataforma ---
    use = plan.get("modelo_de_uso") or {}
    w("<h2>Modelo de uso y contexto<small>lo que declara el config de la plataforma</small></h2>")
    if not use.get("declarado"):
        w(
            "<div class='alert bad'><b>Nadie declaró cómo se usa esta plataforma ni en qué país "
            "y materia.</b> El plan supone que la plataforma enseña sola y que los datos de cada "
            "criterio (líneas de crisis, currículo, idioma) son los de este contexto. Si el "
            "docente hace una parte, o si el banco de criterios se escribió para otro país, "
            "declárelo en el config (<code>modelo_de_uso</code> y <code>contexto</code>) antes "
            "de aprobar: de lo contrario un criterio del docente puede terminar como una falla "
            "de la plataforma.</div>"
        )
    else:
        model_text = usage_mod._MODEL_TEXT.get(use.get("modelo") or "", "no declarado")
        w(f"<p><b>Modelo de uso:</b> {_e(model_text)}.</p>")
        if use.get("quien_hace_que"):
            w(f"<p><b>Quién hace qué:</b> {_e(use['quien_hace_que'])}</p>")
        if use.get("documento"):
            w(f"<p class='small'>Documento de uso previsto: {_e(use['documento'])}</p>")
        ctx = use.get("contexto") or []
        if ctx:
            w("<p><b>Contexto:</b></p><ul>" + "".join(f"<li>{_e(x)}</li>" for x in ctx) + "</ul>")
        else:
            w("<div class='alert'>No se declaró el contexto (país, materia, líneas de crisis): "
              "los hechos localizados de los criterios quedan sin contrastar.</div>")
        cubes = counts.get("cubos") or {}
        if cubes:
            w(
                "<p><b>Alcance de los " + str(counts.get("criterios", 0)) + " criterios:</b> "
                f"{cubes.get(CUBO_PLATAFORMA, 0)} son trabajo de la plataforma, "
                f"{cubes.get(CUBO_DOC_ESTUDIO, 0)} piden un documento o un estudio, "
                f"{cubes.get(CUBO_DOCENTE, 0)} miran algo que hace el docente y "
                f"{cubes.get(CUBO_MAL_LOCALIZADO, 0)} dependen de hechos de otro país o materia.</p>"
            )

    # --- autochequeo del recorrido ---
    w("<h2>0. ¿El recorrido miró todo?<small>autochequeo, hecho por el código</small></h2>")
    uncovered = cover.get("sin_cubrir") or []
    if not cover:
        w("<div class='alert bad'>No hay autochequeo: el recorrido no produjo una lista de entradas.</div>")
    elif uncovered:
        w(
            "<div class='alert bad'><b>El recorrido NO está completo.</b> Estas entradas se vieron en "
            "la plataforma y no se recorrieron: <b>" + _e("; ".join(uncovered))
            + "</b>. Los criterios que dependan de ellas figuran como indefinidos.</div>"
        )
    else:
        w(
            f"<div class='alert good'>Las {cover.get('entradas', 0)} entradas que se vieron en los menús "
            "se recorrieron o tienen un motivo escrito para no hacerlo.</div>"
        )
    for note in cover.get("notas") or []:
        w(f"<div class='alert'>{_e(note)}</div>")
    if cover.get("por_confirmar"):
        w(
            "<div class='alert'>Roles que el explorador dio por no disponibles y conviene confirmar: <b>"
            + _e("; ".join(cover["por_confirmar"])) + "</b>.</div>"
        )
    w("<table><tr><th>Entrada vista en un menú</th><th>Tipo</th><th>Estado</th><th>Pantallas o motivo</th></tr>")
    for entry in recon.get("entradas") or []:
        st = entry.get("estado")
        if st == "mencionada":
            continue   # no es navegación: va en la lista de abajo
        tag = {"recorrida": "t-ok", "no_disponible": "t-no", "sin_cubrir": "t-un"}.get(st, "t-un")
        label = {"recorrida": "Recorrida", "no_disponible": "No disponible", "sin_cubrir": "Sin recorrer"}.get(st, st)
        w(
            f"<tr><td>{_e(entry.get('texto'))}</td>"
            f"<td>{'Cambio de rol' if entry.get('tipo') == 'rol' else 'Menú'}</td>"
            f"<td><span class='tag {tag}'>{label}</span></td>"
            f"<td>{_e(', '.join(entry.get('partes') or []) or entry.get('motivo') or '')}</td></tr>"
        )
    w("</table>")
    w(
        f"<p class='small'>Roles recorridos: {_e(', '.join(recon.get('roles') or []) or 'ninguno')}. "
        f"Estados distintos registrados: {cover.get('estados_distintos', 0)}.</p>"
    )
    if cover.get("mencionadas"):
        w(
            "<p class='small'>Controles que el explorador nombró y que no son navegación según el árbol "
            "de las páginas (no cuentan como entradas): " + _e("; ".join(cover["mencionadas"])) + ".</p>"
        )
    for item in recon.get("sin_explorar") or []:
        w(f"<div class='alert'>No se pudo explorar: {_e(item)}</div>")

    # --- 1. alcance de un vistazo ---
    w("<h2>1. Alcance de un vistazo<small>una fila por pantalla que el recorrido registró</small></h2>")
    w("<p class='lead'>Verde: se usa en al menos una prueba. Amarillo: se vio y no se recorrió, o no alimenta ningún criterio. Rojo punteado: no hay captura.</p>")
    w("<table><tr><th>Captura</th><th>Pantalla</th><th>Rol</th><th>Criterios que alimenta</th><th>Pruebas</th><th>Estado</th></tr>")
    for s in surfaces:
        state = s.get("estado")
        tag = {"se_prueba": ("t-ok", "Se prueba"), "indefinido": ("t-un", "Indefinida"),
               "sin_captura": ("t-no", "Sin captura"), "sin_criterios": ("t-un", "Sin criterios")}[state]
        w(
            f"<tr><td>{shot(s.get('captura'), anchor=s.get('id') or '')}</td>"
            f"<td><b>{_e(s.get('id'))} {_e(s.get('nombre'))}</b><br><span class='small'>{_e(s.get('que_muestra'))}</span></td>"
            f"<td><span class='tag t-role'>{_e(_ROLE_LABEL.get(s.get('rol'), s.get('rol') or 'sin dato'))}</span></td>"
            f"<td>{_e(', '.join(s.get('criterios') or []) or 'ninguno')}</td>"
            f"<td>{s.get('pruebas', 0)}</td>"
            f"<td><span class='tag {tag[0]}'>{tag[1]}</span></td></tr>"
        )
    w("</table>")

    # --- 2. detalle de lo que se prueba ---
    tested = [c for c in criteria if c["estado"] == SE_PRUEBA]
    w(f"<h2>2. Lo que se va a probar<small>{len(tested)} criterios, {counts.get('pruebas', 0)} pruebas</small></h2>")
    for c in tested:
        mark = " · revisar" if c.get("revisar") else ""
        w(f"<details><summary>{_e(c['id'])} · {_e(c['nombre'])} · n = {c.get('n', 0)}{mark}</summary>")
        w(f"<p>{_e(c.get('que_se_prueba'))}</p>")
        w(_ficha_table(c))
        w(f"<p class='small'>Por qué se puede probar: {_e(c.get('motivo'))}</p>")
        if c.get("celdas"):
            sens = "sensible al tema" if c.get("sensible_al_tema") else "no depende del tema"
            w(f"<p><b>Muestreo</b> ({sens}). {_e(c.get('motivo_muestreo', ''))}</p>")
            w("<table class='cells'><tr><th>#</th><th>Tema</th><th>Variante del estímulo</th></tr>")
            for i, cell in enumerate(c["celdas"], start=1):
                flag = " <span class='tag t-un'>tema no visto en el recorrido</span>" if cell.get("tema_no_visto") else ""
                w(f"<tr><td>{i}</td><td>{_e(cell.get('tema') or 'cualquiera')}{flag}</td><td>{_e(cell.get('variante'))}</td></tr>")
            w("</table>")
        for note in c.get("advertencias") or []:
            w(f"<div class='alert'>{_e(note)}</div>")
        for pid in c.get("partes") or []:
            s = next((x for x in surfaces if x.get("id") == pid), None)
            if s is None:
                continue
            link = (
                f"<a href='#cap-{_e(pid)}'>ver la captura</a>" if s.get("captura")
                else "<span class='tag t-no'>sin captura</span>"
            )
            w(f"<p><b>{_e(pid)} {_e(s.get('nombre'))}</b> ({link}): {_e(s.get('como_llegar'))}</p>")
        w("</details>")

    # --- 3. no se puede evaluar ---
    cannot = [c for c in criteria if c["cubeta"] == "no_se_puede"]
    undefined = [c for c in criteria if c["cubeta"] == "indefinido"]
    w(f"<h2>3. Indefinidos<small>{len(undefined)} criterios: aquí es donde hace falta su corrección</small></h2>")
    w("<p class='lead'>El planificador no pudo ubicar estos criterios en ninguna pantalla recorrida. Puede que la función no exista o que el recorrido no la haya encontrado.</p>")
    if undefined:
        w("<table><tr><th>Criterio</th><th>Qué faltó</th></tr>")
        for c in undefined:
            extra = "".join(f"<br><span class='small'>{_e(n)}</span>" for n in c.get("advertencias") or [])
            w(f"<tr><td><b>{_e(c['id'])}</b> {_e(c['nombre'])}</td><td>{_e(c.get('motivo'))}{extra}</td></tr>")
        w("</table>")
    else:
        w("<p>Ninguno.</p>")
    w(f"<h2>4. No se puede evaluar<small>{len(cannot)} criterios: son hallazgos, no ceros</small></h2>")
    if cannot:
        w("<table><tr><th>Criterio</th><th>Estado</th><th>Motivo y qué lo destrabaría</th></tr>")
        for c in cannot:
            w(f"<tr><td><b>{_e(c['id'])}</b> {_e(c['nombre'])}</td><td>{_tag(c['estado'])}</td><td>{_e(c.get('motivo'))}</td></tr>")
        w("</table>")
    else:
        w("<p>Ninguno.</p>")

    # --- fuera del alcance de la plataforma ---
    outside = [c for c in criteria if c["cubeta"] == "fuera_de_alcance"]
    if outside or use.get("declarado"):
        w(f"<h2>4b. Fuera del alcance de la plataforma<small>{len(outside)} criterios: "
          "no se prueban y no pueden terminar como una falla de la plataforma</small></h2>")
        w("<p class='lead'>Son del docente según el modelo de uso declarado, o su regla depende de "
          "un dato de otro país o de otra materia. Quedan sin evaluar, con lo que haría falta. "
          "Si alguno está mal clasificado, es el lugar para vetarlo.</p>")
        if outside:
            for c in outside:
                w(f"<details open><summary>{_e(c['id'])} · {_e(c['nombre'])} · {_tag(c['estado'])}</summary>")
                w(f"<p>{_e(c.get('motivo'))}</p>")
                w(_ficha_table(c))
                for note in c.get("advertencias") or []:
                    w(f"<div class='alert'>{_e(note)}</div>")
                w("</details>")
        else:
            w("<p>Ninguno.</p>")

    # --- 5. documentos ---
    docs = plan.get("documentos_necesarios") or []
    w(f"<h2>5. Documentos necesarios<small>{len(docs)} pedidos al proveedor</small></h2>")
    received = plan.get("documentos_recibidos") or []
    w(f"<p class='lead'>Recibidos hasta ahora: {_e('; '.join(received) or 'ninguno')}. Sin estos documentos, los criterios de la derecha quedan sin evaluar.</p>")
    if docs:
        w("<table><tr><th>Documento</th><th>Criterios que destraba</th></tr>")
        for d in docs:
            names = "; ".join(f"{cid} {by_id.get(cid, {}).get('nombre', '')}" for cid in d["criterios"])
            w(f"<tr><td>{_e(d['documento'])}</td><td>{_e(names)}</td></tr>")
        w("</table>")
    else:
        w("<p>Ninguno.</p>")

    # --- 6. comportamientos y experimentos ---
    behaviors = recon.get("comportamientos") or []
    w("<h2>6. Lo que el recorrido averiguó haciendo<small>una corrida cada experimento</small></h2>")
    for exp in recon.get("experimentos") or []:
        w(f"<details><summary>Experimento: {_e(exp.get('pregunta'))} · {_e(exp.get('estado'))}</summary>")
        w(f"<p><b>Qué significaría cada resultado (escrito antes de correr):</b> {_e(exp.get('oraculo'))}</p>")
        for bid in exp.get("observaciones") or []:
            b = next((x for x in behaviors if x.get("id") == bid), None)
            if b is None:
                continue
            w(f"<p><b>{_e(bid)} {_e(b.get('nombre'))}</b><br>Qué se hizo: {_e(b.get('que_hice'))}<br>"
              f"Qué pasó: {_e(b.get('que_observe'))}<br>Conclusión: {_e(b.get('conclusion'))}</p>")
            w(shot(b.get("captura"), "full"))
        w(f"<p class='small'>Corridas: {exp.get('corridas', 1)}. Con una corrida no se afirma que siempre sea así.</p></details>")
    if recon.get("no_encontrado"):
        w("<p><b>Se buscó y no se encontró:</b></p><ul>" + "".join(f"<li>{_e(x)}</li>" for x in recon["no_encontrado"]) + "</ul>")
    if recon.get("documentado_sin_verificar"):
        w("<p><b>Lo afirma un documento del proveedor y no se verificó en pantalla:</b></p><ul>"
          + "".join(f"<li>{_e(x)}</li>" for x in recon["documentado_sin_verificar"]) + "</ul>")

    # --- 7. reglas de la corrida ---
    w("<h2>7. Reglas de la corrida</h2><ul>")
    w(f"<li>Pruebas por criterio: {_e(rules.get('pruebas_por_criterio'))}.</li>")
    for rule in rules.get("condiciones_de_corte") or []:
        w(f"<li>{_e(rule)}</li>")
    w(f"<li>Costo y tiempo estimados: USD {rules.get('costo_estimado_usd', 0):.2f} y unos {rules.get('tiempo_estimado_min', 0)} minutos. {_e(rules.get('base_de_la_estimacion'))}.</li>")
    w(f"<li>Compuertas: {_e(rules.get('con_compuerta'))}</li></ul>")

    # --- anexo: las capturas, una vez cada una ---
    shown = [s for s in surfaces if s.get("id") and s.get("captura")]
    w(f"<h2>Anexo. Capturas del recorrido<small>{len(shown)} pantallas, a tamaño de lectura</small></h2>")
    for s in shown:
        w(f"<details id='cap-{_e(s['id'])}' open><summary>{_e(s['id'])} · {_e(s.get('nombre'))} · "
          f"{_e(_ROLE_LABEL.get(s.get('rol'), s.get('rol') or 'sin dato'))}</summary>")
        w(f"<p><b>Cómo se llega:</b> {_e(s.get('como_llegar'))}<br><b>Qué permite:</b> {_e(s.get('que_permite'))}</p>")
        w(shot(s.get("captura"), "full"))
        w(f"<p class='small'>{_e(s.get('url'))} · sha256 {_e(s.get('sha256'))} · "
          f"archivo {_e(s.get('captura'))} · árbol de la página: {_e(s.get('texto'))}</p></details>")

    # --- cómo corregir ---
    w("<h2>Cómo corregir este plan</h2><div class='how'>")
    w("<p>Tres respuestas posibles, por superficie o por criterio:</p><ul>"
      "<li><b>Eso sí existe:</b> indique dónde (una dirección o el camino de toques). El recorrido vuelve a esa parte y el plan se rehace.</li>"
      "<li><b>Falta esta parte:</b> nómbrela y diga cómo se llega.</li>"
      "<li><b>Cambiar el tamaño:</b> más o menos pruebas en un criterio, otros temas, o quitar un criterio.</li>"
      "<li><b>Vetar el alcance:</b> si un criterio figura como del docente y es de la plataforma (o al revés), "
      "o si figura como de otro contexto y sí aplica, dígalo por criterio (corrección de tipo "
      "<code>alcance</code>, con <code>rol</code> o <code>localizacion</code>). Lo que fija una persona manda "
      "sobre lo que clasificó el modelo.</li></ul>"
      "<p>Las correcciones se guardan como datos junto al plan (<code>correcciones</code> en <code>plan.json</code>) "
      "y la versión siguiente muestra solo lo que cambió.</p></div>")
    w("</main></body></html>")
    return "\n".join(o)
