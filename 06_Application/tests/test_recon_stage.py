"""La etapa `recon`: conectarse ANTES de gastar.

El orden del pipeline era `framework -> probes -> awaiting_approval -> run`, y
el navegador aparecía recién en `run`. Una contraseña mal tipeada costaba, en
ese orden: una construcción de rúbrica (llamadas al modelo), un consejo de
cinco jueces por cada prueba observable (muchas más), y sólo entonces un
Chromium que se estrellaba contra la pantalla de acceso. El usuario pagaba
todo eso para enterarse de algo que se sabía en el primer segundo.

`recon` corre primera y se conecta. Si la plataforma no acepta la cuenta de
prueba, el trabajo muere ahí y NO se llamó al modelo ni una vez.

Esa garantía es lo que se prueba acá, y se prueba con un cliente HOSTIL: no uno
que cuenta llamadas para que alguien afirme `== 0` al final -- eso deja pasar el
caso en que el trabajo llama, traga la excepción y sigue -- sino uno que revienta
EN EL MOMENTO del uso, con el nombre del llamador en el traceback.

Los dobles siguen el patrón de `tests/test_service.py` (no hay conftest con
fixtures compartidas). Lo que un doble NO puede probar: que el Chromium real
falle donde este `driver_factory` falla. Eso lo cubre
`test_service.py::test_the_driver_factory_names_a_missing_playwright_in_spanish`.
"""
import json

import pytest
from fastapi.testclient import TestClient

from lxp_eval.evalkit.session import SessionExpired
from lxp_eval.service import Engine, create_app
from lxp_eval.service.crypto import generate_key
from lxp_eval.service.jobs import Stage

KEY = generate_key()
TOKEN = "token-de-servicio-solo-para-tests-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

USER = "evaluador@ejemplo.org"
PASSWORD = "una-clave-mal-tipeada"

#: Lo que la plataforma contesta cuando la cuenta de prueba no entra. Es el
#: texto que el usuario tiene que leer, así que el test lo persigue literal.
NO_ENTRA = (
    "la plataforma no aceptó las credenciales de la cuenta de prueba: no hay "
    "ninguna señal positiva de que la sesión esté abierta"
)


class RefusingClient:
    """Un cliente de LLM que se niega a ser usado.

    Contar llamadas y afirmar `== 0` al final deja pasar el caso en que el
    trabajo llama y traga la excepción. Fallar EN el momento del uso dice
    exactamente quién lo llamó, en el traceback.
    """

    @property
    def messages(self):
        raise AssertionError(
            "se llamó al LLM en un trabajo cuyo login falló: el reconocimiento "
            "tiene que correr ANTES y fallar gratis"
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("EVALKIT_SECRET_KEY", KEY)
    monkeypatch.setenv("EVALKIT_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "no-se-usa-en-tests")
    return None


def _client(tmp_path, engine):
    app = create_app(root=tmp_path / "runs", engine=engine, background=False)
    return TestClient(app, headers=dict(AUTH))


def _start(client):
    body = {
        "objective": "Un tutor de IA de matemática para 1.º a 6.º grado.",
        "areas": ["D9"],
        "platform": {
            "url": "https://plataforma.test",
            "username": USER,
            "password": PASSWORD,
        },
        "documents": [],
    }
    r = client.post("/jobs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def test_recon_is_declared_everywhere_a_stage_has_to_be():
    """Una etapa a medio declarar deja al watchdog sin timeout para ella."""
    from lxp_eval.service.watchdog import ACTIVE_STAGES, ALL_STAGES, Stage

    assert Stage.RECON == "recon"
    assert Stage.RECON in ALL_STAGES
    assert Stage.RECON in ACTIVE_STAGES


def test_a_login_that_fails_costs_zero_llm_calls(tmp_path, env):
    """LA razón de ser del reordenamiento. Si esto se rompe, se rompió el ahorro.

    Un refactor que vuelva a poner `framework` antes que `recon` no falla acá
    con un contador en cero: falla con el `AssertionError` de `RefusingClient`,
    dentro del POST, señalando la línea que llamó al modelo.
    """
    def driver_factory(record, credentials, artifacts_dir):
        raise SessionExpired(NO_ENTRA)

    engine = Engine(
        client_factory=RefusingClient,
        driver_factory=driver_factory,
        # Si el pipeline llegara hasta acá, ya habría gastado. Que lo diga.
        build_framework=lambda *a, **k: pytest.fail(
            "se construyó la rúbrica en un trabajo cuyo login falló"
        ),
    )
    c = _client(tmp_path, engine)
    job_id = _start(c)

    state = c.get(f"/jobs/{job_id}").json()
    assert state["stage"] == Stage.ERROR
    assert state["error"]["stage"] == Stage.RECON
    assert "credenciales" in state["error"]["message"]
    # Es una falla de SESIÓN, no un veredicto sobre la plataforma.
    assert state["error"]["sessionLost"] is True
    # Y el cliente hostil no dejó su huella en ninguna parte: si alguien lo
    # hubiera llamado y se hubiera comido la excepción, el mensaje del trabajo
    # sería el suyo y no el de la plataforma.
    assert "se llamó al LLM" not in json.dumps(state, ensure_ascii=False)
    c.close()


def test_recon_runs_before_the_framework_stage(tmp_path, env):
    """El orden, afirmado directamente y no sólo por el lado del fallo.

    El mapa de superficies que produce esta etapa va a decidir qué criterios
    tienen sentido pedirle a ESTA plataforma, así que la rúbrica lo necesita
    construido antes. Un `recon` que corriera después seguiría ahorrando en el
    caso del login roto y sería inútil para lo otro.
    """
    order = []

    class Driver:
        async def assert_session_alive(self):
            order.append("recon")

    def driver_factory(record, credentials, artifacts_dir):
        return Driver()

    def build(*a, **k):
        order.append("framework")
        raise RuntimeError("basta: ya se probó lo que había que probar")

    engine = Engine(
        client_factory=lambda: object(),
        driver_factory=driver_factory,
        build_framework=build,
        retry=__import__(
            "lxp_eval.service.retry", fromlist=["RetryPolicy"]
        ).RetryPolicy(max_attempts=1),
    )
    c = _client(tmp_path, engine)
    _start(c)

    assert order == ["recon", "framework"]
    c.close()


def test_recon_spend_is_folded_into_the_record(tmp_path, env):
    """El recorrido de recon gasta plata del agente (~$0.37 medidos contra
    Plataforma A) y esa cifra no se plegaba al registro: los $8.31 del build medido
    eran ~$8.70 reales. El driver lleva la cuenta; el registro tiene que
    heredarla aunque el trabajo muera en la etapa siguiente."""
    class Driver:
        spend_usd = 0.38
        cache_read_tokens = 163_099
        cache_write_tokens = 12_345

        async def assert_session_alive(self):
            pass

    def driver_factory(record, credentials, artifacts_dir):
        return Driver()

    def build(*a, **k):
        raise RuntimeError("basta: el gasto ya deberia estar plegado")

    engine = Engine(
        client_factory=lambda: object(),
        driver_factory=driver_factory,
        build_framework=build,
        retry=__import__(
            "lxp_eval.service.retry", fromlist=["RetryPolicy"]
        ).RetryPolicy(max_attempts=1),
    )
    c = _client(tmp_path, engine)
    job_id = _start(c)

    state = c.get(f"/jobs/{job_id}").json()
    assert state["spendUsd"] == pytest.approx(0.38), \
        "el gasto del recorrido de recon no se plego al registro"
    assert state["cacheReadTokens"] == 163_099
    c.close()
