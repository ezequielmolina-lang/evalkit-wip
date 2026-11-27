"""El arnés de la aceptación de la lectura congelada: se compromete antes, mide validez, y no enciende nada.

EL CASO. El 2026-11-27 se midió el ruido del juez sobre una muestra de
criterios elegida porque volcaban, y la baja que apareció después de un arreglo
no se pudo atribuir al arreglo: una muestra elegida por su resultado mejora
sola la vez siguiente. Y congelar la lectura hace que el juez se repita casi
por construcción, lo que medido por acuerdo parece un éxito aunque la lectura
congelada esté equivocada. Estas pruebas fijan lo que el arnés tiene que hacer
para que eso no pase: el conjunto reservado se sortea y se bloquea antes, el
crédito es la validez contra personas y no el test-retest, y nada se enciende
solo.

Los veredictos de acá son inventados. Lo que estas pruebas NO ven: si la
lectura congelada mejora de verdad el juicio. Eso lo dicen las etiquetas de
personas sobre el conjunto reservado, que todavía no existen.
"""
from __future__ import annotations

import pytest

from lxp_eval.evalkit import aceptacion as ac
from lxp_eval.evalkit.scoring import CUMPLE, NO_CUMPLE, NO_EVALUADO, PARCIAL

CRITERIOS = [f"X-{i:02d}" for i in range(1, 31)]


def _registro(**kw):
    kw.setdefault("seed", "semilla-de-prueba")
    kw.setdefault("noise_floor", 0.10)
    kw.setdefault("noise_floor_source", "medición inventada para la prueba")
    kw.setdefault("registered_at", "2026-11-27T00:00:00+00:00")
    return ac.preregister(CRITERIOS, **kw)


def _ciclo(criterios, desde=0):
    """Veredictos que recorren la escala, para que haya variación que medir."""
    return {c: ac.SCALE[(i + desde) % 3] for i, c in enumerate(criterios)}


# --- la medida -----------------------------------------------------------------


def test_alpha_reproduces_the_published_example():
    """Krippendorff (2011): cuatro observadores, doce unidades. Nominal 0,743; ordinal 0,815."""
    a = [1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None]
    b = [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3]
    c = [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None]
    d = [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None]
    units = [list(x) for x in zip(a, b, c, d)]
    assert ac.krippendorff_alpha(units, metric="nominal", order=[1, 2, 3, 4, 5]) == pytest.approx(0.743, abs=0.001)
    assert ac.krippendorff_alpha(units, metric="ordinal", order=[1, 2, 3, 4, 5]) == pytest.approx(0.815, abs=0.001)


def test_alpha_is_not_defined_when_nothing_varies():
    """Todos CUMPLE siempre: no hay desacuerdo esperado, y decir "1" sería afirmar lo que no se midió."""
    assert ac.krippendorff_alpha([[CUMPLE, CUMPLE]] * 5, order=ac.SCALE) is None


# --- el compromiso -------------------------------------------------------------


def test_the_held_out_set_is_drawn_by_a_seed_and_nothing_else():
    uno, otro = ac.split(CRITERIOS, seed="s1"), ac.split(list(reversed(CRITERIOS)), seed="s1")
    assert uno == otro, "el orden en que llegan los criterios no cambia el sorteo"
    assert len(uno["reservado"]) == 10 and len(uno["ajuste"]) == 20
    assert not set(uno["reservado"]) & set(uno["ajuste"])
    assert ac.split(CRITERIOS, seed="s2")["reservado"] != uno["reservado"]


def test_the_preregistration_is_written_once_and_cannot_be_edited(tmp_path):
    registro = _registro()
    path = ac.write_preregistration(tmp_path / "prereg.json", registro)
    assert ac.load_preregistration(path) == registro

    with pytest.raises(ac.AcceptanceError, match="no se reescribe"):
        ac.write_preregistration(path, _registro(noise_floor=0.01))

    texto = path.read_text(encoding="utf-8").replace('"piso_de_ruido": 0.1', '"piso_de_ruido": 0.01')
    path.write_text(texto, encoding="utf-8")
    with pytest.raises(ac.AcceptanceError, match="cambiada después"):
        ac.load_preregistration(path)


def test_the_noise_floor_has_to_come_from_a_measurement():
    with pytest.raises(ac.AcceptanceError, match="de qué medición"):
        _registro(noise_floor_source="  ")


def test_a_held_out_set_too_small_is_refused():
    with pytest.raises(ac.AcceptanceError, match="mínimo"):
        ac.preregister(CRITERIOS[:9], seed="s", noise_floor=0.1, noise_floor_source="m",
                       registered_at="2026-11-27T00:00:00+00:00")


def test_the_labeling_sheet_shows_nothing_of_the_judge():
    registro = _registro()
    planilla = ac.labeling_sheet(registro)
    assert [fila["criterio"] for fila in planilla] == registro["reservado"]
    assert all(set(fila) == {"criterio", "veredicto", "nota"} and fila["veredicto"] == "" for fila in planilla)


# --- la medición ---------------------------------------------------------------


def _medir(registro, **kw):
    held = registro["reservado"]
    personas = kw.pop("humans", [_ciclo(held), _ciclo(held)])
    return ac.evaluate(registro, humans=personas, **kw)


def test_validity_against_people_is_what_counts(tmp_path):
    registro = _registro()
    held = registro["reservado"]
    personas = _ciclo(held)
    antes = _ciclo(held, desde=1)          # el juez con la lectura en vivo: corrido un paso
    despues = dict(personas)               # con la lectura congelada: coincide con las personas

    informe = _medir(registro, before=antes, after=despues)

    assert list(informe)[:3] == ["hash_registro", "criterios_reservados", "acuerdo_entre_personas"], \
        "el techo va primero"
    assert informe["acuerdo_entre_personas"] == pytest.approx(1.0)
    assert informe["validez"][ac.ARM_AFTER] == pytest.approx(1.0)
    assert informe["validez"][ac.ARM_BEFORE] < informe["validez"][ac.ARM_AFTER]
    assert informe["cumple_la_regla"] is True
    assert informe["cobertura"] == {ac.ARM_BEFORE: "10 de 10", ac.ARM_AFTER: "10 de 10"}
    assert len(informe["por_criterio"]) == 10


def test_a_sharper_judge_that_is_wrong_gets_no_credit():
    """Se repite perfecto y no coincide con las personas: el test-retest sube, la validez no."""
    registro = _registro()
    held = registro["reservado"]
    antes = _ciclo(held, desde=1)
    equivocado = _ciclo(held, desde=1)     # igual de lejos de las personas que antes

    informe = _medir(
        registro, before=antes, after=equivocado,
        retest={ac.ARM_BEFORE: [_ciclo(held, desde=1), _ciclo(held, desde=2)],
                ac.ARM_AFTER: [equivocado, equivocado, equivocado]},
    )

    assert informe["test_retest_no_es_credito"][ac.ARM_AFTER] == pytest.approx(1.0)
    assert informe["test_retest_no_es_credito"][ac.ARM_AFTER] > informe["test_retest_no_es_credito"][ac.ARM_BEFORE]
    assert informe["validez"]["diferencia"] == pytest.approx(0.0)
    assert informe["cumple_la_regla"] is False
    assert any("no se distingue de volver a juzgar" in m for m in informe["motivos"])


def test_a_gain_bought_by_answering_less_is_refused():
    registro = _registro()
    held = registro["reservado"]
    personas = _ciclo(held)
    antes = _ciclo(held, desde=1)
    despues = dict(personas)
    despues[held[0]] = NO_EVALUADO         # coincide en todo lo que contesta, pero contesta menos

    informe = _medir(registro, before=antes, after=despues)
    assert informe["cobertura"][ac.ARM_AFTER] == "9 de 10"
    assert informe["cumple_la_regla"] is False
    assert any("la cobertura cae" in m for m in informe["motivos"])


def test_when_the_measure_cannot_be_computed_the_answer_is_not_yes():
    registro = _registro()
    held = registro["reservado"]
    todos = {c: CUMPLE for c in held}
    informe = ac.evaluate(registro, humans=[todos, todos], before=todos, after=todos)
    assert informe["validez"]["diferencia"] is None and informe["cumple_la_regla"] is None


def test_what_was_committed_beforehand_is_enforced():
    registro = _registro()
    held = registro["reservado"]
    bien = dict(humans=[_ciclo(held), _ciclo(held)], before=_ciclo(held), after=_ciclo(held))

    with pytest.raises(ac.AcceptanceError, match="una sola no hay techo"):
        ac.evaluate(registro, **{**bien, "humans": [_ciclo(held)]})
    with pytest.raises(ac.AcceptanceError, match="ya se midió"):
        ac.evaluate(registro, **bien, already_measured=True)
    with pytest.raises(ac.AcceptanceError, match="rondas de reescritura"):
        ac.evaluate(registro, **bien, rewrite_rounds=3)
    incompleta = _ciclo(held)
    incompleta.pop(held[0])
    with pytest.raises(ac.AcceptanceError, match="le faltan 1 de 10"):
        ac.evaluate(registro, **{**bien, "humans": [_ciclo(held), incompleta]})
    otro = dict(registro, reservado=held[:-1])
    with pytest.raises(ac.AcceptanceError, match="no es el registrado"):
        ac.evaluate(otro, **bien)


def test_a_passing_acceptance_does_not_switch_the_contract_on():
    """El arnés informa. Encender el contrato es una decisión de su dueño, a mano."""
    from lxp_eval.service.jobs import Engine

    registro = _registro()
    held = registro["reservado"]
    informe = _medir(registro, before=_ciclo(held, desde=1), after=_ciclo(held))

    assert informe["cumple_la_regla"] is True
    assert Engine().frozen_readings is False
    assert "no enciende nada" in informe["nota"]
    import inspect

    assert "frozen_readings" not in inspect.getsource(ac).replace("`Engine.frozen_readings`", ""), \
        "el arnés no lee ni escribe el interruptor del contrato"
