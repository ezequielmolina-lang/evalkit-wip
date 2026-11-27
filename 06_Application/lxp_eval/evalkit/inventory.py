"""El inventario de contenidos navegables: los únicos temas que una prueba puede pedir.

POR QUÉ EXISTE. En la corrida completa del 2026-11-25, 14 pruebas se cortaron
buscando en el menú un "tema" que no era un tema: el nombre de una PANTALLA del
recorrido ("Pantalla de pregunta y feedback de error"), el de una pestaña, o una
etiqueta que el muestreo inventó para la celda ("Chat con Gallito: sonda de
memoria inventada"). La marca `tema_no_visto` no las frenó, porque comprobaba
que el texto figurara en la descripción del recorrido, y el nombre de una
pantalla figura. Otras cinco pidieron un contenido real por la ruta equivocada
(estaba bajo otro eje, o era un tema y no un subtema).

QUÉ ES. Una lista de RUTAS, cada una del menú principal hasta un contenido en
el que se puede entrar: ["Operaciones y expresiones algebraicas", "Conceptos
básicos del álgebra", "Valor numérico de una expresión algebraica"]. El
muestreo elige temas SÓLO de las hojas de esta lista, y cada celda lleva su
ruta para que el paso de navegación no tenga que adivinarla.

LO QUE EL CÓDIGO NO LE CREE AL MODELO. El modelo arma las rutas leyendo la
descripción del recorrido y el texto de las pantallas guardadas. El código se
queda sólo con las rutas cuyos niveles figuran TODOS, tal cual, en el texto de
alguna pantalla que el recorrido guardó. Una ruta con un nivel que nadie vio se
descarta: puede existir, pero mandar una prueba ahí es lo que costó los cortes.
"""
from __future__ import annotations

import re
from typing import Any

from .errors import EvalkitError

INVENTORY_TOOL_NAME = "emitir_inventario"

INVENTORY_TOOL: dict[str, Any] = {
    "name": INVENTORY_TOOL_NAME,
    "description": "Emití las rutas del menú hasta cada contenido en el que se puede entrar.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rutas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "niveles": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Los nombres que hay que tocar, en orden, desde el "
                                "menú principal de contenidos hasta el contenido. "
                                "Cada nombre TAL CUAL figura en pantalla, sin "
                                "contadores ('3 subtemas') ni flechas."
                            ),
                        },
                    },
                    "required": ["niveles"],
                },
            },
        },
        "required": ["rutas"],
    },
}

INVENTORY_SYSTEM = (
    "Armás el INVENTARIO DE CONTENIDOS de una plataforma educativa a partir de "
    "lo que un recorrido vio: su descripción y el texto de las pantallas que "
    "guardó. Un contenido es algo en lo que un estudiante ENTRA desde el menú "
    "para practicar o estudiar: una unidad, un tema, un subtema, una lección.\n"
    "Reglas:\n"
    "- Una ruta por contenido, del menú principal hasta el contenido, con los "
    "nombres tal cual figuran en pantalla.\n"
    "- Sólo lo que figura en el texto de las pantallas. No completes un menú "
    "con lo que suponés que hay.\n"
    "- NO son contenidos: las pantallas (una pantalla de pregunta, un panel), "
    "las pestañas, los botones de acción, los roles, los nombres de personas.\n"
    "- Si de un tema se vio que tiene subtemas pero no cuáles, la ruta termina "
    "en el tema.\n"
    "Respondés con la herramienta."
)

#: Cuánto texto de cada pantalla se le pasa al modelo. Alcanza para un menú.
_TREE_CHARS = 3500


class InventoryError(EvalkitError):
    """El inventario no se pudo armar."""


#: Lo que separa los niveles cuando alguien escribe una ruta en una línea.
_SEPARATORS = re.compile(r"\s*[›>»→/]\s*")


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _tool_input(response: Any, tool_name: str) -> dict:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return dict(block.input)
    raise InventoryError(f'el modelo no usó la herramienta "{tool_name}"')


def validate_inventory(routes: Any, seen_text: str) -> list[dict]:
    """Las rutas que el modelo propuso, pasadas por lo que las pantallas dicen."""
    seen = _norm(seen_text)
    out: list[dict] = []
    leaves: set[str] = set()
    for row in routes or []:
        levels = row.get("niveles") if isinstance(row, dict) else row
        levels = [str(x).strip() for x in (levels or []) if str(x).strip()]
        if not levels or any(_norm(x) not in seen for x in levels):
            continue
        leaf = _norm(levels[-1])
        if leaf in leaves:
            continue
        leaves.add(leaf)
        out.append({"ruta": levels, "hoja": levels[-1]})
    return out


def find(inventory: list[dict] | None, name: Any) -> dict | None:
    """La entrada cuya hoja es `name` (sin importar mayúsculas ni espacios)."""
    key = _norm(name)
    if not key:
        return None
    # LA HOJA O LA RUTA ENTERA. En la primera verificación en vivo el muestreo
    # copió la ruta completa ("Eje › Tema › Subtema") como tema, tal como se la
    # habíamos mostrado; la comparación era sólo contra la hoja, así que TODAS
    # las celdas perdieron su tema y las pruebas fueron al primer contenido.
    last = _norm(_SEPARATORS.split(str(name))[-1])
    for entry in inventory or []:
        leaf = _norm(entry.get("hoja"))
        if leaf in (key, last):
            return entry
    return None


def render_inventory(inventory: list[dict] | None) -> str:
    if not inventory:
        return "(el recorrido no dejó un inventario de contenidos)"
    return "\n".join(
        f"- {e['hoja']}   [se llega por: {' › '.join(e['ruta'])}]" for e in inventory
    )


def build_inventory(
    *, profile_text: str, trees: list[str], client: Any, model: str, max_tokens: int = 8000
) -> list[dict]:
    """Una llamada al modelo y la verificación por código. Sin pantallas, vacío."""
    trees = [t for t in trees if t and t.strip()]
    if not trees:
        return []
    screens = "\n\n".join(f"--- pantalla {i} ---\n{t[:_TREE_CHARS]}" for i, t in enumerate(trees, 1))
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": INVENTORY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "LO QUE EL RECORRIDO DESCRIBIÓ\n" + profile_text},
            {"type": "text", "text": "TEXTO DE LAS PANTALLAS GUARDADAS\n" + screens},
        ]}],
        tools=[INVENTORY_TOOL],
        tool_choice={"type": "tool", "name": INVENTORY_TOOL_NAME},
    )
    data = _tool_input(response, INVENTORY_TOOL_NAME)
    return validate_inventory(data.get("rutas"), "\n".join(trees))
