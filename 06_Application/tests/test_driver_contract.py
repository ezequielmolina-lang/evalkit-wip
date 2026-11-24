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
