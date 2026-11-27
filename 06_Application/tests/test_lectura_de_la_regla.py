"""La lectura de la regla que el motor descartaba, y lo que pasa cuando de verdad no hay lectura.

EL CASO. El 2026-11-27 se volvió a juzgar cuatro veces la evidencia congelada
de una corrida completa (43 criterios, 172 juicios de criterio). En 48 de esos
172 la lectura de la regla llegó VACÍA y el criterio se juzgó sin ella, sin que
nada lo dijera. Dos pasadas del mismo criterio diferían en 21 % de los pares
cuando las dos tenían (o no tenían) lectura, y en 58 % cuando una la tenía y la
otra no: una instrucción que aparece y desaparece al azar es ruido puesto por
el evaluador.

El modelo SÍ había escrito la lectura entera. La respuesta cruda de siete
llamadas mostró siempre lo mismo: dentro del texto de un campo venía pegado el
cierre de ese campo y la apertura del siguiente,

    "definiciones": "...no inferido de documentación.</parameter>\n<parameter name=\"cumple\">En 3 o más pares...",

así que `cumple` no llegaba como clave, `read_rule` veía que faltaba un extremo
y devolvía "". Estas pruebas fijan las tres cosas que cambian: lo que vino
pegado se recupera, una lectura que igual queda incompleta se pide otra vez, y
si tampoco sale el criterio NO se puntúa en silencio.

Todo corre contra dobles, con textos inventados. Lo que NO ven: si el modelo
real sigue pegando los campos de esta forma o de otra nueva. Eso se mira
contando las lecturas vacías en una corrida.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lxp_eval.evalkit import scoring

from tests.test_arreglos_corrida_2026_11_26 import (  # noqa: F401 -- `env` es un fixture
    _correr,
    _fila,
    _plan,
    env,
)


def _criterio():
    return SimpleNamespace(
        id="X-01", name="Elementos por pantalla", points=1.0,
        pass_criteria="como mucho cuatro", fail_criteria="más de cuatro",
        verification="contar",
    )


class _Modelo:
    """Responde, en orden, las entradas de herramienta que le den. Repite la última."""

    def __init__(self, *respuestas):
        self.respuestas = list(respuestas)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        data = self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]
        name = kw["tools"][0]["name"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=name, input=data)],
            stop_reason="tool_use",
        )


#: La forma exacta que tenía la respuesta que el motor descartaba: `cumple`
#: pegado dentro de `definiciones`, y `parcial` pegado al final de `no_alcanza`,
#: con el cierre de la llamada incluido.
_PEGADA = {
    "pregunta": "¿Cuántas cosas nuevas hay a la vez?",
    "definiciones": (
        '"Cosa nueva": cada botón cuenta.</parameter>\n'
        '<parameter name="cumple">Se cuentan cuatro o menos.'
    ),
    "no_cumple": "Se cuentan cinco o más.",
    "no_alcanza": (
        "Una pantalla que no es de contenido.</parameter>\n"
        '<parameter name="parcial">No aplica.</parameter>\n</invoke>\n'
    ),
}

_COMPLETA = {
    "pregunta": "¿Cuántas cosas nuevas hay a la vez?",
    "definiciones": "Cada botón cuenta.",
    "cumple": "Cuatro o menos.", "no_cumple": "Cinco o más.",
    "no_alcanza": "Una pantalla que no es de contenido.",
}


def test_a_field_glued_inside_another_is_put_back_where_it_belongs():
    modelo = _Modelo(_PEGADA)
    reading = str(scoring.read_rule(_criterio(), modelo, model="m"))

    assert "- CUMPLE si se observa: Se cuentan cuatro o menos." in reading
    assert "- PARCIAL si se observa: No aplica." in reading
    assert '- Definiciones: "Cosa nueva": cada botón cuenta.' in reading
    assert "- NO alcanza para decidir (es NO_EVALUADO): Una pantalla que no es de contenido." in reading
    for resto in ("</parameter>", "<parameter", "</invoke>"):
        assert resto not in reading, "al juez no le llega el marcado de la llamada"
    assert len(modelo.calls) == 1, "lo que vino pegado se recupera, no se vuelve a pedir"


def test_everything_glued_into_one_field_is_still_a_whole_reading():
    """En una de las siete respuestas crudas sólo llegaron dos claves: el resto venía adentro."""
    modelo = _Modelo({
        "pregunta": "¿Cambia la dificultad?",
        "definiciones": (
            "Cambia: sube o baja.</parameter>\n"
            '<parameter name="cumple">Cambia en tres pares.</parameter>\n'
            '<parameter name="no_cumple">Es siempre igual.</parameter>\n'
            '<parameter name="no_alcanza">Que el banco rote.</parameter>\n</invoke>'
        ),
    })
    reading = str(scoring.read_rule(_criterio(), modelo, model="m"))

    assert "- Definiciones: Cambia: sube o baja." in reading
    assert "- CUMPLE si se observa: Cambia en tres pares." in reading
    assert "- NO_CUMPLE si se observa: Es siempre igual." in reading
    assert reading.rstrip().endswith("Que el banco rote.")


def test_a_real_key_is_never_overwritten_by_a_glued_one():
    data = dict(_COMPLETA)
    data["definiciones"] = 'Cada botón cuenta.</parameter>\n<parameter name="cumple">OTRA COSA.'
    reading = str(scoring.read_rule(_criterio(), _Modelo(data), model="m"))

    assert "- CUMPLE si se observa: Cuatro o menos." in reading
    assert "OTRA COSA" not in reading, "lo pegado que repite una clave que sí llegó se descarta"
    assert "- Definiciones: Cada botón cuenta." in reading and "<parameter" not in reading


def test_a_reading_that_is_still_incomplete_is_asked_again_once():
    modelo = _Modelo({"pregunta": "¿Cuántas?", "no_cumple": "Cinco o más."}, _COMPLETA)
    reading = str(scoring.read_rule(_criterio(), modelo, model="m"))

    assert "- CUMPLE si se observa: Cuatro o menos." in reading
    assert len(modelo.calls) == 2
    assert modelo.calls[0] == modelo.calls[1], "el segundo pedido es el mismo, sin agregados"


def test_two_incomplete_readings_are_an_error_that_names_what_was_missing():
    modelo = _Modelo({"pregunta": "¿Cuántas?", "no_cumple": "Cinco o más."})
    with pytest.raises(scoring.ReadingUnavailable) as err:
        scoring.read_rule(_criterio(), modelo, model="m")

    assert len(modelo.calls) == 2, "se pide dos veces y no más"
    assert "cumple" in str(err.value) and "X-01" in str(err.value)


def test_an_answer_that_is_not_a_reading_at_all_is_the_same_error():
    with pytest.raises(scoring.ReadingUnavailable):
        scoring.read_rule(_criterio(), _Modelo({"verdict": "CUMPLE"}), model="m")


def test_an_api_error_is_not_swallowed_nor_retried_here():
    """Un 400 falla igual para siempre: sube al primer intento (ver service/retry.py)."""
    calls = []

    def create(**kw):
        calls.append(kw)
        raise RuntimeError("400 invalid_request_error")

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    with pytest.raises(RuntimeError):
        scoring.read_rule(_criterio(), client, model="m")
    assert len(calls) == 1


# --- en el servicio: sin lectura no se puntúa en silencio --------------------


def _sin_lectura(tmp_path, read):
    juzgados: list[str] = []

    def score(test, rows, client, **kw):
        juzgados.append(test.id)
        return scoring.score_test(test, rows, client, **kw)

    store, engine, job_id = _plan(
        tmp_path, read_rule=read, score_test=score,
        check_verdicts=lambda *a, **k: {"ok": True, "motivo": ""},
    )
    return store, engine, job_id, juzgados


def test_a_criterion_whose_rule_could_not_be_read_is_not_scored(tmp_path, env):  # noqa: F811
    def read(test, client, *, model=None, context=""):
        raise scoring.ReadingUnavailable(f"la lectura de {test.id} llegó sin: cumple")

    store, engine, job_id, juzgados = _sin_lectura(tmp_path, read)
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)

    fila = _fila(store, job_id, probados[0])
    assert fila["verdict"] == "NO_EVALUADO"
    assert fila["rationale"].startswith("[causa: no se pudo leer la regla]")
    assert "llegó sin: cumple" in fila["rationale"]
    assert juzgados == [], "sin lectura no se llama al juez: no hay veredicto a medias"

    meta = store.get(job_id).report["meta"]
    assert meta["autochequeo"]["ok"] is False
    assert "no se pudo leer la regla" in meta["autochequeo"]["fallasDelEvaluador"], \
        "es una falla del evaluador y el informe la nombra arriba"


def test_an_empty_reading_counts_as_no_reading_too(tmp_path, env):  # noqa: F811
    """Un lector que devuelve "" no es un permiso para juzgar sin lectura."""
    store, engine, job_id, juzgados = _sin_lectura(
        tmp_path, lambda test, client, *, model=None, context="": "",
    )
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)

    assert _fila(store, job_id, probados[0])["rationale"].startswith(
        "[causa: no se pudo leer la regla]"
    )
    assert juzgados == []


def test_turning_the_reading_off_on_purpose_still_judges(tmp_path, env):  # noqa: F811
    """`read_rule=None` es una decisión de quien configura el motor, no una falla."""
    store, engine, job_id, juzgados = _sin_lectura(tmp_path, None)
    probados = [d["id"] for d in store.get(job_id).strategy if d["estado"] == "se_prueba"]
    _correr(store, engine, job_id)

    assert juzgados, "con la lectura apagada a propósito se juzga como antes"
    assert not _fila(store, job_id, probados[0])["rationale"].startswith(
        "[causa: no se pudo leer la regla]"
    )
