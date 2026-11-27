"""La lectura de la regla, producida al generar, revisada por una persona y congelada en el plan.

EL CASO. El 2026-11-27 se volvió a juzgar cuatro veces la evidencia congelada
de una corrida: el juez cambió el veredicto en 23 de 43 criterios, y dos
lectores independientes encontraron que en 21 de esos 23 relataba los mismos
hechos y los juzgaba distinto. La lectura de la regla se generaba de nuevo al
puntuar, en cada corrida, y nadie la veía antes de gastar. Arreglado el defecto
que la dejaba vacía (ver test_lectura_de_la_regla.py), 13 de 24 criterios
seguían volcando con la lectura siempre presente: lo que cambiaba era la
lectura misma.

Lo que estas pruebas fijan, tarea por tarea del plan de la lectura congelada:
la lectura se produce en la etapa de generación (la que tiene cliente y corre
vigilada), no al armar el documento del plan; se muestrea dos veces y sólo una
lectura estable entre muestras, o decidida por una persona, se puede congelar;
y al puntuar se usa la congelada sin volver a pedirla.

EL CONTRATO ESTÁ APAGADO POR DEFECTO (`Engine.frozen_readings`). Encenderlo lo
decide una aceptación contra veredictos humanos que todavía no corrió.

Todo corre contra dobles, con textos inventados. Lo que NO ven: si las
lecturas que escribe el modelo real son estables entre muestras, ni si la
lectura congelada es la CORRECTA. Lo primero se mide al generar; lo segundo,
sólo contra personas.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lxp_eval.evalkit import lectura as lec
from lxp_eval.evalkit import scoring
from lxp_eval.service.jobs import JobRunner
from lxp_eval.service.watchdog import Stage

from tests.test_arreglos_corrida_2026_11_26 import (  # noqa: F401 -- `env` es un fixture
    _correr,
    _fila,
    _plan,
    env,
)


def _criterio(cid="X-01"):
    return SimpleNamespace(
        id=cid, name="Elementos por pantalla", points=1.0,
        pass_criteria="como mucho cuatro", fail_criteria="más de cuatro",
        verification="contar",
    )


def _lectura(**cambios):
    base = dict(
        pregunta="¿Cuántas cosas nuevas hay a la vez?",
        definiciones="Cada botón cuenta.",
        cumple="Cuatro o menos.", no_cumple="Cinco o más.",
        no_alcanza="Una pantalla que no es de contenido.",
    )
    base.update(cambios)
    return scoring.RuleReading(**base)


class _Modelo:
    """Responde por herramienta: {nombre de la herramienta: entrada o lista de entradas}."""

    def __init__(self, **por_herramienta):
        self.por_herramienta = {k: (v if isinstance(v, list) else [v]) for k, v in por_herramienta.items()}
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        name = kw["tools"][0]["name"]
        cola = self.por_herramienta[name]
        data = cola.pop(0) if len(cola) > 1 else cola[0]
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=name, input=data)],
            stop_reason="tool_use",
        )

    def de(self, name):
        return [c for c in self.calls if c["tools"][0]["name"] == name]


# --- Tarea 1: la lectura tiene campos, se produce al generar y se muestrea ---


def test_read_rule_devuelve_campos_y_el_mismo_texto_para_el_juez():
    modelo = _Modelo(emitir_lectura={
        "pregunta": "¿Cuántas?", "definiciones": "Cada botón cuenta.",
        "cumple": "Cuatro o menos.", "no_cumple": "Cinco o más.",
        "no_alcanza": "Una portada.",
    })
    reading = scoring.read_rule(_criterio(), modelo, model="m")

    assert isinstance(reading, scoring.RuleReading)
    assert (reading.cumple, reading.no_cumple) == ("Cuatro o menos.", "Cinco o más.")
    assert reading.parcial == "", "PARCIAL puede faltar: hay reglas sin estado intermedio"
    assert str(reading) == (
        "- Qué pregunta la regla: ¿Cuántas?\n"
        "- Definiciones: Cada botón cuenta.\n"
        "- CUMPLE si se observa: Cuatro o menos.\n"
        "- NO_CUMPLE si se observa: Cinco o más.\n"
        "- NO alcanza para decidir (es NO_EVALUADO): Una portada."
    ), "el texto que recibe el juez es el de siempre: el instrumento no cambia"
    assert scoring.RuleReading.from_dict(reading.as_dict()) == reading


def test_a_reading_without_its_two_ends_cannot_be_built():
    with pytest.raises(scoring.ReadingUnavailable):
        scoring.RuleReading.from_dict({"pregunta": "¿Cuántas?", "cumple": "Cuatro."})


def test_two_samples_that_agree_make_one_stable_reading():
    leidas = []

    def read(test, client, *, model=None, context=""):
        leidas.append(context)
        return _lectura(definiciones=f"Cada botón cuenta (muestra {len(leidas)}).")

    def compare(test, readings, client, *, model=None):
        return {"equivalentes": True, "diferencias": ""}

    out = lec.write_readings(
        [_criterio()], client=None, contexts={"X-01": "abrir una pregunta"},
        reader=read, compare=compare,
    )

    r = out["X-01"]
    assert leidas == ["abrir una pregunta"] * 2, "dos muestras, con el contexto del plan"
    assert r["estado"] == lec.ESTABLE
    assert len(r["muestras"]) == 2
    assert r["lectura"] == r["muestras"][0], "estable: vale la primera muestra"
    assert r["acuerdo"]["equivalentes"] is True


def test_two_samples_that_disagree_wait_for_a_person():
    def compare(test, readings, client, *, model=None):
        return {"equivalentes": False, "diferencias": "una cuenta cada opción y la otra el bloque"}

    out = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=compare,
    )

    r = out["X-01"]
    assert r["estado"] == lec.REQUIERE_ADJUDICACION
    assert r["lectura"] is None, "sin acuerdo no hay UNA lectura: no se congela n=1"
    assert len(r["muestras"]) == 2
    assert "cada opción" in r["acuerdo"]["diferencias"]
    assert not lec.freezable(r)


def test_a_sample_that_does_not_come_out_is_not_papered_over():
    veces = []

    def read(test, client, *, model=None, context=""):
        veces.append(1)
        if len(veces) == 2:
            raise scoring.ReadingUnavailable("la lectura de X-01 llegó sin: cumple")
        return _lectura()

    out = lec.write_readings([_criterio()], client=None, reader=read,
                             compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""})

    r = out["X-01"]
    assert r["estado"] == lec.NO_DISPONIBLE
    assert r["lectura"] is None and "llegó sin: cumple" in r["motivo"]
    assert not lec.freezable(r)


def test_the_comparison_is_its_own_question_to_the_model():
    modelo = _Modelo(comparar_lecturas={"equivalentes": False, "diferencias": "el umbral cambia"})
    out = lec.compare_readings(_criterio(), [_lectura(), _lectura(cumple="Tres o menos.")], modelo, model="m")

    assert out == {"equivalentes": False, "diferencias": "el umbral cambia"}
    pedido = modelo.calls[0]
    texto = pedido["messages"][0]["content"][0]["text"]
    assert "Cuatro o menos." in texto and "Tres o menos." in texto
    assert pedido["system"][0]["text"] == lec.COMPARE_SYSTEM


def test_an_answer_that_does_not_say_whether_they_agree_counts_as_disagreement():
    """Lo que no se pudo comparar no se da por igual: va a una persona."""
    modelo = _Modelo(comparar_lecturas={"diferencias": ""})
    out = lec.compare_readings(_criterio(), [_lectura(), _lectura()], modelo, model="m")
    assert out["equivalentes"] is False


def _generar(store, engine, job_id):
    record = store.get(job_id)
    asyncio.run(JobRunner(store, engine, None).draft_from_plan(record))
    return store.get(job_id)


def test_lectura_se_produce_en_generacion_y_se_muestrea(tmp_path, env):  # noqa: F811
    leidas: list[str] = []

    def read(test, client, *, model=None, context=""):
        leidas.append(test.id)
        return _lectura()

    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True, read_rule=read,
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
    )
    antes = store.get(job_id)
    assert antes.stage == Stage.PLAN_READY
    assert leidas == [] and antes.readings == {}, \
        "armar el documento del plan no llama al modelo: la lectura no se produce ahí"

    record = _generar(store, engine, job_id)

    assert record.stage == Stage.AWAITING_APPROVAL
    probados = [d["id"] for d in record.strategy if d["estado"] == "se_prueba"]
    assert probados and set(record.readings) == set(probados)
    for cid in probados:
        assert leidas.count(cid) == 2, "dos muestras por criterio"
        r = record.readings[cid]
        assert r["estado"] == lec.ESTABLE and len(r["muestras"]) == 2
        assert r["lectura"]["cumple"] == "Cuatro o menos."


def test_with_the_contract_off_nothing_is_read_while_generating(tmp_path, env):  # noqa: F811
    leidas: list[str] = []

    def read(test, client, *, model=None, context=""):
        leidas.append(test.id)
        return _lectura()

    store, engine, job_id = _plan(tmp_path, read_rule=read)
    assert engine.frozen_readings is False, "apagado por defecto"
    record = _generar(store, engine, job_id)

    assert record.stage == Stage.AWAITING_APPROVAL
    assert leidas == [] and record.readings == {}


# --- Tarea 2: el chequeo de vaguedad es DE LA LECTURA, con su propio prompt ---

#: Las huellas del instrumento el 2026-11-27, antes de la lectura congelada.
#: Dos corridas sólo se comparan si el juez y el consejo fueron los mismos; el
#: informe de la corrida que midió el piso de ruido trae esta huella del juez.
#: Si una de estas pruebas falla, alguien cambió un prompt del juez o del
#: consejo: eso vuelve incomparable lo anterior, y es una decisión que se toma
#: a la vista, actualizando el valor acá, no de costado.
_HUELLA_DEL_JUEZ = "de7523978a9b71dd5240b09fe52a96ea7ef5cbc4cd5580712d2e19e526414e8e"
_HUELLA_DEL_CONSEJO = "b87a476d539548af27f2fef28bc8e256f2fda98204e1ccc4fb2b8ccf15cde5fc"


def test_council_fingerprint_estable():
    from lxp_eval.evalkit import generator

    assert generator.council_fingerprint() == _HUELLA_DEL_CONSEJO
    textos = " ".join(j.system for j in generator.COUNCIL)
    assert lec.VAGUE_SYSTEM not in textos and lec.COMPARE_SYSTEM not in textos, \
        "los chequeos de la lectura no son asientos del consejo"


def test_judge_fingerprint_estable():
    assert scoring.judge_fingerprint() == _HUELLA_DEL_JUEZ


def test_chequeo_vaguedad_lectura():
    modelo = _Modelo(chequear_lectura={
        "vaga": True, "parcial_definido": False, "anclas_contables": True,
        "objeciones": ["PARCIAL no dice qué hecho lo distingue de CUMPLE"],
    })
    out = lec.check_reading(_criterio(), _lectura(parcial="Algo intermedio."), modelo, model="m")

    assert out == {
        "vaga": True, "parcial_definido": False, "anclas_contables": True,
        "objeciones": ["PARCIAL no dice qué hecho lo distingue de CUMPLE"],
        "sobre": lec.SOBRE_MODELO,
    }
    pedido = modelo.calls[0]
    assert pedido["system"][0]["text"] == lec.VAGUE_SYSTEM
    assert "- PARCIAL si se observa: Algo intermedio." in pedido["messages"][0]["content"][0]["text"]


def test_a_check_that_did_not_answer_is_an_objection_not_a_pass():
    out = lec.check_reading(_criterio(), _lectura(), _Modelo(chequear_lectura={"objeciones": []}), model="m")
    assert out["vaga"] is True and "no se pudo chequear" in out["objeciones"][0]


def test_a_stable_reading_carries_its_vagueness_check_as_a_field():
    vistas = []

    def check(test, reading, client, *, model=None):
        vistas.append(reading)
        return {"vaga": True, "parcial_definido": False, "anclas_contables": True,
                "objeciones": ["PARCIAL se decide a ojo"], "sobre": lec.SOBRE_MODELO}

    out = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        check=check,
    )

    r = out["X-01"]
    assert r["vaguedad"]["vaga"] is True and r["vaguedad"]["objeciones"] == ["PARCIAL se decide a ojo"]
    assert vistas == [_lectura()], "se chequea la lectura que quedó, una vez"
    assert "PARCIAL se decide a ojo" in lec.review_note(r)


def test_there_is_nothing_to_check_when_there_is_no_single_reading():
    llamado = []
    out = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
        check=lambda *a, **k: llamado.append(1),
    )
    assert llamado == [] and out["X-01"]["vaguedad"] is None
    assert "otro umbral" in lec.review_note(out["X-01"])


def test_a_vague_reading_marks_its_criterion_for_review_in_the_plan(tmp_path, env):  # noqa: F811
    def check(test, reading, client, *, model=None):
        return {"vaga": True, "parcial_definido": False, "anclas_contables": True,
                "objeciones": ["PARCIAL se decide a ojo"], "sobre": lec.SOBRE_MODELO}

    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        check_reading=check,
    )
    record = _generar(store, engine, job_id)

    probados = [d for d in record.strategy if d["estado"] == "se_prueba"]
    assert probados
    for d in probados:
        assert d["revisar"] is True
        assert any("PARCIAL se decide a ojo" in n for n in d["advertencias"])
        assert record.readings[d["id"]]["vaguedad"]["vaga"] is True, "y es un campo de la lectura, no sólo un aviso"


# --- lo que encontró la revisión de la tarea 1 --------------------------------


def test_only_a_true_agreement_makes_a_reading_stable():
    """Un comparador reemplazado que devuelve "false" como texto no es un acuerdo."""
    out = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": "false", "diferencias": ""},
    )
    assert out["X-01"]["estado"] == lec.REQUIERE_ADJUDICACION and out["X-01"]["lectura"] is None


def test_an_api_error_stops_the_readings_that_had_not_started():
    """Un 400 falla igual para siempre: no se siguen pagando los criterios que faltaban."""
    leidas = []

    def read(test, client, *, model=None, context=""):
        leidas.append(test.id)
        raise RuntimeError("400 invalid_request_error")

    with pytest.raises(RuntimeError):
        lec.write_readings(
            [_criterio("X-01"), _criterio("X-02"), _criterio("X-03")], client=None,
            reader=read, compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
            max_workers=1,
        )
    assert leidas == ["X-01"]


def test_a_failed_reading_stage_fails_the_job_and_says_why(tmp_path, env):  # noqa: F811
    def write(tests, **kw):
        raise RuntimeError("400 invalid_request_error: tool schema")

    store, engine, job_id = _plan(tmp_path, frozen_readings=True, write_readings=write)
    record = _generar(store, engine, job_id)

    assert record.stage == Stage.ERROR
    assert "400 invalid_request_error" in str(record.error)
    assert record.probes == [], "sin lecturas no se redacta: la etapa se corta ahí"


def test_readings_of_a_second_drafting_add_to_the_first(tmp_path, env):  # noqa: F811
    """Un piloto de un criterio y después el resto: las lecturas del piloto no se pierden."""
    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
    )
    record = store.get(job_id)
    record.readings = {"OTRO-99": {"estado": lec.ESTABLE, "lectura": _lectura().as_dict()}}
    store.save(record)
    record = _generar(store, engine, job_id)

    probados = [d["id"] for d in record.strategy if d["estado"] == "se_prueba"]
    assert set(record.readings) == set(probados) | {"OTRO-99"}


# --- Tarea 3: una lectura objetada sienta al consejo completo ----------------


def test_a_vague_reading_seats_the_whole_council_even_if_no_seat_objected():
    """El asiento nuevo: nadie del consejo reducido objetó, y el consejo igual se amplía."""
    from lxp_eval.evalkit import generator as gen
    from tests.test_arreglos_corrida_completa import _Cliente, _criterios

    objetada = _Cliente([])
    gen.generate_probes(_criterios(), client=objetada, council=gen.REDUCED_COUNCIL,
                        escalate_to=gen.COUNCIL, max_rounds=1, escalate_for={"PT-D4-05"})
    assert sorted(objetada.jueces) == sorted(j.name for j in gen.COUNCIL)

    de_otro = _Cliente([])
    gen.generate_probes(_criterios(), client=de_otro, council=gen.REDUCED_COUNCIL,
                        escalate_to=gen.COUNCIL, max_rounds=1, escalate_for={"OTRO-01"})
    assert sorted(de_otro.jueces) == ["coherencia_criterio", "seguridad"], \
        "la objeción a la lectura de OTRO criterio no amplía el consejo de este"


def _con_espia_del_consejo(tmp_path, vaga, **kw):
    from lxp_eval.evalkit import generator as gen

    pedidos: list[dict] = []

    def generate(criteria, **llamada):
        pedidos.append(llamada)
        return gen.generate_probes(criteria, **llamada)

    def check(test, reading, client, *, model=None):
        return {"vaga": vaga, "parcial_definido": not vaga, "anclas_contables": True,
                "objeciones": ["PARCIAL se decide a ojo"] if vaga else [], "sobre": lec.SOBRE_MODELO}

    store, engine, job_id = _plan(
        tmp_path, generate_probes=generate, check_reading=check,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        **kw,
    )
    return _generar(store, engine, job_id), pedidos


def test_the_reading_check_runs_before_approval_and_escalates_when_it_objects(tmp_path, env):  # noqa: F811
    record, pedidos = _con_espia_del_consejo(tmp_path, vaga=True, frozen_readings=True)

    probados = {d["id"] for d in record.strategy if d["estado"] == "se_prueba"}
    assert record.stage == Stage.AWAITING_APPROVAL, "todo esto pasa antes de la aprobación"
    assert set(pedidos[0]["escalate_for"]) == probados
    assert all(p["consejo"] == "ampliado" for p in record.probes)


def test_a_clean_reading_keeps_the_reduced_council(tmp_path, env):  # noqa: F811
    record, pedidos = _con_espia_del_consejo(tmp_path, vaga=False, frozen_readings=True)

    assert set(pedidos[0]["escalate_for"]) == set()
    assert all(p["consejo"] == "reducido" for p in record.probes)


def test_with_the_contract_off_the_council_is_asked_exactly_as_before(tmp_path, env):  # noqa: F811
    record, pedidos = _con_espia_del_consejo(tmp_path, vaga=True)

    assert "escalate_for" not in pedidos[0]
    assert all(p["consejo"] == "reducido" for p in record.probes)
