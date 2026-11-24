"""El mapa descubierto tiene que ser legible por el consumidor REAL.

No se prueba contra un doble: se serializa, se lee con `load_surfaces` y se le
pide los pasos a `ProbeBank`. Un mapa que el banco no puede consumir no sirve
para nada, y esa incompatibilidad es exactamente la clase de defecto que un
doble esconde.
"""
from pathlib import Path

import pytest

from lxp_eval.evalkit.probe_bank import ProbeBank, SurfaceUnavailable, load_surfaces
from lxp_eval.evalkit.recon import PlatformMap, discover_surfaces

# Resuelto desde el archivo, no desde el cwd: un test que sólo pasa cuando se
# lo invoca parado en 06_Application falla en CI por una razón que no tiene
# nada que ver con lo que prueba. Es la convención del resto de la suite
# (ver tests/test_probe_bank.py).
BANKS = Path(__file__).resolve().parents[1] / "lxp_eval/evalkit/banks"


def test_a_discovered_map_round_trips_through_the_real_consumer(tmp_path: Path):
    m = PlatformMap()
    m.found(
        "tutor_chat",
        name="Chat del tutor de IA",
        steps=["Abrir el curso {course}", "Abrir la primera leccion"],
        send=["Escribir {stimulus} y enviarlo"],
        read=["Leer la respuesta completa"],
        defaults={"course": "BM - Matematicas Grado 3"},
    )
    m.unavailable("teacher_dashboard", "No accesible con esta cuenta de alumno.")

    path = tmp_path / "surfaces.yaml"
    m.write(path)

    surfaces = load_surfaces(path)
    assert "tutor_chat" in surfaces["surfaces"]
    assert surfaces["unavailable"]["teacher_dashboard"].startswith("No accesible")

    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    steps = bank.steps_for("PT-D9-H2", surfaces)
    assert any("BM - Matematicas Grado 3" in s for s in steps), \
        "los pasos tienen que salir de la navegacion descubierta"


def test_a_criterion_whose_surface_is_missing_raises_with_the_observed_reason(tmp_path: Path):
    """No encontrar una superficie es un HALLAZGO, no una falla silenciosa."""
    m = PlatformMap()
    m.unavailable("tutor_chat", "Esta cuenta no expone ningun tutor en las lecciones.")
    path = tmp_path / "surfaces.yaml"
    m.write(path)

    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    try:
        bank.steps_for("PT-D9-H2", load_surfaces(path))
    except SurfaceUnavailable as exc:
        assert "no expone ningun tutor" in str(exc)
    else:
        raise AssertionError("tenia que negarse a inventar una prueba")


def test_the_recon_agent_cannot_type_at_all():
    """No alcanza con pedirle que no escriba: hay que sacarle la herramienta.

    El reconocimiento corre contra plataformas EN USO. Un `type_text` en el
    chat del tutor es un mensaje real en la cuenta de un alumno real. La
    garantía tiene que ser estructural, no una línea del prompt que el modelo
    puede decidir ignorar.
    """
    from lxp_eval.browser_agents.claude_browser_agent import read_only_tools

    names = {t["name"] for t in read_only_tools()}
    assert "type_text" not in names, "el explorador no debe poder tipear"
    assert {"navigate", "click", "read_page", "screenshot"} <= names, \
        "pero sí tiene que poder recorrer y mirar"


def test_read_only_tools_does_not_mutate_the_shared_tool_list():
    """Si filtrara la lista en el lugar, el proximo agente NORMAL se quedaria
    sin `type_text` y las pruebas del tutor dejarian de poder enviar estimulos.
    Un bug asi no aparece hasta la corrida paga."""
    from lxp_eval.browser_agents.claude_browser_agent import (
        BROWSER_TOOLS,
        read_only_tools,
    )

    before = [t["name"] for t in BROWSER_TOOLS]
    read_only_tools()
    assert [t["name"] for t in BROWSER_TOOLS] == before
    assert "type_text" in before


# --- Task 4: el recorrido -----------------------------------------------------
#
# Los dobles implementan la MISMA interfaz que `AgentDriver` (ver
# `browser_agents/agent_driver.py` y los dobles de `tests/test_runner.py`):
# assert_session_alive / is_logged_out / url / do(step) / read_page / screenshot.


class _WalkAgent:
    """Un agente falso que muestra un curso y una lección. Como el agente real,
    `do()` LEVANTA cuando no puede completar un paso -- así se prueba que el
    tutor se detecta abriéndolo, no buscando texto. `fail_on` hace fallar los
    pasos cuyo texto contenga alguno de esos fragmentos."""

    def __init__(self, tmp_path: Path, page: str, fail_on: tuple[str, ...] = ()):
        self._tmp = tmp_path
        self._page = page
        self._fail_on = fail_on
        self.steps: list[str] = []

    async def assert_session_alive(self):
        pass

    async def is_logged_out(self) -> bool:
        return False

    async def url(self) -> str:
        return "https://app.ejemplo.com/studentV2/"

    async def do(self, step: str):
        for frag in self._fail_on:
            if frag.lower() in step.lower():
                raise RuntimeError(
                    f"el agente no pudo completar el paso: {step!r} — no existe "
                    "ese elemento en la pantalla"
                )
        self.steps.append(step)

    async def read_page(self, max_chars=None) -> str:
        return self._page

    async def screenshot(self, label="s", *, full_page=True) -> Path:
        p = self._tmp / f"{label}.png"
        p.write_bytes(b"PNG")
        return p


@pytest.mark.asyncio
async def test_the_walk_finds_the_tutor_inside_a_lesson(tmp_path):
    """El caso que ningún banco adivina.

    En Plataforma A el tutor no está en la pantalla de inicio: vive dentro de una
    lección, detrás de un botón violeta. Descubrirlo costó tres corridas pagas y
    terminó escrito a mano. El recorrido tiene que llegar solo, y los pasos que
    registra para el tutor tienen que pasar por una lección: un banco genérico
    que dijera "abrir el tutor" fallaría, porque el tutor no está en la home.
    """
    from lxp_eval.evalkit.recon import discover_surfaces

    agent = _WalkAgent(
        tmp_path,
        "Assignments: BM - Matematicas Grado 3 · Leccion 1 "
        "· boton de ayuda del tutor abajo a la derecha",
    )
    m = await discover_surfaces(agent, root_url="https://app.ejemplo.com/studentV2/")
    d = m.as_dict()

    assert "tutor_chat" in d["surfaces"], "no ubicó el tutor"
    assert "lesson_content" in d["surfaces"], "no ubicó una lección"
    assert any(
        "leccion" in s.lower() for s in d["surfaces"]["tutor_chat"]["steps"]
    ), "los pasos del tutor tienen que pasar por una lección, no por la home"


@pytest.mark.asyncio
async def test_the_tutor_is_unavailable_when_its_button_cannot_be_opened(tmp_path):
    """El caso real de Plataforma A: la lección abre, pero el tutor es un botón violeta
    SIN texto. La detección por palabra lo daba por ausente aunque estuviera;
    ahora se detecta ABRIÉNDOLO. Si el agente no puede abrirlo, `tutor_chat`
    queda en `unavailable` con su explicación, y `lesson_content` -- que SÍ se
    abrió -- sigue disponible."""
    from lxp_eval.evalkit.recon import discover_surfaces

    # El paso del tutor ("panel del tutor de IA") falla; el resto anda.
    agent = _WalkAgent(
        tmp_path,
        "Assignments: BM - Matematicas Grado 3 · Leccion 1 · contenido",
        fail_on=("tutor de IA",),
    )
    m = await discover_surfaces(agent, root_url="https://app.ejemplo.com/studentV2/")
    d = m.as_dict()

    assert "lesson_content" in d["surfaces"], "la lección sí se abrió"
    assert "tutor_chat" in d.get("unavailable", {}), "el tutor debía quedar no disponible"
    assert d["unavailable"]["tutor_chat"].strip(), "tiene que decir por qué"


def test_provider_root_switches_an_app_subdomain_to_the_public_site():
    """El recorrido real fallaba en privacidad: navegaba a app.plataforma-a.ejemplo
    (la app de login, sin pie de página) en vez de www.plataforma-a.ejemplo, donde
    vive el documento público."""
    from lxp_eval.evalkit.recon import _provider_root

    assert _provider_root("https://app.plataforma-a.ejemplo/studentV2/") == \
        "https://www.plataforma-a.ejemplo"
    # Un host que ya es público se deja como está.
    assert _provider_root("https://www.plataforma-a.ejemplo") == \
        "https://www.plataforma-a.ejemplo"


@pytest.mark.asyncio
async def test_a_surface_that_is_not_there_is_recorded_with_its_reason(tmp_path):
    """No encontrar una superficie es un HALLAZGO, no una falla silenciosa: se
    anota en `unavailable` con el motivo observado."""
    from lxp_eval.evalkit.recon import discover_surfaces

    class _Empty:
        async def assert_session_alive(self):
            pass

        async def is_logged_out(self) -> bool:
            return False

        async def url(self) -> str:
            return "https://app.ejemplo.com/"

        async def do(self, step: str):
            pass

        async def read_page(self, max_chars=None) -> str:
            return "Panel del alumno. No hay cursos asignados."

        async def screenshot(self, label="s", *, full_page=True) -> Path:
            p = tmp_path / f"{label}.png"
            p.write_bytes(b"PNG")
            return p

    m = await discover_surfaces(_Empty(), root_url="https://app.ejemplo.com/")
    d = m.as_dict()

    assert "tutor_chat" in d.get("unavailable", {}), \
        "un tutor ausente tiene que quedar declarado, no desaparecer"
    assert d["unavailable"]["tutor_chat"].strip(), "tiene que decir POR QUÉ"


@pytest.mark.asyncio
async def test_the_discovered_map_is_consumable_by_the_real_bank(tmp_path):
    """La guarda anti-doble: el mapa DESCUBIERTO (no uno escrito a mano) tiene
    que ser legible por el `ProbeBank` REAL y producir pasos que pasan por una
    lección. Un mapa con la forma correcta pero que el banco no puede consumir
    es exactamente el defecto que un doble esconde.
    """
    from lxp_eval.evalkit.recon import discover_surfaces

    agent = _WalkAgent(
        tmp_path,
        "Assignments: BM - Matematicas Grado 3 · Leccion 1 "
        "· abrir el chat del tutor de IA dentro de la leccion",
    )
    m = await discover_surfaces(agent, root_url="https://app.ejemplo.com/studentV2/")

    path = tmp_path / "surfaces.yaml"
    m.write(path)
    surfaces = load_surfaces(path)

    bank = ProbeBank.load(BANKS / "probes_sv_v7_4.yaml")
    steps = bank.steps_for("PT-D9-H2", surfaces)
    assert any("leccion" in s.lower() for s in steps), \
        "los pasos de la prueba tienen que salir de la navegación descubierta " \
        "y pasar por una lección"


# --- regresion: la pagina real de Plataforma A, que la heuristica vieja no leia -------


def test_detect_course_skips_nav_items_on_a_real_multiline_landing():
    """La landing real de Plataforma A: nav, "Class Assignments", el conteo, y recien los
    cursos -- SIN dos puntos. La heuristica vieja caia a "la primera linea" y
    devolvia "Home"; el agente entonces se negaba a abrir un curso llamado Home.
    Capturado en vivo el 2026-07-25 (runs/_walk_check/landing_real.txt)."""
    from lxp_eval.evalkit.recon import _detect_course

    landing = (
        "Home\nCode Editor\nHelp\nSettings\nV\nEstudiante de prueba\n"
        "Good evening, Estudiante de prueba\nJoin\nAssignments\nClass Assignments\n8\n"
        "BM - Lenguaje Grado 3\nBM - Lenguaje Grado 3\n"
        "Docente de prueba • Clase de prueba\nNo Due Date\nCourse\nNot Started\n"
        "BM - Matemáticas Grado 3\nBM - Matemáticas Grado 3\n"
        "Docente de prueba • Clase de prueba\nNo Due Date\nCourse\nIn Progress\n"
    )
    assert _detect_course(landing) == "BM - Lenguaje Grado 3"


def test_detect_course_still_reads_the_inline_colon_form():
    """No romper la forma con dos puntos que otras plataformas (y los dobles) usan."""
    from lxp_eval.evalkit.recon import _detect_course

    assert _detect_course(
        "Assignments: BM - Matematicas Grado 3 · Leccion 1 · boton de ayuda"
    ) == "BM - Matematicas Grado 3"


def test_detect_course_returns_none_when_only_navigation_is_present():
    from lxp_eval.evalkit.recon import _detect_course

    assert _detect_course("Home\nSettings\nHelp\nCode Editor") is None


@pytest.mark.needs_real_platform
def test_detect_course_passes_on_the_captured_plataforma_a_fixture():
    """La regla de fixtures que la auditoría volvió obligatoria: ningún parser
    corre en vivo sin pasar primero sobre texto REAL capturado. Las corridas
    1-2 del walk pagaron por saltearse esto (parsearon «Home» y un nombre
    fantasma antes de que nadie mirara la página real). Esta es la landing de
    Plataforma A capturada en vivo el 2026-07-25, sin retocar."""
    from lxp_eval.evalkit.recon import _detect_course

    fx = Path(__file__).parent / "fixtures" / "plataforma_a_landing_20260725.txt"
    assert _detect_course(fx.read_text(encoding="utf-8")) == "BM - Lenguaje Grado 3"


# --- el mapa tiene que describir la plataforma que HAY, no la que había ------


VIEJO = ("class assignments", "violeta", "esquina inferior derecha",
         "boton circular")

#: Una portada como la que hay hoy: tablero con clases, sin "Assignments".
PAGINA_CON_CURSOS = """Buen dia, estudiante
Mis clases
Clase de prueba - Matematica
Herramientas
Esta semana"""


@pytest.mark.asyncio
async def test_the_steps_describe_the_goal_not_yesterdays_widget(tmp_path):
    """Nombrar el widget ata el mapa a una versión de la interfaz.

    Los pasos decían «en la lista de Class Assignments» y «el boton circular
    violeta de la esquina inferior derecha». Tres semanas después la plataforma
    se rediseñó: la portada pasó a ser un tablero con «My classes» y el tutor a
    un botón con texto dentro de la clase. El recorrido igual anotaba la prosa
    vieja, así que el mapa describía una interfaz que ya no existe.

    Un paso que dice QUÉ conseguir sobrevive un rediseño; uno que dice dónde
    hacer clic, no.
    """
    agent = _WalkAgent(tmp_path, page=PAGINA_CON_CURSOS)

    m = await discover_surfaces(agent, root_url="https://ejemplo.org")

    texto = " ".join(
        " ".join(s.get("steps") or []) for s in m.as_dict()["surfaces"].values()
    ).lower()
    for viejo in VIEJO:
        assert viejo not in texto, f"el mapa todavía nombra {viejo!r}"


@pytest.mark.asyncio
async def test_a_found_surface_records_where_it_actually_landed(tmp_path):
    """Descubrir es mirar, no recordar.

    Sin esto el mapa sólo repite los pasos que se le pidieron ejecutar: si
    salen bien, se guardan tal cual. Anotar la URL observada es lo que
    convierte el recorrido en evidencia de que la superficie existe ahí.
    """
    agent = _WalkAgent(tmp_path, page=PAGINA_CON_CURSOS)

    m = await discover_surfaces(agent, root_url="https://ejemplo.org")

    esperado = await agent.url()
    for clave, superficie in m.as_dict()["surfaces"].items():
        assert superficie.get("observed", {}).get("url") == esperado, (
            f"{clave} no anota dónde se llegó: sin eso el mapa repite los pasos "
            "que se pidió ejecutar y «descubrir» es indistinguible de «recordar»"
        )


@pytest.mark.asyncio
async def test_an_unavailable_surface_still_says_why(tmp_path):
    """Lo de siempre: no encontrarla es un hallazgo, no un silencio."""
    agent = _WalkAgent(tmp_path, page=PAGINA_CON_CURSOS, fail_on=("tutor",))

    m = await discover_surfaces(agent, root_url="https://ejemplo.org")

    assert "tutor_chat" in m.as_dict()["unavailable"]
