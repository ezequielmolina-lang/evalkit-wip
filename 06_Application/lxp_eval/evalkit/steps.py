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

#: Más de esta fracción de pasos opcionales y la prueba puede terminar sin
#: haber hecho nada: deja de ser una prueba.
MAX_OPTIONAL_SHARE = 0.5


def split_marks(step: str) -> tuple[str, bool, bool]:
    """(texto sin marcas, es opcional, lleva varios turnos)."""
    text = str(step or "").strip()
    optional = long = False
    changed = True
    while changed:
        changed = False
        for mark in MARKS:
            if text.upper().startswith(mark):
                text = text[len(mark):].lstrip(" :.-")
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

    Conservador a propósito: ante la duda es un paso común, con sus turnos.
    """
    text, _, _ = split_marks(step)
    bare = _QUOTED.sub("''", text)
    return bool(_OBSERVES.search(bare)) and not _THEN_ACTS.search(bare)

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
    total = len(steps or [])
    if total and optional / total > MAX_OPTIONAL_SHARE:
        out.append(
            f"{optional} de {total} pasos llevan {OPTIONAL_MARK}: la prueba puede "
            "terminar sin haber hecho nada. Sólo lo que de verdad puede no "
            "aparecer lleva la marca."
        )
    return out


def repair_steps(steps: list[str]) -> tuple[list[str], list[str]]:
    """Último recurso cuando el redactor no corrigió: lo que el código puede arreglar.

    Un condicional pasa a `[SI APARECE]` (la semántica es la misma y quien
    ejecuta sabe cerrarlo). Un paso imposible se saca. Una repetición no se
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
        if not is_optional and _CONDITIONAL.search(bare):
            step = with_mark(step, OPTIONAL_MARK)
        if _LOOP.search(bare):
            step = with_mark(step, LONG_MARK)
        fixed.append(step)
    left = [o for o in lint_steps(fixed) if "repite" in o or "pasos llevan" in o]
    return fixed, left
