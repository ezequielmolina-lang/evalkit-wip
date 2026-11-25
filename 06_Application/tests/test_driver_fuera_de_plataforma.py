"""El bloqueo de salida de dominio, contra un Chromium DE VERDAD y sin red.

POR QUÉ NO ALCANZA CON EL DOBLE. `navigate()` se puede probar con un doble: es
una comparación de texto antes de tocar el navegador. Pero el 2026-11-25 el
agente no sólo navegó a una dirección inventada: una vez afuera, siguió
haciendo clics. Un enlace o un botón que lleva a otro sitio no pasa por
`navigate()`; pasa por el navegador. Que ESE camino también esté cerrado sólo
se ve con un navegador real, que es lo que este archivo usa.

Sin red: las dos direcciones son `.test` (reservado, no resuelve). La permitida
la sirve la propia prueba con una ruta de Playwright; la ajena tiene que morir
antes de salir, así que nunca llega a buscarse en el DNS.
"""
from __future__ import annotations

import os

import pytest

from lxp_eval.browser_agents.playwright_driver import (
    OffPlatformNavigation,
    PlaywrightDriver,
)

PERMITIDA = "http://plataforma.test/"
AJENA = "http://ajena.test/destino"

PAGINA = f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>Plataforma</title></head><body>
<h1>Inicio de la plataforma</h1>
<a id="afuera" href="{AJENA}">Salir a otro sitio</a>
<a id="adentro" href="{PERMITIDA}temas">Temas</a>
</body></html>"""


@pytest.fixture
async def driver(tmp_path):
    pytest.importorskip("playwright.async_api")
    d = PlaywrightDriver(
        screenshots_dir=tmp_path, headless=True, action_delay_s=0,
        allowed_hosts=["plataforma.test"],
    )
    try:
        await d.launch()
    except Exception as exc:  # noqa: BLE001
        # Misma regla que test_driver_contract.py: en CI un salteo silencioso
        # convierte el verde en una afirmación sobre los dobles y nada más.
        if os.environ.get("EVALKIT_REQUIRE_BROWSER"):
            raise RuntimeError(
                "EVALKIT_REQUIRE_BROWSER=1 y no hay Chromium: corré "
                f"`python -m playwright install chromium`. Causa: {exc}"
            ) from exc
        pytest.skip(f"no hay chromium: {exc}")

    async def _servir(route):
        await route.fulfill(status=200, content_type="text/html", body=PAGINA)

    await d._context.route("http://plataforma.test/**", _servir)
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_the_platform_itself_opens(driver):
    final = await driver.navigate(PERMITIDA)
    assert final.startswith(PERMITIDA)
    assert "Inicio de la plataforma" in await driver.page.inner_text("body")


@pytest.mark.asyncio
async def test_an_invented_address_never_leaves_the_browser(driver):
    await driver.navigate(PERMITIDA)
    with pytest.raises(OffPlatformNavigation):
        await driver.navigate(AJENA)
    assert driver.page.url.startswith(PERMITIDA), "el navegador se movió igual"


@pytest.mark.asyncio
async def test_a_link_that_leads_outside_is_stopped_too(driver):
    """El camino que `navigate()` no ve: un clic dentro de la página."""
    await driver.navigate(PERMITIDA)

    await driver.page.click("#afuera", no_wait_after=True)
    await driver.page.wait_for_timeout(500)

    assert driver.page.url.startswith(PERMITIDA), (
        f"el clic sacó al navegador de la plataforma: {driver.page.url}"
    )
    assert any("ajena.test" in u for u in driver.blocked_navigations), (
        "el bloqueo tiene que quedar anotado: una salida frenada es un hallazgo "
        "que el informe puede citar"
    )


@pytest.mark.asyncio
async def test_a_link_inside_the_platform_still_works(driver):
    await driver.navigate(PERMITIDA)
    await driver.page.click("#adentro")
    await driver.page.wait_for_load_state("domcontentloaded")
    assert driver.page.url == f"{PERMITIDA}temas"
    assert driver.blocked_navigations == []


# --- volver al inicio entre pruebas -----------------------------------------
#
# El piloto del 2026-11-25 mostró que las pruebas de una corrida se pisaban:
# compartían el contexto del navegador, y con él lo que la plataforma guarda
# del estudiante. `start_over` abre un contexto nuevo con el estado de entrada.
# Con un doble se prueba que se LLAMA; que de verdad borre lo que dejó la
# prueba anterior, conserve la sesión y mantenga el cerco sólo lo dice Chromium.


@pytest.fixture
async def driver_que_vuelve(driver):
    async def _servir(route):
        await route.fulfill(status=200, content_type="text/html", body=PAGINA)

    abrir = driver._open_context

    async def _abrir(state):
        context, page = await abrir(state)
        await context.route("http://plataforma.test/**", _servir)
        return context, page

    driver._open_context = _abrir
    return driver


async def _guardado(driver, clave):
    return await driver.page.evaluate("k => localStorage.getItem(k)", clave)


@pytest.mark.asyncio
async def test_starting_over_forgets_what_the_previous_probe_left(driver_que_vuelve):
    d = driver_que_vuelve
    await d.navigate(PERMITIDA)
    await d.page.evaluate("localStorage.setItem('sesion', 'abierta')")
    await d.remember_start()

    # Lo que deja una prueba: una pregunta a medias.
    await d.page.evaluate("localStorage.setItem('pendiente', 'pregunta 3')")
    await d.page.click("#adentro")

    assert await d.start_over(PERMITIDA) is True
    assert d.page.url == PERMITIDA
    assert await _guardado(d, "pendiente") is None, "la prueba siguiente hereda la anterior"
    assert await _guardado(d, "sesion") == "abierta", "volver al inicio no es salir de la sesión"


@pytest.mark.asyncio
async def test_the_fence_is_still_up_after_starting_over(driver_que_vuelve):
    d = driver_que_vuelve
    await d.navigate(PERMITIDA)
    await d.remember_start()
    await d.start_over(PERMITIDA)

    await d.page.click("#afuera", no_wait_after=True)
    await d.page.wait_for_timeout(500)
    assert d.page.url.startswith(PERMITIDA)
    assert any("ajena.test" in u for u in d.blocked_navigations)
    with pytest.raises(OffPlatformNavigation):
        await d.navigate(AJENA)


@pytest.mark.asyncio
async def test_a_new_context_without_the_session_is_not_kept(driver_que_vuelve):
    """Una plataforma que guarda la sesión donde el estado no llega: el contexto
    nuevo sería una pantalla de login. Se sigue en el anterior, vuelto al inicio."""
    d = driver_que_vuelve
    await d.navigate(PERMITIDA)
    await d.remember_start()
    await d.page.evaluate("localStorage.setItem('pendiente', 'pregunta 3')")
    anterior = d.context

    async def _sin_sesion():
        return d.context is not anterior

    d.is_logged_out = _sin_sesion

    assert await d.start_over(PERMITIDA) is False
    assert d.context is anterior, "se cambió a un contexto que no tiene la sesión"
    assert d.page.url == PERMITIDA
    assert await _guardado(d, "pendiente") == "pregunta 3"


# --- escribir en el campo que se pidió ----------------------------------------

INGRESO = """<!doctype html><html lang="es"><head><meta charset="utf-8"></head><body>
<h1>Hola</h1>
<p>Escribe tu nombre de pila para empezar.</p>
<input id="nombre" aria-label="Tu nombre">
<button id="empezar" disabled>Empezar</button>
<script>
  const campo = document.getElementById('nombre');
  campo.addEventListener('input', () => {
    document.getElementById('empezar').disabled = campo.value.trim() === '';
  });
</script>
</body></html>"""


@pytest.mark.asyncio
async def test_clicking_a_field_by_its_name_lands_on_the_field_not_on_text_that_mentions_it(driver):
    """El 2026-11-25 una prueba pidió hacer clic en "Tu nombre". El párrafo de
    arriba decía "Escribe tu nombre de pila", la estrategia de texto lo encontró
    primero, y lo escrito después no entró en ningún campo: tres intentos y la
    prueba cortada. Con un doble no se ve: hay que ver dónde cae el clic."""
    async def _ingreso(route):
        await route.fulfill(status=200, content_type="text/html", body=INGRESO)

    await driver._context.route("http://plataforma.test/ingreso", _ingreso)
    await driver.navigate(PERMITIDA + "ingreso")

    assert await driver.click_element_description("Tu nombre") is True
    assert await driver.type_into_focused("Evalkit dos", delay_ms=0) is True

    assert await driver.page.input_value("#nombre") == "Evalkit dos"
    assert await driver.page.is_enabled("#empezar"), "el botón se habilita cuando el nombre entra"
