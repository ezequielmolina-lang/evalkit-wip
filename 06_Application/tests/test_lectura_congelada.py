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


# --- lo que encontró la revisión de la tarea 2 --------------------------------


def test_the_real_error_surfaces_even_if_a_skipped_criterion_finishes_first(monkeypatch):
    """Un 400 se muestra, no se tapa.

    El hilo que falla avisa a los demás ANTES de que su resultado quede listo,
    así que un criterio salteado puede terminar primero. Su resultado vacío no
    puede usarse ni convertir el 400 en un error de tipos.
    """
    from concurrent.futures import wait

    def salteados_primero(futures):
        wait(futures)
        return sorted(futures, key=lambda f: f.exception() is not None)

    monkeypatch.setattr(lec, "as_completed", salteados_primero)

    def read(test, client, *, model=None, context=""):
        raise RuntimeError("400 invalid_request_error")

    with pytest.raises(RuntimeError, match="400 invalid_request_error"):
        lec.write_readings(
            [_criterio("X-01"), _criterio("X-02"), _criterio("X-03")], client=None,
            reader=read, compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
            max_workers=1,
        )


def test_a_check_that_contradicts_itself_is_an_objection():
    """`vaga: false` con un PARCIAL sin definir o con objeciones escritas no es un visto bueno."""
    for respuesta in (
        {"vaga": False, "parcial_definido": False, "anclas_contables": True, "objeciones": []},
        {"vaga": False, "parcial_definido": True, "anclas_contables": False, "objeciones": []},
        {"vaga": False, "parcial_definido": True, "anclas_contables": True, "objeciones": ["PARCIAL falta"]},
    ):
        out = lec.check_reading(_criterio(), _lectura(), _Modelo(chequear_lectura=respuesta), model="m")
        assert out["vaga"] is True, respuesta
        assert lec.review_note({"estado": lec.ESTABLE, "lectura": _lectura().as_dict(), "vaguedad": out})

    limpia = lec.check_reading(_criterio(), _lectura(), _Modelo(chequear_lectura={
        "vaga": False, "parcial_definido": True, "anclas_contables": True, "objeciones": [],
    }), model="m")
    assert limpia["vaga"] is False
    assert lec.review_note({"estado": lec.ESTABLE, "lectura": _lectura().as_dict(), "vaguedad": limpia}) == ""


def test_a_reviewer_double_that_objects_without_saying_vague_still_marks_the_criterion():
    """La nota mira las objeciones, no sólo la bandera: un chequeo reemplazado no la esquiva."""
    entrada = {"estado": lec.ESTABLE, "lectura": _lectura().as_dict(),
               "vaguedad": {"vaga": False, "parcial_definido": False, "anclas_contables": True, "objeciones": []}}
    assert "decidir a ojo" in lec.review_note(entrada)


def test_with_the_contract_off_a_new_drafting_drops_the_old_readings_of_those_criteria(tmp_path, env):  # noqa: F811
    """El único cambio con el contrato apagado: una lectura vieja no sobrevive a otra redacción."""
    store, engine, job_id = _plan(tmp_path)
    record = store.get(job_id)
    probados = [d["id"] for d in record.strategy if d["estado"] == "se_prueba"]
    record.readings = {
        probados[0]: {"estado": lec.ESTABLE, "lectura": _lectura().as_dict()},
        "OTRO-99": {"estado": lec.ESTABLE, "lectura": _lectura().as_dict()},
    }
    store.save(record)
    record = _generar(store, engine, job_id)

    assert set(record.readings) == {"OTRO-99"}, "se va la del criterio vuelto a redactar; la de otro queda"


# --- Tarea 4: la persona edita la lectura por un camino propio ---------------


def _estable(**cambios):
    entrada = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        check=lambda *a, **k: {"vaga": False, "parcial_definido": True, "anclas_contables": True,
                               "objeciones": [], "sobre": lec.SOBRE_MODELO},
    )["X-01"]
    entrada.update(cambios)
    return entrada


def _chequeo_que_anota(vistas, vaga=False):
    def check(test, reading, client, *, model=None, sobre=lec.SOBRE_MODELO):
        vistas.append((reading, sobre))
        return {"vaga": vaga, "parcial_definido": not vaga, "anclas_contables": True,
                "objeciones": ["PARCIAL se decide a ojo"] if vaga else [], "sobre": sobre}
    return check


def test_edicion_humana_de_lectura_rehashea_y_rechequea():
    antes = _estable()
    vistas = []
    despues = lec.edit_reading(
        antes, _criterio(), {"cumple": "Tres o menos."}, by="E. M.",
        client=None, check=_chequeo_que_anota(vistas, vaga=True), now="2026-11-27T20:00:00+00:00",
    )

    assert despues["lectura"]["cumple"] == "Tres o menos."
    assert despues["lectura"]["no_cumple"] == antes["lectura"]["no_cumple"], "sólo cambia el campo editado"
    assert despues["estado"] == lec.EDITADA and lec.freezable(despues)
    assert (despues["editada_por"], despues["editada_en"]) == ("E. M.", "2026-11-27T20:00:00+00:00")
    assert despues["hash_lectura"] != antes["hash_lectura"], "se vuelve a hashear"
    assert despues["hash_lectura"] == lec.reading_hash(despues["lectura"])

    abajo = despues["historial"][-1]
    assert abajo["lectura"] == antes["lectura"] and abajo["hash_lectura"] == antes["hash_lectura"]
    assert abajo["estado"] == lec.ESTABLE, "la lectura anterior y su huella quedan debajo"

    assert vistas == [(scoring.RuleReading.from_dict(despues["lectura"]), lec.SOBRE_PERSONA)], \
        "el chequeo de vaguedad corre de nuevo, sobre el texto de la persona, y lo dice"
    assert despues["vaguedad"]["sobre"] == lec.SOBRE_PERSONA and despues["vaguedad"]["vaga"] is True
    assert antes["estado"] == lec.ESTABLE and "historial" not in antes, "no se toca el registro que entró"


def test_an_objection_to_a_persons_text_is_shown_but_does_not_undo_their_decision():
    despues = lec.edit_reading(
        _estable(), _criterio(), {"parcial": "Algo intermedio."}, by="E. M.",
        client=None, check=_chequeo_que_anota([], vaga=True),
    )
    assert lec.freezable(despues), "la persona decide; la objeción queda a la vista"
    assert "decidir a ojo" in lec.review_note(despues)


def test_a_person_can_write_the_reading_nobody_agreed_on():
    sin_acuerdo = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": _lectura(),
        compare=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
    )["X-01"]
    assert not lec.freezable(sin_acuerdo)

    escrita = lec.edit_reading(sin_acuerdo, _criterio(), _lectura(cumple="Tres o menos.").as_dict(),
                               by="E. M.", client=None, check=_chequeo_que_anota([]))
    assert escrita["estado"] == lec.EDITADA and lec.freezable(escrita)
    assert escrita["historial"][-1]["estado"] == lec.REQUIERE_ADJUDICACION
    assert len(escrita["muestras"]) == 2, "las muestras quedan: son lo que la persona tuvo delante"


def test_an_edit_that_leaves_the_reading_without_an_end_is_refused():
    antes = _estable()
    with pytest.raises(scoring.ReadingUnavailable):
        lec.edit_reading(antes, _criterio(), {"cumple": "  "}, by="E. M.", client=None,
                         check=_chequeo_que_anota([]))


def test_an_edit_needs_a_name():
    with pytest.raises(ValueError):
        lec.edit_reading(_estable(), _criterio(), {"cumple": "Tres."}, by=" ", client=None,
                         check=_chequeo_que_anota([]))


def test_a_person_can_pick_one_of_the_samples():
    muestras = iter([_lectura(), _lectura(cumple="Tres o menos.")])
    sin_acuerdo = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": next(muestras),
        compare=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
    )["X-01"]
    vistas = []
    elegida = lec.adjudicate_reading(sin_acuerdo, _criterio(), 2, by="E. M.", client=None,
                                     check=_chequeo_que_anota(vistas))

    assert elegida["estado"] == lec.ADJUDICADA and lec.freezable(elegida)
    assert elegida["lectura"]["cumple"] == "Tres o menos." and elegida["muestra_elegida"] == 2
    assert vistas[0][1] == lec.SOBRE_MODELO, "el texto elegido lo escribió el modelo"
    with pytest.raises(ValueError):
        lec.adjudicate_reading(sin_acuerdo, _criterio(), 3, by="E. M.", client=None,
                               check=_chequeo_que_anota([]))


def test_the_service_edits_a_reading_before_approval_and_leaves_the_probes_alone(tmp_path, env):  # noqa: F811
    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        check_reading=_chequeo_que_anota([]),
    )
    record = _generar(store, engine, job_id)
    cid = next(d["id"] for d in record.strategy if d["estado"] == "se_prueba")
    pruebas = [dict(p) for p in record.probes]

    runner = JobRunner(store, engine, None)
    asyncio.run(runner.edit_reading(record, cid, {"cumple": "Tres o menos."}, by="E. M."))

    guardado = store.get(job_id)
    assert guardado.readings[cid]["estado"] == lec.EDITADA
    assert guardado.readings[cid]["lectura"]["cumple"] == "Tres o menos."
    assert guardado.readings[cid]["editada_por"] == "E. M."
    assert guardado.probes == pruebas, "editar la lectura no toca las pruebas ni las vuelve a revisar"


def test_the_service_refuses_to_edit_a_reading_once_the_plan_is_approved(tmp_path, env):  # noqa: F811
    from lxp_eval.service.crypto import ServiceError

    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
    )
    _correr(store, engine, job_id)
    record = store.get(job_id)
    cid = next(iter(record.readings))
    with pytest.raises(ServiceError):
        asyncio.run(JobRunner(store, engine, None).edit_reading(record, cid, {"cumple": "Tres."}, by="E. M."))


# --- lo que encontró la revisión de la tarea 4 --------------------------------


def test_an_edit_with_a_misspelled_field_is_refused_not_ignored():
    """Quien escribe "cumpel" cree que corrigió algo: no puede quedar como editada sin cambiar nada."""
    vistas = []
    with pytest.raises(ValueError, match="cumpel"):
        lec.edit_reading(_estable(), _criterio(), {"cumpel": "Tres."}, by="E. M.", client=None,
                         check=_chequeo_que_anota(vistas))
    assert vistas == [], "se rechaza antes de gastar en el chequeo"


def test_an_edit_that_changes_nothing_is_refused():
    antes = _estable()
    for campos in ({}, {"cumple": antes["lectura"]["cumple"]}, {"cumple": "  Cuatro o menos.  "}):
        with pytest.raises(ValueError, match="no cambia nada"):
            lec.edit_reading(antes, _criterio(), campos, by="E. M.", client=None,
                             check=_chequeo_que_anota([]))


def test_only_text_goes_into_a_reading():
    """Una lista o un número no se convierten en texto para el juez."""
    for valor in (["a", "b"], {"x": 1}, 0, True):
        with pytest.raises(ValueError, match="texto"):
            lec.edit_reading(_estable(), _criterio(), {"cumple": valor}, by="E. M.", client=None,
                             check=_chequeo_que_anota([]))


def test_none_clears_an_optional_field_but_not_an_end():
    con_parcial = _estable()
    con_parcial["lectura"]["parcial"] = "Algo intermedio."
    sin = lec.edit_reading(con_parcial, _criterio(), {"parcial": None}, by="E. M.", client=None,
                           check=_chequeo_que_anota([]))
    assert sin["lectura"]["parcial"] == ""
    with pytest.raises(scoring.ReadingUnavailable):
        lec.edit_reading(_estable(), _criterio(), {"cumple": None}, by="E. M.", client=None,
                         check=_chequeo_que_anota([]))


def test_the_picked_sample_does_not_outlive_a_later_edit():
    muestras = iter([_lectura(), _lectura(cumple="Tres o menos.")])
    sin_acuerdo = lec.write_readings(
        [_criterio()], client=None,
        reader=lambda test, client, *, model=None, context="": next(muestras),
        compare=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
    )["X-01"]
    elegida = lec.adjudicate_reading(sin_acuerdo, _criterio(), 2, by="E. M.", client=None,
                                     check=_chequeo_que_anota([]))
    editada = lec.edit_reading(elegida, _criterio(), {"cumple": "Dos o menos."}, by="E. M.",
                               client=None, check=_chequeo_que_anota([]))

    assert "muestra_elegida" not in editada, "ya no es la muestra 2: la persona la cambió"
    assert editada["historial"][-1]["muestra_elegida"] == 2, "y el historial dice cuál se había elegido"


def test_picking_needs_two_samples_and_a_real_number():
    una_sola = lec.write_readings(
        [_criterio()], client=None,
        reader=_lector_que_falla_la_segunda(),
        compare=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
    )["X-01"]
    assert una_sola["estado"] == lec.NO_DISPONIBLE and len(una_sola["muestras"]) == 1
    with pytest.raises(ValueError, match="dos muestras"):
        lec.adjudicate_reading(una_sola, _criterio(), 1, by="E. M.", client=None,
                               check=_chequeo_que_anota([]))
    with pytest.raises(ValueError):
        lec.adjudicate_reading(_estable(), _criterio(), True, by="E. M.", client=None,
                               check=_chequeo_que_anota([]))


def _lector_que_falla_la_segunda():
    veces = []

    def read(test, client, *, model=None, context=""):
        veces.append(1)
        if len(veces) == 2:
            raise scoring.ReadingUnavailable("llegó sin: cumple")
        return _lectura()
    return read


def test_the_plan_warnings_follow_the_reading_after_an_edit(tmp_path, env):  # noqa: F811
    """Adjudicada la lectura, el plan deja de decir "hay que decidir cuál vale"."""
    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": False, "diferencias": "otro umbral"},
        check_reading=_chequeo_que_anota([], vaga=True),
    )
    record = _generar(store, engine, job_id)
    cid = next(d["id"] for d in record.strategy if d["estado"] == "se_prueba")
    decision = next(d for d in record.strategy if d["id"] == cid)
    assert any("no salió igual dos veces" in n for n in decision["advertencias"])

    asyncio.run(JobRunner(store, engine, None).edit_reading(record, cid, by="E. M.", sample=1))

    decision = next(d for d in store.get(job_id).strategy if d["id"] == cid)
    assert not any("no salió igual dos veces" in n for n in decision["advertencias"]), "la nota vieja se va"
    assert any("decidir a ojo" in n for n in decision["advertencias"]), "y la objeción nueva queda a la vista"
    assert decision["revisar"] is True


def test_an_edit_does_not_overwrite_a_plan_approved_meanwhile(tmp_path, env):  # noqa: F811
    """El chequeo tarda segundos: si el plan se aprobó mientras tanto, la edición no lo pisa."""
    from lxp_eval.service.crypto import ServiceError
    from lxp_eval.service.ledger_decisions import approve_from_plan
    from tests.test_plan_ejecucion import APROBO

    caja = {}

    def check(test, reading, client, *, model=None, sobre=lec.SOBRE_MODELO):
        if "store" in caja:                      # mientras corre el chequeo, alguien aprueba
            approve_from_plan(caja["store"], caja["store"].get(caja["job_id"]), approved_by=APROBO)
        return {"vaga": False, "parcial_definido": True, "anclas_contables": True, "objeciones": [], "sobre": sobre}

    store, engine, job_id = _plan(
        tmp_path, frozen_readings=True,
        read_rule=lambda test, client, *, model=None, context="": _lectura(),
        compare_readings=lambda *a, **k: {"equivalentes": True, "diferencias": ""},
        check_reading=check,
    )
    record = _generar(store, engine, job_id)
    cid = next(d["id"] for d in record.strategy if d["estado"] == "se_prueba")
    caja.update(store=store, job_id=job_id)

    with pytest.raises(ServiceError):
        asyncio.run(JobRunner(store, engine, None).edit_reading(record, cid, {"cumple": "Tres."}, by="E. M."))

    guardado = store.get(job_id)
    assert guardado.approved_plan is not None, "la aprobación sigue ahí"
    assert guardado.approved_plan["lecturas"][cid]["lectura"]["cumple"] == "Cuatro o menos."
