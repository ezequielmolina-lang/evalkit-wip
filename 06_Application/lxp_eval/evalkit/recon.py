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

_EXPLORATIONS: tuple[tuple[str, str], ...] = (
    (
        "entrada y estructura",
        "Entendé CÓMO SE ENTRA y CÓMO ESTÁ ORGANIZADA la plataforma. Desde la "
        "dirección inicial: qué hay que tocar para empezar a usarla, qué opciones "
        "tiene el menú, cómo se elige qué estudiar (materias, unidades, temas, "
        "niveles) y qué otras secciones se ven desde ahí. Abrí cada sección del "
        "menú lo suficiente para saber qué es. No respondas actividades todavía.",
    ),
    (
        "una actividad por dentro",
        "Entendé QUÉ PASA DENTRO DE UNA ACTIVIDAD. Abrí el primer contenido "
        "disponible y trabajá como un estudiante: respondé al menos una actividad "
        "BIEN y al menos una MAL, y seguí cada camino hasta donde llegue. Anotá "
        "qué muestra la plataforma después de un acierto y después de un error, "
        "qué ayudas ofrece, si aparece un tutor o asistente de IA y en qué "
        "momento, y si en algún punto hay un campo para escribirle (a quién le "
        "llega lo escrito). Anotá también cómo son las preguntas: formato, "
        "cuántas opciones, y el enunciado textual de las que viste.",
    ),
    (
        "otras partes",
        "Entendé LAS OTRAS PARTES de la plataforma, las que no son la actividad "
        "del estudiante: vista o panel docente, informes y seguimiento, repasos, "
        "cierres de sesión, ayuda, configuración, y dónde se informa sobre los "
        "datos y la privacidad. Abrí cada una y describí qué muestra y qué "
        "permite. Mirá sin cambiar nada: no toques controles que afecten a otras "
        "personas (cerrar una clase, reiniciar, borrar).",
    ),
)

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
    focus: str, root_url: str, hints: str, already: list[str] | None = None
) -> str:
    """El encargo de UNA exploración.

    `already` son las partes que una exploración anterior ya describió. Sin
    eso, el primer recorrido en vivo que informó (2026-11-25) devolvió 27
    entradas para unas 14 partes reales -- la pantalla de inicio descrita tres
    veces -- y, peor, la primera exploración anotó como "no verificado" el
    camino del error que la segunda sí recorrió. Un perfil que se contradice le
    hace dudar al redactor de lo que sí se vio.
    """
    previo = ""
    if already:
        previo = (
            "\n\nYA DESCRITAS por una exploración anterior (no las repitas; "
            "describí sólo lo nuevo, o lo que veas distinto): "
            + "; ".join(already) + "."
        )
    return (
        f"La plataforma bajo evaluación está en {root_url} . Empezá navegando a "
        f"esa dirección.\n\n{focus}\n\n"
        "Limitate a este encargo. Lo que queda fuera de él NO lo anotes como no "
        "encontrado ni como no verificado: otra exploración lo mira."
        f"{previo}{hints}"
    )


def _merge_profile(profile: dict, report: dict) -> None:
    """Suma el informe de UNA exploración al perfil. Las partes no se repiten."""
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
        profile["partes"].append(
            {k: str(v).strip() for k, v in part.items() if str(v or "").strip()}
        )
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
) -> dict | None:
    """Describe la plataforma. None si el agente no sabe explorar.

    Una exploración que falla NO tumba las otras: queda anotada en
    `sin_explorar` con su motivo y el perfil lleva lo que sí se vio. Es la
    misma regla que el mapa de superficies: no poder ver algo es un hallazgo.
    """
    explore = getattr(agent, "explore", None)
    if explore is None:
        return None
    profile: dict[str, Any] = {
        "direccion": root_url,
        "entrada": "",
        "partes": [],
        "no_encontrado": [],
        "documentado_sin_verificar": [],
        "sin_explorar": [],
        "notas": "",
    }
    hints = _document_hints(documents)
    for index, (label, focus) in enumerate(_EXPLORATIONS, start=1):
        if on_progress is not None:
            try:
                on_progress(index, len(_EXPLORATIONS), label)
            except Exception:  # noqa: BLE001 -- el aviso no puede tumbar el recorrido
                logger.debug("no pude avisar el avance del recorrido", exc_info=True)
        try:
            already = [str(p.get("nombre", "")) for p in profile["partes"]]
            report = await explore(
                _exploration_task(focus, root_url, hints, already)
            )
        except Exception as exc:  # noqa: BLE001 -- una exploración fallida es un hallazgo
            profile["sin_explorar"].append(f"{label}: {exc}")
            continue
        _merge_profile(profile, report if isinstance(report, dict) else {})
    return profile


def render_profile(profile: dict) -> str:
    """El perfil como texto, para quien redacta y juzga las pruebas."""
    lines = [f"Dirección: {profile.get('direccion') or '(no registrada)'}"]
    if profile.get("entrada"):
        lines.append(f"Cómo se entra: {profile['entrada']}")
    parts = profile.get("partes") or []
    if parts:
        lines.append("Partes que se vieron:")
        for n, part in enumerate(parts, start=1):
            lines.append(f"{n}. {part.get('nombre', '(sin nombre)')}")
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
        agent, root_url=root_url, documents=documents, on_progress=on_progress
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
