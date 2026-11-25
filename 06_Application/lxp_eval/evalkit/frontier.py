"""La frontera del recorrido: qué falta mirar, llevado por el código y no por el agente.

POR QUÉ EXISTE. En la corrida del 2026-11-25 (trabajo ec2f976187b243c2) el
explorador escribió en sus propias notas que el menú tenía "Temas", "Vista
docente", "Cómo funciona" y "Cambiar estudiante". Que después hubiera entrado o
no a cada una dependía de que él mismo se acordara, y de que él mismo decidiera
que ya había terminado. Un agente que decide solo cuándo terminó termina antes
de tiempo: es el error más común de esta clase de agentes, y el más caro acá,
porque una parte que el recorrido no pisó se convierte más tarde en un criterio
"sin evidencia" que nadie sabe a qué atribuir.

La regla de este módulo es una sola: **la cobertura es una propiedad de una
lista externa, nunca de la sensación del agente de haber terminado.**

* Las entradas (ítems de menú, cambios de rol, pestañas de navegación) se leen
  del ÁRBOL DE ACCESIBILIDAD de cada pantalla registrada, que es completo e
  incluye lo que está fuera de la vista. Lo que el agente declare se suma; no
  reemplaza.
* Cada entrada termina en uno de tres estados: recorrida (hay una parte
  registrada, con captura, a la que se llega por ahí), no disponible (con un
  motivo escrito) o sin cubrir. No hay un cuarto estado "seguro que no importa".
* Un cambio de rol es una entrada obligatoria: haber terminado con lo del
  estudiante no dice nada sobre lo del docente.

Todo acá es puro (texto adentro, datos afuera): no abre un navegador ni llama a
un modelo, así que se prueba sin dobles.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable
from urllib.parse import urlparse

#: Roles ARIA que son una entrada cuando están dentro de una zona de navegación.
ENTRY_ROLES = frozenset({"link", "button", "tab", "menuitem"})

#: Zonas donde vive la navegación de la plataforma. Un botón dentro de `main`
#: es contenido (la opción de una pregunta, un tema de la lista), no una
#: entrada: contarlo haría explotar la frontera con cada ejercicio.
NAV_LANDMARKS = frozenset({"navigation", "banner", "menubar", "menu", "tablist"})

#: Roles interactivos que entran en la firma de un estado.
_INTERACTIVE = frozenset({
    "link", "button", "tab", "menuitem", "textbox", "combobox", "checkbox",
    "radio", "switch", "slider", "searchbox", "listbox", "option",
})

#: Una entrada cuyo texto sugiere OTRO ROL. Es una heurística para decidir qué
#: se recorre primero y qué se marca como obligatorio en el documento; no decide
#: la cobertura (todas las entradas cuentan igual).
_ROLE_HINT = re.compile(
    r"docente|profesor|maestr|teacher|instructor|admin|director|coordinador"
    r"|familia|padre|parent|tutor legal|\brol\b|\brole\b",
    re.IGNORECASE,
)

#: Entradas que NO se recorren a propósito: cierran la sesión o destruyen algo.
#: No se ignoran: quedan en la lista como "no disponible" con este motivo, que
#: es la diferencia entre un salto anotado y un olvido.
_SKIP = re.compile(
    r"cerrar sesi[oó]n|\bsalir\b|log ?out|sign ?out|cambiar (de )?(estudiante|usuario|cuenta)"
    r"|borrar|eliminar|reiniciar|delete|reset",
    re.IGNORECASE,
)

_LINE = re.compile(r'^(\s*)- (/?[A-Za-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?')
_URL_LINE = re.compile(r'^\s*- /url:\s*"?([^"\s]+)"?')


def normalize(text: Any) -> str:
    """El texto de una entrada, comparable: sin mayúsculas ni adornos."""
    out = re.sub(r"\s+", " ", str(text or "")).strip().casefold()
    return out.strip(" ›»>·:").strip()


def enumerate_entries(page_text: str) -> list[dict]:
    """Las entradas de navegación de UNA pantalla, leídas de su árbol.

    Devuelve `[{texto, tipo, url}]` sin repetidos. `tipo` es `rol` cuando el
    texto sugiere otro rol y `menu` en el resto. Un texto de página que no es un
    árbol (el navegador cayó al texto plano) devuelve una lista vacía: en ese
    caso la frontera se arma sólo con lo que declare el explorador, y el
    informe de cobertura lo dice.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    stack: list[tuple[int, str]] = []
    last: dict | None = None
    last_indent = -1
    for line in (page_text or "").splitlines():
        url_match = _URL_LINE.match(line)
        if url_match and last is not None:
            indent = len(line) - len(line.lstrip())
            if indent > last_indent and not last.get("url"):
                last["url"] = url_match.group(1)
            continue
        m = _LINE.match(line)
        if not m:
            continue
        indent, role, name = len(m.group(1)), m.group(2).lower(), (m.group(3) or "").strip()
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if role in ENTRY_ROLES and name and any(r in NAV_LANDMARKS for _, r in stack):
            key = normalize(name)
            if key and key not in seen:
                seen.add(key)
                last = {
                    "texto": name,
                    "tipo": "rol" if _ROLE_HINT.search(name) else "menu",
                    "url": "",
                }
                last_indent = indent
                entries.append(last)
            else:
                last = None
        else:
            last = None
        stack.append((indent, role))
    return entries


def state_signature(page_text: str, url: str = "") -> str:
    """La firma de un ESTADO: qué se puede tocar ahí, no qué dice.

    Dos turnos del mismo chat tienen los mismos controles y colapsan en un
    estado; la vista del estudiante y la del docente tienen controles distintos
    y no. Se ignoran el texto libre y los números de la dirección (el id de un
    curso), que es lo que hace explotar la cuenta de estados sin agregar nada.
    """
    controls: set[str] = set()
    for line in (page_text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        role, name = m.group(2).lower(), (m.group(3) or "")
        if role in _INTERACTIVE:
            # Los dígitos fuera: "Pregunta 3 de 10" y "Pregunta 4 de 10" son el
            # mismo control.
            controls.add(f"{role}:{re.sub(r'[0-9]+', '#', normalize(name))}")
    path = re.sub(r"[0-9]+", "#", urlparse(url or "").path or "/")
    blob = path + "\n" + "\n".join(sorted(controls))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _path_of(url: str) -> str:
    """La ruta de una dirección, comparable. La raíz es "/" y un ancla no es nada.

    La raíz cuenta: en la primera corrida en vivo el menú del docente tenía un
    enlace "Práctica" a "/", el explorador ya había registrado la pantalla
    principal en "/", y el enlace figuró como no recorrido.
    """
    url = (url or "").strip()
    if not url or url.startswith("#"):
        return ""
    path = urlparse(url).path if "//" in url else url.split("#", 1)[0].split("?", 1)[0]
    return (path or "/").rstrip("/") or "/"


def build_frontier(
    parts: Iterable[dict],
    declared: Iterable[dict] | None = None,
) -> list[dict]:
    """La lista de entradas con su estado, a partir de lo que se registró.

    `parts` son las partes del perfil (cada una con `entradas_en_pantalla`, que
    el código leyó del árbol al registrarla, y con `entrada`/`url`). `declared`
    es el inventario que escribió el explorador: aporta entradas que el árbol no
    mostró y los motivos de "no disponible"; nunca saca una entrada de la lista.
    """
    parts = [p for p in parts if isinstance(p, dict)]
    frontier: dict[str, dict] = {}

    def add(text: str, kind: str = "menu", url: str = "", source: str = "arbol",
            state: str = "sin_cubrir") -> dict | None:
        key = normalize(text)
        if not key:
            return None
        item = frontier.get(key)
        if item is None:
            # La pista de "otro rol" vale para lo que salió del árbol de
            # navegación. Un botón que el explorador mencionó y dice "docente"
            # ("Pedir ayuda al docente") no es un cambio de rol.
            hinted = source == "arbol" and _ROLE_HINT.search(str(text))
            item = frontier[key] = {
                "texto": str(text).strip(),
                "tipo": "rol" if hinted else (kind or "menu"),
                "url": url or "",
                "estado": state,
                "motivo": "",
                "partes": [],
                "origen": source,
            }
        elif url and not item["url"]:
            item["url"] = url
        return item

    for part in parts:
        for entry in part.get("entradas_en_pantalla") or []:
            if isinstance(entry, dict):
                add(entry.get("texto", ""), entry.get("tipo", "menu"), entry.get("url", ""))
            else:
                add(str(entry))
    tree_seen = bool(frontier)

    for entry in declared or []:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("tipo") or "menu")
        mandatory = bool(entry.get("obligatoria")) or kind in ("rol", "ingreso")
        # LO QUE EL EXPLORADOR MENCIONA NO ES, POR ESO, NAVEGACIÓN. En la
        # primera corrida en vivo declaró como entradas "Siguiente pregunta",
        # "Sí, hice eso", "Cerrar" y "Marcar como atendido": 16 falsas alarmas
        # de "sin recorrer" que tapaban el dato cierto, que las entradas leídas
        # del árbol estaban todas cubiertas. Cuando hay árbol, lo declarado que
        # el árbol no muestra queda como `mencionada` (a la vista, sin
        # bloquear), salvo que sea un rol, un ingreso o algo que una persona
        # dijo que existe. Sin árbol no hay con qué contrastar y cuenta todo.
        state = "sin_cubrir" if (mandatory or not tree_seen) else "mencionada"
        item = add(
            entry.get("texto", ""), kind, str(entry.get("url") or ""),
            source="persona" if entry.get("obligatoria") else "explorador", state=state,
        )
        if item is None:
            continue
        if entry.get("estado") == "no_disponible" and str(entry.get("motivo") or "").strip():
            item["estado"] = "no_disponible"
            item["motivo"] = str(entry["motivo"]).strip()

    # Recorrida = hay una parte registrada (con captura) a la que se llega por
    # esa entrada: porque el explorador lo dijo, o porque la dirección de la
    # parte es la del enlace.
    for part in parts:
        if not part.get("captura"):
            continue
        via = normalize(part.get("entrada"))
        part_path = _path_of(part.get("url", ""))
        for key, item in frontier.items():
            entry_path = _path_of(item["url"])
            if (via and via == key) or (entry_path and entry_path == part_path):
                if item["estado"] == "no_disponible" and item["origen"] != "arbol":
                    continue
                item["estado"] = "recorrida"
                item["motivo"] = ""
                pid = part.get("id")
                if pid and pid not in item["partes"]:
                    item["partes"].append(pid)

    for item in frontier.values():
        if item["estado"] == "sin_cubrir" and _SKIP.search(item["texto"]):
            item["estado"] = "no_disponible"
            item["motivo"] = (
                "no se recorre a propósito: cierra la sesión o cambia datos"
            )
    return list(frontier.values())


def pending(frontier: Iterable[dict]) -> list[dict]:
    """Lo que falta, los cambios de rol primero."""
    left = [e for e in frontier if e.get("estado") == "sin_cubrir"]
    return sorted(left, key=lambda e: 0 if e.get("tipo") == "rol" else 1)


def coverage(frontier: Iterable[dict], parts: Iterable[dict], *, tree_seen: bool) -> dict:
    """El autochequeo: qué se vio, qué se recorrió y qué quedó sin cubrir.

    `completo` es True sólo si no queda ninguna entrada sin cubrir Y ningún
    cambio de rol quedó como "no disponible" por pura declaración del
    explorador sin que una persona lo haya visto (eso va a `por_confirmar`:
    no bloquea, pero el documento del plan lo pone arriba).
    """
    frontier = list(frontier)
    parts = [p for p in parts if isinstance(p, dict)]
    uncovered = [e["texto"] for e in frontier if e["estado"] == "sin_cubrir"]
    unavailable = [
        {"texto": e["texto"], "motivo": e["motivo"], "tipo": e["tipo"]}
        for e in frontier if e["estado"] == "no_disponible"
    ]
    roles_seen = sorted({e["texto"] for e in frontier if e["tipo"] == "rol"})
    roles_entered = sorted(
        {e["texto"] for e in frontier if e["tipo"] == "rol" and e["estado"] == "recorrida"}
    )
    without_shot = [p.get("nombre", "") for p in parts if not p.get("captura")]
    notes = []
    if not tree_seen:
        notes.append(
            "Ninguna pantalla registrada trajo su árbol de accesibilidad: la lista "
            "de entradas sale sólo de lo que declaró el explorador, que es "
            "justamente lo que este chequeo no debería tener que creer."
        )
    return {
        # Las de navegación. Lo que el explorador sólo mencionó no es una
        # entrada y no infla la cuenta.
        "entradas": sum(1 for e in frontier if e["estado"] != "mencionada"),
        "recorridas": sum(1 for e in frontier if e["estado"] == "recorrida"),
        "no_disponibles": unavailable,
        "sin_cubrir": uncovered,
        "roles_vistos": roles_seen,
        "roles_recorridos": roles_entered,
        "por_confirmar": [u["texto"] for u in unavailable if u["tipo"] == "rol"],
        # Controles que el explorador nombró y que no son navegación según el
        # árbol. No bloquean; se listan para que nadie tenga que creerle.
        "mencionadas": [e["texto"] for e in frontier if e["estado"] == "mencionada"],
        "partes_sin_captura": [n for n in without_shot if n],
        "estados_distintos": len({p.get("firma") for p in parts if p.get("firma")}),
        "notas": notes,
        "completo": not uncovered and tree_seen,
    }
