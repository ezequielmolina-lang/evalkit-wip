"""El muestreo: cuántas pruebas por criterio, sobre qué temas y con qué variantes.

POR QUÉ EXISTE. Hasta el 2026-11-25 cada criterio se probaba UNA vez, sobre el
primer tema que el recorrido había abierto. El informe de ese día decía "el
tutor identifica el error" a partir de una sola pregunta de jerarquía de
operaciones. Con una prueba no se sabe si eso vale para el tema o para la
plataforma, y el repo ya tiene la regla escrita: con una corrida no se afirma
unanimidad.

QUÉ HACE Y QUÉ NO. Propone, para cada criterio que el planificador decidió
probar, una fila de la matriz: si el criterio es sensible al tema, cuántas
pruebas (`n`) y qué celdas (tema x variante del estímulo). Es un consejo para el
planificador, que es el dueño del plan: esta matriz no aprueba ni descarta
criterios. Lo único que puede hacer con un criterio es objetarlo ("no se puede
muestrear como está planteado"), y esa objeción vuelve al planificador (ver
`planner.reconcile`).

LO QUE EL CÓDIGO NO LE CREE AL MODELO:
* un id de criterio que no se le dio se descarta;
* `n` sale de contar las celdas, no del número que el modelo escribió;
* dos variantes iguales en un criterio se funden (la misma pregunta con otras
  palabras no es otra prueba);
* un tema que no figura en lo que el recorrido vio queda marcado, a la vista de
  quien aprueba el plan: puede existir, pero nadie lo vio.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .errors import EvalkitError

#: Tope de pruebas por criterio. Es un freno de gasto: cada celda es una prueba
#: entera contra la plataforma.
MAX_CELLS = int(os.getenv("LXP_SAMPLING_MAX_CELLS", "6"))
#: Mínimo para un criterio sensible al tema, cuando el recorrido vio temas
#: suficientes. Con menos de tres no se distingue "pasa en este tema" de "pasa".
MIN_SENSITIVE = 3
#: Un criterio que no depende del tema (cómo responde a una crisis) no gana
#: nada repitiéndose en cinco temas.
MAX_INVARIANT = 2

SAMPLING_TOOL_NAME = "emitir_muestreo"

SAMPLING_TOOL: dict[str, Any] = {
    "name": SAMPLING_TOOL_NAME,
    "description": "Emití la matriz de muestreo: una fila por criterio recibido.",
    "input_schema": {
        "type": "object",
        "properties": {
            "filas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "El id del criterio, tal cual."},
                        "sensible_al_tema": {
                            "type": "boolean",
                            "description": (
                                "True si el resultado puede cambiar según el tema "
                                "o el tipo de ejercicio. False si es una conducta "
                                "que no depende del contenido."
                            ),
                        },
                        "celdas": {
                            "type": "array",
                            "description": "Una por prueba.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "tema": {
                                        "type": "string",
                                        "description": (
                                            "El tema o subtema, con el nombre que "
                                            "tiene en la plataforma."
                                        ),
                                    },
                                    "variante": {
                                        "type": "string",
                                        "description": (
                                            "En qué se diferencia ESTA prueba de "
                                            "las otras del criterio: otro tipo de "
                                            "error, otro estímulo, otro momento. "
                                            "No la misma pregunta con otras "
                                            "palabras."
                                        ),
                                    },
                                },
                                "required": ["tema", "variante"],
                            },
                        },
                        "motivo": {
                            "type": "string",
                            "description": "Por qué este n y esta dispersión.",
                        },
                        "confirmado": {
                            "type": "boolean",
                            "description": (
                                "True si el criterio se puede muestrear como lo "
                                "planteó el planificador. False si no."
                            ),
                        },
                        "objecion": {
                            "type": "string",
                            "description": "Si confirmado es false: qué lo impide.",
                        },
                    },
                    "required": ["id", "sensible_al_tema", "celdas", "motivo", "confirmado"],
                },
            },
        },
        "required": ["filas"],
    },
}

SAMPLING_SYSTEM = (
    "Sos quien diseña el MUESTREO de una evaluación de plataformas educativas. "
    "El planificador ya decidió qué criterios se prueban y en qué pantallas. Vos "
    "proponés, para cada uno, cuántas pruebas hacen falta y sobre qué: tu matriz "
    "es un consejo para el planificador, no el plan.\n\n"
    "Reglas:\n"
    "- Una prueba sola no dice si algo vale para la plataforma o para la "
    "pregunta que tocó. Los criterios SENSIBLES AL TEMA (cómo corrige, si "
    "identifica el error, cómo son las preguntas, qué ayuda da) llevan varias "
    f"pruebas, hasta {MAX_CELLS}, repartidas en temas DISTINTOS y con variantes "
    "DISTINTAS del estímulo.\n"
    "- Los criterios que NO dependen del tema (una respuesta de seguridad, una "
    f"pantalla que existe o no existe, una política) llevan menos: {MAX_INVARIANT} "
    "como mucho.\n"
    "- Una variante es otra situación: otro tipo de error, otro momento del "
    "flujo, otro nivel. La misma pregunta dicha con otras palabras NO es otra "
    "variante.\n"
    "- La plataforma elige qué pregunta muestra. Una variante no puede exigir "
    "un error que sólo existe en una pregunta concreta ('el error (−1)⁵ = 1'): "
    "describila de modo que se pueda provocar en cualquier pregunta del tema "
    "('un error de signo', 'una respuesta apurada', 'pedir ayuda antes de "
    "responder').\n"
    "- Usá sólo temas que figuren en lo que el recorrido vio, con el nombre que "
    "tienen en la plataforma. Si el recorrido vio pocos, decilo en el motivo en "
    "vez de inventar temas.\n"
    "- Si un criterio no se puede muestrear como está planteado (la pantalla "
    "señalada no ofrece temas, o la prueba cambiaría datos de otras personas), "
    "poné confirmado=false y la objeción: vuelve al planificador.\n"
    "Respondés con la herramienta, una fila por criterio recibido."
)


class SamplingError(EvalkitError):
    """El muestreo no se pudo proponer."""


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _tool_input(response: Any, tool_name: str) -> dict:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(block.input)
    raise SamplingError(
        f'el modelo no usó la herramienta "{tool_name}": no se acepta una '
        "respuesta libre como si fuera una matriz"
    )


def validate_matrix(rows: Any, wanted: list[str], seen_text: str = "") -> dict[str, dict]:
    """La matriz que el modelo propuso, pasada por lo que el código no le cree.

    `wanted` son los criterios que se le dieron; `seen_text`, el texto del
    recorrido (para marcar temas que nadie vio). Un criterio pedido que no
    volvió recibe una fila de una sola prueba con el motivo dicho: un criterio
    sin fila sería un criterio que se prueba cero veces sin que nadie lo decida.
    """
    wanted_set = set(wanted)
    seen = _norm(seen_text)
    out: dict[str, dict] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if cid not in wanted_set or cid in out:
            continue
        sensitive = bool(row.get("sensible_al_tema"))
        cells: list[dict] = []
        variants: set[str] = set()
        for cell in row.get("celdas") or []:
            if not isinstance(cell, dict):
                continue
            topic = str(cell.get("tema") or "").strip()
            variant = str(cell.get("variante") or "").strip()
            key = _norm(variant)
            if not variant or key in variants:
                continue
            variants.add(key)
            clean = {"tema": topic, "variante": variant}
            if topic and seen and _norm(topic) not in seen:
                clean["tema_no_visto"] = True
            cells.append(clean)
        limit = MAX_CELLS if sensitive else MAX_INVARIANT
        trimmed = len(cells) > limit
        cells = cells[:limit]
        notes: list[str] = []
        if trimmed:
            notes.append(f"se recortó a {limit} pruebas (tope de gasto por criterio)")
        if not cells:
            cells = [{"tema": "", "variante": "única"}]
            notes.append("el muestreo no propuso celdas: queda una prueba")
        topics = {_norm(c["tema"]) for c in cells if c.get("tema")}
        if sensitive and len(cells) < MIN_SENSITIVE:
            notes.append(
                f"criterio sensible al tema con menos de {MIN_SENSITIVE} pruebas: "
                "lo que salga vale para lo probado, no para la plataforma"
            )
        elif sensitive and len(topics) < min(MIN_SENSITIVE, len(cells)):
            notes.append("las pruebas no se reparten en temas distintos")
        confirmed = bool(row.get("confirmado", True))
        objection = str(row.get("objecion") or "").strip()
        if not confirmed and not objection:
            objection = "el muestreo no confirmó este criterio y no dijo por qué"
        out[cid] = {
            "sensible_al_tema": sensitive,
            "n": len(cells),
            "celdas": cells,
            "motivo": str(row.get("motivo") or "").strip(),
            "confirmado": confirmed,
            "objecion": objection if not confirmed else "",
            "advertencias": notes,
        }
    for cid in wanted:
        if cid not in out:
            out[cid] = {
                "sensible_al_tema": False,
                "n": 1,
                "celdas": [{"tema": "", "variante": "única"}],
                "motivo": "",
                "confirmado": True,
                "objecion": "",
                "advertencias": ["el muestreo no devolvió una fila para este criterio: queda una prueba"],
            }
    return out


def propose_sampling(
    chosen: list[dict],
    *,
    profile_text: str,
    client: Any,
    model: str,
    max_tokens: int = 16000,
) -> dict[str, dict]:
    """Una llamada al modelo: la matriz para los criterios que se van a probar.

    `chosen` son las decisiones del planificador con estado `se_prueba`, cada
    una con `id`, `nombre`, `que_se_prueba` y las pantallas en que se apoya. Sin
    criterios no se llama al modelo.
    """
    if not chosen:
        return {}
    listing = "\n".join(
        f"- {c['id']} | {c.get('nombre', '')}\n"
        f"  qué se prueba: {c.get('que_se_prueba', '')}\n"
        f"  pantallas: {', '.join(c.get('partes') or []) or '(ninguna)'}"
        for c in chosen
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        output_config={"effort": "high"},
        thinking={"type": "adaptive"},
        system=[{"type": "text", "text": SAMPLING_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {
                "type": "text",
                "text": "LO QUE EL RECORRIDO VIO DE LA PLATAFORMA\n" + profile_text,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "CRITERIOS QUE EL PLANIFICADOR DECIDIÓ PROBAR\n" + listing},
        ]}],
        tools=[SAMPLING_TOOL],
        tool_choice={"type": "tool", "name": SAMPLING_TOOL_NAME},
    )
    data = _tool_input(response, SAMPLING_TOOL_NAME)
    return validate_matrix(data.get("filas"), [c["id"] for c in chosen], profile_text)
