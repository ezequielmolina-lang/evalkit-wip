"""El driver contra un Chromium DE VERDAD, sobre una página local.

POR QUÉ EXISTE. El resto de la suite corre contra dobles, y un doble presenta
la forma que el código espera -- por eso 950 tests en verde convivieron todo un
día con tres defectos que sólo aparecieron contra la plataforma en vivo:

  * `page.accessibility` había sido ELIMINADO de Playwright, así que cada
    lectura de página caía a `inner_text` detrás de un warning que nadie leía, y
    el agente pasó de un árbol de roles a texto crudo -- 20.414 caracteres
    contra un presupuesto de 3.000;
  * `click_by_text` buscaba texto visible, así que un botón nombrado por
    `aria-label` no existía para ella. El agente lo veía en el árbol y no podía
    tocarlo;
  * el texto de página se truncaba por el principio, y en una página larga lo
    que sobrevive es el índice de navegación, no el contenido.

Ninguno de los tres necesita la plataforma para reproducirse: necesitan un
navegador real y una página con esas formas. Eso cuesta un segundo y cero pesos,
y es la diferencia entre enterarse acá o enterarse en una corrida paga de 90
minutos.

La reproducción desde el ledger (`evalkit.replay`) es el otro lazo, y es ciego a
esta capa por construcción: una grabación está río abajo del navegador. Los dos
hacen falta.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from lxp_eval.browser_agents.playwright_driver import PlaywrightDriver

PAGINA = (Path(__file__).parent / "fixtures" / "contrato_navegador.html").as_uri()


@pytest.fixture
async def driver():
    """Un PlaywrightDriver real sobre la página local. Sin red."""
    playwright = pytest.importorskip("playwright.async_api")
    async with playwright.async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            # Sin navegador estos siete se saltean, que es lo correcto en la
            # máquina de alguien que todavía no corrió `playwright install`.
            # En CI NO lo es: son los únicos que ejercitan un Chromium de
            # verdad, así que un salteo silencioso ahí convierte el verde en
            # una afirmación sobre los dobles y nada más. EVALKIT_REQUIRE_BROWSER
            # lo vuelve un error ruidoso, y el workflow lo pone en 1.
            if os.environ.get("EVALKIT_REQUIRE_BROWSER"):
                raise RuntimeError(
                    "EVALKIT_REQUIRE_BROWSER=1 y no hay Chromium: corré "
                    f"`python -m playwright install chromium`. Causa: {exc}"
                ) from exc
            pytest.skip(f"no hay chromium: {exc}")
        page = await (await browser.new_context()).new_page()
        await page.goto(PAGINA)
        d = PlaywrightDriver.__new__(PlaywrightDriver)
        d.page = page
        d.action_delay_s = 0
        d.state = type("S", (), {"log": lambda self, *a, **k: None})()
        yield d
        await browser.close()


@pytest.mark.asyncio
async def test_the_page_is_read_as_an_accessibility_tree(driver):
    """`page.accessibility` ya no existe en Playwright; el árbol tiene que salir igual.

    Cuando la llamada empezó a fallar, el driver caía a `inner_text` y nada se
    rompía ruidosamente: el agente simplemente empezó a recibir texto crudo. Un
    doble nunca lo habría mostrado, porque el doble no tiene API de Playwright
    que se pueda haber eliminado.
    """
    texto = await driver._page_text()

    assert "Navigate to step 1" in texto, (
        "el nombre accesible no aparece: se está leyendo texto crudo, no el árbol"
    )
    assert "button" in texto.lower(), "el árbol tiene que traer roles, no sólo palabras"


@pytest.mark.asyncio
async def test_a_button_named_only_by_aria_label_can_be_clicked(driver):
    """El botón que el agente veía y no podía tocar."""
    assert await driver.click_by_text("Navigate to step 2") is True
    assert await driver.page.title() == "clic: Navigate to step 2"


@pytest.mark.asyncio
async def test_visible_text_is_still_the_first_way_in(driver):
    assert await driver.click_by_text("Siguiente") is True
    assert await driver.page.title() == "clic: Siguiente"


@pytest.mark.asyncio
async def test_an_element_named_by_its_label_is_reachable(driver):
    assert await driver.click_by_text("Tu respuesta") is True


@pytest.mark.asyncio
async def test_something_absent_fails_instead_of_clicking_something_near(driver):
    assert await driver.click_by_text("Un boton que no existe") is False


@pytest.mark.asyncio
async def test_the_record_is_never_truncated(driver):
    """`max_chars=None` tiene que traer la página entera.

    En una página larga el contenido evaluable está al FINAL -- una conversación
    con el tutor, el indicador de logro de la lección -- y el índice de
    navegación está al principio. Truncar por el principio guarda justo lo que
    no se evalúa, y el hash certifica esos bytes como «lo que se vio».
    """
    completo = await driver.read_page(max_chars=None)

    assert "Indicador de logro" in completo, "se perdió el final de la página"
    assert driver.last_read_truncated is False


@pytest.mark.asyncio
async def test_a_capped_read_says_so_and_reports_the_real_size(driver):
    """El presupuesto del agente sí se recorta, pero la lectura lo admite."""
    corto = await driver.read_page(max_chars=1200)

    assert driver.last_read_truncated is True
    assert driver.last_read_full_chars > 1200
    assert "TEXTO TRUNCADO" in corto, "una lectura recortada tiene que decirlo"


# --- los dos cortes de la corrida completa del 2026-11-27 --------------------
#
# En tests/test_bloqueos_del_ejecutor.py se prueba la lógica contra dobles. Acá
# lo que un doble no puede mostrar: que el control declarado se encuentra y se
# toca en un DOM real, que la pregunta se reconoce en el árbol de accesibilidad
# tal como lo devuelve Chromium, y que lo escrito en un cuadro se puede leer.
# El modelo sigue siendo de mentira: no hay red.


class _Resultado:
    def __init__(self, accomplished, done_reason=""):
        self.accomplished = accomplished
        self.done_reason = done_reason
        self.blocked = False
        self.turns = 1
        self.estimated_cost_usd = 0.0


class _Agente:
    """Devuelve los resultados en orden y guarda lo que se le pidió."""

    def __init__(self, *results):
        self._results = list(results)
        self.tasks: list[str] = []
        self.max_iterations = 25
        self.cost_cap_usd = 5.0
        self.tools = None

    async def run(self, *, system, user_task):
        self.tasks.append(user_task)
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


BLOQUEO = {
    "aparece": "después de una respuesta equivocada",
    "pregunta": "¿Lo hiciste así?",
    "se_despeja_con": "Sí, así fue",
    "opciones": ["Sí, así fue", "No, hice otra cosa"],
}


@pytest.mark.asyncio
async def test_una_pregunta_que_bloquea_se_despeja_en_un_dom_real(driver):
    """Tras el error la página sólo ofrece la pregunta; el paso siguiente no se puede hacer.

    Es la forma exacta del corte: las opciones quedan deshabilitadas, "Otra
    pregunta" no existe todavía, y quien ejecuta dice que no pudo. Con el
    bloqueo declarado, el evaluador toca el control y el paso se reintenta.
    """
    from lxp_eval.browser_agents.agent_driver import AgentDriver
    from lxp_eval.evalkit.steps import DEVIATION

    assert await driver.click_by_text("A 3") is True
    assert await driver.page.locator("#bloqueo").is_visible()
    assert not await driver.page.locator("#correccion").is_visible()

    agent = _Agente(
        _Resultado(False, "No hay una pregunta nueva; la plataforma pide responder otra cosa."),
        _Resultado(True),
    )
    ad = AgentDriver(agent, driver, blockers=[BLOQUEO])

    out = await ad.do("En la nueva pregunta que aparezca, tocar una opción incorrecta.")

    assert not await driver.page.locator("#bloqueo").is_visible(), "la pregunta sigue en pantalla"
    assert await driver.page.locator("#correccion").is_visible()
    assert len(agent.tasks) == 2
    assert out.startswith(DEVIATION) and "Sí, así fue" in out


@pytest.mark.asyncio
async def test_sin_la_pregunta_en_pantalla_no_se_toca_nada(driver):
    """El mismo paso fallando en otra pantalla: el despeje no inventa un toque."""
    from lxp_eval.browser_agents.agent_driver import AgentDriver
    from lxp_eval.evalkit.repair import StepNotDone

    ad = AgentDriver(_Agente(_Resultado(False, "no pude")), driver, blockers=[BLOQUEO])
    with pytest.raises(StepNotDone):
        await ad.do("En la nueva pregunta que aparezca, tocar una opción incorrecta.")
    assert not await driver.page.locator("#correccion").is_visible()


@pytest.mark.asyncio
async def test_lo_escrito_y_sin_enviar_se_puede_leer(driver):
    """La precondición de "enviar": hay algo escrito. Y después de enviar, ya no."""
    assert await driver.pending_text() == ""

    await driver.page.locator("#mensaje").click()
    assert await driver.type_into_focused("no entiendo el paso dos", delay_ms=0) is True
    assert await driver.pending_text() == "no entiendo el paso dos"

    assert await driver.click_by_role("button", "Enviar") is True
    assert await driver.pending_text() == "", "enviar vacía el cuadro: no queda nada por enviar"
    assert "Estudiante: no entiendo el paso dos" in await driver.read_page(max_chars=None)


@pytest.mark.asyncio
async def test_una_contrasena_nunca_cuenta_como_texto_escrito(driver):
    """Lo que esta lectura devuelve termina en un prompt y en un log."""
    await driver.page.evaluate(
        """() => {
          const c = document.createElement('input');
          c.type = 'password'; c.value = 'no-va-a-ningun-lado';
          document.querySelector('main').prepend(c);
        }"""
    )
    assert "no-va-a-ningun-lado" not in await driver.pending_text()


@pytest.mark.asyncio
async def test_un_paso_de_escribir_que_tambien_envio_queda_dicho(driver):
    """El corte de la prueba de memoria: escribir Y enviar en el paso de escribir.

    El modelo de mentira hace lo que hizo el de verdad (escribe y toca Enviar);
    la lectura del DOM real es la que nota que el cuadro quedó vacío.
    """
    from lxp_eval.browser_agents.agent_driver import AgentDriver
    from lxp_eval.evalkit.steps import DEVIATION

    class _EscribeYEnvia(_Agente):
        async def run(self, *, system, user_task):
            self.tasks.append(user_task)
            if len(self.tasks) == 1:
                await driver.page.locator("#mensaje").click()
                await driver.type_into_focused("hola", delay_ms=0)
                await driver.click_by_role("button", "Enviar")
            return _Resultado(True, "Escribí el mensaje y lo envié.")

    agent = _EscribeYEnvia()
    ad = AgentDriver(agent, driver)

    out = await ad.do("En el cuadro para escribirle al tutor, escribir exactamente: hola")
    assert out.startswith(DEVIATION) and "sólo pedía escribir" in out

    await ad.do("Enviar ese mensaje al tutor (ejemplo: botón 'Enviar').")
    assert "ANTES DE ENVIAR" in agent.tasks[1]
    assert "Escribí el mensaje y lo envié." in agent.tasks[1]
