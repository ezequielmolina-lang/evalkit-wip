"""Qué se saltea cuando faltan los datos de una evaluación real, y por qué.

Este repositorio trae la HERRAMIENTA. La evaluación real -- los `config.yaml` de
las plataformas medidas, sus mapas de superficies y la evidencia capturada -- no
se publica, así que las pruebas que leen esos archivos no tienen contra qué
correr. Saltan con motivo, no fallan: una suite en rojo al clonar diría que la
herramienta está rota, y no lo está.

CÓMO SE MARCAN. Con `@pytest.mark.needs_real_platform`, sobre la prueba misma.
Antes era una lista de NOMBRES acá adentro, y esa lista tenía una falla callada:
renombrar una prueba la sacaba de la lista sin que nadie se enterara, y la
prueba empezaba a correr contra datos que no existen (o, peor, una prueba nueva
con el nombre viejo quedaba salteada para siempre). Un marcador viaja con la
función: no hay forma de renombrarla y perderlo.

CÓMO CORRERLAS. Con tu propia plataforma en `platforms/<nombre>/config.yaml`
(hay un ejemplo en `platforms/ejemplo/`), y después:

    pytest --real-platform          # o EVALKIT_REAL_PLATFORM=1 pytest

No se activa solo al detectar un `platforms/<algo>/`: algunas de estas pruebas
leen además evidencia capturada y `scripts/`, que es el andamio de la evaluación
y no viaja con el repo. Que la decisión sea explícita evita prometer un verde
que depende de archivos que nadie puede ver.

QUÉ SE PIERDE MIENTRAS TANTO, dicho en voz alta: entre las salteadas está
`test_the_vendor_and_the_people_are_gone`, que es la prueba de que la
anonimización del export funcionó. En un clon público esa garantía no la
verifica nadie.
"""
import os

import pytest

MARCADOR = "needs_real_platform"


def pytest_addoption(parser):
    parser.addoption(
        "--real-platform",
        action="store_true",
        default=False,
        help=(
            "correr también las pruebas que necesitan los datos de una "
            "evaluación real (ver conftest.py)"
        ),
    )


def _hay_datos_reales(config) -> bool:
    return bool(
        config.getoption("--real-platform")
        or os.environ.get("EVALKIT_REAL_PLATFORM")
    )


def pytest_collection_modifyitems(config, items):
    if _hay_datos_reales(config):
        return
    saltear = pytest.mark.skip(
        reason=(
            "necesita datos de una evaluación real, que este repo no publica "
            "(corré con --real-platform si los tenés)"
        )
    )
    for item in items:
        if MARCADOR in item.keywords:
            item.add_marker(saltear)
