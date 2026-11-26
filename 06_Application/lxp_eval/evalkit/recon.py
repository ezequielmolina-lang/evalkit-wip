"""El mapa de superficies que produce el reconocimiento.

Deliberadamente NO es un formato nuevo: es el mismo `surfaces.yaml` que
`ProbeBank.steps_for` ya consume y que hasta hoy escribia una persona a mano
(`platforms/plataforma_a/surfaces.yaml`). Que lo genere el producto lo vuelve
versionado, auditable y corregible.

Una superficie que no aparece va a `unavailable` CON EL MOTIVO OBSERVADO. No
encontrarla es un hallazgo que el informe tiene que poder citar, no una razon
para descartar el criterio en silencio: un proveedor que no expone una
superficie no debe puntuar mejor que uno que si la expone.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

logger = logging.getLogger(__name__)


class PlatformMap:
    def __init__(self) -> None:
        self._surfaces: dict[str, dict[str, Any]] = {}
        self._unavailable: dict[str, str] = {}
        #: La descripción de la plataforma que hizo el recorrido (ver
        #: `explore_platform`). None cuando no se exploró: un mapa sin perfil
        #: es el de antes y todo lo que lo consume sigue funcionando.
        self.profile: dict[str, Any] | None = None

    def found(self, key: str, **spec: Any) -> None:
        """Una superficie que existe, y como se llega a ella."""
        self._surfaces[key] = {k: v for k, v in spec.items() if v is not None}
        self._unavailable.pop(key, None)

    def unavailable(self, key: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError(
                "una superficie ausente necesita un motivo: sin el, el informe "
                "no puede decir por que el criterio quedo sin evaluar"
            )
        self._unavailable[key] = reason.strip()
        self._surfaces.pop(key, None)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"surfaces": self._surfaces}
        if self._unavailable:
            out["unavailable"] = self._unavailable
        if self.profile is not None:
            out["profile"] = self.profile
        return out

    def write(self, path: Path | str) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            yaml.safe_dump(self.as_dict(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return p


# --- el recorrido -----------------------------------------------------------
#
# Descubre el mapa recorriendo la plataforma con el agente ya logueado. La forma
# de la salida es la MISMA que `platforms/plataforma_a/surfaces.yaml` (escrita a mano
# tras tres corridas pagas): ese archivo es la salida de referencia, y la
# redacción de los pasos se copia de ahí a propósito -- son prosa que el mismo
# agente reinterpreta en la corrida, con marcadores {course}/{stimulus}/{root}.
#
# LA REGLA de esta etapa, tomada del diseño: no encontrar una superficie es un
# HALLAZGO, no una falla. Cada tramo se envuelve para que lo que no aparece
# quede anotado en `unavailable` con lo que se vio, en vez de tumbar el
# recorrido entero. La única excepción es la sesión: sin sesión no hay nada que
# recorrer, así que ese error se propaga y el llamador lo convierte en un
# fallo barato (cero gasto en LLM).

#: Pasos para llegar a una lección. POR POSICIÓN, no por nombre.
#:
#: La primera versión nombraba el curso -- `clic en el curso "{course}"` -- con
#: el nombre que sacaba `_detect_course` del texto de la página. Contra Plataforma A en
#: vivo eso falló dos veces seguidas: una lectura devolvió "Home" (un ítem de
#: nav), otra "Estudio de alfabetización" (que no era ninguno de los 8 cursos).
#: El texto de la página varía entre lecturas, así que parsear el nombre es
#: whack-a-mole. El agente, en cambio, lista los 8 cursos perfectamente cuando
#: se niega -- tiene el entendimiento. Así que se le pide por POSICIÓN y él elige
#: un curso real. Esto también hace el paso re-ejecutable sin depender de que un
#: nombre siga existiendo.
#: SEGUNDA LECCIÓN, más cara que la primera. Estos pasos nombraban el widget:
#: "en la lista de Class Assignments". Tres semanas después la plataforma
#: rediseñó la portada -- de una lista de asignaciones a un tablero con "My
#: classes" -- y el mapa siguió anotando prosa sobre una pantalla que ya no
#: existe. Un paso que dice QUÉ conseguir sobrevive un rediseño; uno que dice
#: dónde hacer clic dura hasta el próximo despliegue del proveedor.
_LESSON_STEPS = [
    "Abrir el PRIMER curso o clase asignada al alumno, por el mecanismo de "
    "navegacion que la plataforma ofrezca (lista, tarjetas, tablero, menu o "
    "buscador). Elegir contenido asignado, NO elementos de navegacion del "
    "sitio como inicio, ayuda o configuracion. Esperar a que abra.",
    "Dentro de ese curso, abrir la primera leccion o actividad disponible y "
    "esperar a que cargue su contenido.",
]
_LESSON_READ = [
    "Leer el contenido completo de la leccion tal como aparece en pantalla.",
]

#: El paso que abre el panel del tutor DESDE una lección abierta. Se detecta el
#: tutor ejecutándolo (ver `discover_surfaces`), no buscando texto: el botón es
#: un ícono violeta sin palabras.
_TUTOR_OPEN_STEP = (
    "Abrir la interfaz conversacional del tutor de IA disponible en este "
    "contexto, cualquiera sea su forma: un boton flotante, un boton con texto "
    "(por ejemplo 'preguntar' o 'ayuda'), un panel lateral, una pestana o un "
    "chat embebido. Esperar a que muestre su estado inicial. NO escribir ni "
    "enviar nada."
)
#: El tutor NO está en la home: vive DENTRO de una lección. Por eso sus pasos
#: son los de la lección MÁS el paso que abre el panel del tutor.
_TUTOR_STEPS = _LESSON_STEPS + [_TUTOR_OPEN_STEP]
_TUTOR_SEND = [
    "Hacer clic en el campo de texto del tutor y escribir exactamente "
    "{stimulus} . Enviarlo con el boton de envio.",
]
_TUTOR_READ = [
    "Esperar a que el tutor termine de responder y leer la respuesta completa "
    "tal como aparece en el panel.",
]

_PRIVACY_STEPS = [
    "Navegar a la pagina publica del proveedor en {root} .",
    "En el pie de pagina o el menu, buscar y hacer clic en el enlace de la "
    'politica de privacidad (suele decir "Privacy", "Privacy Policy" o '
    '"Privacidad").',
    "Confirmar que la pagina cargada es realmente una politica de privacidad "
    "(titulo y clausulas legales, no un 404 ni un muro de login). Si no lo es, "
    "dejar constancia y no continuar.",
]
_PRIVACY_READ = [
    "Leer el texto completo de la politica de privacidad tal como aparece "
    "publicada.",
]

#: Encabezados que marcan el comienzo de la lista de cursos/asignaciones.
#: "My classes" se sumó cuando la plataforma medida rediseñó su portada: pasó
#: de una lista de "Class Assignments" a un tablero con "My classes", y el
#: detector -- que sólo conocía el vocabulario viejo -- dejó de encontrar
#: cursos en una cuenta que tenía uno. No encontrar curso hace que el recorrido
#: anote lesson_content y tutor_chat como no disponibles, así que un sinónimo
#: faltante se convierte en "la plataforma no ofrece nada", que es una
#: afirmación sobre el proveedor y no sobre nuestro vocabulario.
_ASSIGN_MARK = (
    "class assignments", "assignments", "asignaciones", "mis cursos",
    "cursos", "courses", "my classes", "mis clases", "clases", "classes",
)
#: Líneas de ruido en una lista de cursos: navegación, metadatos de la tarjeta
#: del curso, estados. Comparación exacta en minúsculas. Esto existe porque la
#: página real de Plataforma A NO usa "Assignments:" con dos puntos -- pone "Home",
#: "Settings", "Class Assignments", el conteo "8" y RECIÉN los cursos, cada uno
#: en su línea. La heurística vieja, que buscaba dos puntos y si no caía a "la
#: primera línea del texto", devolvía "Home" -- y el agente, con razón, se
#: negaba a abrir un curso llamado "Home".
_NAV_NOISE = frozenset({
    "home", "inicio", "code editor", "help", "ayuda", "settings", "ajustes",
    "configuracion", "configuración", "join", "unirse", "assignments",
    "class assignments", "asignaciones", "mis cursos", "cursos", "courses",
    "course", "curso", "dashboard", "panel", "notifications", "notificaciones",
    "menu", "menú", "profile", "perfil", "no due date", "sin fecha de entrega",
    "sin fecha", "not started", "no iniciado", "in progress", "en progreso",
    "completed", "completado", "progress", "progreso",
})
_NOISE_PREFIX = (
    "good evening", "good morning", "good afternoon", "buenos", "buenas",
    "hola,", "welcome", "bienvenid",
)

#: Señales de que la cuenta NO tiene cursos donde entrar.
_NO_COURSE = (
    "no hay curso",
    "sin curso",
    "no courses",
    "no course",
    "no hay asignacion",
    "no assignments",
    "no hay assignments",
)
#: Señales de que la página abierta es de verdad una política de privacidad.
_PRIVACY_HINTS = (
    "privacidad",
    "privacy",
    "datos personales",
    "proteccion de datos",
    "protección de datos",
)


def _short(text: str, limit: int = 240) -> str:
    """El texto observado, recortado, para dejarlo como motivo legible."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _is_course_noise(line: str) -> bool:
    """Una línea que NO puede ser el nombre de un curso: nav, conteos, metadatos."""
    low = line.strip().lower()
    if not low or low in _NAV_NOISE:
        return True
    if re.fullmatch(r"\d+%?", low):          # el conteo "8", el progreso "1%"
        return True
    if len(low) <= 2:                        # iniciales del avatar ("V")
        return True
    if any(low.startswith(p) for p in _NOISE_PREFIX):  # "Good evening, Estudiante de prueba"
        return True
    if "•" in line or "·" in line:           # "Docente de prueba • Clase de prueba"
        return True
    return False


def _detect_course(text: str) -> str | None:
    """El nombre del primer curso ofrecido, o None si no hay ninguno.

    Es heurístico a propósito: el agente sólo devuelve el texto de la pantalla,
    así que el nombre se extrae de ahí. Un None NO es un error: es "esta cuenta
    no ofrece cursos", que se anota como hallazgo.

    Dos formas, porque las páginas reales varían:
    1. Inline con dos puntos -- "Assignments: BM - Matemáticas Grado 3 · ..." --
       de donde se toma lo que sigue al ":" hasta el primer separador.
    2. Multilínea -- la página real de Plataforma A: nav, "Class Assignments", el conteo,
       y los cursos cada uno en su línea. Se salta el ruido y se toma el primer
       nombre real. La forma 1 sola devolvía "Home" acá, y el agente se negaba.
    """
    if not text:
        return None
    if any(neg in text.lower() for neg in _NO_COURSE):
        return None

    # Forma 1: "…assignments: <curso> · <otra cosa>" en una sola línea.
    m = re.search(
        r"(?:assignments|asignaciones|mis cursos|cursos|courses)\s*[:：]\s*(.+)",
        text,
        re.IGNORECASE,
    )
    if m:
        chunk = m.group(1)
        for sep in ("·", "•", "|", "\n", "\t", ";"):
            chunk = chunk.split(sep)[0]
        chunk = chunk.strip(" .·•|\t")
        if chunk:
            return chunk

    # Forma 2: multilínea. Empezar después del encabezado de asignaciones (si lo
    # hay) y devolver la primera línea que no sea ruido.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    start = 0
    for i, ln in enumerate(lines):
        if any(ln.lower().startswith(mark) for mark in _ASSIGN_MARK):
            start = i + 1
            break
    for ln in lines[start:]:
        if not _is_course_noise(ln):
            return ln
    return None


def _looks_like_privacy(text: str) -> bool:
    low = (text or "").lower()
    return any(hint in low for hint in _PRIVACY_HINTS)


#: Subdominios de aplicación: donde vive la app logueada, NO el sitio público.
#: La política de privacidad casi nunca está acá -- `app.plataforma-a.ejemplo` es el
#: flujo de login, sin pie de página; el documento vive en `www.plataforma-a.ejemplo`.
_APP_SUBDOMAINS = frozenset({
    "app", "learn", "my", "portal", "student", "students", "studentv2",
    "classroom", "aula", "plataforma", "platform", "dashboard", "go",
})


def _provider_root(root_url: str) -> str:
    """La raíz PÚBLICA del proveedor: esquema + host de marketing, sin la ruta.

    Dos correcciones sobre "esquema + host tal cual":
    1. Sin la ruta profunda de la app -- una URL adivinada que da 404 haría que
       el agente lea una página de error como si fuera la política, el bug que
       esta etapa evita. Se entra por la raíz y se busca el enlace.
    2. Si el host es un subdominio de APP (`app.plataforma-a.ejemplo`), se pasa al
       sitio público (`www.plataforma-a.ejemplo`). El recorrido real fallaba acá: el
       agente aterrizaba en la app de login, que no tiene pie de página ni enlace
       de privacidad, y no hay documento público que encontrar ahí.
    """
    parts = urlsplit(root_url)
    if not (parts.scheme and parts.netloc):
        return (root_url or "").rstrip("/")
    host = parts.netloc
    labels = host.split(".")
    if len(labels) >= 3 and labels[0].lower() in _APP_SUBDOMAINS:
        host = "www." + ".".join(labels[1:])
    return urlunsplit((parts.scheme, host, "", "", ""))


# --- la descripción de la plataforma -----------------------------------------
#
# POR QUÉ EXISTE. El mapa de arriba contesta tres preguntas fijas: ¿hay una
# lección?, ¿hay un chat del tutor?, ¿hay una política pública? Sirve para una
# plataforma con esa forma y deja ciego al plan frente a cualquier otra. En la
# corrida del 2026-11-25 la plataforma no tenía un chat en la esquina: el tutor
# intervenía después de un error, y el canal para escribirle aparecía recién
# ahí. El recorrido anotó "tutor encontrado", el plan se redactó para un chat
# libre que no existe, y se gastaron USD 7,22 antes de que una sola prueba
# pudiera ejecutarse. Siempre fue el mismo problema: gastar sin haber mirado.
#
# (Desde el piloto de punta a punta de ese mismo día el recorrido ya no son
# tres exploraciones fijas: ver `explore_platform` y `frontier.py`. Lo que
# sigue describe el punto de partida.)
#
# Entonces el recorrido INDAGA antes de que nadie piense el plan. Tres
# exploraciones, cada una con su tope, cada una usando la plataforma como un
# estudiante (ver `agent_driver.explore_tools`: mira y toca, no escribe):
#
#   1. cómo se entra y cómo está organizada;
#   2. una actividad por dentro, respondiendo bien y mal para ver qué pasa
#      después de cada cosa;
#   3. todo lo demás que ofrezca (vista docente, informes, ayuda, privacidad).
#
# Lo que devuelven se une en un perfil que el redactor de pruebas recibe (ver
# `generator._platform_block`). No depende de que exista un documento del
# proveedor: cuando lo hay, le dice al explorador dónde mirar, y lo que el
# documento afirma y no se pudo ver queda aparte, como no verificado.

#: La primera exploración: entrar y dejar a la vista TODO lo que hay para abrir.
_ENTRY = (
    "entrada e inventario",
    "Entendé CÓMO SE ENTRA y QUÉ HAY PARA ABRIR. Desde la dirección inicial: qué "
    "hay que tocar para empezar a usar la plataforma y cómo se elige qué estudiar "
    "(materias, unidades, temas, niveles). Registrá con `record_part` la pantalla "
    "de ingreso y la pantalla principal una vez adentro. Después hacé el "
    "INVENTARIO: cada ítem de menú, cada botón de ingreso y cada cambio de rol o "
    "de vista (docente, administrador, familia) que veas va en `entradas_vistas`, "
    "uno por uno, lo abras o no. No respondas actividades todavía y no te quedes "
    "en una sola sección: otras exploraciones entran a cada una.",
)

_ACTIVITY = (
    "una actividad por dentro",
    "Entendé QUÉ PASA DENTRO DE UNA ACTIVIDAD. Abrí el primer contenido "
    "disponible y trabajá como un estudiante: respondé al menos una actividad "
    "BIEN y al menos una MAL, y seguí cada camino hasta donde llegue. Registrá "
    "con `record_part` cada momento distinto: la pregunta, lo que aparece "
    "después de un acierto, lo que aparece después de un error, cada ayuda, y el "
    "tutor o asistente de IA si aparece (en qué momento, y si hay un campo para "
    "escribirle y a quién le llega lo escrito). Anotá cómo son las preguntas: "
    "formato, cuántas opciones, el enunciado textual de las que viste, y los "
    "nombres de los temas o subtemas entre los que se puede elegir.",
)

#: Cuántas exploraciones, como mucho, se gastan en vaciar la frontera. Es un
#: tope de gasto, no una definición de "listo": lo que quede sin cubrir cuando
#: se acaba figura en el autochequeo con su nombre.
MAX_FRONTIER_ROUNDS = int(os.getenv("LXP_RECON_FRONTIER_ROUNDS", "4"))

#: Entradas comunes (no de rol) que una misma exploración recorre juntas.
_BATCH = 5

# --- experimentos: lo que se averigua HACIENDO --------------------------------
#
# Cada uno es UN contraste controlado, con el oráculo escrito ANTES de correr:
# qué resultado significaría qué. Así el recorrido no puede acomodar la
# conclusión a lo que haya salido. Corre una vez y lo dice: con una corrida no
# se afirma "siempre".

_MASTERY = {
    "id": "regla_de_avance",
    "pregunta": (
        "¿Qué hace la plataforma cuando el estudiante falla varias veces "
        "seguidas en el mismo tema, y de qué depende avanzar?"
    ),
    "oraculo": (
        "Depende del dominio si, tras varios errores seguidos, sigue dando "
        "práctica del mismo tema (o más fácil, o un repaso) y el indicador de "
        "avance no sube. No depende si deja pasar al contenido siguiente igual "
        "o el avance sube con sólo responder. No concluyente si la pantalla no "
        "muestra ningún indicador de avance."
    ),
    "encargo": (
        "EXPERIMENTO: la regla de avance. Entrá a UN subtema y respondé MAL a "
        "propósito cinco preguntas seguidas (elegí cada vez una opción que sepas "
        "incorrecta y seguí hasta la pregunta siguiente). Antes de empezar, "
        "registrá con `record_part` el indicador de avance o de nivel que se vea. "
        "Después de los cinco errores registrá con `record_behavior` qué cambió: "
        "el indicador, el tipo de preguntas, si aparece un repaso, un aviso o un "
        "bloqueo. Luego respondé BIEN tres preguntas seguidas y registrá con otro "
        "`record_behavior` qué cambió ahora. En `conclusion` decí de qué depende "
        "avanzar según lo que viste, o 'no concluyente'."
    ),
}

_MEMORY = {
    "id": "memoria_entre_sesiones",
    "pregunta": (
        "¿Qué conserva la plataforma de un estudiante cuando cierra y vuelve?"
    ),
    "oraculo": (
        "Conserva si, al volver en una sesión nueva, muestra por sí sola el "
        "avance, el tema o la pregunta en que quedó el estudiante. No conserva "
        "si vuelve al estado inicial. Lo que el tutor de IA recuerda de lo "
        "CONVERSADO no se averigua acá (el recorrido no escribe): eso lo prueban "
        "las pruebas del plan."
    ),
    "encargo_1": (
        "EXPERIMENTO: memoria entre sesiones, PRIMERA sesión. Dejá una huella "
        "reconocible: entrá a un subtema y respondé dos preguntas, una bien y "
        "una mal. Registrá con `record_behavior` (nombre: 'huella de la primera "
        "sesión') el estado EXACTO que queda a la vista: nombre del subtema, su "
        "estado o nivel, el indicador de avance, en qué pregunta quedaste y lo "
        "último que dijo la plataforma o el tutor. No cierres ni borres nada."
    ),
    "encargo_2": (
        "EXPERIMENTO: memoria entre sesiones, SEGUNDA sesión. Sos el mismo "
        "estudiante que vuelve después de cerrar. Lo primero: registrá con "
        "`record_part` la pantalla que aparece al volver, antes de tocar nada. "
        "Después buscá cada elemento de la huella de la primera sesión (está "
        "abajo): entrá al mismo subtema y mirá su estado, el indicador de avance "
        "y si retoma la pregunta en que quedaste. Registrá con `record_behavior` "
        "(nombre: 'qué se conserva al volver') qué se conserva y qué no, "
        "elemento por elemento."
    ),
}

#: Cuánto texto de documentos se le pasa al explorador. Es una guía de dónde
#: mirar, no el documento entero: el resto lo lee la rúbrica, que es su lugar.
_DOC_HINT_CHARS = 6000


def _document_hints(documents: Any) -> str:
    chunks = []
    for d in documents or []:
        text = str((d or {}).get("text") or "").strip()
        if text:
            name = str((d or {}).get("name") or "documento").strip()
            chunks.append(f"[{name}]\n{text}")
    if not chunks:
        return ""
    joined = "\n\n".join(chunks)[:_DOC_HINT_CHARS]
    return (
        "\n\nDOCUMENTOS DEL PROVEEDOR (sin verificar). Te dicen dónde mirar; no "
        "son lo que viste. Buscá las partes que describen. Lo que afirmen y no "
        "puedas ver en pantalla va en `documentado_sin_verificar`, nunca en "
        f"`partes`.\n\n{joined}"
    )


def _exploration_task(
    focus: str, root_url: str, hints: str, already: list[str] | None = None,
    *, navigate: bool = True,
) -> str:
    """El encargo de UNA exploración.

    `already` son las partes que una exploración anterior ya registró. Sin
    eso, el primer recorrido en vivo que informó (2026-11-25) devolvió 27
    entradas para unas 14 partes reales -- la pantalla de inicio descrita tres
    veces -- y, peor, la primera exploración anotó como "no verificado" el
    camino del error que la segunda sí recorrió. Un perfil que se contradice le
    hace dudar al redactor de lo que sí se vio.
    """
    previo = ""
    if already:
        previo = (
            "\n\nYA REGISTRADAS por una exploración anterior (no las repitas; "
            "registrá sólo lo nuevo, o lo que veas distinto): "
            + "; ".join(already) + "."
        )
    start = (
        f"La plataforma bajo evaluación está en {root_url} . Empezá navegando a "
        "esa dirección."
        if navigate else
        f"La plataforma bajo evaluación está en {root_url} y ya está abierta: "
        "empezá leyendo la pantalla actual."
    )
    return (
        f"{start}\n\n{focus}\n\n"
        "Limitate a este encargo. Lo que queda fuera de él NO lo anotes como no "
        "encontrado ni como no verificado: otra exploración lo mira."
        f"{previo}{hints}"
    )


def _frontier_focus(batch: list[dict]) -> str:
    """El encargo para vaciar una parte de la frontera."""
    if len(batch) == 1 and batch[0].get("tipo") == "rol":
        text = batch[0]["texto"]
        return (
            f"RECORRÉ OTRO ROL. En la plataforma hay una entrada «{text}» que "
            "todavía no se abrió. Entrá por ahí y recorré TODO lo que ese rol "
            "expone: cada sección de su menú, cada pestaña, cada panel y el "
            "detalle que se abre al tocar un elemento (por ejemplo, un "
            "estudiante de la lista). Registrá cada pantalla con `record_part`, "
            f"con el `rol` que corresponda y `entrada` = «{text}» en la primera; "
            "en las secciones internas, `entrada` = el texto de su propio ítem. "
            "Mirá sin cambiar nada: no toques controles que afecten a otras "
            "personas o borren datos (cerrar una clase, reiniciar, borrar). Si "
            "no se puede entrar (pide otra cuenta, da error), declarala en "
            "`entradas_vistas` como `no_disponible` con el motivo exacto que "
            "muestra la pantalla."
        )
    listed = "; ".join(f"«{e['texto']}»" for e in batch)
    return (
        "RECORRÉ ESTAS ENTRADAS, que se vieron en el menú y todavía no se "
        f"abrieron: {listed}. Abrí cada una y registrala con `record_part`, "
        "poniendo en `entrada` su texto EXACTO, tal como figura acá. Si alguna "
        "no se puede abrir, declarala en `entradas_vistas` como `no_disponible` "
        "con el motivo. Mirá sin cambiar nada: no toques controles que borren "
        "datos o afecten a otras personas."
    )


def _empty_profile(root_url: str) -> dict[str, Any]:
    return {
        "version": 2,
        "direccion": root_url,
        "entrada": "",
        "partes": [],
        "comportamientos": [],
        "experimentos": [],
        "entradas": [],
        "roles": [],
        "cobertura": {},
        "no_encontrado": [],
        "documentado_sin_verificar": [],
        "sin_explorar": [],
        "notas": "",
    }


def _merge_profile(profile: dict, report: dict, label: str = "") -> None:
    """Suma el informe de UNA exploración al perfil. Las partes no se repiten.

    Cada parte recibe un id (`P1`, `P2`...) y cada comportamiento otro (`C1`...):
    es con lo que el plan cita "esto se apoya en la pantalla P7", y lo que deja
    comprobar en código que nadie citó una pantalla que no se vio.
    """
    profile.setdefault("comportamientos", [])
    if not profile.get("entrada") and str(report.get("entrada") or "").strip():
        profile["entrada"] = str(report["entrada"]).strip()
    seen = {str(p.get("nombre", "")).strip().lower() for p in profile["partes"]}
    for part in report.get("partes") or []:
        if not isinstance(part, dict):
            continue
        name = str(part.get("nombre") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        clean = {
            k: (v if isinstance(v, list) else str(v).strip())
            for k, v in part.items()
            if (v if isinstance(v, list) else str(v or "").strip())
        }
        clean["id"] = f"P{len(profile['partes']) + 1}"
        if label:
            clean["exploracion"] = label
        profile["partes"].append(clean)
    for behavior in report.get("comportamientos") or []:
        if not isinstance(behavior, dict) or not str(behavior.get("nombre") or "").strip():
            continue
        clean = {k: str(v).strip() for k, v in behavior.items() if str(v or "").strip()}
        clean["id"] = f"C{len(profile['comportamientos']) + 1}"
        if label:
            clean["exploracion"] = label
        profile["comportamientos"].append(clean)
    for key in ("no_encontrado", "documentado_sin_verificar"):
        for item in report.get(key) or []:
            text = str(item or "").strip()
            if text and text not in profile[key]:
                profile[key].append(text)
    note = str(report.get("notas") or "").strip()
    if note:
        profile["notas"] = (profile["notas"] + "\n" + note).strip()


async def explore_platform(
    agent: Any,
    *,
    root_url: str,
    documents: Any = None,
    on_progress: Any = None,
    must_visit: list[dict] | None = None,
) -> dict | None:
    """Describe la plataforma. None si el agente no sabe explorar.

    `must_visit` son lugares que una PERSONA dijo que existen (las correcciones
    al plan anterior): entran a la frontera como entradas obligatorias, con la
    dirección o la nota que haya dado.

    EL ORDEN, y por qué no es una narración de punta a punta. Dos exploraciones
    fijas (entrar e inventariar; una actividad por dentro) y después un bucle
    que NO decide el agente: mientras la frontera tenga entradas sin cubrir y
    quede presupuesto, se manda una exploración a la siguiente, los cambios de
    rol primero. Seguir un camino hasta el fondo es lo que hace que un
    recorrido persiga el caso feliz del estudiante y nunca vuelva a la barra de
    menú; acá a la barra de menú se vuelve porque la lista lo exige.

    Al final van los experimentos (ver `_MASTERY`, `_MEMORY`) y el autochequeo.

    Una exploración que falla NO tumba las otras: queda anotada en
    `sin_explorar` con su motivo y el perfil lleva lo que sí se vio. Es la
    misma regla que el mapa de superficies: no poder ver algo es un hallazgo.
    """
    from . import frontier as fr

    explore = getattr(agent, "explore", None)
    if explore is None:
        return None
    profile = _empty_profile(root_url)
    hints = _document_hints(documents)
    declared: list[dict] = [dict(e) for e in (must_visit or []) if isinstance(e, dict)]
    hints_by_entry = {
        fr.normalize(e.get("texto")): e for e in declared if e.get("url") or e.get("nota")
    }
    done = 0
    total = 2 + MAX_FRONTIER_ROUNDS + 3

    async def run(label: str, focus: str, *, navigate: bool = True) -> dict | None:
        nonlocal done
        done += 1
        if on_progress is not None:
            try:
                on_progress(done, max(total, done), label)
            except Exception:  # noqa: BLE001 -- el aviso no puede tumbar el recorrido
                logger.debug("no pude avisar el avance del recorrido", exc_info=True)
        before = (len(profile["partes"]), len(profile["comportamientos"]))
        try:
            already = [str(p.get("nombre", "")) for p in profile["partes"]]
            report = await explore(
                _exploration_task(focus, root_url, hints, already, navigate=navigate)
            )
        except Exception as exc:  # noqa: BLE001 -- una exploración fallida es un hallazgo
            profile["sin_explorar"].append(f"{label}: {exc}")
            return None
        report = report if isinstance(report, dict) else {}
        _merge_profile(profile, report, label)
        declared.extend(e for e in (report.get("entradas_vistas") or []) if isinstance(e, dict))
        if report.get("incompleto"):
            profile["sin_explorar"].append(
                f"{label}: terminó sin el informe de cierre ({report['incompleto']}); "
                "lo que registró al pasar se conserva"
            )
        report["_nuevas"] = (
            profile["partes"][before[0]:], profile["comportamientos"][before[1]:]
        )
        return report

    await run(*_ENTRY)
    await run(*_ACTIVITY)

    # --- vaciar la frontera ---------------------------------------------------
    tried: set[str] = set()
    for _ in range(MAX_FRONTIER_ROUNDS):
        left = [
            e for e in fr.pending(fr.build_frontier(profile["partes"], declared))
            if fr.normalize(e["texto"]) not in tried
        ]
        if not left:
            break
        if left[0]["tipo"] == "rol":
            batch = [left[0]]
            label = f"otro rol: {left[0]['texto']}"
        else:
            batch = left[:_BATCH]
            label = "entradas del menú: " + ", ".join(e["texto"] for e in batch)
        tried.update(fr.normalize(e["texto"]) for e in batch)
        told = [
            hints_by_entry[fr.normalize(e["texto"])] for e in batch
            if fr.normalize(e["texto"]) in hints_by_entry
        ]
        extra = "".join(
            f"\n\nUNA PERSONA INDICÓ dónde está «{h['texto']}»: "
            f"{h.get('url') or ''} {h.get('nota') or ''}".rstrip()
            for h in told
        )
        await run(label, _frontier_focus(batch) + extra)

    # --- experimentos ---------------------------------------------------------
    for spec, runs in ((_MASTERY, ("encargo",)), (_MEMORY, ("encargo_1", "encargo_2"))):
        result: dict[str, Any] = {
            "id": spec["id"], "pregunta": spec["pregunta"], "oraculo": spec["oraculo"],
            "corridas": 1, "observaciones": [], "estado": "hecho",
        }
        for index, key in enumerate(runs):
            task = spec[key]
            navigate = True
            if index == 1:
                # SEGUNDA SESIÓN: página nueva, mismo contexto (el estudiante
                # vuelve). Sin esa operación no hay segundo término del
                # contraste, y un "experimento" de una sola sesión no dice nada.
                reopen = getattr(agent, "new_session", None)
                reopened = False
                if reopen is not None:
                    try:
                        reopened = bool(await reopen())
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("no se pudo abrir la segunda sesión: %s", exc)
                if not reopened:
                    result["estado"] = "no se pudo: el navegador no abrió una segunda sesión"
                    break
                trace = [
                    f"- {b.get('nombre', '')}: {b.get('que_observe', '')}"
                    for b in profile["comportamientos"]
                    if b.get("id") in result["observaciones"]
                ]
                task += "\n\nHUELLA DE LA PRIMERA SESIÓN:\n" + (
                    "\n".join(trace) or "(la primera sesión no registró una huella)"
                )
                navigate = False
            label = f"experimento: {spec['id']}" + (f" ({index + 1} de {len(runs)})" if len(runs) > 1 else "")
            report = await run(label, task, navigate=navigate)
            if report is None:
                result["estado"] = "no se pudo: " + profile["sin_explorar"][-1]
                break
            result["observaciones"] += [b["id"] for b in report["_nuevas"][1]]
        if result["estado"] == "hecho" and not result["observaciones"]:
            result["estado"] = "no concluyente: el explorador no registró ninguna observación"
        profile["experimentos"].append(result)

    # --- autochequeo ----------------------------------------------------------
    frontier = fr.build_frontier(profile["partes"], declared)
    profile["entradas"] = frontier
    tree_seen = any(p.get("texto") for p in profile["partes"])
    profile["cobertura"] = fr.coverage(frontier, profile["partes"], tree_seen=tree_seen)
    profile["roles"] = sorted({str(p.get("rol")) for p in profile["partes"] if p.get("rol")})
    return profile


#: Cuántos criterios pendientes se le encargan a UNA exploración dirigida.
#: Pocos: cada uno es un lugar distinto de la plataforma, y una exploración que
#: persigue seis termina sin turnos a mitad del tercero.
REVISIT_BATCH = int(os.getenv("LXP_RECON_REVISIT_BATCH", "3"))
#: Tope de exploraciones dirigidas por plan. Es un freno de gasto, no un
#: presupuesto: lo que no entra queda como estaba, dicho.
MAX_REVISITS = int(os.getenv("LXP_RECON_MAX_REVISITS", "4"))

REVISIT_DONE = "hecha"


def _revisit_focus(batch: list[dict]) -> str:
    items = "\n".join(
        f"- {d.get('id')} ({d.get('nombre') or 'sin nombre'}): {d.get('motivo') or ''}"
        for d in batch
    )
    return (
        "EXPLORACIÓN DIRIGIDA. El plan dejó SIN DECIDIR estos criterios porque el "
        "recorrido no llegó a la pantalla donde se probarían. Cada uno dice qué "
        f"habría que mirar:\n{items}\n\n"
        "Andá a buscar esas pantallas siguiendo esas indicaciones, una por una. "
        "Registrá con `record_part` cada pantalla nueva a la que llegues y con "
        "`record_behavior` lo que averigües haciendo (por ejemplo, qué aparece "
        "al empezar un contenido que nunca se abrió, o al volver a uno ya "
        "empezado). Podés responder actividades para llegar. No podés escribir "
        "texto: si una pantalla sólo avanza escribiendo, registrala hasta donde "
        "se llega y decí que lo que sigue pide escribir.\n\n"
        "Si después de buscar por el camino indicado una pantalla NO EXISTE, "
        "anotalo en `no_encontrado` diciendo qué buscaste, por dónde y qué había "
        "en su lugar. 'No existe' es una respuesta válida y es lo que este "
        "encargo tiene que poder decir; lo que no vale es darla por inexistente "
        "sin haber abierto el lugar que se indica."
    )


async def revisit(
    agent: Any,
    profile: dict,
    pending: list[dict],
    *,
    root_url: str,
    prepare: Any = None,
    on_progress: Any = None,
) -> dict[str, str]:
    """Vuelve a la plataforma a buscar lo que el plan dejó en "falta recorrer".

    POR QUÉ. En la corrida completa del 2026-11-26 siete criterios quedaron sin
    evaluar por "falta recorrer". El planificador había hecho bien su parte: no
    los dio por inexistentes y escribió, para cada uno, qué habría que mirar
    ("abrir un subtema 'Sin empezar' y ver si hay modelado antes de la primera
    pregunta", "abrir 'Mezclar subtemas, como en una prueba'"). Pero nadie iba a
    mirar: el recorrido ya había terminado, y esa indicación sólo servía para
    que una persona la leyera en el plan. El resultado era un criterio sin
    respuesta por una pantalla que estaba a dos toques.

    Esto cierra ese círculo: lo que el planificador pide mirar se le encarga al
    explorador, en tandas chicas, y lo que encuentra se suma al MISMO perfil
    (con sus capturas e ids nuevos), para que el planificador decida otra vez
    con eso a la vista.

    `pending` son las decisiones en `falta_recorrer` (con `id`, `nombre`,
    `motivo`). `prepare` es una corrutina opcional que deja la plataforma lista
    antes de cada exploración (volver al inicio, entrar con un estudiante
    nuevo); con ella la exploración arranca leyendo la pantalla en vez de
    navegar.

    Devuelve, por id de criterio, `REVISIT_DONE` o el motivo por el que su
    exploración no se completó. Sólo de una exploración COMPLETA se puede
    concluir "esa pantalla no existe"; de una que se quedó sin turnos, no.
    """
    explore = getattr(agent, "explore", None)
    outcome: dict[str, str] = {}
    todo = [d for d in (pending or []) if isinstance(d, dict) and d.get("id")]
    if explore is None or not todo:
        return outcome
    profile.setdefault("partes", [])
    profile.setdefault("comportamientos", [])
    for key in ("no_encontrado", "documentado_sin_verificar", "sin_explorar"):
        profile.setdefault(key, [])
    profile.setdefault("notas", "")
    batches = [todo[i:i + REVISIT_BATCH] for i in range(0, len(todo), REVISIT_BATCH)]
    for number, batch in enumerate(batches, start=1):
        ids = [str(d["id"]) for d in batch]
        label = "dirigida: " + ", ".join(ids)
        if number > MAX_REVISITS:
            for cid in ids:
                outcome[cid] = (
                    f"no se exploró: el plan admite {MAX_REVISITS} exploraciones "
                    "dirigidas y esta quedó afuera"
                )
            continue
        if on_progress is not None:
            try:
                on_progress(number, len(batches), label)
            except Exception:  # noqa: BLE001 -- el aviso no puede tumbar el recorrido
                logger.debug("no pude avisar el avance del recorrido dirigido", exc_info=True)
        navigate = True
        try:
            if prepare is not None:
                await prepare(number)
                navigate = False
            already = [str(p.get("nombre", "")) for p in profile["partes"]]
            report = await explore(
                _exploration_task(_revisit_focus(batch), root_url, "", already, navigate=navigate)
            )
        except Exception as exc:  # noqa: BLE001 -- una exploración fallida es un hallazgo
            profile["sin_explorar"].append(f"{label}: {exc}")
            for cid in ids:
                outcome[cid] = f"no se pudo explorar: {exc}"
            continue
        report = report if isinstance(report, dict) else {}
        _merge_profile(profile, report, label)
        if report.get("incompleto"):
            why = f"terminó sin el informe de cierre ({report['incompleto']})"
            profile["sin_explorar"].append(f"{label}: {why}; lo que registró al pasar se conserva")
            for cid in ids:
                outcome[cid] = why
        else:
            for cid in ids:
                outcome[cid] = REVISIT_DONE
    return outcome


def render_profile(profile: dict) -> str:
    """El perfil como texto, para quien redacta, planifica y juzga las pruebas."""
    lines = [f"Dirección: {profile.get('direccion') or '(no registrada)'}"]
    if profile.get("entrada"):
        lines.append(f"Cómo se entra: {profile['entrada']}")
    parts = profile.get("partes") or []
    if parts:
        lines.append("Partes que se vieron:")
        for n, part in enumerate(parts, start=1):
            tag = part.get("id") or str(n)
            role = f" [{part['rol']}]" if part.get("rol") else ""
            lines.append(f"{tag}. {part.get('nombre', '(sin nombre)')}{role}")
            for key, human in (
                ("como_llegar", "cómo se llega"),
                ("que_muestra", "qué muestra"),
                ("que_permite", "qué permite"),
                ("url", "dirección"),
            ):
                if part.get(key):
                    lines.append(f"   - {human}: {part[key]}")
    else:
        lines.append("Partes que se vieron: ninguna.")
    behaviors = profile.get("comportamientos") or []
    if behaviors:
        lines.append("Comportamientos que se averiguaron haciendo:")
        for b in behaviors:
            lines.append(f"{b.get('id', '-')}. {b.get('nombre', '')}")
            for key, human in (
                ("que_hice", "qué se hizo"),
                ("que_observe", "qué pasó"),
                ("conclusion", "conclusión"),
            ):
                if b.get(key):
                    lines.append(f"   - {human}: {b[key]}")
    for exp in profile.get("experimentos") or []:
        lines.append(
            f"Experimento «{exp.get('id')}» ({exp.get('corridas', 1)} corrida): "
            f"{exp.get('estado')}. Observaciones: "
            f"{', '.join(exp.get('observaciones') or []) or 'ninguna'}."
        )
    cover = profile.get("cobertura") or {}
    if cover.get("sin_cubrir"):
        lines.append(
            "Entradas que se VIERON y NO se recorrieron (no hay camino conocido "
            "hacia ellas): " + "; ".join(cover["sin_cubrir"]) + "."
        )
    for item in cover.get("no_disponibles") or []:
        lines.append(f"Entrada no disponible: {item['texto']} ({item['motivo']}).")
    for key, human in (
        ("no_encontrado", "Se buscó y NO se encontró"),
        ("documentado_sin_verificar",
         "Lo afirma un documento del proveedor y NO se verificó en pantalla"),
        ("sin_explorar", "No se pudo explorar"),
    ):
        items = profile.get(key) or []
        if items:
            lines.append(f"{human}:")
            lines.extend(f"   - {item}" for item in items)
    if profile.get("notas"):
        lines.append(f"Notas del recorrido: {profile['notas']}")
    return "\n".join(lines)


async def discover_surfaces(
    agent: Any,
    *,
    root_url: str,
    documents: Any = None,
    on_progress: Any = None,
    must_visit: list[dict] | None = None,
) -> PlatformMap:
    """Recorre la plataforma logueada y devuelve su mapa de superficies.

    El `agent` es un `AgentDriver` (interfaz async: assert_session_alive /
    is_logged_out / url / do(paso en prosa) / read_page / screenshot). Se recorre
    en profundidad MEDIA -- sesión, cursos, UNA lección por dentro, ubicar el
    tutor, la política pública -- y se ANOTA lo que no aparece.

    Después, si el agente sabe explorar, se describe la plataforma entera (ver
    `explore_platform`) y esa descripción viaja en el mapa como `profile`. Va
    al final porque cada exploración arranca navegando a la dirección inicial,
    así que no depende de dónde haya quedado el navegador.
    """
    m = PlatformMap()

    # 1. Sesión viva. Si no lo está, se propaga: sin sesión no hay recorrido, y
    #    el llamador convierte esto en un fallo barato (cero gasto en LLM). Se
    #    pregunta sólo si el driver expone la comprobación -- misma tolerancia
    #    que traía `_recon` antes de este recorrido: un `SessionExpired` sube,
    #    pero un doble que no la implementa no se convierte en un falso fallo.
    check = getattr(agent, "assert_session_alive", None)
    if check is not None:
        await check()

    # 2. Leer la pantalla inicial e identificar los cursos ofrecidos.
    course: str | None = None
    landing_read = True
    try:
        landing = await agent.read_page()
        await _snap(agent, "recon_landing")
        course = _detect_course(landing)
    except Exception as exc:  # noqa: BLE001 -- una lectura fallida es un hallazgo
        landing_read = False
        reason = f"No se pudo leer la pagina de inicio para ubicar cursos: {exc}"
        m.unavailable("lesson_content", reason)
        m.unavailable("tutor_chat", reason)
        landing = ""

    # 3 + 4. Entrar a UN curso y UNA lección; dentro, buscar el tutor.
    if landing_read and course is None:
        seen = _short(landing) or "no se ofrecieron cursos en la pagina de inicio"
        m.unavailable(
            "lesson_content",
            f"La cuenta no ofrece ningun curso donde entrar. Se vio: {seen}",
        )
        m.unavailable(
            "tutor_chat",
            "No hay curso ni leccion donde pueda vivir el tutor de IA. "
            f"Se vio: {seen}",
        )
    elif landing_read:
        # `course` sólo dice que HAY cursos; NO se usa como nombre para navegar
        # (ver la nota en `_LESSON_STEPS`). Se navega por posición, con los mismos
        # pasos que se guardan en el mapa, así descubrimiento y re-ejecución son
        # idénticos.
        lesson_text: str | None = None
        try:
            for step in _LESSON_STEPS:
                await agent.do(step)
            lesson_text = await agent.read_page()
            await _snap(agent, "recon_lesson")
            m.found(
                "lesson_content",
                name="Contenido de una leccion",
                steps=list(_LESSON_STEPS),
                read=list(_LESSON_READ),
                # DÓNDE se llegó de verdad. Sin esto el mapa sólo repite los
                # pasos que se pidió ejecutar: si salen bien, se guardan tal
                # cual, y "descubrir" es indistinguible de "recordar". La URL
                # observada es lo que convierte el recorrido en evidencia de
                # que la superficie está ahí.
                observed=await _observed(agent),
            )
        except Exception as exc:  # noqa: BLE001
            reason = f"No se pudo abrir la primera leccion del primer curso: {exc}"
            m.unavailable("lesson_content", reason)
            m.unavailable("tutor_chat", reason)

        if lesson_text is not None:
            # El tutor se detecta ABRIÉNDOLO, no buscando la palabra "tutor" en
            # el texto. En Plataforma A es un botón circular violeta SIN texto en la
            # esquina de la lección: un ícono no aparece en el texto de la
            # página, así que la búsqueda por palabra lo daba por ausente aunque
            # estaba ahí. Se intenta el paso que lo abre; si el agente lo logra,
            # existe; si no, su explicación es el motivo. Abrir el panel es de
            # sólo lectura -- no se envía ningún mensaje (el agente ni siquiera
            # puede tipear).
            try:
                await agent.do(_TUTOR_OPEN_STEP)
                await _snap(agent, "recon_tutor")
                m.found(
                    "tutor_chat",
                    name="Chat del tutor de IA",
                    steps=list(_TUTOR_STEPS),
                    send=list(_TUTOR_SEND),
                    read=list(_TUTOR_READ),
                    # La más importante de las tres: dónde vive el tutor es
                    # justo lo que cambió entre julio y agosto, y lo que costó
                    # tres corridas pagas descubrir la primera vez.
                    observed=await _observed(agent),
                )
            except Exception as exc:  # noqa: BLE001
                m.unavailable(
                    "tutor_chat",
                    "No se pudo abrir un tutor de IA desde la primera leccion: "
                    f"{exc}",
                )

    # 5. Salir del login y buscar la política pública desde la raíz del proveedor.
    root = _provider_root(root_url)
    try:
        await agent.do(
            "Salir de la aplicacion logueada y navegar a la pagina publica del "
            f"proveedor en {root} ."
        )
        await agent.do(
            "En el pie de pagina o el menu principal, buscar y abrir el enlace "
            "de la politica de privacidad."
        )
        policy_text = await agent.read_page()
        await _snap(agent, "recon_privacy")
        if _looks_like_privacy(policy_text):
            m.found(
                "privacy_policy",
                name="Politica de privacidad publicada",
                no_session=True,
                steps=list(_PRIVACY_STEPS),
                read=list(_PRIVACY_READ),
                defaults={"root": root},
                # La URL a la que se llegó vale doble acá: una política se busca
                # desde la raíz pública, y anotar dónde terminó es lo que
                # permite comprobar después que no era un 404 disfrazado.
                observed=await _observed(agent),
            )
        else:
            m.unavailable(
                "privacy_policy",
                f"No se encontro una politica de privacidad publica desde {root}: "
                f"la pagina abierta no parece una politica. Se vio: "
                f"{_short(policy_text)}",
            )
    except Exception as exc:  # noqa: BLE001
        m.unavailable(
            "privacy_policy",
            f"No se pudo llegar a la politica de privacidad publica desde {root}: "
            f"{exc}",
        )

    # 6. Describir la plataforma entera, para que el plan se escriba mirándola.
    m.profile = await explore_platform(
        agent, root_url=root_url, documents=documents, on_progress=on_progress,
        must_visit=must_visit,
    )

    return m


async def _observed(agent: Any) -> dict | None:
    """Dónde quedó el navegador. Mejor esfuerzo: un doble sin `url` no rompe."""
    getter = getattr(agent, "url", None)
    if getter is None:
        return None
    try:
        return {"url": await getter()}
    except Exception:  # noqa: BLE001 -- anotar dónde estamos nunca tumba el recorrido
        return None


async def _snap(agent: Any, label: str) -> None:
    """Captura de pantalla como evidencia del recorrido. Best-effort: una captura
    que falla no debe tumbar el descubrimiento."""
    try:
        await agent.screenshot(label)
    except Exception:  # noqa: BLE001
        logger.debug("no se pudo capturar %s durante el reconocimiento", label)
