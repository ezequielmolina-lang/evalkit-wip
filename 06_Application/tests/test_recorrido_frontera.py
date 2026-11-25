"""El recorrido no decide solo cuándo terminó: lo decide una lista externa.

EL CASO REAL (trabajo ec2f976187b243c2, 2026-11-25). El explorador escribió en
sus notas que el menú tenía "Temas", "Vista docente", "Cómo funciona" y "Cambiar
estudiante". Que hubiera entrado a cada una dependía de que él mismo se
acordara. El informe de esa corrida dejó 20 de 28 criterios sin evaluar, y para
los de la vista docente no había forma de saber, leyendo el perfil, si esa parte
se había recorrido entera, a medias o nada.

Lo que se fija acá:

  1. las entradas salen del ÁRBOL de cada pantalla registrada, las haya
     mencionado el explorador o no;
  2. un cambio de rol se recorre primero y es obligatorio;
  3. lo que no se recorrió queda nombrado en el autochequeo, nunca en silencio;
  4. una parte registrada lleva la captura que tomó el código en ese instante;
  5. lo registrado al pasar sobrevive a una exploración que no llegó a informar;
  6. la memoria entre sesiones se averigua con DOS sesiones, no con una.

Los textos de página de este archivo se escriben acá mismo, con la forma que
devuelve el navegador real (ver PlaywrightDriver._page_text).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lxp_eval.browser_agents.agent_driver import AgentDriver, explore_tools
from lxp_eval.browser_agents.claude_browser_agent import AgentRunResult, ClaudeBrowserAgent
from lxp_eval.evalkit import frontier as fr
from lxp_eval.evalkit.recon import explore_platform, render_profile

RAIZ = "https://practica.ejemplo.test/"

ARBOL_ESTUDIANTE = """- banner:
  - text: Matemática 8.º · práctica con tutor
  - navigation:
    - link "Temas":
      - /url: "#"
    - link "Vista docente":
      - /url: /docente
    - link "Cómo funciona":
      - /url: /como-funciona
    - link "Cambiar estudiante":
      - /url: "#"
- main:
  - heading "8.º grado"
  - button "Operaciones y expresiones algebraicas 6 temas ›"
  - button "A 58"
- paragraph: Tu docente ve tus respuestas.
"""

ARBOL_DOCENTE = """- banner:
  - navigation:
    - link "Resumen":
      - /url: /docente
    - link "Clase en vivo":
      - /url: /docente/clase
    - link "Práctica":
      - /url: /
- main:
  - heading "Vista docente"
  - combobox "Subtema de hoy"
  - button "Borrar datos de la demo"
"""


# --- 1. la frontera sale del árbol -------------------------------------------


def test_the_menu_is_read_from_the_tree_not_from_what_the_agent_remembers():
    entradas = fr.enumerate_entries(ARBOL_ESTUDIANTE)
    assert [e["texto"] for e in entradas] == [
        "Temas", "Vista docente", "Cómo funciona", "Cambiar estudiante",
    ]
    por_texto = {e["texto"]: e for e in entradas}
    assert por_texto["Vista docente"]["tipo"] == "rol", "un cambio de rol se marca"
    assert por_texto["Vista docente"]["url"] == "/docente"
    assert por_texto["Temas"]["tipo"] == "menu"


def test_a_button_inside_the_content_is_not_an_entry():
    """Si cada opción de cada ejercicio fuera una entrada, la frontera no se
    vaciaría nunca y dejaría de decir algo."""
    textos = [e["texto"] for e in fr.enumerate_entries(ARBOL_ESTUDIANTE)]
    assert "A 58" not in textos
    assert not any("Operaciones" in t for t in textos)


def test_a_page_that_is_not_a_tree_yields_no_entries():
    assert fr.enumerate_entries("Bienvenido. Temas Vista docente Cómo funciona") == []


def test_a_state_is_what_can_be_touched_not_what_it_says():
    turno_1 = '- main:\n  - paragraph: Probá con 2 por 3\n  - textbox "Escribe tu respuesta"\n  - button "Enviar"\n'
    turno_2 = '- main:\n  - paragraph: Muy bien, ahora restá\n  - textbox "Escribe tu respuesta"\n  - button "Enviar"\n'
    assert fr.state_signature(turno_1, RAIZ) == fr.state_signature(turno_2, RAIZ), \
        "dos turnos del mismo chat son un estado"
    assert fr.state_signature(ARBOL_ESTUDIANTE, RAIZ) != fr.state_signature(
        ARBOL_DOCENTE, RAIZ + "docente"
    ), "la vista del estudiante y la del docente no se pueden fundir"
    # El número de un curso en la dirección no hace un estado nuevo.
    assert fr.state_signature(turno_1, RAIZ + "curso/12") == fr.state_signature(
        turno_1, RAIZ + "curso/97"
    )


def _parte(pid, nombre, *, arbol="", entrada="", url=RAIZ, captura=True, rol="estudiante"):
    return {
        "id": pid, "nombre": nombre, "rol": rol, "entrada": entrada, "url": url,
        "captura": f"{pid}.png" if captura else "",
        "texto": f"{pid}.txt" if arbol else "",
        "entradas_en_pantalla": fr.enumerate_entries(arbol),
        "firma": fr.state_signature(arbol, url),
    }


def test_an_entry_is_walked_only_if_a_captured_part_is_reached_through_it():
    partes = [
        _parte("P1", "Pantalla principal", arbol=ARBOL_ESTUDIANTE),
        _parte("P2", "Temas", entrada="temas"),
        # Sin `entrada` declarada, pero su dirección es la del enlace.
        _parte("P3", "Explicación", url=RAIZ + "como-funciona"),
    ]
    frontera = {e["texto"]: e for e in fr.build_frontier(partes)}

    assert frontera["Temas"]["estado"] == "recorrida"
    assert frontera["Temas"]["partes"] == ["P2"]
    assert frontera["Cómo funciona"]["estado"] == "recorrida"
    assert frontera["Vista docente"]["estado"] == "sin_cubrir"
    # No se recorre, pero no se olvida: queda con su motivo.
    assert frontera["Cambiar estudiante"]["estado"] == "no_disponible"
    assert "a propósito" in frontera["Cambiar estudiante"]["motivo"]


def test_a_part_without_a_capture_does_not_count_as_walked():
    partes = [
        _parte("P1", "Pantalla principal", arbol=ARBOL_ESTUDIANTE),
        _parte("P2", "Vista docente", entrada="Vista docente", captura=False),
    ]
    frontera = {e["texto"]: e for e in fr.build_frontier(partes)}
    assert frontera["Vista docente"]["estado"] == "sin_cubrir"


def test_the_explorer_can_add_entries_and_reasons_but_never_remove_one():
    partes = [_parte("P1", "Pantalla principal", arbol=ARBOL_ESTUDIANTE)]
    declarado = [
        {"texto": "Vista docente", "tipo": "rol", "estado": "no_disponible",
         "motivo": "pide una cuenta de docente"},
        # Dice que la recorrió, y no hay ninguna parte que lo respalde.
        {"texto": "Cómo funciona", "tipo": "menu", "estado": "recorrida"},
        {"texto": "Ayuda", "tipo": "menu", "estado": "pendiente"},
        # Un "no disponible" sin motivo no vale.
        {"texto": "Temas", "tipo": "menu", "estado": "no_disponible", "motivo": ""},
    ]
    frontera = {e["texto"]: e for e in fr.build_frontier(partes, declarado)}

    assert frontera["Vista docente"]["estado"] == "no_disponible"
    assert frontera["Cómo funciona"]["estado"] == "sin_cubrir", \
        "que el explorador diga 'recorrida' no alcanza: hace falta la parte"
    assert frontera["Ayuda"]["estado"] == "sin_cubrir"
    assert frontera["Temas"]["estado"] == "sin_cubrir"

    chequeo = fr.coverage(frontera.values(), partes, tree_seen=True)
    assert chequeo["completo"] is False
    assert set(chequeo["sin_cubrir"]) == {"Temas", "Cómo funciona", "Ayuda"}
    assert chequeo["por_confirmar"] == ["Vista docente"], \
        "un rol dado por no disponible lo tiene que ver una persona"


def test_roles_go_first():
    partes = [_parte("P1", "Pantalla principal", arbol=ARBOL_ESTUDIANTE)]
    pendientes = fr.pending(fr.build_frontier(partes))
    assert pendientes[0]["texto"] == "Vista docente"


def test_without_any_tree_the_check_does_not_claim_to_be_complete():
    partes = [_parte("P1", "Pantalla principal")]
    chequeo = fr.coverage(fr.build_frontier(partes), partes, tree_seen=False)
    assert chequeo["completo"] is False
    assert chequeo["notas"], "tiene que decir por qué no puede afirmar cobertura"


# --- 2. el recorrido vacía la frontera, los roles primero ---------------------


class _Explorador:
    """Un explorador guionado: contesta según lo que se le encarga."""

    def __init__(self, *, entra_al_rol=True, segunda_sesion=True):
        self.encargos: list[str] = []
        self.sesiones_nuevas = 0
        self._entra_al_rol = entra_al_rol
        if not segunda_sesion:
            self.new_session = None

    async def new_session(self) -> bool:
        self.sesiones_nuevas += 1
        return True

    async def explore(self, task: str, **kw) -> dict:
        self.encargos.append(task)
        n = len(self.encargos)
        if "INVENTARIO" in task:
            return {
                "entrada": "Tocar el nombre guardado.",
                "partes": [_sin_id(_parte("", "Pantalla principal", arbol=ARBOL_ESTUDIANTE))
                           | {"captura": "principal.png"}],
            }
        if "DENTRO DE UNA ACTIVIDAD" in task:
            return {"partes": [_sin_id(_parte("", "Después de un error")) | {"captura": "error.png"}]}
        if "RECORRÉ OTRO ROL" in task:
            if not self._entra_al_rol:
                return {"no_encontrado": ["no encontré cómo entrar"]}
            return {"partes": [
                _sin_id(_parte("", "Vista docente: resumen", arbol=ARBOL_DOCENTE,
                               entrada="Vista docente", url=RAIZ + "docente", rol="docente"))
                | {"captura": "docente.png"},
                _sin_id(_parte("", "Clase en vivo", entrada="Clase en vivo",
                               url=RAIZ + "docente/clase", rol="docente"))
                | {"captura": "clase.png"},
            ]}
        if "RECORRÉ ESTAS ENTRADAS" in task:
            partes = []
            for texto in ("Temas", "Cómo funciona", "Práctica"):
                if f"«{texto}»" in task:
                    partes.append(
                        _sin_id(_parte("", f"Sección {texto}", entrada=texto))
                        | {"captura": f"{texto}.png"}
                    )
            return {"partes": partes} if partes else {"no_encontrado": ["nada"]}
        if "EXPERIMENTO" in task:
            return {"comportamientos": [{
                "nombre": f"observación {n}", "que_hice": "respondí",
                "que_observe": "el subtema quedó en nivel 1", "conclusion": "se conserva",
                "captura": f"exp{n}.png",
            }]}
        return {"no_encontrado": ["encargo desconocido"]}


def _sin_id(parte: dict) -> dict:
    return {k: v for k, v in parte.items() if k != "id"}


@pytest.mark.asyncio
async def test_the_teacher_view_is_entered_because_the_list_demands_it():
    agente = _Explorador()
    perfil = await explore_platform(agente, root_url=RAIZ)

    roles = [i for i, e in enumerate(agente.encargos) if "RECORRÉ OTRO ROL" in e]
    menu = [i for i, e in enumerate(agente.encargos) if "RECORRÉ ESTAS ENTRADAS" in e]
    assert roles, "nadie mandó a recorrer la vista docente"
    assert "«Vista docente»" in agente.encargos[roles[0]]
    assert roles[0] < menu[0], "el cambio de rol va antes que el resto del menú"

    assert "docente" in perfil["roles"] and "estudiante" in perfil["roles"]
    chequeo = perfil["cobertura"]
    assert chequeo["sin_cubrir"] == []
    assert chequeo["completo"] is True
    assert chequeo["roles_recorridos"] == ["Vista docente"]
    # Lo que apareció DENTRO del rol (su propio menú) también entró en la lista.
    assert {e["texto"] for e in perfil["entradas"]} >= {"Resumen", "Clase en vivo", "Práctica"}
    # Y lo que no se recorre a propósito quedó anotado, no olvidado.
    assert any(u["texto"] == "Cambiar estudiante" for u in chequeo["no_disponibles"])
    # Cada parte tiene un id con el que el plan la puede citar.
    assert [p["id"] for p in perfil["partes"]] == [f"P{i}" for i in range(1, len(perfil["partes"]) + 1)]


@pytest.mark.asyncio
async def test_a_role_nobody_entered_is_named_not_silently_dropped():
    agente = _Explorador(entra_al_rol=False)
    perfil = await explore_platform(agente, root_url=RAIZ)

    chequeo = perfil["cobertura"]
    assert "Vista docente" in chequeo["sin_cubrir"]
    assert chequeo["completo"] is False
    assert chequeo["roles_recorridos"] == []
    # Y no se insiste para siempre sobre la misma entrada: una vez.
    assert sum("«Vista docente»" in e and "RECORRÉ OTRO ROL" in e for e in agente.encargos) == 1
    # Quien redacta el plan lo lee.
    assert "Vista docente" in render_profile(perfil)
    assert "NO se recorrieron" in render_profile(perfil)


# --- 3. los experimentos tienen el oráculo escrito antes ----------------------


@pytest.mark.asyncio
async def test_memory_is_found_out_with_two_sessions():
    agente = _Explorador()
    perfil = await explore_platform(agente, root_url=RAIZ)

    memoria = next(e for e in perfil["experimentos"] if e["id"] == "memoria_entre_sesiones")
    assert agente.sesiones_nuevas == 1, "sin segunda sesión no hay contraste"
    assert memoria["estado"] == "hecho"
    assert len(memoria["observaciones"]) == 2
    assert memoria["oraculo"], "qué significaría cada resultado se escribe antes de correr"
    assert memoria["corridas"] == 1, "una corrida es una corrida: se dice"

    segunda = next(e for e in agente.encargos if "SEGUNDA sesión" in e)
    assert "HUELLA DE LA PRIMERA SESIÓN" in segunda
    assert "nivel 1" in segunda, "la segunda sesión tiene que saber qué buscar"
    assert "ya está abierta" in segunda, "volver a navegar borraría lo que se quiere mirar"

    avance = next(e for e in perfil["experimentos"] if e["id"] == "regla_de_avance")
    assert avance["observaciones"] and "MAL" in next(
        e for e in agente.encargos if "regla de avance" in e
    )


@pytest.mark.asyncio
async def test_without_a_second_session_the_experiment_says_so():
    agente = _Explorador(segunda_sesion=False)
    perfil = await explore_platform(agente, root_url=RAIZ)
    memoria = next(e for e in perfil["experimentos"] if e["id"] == "memoria_entre_sesiones")
    assert memoria["estado"].startswith("no se pudo")
    assert not any("SEGUNDA sesión" in e for e in agente.encargos)


# --- 4. una parte registrada lleva la captura que tomó el código --------------


class _Navegador:
    def __init__(self, tmp: Path, arbol: str = ARBOL_ESTUDIANTE):
        self.tmp, self.arbol, self.capturas = tmp, arbol, 0
        self.state = SimpleNamespace(screenshots_taken=[], actions_log=[], log=lambda *a, **k: None)

    async def screenshot(self, label="s", *, full_page=True) -> Path:
        self.capturas += 1
        p = self.tmp / f"{label}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + label.encode())
        return p

    async def read_page(self, max_chars=None) -> str:
        return self.arbol

    async def get_url(self) -> str:
        return RAIZ


@pytest.fixture
def agente_de(monkeypatch):
    """Un ClaudeBrowserAgent real sobre un navegador de prueba, sin llamar al modelo."""
    from lxp_eval.config import CONFIG

    monkeypatch.setattr(CONFIG, "dry_run", True, raising=False)
    return ClaudeBrowserAgent


@pytest.mark.asyncio
async def test_recording_a_part_captures_the_screen_and_its_tree(tmp_path, agente_de):
    nav = _Navegador(tmp_path)
    agente = agente_de(nav)
    resultado = AgentRunResult()

    salida, fin = await agente._dispatch_tool(
        "record_part",
        {"nombre": "Pantalla principal", "rol": "estudiante",
         "como_llegar": "Tocar el nombre", "que_muestra": "Los temas de 8.º"},
        resultado,
    )

    assert fin is False, "registrar no termina la exploración"
    assert nav.capturas == 1, "la captura la toma el código, no la pide el modelo"
    parte = resultado.parts[0]
    assert (tmp_path / parte["captura"]).exists()
    assert len(parte["sha256"]) == 64
    assert (tmp_path / parte["texto"]).read_text(encoding="utf-8") == ARBOL_ESTUDIANTE
    assert [e["texto"] for e in parte["entradas_en_pantalla"]][:2] == ["Temas", "Vista docente"]
    assert parte["firma"] and parte["url"] == RAIZ
    # El modelo VE la captura que acaba de registrar.
    assert salida[0]["type"] == "image"


@pytest.mark.asyncio
async def test_a_part_without_a_name_is_refused(tmp_path, agente_de):
    nav = _Navegador(tmp_path)
    resultado = AgentRunResult()
    salida, _ = await agente_de(nav)._dispatch_tool(
        "record_part", {"nombre": " ", "rol": "estudiante"}, resultado
    )
    assert "RECHAZADO" in salida and resultado.parts == [] and nav.capturas == 0


@pytest.mark.asyncio
async def test_a_behaviour_is_recorded_with_its_capture_too(tmp_path, agente_de):
    nav = _Navegador(tmp_path)
    resultado = AgentRunResult()
    await agente_de(nav)._dispatch_tool(
        "record_behavior",
        {"nombre": "cinco errores seguidos", "que_hice": "respondí mal cinco veces",
         "que_observe": "el nivel no subió", "conclusion": "avanzar depende de acertar"},
        resultado,
    )
    assert resultado.behaviors[0]["captura"] and resultado.parts == []
    assert "entradas_en_pantalla" not in resultado.behaviors[0]


def test_the_explorer_has_the_recording_tools_and_still_cannot_type():
    nombres = [t["name"] for t in explore_tools()]
    assert {"record_part", "record_behavior", "describe_platform"} <= set(nombres)
    assert "type_text" not in nombres


# --- 5. lo registrado al pasar sobrevive -------------------------------------


class _AgenteQueNoCierra:
    """Registra dos partes y se queda sin turnos antes del informe de cierre."""

    def __init__(self, *, partes):
        self.tools, self.max_iterations, self.cost_cap_usd = [], 0, 0.0
        self._partes = partes

    async def run(self, *, system, user_task):
        return SimpleNamespace(
            report=None, parts=self._partes, behaviors=[], blocked=True,
            block_reason="max_iterations (40) reached", estimated_cost_usd=0.5,
            cache_read_tokens=0, cache_write_tokens=0, turns=40,
        )


@pytest.mark.asyncio
async def test_what_was_recorded_survives_an_exploration_that_never_reported():
    """El 2026-11-25 tres exploraciones de 30 turnos vieron la plataforma entera
    y devolvieron cero partes, porque todo viajaba en el informe final."""
    partes = [{"nombre": "Pantalla principal", "captura": "a.png"},
              {"nombre": "Después de un error", "captura": "b.png"}]
    adaptador = AgentDriver(_AgenteQueNoCierra(partes=partes), SimpleNamespace())

    informe = await adaptador.explore("x")

    assert [p["nombre"] for p in informe["partes"]] == ["Pantalla principal", "Después de un error"]
    assert "max_iterations" in informe["incompleto"]
    assert adaptador.spend_usd == 0.5, "una exploración que no cerró igual se pagó"


@pytest.mark.asyncio
async def test_an_exploration_with_nothing_recorded_and_no_report_still_fails():
    from lxp_eval.evalkit.errors import EvalkitError

    adaptador = AgentDriver(_AgenteQueNoCierra(partes=[]), SimpleNamespace())
    with pytest.raises(EvalkitError):
        await adaptador.explore("x")
