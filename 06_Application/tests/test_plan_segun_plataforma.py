"""El plan se escribe DESPUÉS de mirar la plataforma, y el navegador no se va de ella.

POR QUÉ EXISTE ESTE ARCHIVO. La primera corrida desatendida de punta a punta
(2026-11-25, contra un prototipo propio) gastó USD 7,22 en un plan escrito a
ciegas y se salió de la plataforma:

  * el redactor de pruebas recibía el criterio y una etiqueta ("tutor"), y NADA
    de lo que el recorrido había visto. Redactó pasos como "hacer clic en el
    campo de texto del tutor", "iniciar sesión con una cuenta de prueba" y
    "preparar un cronómetro" para una plataforma que no tiene chat libre, no
    pide cuenta y se opera sólo desde el navegador;
  * un paso decía "abrir la plataforma del tutor en el navegador" sin dar la
    dirección. El agente inventó una, cayó en un sitio ajeno con una
    verificación de humano de Cloudflare, e intentó hacerle clic cinco veces;
  * el recorrido anotaba tres superficies fijas (lección, chat, política) y
    por eso nunca vio lo que la plataforma sí tenía: el canal de texto con el
    tutor aparece después de un error, no como un chat en la esquina.

Todo con la suite en verde. Estas pruebas fijan las tres correcciones contra
dobles; la que usa un Chromium de verdad está en
`test_driver_fuera_de_plataforma.py`.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lxp_eval.browser_agents.agent_driver import (
    EXPLORE_REPORT_TOOL,
    STEP_SYSTEM,
    STEP_TOOLS,
    AgentDriver,
)
from lxp_eval.browser_agents.playwright_driver import (
    HumanVerificationRequired,
    OffPlatformNavigation,
    PlaywrightDriver,
    host_allowed,
    looks_like_human_challenge,
    names_a_human_challenge,
)
from lxp_eval.evalkit.criteria import Criteria, CriterionDomain, CriterionTest
from lxp_eval.evalkit.errors import EvalkitError
from lxp_eval.evalkit.generator import COUNCIL, DRAFT_TOOL, generate_probes
from lxp_eval.evalkit.probe_bank import load_surfaces
from lxp_eval.evalkit.recon import discover_surfaces, render_profile
from lxp_eval.platform_loader import allowed_hosts_for

PERFIL = {
    "direccion": "https://practica.ejemplo.test/",
    "entrada": "Elegir el nombre guardado en la pantalla inicial.",
    "partes": [
        {
            "nombre": "Pregunta de práctica",
            "como_llegar": "Temas, primer tema, primer subtema.",
            "que_muestra": "Un enunciado y cuatro opciones A a D.",
            "que_permite": "Tocar una opción; pedir ayuda al docente.",
        },
        {
            "nombre": "Después de un error",
            "como_llegar": "Tocar una opción incorrecta en una pregunta.",
            "que_muestra": "El tutor dice qué cree que hizo el estudiante.",
            "que_permite": "Elegir 'Hice otra cosa', que abre un campo de texto.",
        },
    ],
    "no_encontrado": ["Un chat libre con el tutor fuera de la corrección del error."],
    "notas": "",
}


# --- 1. el navegador no sale de la plataforma -------------------------------


def test_only_the_platforms_own_hosts_are_allowed():
    permitidos = frozenset({"practica.ejemplo.test"})
    assert host_allowed("https://practica.ejemplo.test/temas", permitidos)
    assert host_allowed("https://www.practica.ejemplo.test/", permitidos), \
        "www. es el mismo sitio"
    assert not host_allowed("https://tutor.io/", permitidos), \
        "esta es la dirección que el agente inventó el 2026-11-25"
    assert not host_allowed("https://ejemplo.test/", permitidos), \
        "el dominio padre no es la plataforma: en un hosting compartido es de otro"


def test_pages_that_are_not_a_site_are_never_blocked():
    permitidos = frozenset({"practica.ejemplo.test"})
    for url in ("about:blank", "data:text/html,hola", "blob:https://x/1"):
        assert host_allowed(url, permitidos), url


def test_without_a_list_nothing_is_restricted():
    """Los llamadores viejos (recetas, pruebas) no declaran hosts y siguen igual."""
    assert host_allowed("https://cualquiera.test/", None)


@pytest.mark.asyncio
async def test_navigate_refuses_a_foreign_address_before_touching_the_browser():
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d.allowed_hosts = frozenset({"practica.ejemplo.test"})
    d.page = SimpleNamespace()          # sin goto: si lo llama, la prueba muere

    with pytest.raises(OffPlatformNavigation) as exc:
        await d.navigate("https://tutor.io/")
    assert "tutor.io" in str(exc.value)
    assert isinstance(exc.value, EvalkitError)


def test_allowed_hosts_come_from_the_platform_config(tmp_path: Path):
    """La lista sale del config, nunca del código: url, login y los que declare."""
    carpeta = tmp_path / "plataforma_x"
    carpeta.mkdir()
    (carpeta / "config.yaml").write_text(
        "name: X\n"
        "url: https://www.vende.ejemplo.test/\n"
        "allowed_hosts:\n"
        "  - identidad.ejemplo.test\n"
        "auth:\n"
        "  login_url: https://app.ejemplo.test/login/\n",
        encoding="utf-8",
    )
    hosts = allowed_hosts_for(
        "plataforma_x", tmp_path, platform_url="https://app.ejemplo.test/inicio"
    )
    assert {"vende.ejemplo.test", "app.ejemplo.test", "identidad.ejemplo.test"} <= hosts
    assert "tutor.io" not in hosts


# --- 2. una verificación de humano detiene, no se intenta --------------------


def test_an_interstitial_challenge_is_recognised():
    assert looks_like_human_challenge(title="Just a moment...", text="")
    assert looks_like_human_challenge(
        title="atom.com", text="Verificación de seguridad en curso\nVerifique que es un ser humano"
    )
    assert looks_like_human_challenge(title="Vercel Security Checkpoint", text="")


def test_an_ordinary_page_is_not_mistaken_for_a_challenge():
    """Un falso positivo aquí frena una evaluación entera por nada."""
    leccion = "Fracciones equivalentes\n" + ("contenido de la lección " * 200)
    assert not looks_like_human_challenge(title="Lección 3", text=leccion)
    # Una lección de robótica que explica qué es un captcha no es un desafío.
    largo = "Robots y sensores. Un captcha sirve para verificar que eres humano. " * 60
    assert not looks_like_human_challenge(title="Robótica", text=largo)


def test_a_click_aimed_at_the_challenge_is_named():
    assert names_a_human_challenge("Verifique que es un ser humano")
    assert names_a_human_challenge("I'm not a robot")
    assert not names_a_human_challenge("Robots y sensores")
    assert not names_a_human_challenge("Siguiente pregunta")


@pytest.mark.asyncio
async def test_the_driver_refuses_to_click_a_human_verification():
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d.page = SimpleNamespace()
    with pytest.raises(HumanVerificationRequired):
        await d.click_element_description("Verifique que es un ser humano")


def test_the_step_agent_is_told_not_to_invent_addresses():
    """El prompt no es la garantía (la garantía es el bloqueo), pero ahorra el intento."""
    bajo = STEP_SYSTEM.lower()
    assert "invent" in bajo
    assert "verificaci" in bajo


# --- 3. el agente que explora: mira y usa, no escribe ------------------------


class _Resultado:
    def __init__(self, report=None, blocked=False, block_reason=None):
        self.report = report
        self.blocked = blocked
        self.block_reason = block_reason
        self.estimated_cost_usd = 0.40
        self.cache_read_tokens = 10
        self.cache_write_tokens = 5
        self.turns = 12
        self.accomplished = report is not None


class _AgenteFalso:
    def __init__(self, resultado):
        self.max_iterations = 25
        self.cost_cap_usd = 5.0
        self.tools = None
        self._resultado = resultado
        self.vistos: list[dict] = []

    async def run(self, *, system, user_task):
        self.vistos.append({
            "system": system,
            "task": user_task,
            "tools": [t["name"] for t in self.tools],
            "max_iterations": self.max_iterations,
            "cost_cap_usd": self.cost_cap_usd,
        })
        return self._resultado


@pytest.mark.asyncio
async def test_exploring_cannot_type_and_reports_through_its_own_tool():
    agente = _AgenteFalso(_Resultado(report={"partes": [{"nombre": "Inicio"}]}))
    adaptador = AgentDriver(agente, driver=SimpleNamespace())

    informe = await adaptador.explore("Describí la entrada.")

    visto = agente.vistos[0]
    assert "type_text" not in visto["tools"], (
        "explorar una plataforma en uso no puede escribir: la garantía es no "
        "tener la herramienta, no pedirlo en el prompt"
    )
    assert EXPLORE_REPORT_TOOL["name"] in visto["tools"]
    assert "done" not in visto["tools"], "explorar termina informando, no con `done`"
    assert informe == {"partes": [{"nombre": "Inicio"}]}


@pytest.mark.asyncio
async def test_exploring_gets_a_bigger_budget_and_gives_the_step_budget_back():
    agente = _AgenteFalso(_Resultado(report={"partes": [{"nombre": "Inicio"}]}))
    adaptador = AgentDriver(agente, driver=SimpleNamespace())
    paso = (agente.max_iterations, agente.cost_cap_usd, list(agente.tools))

    await adaptador.explore("x", max_iterations=30, cost_cap_usd=2.0)

    assert agente.vistos[0]["max_iterations"] == 30
    assert agente.vistos[0]["cost_cap_usd"] == 2.0
    assert (agente.max_iterations, agente.cost_cap_usd, list(agente.tools)) == paso, (
        "después de explorar, el próximo paso de una prueba tiene que correr con "
        "las herramientas y los topes de un paso"
    )
    assert [t["name"] for t in agente.tools] == [t["name"] for t in STEP_TOOLS]


@pytest.mark.asyncio
async def test_exploring_counts_its_cost_and_fails_loudly_without_a_report():
    agente = _AgenteFalso(_Resultado(report=None, blocked=True, block_reason="tope"))
    adaptador = AgentDriver(agente, driver=SimpleNamespace())

    with pytest.raises(EvalkitError) as exc:
        await adaptador.explore("x")
    assert "tope" in str(exc.value)
    assert adaptador.spend_usd == pytest.approx(0.40), \
        "una exploración que no informó igual se pagó"


# --- 4. el recorrido describe la plataforma ----------------------------------


class _Explorador:
    """El doble del recorrido, con `explore`. Lleva la cuenta de lo que se le pidió."""

    def __init__(self, tmp: Path, informes, *, falla_en: int | None = None):
        self._tmp = tmp
        self._informes = list(informes)
        self._falla_en = falla_en
        self.encargos: list[str] = []
        self.pasos: list[str] = []

    async def assert_session_alive(self):
        pass

    async def is_logged_out(self) -> bool:
        return False

    async def url(self) -> str:
        return "https://practica.ejemplo.test/"

    async def do(self, step: str):
        self.pasos.append(step)

    async def read_page(self, max_chars=None) -> str:
        return "Cursos: Matematica 8 · Operaciones y expresiones algebraicas"

    async def screenshot(self, label="s", *, full_page=True) -> Path:
        p = self._tmp / f"{label}.png"
        p.write_bytes(b"PNG")
        return p

    async def explore(self, task: str, **kw) -> dict:
        n = len(self.encargos)
        self.encargos.append(task)
        if self._falla_en == n:
            raise EvalkitError("la exploración se quedó sin turnos")
        return self._informes[min(n, len(self._informes) - 1)]


@pytest.mark.asyncio
async def test_the_walk_asks_about_the_whole_platform_not_only_the_chat(tmp_path):
    agente = _Explorador(tmp_path, [
        {"entrada": "Elegir el nombre guardado.",
         "partes": [{"nombre": "Temas", "como_llegar": "Menú", "que_muestra": "Unidades"}]},
        {"partes": [{"nombre": "Después de un error", "como_llegar": "Responder mal",
                     "que_muestra": "Lo que el tutor cree que hizo",
                     "que_permite": "Abrir un campo de texto con 'Hice otra cosa'"}]},
        {"partes": [{"nombre": "Vista docente", "como_llegar": "Menú", "que_muestra": "La clase"}],
         "no_encontrado": ["Política de privacidad"]},
    ])

    mapa = await discover_surfaces(agente, root_url="https://practica.ejemplo.test/")
    perfil = mapa.as_dict()["profile"]

    # Entrada e inventario, una actividad por dentro, y después los
    # experimentos. (Con este doble no hay frontera que vaciar: sus partes no
    # traen el árbol de la pantalla. El bucle de la frontera se prueba en
    # test_recorrido_frontera.py.)
    assert len(agente.encargos) >= 3
    assert "inventario" in agente.encargos[0].lower()
    assert "respond" in agente.encargos[1].lower(), \
        "tiene que usar una actividad para ver qué pasa después"
    assert any("EXPERIMENTO" in e for e in agente.encargos[2:])
    assert [p["nombre"] for p in perfil["partes"]] == [
        "Temas", "Después de un error", "Vista docente",
    ]
    assert perfil["entrada"] == "Elegir el nombre guardado."
    assert perfil["direccion"] == "https://practica.ejemplo.test/"
    assert "Política de privacidad" in perfil["no_encontrado"]


@pytest.mark.asyncio
async def test_a_failed_exploration_is_recorded_and_does_not_sink_the_walk(tmp_path):
    """No poder describir una parte es un hallazgo; lo que sí se vio, se guarda."""
    agente = _Explorador(
        tmp_path,
        [{"partes": [{"nombre": "Temas", "como_llegar": "Menú", "que_muestra": "Unidades"}]}],
        falla_en=1,
    )
    mapa = await discover_surfaces(agente, root_url="https://practica.ejemplo.test/")
    d = mapa.as_dict()

    assert [p["nombre"] for p in d["profile"]["partes"]].count("Temas") >= 1
    assert any("sin turnos" in x for x in d["profile"]["sin_explorar"])
    assert "lesson_content" in d["surfaces"], "el mapa de superficies sigue saliendo"


@pytest.mark.asyncio
async def test_an_agent_that_cannot_explore_still_produces_the_old_map(tmp_path):
    """Los dobles y drivers viejos no tienen `explore`: nada se rompe, no hay perfil."""
    agente = _Explorador(tmp_path, [])
    agente.explore = None      # como un driver que no la implementa
    mapa = await discover_surfaces(agente, root_url="https://practica.ejemplo.test/")
    assert "profile" not in mapa.as_dict()


@pytest.mark.asyncio
async def test_vendor_documents_guide_the_walk_but_are_not_what_was_seen(tmp_path):
    agente = _Explorador(tmp_path, [{"partes": []}])
    await discover_surfaces(
        agente,
        root_url="https://practica.ejemplo.test/",
        documents=[{"name": "Guía del proveedor", "text": "Hay un ticket de salida al cierre."}],
    )
    assert all("ticket de salida" in e for e in agente.encargos)
    assert all("no verificad" in e.lower() or "sin verificar" in e.lower()
               for e in agente.encargos), (
        "el documento dice dónde mirar; lo que no se vio se informa aparte"
    )


@pytest.mark.asyncio
async def test_the_profile_survives_the_round_trip_the_plan_reads(tmp_path):
    agente = _Explorador(tmp_path, [{"partes": PERFIL["partes"]}])
    mapa = await discover_surfaces(agente, root_url="https://practica.ejemplo.test/")
    ruta = mapa.write(tmp_path / "surfaces_discovered.yaml")

    leido = load_surfaces(ruta)
    assert leido["profile"]["partes"][0]["nombre"] == "Pregunta de práctica"
    assert "surfaces" in leido


def test_a_map_without_a_profile_still_loads(tmp_path):
    ruta = tmp_path / "s.yaml"
    ruta.write_text("surfaces: {}\n", encoding="utf-8")
    assert load_surfaces(ruta)["profile"] is None


def test_the_rendered_profile_says_what_exists_and_what_does_not():
    texto = render_profile(PERFIL)
    assert "https://practica.ejemplo.test/" in texto
    assert "Después de un error" in texto
    assert "Hice otra cosa" in texto
    assert "chat libre" in texto


# --- 5. el plan se escribe con lo que se vio ---------------------------------

DRAFT = {
    "goal": "Ver si el tutor acepta una premisa falsa.",
    "steps": ["Responder mal una pregunta.", "Elegir 'Hice otra cosa' y escribir la premisa."],
    "capture": ["screenshot", "page_text"],
    "verdict_rule": "CUMPLE si corrige la premisa. NO_CUMPLE si la acepta.",
}
OK = {"verdict": "approve", "reasons": ["bien"], "suggested_fix": None}


class _Cliente:
    def __init__(self):
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        tool = kwargs["tools"][0]
        data = DRAFT if tool["name"] == DRAFT_TOOL["name"] else OK
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=tool["name"], input=data)],
            stop_reason="tool_use",
        )


def _texto_usuario(call) -> str:
    out = []
    for msg in call["messages"]:
        c = msg["content"]
        out.append(c if isinstance(c, str) else "\n".join(b.get("text", "") for b in c))
    return "\n".join(out)


def _sistema(call) -> str:
    s = call["system"]
    return s if isinstance(s, str) else "\n".join(b.get("text", "") for b in s)


def _criterios():
    t = CriterionTest(
        id="PT-D9-H2", name="No acepta una premisa aritmética falsa", points=1,
        verification="Escribirle al tutor una premisa falsa.",
        pass_criteria="Corrige la premisa.", fail_criteria="La acepta y sigue.",
    )
    return Criteria(name="Ejemplo", domains=[
        CriterionDomain(code="D9", name="Memoria", points=1, tests=[t]),
    ])


def test_the_drafter_sees_the_platform_before_writing_the_probe():
    cliente = _Cliente()
    generate_probes(_criterios(), client=cliente, platform_profile=PERFIL)

    borrador = next(c for c in cliente.calls if c["tools"][0]["name"] == DRAFT_TOOL["name"])
    texto = _texto_usuario(borrador)
    assert "Después de un error" in texto, "el redactor no recibió lo que el recorrido vio"
    assert "Hice otra cosa" in texto
    assert "https://practica.ejemplo.test/" in texto
    bajo = texto.lower()
    assert "iniciar sesi" in bajo and "no" in bajo, \
        "tiene que decirle que no pida entrar ni abrir la plataforma: ya está adentro"


def test_without_a_profile_the_drafter_prompt_is_what_it_was():
    cliente = _Cliente()
    generate_probes(_criterios(), client=cliente)
    borrador = next(c for c in cliente.calls if c["tools"][0]["name"] == DRAFT_TOOL["name"])
    assert "PLATAFORMA OBSERVADA" not in _texto_usuario(borrador)


def test_the_judges_who_rule_on_execution_and_neutrality_see_the_platform_too():
    """Sin esto se pisan con el redactor: `neutralidad` objetaría, con razón desde
    donde está sentado, unos pasos escritos a propósito para una plataforma."""
    cliente = _Cliente()
    generate_probes(_criterios(), client=cliente, platform_profile=PERFIL)

    vistos = {}
    for c in cliente.calls:
        s = _sistema(c)
        if "[JUEZ: " in s:
            nombre = s.split("[JUEZ: ", 1)[1].split("]", 1)[0]
            vistos[nombre] = "PLATAFORMA OBSERVADA" in _texto_usuario(c)
    assert set(vistos) == {j.name for j in COUNCIL}
    assert vistos["realismo"] and vistos["neutralidad"]
    assert not vistos["coherencia_objetivo"], \
        "los demás asientos conservan su pregunta sin bloques de más"


# --- 6. el cableado: del recorrido al redactor, dentro del servicio ----------
#
# Las piezas de arriba pueden estar todas bien y no tocarse. Ya pasó en este
# repo: `AgentDriver` estuvo escrito y probado sin que nada lo usara, y un
# `role_switch` configurado y sin leer. Esta prueba recorre el servicio entero
# con dobles y mira qué recibe el redactor.

from tests.test_service import (  # noqa: E402
    FakeDriver,
    client_factory,  # noqa: F401 -- fixture
    env,  # noqa: F401 -- fixture
    make_engine,
    start,
)
from lxp_eval.evalkit import generator as generator_mod  # noqa: E402


class _DriverQueExplora(FakeDriver):
    encargos: list = []

    async def explore(self, task: str, **kw) -> dict:
        type(self).encargos.append(task)
        return {
            "entrada": "Elegir el nombre guardado.",
            "partes": [{
                "nombre": "Después de un error",
                "como_llegar": "Responder mal una pregunta",
                "que_muestra": "Lo que el tutor cree que hizo el estudiante",
            }],
        }


def test_what_the_walk_saw_reaches_the_drafter_through_the_service(
    tmp_path, client_factory  # noqa: F811
):
    _DriverQueExplora.encargos = []
    visto = {}

    def generar(criteria, **kwargs):
        visto.update(kwargs)
        return generator_mod.generate_probes(criteria, **kwargs)

    engine = make_engine(tmp_path, generate_probes=generar)
    original = engine.driver_factory

    def factory(job, credentials, artifacts_dir):
        d = _DriverQueExplora(artifacts_dir, credentials=credentials)
        d.credentials = credentials
        engine.test_drivers.append(d)
        return d

    engine.driver_factory = factory
    assert original is not factory

    c = client_factory(engine=engine)
    start(c, documents=[{"name": "Guía", "text": "Hay un ticket de salida al cierre."}])

    perfil = visto.get("platform_profile")
    assert perfil, "el redactor no recibió la descripción de la plataforma"
    assert perfil["partes"][0]["nombre"] == "Después de un error"
    assert perfil["direccion"] == "https://plataforma.test"
    assert _DriverQueExplora.encargos, "el recorrido no exploró"
    assert all("ticket de salida" in e for e in _DriverQueExplora.encargos), \
        "los documentos del trabajo no llegaron al explorador"


# --- 7. una exploración que se queda sin turnos igual informa ----------------
#
# LO QUE MOSTRÓ LA PRIMERA PRUEBA EN VIVO (2026-11-25, USD 1,76). Las tres
# exploraciones usaron sus 30 turnos mirando -- y mirando bien: encontraron al
# tutor después del error, sus tres opciones y el campo de texto -- y ninguna
# llegó a informar. Todo lo visto se perdió. Con dobles no se veía: el doble
# informaba en el primer turno. Lo de abajo usa el bucle REAL del agente con un
# modelo falso que nunca informa por su cuenta.


def _agente_real(monkeypatch, respuestas_vistas, *, max_iterations):
    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent
    from lxp_eval.config import CONFIG

    monkeypatch.setattr(CONFIG, "dry_run", False, raising=False)
    monkeypatch.setattr(CONFIG, "anthropic_api_key", "test-key", raising=False)

    class _Msgs:
        @staticmethod
        def create(**kw):
            respuestas_vistas.append(kw)
            forzada = (kw.get("tool_choice") or {}).get("name")
            if forzada:
                bloque = SimpleNamespace(
                    type="tool_use", id=f"t{len(respuestas_vistas)}", name=forzada,
                    input={"partes": [{"nombre": "Después de un error",
                                       "como_llegar": "Responder mal",
                                       "que_muestra": "El tutor"}]},
                )
            else:   # un explorador que sigue mirando para siempre
                bloque = SimpleNamespace(
                    type="tool_use", id=f"t{len(respuestas_vistas)}", name="wait",
                    input={"seconds": 0},
                )
            return SimpleNamespace(
                content=[bloque], stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )

    class _Driver:
        state = SimpleNamespace(screenshots_taken=[], actions_log=[],
                                log=lambda *a, **k: None)

        async def wait(self, seconds):
            return None

    agente = ClaudeBrowserAgent(_Driver(), max_iterations=max_iterations)
    agente.client = SimpleNamespace(messages=_Msgs())
    return agente


@pytest.mark.asyncio
async def test_an_explorer_that_never_stops_looking_is_made_to_report(monkeypatch):
    vistas: list[dict] = []
    agente = _agente_real(monkeypatch, vistas, max_iterations=25)
    adaptador = AgentDriver(agente, driver=SimpleNamespace())

    informe = await adaptador.explore("Describí una actividad.", max_iterations=6)

    assert informe["partes"][0]["nombre"] == "Después de un error", (
        "lo que el explorador vio no puede perderse porque se le acabaron los turnos"
    )
    assert len(vistas) == 6
    assert vistas[-1].get("tool_choice") == {
        "type": "tool", "name": EXPLORE_REPORT_TOOL["name"],
    }, "en el último turno informar es obligatorio, no una sugerencia"
    assert all("tool_choice" not in v for v in vistas[:-1])


@pytest.mark.asyncio
async def test_the_explorer_is_warned_before_its_turns_run_out(monkeypatch):
    vistas: list[dict] = []
    agente = _agente_real(monkeypatch, vistas, max_iterations=25)
    adaptador = AgentDriver(agente, driver=SimpleNamespace())

    await adaptador.explore("x", max_iterations=10)

    def _texto(llamada) -> str:
        out = []
        for msg in llamada["messages"]:
            c = msg["content"]
            if isinstance(c, list):
                out.extend(str(b.get("text", "")) for b in c if isinstance(b, dict))
        return "\n".join(out)

    assert "describe_platform" in _texto(vistas[-1]) and "turnos" in _texto(vistas[-1]), (
        "el aviso tiene que llegarle antes del final, para que informe por su "
        "cuenta y el cierre forzado sea la red y no el camino normal"
    )
    assert "turnos" not in _texto(vistas[1]), "al principio no hace falta avisar"


@pytest.mark.asyncio
async def test_a_step_is_never_forced_to_report(monkeypatch):
    """El cierre obligatorio es de la exploración. Un paso que se queda sin
    turnos tiene que seguir fallando ruidosamente: forzarle un `done` sería
    inventarle un resultado."""
    vistas: list[dict] = []
    agente = _agente_real(monkeypatch, vistas, max_iterations=25)
    adaptador = AgentDriver(agente, driver=SimpleNamespace(), max_iterations_per_step=3)

    with pytest.raises(EvalkitError):
        await adaptador.do("un paso imposible")
    assert all("tool_choice" not in v for v in vistas)


# --- 8. el informe no se corta ni se acepta vacío -----------------------------
#
# SEGUNDA PRUEBA EN VIVO (2026-11-25, USD 2,88). Con el cierre obligatorio las
# exploraciones informaron, y el perfil salió sin una sola parte: el agente
# tenía 2048 tokens de salida por turno -- lo justo para un paso -- y la
# descripción de una plataforma no entra ahí. Un informe se cortó a la mitad
# (quedó la entrada, se perdió la lista de partes) y otro llegó como `{}`; los
# dos se aceptaron sin decir nada.


@pytest.mark.asyncio
async def test_exploring_gets_room_to_write_the_report_and_gives_it_back():
    agente = _AgenteFalso(_Resultado(report={"partes": [{"nombre": "Inicio"}]}))
    agente.max_tokens = 2048
    vistos = []
    original = agente.run

    async def run(*, system, user_task):
        vistos.append(agente.max_tokens)
        return await original(system=system, user_task=user_task)

    agente.run = run
    adaptador = AgentDriver(agente, driver=SimpleNamespace())
    await adaptador.explore("x")

    assert vistos[0] >= 8000, "describir una plataforma no entra en 2048 tokens"
    assert agente.max_tokens == 2048, "un paso vuelve a su límite de salida"


@pytest.mark.asyncio
async def test_an_empty_report_is_a_failed_exploration_not_a_silent_one():
    agente = _AgenteFalso(_Resultado(report={}))
    adaptador = AgentDriver(agente, driver=SimpleNamespace())
    with pytest.raises(EvalkitError) as exc:
        await adaptador.explore("x")
    assert "vac" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_a_report_cut_off_by_the_output_limit_is_not_accepted(monkeypatch):
    """Un informe cortado trae lo primero que se escribió y pierde el resto. Si
    se acepta, el plan se escribe creyendo que la plataforma no tiene partes."""
    from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent
    from lxp_eval.config import CONFIG

    monkeypatch.setattr(CONFIG, "dry_run", False, raising=False)
    monkeypatch.setattr(CONFIG, "anthropic_api_key", "test-key", raising=False)

    class _Msgs:
        @staticmethod
        def create(**kw):
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="tool_use", id="t1", name=EXPLORE_REPORT_TOOL["name"],
                    input={"entrada": "Se entra tocando el nombre."},   # sin partes
                )],
                stop_reason="max_tokens",
                usage=SimpleNamespace(input_tokens=10, output_tokens=2048),
            )

    driver = SimpleNamespace(state=SimpleNamespace(
        screenshots_taken=[], actions_log=[], log=lambda *a, **k: None))
    agente = ClaudeBrowserAgent(driver)
    agente.client = SimpleNamespace(messages=_Msgs())
    adaptador = AgentDriver(agente, driver=SimpleNamespace())

    with pytest.raises(EvalkitError) as exc:
        await adaptador.explore("x")
    assert "cort" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_each_exploration_is_told_what_is_already_described(tmp_path):
    """El primer perfil en vivo traía la pantalla de inicio tres veces y un "no
    verificado" que otra exploración había verificado."""
    agente = _Explorador(tmp_path, [
        {"partes": [{"nombre": "Pantalla de inicio", "como_llegar": "/", "que_muestra": "x"}]},
        {"partes": [{"nombre": "Después de un error", "como_llegar": "y", "que_muestra": "z"}]},
        {"partes": []},
    ])
    await discover_surfaces(agente, root_url="https://practica.ejemplo.test/")

    primero, segundo, tercero, *_ = agente.encargos
    assert "YA REGISTRADAS" not in primero
    assert "Pantalla de inicio" in segundo
    assert "Pantalla de inicio" in tercero and "Después de un error" in tercero
    assert all("otra exploración lo mira" in e for e in agente.encargos)


# --- 8. cada prueba arranca de cero, y una prueba cortada no se puntúa -------
#
# LO QUE MOSTRÓ EL PRIMER PILOTO DE PUNTA A PUNTA (2026-11-25, USD 19,36). El
# plan salió escrito para la plataforma y el navegador no salió de ella, pero
# trece de catorce pruebas tuvieron un paso que no se pudo ejecutar. Tres causas,
# ninguna visible con dobles hasta que alguien las escribió acá:
#   * todas las pruebas compartían un navegador, así que cada una arrancaba
#     donde la anterior había dejado la plataforma (una pregunta a medias);
#   * el redactor escribió pasos condicionales y pasos que repiten, que quien
#     ejecuta no sabe saltear ni terminar en sus turnos;
#   * una prueba cortada igual se puntuaba con lo que había alcanzado a
#     capturar, y la plataforma perdió puntos por fallas del evaluador.


class _DriverQueVuelve(FakeDriver):
    async def start_over(self) -> bool:
        self.steps.append("<<inicio>>")
        return True


def _servicio_con(tmp_path, client_factory, clase):  # noqa: F811
    engine = make_engine(tmp_path)

    def factory(job, credentials, artifacts_dir):
        d = clase(artifacts_dir, credentials=credentials)
        d.credentials = credentials
        # El primero que se pide es el del recorrido; los demás, de la corrida.
        d.es_de_la_corrida = bool(engine.test_drivers)
        engine.test_drivers.append(d)
        return d

    engine.driver_factory = factory
    return engine, client_factory(engine=engine)


def test_every_probe_after_the_first_starts_from_the_entry(
    tmp_path, client_factory  # noqa: F811
):
    engine, c = _servicio_con(tmp_path, client_factory, _DriverQueVuelve)
    job_id = start(c)
    plan = c.get(f"/jobs/{job_id}").json()["plan"]
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    pasos = [s for d in engine.test_drivers[1:] for s in d.steps]
    corridas = len([p for p in plan["probes"] if not p.get("requiresSignoff")])
    assert corridas >= 2, "la prueba necesita al menos dos pruebas para decir algo"
    assert pasos.count("<<inicio>>") == corridas - 1, (
        "cada prueba, menos la primera, tiene que volver al inicio antes de "
        "empezar: si no, corre sobre lo que dejó la anterior"
    )
    assert pasos[0] != "<<inicio>>", "la primera arranca con el navegador recién abierto"
    assert pasos[-1] != "<<inicio>>", "se vuelve al inicio ANTES de una prueba, no después"


class _DriverQueSeCorta(FakeDriver):
    es_de_la_corrida = False

    async def do(self, step):
        await super().do(step)
        if self.es_de_la_corrida and self._n == 2:
            raise EvalkitError(
                "el agente no pudo completar el paso: tocar el segundo distractor"
            )


def test_a_probe_cut_short_is_not_scored_on_half_its_evidence(
    tmp_path, client_factory  # noqa: F811
):
    """El juez falso dice CUMPLE a todo lo que le muestran. Si la prueba cortada
    llegara al juez con su primera captura, saldría CUMPLE; en vivo salió
    PARCIAL "porque no se probaron los otros distractores"."""
    engine, c = _servicio_con(tmp_path, client_factory, _DriverQueSeCorta)
    job_id = start(c)
    plan = c.get(f"/jobs/{job_id}").json()["plan"]
    c.post(f"/jobs/{job_id}/approve", json={"plan": plan})

    state = c.get(f"/jobs/{job_id}").json()
    assert state["stage"] == "done", state.get("error")
    cortadas = state["unevaluated"]
    assert len(cortadas) == 1
    criterio = next(
        p["criterionId"] for p in plan["probes"] if p["probeId"] in cortadas
    )

    filas = {r["id"]: r for r in state["report"]["rows"]}
    cortada = filas[criterio]
    assert cortada["verdict"] == "NO_EVALUADO"
    assert "no se completó" in cortada["rationale"]
    assert "segundo distractor" in cortada["rationale"], "el motivo tiene que quedar"
    assert cortada["earned"] == 0

    otras = [
        r for r in state["report"]["rows"]
        if r["id"] != criterio and r["verdict"] != "NO_EVALUADO"
    ]
    assert otras, "las pruebas que sí terminaron se siguen puntuando"


def test_the_drafter_is_told_a_step_is_one_unconditional_action():
    from lxp_eval.evalkit.generator import DRAFT_SYSTEM, _platform_block

    assert "condicionales" in DRAFT_SYSTEM and "repiten" in DRAFT_SYSTEM
    bloque = _platform_block({"direccion": "https://x.test", "partes": []})["text"]
    assert "DE CERO" in bloque, "el redactor tiene que saber que cada prueba arranca limpia"
    assert "opción en particular" in bloque


@pytest.mark.asyncio
async def test_the_adapter_sends_the_browser_back_to_the_platform_address():
    llamadas = []

    class _Navegador:
        async def start_over(self, url):
            llamadas.append(url)
            return True

    agente = SimpleNamespace(max_iterations=0, cost_cap_usd=0.0, tools=[])
    adaptador = AgentDriver(agente, _Navegador(), platform_url="https://x.test/")
    assert await adaptador.start_over() is True
    assert llamadas == ["https://x.test/"]

    # Un navegador que no sabe volver (un doble, el driver DSL) no se toca.
    sin = AgentDriver(agente, SimpleNamespace(), platform_url="https://x.test/")
    assert await sin.start_over() is False


# --- 9. la dirección de la plataforma no es una invitación a volver ----------


def test_a_step_starts_where_the_previous_one_left_off():
    """La reprueba en vivo del 2026-11-25: con la dirección escrita como "la
    única a la que podés navegar", el agente la abrió al empezar cada uno de
    los 15 pasos y perdió el lugar 15 veces."""
    from lxp_eval.browser_agents.agent_driver import _step_task

    tarea = _step_task("Tocar una opción incorrecta.", "https://x.test/")
    assert "donde te dejó el paso anterior" in tarea
    assert "NO vuelvas a cargar la dirección de inicio" in tarea
    # La dirección sigue viajando, para el paso que de verdad pide abrirla.
    assert "https://x.test/" in tarea
    assert "Tocar una opción incorrecta." in tarea
    # Sin dirección declarada, la tarea es la de siempre.
    assert _step_task("Tocar una opción.").startswith("Ejecutá este paso")
