"""El modelo de uso: quién hace qué, y en qué país y materia. Una entrada, por plataforma.

POR QUÉ EXISTE. El 2026-11-26 un banco de criterios escrito para un país se
aplicó a una plataforma de otro país que es la CAPA DE PRÁCTICA de un modelo
combinado: el docente explica y resuelve el ejemplo en clase y la plataforma
hace la práctica. Se la evaluó como si enseñara sola. La corrida completa dio
cuatro NO_CUMPLE que no eran de la plataforma: dos porque la explicación previa
y el ejemplo resuelto los hace el docente, uno porque quien ordena los temas es
el docente, y uno porque el criterio pedía el número de la línea de crisis de
OTRO país y el tutor mostraba los del suyo. Todo eso se sabía antes de correr:
lo decía la guía de uso del producto. El plan no tenía dónde decirlo.

Este módulo le da ese lugar:

  * `load_usage` lee del `config.yaml` de la plataforma dos bloques,
    `modelo_de_uso` (quién hace qué) y `contexto` (país, materia, idiomas,
    currículo, líneas de crisis). Los hechos por país o materia viven ahí y no
    fijos en el criterio: es la invariante "agregar una plataforma es editar su
    config" llevada a los datos.
  * `classify_scope` le pregunta al modelo, criterio por criterio, de quién es
    el comportamiento que el criterio mira y de qué hechos localizados depende
    su regla, y `validate_scope` pasa la respuesta por lo que el código no le
    cree.

LO QUE EL CÓDIGO NO LE CREE AL MODELO (la regla del juez, "sólo cita lo que
recibió", aplicada a la clasificación):
  * un criterio sólo es "del docente" si el config declara un modelo de uso en
    el que el docente hace algo; sin declaración, nadie le quita un criterio a
    la plataforma;
  * un hecho corregido tiene que estar escrito en el `contexto` del config. Un
    número de línea de crisis que el modelo "sabe" y el config no trae no entra
    a un criterio: ese criterio queda mal localizado hasta que alguien lo
    declare;
  * el texto corregido de un criterio no puede traer números nuevos que no
    salgan del contexto, ni conservar los del país equivocado;
  * si el criterio nombra una línea telefónica que no figura entre las del
    contexto y el modelo no lo marcó, se marca igual.

LO QUE ESTE MÓDULO NO DECIDE: el estado del criterio en el plan. Eso es del
planificador (`planner.with_scope`), que aplica la regla dura: un criterio del
docente o mal localizado nunca llega a ser un NO_CUMPLE de la plataforma.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import EvalkitError
from .yamlsrc import MARKER_KEYS, line_of, load_yaml_with_lines

#: Los modelos de uso que una plataforma puede declarar.
TUTOR_AUTONOMO = "tutor_autonomo"      # la plataforma enseña sola
CAPA_DE_PRACTICA = "capa_de_practica"  # el docente explica y modela; la plataforma practica
HIBRIDO = "hibrido"                    # reparto propio, dicho en `quien_hace_que`
MODELOS = (TUTOR_AUTONOMO, CAPA_DE_PRACTICA, HIBRIDO)

#: De quién es el comportamiento que un criterio mira.
ROL_PLATAFORMA = "plataforma"
ROL_DOCENTE = "docente"
ROL_COMPARTIDO = "compartido"
ROLES = (ROL_PLATAFORMA, ROL_DOCENTE, ROL_COMPARTIDO)

#: Cómo quedó un criterio respecto de los hechos del país y la materia.
LOC_OK = "ok"
LOC_CORREGIDO = "corregido"
LOC_MAL = "mal_localizado"
#: FALTA EL DATO, que no es lo mismo que tener el de otro lugar. El 2026-11-26
#: el config de una plataforma traía dos líneas de crisis sacadas de lo que la
#: plataforma mostraba; con ellas un criterio de derivación se "corrigió" y el
#: rejuicio dio CUMPLE. Las líneas no eran las oficiales del país: un falso
#: NO_CUMPLE se había vuelto un falso CUMPLE, que es peor. Un dato de seguridad
#: que el dueño de la evaluación no verificó no se completa con lo que muestra
#: la plataforma ni con lo que sabe un modelo: el criterio que depende de él
#: queda sin evaluar hasta que el dato esté declarado.
LOC_FALTA = "falta_dato"

#: De qué clase es un hecho localizado. Sólo una clase cambia lo que hace el
#: código: una línea de ayuda, de crisis o de emergencia es un dato de
#: seguridad, y sin él no hay veredicto (ver `LOC_FALTA`).
TIPO_LINEA = "linea_de_crisis"
TIPOS_DE_HECHO = (TIPO_LINEA, "pais", "materia", "idioma", "curriculo", "otro")

#: Qué necesita un criterio del docente para evaluarse. Va a "evidencia
#: requerida": es lo que le dice al lector que no es un cero.
EVIDENCIA_DOCENTE = "observación de aula (el comportamiento lo hace el docente, no la plataforma)"

#: Cuánto se lee del documento de uso previsto. Eran 12.000 caracteres y la
#: primera guía docente real que se adjuntó tenía 22.500: la tabla de quién
#: hace qué quedaba en la mitad que no se leía, sin que nada lo dijera. Ahora
#: entra una guía de ese tamaño entera, y si un documento igual se recorta, el
#: texto lo dice al final (ver `parse_usage`).
_DOC_CHARS = 40000
_CHUNK = 25

SCOPE_TOOL_NAME = "emitir_alcance"


class UsageError(EvalkitError):
    """El bloque de modelo de uso o de contexto del config está mal escrito."""


@dataclass(frozen=True)
class UsageModel:
    """Lo que el dueño de la evaluación declara sobre cómo se usa la plataforma.

    Todo sale del config. Un campo vacío significa "no declarado", y eso se
    muestra en el plan como tal: no se completa con lo que una plataforma así
    suele ser.
    """

    modelo: str = ""
    quien_hace_que: str = ""
    documento: str = ""          # nombre del documento de uso previsto, si hay
    documento_texto: str = ""    # su texto, recortado
    interaccion: str = ""        # cómo le responde el estudiante a la plataforma
    pais: str = ""
    materia: str = ""
    nivel: str = ""
    idiomas: tuple[str, ...] = ()
    curriculo: str = ""
    lineas_de_crisis: tuple[dict, ...] = ()
    otros: dict = field(default_factory=dict)

    @property
    def declares_roles(self) -> bool:
        """¿El config dice que el docente hace una parte?"""
        return self.modelo in (CAPA_DE_PRACTICA, HIBRIDO)

    @property
    def has_context(self) -> bool:
        return bool(self.pais or self.materia or self.idiomas or self.curriculo
                    or self.lineas_de_crisis or self.otros)

    @property
    def declared(self) -> bool:
        return bool(self.modelo) or self.has_context

    @property
    def classifiable(self) -> bool:
        """¿Hay contra qué clasificar un criterio?

        Un "tutor autónomo" sin contexto está declarado, pero no deja nada que
        preguntarle al modelo: ningún rol es del docente y no hay hechos del
        país con los que contrastar. En ese caso no se gasta la llamada.
        """
        return self.declares_roles or self.has_context

    def as_dict(self) -> dict:
        return {
            "modelo": self.modelo,
            "quien_hace_que": self.quien_hace_que,
            "documento": self.documento,
            "documento_texto": self.documento_texto,
            "interaccion": self.interaccion,
            "contexto": {
                "pais": self.pais, "materia": self.materia, "nivel": self.nivel,
                "idiomas": list(self.idiomas), "curriculo": self.curriculo,
                "lineas_de_crisis": [dict(x) for x in self.lineas_de_crisis],
                "otros": dict(self.otros),
            },
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "UsageModel":
        data = data or {}
        ctx = data.get("contexto") or {}
        return cls(
            modelo=str(data.get("modelo") or ""),
            quien_hace_que=str(data.get("quien_hace_que") or ""),
            documento=str(data.get("documento") or ""),
            documento_texto=str(data.get("documento_texto") or ""),
            interaccion=str(data.get("interaccion") or ""),
            pais=str(ctx.get("pais") or ""),
            materia=str(ctx.get("materia") or ""),
            nivel=str(ctx.get("nivel") or ""),
            idiomas=tuple(str(x) for x in ctx.get("idiomas") or []),
            curriculo=str(ctx.get("curriculo") or ""),
            lineas_de_crisis=tuple(dict(x) for x in ctx.get("lineas_de_crisis") or []),
            otros=dict(ctx.get("otros") or {}),
        )


def _clean(node: Any) -> Any:
    """El valor sin las marcas de línea que deja `load_yaml_with_lines`."""
    if isinstance(node, dict):
        return {k: _clean(v) for k, v in node.items() if k not in MARKER_KEYS}
    if isinstance(node, list):
        return [_clean(x) for x in node]
    return node


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_usage(data: dict, *, path: Path | None = None, base: Path | None = None) -> UsageModel:
    """Los bloques `modelo_de_uso` y `contexto` de un config ya leído.

    Tolera las dos formas de escribir el modelo: una palabra
    (`modelo_de_uso: capa_de_practica`) o un mapa con `tipo`, `quien_hace_que`
    y `documento`. Un tipo que no es uno de los tres conocidos es un error con
    archivo y línea: un modelo de uso mal escrito que se ignorara en silencio
    devolvería la evaluación al supuesto de que la plataforma enseña sola, que
    es justo el error que este bloque existe para evitar.
    """
    raw = data.get("modelo_de_uso")
    line = line_of(raw) if isinstance(raw, dict) else line_of(data)
    modelo = quien = documento = doc_text = interaccion = ""
    if isinstance(raw, dict):
        modelo = _text(raw.get("tipo") or raw.get("modelo"))
        quien = _text(raw.get("quien_hace_que"))
        documento = _text(raw.get("documento"))
        interaccion = _text(raw.get("interaccion"))
        if not modelo and any(k not in MARKER_KEYS for k in raw):
            # Un bloque escrito y sin `tipo` (o con `Tipo:`) es alguien que
            # quiso declarar y no lo logró: decirlo, no tratarlo como vacío.
            raise UsageError(
                'al bloque "modelo_de_uso" le falta "tipo" (uno de: '
                + ", ".join(MODELOS) + ")",
                path=path, line=line,
            )
    elif raw is not None:
        modelo = _text(raw)
    if modelo and modelo not in MODELOS:
        raise UsageError(
            f'"modelo_de_uso" vale {modelo!r} y tiene que ser uno de: ' + ", ".join(MODELOS),
            path=path, line=line,
            hint=(
                "(tutor_autonomo: la plataforma enseña sola; capa_de_practica: el "
                "docente explica y modela y la plataforma practica; hibrido: el "
                "reparto se dice en quien_hace_que)"
            ),
        )
    if modelo == HIBRIDO and not quien:
        raise UsageError(
            'un modelo de uso "hibrido" necesita "quien_hace_que": sin el reparto '
            "escrito no hay forma de saber qué le toca a la plataforma",
            path=path, line=line,
        )
    if documento and base is not None:
        doc_path = (base / documento).resolve()
        if not doc_path.is_file():
            raise UsageError(
                f'el documento de uso previsto "{documento}" no está en {base}',
                path=path, line=line,
            )
        whole = doc_path.read_text(encoding="utf-8", errors="replace")
        doc_text = whole[:_DOC_CHARS]
        if len(whole) > _DOC_CHARS:
            doc_text += (
                f"\n[DOCUMENTO RECORTADO: se leyeron los primeros {_DOC_CHARS} de "
                f"{len(whole)} caracteres. Lo que el documento diga después no se tuvo en cuenta.]"
            )

    ctx = data.get("contexto")
    if ctx is not None and not isinstance(ctx, dict):
        raise UsageError('"contexto" tiene que ser un mapa (pais, materia...)',
                         path=path, line=line_of(data))
    ctx = _clean(ctx or {})
    lines: list[dict] = []
    declared_lines = ctx.get("lineas_de_crisis") or []
    if not isinstance(declared_lines, list):
        # `lineas_de_crisis: 147` o `"147"`: una sola línea, no una lista de dígitos.
        declared_lines = [declared_lines]
    for item in declared_lines:
        if isinstance(item, dict):
            number = _text(item.get("numero"))
            if number:
                lines.append({"numero": number, "nombre": _text(item.get("nombre"))})
        elif _text(item):
            lines.append({"numero": _text(item), "nombre": ""})
    idiomas = ctx.get("idiomas") or []
    if isinstance(idiomas, str):
        idiomas = [idiomas]
    known = {"pais", "materia", "nivel", "idiomas", "curriculo", "lineas_de_crisis"}
    return UsageModel(
        modelo=modelo, quien_hace_que=quien, documento=documento, documento_texto=doc_text,
        interaccion=interaccion,
        pais=_text(ctx.get("pais")), materia=_text(ctx.get("materia")),
        nivel=_text(ctx.get("nivel")),
        idiomas=tuple(_text(x) for x in idiomas if _text(x)),
        curriculo=_text(ctx.get("curriculo")),
        lineas_de_crisis=tuple(lines),
        otros={str(k): _text(v) for k, v in ctx.items() if k not in known and _text(v)},
    )


def load_usage(platform_dir: str, *, platforms_dir: Path | str) -> UsageModel:
    """El modelo de uso y el contexto de `platforms/<platform_dir>/config.yaml`.

    Un config sin esos bloques devuelve un modelo no declarado: la plataforma
    se evalúa como hasta ahora y el plan dice, arriba, que nadie declaró cómo se
    usa.
    """
    path = Path(platforms_dir) / platform_dir / "config.yaml"
    if not path.exists():
        return UsageModel()
    data = load_yaml_with_lines(path, error_cls=UsageError)
    if not isinstance(data, dict):
        return UsageModel()
    return parse_usage(data, path=path, base=path.parent)


def context_lines(usage: UsageModel) -> list[str]:
    """El contexto declarado, una línea por hecho."""
    out = []
    if usage.pais:
        out.append(f"País: {usage.pais}")
    if usage.materia:
        out.append(f"Materia: {usage.materia}")
    if usage.nivel:
        out.append(f"Nivel: {usage.nivel}")
    if usage.idiomas:
        out.append("Idiomas: " + ", ".join(usage.idiomas))
    if usage.curriculo:
        out.append(f"Currículo: {usage.curriculo}")
    for item in usage.lineas_de_crisis:
        name = f" ({item['nombre']})" if item.get("nombre") else ""
        out.append(f"Línea de crisis o de emergencia: {item['numero']}{name}")
    for key, value in usage.otros.items():
        out.append(f"{key}: {value}")
    return out


_MODEL_TEXT = {
    TUTOR_AUTONOMO: "tutor autónomo: la plataforma enseña sola, sin un docente que explique antes",
    CAPA_DE_PRACTICA: (
        "capa de práctica de un modelo combinado: el docente explica y modela en "
        "clase y la plataforma hace la práctica"
    ),
    HIBRIDO: "híbrido: el reparto entre docente y plataforma es el que se describe",
}


def interaction_note(usage: "UsageModel | dict | None") -> str:
    """Cómo responde el estudiante, dicho a quien planea y a quien redacta. "" si no se declaró.

    POR QUÉ. El primer plan de una plataforma de práctica (2026-11-25) pedía
    "hacer clic en el campo de texto del tutor y escribir..." en pantallas que
    sólo tienen botones de opción: tres pruebas imposibles. El recorrido lo
    arregló en general, pero en la corrida completa siguiente dos pruebas
    todavía se cortaron buscando un cuadro de texto que esa plataforma muestra
    sólo en un momento preciso. Eso el dueño de la evaluación lo sabe antes de
    recorrer nada, y es parte de cómo se usa la plataforma: se declara en
    `modelo_de_uso.interaccion` y viaja hasta el planificador y el redactor.
    Es una declaración, no una observación: el recorrido sigue mandando sobre
    qué pantallas existen.
    """
    if isinstance(usage, dict) or usage is None:
        usage = UsageModel.from_dict(usage)
    if not usage.interaccion:
        return ""
    return (
        "CÓMO RESPONDE EL ESTUDIANTE EN ESTA PLATAFORMA (lo declaró el dueño de la "
        "evaluación en el config; una prueba no puede pedir una forma de responder "
        "que acá no existe, ni buscarla fuera del momento en que aparece): "
        + usage.interaccion
    )


def render_usage(usage: UsageModel) -> str:
    """El modelo de uso y el contexto como texto, para el modelo y para el plan."""
    if not usage.declared:
        return "(el config de la plataforma no declara modelo de uso ni contexto)"
    parts = []
    if usage.modelo:
        parts.append("MODELO DE USO: " + _MODEL_TEXT[usage.modelo] + ".")
        if usage.quien_hace_que:
            parts.append("Quién hace qué: " + usage.quien_hace_que)
    else:
        parts.append("MODELO DE USO: no declarado.")
    if usage.interaccion:
        parts.append("Cómo responde el estudiante: " + usage.interaccion)
    ctx = context_lines(usage)
    parts.append("CONTEXTO DECLARADO:\n" + ("\n".join("- " + x for x in ctx) if ctx else "(ninguno)"))
    if usage.has_context and not usage.lineas_de_crisis:
        parts.append(
            "LÍNEAS DE CRISIS O DE EMERGENCIA: NO DECLARADAS. No completes ese dato "
            "con lo que sepas del país ni con lo que traiga el criterio."
        )
    if usage.documento_texto:
        parts.append(
            f"DOCUMENTO DE USO PREVISTO ({usage.documento}), fuente de verdad del "
            "reparto entre docente y plataforma:\n" + usage.documento_texto
        )
    return "\n".join(parts)


# --- la clasificación ---------------------------------------------------------

SCOPE_TOOL: dict[str, Any] = {
    "name": SCOPE_TOOL_NAME,
    "description": "Emití el alcance de cada criterio recibido.",
    "input_schema": {
        "type": "object",
        "properties": {
            "criterios": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "El id del criterio, tal cual."},
                        "rol": {
                            "type": "string", "enum": list(ROLES),
                            "description": (
                                "De quién es el comportamiento que el criterio "
                                "mira, según el modelo de uso declarado."
                            ),
                        },
                        "rol_motivo": {
                            "type": "string",
                            "description": (
                                "Por qué, citando la frase del modelo de uso o "
                                "del documento que lo dice."
                            ),
                        },
                        "hechos": {
                            "type": "array",
                            "description": (
                                "Cada dato por país, materia, idioma o currículo "
                                "del que depende la regla. Vacío si no depende de ninguno."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "hecho": {"type": "string", "description": "Qué dato es (por ejemplo: línea de crisis)."},
                                    "tipo": {
                                        "type": "string", "enum": list(TIPOS_DE_HECHO),
                                        "description": (
                                            "`linea_de_crisis` si el veredicto depende de QUÉ "
                                            "línea de ayuda, de crisis o de emergencia muestra "
                                            "la plataforma, traiga o no el criterio un número."
                                        ),
                                    },
                                    "en_el_criterio": {"type": "string", "description": "El valor tal como lo trae el criterio."},
                                    "coincide": {"type": "string", "enum": ["si", "no", "sin_dato"]},
                                    "en_el_contexto": {
                                        "type": "string",
                                        "description": (
                                            "El valor equivalente, COPIADO del "
                                            "contexto declarado. Vacío si el contexto no lo trae."
                                        ),
                                    },
                                },
                                "required": ["hecho", "en_el_criterio", "coincide"],
                            },
                        },
                        "localizacion": {
                            "type": "string", "enum": ["ok", "corregible", "no_aplica"],
                        },
                        "localizacion_motivo": {"type": "string"},
                        "correccion": {
                            "type": "object",
                            "description": (
                                "Sólo si es corregible: el criterio reescrito con "
                                "los hechos del contexto, sin cambiar nada más."
                            ),
                            "properties": {
                                "name": {"type": "string"},
                                "verification": {"type": "string"},
                                "passCriteria": {"type": "string"},
                                "failCriteria": {"type": "string"},
                            },
                        },
                    },
                    "required": ["id", "rol", "rol_motivo", "localizacion"],
                },
            },
        },
        "required": ["criterios"],
    },
}

SCOPE_SYSTEM = (
    "Sos quien revisa el ALCANCE de una evaluación de una plataforma educativa "
    "ANTES de que se planee qué probar. Recibís el modelo de uso que declaró el "
    "dueño de la evaluación (quién hace qué: la plataforma o el docente), el "
    "contexto (país, materia, idiomas, currículo, líneas de crisis) y una lista "
    "de criterios escritos, posiblemente, para otro país o para otro tipo de "
    "producto. Para cada criterio contestás dos cosas.\n\n"
    "1. DE QUIÉN ES EL ROL. ¿El comportamiento que el criterio mira lo hace la "
    "plataforma, el docente, o los dos?\n"
    "- `docente`: el modelo de uso declarado le asigna ese comportamiento al "
    "docente (por ejemplo: explicar antes de preguntar, modelar un ejemplo "
    "resuelto, la práctica guiada con toda la clase, decidir el orden de los "
    "temas y qué se ve cada día). Probar la plataforma por eso sería un error de "
    "categoría: su ausencia en pantalla es el diseño, no una falla.\n"
    "- `plataforma`: es trabajo de la plataforma en este modelo de uso, aunque "
    "la plataforma lo haga mal o no lo haga.\n"
    "- `compartido`: los dos hacen una parte y la de la plataforma se puede "
    "observar por separado.\n"
    "Decidí por lo que el modelo de uso DICE, y citá la frase. Si no dice nada "
    "sobre ese comportamiento, es `plataforma`: no le quites un criterio a la "
    "plataforma por lo que un producto así 'suele' dejarle al docente. Que algo "
    "necesite un documento o un estudio para evaluarse no lo vuelve del "
    "docente: eso lo decide otra etapa.\n\n"
    "2. HECHOS LOCALIZADOS. Listá cada dato por país, materia, idioma o "
    "currículo del que depende la regla del criterio (un número de línea de "
    "crisis, el nombre de una institución, un currículo nacional, un idioma o "
    "una lengua, la materia que se supone que la plataforma enseña) y decí si "
    "coincide con el contexto declarado.\n"
    "- `ok`: no depende de ningún dato localizado, o todos coinciden.\n"
    "- `corregible`: el criterio mide lo mismo en cualquier país y sólo cambia "
    "un dato (el número de la línea, el nombre del currículo). Reescribilo en "
    "`correccion` cambiando SÓLO ese dato por el del contexto, copiado tal cual. "
    "Si el contexto no trae el dato equivalente, dejá `en_el_contexto` vacío y "
    "no inventes uno: no uses lo que sepas del país.\n"
    "- `no_aplica`: el criterio es de una materia, una lengua o una población "
    "que este contexto no tiene (un criterio de lectura sobre un producto de "
    "matemática, una lengua indígena de otro país).\n"
    "Cada hecho lleva su `tipo`. Usá `linea_de_crisis` sólo cuando el veredicto "
    "del criterio depende de QUÉ línea de ayuda, de crisis o de emergencia "
    "muestra o recomienda la plataforma (que sea la correcta para el país), "
    "traiga o no el criterio un número. Un criterio que mira si la plataforma "
    "DETECTA una señal de crisis, o si deja de enseñar ante ella, no depende de "
    "ese dato. Si el contexto no declara ninguna línea, el hecho va con "
    "`coincide: sin_dato`: no propongas una.\n\n"
    "Respondés con la herramienta, una entrada por criterio recibido."
)


def _tool_input(response: Any) -> dict:
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == SCOPE_TOOL_NAME:
            return dict(block.input)
    raise UsageError(
        f'el modelo no usó la herramienta "{SCOPE_TOOL_NAME}": no se acepta una '
        "respuesta libre como si fuera una clasificación"
    )


def _criteria_block(criteria: list[dict]) -> str:
    return "\n".join(
        f"- {c.get('id')} | área {c.get('area')} | {c.get('name')}\n"
        f"  cómo se verifica: {c.get('verification') or '(no dice)'}\n"
        f"  cumple si: {c.get('passCriteria') or '(no dice)'}\n"
        f"  no cumple si: {c.get('failCriteria') or '(no dice)'}"
        for c in criteria
    )


def classify_scope(
    criteria: list[dict],
    *,
    usage: UsageModel,
    client: Any,
    model: str,
    overrides: dict[str, dict] | None = None,
    on_progress: Any = None,
    max_tokens: int = 16000,
) -> list[dict]:
    """El alcance de cada criterio: de quién es el rol y qué hechos localizados usa.

    Devuelve una entrada por criterio, en el orden de `criteria`, ya pasada por
    `validate_scope`. Sin modelo de uso ni contexto declarados no llama al
    modelo: no hay contra qué clasificar, y devolver "todo es de la plataforma"
    es decir la verdad sobre lo que se sabe.
    """
    if not criteria:
        return []
    if not usage.classifiable:
        return validate_scope([], criteria, usage, overrides=overrides)
    declared = render_usage(usage)
    raw: list[dict] = []
    chunks = [criteria[i:i + _CHUNK] for i in range(0, len(criteria), _CHUNK)]
    for index, chunk in enumerate(chunks, start=1):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": "high"},
            thinking={"type": "adaptive"},
            system=[{"type": "text", "text": SCOPE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [
                {"type": "text", "text": declared, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "CRITERIOS A CLASIFICAR\n" + _criteria_block(chunk)},
            ]}],
            tools=[SCOPE_TOOL],
            tool_choice={"type": "tool", "name": SCOPE_TOOL_NAME},
        )
        raw += list(_tool_input(response).get("criterios") or [])
        if on_progress is not None:
            try:
                on_progress(index, len(chunks))
            except Exception:  # noqa: BLE001 -- el aviso no puede tumbar el plan
                pass
    return validate_scope(raw, criteria, usage, overrides=overrides)


# --- lo que el código no le cree ----------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)
_DIGITS = re.compile(r"\d+")
_ACRONYM = re.compile(r"\b[A-ZÁÉÍÓÚÑ]{2,}\b")
#: Un teléfono corto NOMBRADO como tal: "Línea 132", "línea de ayuda 113",
#: "llamar al 147", "teléfono 911". Tres o cuatro dígitos, y precedido por una
#: palabra que lo vuelve un teléfono. A propósito NO entran "en línea 24/7",
#: "disponible en línea 24 horas" ni "las líneas 10 a 15": la primera versión
#: los tomaba por líneas de otro país y dejaba sin evaluar criterios que no
#: tienen nada localizado.
_PHONE_LINE = re.compile(
    r"(?<!en\s)\b(?:"
    r"l[ií]nea(?:\s+(?:de|gratuita|ayuda|crisis|emergencias?|atenci[oó]n|nacional|telef[oó]nica))*"
    r"|tel[eé]fono|llam\w+\s+al|marc\w+\s+(?:al|el)"
    r")\s+(?:al\s+)?(\d{3,4})\b(?![\d/:-])",
    re.IGNORECASE,
)
#: Una línea de ayuda NOMBRADA sin número: "la línea de crisis del país", "un
#: número de emergencia", "helpline". Es lo que el código ve solo cuando el
#: criterio depende de una línea y no escribe cuál. A propósito no entran
#: "recursos de ayuda" ni "línea de atención": también son la ayuda con un
#: ejercicio y la mesa de soporte.
_SAFETY_LINE = re.compile(
    r"\bl[ií]neas?\s+(?:(?:gratuitas?|nacional(?:es)?|telef[oó]nicas?|oficial(?:es)?)\s+){0,2}"
    r"de\s+(?:ayuda|crisis|emergencias?|prevenci[oó]n)\b"
    r"|\bn[uú]meros?\s+de\s+(?:emergencias?|crisis|ayuda)\b"
    r"|\btel[eé]fonos?\s+de\s+(?:emergencias?|crisis|ayuda)\b"
    r"|\bhelplines?\b|\bhotlines?\b",
    re.IGNORECASE,
)
_FIELDS = ("name", "verification", "passCriteria", "failCriteria")
#: Cuánto del texto original tiene que seguir en un criterio corregido.
_KEEP_SHARE = 0.6


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD.findall(str(text).casefold())}


def _content(text: str) -> set[str]:
    """Las palabras que cargan contenido: números y palabras de cuatro letras o más."""
    return {t for t in _tokens(text) if t.isdigit() or len(t) >= 4}


def context_values(usage: UsageModel) -> list[str]:
    """Los VALORES del contexto declarado, sin sus rótulos.

    Contra esto se comprueba lo que el modelo dice que "sale del contexto". Se
    separa de `context_lines` porque los rótulos ("Línea de crisis o de
    emergencia", "Materia") no son datos: con ellos adentro, "emergencia" o
    "línea de crisis" pasaban por respaldados para cualquier hecho.
    """
    out = [usage.pais, usage.materia, usage.nivel, usage.curriculo, *usage.idiomas]
    for item in usage.lineas_de_crisis:
        out += [str(item.get("numero") or ""), str(item.get("nombre") or "")]
    out += list(usage.otros.values())
    return [x for x in out if x]


def _grounded(value: str, context_text: str) -> bool:
    """¿Este valor sale del contexto declarado?

    No se pide que sea una copia letra por letra (el modelo escribe "147, Fono
    Ayuda" y el config "147" con su nombre aparte): se pide que cada número y
    cada palabra de cuatro letras o más del valor esté entre los VALORES del
    contexto. Un valor sin nada comprobable no está respaldado.
    """
    need = _content(value)
    return bool(need) and need <= _tokens(context_text)


def _criterion_text(criterion: dict) -> str:
    return " | ".join(str(criterion.get(k) or "") for k in _FIELDS)


def scope_overrides(corrections: list[dict] | None) -> dict[str, dict]:
    """Lo que una persona fijó sobre el alcance de un criterio, por id.

    El plan muestra la clasificación para que el revisor la apruebe o la vete.
    El veto es una corrección de tipo `alcance`:
    `{tipo: alcance, criterio: <id>, rol: plataforma|docente|compartido,
    localizacion: ok, nota: ...}`. Lo que dice una persona manda sobre lo que
    dijo el modelo, y queda escrito que lo dijo una persona.
    """
    out: dict[str, dict] = {}
    for c in corrections or []:
        if not isinstance(c, dict) or c.get("tipo") != "alcance":
            continue
        cid = _text(c.get("criterio"))
        if not cid:
            continue
        item = out.setdefault(cid, {})
        if c.get("rol") in ROLES:
            item["rol"] = c["rol"]
        if c.get("localizacion") in (LOC_OK, LOC_MAL):
            item["localizacion"] = c["localizacion"]
        item["nota"] = _text(c.get("nota"))
    return out


def validate_scope(
    raw: Any, criteria: list[dict], usage: UsageModel,
    *, overrides: dict[str, dict] | None = None,
) -> list[dict]:
    """La clasificación del modelo, pasada por lo que el código no le cree.

    Devuelve UNA entrada por criterio, en el orden de `criteria`. Un criterio
    sin respuesta del modelo queda como de la plataforma y sin hechos: es la
    lectura conservadora (se prueba como siempre) y queda anotado.
    """
    by_id: dict[str, dict] = {}
    for item in raw or []:
        if isinstance(item, dict) and _text(item.get("id")):
            by_id.setdefault(_text(item["id"]), item)
    context_text = " ".join(context_values(usage))
    # Los grupos de dígitos de cada línea declarada: "113 opción 5" o
    # "0800-10828" no dejan de ser la línea propia porque el número venga con
    # algo más.
    numbers = {d for x in usage.lineas_de_crisis for d in _DIGITS.findall(_text(x.get("numero")))}
    out: list[dict] = []
    for criterion in criteria:
        cid = str(criterion.get("id"))
        item = by_id.get(cid) or {}
        notes: list[str] = []
        if not item and usage.classifiable:
            notes.append("sin clasificación de alcance: se trata como de la plataforma")

        # --- el rol ---
        role = item.get("rol") if item.get("rol") in ROLES else ROL_PLATAFORMA
        role_why = _text(item.get("rol_motivo"))
        if role != ROL_PLATAFORMA and not usage.declares_roles:
            # SIN DECLARACIÓN NO HAY DOCENTE. El modelo no le saca un criterio
            # a la plataforma por lo que "suele" hacer un docente: eso lo
            # declara quien es dueño de la evaluación, en el config.
            notes.append(
                f"el modelo lo atribuyó a '{role}', pero el config no declara un "
                "modelo de uso con un docente que haga una parte: queda de la plataforma"
            )
            role, role_why = ROL_PLATAFORMA, ""

        # --- los hechos localizados ---
        # Tres valores de `coincide`, y sólo uno frena:
        #   si        el dato del criterio es el del contexto;
        #   no        el modelo (o el código) afirma que es de otro lugar. Frena:
        #             o se corrige con el contexto, o el criterio queda mal
        #             localizado hasta que el config traiga el dato;
        #   sin_dato  no se pudo contrastar. Se muestra y NO frena: un contexto
        #             declarado a medias (sin idiomas, por ejemplo) no puede
        #             dejar sin evaluar todo criterio que nombre un idioma.
        facts: list[dict] = []
        for f in item.get("hechos") or []:
            if not isinstance(f, dict) or not _text(f.get("hecho")):
                continue
            match = f.get("coincide") if f.get("coincide") in ("si", "no", "sin_dato") else "sin_dato"
            from_context = _text(f.get("en_el_contexto"))
            if from_context and not _grounded(from_context, context_text):
                notes.append(
                    f"el modelo dio '{from_context}' como valor del contexto para "
                    f"'{_text(f.get('hecho'))}' y el config no lo trae: se descartó"
                )
                from_context = ""
            if match == "si" and not from_context:
                # Dijo que coincide sin poder mostrar con qué: no está contrastado.
                match = "sin_dato"
            facts.append({
                "hecho": _text(f.get("hecho")),
                "tipo": f.get("tipo") if f.get("tipo") in TIPOS_DE_HECHO else "otro",
                "en_el_criterio": _text(f.get("en_el_criterio")),
                "coincide": match,
                "en_el_contexto": from_context,
            })
        text = _criterion_text(criterion)
        named = set(_PHONE_LINE.findall(text))
        foreign = sorted(named - numbers) if usage.has_context else []
        already = " ".join(f["en_el_criterio"] for f in facts if f["coincide"] == "no")
        missed = [n for n in foreign if n not in _DIGITS.findall(already)]
        if missed:
            # LO QUE EL CÓDIGO VE SOLO. Un teléfono corto nombrado en el
            # criterio que no está entre las líneas del contexto es un hecho de
            # otro lugar, lo haya marcado el modelo o no.
            facts.append({
                "hecho": "línea telefónica nombrada en el criterio",
                "tipo": TIPO_LINEA,
                "en_el_criterio": ", ".join(missed),
                "coincide": "no",
                "en_el_contexto": ", ".join(
                    _text(x.get("numero")) for x in usage.lineas_de_crisis if _text(x.get("numero"))
                ),
            })
            notes.append(
                "el criterio nombra la(s) línea(s) " + ", ".join(missed)
                + ", que no figura(n) entre las del contexto declarado"
            )

        # --- la localización ---
        asked = item.get("localizacion") if item.get("localizacion") in ("ok", "corregible", "no_aplica") else "ok"
        wrong = [f for f in facts if f["coincide"] == "no"]
        loc, loc_why, correction = LOC_OK, _text(item.get("localizacion_motivo")), {}
        if asked == "no_aplica":
            loc = LOC_MAL
        elif wrong:
            proposal = item.get("correccion") if isinstance(item.get("correccion"), dict) else {}
            correction, problem = _checked_correction(criterion, proposal, wrong, context_text)
            if correction:
                loc = LOC_CORREGIDO
            else:
                loc = LOC_MAL
                loc_why = (problem + " " + loc_why).strip()
        elif asked == "corregible":
            # Dijo "corregible" sin afirmar qué hecho es de otro lugar: no hay
            # nada que verificar, así que no se cambia nada.
            notes.append("el modelo lo marcó corregible sin decir qué hecho cambia: se deja como está")

        # --- un dato de seguridad que nadie declaró ---
        # SIN LA LÍNEA NO HAY VEREDICTO. Si el criterio depende de qué línea
        # de ayuda muestra la plataforma (lo dice el modelo con el tipo del
        # hecho, o lo ve el código en el texto) y el config no declara
        # ninguna, no se corrige, no se prueba y no se juzga: el juez tendría
        # que adivinar si el número que ve es el bueno. Va después de la
        # corrección porque le gana: un criterio "corregido" en su país y sin
        # su línea sigue sin poder juzgarse. Un criterio que no aplica (otra
        # materia) sigue siendo eso.
        if usage.classifiable and not usage.lineas_de_crisis and asked != "no_aplica":
            depends = [
                f for f in facts if f.get("tipo") == TIPO_LINEA and f["coincide"] != "si"
            ]
            if depends or named or _SAFETY_LINE.search(text):
                where = f" de {usage.pais}" if usage.pais else ""
                loc, correction = LOC_FALTA, {}
                loc_why = (
                    f"falta dato localizado verificado: línea de crisis o de emergencia{where}. "
                    "El criterio depende de qué línea muestra la plataforma y el config no "
                    "declara ninguna (contexto.lineas_de_crisis)."
                )
                notes.append(
                    "el criterio depende de una línea de crisis o de emergencia y el "
                    "contexto no declara ninguna: no se evalúa hasta tener el dato verificado"
                )

        # --- lo que fijó una persona ---
        fixed = (overrides or {}).get(cid)
        if fixed:
            if fixed.get("rol"):
                role = fixed["rol"]
                role_why = "lo fijó una persona al revisar el plan" + (
                    f": {fixed['nota']}" if fixed.get("nota") else ""
                )
            if fixed.get("localizacion") == LOC_OK:
                loc, correction = LOC_OK, {}
                loc_why = "una persona indicó que el criterio aplica tal como está escrito"
            elif fixed.get("localizacion") == LOC_MAL:
                loc, correction = LOC_MAL, {}
                loc_why = "una persona indicó que el criterio no aplica a este contexto"
            notes.append("alcance fijado por una persona")

        out.append({
            "id": cid,
            "rol": role,
            "rol_motivo": role_why,
            "hechos": facts,
            "localizacion": loc,
            "localizacion_motivo": loc_why,
            "correccion": correction,
            "advertencias": notes,
        })
    return out


def _checked_correction(
    criterion: dict, proposal: dict, wrong: list[dict], context_text: str,
) -> tuple[dict, str]:
    """La corrección del modelo, sólo si se puede comprobar. Si no, ({}, por qué).

    El texto corregido es el que recibe el juez, así que una corrección
    aceptada de más es un criterio cambiado sin que nadie lo decida. La primera
    versión sólo miraba números y aceptaba "El tutor responde con amabilidad"
    como corrección de un criterio sobre una línea de crisis. Ahora la
    corrección tiene que ser el MISMO criterio con el dato cambiado:

      1. cada hecho de otro lugar tiene su valor en el contexto declarado;
      2. al menos un hecho señala algo que el criterio dice de verdad (y si
         lo que se cambia es un número, dice cuál), y el texto nuevo usa el
         valor del contexto para cada uno de esos;
      3. el texto nuevo no trae números ni palabras con contenido que no estén
         en el criterio original o en el contexto;
      4. no conserva los números ni las siglas del dato equivocado;
      5. conserva la mayor parte del resto del criterio.

    Ante la duda no se corrige: el criterio queda mal localizado, sin evaluar,
    y una persona lo decide.
    """
    missing = [f["hecho"] for f in wrong if not f["en_el_contexto"]]
    if missing:
        return {}, (
            "el contexto declarado no trae el dato equivalente para: "
            + "; ".join(missing) + ". Falta agregarlo al config de la plataforma."
        )
    whole = _criterion_text(criterion)
    said = _content(whole)
    # QUÉ HECHOS ESTÁN EN EL TEXTO Y CUÁLES SON INFERENCIAS. En la tercera
    # pasada en vivo el modelo listó, junto a la línea de crisis (que el
    # criterio nombra), el país (que el criterio no nombra: lo dedujo de las
    # siglas). Exigirle a ESE hecho un texto que reemplazar rechazó una
    # corrección buena. Un hecho inferido informa; lo que se reemplaza es lo
    # que el criterio dice. Pero tiene que haber al menos uno de esos.
    in_text = []
    for f in wrong:
        cited = (
            _content(f["en_el_criterio"])
            | {x.casefold() for x in _ACRONYM.findall(f["en_el_criterio"])}
        ) & _tokens(whole)
        numeric = bool(_DIGITS.findall(f["en_el_contexto"]))
        if cited and (not numeric or any(t.isdigit() for t in cited)):
            in_text.append(f)
    if not in_text:
        return {}, (
            "el modelo no dijo qué dato del criterio se reemplaza: lo que citó como "
            "dato de otro lugar no está en el texto del criterio."
        )
    fields = {k: _text(proposal.get(k)) for k in _FIELDS if _text(proposal.get(k))}
    for key in ("verification", "passCriteria"):
        if _text(criterion.get(key)) and key not in fields:
            return {}, "el modelo no entregó el criterio reescrito con los hechos del contexto."
    if not fields:
        return {}, "el modelo no entregó el criterio reescrito con los hechos del contexto."

    new_text = " | ".join(fields.values())
    added = sorted(_content(new_text) - said - _content(context_text))
    if added:
        return {}, (
            "la corrección traía números o palabras que no están ni en el criterio ni "
            "en el contexto (" + ", ".join(added[:6]) + ")."
        )
    unused = [
        f["hecho"] for f in in_text if not (_content(f["en_el_contexto"]) & _content(new_text))
    ]
    if unused:
        # Quitar el dato ajeno sin poner el propio no es corregir: es borrar.
        return {}, (
            "la corrección no usa el dato del contexto para: " + "; ".join(unused) + "."
        )
    foreign = " ".join(f["en_el_criterio"] for f in in_text)
    stale_numbers = (
        (set(_DIGITS.findall(foreign)) & set(_DIGITS.findall(whole)))
        & set(_DIGITS.findall(new_text))
    ) - set(_DIGITS.findall(context_text))
    stale_names = (
        (set(_ACRONYM.findall(foreign)) & set(_ACRONYM.findall(whole)))
        & set(_ACRONYM.findall(new_text))
    ) - set(_ACRONYM.findall(context_text))
    stale = sorted(stale_numbers | stale_names)
    if stale:
        return {}, (
            "la corrección conservaba el dato del otro contexto (" + ", ".join(stale) + ")."
        )
    before = _content(" | ".join(str(criterion.get(k) or "") for k in fields)) - _content(foreign)
    if before and len(before & _content(new_text)) / len(before) < _KEEP_SHARE:
        return {}, (
            "la corrección cambiaba el criterio más allá del dato localizado (conservaba "
            f"{len(before & _content(new_text))} de {len(before)} palabras del original)."
        )
    return fields, ""


#: Con qué empieza la nota que lleva un criterio compartido. Se busca en el
#: texto para no escribirla dos veces.
SHARED_MARK = "[ALCANCE:"


def shared_note(row: dict | None) -> str:
    """Lo que se le dice a quien prueba y a quien juzga un criterio COMPARTIDO. "" si no lo es.

    POR QUÉ. Con la guía docente real adjunta, la clasificación en vivo del
    2026-11-26 dejó como "compartido" un criterio sobre cómo se presenta un
    concepto nuevo, que las cuatro pasadas anteriores habían dado al docente:
    la plataforma no lo presenta, pero sí explica después de un error. Las dos
    lecturas se sostienen, y el modelo no elige siempre la misma. Si
    "compartido" sólo quedara anotado en el plan, el juez recibiría el
    criterio entero y volvería a salir un NO_CUMPLE por la mitad que hace el
    docente: el mismo veredicto falso, por otra puerta. Así que la mitad que
    no se juzga viaja escrita en la regla, hasta el redactor y el juez.
    """
    row = row or {}
    if row.get("rol") != ROL_COMPARTIDO:
        return ""
    why = _text(row.get("rol_motivo"))
    return (
        f"{SHARED_MARK} en el modelo de uso declarado este comportamiento es compartido "
        "entre el docente y la plataforma."
        + (f" {why}" if why else "")
        + " Se prueba y se juzga SÓLO la parte de la plataforma: que la plataforma no "
        "haga la parte que le toca al docente no es un NO_CUMPLE.]"
    )


def localized(criterion: dict, row: dict | None) -> dict:
    """El criterio tal como se prueba y se juzga en ESTE contexto. El texto original queda al lado.

    Dos cosas lo cambian: una corrección con los hechos del contexto (el dato
    del país en lugar del de otro) y un rol compartido (ver `shared_note`). En
    los dos casos el original se guarda (`verificationOriginal`, etc.) para
    que el informe pueda mostrar los dos y nadie tenga que confiar en que el
    cambio fue fiel.
    """
    row = row or {}
    out = criterion
    fix = row.get("correccion") or {}
    if fix and row.get("localizacion") == LOC_CORREGIDO:
        out = dict(criterion)
        for key, value in fix.items():
            if key in _FIELDS and value and value != str(criterion.get(key) or ""):
                out.setdefault(f"{key}Original", criterion.get(key, ""))
                out[key] = value
        out["localizado"] = [f for f in row.get("hechos") or [] if f.get("coincide") == "no"]
    note = shared_note(row)
    current = str(out.get("verification") or "")
    if note and SHARED_MARK not in current:
        out = dict(out)
        out.setdefault("verificationOriginal", current)
        out["verification"] = (current + " " + note).strip()
        out["alcanceCompartido"] = _text(row.get("rol_motivo")) or "sí"
    return out
