"""El plan que una persona aprueba antes de que se gaste en correr.

EL CASO REAL (trabajo ec2f976187b243c2, 2026-11-25). El plan se aprobó en
automático y el informe dejó 20 de 28 criterios sin evaluar. Leyendo el informe
se pudo reconstruir qué había faltado; leyendo el plan, no: eran pasos de
prueba sin una sola imagen de la plataforma.

Lo que se fija acá, pieza por pieza:

  * el PLANIFICADOR decide el estado de cada criterio, y el código no le cree
    una pantalla que el recorrido no registró;
  * el MUESTREO propone n, temas y variantes, y el código cuenta las celdas en
    vez de creerle el número;
  * los dos CIERRAN JUNTOS: una objeción del muestreo cambia el plan;
  * el DOCUMENTO muestra las capturas, las tres cubetas y lo que quedó sin
    recorrer, y un criterio que no se puede evaluar nunca aparece como un cero.

Las capturas de estas pruebas se crean acá (un PNG mínimo válido), no vienen de
un archivo guardado en el repo.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from lxp_eval.evalkit import planner as pl
from lxp_eval.evalkit import sampling as sm

# Un PNG de 1x1, válido, escrito acá.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


def _perfil():
    return {
        "direccion": "https://practica.ejemplo.test/",
        "entrada": "Tocar el nombre guardado.",
        "roles": ["docente", "estudiante"],
        "partes": [
            {"id": "P1", "nombre": "Pregunta de práctica", "rol": "estudiante",
             "como_llegar": "Temas, Álgebra, Monomios", "que_muestra": "Una pregunta con cuatro opciones",
             "captura": "p1.png", "sha256": "a" * 64, "texto": "p1.txt",
             "url": "https://practica.ejemplo.test/"},
            {"id": "P2", "nombre": "Después de un error", "rol": "estudiante",
             "como_llegar": "Responder mal", "que_muestra": "Lo que el tutor cree que hizo el estudiante",
             "captura": "p2.png", "sha256": "b" * 64, "texto": "p2.txt"},
            {"id": "P3", "nombre": "Clase en vivo", "rol": "docente",
             "como_llegar": "Vista docente, Clase en vivo", "que_muestra": "Los estudiantes de la clase",
             "captura": "", "sha256": ""},
        ],
        "comportamientos": [
            {"id": "C1", "nombre": "cinco errores seguidos", "que_hice": "respondí mal cinco veces",
             "que_observe": "el nivel no subió", "conclusion": "avanzar depende de acertar",
             "captura": "c1.png"},
        ],
        "experimentos": [
            {"id": "regla_de_avance", "pregunta": "¿De qué depende avanzar?",
             "oraculo": "Depende del dominio si no deja avanzar.", "corridas": 1,
             "observaciones": ["C1"], "estado": "hecho"},
        ],
        "entradas": [
            {"texto": "Temas", "tipo": "menu", "estado": "recorrida", "partes": ["P1"], "motivo": ""},
            {"texto": "Informes", "tipo": "menu", "estado": "sin_cubrir", "partes": [], "motivo": ""},
        ],
        "cobertura": {
            "entradas": 2, "recorridas": 1, "no_disponibles": [], "sin_cubrir": ["Informes"],
            "roles_vistos": [], "roles_recorridos": [], "por_confirmar": [],
            "partes_sin_captura": ["Clase en vivo"], "estados_distintos": 2, "notas": [],
            "completo": False,
        },
        "no_encontrado": [], "documentado_sin_verificar": [], "sin_explorar": [],
    }


def _criterios():
    return [
        {"id": "D4-02", "area": "D4", "name": "Identifica el error",
         "verification": "Responder mal y leer la corrección", "passCriteria": "Nombra el error",
         "observability": "OBSERVABLE_EN_PLATAFORMA"},
        {"id": "D7-03", "area": "D7", "name": "El panel señala quién está en riesgo",
         "verification": "Abrir el panel docente", "passCriteria": "Marca estudiantes",
         "observability": "OBSERVABLE_EN_PLATAFORMA"},
        {"id": "D5-01", "area": "D5", "name": "Registro de errores documentado",
         "verification": "Pedir el registro", "passCriteria": "Existe",
         "observability": "REQUIERE_DOCUMENTACION",
         "evidenciaRequerida": "El registro de errores conceptuales por pregunta"},
        {"id": "D7-07", "area": "D7", "name": "El contenido complementa al docente",
         "verification": "Relevamiento", "passCriteria": "Complementa",
         "observability": "REQUIERE_ESTUDIO", "observabilityReason": "exige un relevamiento con docentes"},
        {"id": "D4-01", "area": "D4", "name": "Latencia menor a un segundo",
         "verification": "Medir", "passCriteria": "<1s",
         "observability": "OBSERVABLE_EN_PLATAFORMA"},
    ]


# --- 1. el planificador ------------------------------------------------------


def test_a_screen_the_walk_never_recorded_cannot_back_a_test():
    """La misma regla del juez, un paso antes: sólo se cita lo que se recibió."""
    decisiones = pl.validate_strategy(
        [
            {"id": "D4-02", "estado": "se_prueba", "partes": ["P2", "P9"],
             "que_se_prueba": "Responder mal y leer la hipótesis", "motivo": "se vio"},
            # Dice que se prueba y no puede señalar dónde.
            {"id": "D7-03", "estado": "se_prueba", "partes": ["P44"], "motivo": "debe haber un panel"},
        ],
        _criterios(), _perfil(),
    )
    por_id = {d["id"]: d for d in decisiones}

    assert por_id["D4-02"]["estado"] == "se_prueba"
    assert por_id["D4-02"]["partes"] == ["P2"], "P9 no existe: se descarta"
    assert any("P9" in a for a in por_id["D4-02"]["advertencias"])

    assert por_id["D7-03"]["estado"] == "falta_recorrer", \
        "un 'se prueba' sin pantalla es el plan a ciegas de siempre"
    assert por_id["D7-03"]["cubeta"] == "indefinido"


def test_a_criterion_without_a_decision_does_not_vanish():
    decisiones = pl.validate_strategy([], _criterios(), _perfil())
    por_id = {d["id"]: d for d in decisiones}

    assert [d["id"] for d in decisiones] == [c["id"] for c in _criterios()]
    assert por_id["D5-01"]["estado"] == "necesita_documento"
    assert "registro de errores" in por_id["D5-01"]["documento"].lower()
    assert por_id["D7-07"]["estado"] == "necesita_estudio"
    # Observable según el marco, pero nadie dijo dónde: indefinido, no "se prueba".
    assert por_id["D4-02"]["estado"] == "falta_recorrer"


def test_an_unknown_state_or_criterion_from_the_model_is_ignored():
    decisiones = pl.validate_strategy(
        [{"id": "INVENTADO", "estado": "se_prueba", "partes": ["P1"], "motivo": "x"},
         {"id": "D4-01", "estado": "aprobado", "motivo": "x"}],
        _criterios(), _perfil(),
    )
    assert "INVENTADO" not in {d["id"] for d in decisiones}
    assert next(d for d in decisiones if d["id"] == "D4-01")["estado"] == "falta_recorrer"


class _Cliente:
    """Devuelve lo que se le cargue para cada herramienta y guarda lo que recibió."""

    def __init__(self, respuestas):
        self.respuestas, self.llamadas = respuestas, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.llamadas.append(kw)
        nombre = kw["tool_choice"]["name"]
        return SimpleNamespace(content=[
            SimpleNamespace(type="tool_use", name=nombre, input=self.respuestas[nombre])
        ])


def test_the_planner_decides_with_what_the_walk_saw():
    cliente = _Cliente({pl.STRATEGY_TOOL_NAME: {"decisiones": [
        {"id": "D4-02", "estado": "se_prueba", "partes": ["P2"], "motivo": "se vio",
         "que_se_prueba": "Responder mal"},
        {"id": "D4-01", "estado": "no_se_puede_medir", "motivo": "la pantalla no muestra el tiempo"},
    ]}})
    decisiones = pl.evaluate_strategy(
        _criterios(), profile=_perfil(), profile_text="P2. Después de un error",
        client=cliente, model="m", documents=["Guía del proveedor"],
    )

    enviado = " ".join(b["text"] for b in cliente.llamadas[0]["messages"][0]["content"])
    assert "P2. Después de un error" in enviado, "el planificador decide con el recorrido delante"
    assert "Guía del proveedor" in enviado
    assert "D7-03" in enviado, "todos los criterios se le presentan"
    por_id = {d["id"]: d for d in decisiones}
    assert por_id["D4-01"]["cubeta"] == "no_se_puede"
    assert por_id["D5-01"]["cubeta"] == "documento", "lo que el modelo no decidió hereda del marco"


def test_the_planner_is_not_called_for_nothing():
    cliente = _Cliente({})
    assert pl.evaluate_strategy([], profile=None, profile_text="", client=cliente, model="m") == []
    assert cliente.llamadas == []


# --- 2. el muestreo ----------------------------------------------------------


def test_n_is_counted_from_the_cells_not_taken_from_the_model():
    matriz = sm.validate_matrix(
        [{"id": "D4-02", "sensible_al_tema": True, "n": 40, "confirmado": True, "motivo": "varía",
          "celdas": [
              {"tema": "Monomios", "variante": "error de signo"},
              {"tema": "Potencias", "variante": "error de orden de operaciones"},
              {"tema": "Ecuaciones", "variante": "error de despeje"},
              # La misma variante con otro tema no es otra situación.
              {"tema": "Fracciones", "variante": "Error de signo"},
          ]}],
        ["D4-02"], "Monomios Potencias Ecuaciones Fracciones",
    )
    fila = matriz["D4-02"]
    assert fila["n"] == 3
    assert [c["variante"] for c in fila["celdas"]] == [
        "error de signo", "error de orden de operaciones", "error de despeje",
    ]


def test_a_topic_nobody_saw_is_flagged_for_the_person_approving():
    matriz = sm.validate_matrix(
        [{"id": "D4-02", "sensible_al_tema": True, "confirmado": True, "motivo": "",
          "celdas": [{"tema": "Monomios", "variante": "a"}, {"tema": "Geometría analítica", "variante": "b"}]}],
        ["D4-02"], "Temas vistos: Monomios, Potencias",
    )
    celdas = matriz["D4-02"]["celdas"]
    assert "tema_no_visto" not in celdas[0]
    assert celdas[1]["tema_no_visto"] is True


def test_topic_invariant_criteria_are_not_repeated_across_topics():
    matriz = sm.validate_matrix(
        [{"id": "S1", "sensible_al_tema": False, "confirmado": True, "motivo": "",
          "celdas": [{"tema": "", "variante": f"v{i}"} for i in range(6)]}],
        ["S1"],
    )
    assert matriz["S1"]["n"] == sm.MAX_INVARIANT
    assert any("recortó" in a for a in matriz["S1"]["advertencias"])


def test_a_sensitive_criterion_with_too_few_probes_says_what_that_means():
    matriz = sm.validate_matrix(
        [{"id": "D4-02", "sensible_al_tema": True, "confirmado": True, "motivo": "",
          "celdas": [{"tema": "Monomios", "variante": "a"}]}],
        ["D4-02"],
    )
    assert any("vale para lo probado" in a for a in matriz["D4-02"]["advertencias"])


def test_a_criterion_the_sampler_skipped_still_gets_one_probe_and_a_note():
    matriz = sm.validate_matrix([{"id": "OTRO", "celdas": []}], ["D4-02"])
    assert set(matriz) == {"D4-02"}, "un criterio que no se pidió no entra"
    assert matriz["D4-02"]["n"] == 1
    assert matriz["D4-02"]["advertencias"]


def test_sampling_only_sees_what_the_planner_chose():
    cliente = _Cliente({sm.SAMPLING_TOOL_NAME: {"filas": [
        {"id": "D4-02", "sensible_al_tema": True, "confirmado": True, "motivo": "",
         "celdas": [{"tema": "Monomios", "variante": "a"}]},
    ]}})
    elegidos = [{"id": "D4-02", "nombre": "Identifica el error", "que_se_prueba": "x", "partes": ["P2"]}]
    matriz = sm.propose_sampling(elegidos, profile_text="Monomios", client=cliente, model="m")

    enviado = " ".join(b["text"] for b in cliente.llamadas[0]["messages"][0]["content"])
    assert "D4-02" in enviado and "D7-03" not in enviado
    assert matriz["D4-02"]["n"] == 1
    assert sm.propose_sampling([], profile_text="", client=cliente, model="m") == {}
    assert len(cliente.llamadas) == 1, "sin criterios elegidos no se llama al modelo"


# --- 3. el cierre conjunto ---------------------------------------------------


def _decisiones():
    return pl.validate_strategy(
        [
            {"id": "D4-02", "estado": "se_prueba", "partes": ["P2"], "motivo": "se vio",
             "que_se_prueba": "Responder mal y leer la hipótesis"},
            {"id": "D7-03", "estado": "se_prueba", "partes": ["P3"], "motivo": "hay un panel",
             "que_se_prueba": "Abrir la clase en vivo"},
            {"id": "D4-01", "estado": "no_se_puede_medir", "motivo": "la pantalla no muestra el tiempo"},
        ],
        _criterios(), _perfil(),
    )


def test_an_objection_from_sampling_changes_the_plan():
    matriz = sm.validate_matrix(
        [
            {"id": "D4-02", "sensible_al_tema": True, "confirmado": True, "motivo": "varía por tema",
             "celdas": [{"tema": "Monomios", "variante": "a"}, {"tema": "Potencias", "variante": "b"},
                        {"tema": "Ecuaciones", "variante": "c"}]},
            {"id": "D7-03", "sensible_al_tema": False, "confirmado": False,
             "objecion": "el panel sólo tiene estudiantes de demostración", "motivo": "",
             "celdas": [{"tema": "", "variante": "a"}, {"tema": "", "variante": "b"}]},
        ],
        ["D4-02", "D7-03"],
    )
    final = {d["id"]: d for d in pl.reconcile(_decisiones(), matriz)}

    assert final["D4-02"]["n"] == 3 and final["D4-02"]["revisar"] is False
    # El muestreo no saca el criterio del plan (eso es del planificador), pero
    # su objeción queda escrita y el criterio vuelve a una sola prueba.
    assert final["D7-03"]["estado"] == "se_prueba"
    assert final["D7-03"]["n"] == 1 and final["D7-03"]["revisar"] is True
    assert any("demostración" in a for a in final["D7-03"]["advertencias"])
    # Lo que no se prueba no lleva muestreo.
    assert final["D4-01"]["n"] == 0 and final["D4-01"]["celdas"] == []


# --- 4. el plan y su documento -----------------------------------------------


def _plan():
    matriz = sm.validate_matrix(
        [{"id": "D4-02", "sensible_al_tema": True, "confirmado": True, "motivo": "varía por tema",
          "celdas": [{"tema": "Monomios", "variante": "error de signo"},
                     {"tema": "Potencias", "variante": "orden de operaciones"},
                     {"tema": "Ecuaciones", "variante": "despeje"}]}],
        ["D4-02", "D7-03"],
    )
    return pl.build_plan(
        platform="practica.ejemplo.test", url="https://practica.ejemplo.test/",
        objective="Evaluar la plataforma entera", profile=_perfil(),
        decisions=pl.reconcile(_decisiones(), matriz),
        documents=["Guía del proveedor"], spend_usd=9.5,
    )


def test_the_plan_carries_the_three_buckets_and_what_they_cost():
    plan = _plan()
    c = plan["conteos"]
    assert (c["se_prueba"], c["no_se_puede"], c["documentos"]) == (2, 2, 1)
    assert c["pruebas"] == 4, "3 de D4-02 más 1 de D7-03"
    assert c["criterios"] == 5
    assert plan["reglas"]["costo_estimado_usd"] == pytest.approx(4 * 0.40 + 2 * 0.30)
    assert plan["gasto_hasta_ahora_usd"] == 9.5
    assert plan["documentos_necesarios"][0]["criterios"] == ["D5-01"]


def test_every_surface_is_a_row_including_the_one_nobody_walked():
    filas = {s["nombre"]: s for s in _plan()["superficies"]}
    assert filas["Después de un error"]["estado"] == "se_prueba"
    assert filas["Después de un error"]["pruebas"] == 3
    assert filas["Pregunta de práctica"]["estado"] == "sin_criterios"
    # La entrada que se vio y no se recorrió es una fila, no una nota al pie.
    assert filas["Informes"]["estado"] == "indefinido"


def test_the_document_shows_the_captures_and_never_a_score(tmp_path):
    for nombre in ("p1.png", "p2.png", "c1.png"):
        (tmp_path / nombre).write_bytes(PNG)
    doc = pl.render_plan_html(_plan(), tmp_path)

    assert doc.count("data:image/") >= 3, "las capturas del recorrido van adentro del documento"
    assert "sin captura" in doc, "una pantalla sin captura se ve como tal, no se esconde"
    assert "El recorrido NO está completo" in doc and "Informes" in doc
    assert "Nada se ha ejecutado todavía" in doc
    assert "son hallazgos, no ceros" in doc
    # Ningún puntaje ni porcentaje: todavía no se midió nada.
    assert "%" not in doc.split("<main>")[1]
    # Las tres cubetas y los documentos, a la vista en la cabecera.
    for texto in ("criterios se prueban", "no se pueden evaluar", "indefinidos", "documentos necesarios"):
        assert texto in doc
    # El muestreo de cada criterio, con sus temas.
    assert "Monomios" in doc and "orden de operaciones" in doc
    # El oráculo del experimento, escrito antes.
    assert "escrito antes de correr" in doc


def test_the_document_survives_missing_capture_files(tmp_path):
    doc = pl.render_plan_html(_plan(), tmp_path)   # ninguna captura en disco
    assert "sin captura" in doc and "<html" in doc


def test_a_replan_shows_only_what_changed():
    viejo = _plan()
    nuevo = _plan()
    nuevo["version"] = 2
    for c in nuevo["criterios"]:
        if c["id"] == "D4-01":
            c["estado"] = "se_prueba"
        if c["id"] == "D4-02":
            c["n"] = 5
    nuevo["superficies"].append({"nombre": "Informes", "captura": "p9.png"})

    d = pl.diff_plans(viejo, nuevo)
    assert d["criterios_que_cambiaron_de_estado"] == [
        {"id": "D4-01", "antes": "no_se_puede_medir", "ahora": "se_prueba"}
    ]
    assert d["criterios_con_otro_n"] == [{"id": "D4-02", "antes": 3, "ahora": 5}]
    assert d["superficies_nuevas"] == ["Informes"]
    assert pl.diff_plans(None, nuevo) == {"primera_version": True}


def test_a_correction_becomes_a_place_the_walk_must_visit():
    entradas = pl.corrections_as_entries([
        {"tipo": "existe", "superficie": "Informes", "url": "/informes", "nota": "está en el menú del docente"},
        {"tipo": "reps", "criterio": "D4-02", "n": 5},
        "ruido",
    ])
    assert entradas == [{
        "texto": "Informes", "tipo": "menu", "estado": "pendiente",
        "url": "/informes", "nota": "está en el menú del docente",
    }]
