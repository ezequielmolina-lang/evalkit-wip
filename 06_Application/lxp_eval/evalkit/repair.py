"""Reparar los pasos de una prueba que se cortó, para volver a correrla.

POR QUÉ EXISTE. En la corrida completa del 2026-11-26 (trabajo
91b35f78a25749c9) se cortaron 32 de 86 pruebas y nueve criterios quedaron sin
veredicto por "ejecución fallida". Ninguna se cortó por algo que la plataforma
hiciera mal. Se cortaron porque el redactor, que escribe los pasos antes de que
nadie los ejecute, supuso un estado que la plataforma no tenía en ese momento:

* "Avanzar a la siguiente pregunta" con una pregunta sin responder en pantalla
  (la plataforma no deja saltar una pregunta): 11 pruebas.
* "Confirmar ante el tutor la hipótesis de error que proponga" cuando esa vez el
  tutor hizo una pregunta en lugar de proponer nada: 6 pruebas.
* "Usar el perfil de prueba de la pantalla de ingreso" cuando la preparación de
  la prueba ya había entrado con un estudiante nuevo: 2 pruebas.

Las reglas del ejecutor y la revisión de pasos (ver steps.py, agent_driver.py)
tapan las familias que ya se vieron. Esto es para la que todavía no se vio: una
plataforma nueva va a traer supuestos nuevos, y una prueba cortada es un
criterio que el informe tiene que dar por no evaluado sin que la plataforma
tenga nada que ver.

QUÉ HACE. Cuando un paso no se puede hacer, el motor ya sabe tres cosas que el
redactor no sabía: qué paso fue, qué dijo quien ejecuta que había en pantalla, y
el texto de esa pantalla. Con eso se le pide al modelo que corrija los pasos
(`propose_repair`), el código revisa que la corrección no cambie lo que la
prueba mide ni agregue un riesgo (`accept_repair`), y la prueba se corre de
nuevo, desde cero, con un estudiante nuevo. Una sola vez.

QUÉ NO HACE. No decide que el paso "en realidad se hizo", no saltea el paso y
sigue, y no puntúa con la evidencia del intento cortado: la regla de que una
prueba cortada no se puntúa (ver service/jobs.py) queda igual. Si el segundo
intento también se corta, la prueba queda sin evaluar con los dos motivos.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from .errors import EvalkitError
from .steps import (
    MAX_OPTIONAL_SHARE,
    OPTIONAL_MARK,
    is_observation,
    optional_share,
    repair_steps,
    split_marks,
    step_issues,
)

logger = logging.getLogger(__name__)

REPAIR_TOOL_NAME = "reparar_pasos"

REPAIR_TOOL: dict[str, Any] = {
    "name": REPAIR_TOOL_NAME,
    "description": "Devolvé la lista completa de pasos corregida, o decí que no se puede.",
    "input_schema": {
        "type": "object",
        "properties": {
            "se_puede": {
                "type": "boolean",
                "description": (
                    "False si con lo que la plataforma mostró la prueba no puede "
                    "medir lo que quería (la pantalla que necesita no existe)."
                ),
            },
            "pasos": {
                "type": "array", "items": {"type": "string"},
                "description": "TODOS los pasos, en orden, ya corregidos.",
            },
            "que_cambio": {
                "type": "string",
                "description": "En una o dos frases: qué pasos tocaste y por qué.",
            },
        },
        "required": ["se_puede", "que_cambio"],
    },
}

REPAIR_SYSTEM = (
    "Corregís los pasos de UNA prueba de una evaluación de plataformas "
    "educativas. La prueba se escribió antes de ejecutarla y se cortó: un paso "
    "no se pudo hacer porque suponía algo que la plataforma no mostró. Tenés los "
    "pasos, cuál falló, qué dijo quien ejecuta y el texto de la pantalla en ese "
    "momento. Devolvés la lista COMPLETA de pasos corregida para correr la "
    "prueba de nuevo desde el principio, con un estudiante nuevo.\n\n"
    "Lo que podés hacer:\n"
    f"- Marcar con {OPTIONAL_MARK} al comienzo un paso que depende de algo que "
    "la plataforma puede mostrar o no (un diálogo del tutor, un botón que "
    "aparece por un solo camino).\n"
    "- Sacar un paso que ya no hace falta (pide algo que ya está hecho, o una "
    "pantalla que no existe en este recorrido).\n"
    "- Agregar el paso que faltaba para llegar al estado que el paso siguiente "
    "supone (por ejemplo, responder la pregunta antes de avanzar, si la "
    "plataforma no deja saltarla).\n"
    "- Reescribir un paso como objetivo, con lo que la pantalla muestra de "
    "verdad, sin atarlo al texto exacto de un botón.\n\n"
    "Lo que NO podés hacer:\n"
    "- Cambiar lo que la prueba mide: el estímulo (qué se responde, qué se le "
    "escribe al tutor, sobre qué contenido) y las capturas que el criterio "
    "necesita quedan como están.\n"
    "- Agregar pasos que escriban, envíen o cambien algo que la prueba original "
    "no escribía, enviaba ni cambiaba.\n"
    "- Usar condicionales ('si aparece...') ni repeticiones ('hasta que...'): "
    f"un paso que puede no aplicar lleva {OPTIONAL_MARK}; lo que se repite se "
    "escribe una vez por cada vez.\n"
    "- Pedir cronometrar, grabar video o audio.\n\n"
    "Los pasos que empiezan con 'Guardar' son capturas: no se tocan salvo para "
    "moverlos junto con el paso al que acompañan. Si la pantalla muestra que lo "
    "que la prueba necesita no existe en esta plataforma, respondé se_puede=false "
    "y decí qué faltó: eso es un resultado, no un error tuyo."
)


def _tool_input(response: Any) -> dict | None:
    for block in getattr(response, "content", []) or []:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == REPAIR_TOOL_NAME
        ):
            data = getattr(block, "input", None)
            return dict(data) if isinstance(data, dict) else None
    return None


def propose_repair(
    *,
    criterion: str,
    goal: str,
    verdict_rule: str,
    steps: list[str],
    failed_index: int | None,
    reason: str,
    screen: str,
    client: Any,
    model: str,
    preamble: list[str] | None = None,
) -> dict | None:
    """Le pide al modelo los pasos corregidos. None si no los dio.

    Devuelve {"pasos": [...], "que_cambio": str} o {"se_puede": False,
    "que_cambio": str}. No levanta por una respuesta que no sirve (sin
    herramienta, sin pasos): eso es "no hubo reparación" y la prueba se
    reintenta con sus pasos originales. Un error de la API sí sube: un 400 no se
    tapa.
    """
    numbered = "\n".join(f"{n}. {s}" for n, s in enumerate(steps, start=1))
    where = (
        f"El paso que no se pudo hacer es el {failed_index}."
        if failed_index else "No se sabe con certeza qué paso falló; lo dice el motivo."
    )
    prep = (
        "\n\nANTES de estos pasos, la preparación de cada prueba ya hace esto "
        "(no lo repitas ni lo supongas sin hacer):\n"
        + "\n".join(f"- {split_marks(p)[0]}" for p in preamble)
        if preamble else ""
    )
    response = client.messages.create(
        model=model,
        max_tokens=6000,
        system=[{"type": "text", "text": REPAIR_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": [{"type": "text", "text": (
            f"CRITERIO: {criterion}\n"
            f"QUÉ BUSCA LA PRUEBA: {goal}\n"
            f"CÓMO SE DECIDE: {verdict_rule}\n\n"
            f"PASOS\n{numbered}{prep}\n\n"
            f"{where}\n"
            f"LO QUE DIJO QUIEN EJECUTA: {reason}\n\n"
            "TEXTO DE LA PANTALLA EN ESE MOMENTO\n"
            f"{(screen or '(no se pudo leer)')[:6000]}"
        )}]}],
        tools=[REPAIR_TOOL],
        tool_choice={"type": "tool", "name": REPAIR_TOOL_NAME},
    )
    data = _tool_input(response)
    if not data:
        return None
    change = str(data.get("que_cambio") or "").strip()
    if data.get("se_puede") is False:
        return {"se_puede": False, "que_cambio": change}
    new = [str(s).strip() for s in (data.get("pasos") or []) if str(s or "").strip()]
    if not new:
        return None
    return {"pasos": new, "que_cambio": change}


def _actions(steps: list[str]) -> int:
    return sum(1 for s in steps if not is_observation(s))


def accept_repair(
    old: list[str],
    new: list[str],
    *,
    test: Any = None,
    probe: Any = None,
    policy: Any = None,
) -> tuple[list[str] | None, str]:
    """Lo que el código no le cree a una reparación. (pasos, "") o (None, motivo).

    Tres comprobaciones, las tres baratas:

    * Los pasos que cambiaron pasan la misma revisión que los del redactor
      (`steps.step_issues`); lo que el código sabe arreglar solo, lo arregla.
    * No se perdieron las capturas: una reparación que resuelve el corte
      borrando lo que había que mirar no reparó nada.
    * NO APARECE UN RIESGO NUEVO. La compuerta se decidió sobre los pasos que
      revisó el consejo; si los pasos corregidos disparan un marcador que los
      originales no disparaban (escribirle al tutor algo que antes no se
      escribía, tocar una cuenta real), la reparación se descarta y la prueba
      queda cortada. Una reparación nunca es un camino para saltear la compuerta.
    """
    new = [s for s in (new or []) if str(s or "").strip()]
    if not new:
        return None, "la reparación no trajo pasos"
    if list(new) == list(old):
        return None, "la reparación devolvió los mismos pasos"
    # Sólo los pasos que CAMBIARON pasan por la revisión. Los que ya estaban
    # corrieron con el visto bueno del plan; revisarlos de nuevo acá rechazaba
    # toda reparación de una prueba cuya variante trae la aclaración "(Es una
    # orientación: si lo que aparece en pantalla no permite...)", que la
    # revisión lee como un condicional.
    known = set(old)
    checked: list[str] = []
    for step in new:
        if step not in known and step_issues(step):
            mended, _ = repair_steps([step])
            if not mended or step_issues(mended[0]):
                return None, f"un paso corregido no pasa la revisión de pasos: {step[:120]!r}"
            step = mended[0]
        checked.append(step)
    new = checked
    if optional_share(new) > MAX_OPTIONAL_SHARE and optional_share(new) > optional_share(old):
        return None, (
            "la reparación deja demasiados pasos opcionales: la prueba podría "
            "terminar sin haber hecho nada"
        )
    kept = sum(1 for s in new if is_observation(s))
    had = sum(1 for s in old if is_observation(s))
    if had and kept < max(1, had - 1):
        return None, f"la reparación sacó capturas ({had} antes, {kept} después)"
    if _actions(new) > _actions(old) + 4:
        return None, "la reparación agrega demasiadas acciones: es otra prueba"
    if test is not None and probe is not None and policy is not None:
        try:
            from .generator import _risk_blobs

            before = {m.id for m in policy.matches(_risk_blobs(test, probe, policy))}
            fixed = replace(probe, steps=list(new))
            after = {m.id for m in policy.matches(_risk_blobs(test, fixed, policy))}
            declared = policy.self_declaration(_risk_blobs(test, fixed, policy))
            was = policy.self_declaration(_risk_blobs(test, probe, policy))
        except Exception as exc:  # noqa: BLE001 -- sin poder comprobar, no se acepta
            return None, f"no se pudo comprobar el riesgo de los pasos corregidos: {exc}"
        extra = sorted(after - before)
        if extra or (declared is not None and was is None):
            return None, (
                "los pasos corregidos disparan un marcador de riesgo que los "
                "originales no disparaban (" + ", ".join(extra or ["precondición declarada"])
                + "): no se corren sin pasar por la compuerta"
            )
    return list(new), ""


class StepNotDone(EvalkitError):
    """Un paso no se pudo hacer. Lleva cuál y por qué, para poder repararlo.

    Sigue siendo un EvalkitError: quien no sabe reparar lo trata como antes
    (la prueba se corta con este mensaje).
    """

    def __init__(self, message: str, *, step: str = "", reason: str = "", hint: str | None = None):
        super().__init__(message, hint=hint)
        self.step = step
        self.reason = reason
        #: Lo completa el runner, que es quien sabe en qué número de paso iba.
        self.step_index: int | None = None
