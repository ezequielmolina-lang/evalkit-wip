"""Los pasos de una prueba: sus marcas y la revisión que no necesita un modelo.

POR QUÉ EXISTE. La corrida completa del 2026-11-25 (120 pruebas contra una
plataforma viva) cortó 50, y ninguna por algo que la plataforma hiciera mal:

* 20 pedían tocar un botón por su texto exacto ("Sí, hice eso", "Siguiente
  pregunta") que la plataforma muestra sólo por uno de sus caminos. Quien
  ejecuta no encontró el botón, dijo que el paso no se había hecho, y la prueba
  se cortó con la evidencia que el criterio necesitaba ya en pantalla.
* 2 llevaban un paso condicional ("si tras la actividad 2 aparece..."), que la
  regla del redactor prohíbe desde el piloto de ese mismo día. La regla estaba
  escrita en el prompt y el consejo de siete jueces no las frenó.
* 2 pedían grabar la pantalla para medir una latencia: algo que el motor no
  hace.
* 8 juntaban varias acciones en un paso ("responder bien hasta que...") y
  agotaron los turnos.

Una regla que sólo vive en un prompt se cumple casi siempre. Acá va la parte
que se puede comprobar con código, antes de gastar un consejo o un navegador.

LAS DOS MARCAS. Van al principio del texto del paso, porque el paso viaja como
texto de punta a punta (redactor, plan, registro, runner):

* `[SI APARECE]`: el paso depende de algo que la plataforma puede mostrar o no.
  Si no aparece, quien ejecuta lo dice y la prueba SIGUE; la captura se guarda
  con esa aclaración en la nota, para que el juez sepa qué está mirando. Es la
  forma controlada de un condicional: no hay rama, hay un paso que puede no
  aplicar y queda dicho.
* `[VARIOS TURNOS]`: un solo objetivo que por naturaleza lleva varias
  interacciones (llegar a un contenido por su ruta, responder bien una
  pregunta). Recibe el doble de turnos. No es permiso para repetir.
"""
from __future__ import annotations

import re

OPTIONAL_MARK = "[SI APARECE]"
LONG_MARK = "[VARIOS TURNOS]"
MARKS = (OPTIONAL_MARK, LONG_MARK)

#: Con esto empieza lo que devuelve quien ejecuta cuando un paso [SI APARECE] se
#: quedó sin turnos: el runner lo anota distinto de un "no apareció" dicho por
#: quien miró la pantalla (ver runner._note y agent_driver.do).
NOT_FOUND = "SIN ENCONTRAR: "

#: Lo que devuelve quien ejecuta cuando un paso que sólo mira se resolvió sin
#: llamar al modelo: el runner guarda la pantalla tal como está y lo dice en la
#: nota (ver agent_driver.do para el porqué y runner._note).
DIRECT = "CAPTURA DIRECTA"

#: Con esto empieza lo que devuelve quien ejecuta cuando el paso SE CUMPLIÓ pero
#: por otro camino que el escrito (respondió una pregunta para poder avanzar,
#: encontró ya hecho lo que el paso pedía). El runner lo anota en la captura: el
#: juez lee el nombre del paso y tiene que saber que no se hizo al pie de la
#: letra (ver agent_driver.STEP_DONE_TOOL, campo `desvio`).
DEVIATION = "DESVÍO: "

#: Más de esta fracción de pasos opcionales y la prueba puede terminar sin
#: haber hecho nada: deja de ser una prueba.
MAX_OPTIONAL_SHARE = 0.5


#: Una etiqueta que el redactor antepone al paso ("[VARIANTE: sólo celda
#: estudiante]"). No es una instrucción para quien ejecuta. Lleva dos puntos a
#: propósito: `[TEMA]` y `[VARIANTE]` a secas son los huecos que rellena el plan
#: (ver planner.instantiate) y no se tocan.
_LABEL = re.compile(r"^\[[A-Za-zÁÉÍÓÚÑáéíóúñ ]{3,24}:[^\]\n]{1,80}\]\s*")
#: La marca escrita al final, suelta o entre paréntesis: "... ([VARIOS TURNOS])".
_TRAILING_MARK = re.compile(
    r"\s*\(?\s*(\[SI APARECE\]|\[VARIOS TURNOS\])\s*\)?\s*\.?\s*$", re.IGNORECASE
)


def split_marks(step: str) -> tuple[str, bool, bool]:
    """(texto sin marcas ni etiquetas, es opcional, lleva varios turnos).

    POR QUÉ MIRA MÁS QUE EL COMIENZO. En la corrida completa del 2026-11-26 una
    prueba (PT-D14-04) traía sus pasos así: "[VARIANTE: sólo celda estudiante]
    [SI APARECE] Responder la pregunta de reflexión..." y "... siguiendo su ruta
    desde el menú de temas. ([VARIOS TURNOS])". La marca estaba, pero detrás de
    una etiqueta o al final, y esta función sólo la buscaba al principio: el
    paso opcional se trató como obligatorio y la prueba se cortó. Una marca que
    el redactor escribió vale donde la haya puesto.
    """
    text = str(step or "").strip()
    optional = long = False
    changed = True
    while changed:
        changed = False
        label = _LABEL.match(text)
        if label:
            text = text[label.end():]
            changed = True
            continue
        for mark in MARKS:
            if text.upper().startswith(mark):
                text = text[len(mark):].lstrip(" :.-")
                optional = optional or mark == OPTIONAL_MARK
                long = long or mark == LONG_MARK
                changed = True
        tail = _TRAILING_MARK.search(text)
        if tail and tail.start() > 0:
            mark = tail.group(1).upper()
            text = text[:tail.start()].rstrip()
            optional = optional or mark == OPTIONAL_MARK
            long = long or mark == LONG_MARK
            changed = True
    return text, optional, long


def with_mark(step: str, mark: str) -> str:
    text, optional, long = split_marks(step)
    optional = optional or mark == OPTIONAL_MARK
    long = long or mark == LONG_MARK
    head = (OPTIONAL_MARK + " " if optional else "") + (LONG_MARK + " " if long else "")
    return head + text


_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|«[^»]*»|“[^”]*”")

#: Un paso que sólo mira: empieza por guardar o capturar algo de la pantalla...
_OBSERVES = re.compile(
    r"^(guardar|capturar|registrar|tomar|anotar|copiar|sacar)\b.{0,40}?"
    r"\b(captura|texto|pantalla|recuadro|bloque)\b",
    re.IGNORECASE,
)
#: ...o empieza por un verbo que en una prueba SIEMPRE es mirar, nombre lo que
#: nombre: "Guardar la solución paso a paso", "Guardar la tabla que indica...".
#: Ver `is_observation` para el caso que lo motivó.
_SAVES_VIEW = re.compile(r"^(guardar|capturar|anotar|copiar)\b", re.IGNORECASE)
_NOTES_VIEW = re.compile(
    r"^registrar\s+(lo que|la aparici[oó]n|si\b|qu[eé]\b|c[oó]mo\b|cu[aá]l\b)", re.IGNORECASE
)
#: La excepción: lo que se guarda es algo que la PLATAFORMA guarda. Eso sí es
#: tocar un botón, y además es una escritura.
_PLATFORM_SAVE = re.compile(
    r"^guardar\s+((los|las|el|la|mis|sus|unos|unas)\s+)?"
    r"(cambios|configuraci[oó]n|ajustes|preferencias|formulario|borrador)\b",
    re.IGNORECASE,
)
#: ...y no encadena después una acción sobre la página. "Después de responder la
#: tercera pregunta" NO es una acción: dice cuándo se mira. Por eso sólo cuenta
#: un verbo de acción que venga coordinado ("y luego tocar", "; avanzar").
_THEN_ACTS = re.compile(
    r"(\by\s+(luego\s+|despu[eé]s\s+)?|;\s*|\bluego\s+)"
    r"(tocar|responder|escribir|enviar|avanzar|elegir|seleccionar|abrir|ir a|volver|"
    r"confirmar|pulsar|presionar|hacer clic|ingresar|entrar)\b",
    re.IGNORECASE,
)


def is_observation(step: str) -> bool:
    """¿Este paso sólo mira lo que ya está en pantalla?

    POR QUÉ. En la verificación en vivo del 2026-11-25 se cortaron 7 pruebas y 6
    fueron por un paso así: "Guardar captura y texto de la tercera pregunta que
    aparezca". La tercera pregunta ESTABA en pantalla (las capturas anteriores
    muestran tres preguntas distintas, una tras otra). Quien ejecuta no tiene
    memoria de los pasos anteriores, miró el indicador de avance (que mide
    dominio, no cuenta preguntas), concluyó que no era la tercera y gastó los 20
    turnos respondiendo preguntas para "llegar". Un paso que no pide hacer nada
    no puede costar 20 turnos ni cortar una prueba por una cuenta que no es suya:
    quien ejecuta lo trata aparte (ver agent_driver).

    "GUARDAR" ES MIRAR, NOMBRE LO QUE NOMBRE. La primera versión pedía que el
    paso nombrara "captura", "texto", "pantalla", "recuadro" o "bloque". En la
    corrida completa del 2026-11-26 ocho pruebas se cortaron en pasos como
    "Guardar la solución paso a paso que aparece" o "Guardar la tabla que indica
    si queda lo que se corrigió": no nombraban ninguna de esas palabras, fueron
    al modelo, y el modelo buscó en la plataforma un botón de guardar o exportar.
    En siete de las ocho lo que había que mirar estaba en pantalla (lo dice el
    propio motivo del corte: "la solución paso a paso aparece... pero la página
    no tiene ningún botón para guardarla"). En una prueba nadie le pide a la
    plataforma que guarde: guardar es dejar captura y texto, y eso lo hace el
    runner. La única salvedad es un paso que guarda algo DE la plataforma
    ("Guardar los cambios"), que es una acción y una escritura.

    Conservador en lo demás: ante la duda es un paso común, con sus turnos.
    """
    text, _, _ = split_marks(step)
    bare = _QUOTED.sub("''", text)
    if _THEN_ACTS.search(bare):
        return False
    if _OBSERVES.search(bare) or _NOTES_VIEW.search(bare):
        return True
    return bool(_SAVES_VIEW.search(bare)) and not _PLATFORM_SAVE.search(bare)


#: Un paso que ESCRIBE un texto en un cuadro...
_WRITES = re.compile(r"\b(escribir|tipear|teclear|redactar)\b", re.IGNORECASE)
#: ...y uno que ENVÍA: empieza por enviar, o toca el control que envía.
_SENDS = re.compile(
    r"^(enviar|mandar)\b|"
    r"^(tocar|pulsar|presionar|apretar|hacer clic)\b.{0,60}?\b(enviar|mandar)\b",
    re.IGNORECASE,
)
#: Dentro de un paso que escribe, lo que además lo vuelve un envío.
_ALSO_SENDS = re.compile(r"\b(enviar|enviarlo|mandar|mandarlo)\b", re.IGNORECASE)


def _head(step: str) -> str:
    """La instrucción del paso, sin el texto que hay que escribir.

    Lo que va después de los primeros dos puntos es el mensaje ("escribir
    exactamente: voy a enviar la tarea") o un ejemplo: no dice qué hace el
    paso, y un mensaje puede nombrar cualquier verbo.
    """
    text, _, _ = split_marks(step)
    return text.split(":", 1)[0]


def is_write(step: str) -> bool:
    """¿Este paso escribe un texto en un cuadro y nada más?

    POR QUÉ HACE FALTA SABERLO. En la corrida completa del 2026-11-27 (trabajo
    3e932ff1d50c4a30) seis de los 24 pasos de escribir traían, en lugar del
    mensaje, la descripción de la celda del muestreo ("escribir exactamente lo
    que distingue a esta celda: en una segunda sesión, referir a la lección
    anterior y ver si el tutor recupera..."). En las pruebas que se miraron,
    quien ejecuta hizo tres cosas distintas con eso: copió la descripción tal
    cual en el chat del tutor, la copió Y la envió, o hizo la secuencia entera
    que la descripción contaba (escribir, enviar e irse a mirar el panel del
    docente). En los dos últimos
    casos el paso siguiente, "Enviar ese mensaje", encontró el cuadro vacío u
    otra pantalla, y la prueba se cortó; en una de esas pruebas el mensaje era
    de crisis y se envió sin que quedara captura de lo enviado ni de la
    respuesta. Un paso que escribe se trata aparte: se le dice que sólo
    escribe, y después se comprueba que el texto quedó en el cuadro (ver
    browser_agents/agent_driver.py).

    "Escribir y enviar" en un mismo paso no entra: ese paso también envía.
    """
    if is_observation(step):
        return False
    head = _head(step)
    return bool(_WRITES.search(head)) and not _ALSO_SENDS.search(head)


def is_send(step: str) -> bool:
    """¿Este paso envía lo que un paso anterior dejó escrito?"""
    if is_observation(step):
        return False
    return bool(_SENDS.search(_head(step).strip()))


#: Una ACCIÓN que depende de algo que el tutor o la plataforma puede proponer o
#: no: "Confirmar ante el tutor la hipótesis de error que proponga". Ver
#: `lint_steps` para el caso.
_DEPENDS_ON_OFFER = re.compile(
    r"^(confirmar\s+ante\b|"
    r"(confirmar|aceptar|rechazar|responder|contestar|elegir|tocar|seguir|cerrar)\b"
    r".{0,140}?\b(que|lo que)\s+(\S+\s+){0,3}?"
    r"(proponga|plantee|ofrezca|sugiera|propone|plantea|ofrece|sugiere)\b)",
    re.IGNORECASE,
)

_CONDITIONAL = re.compile(
    r"(^|[.;:,(]\s*|\by\s+)(si|en caso de que|en caso de|de ser as[ií]|de lo contrario|"
    r"si no|cuando aparezca|cuando se muestre|siempre que)\b",
    re.IGNORECASE,
)
_LOOP = re.compile(
    # "Repetir el estímulo de la sonda 9.1.1" NO es una repetición: es una
    # referencia a otra prueba, y de ella depende la compuerta heredada. Por eso
    # "repetir" sólo cuenta cuando dice cuántas veces o hasta cuándo.
    r"\b(hasta que|hasta llegar|hasta ver|varias veces|de forma seguida|una y otra vez|"
    r"cada vez que|seguir respondiendo|las veces que|tantas veces|"
    r"repet\w* (el paso|los pasos|hasta|durante|\w+ veces))\b",
    re.IGNORECASE,
)
_IMPOSSIBLE = re.compile(
    r"\b(grabaci[oó]n de pantalla|grabar la pantalla|screen recording|cronometr\w*|"
    r"cron[oó]metro|medir el tiempo|medir la latencia|milisegundos|marca de tiempo|"
    r"conteo de cuadros|grabar (el )?audio|grabar (un )?video)\b",
    re.IGNORECASE,
)


#: Un paso que pide pasar a la pregunta siguiente...
_ADVANCES = re.compile(
    r"^(avanzar|pasar|continuar|seguir)\b.{0,60}?\b(siguiente|pr[oó]xim[ao])\b.{0,40}?"
    r"\b(pregunta|actividad|ejercicio|[ií]tem|problema)\b|"
    r"^(avanzar|pasar|continuar|seguir)\b.{0,40}?"
    r"\b(pregunta|actividad|ejercicio|[ií]tem|problema)\s+(siguiente|pr[oó]xim[ao])\b",
    re.IGNORECASE,
)
#: ...y uno que responde la que está en pantalla.
_ANSWERS = re.compile(
    r"\b(elegir|responder|seleccionar|marcar|contestar|resolver|acertar|errar|fallar)\b|"
    r"\bopci[oó]n(es)?\b|\brespuesta\s+(in)?correcta\b",
    re.IGNORECASE,
)


def _advances_without_answering(steps: list[str]) -> list[str]:
    """Pasos que piden avanzar sin que nadie haya respondido la pregunta en pantalla.

    POR QUÉ. Once pruebas de la corrida completa del 2026-11-26 se cortaron así:
    "Avanzar a la siguiente pregunta", "Guardar la nueva pregunta", "Avanzar a
    la siguiente pregunta". El primer avance andaba (venía de un error ya
    resuelto, con su botón para seguir); el segundo caía sobre una pregunta
    recién abierta, sin responder, y la plataforma no deja saltar una pregunta.
    El redactor quería ver tres preguntas seguidas y supuso que se podían
    pasar de largo.

    La cuenta es simple: entre un avance y el siguiente (o entre el comienzo y
    el primer avance) tiene que haber un paso que responda. Las capturas y los
    pasos `[SI APARECE]` no cuentan como respuesta.
    """
    out: list[str] = []
    answered = False
    for n, raw in enumerate(steps, start=1):
        text, is_optional, _ = split_marks(raw)
        if is_observation(raw):
            continue
        bare = _QUOTED.sub("''", text)
        if _ADVANCES.search(bare):
            if not answered and not is_optional:
                out.append(
                    f"Paso {n}: pide avanzar a la pregunta siguiente sin que un paso "
                    "anterior haya respondido la que quedó en pantalla. Una "
                    "plataforma puede no dejar saltar una pregunta: agregá antes "
                    "un paso que la responda (y decí si bien o mal)."
                )
            answered = False
        elif not is_optional and _ANSWERS.search(bare):
            answered = True
    return out


def lint_steps(steps: list[str]) -> list[str]:
    """Lo que estos pasos tienen de inejecutable, dicho para el redactor.

    Devuelve una lista vacía si no hay nada que objetar. No juzga si la prueba
    mide el criterio: sólo lo que se sabe, sin mirar la plataforma, que va a
    cortar la ejecución.
    """
    out: list[str] = []
    optional = 0
    for n, raw in enumerate(steps or [], start=1):
        text, is_optional, _ = split_marks(raw)
        optional += 1 if is_optional else 0
        bare = _QUOTED.sub("''", text)
        if not is_optional and _CONDITIONAL.search(bare):
            out.append(
                f"Paso {n}: es condicional. Quien ejecuta hace cada paso siempre; "
                f"si depende de algo que puede no aparecer, escribilo sin el 'si' y "
                f"empezalo con {OPTIONAL_MARK}."
            )
        if not is_optional and _DEPENDS_ON_OFFER.search(bare):
            # En la corrida completa del 2026-11-26 seis pruebas (PT-D4-03 y
            # PT-D14-06) se cortaron en "Confirmar ante el tutor la hipótesis de
            # error que proponga": esa vez el tutor hizo una pregunta en lugar
            # de proponer una hipótesis, y no había nada que confirmar. El mismo
            # paso, en las pruebas donde el redactor sí le puso la marca, se
            # salteó y la prueba llegó al final.
            out.append(
                f"Paso {n}: depende de algo que el tutor o la plataforma puede "
                f"proponer o no. Empezalo con {OPTIONAL_MARK}."
            )
        if _LOOP.search(bare):
            out.append(
                f"Paso {n}: repite una acción. Escribí un paso por cada vez, "
                "con un número fijo de veces."
            )
        if _IMPOSSIBLE.search(bare):
            out.append(
                f"Paso {n}: pide grabar o cronometrar. Quien ejecuta sólo opera la "
                "página y captura pantalla y texto: sacá ese paso."
            )
    out += _advances_without_answering(steps or [])
    total = len(steps or [])
    if total and optional / total > MAX_OPTIONAL_SHARE:
        out.append(
            f"{optional} de {total} pasos llevan {OPTIONAL_MARK}: la prueba puede "
            "terminar sin haber hecho nada. Sólo lo que de verdad puede no "
            "aparecer lleva la marca."
        )
    return out


def step_issues(step: str) -> list[str]:
    """Lo que UN paso tiene de inejecutable, sin mirar a sus vecinos.

    Es `lint_steps` para un paso suelto: no cuenta la proporción de pasos
    opcionales, que es una propiedad de la prueba entera. Lo usa quien revisa
    una corrección de pasos (ver evalkit/repair.py), que sólo tiene que mirar
    los pasos que cambiaron.
    """
    return [
        o for o in lint_steps([step])
        if "pasos llevan" not in o and "sin que un paso anterior" not in o
    ]


def optional_share(steps: list[str]) -> float:
    """Qué fracción de los pasos lleva `[SI APARECE]`."""
    steps = [s for s in (steps or []) if str(s or "").strip()]
    return sum(1 for s in steps if split_marks(s)[1]) / len(steps) if steps else 0.0


def repair_steps(steps: list[str]) -> tuple[list[str], list[str]]:
    """Último recurso cuando el redactor no corrigió: lo que el código puede arreglar.

    Un condicional, o una acción que depende de lo que el tutor proponga, pasa
    a `[SI APARECE]` (la semántica es la misma y quien ejecuta sabe cerrarlo). Un paso imposible se saca. Una repetición no se
    puede arreglar sin saber cuántas veces: queda, marcada `[VARIOS TURNOS]`,
    y se informa. Devuelve (pasos, lo que quedó sin arreglar).
    """
    fixed: list[str] = []
    for raw in steps or []:
        text, is_optional, _ = split_marks(raw)
        bare = _QUOTED.sub("''", text)
        if _IMPOSSIBLE.search(bare):
            continue
        step = raw
        if not is_optional and (_CONDITIONAL.search(bare) or _DEPENDS_ON_OFFER.search(bare)):
            step = with_mark(step, OPTIONAL_MARK)
        if _LOOP.search(bare):
            step = with_mark(step, LONG_MARK)
        fixed.append(step)
    left = [o for o in lint_steps(fixed) if "repite" in o or "pasos llevan" in o]
    return fixed, left
