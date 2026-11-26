"""Armar el repo público de evalkit: la herramienta, sin la evaluación.

    cd 06_Application
    python -m lxp_eval.public_repo ../../evalkit-publico

QUÉ SE PUBLICA Y QUÉ NO. Este repositorio tiene dos cosas encima: una
herramienta de evaluación, que siempre estuvo pensada para ser abierta (por eso
hay licencia MIT), y la evaluación REAL de dos proveedores con nombre y
apellido, hecha para un ministerio y todavía sin comunicar al proveedor. Lo
primero se publica. Lo segundo no se publica hoy y quizá no se publique nunca,
y no es una decisión que tome un script.

Por eso esto NO es un `git push` con exclusiones: construye un árbol nuevo con
una lista blanca. Una lista negra falla abierta -- un archivo nuevo que nadie
agregó a la lista se publica solo -- y acá fallar abierto significa publicar los
correos de nueve personas reales y una comparación cabeza a cabeza sin publicar.

QUÉ SE ANONIMIZA. El nombre del proveedor y sus dominios, en comentarios,
docstrings y bancos de pruebas. No se borra la lección: «el /login/ de
Plataforma A es un selector de proveedor» sigue explicando por qué existe la
guarda, que es para lo que están esos comentarios. Los `platforms/<real>/` se
reemplazan por un `platforms/ejemplo/` con la misma estructura y datos
inventados, que además es mejor documentación: muestra cómo agregar una
plataforma sin exponer la tuya.

Al final vuelve a leer TODO lo que escribió y busca fugas. Si encuentra una,
falla: un repo que se publica a medias anonimizado es peor que ninguno.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from .web_sample import REPLACEMENTS

#: Lista BLANCA, relativa a la raíz del proyecto. Nada fuera de acá viaja.
INCLUDE = [
    "LICENSE",
    "06_Application/lxp_eval",
    "06_Application/tests",
    "06_Application/examples",
    "06_Application/evalkit_app.py",
    "06_Application/pytest.ini",
    "06_Application/requirements.txt",
    "06_Application/requirements-service.txt",
    "06_Application/README.md",
    "web/app",
    "web/package.json",
    "web/package-lock.json",
    "web/next.config.mjs",
    "web/tsconfig.json",
    "web/middleware.ts",
    "web/next-env.d.ts",
    "web/public",
]

#: Dentro de lo incluido, esto igual se cae: configs de plataformas reales,
#: caché, y los 136 scripts de un solo uso que son el andamio de la evaluación.
EXCLUDE_PARTS = {
    "__pycache__", "node_modules", ".next", ".pytest_cache", ".turbo",
    "tsconfig.tsbuildinfo",
}
EXCLUDE_DIRS = {"platforms", "runs", "outputs", "scripts", "dashboard"}

#: Archivos cuyo NOMBRE nombra al proveedor. Se copian con otro nombre; el
#: contenido ya lo arregla la anonimización, y los imports también, porque el
#: módulo se menciona como texto dentro de los .py.
RENAME = {"plataforma_a_recipes.py": "plataforma_a_recipes.py"}

#: Extensiones que se leen como texto para anonimizar. El resto se copia igual.
TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".yaml", ".yml",
                 ".md", ".txt", ".css", ".html"}

#: Lo que NO puede quedar en el árbol publicado. Se verifica al final.
LEAKS = [
    (re.compile(r"[a-zA-Z0-9._%+-]+@(?!ejemplo\.org|example\.com)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
     "una dirección de correo real"),
    (re.compile(r"plataforma_a", re.I), "el nombre del proveedor"),
    (re.compile(r"plataforma_b", re.I), "el nombre del segundo proveedor"),
    (re.compile(r"ejemplo", re.I), "la organización"),
]

EJEMPLO_CONFIG = """\
# Cómo evalkit entra a UNA plataforma, y cómo sabe que entró.
#
# Copiá este archivo a platforms/<tu-plataforma>/config.yaml y editalo. Agregar
# una plataforma tiene que ser una edición ACÁ, nunca un cambio de código.
name: Plataforma de ejemplo
vendor: Proveedor de ejemplo
url: https://www.ejemplo.org
status: piloto

# A qué hosts puede ir el navegador mientras evalúa. Los de `url` y de
# `auth.login_url` ya cuentan. Agregá acá sólo los que la plataforma use de
# verdad: un proveedor de identidad, el sitio donde vive la política de
# privacidad. A cualquier otro el navegador se niega, así que un paso sin
# dirección no puede terminar en un sitio ajeno.
allowed_hosts: []

auth:
  login_url: "https://app.ejemplo.org/login/"

  # Presente => seguro NO autenticado. Poné acá lo que ves en el muro de login.
  # Ojo: si el login es un SELECTOR DE PROVEEDOR ("Continuar con email /
  # Google"), el campo de contraseña no existe todavía, así que buscar sólo
  # input[type='password'] no encuentra nada y la compuerta queda inerte.
  logged_out_selectors:
    - "input[type='password']"
    - "button:has-text('Continue with email')"

  # Presente => seguro autenticado. Sacalos del DOM ya logueado, no los
  # adivines: sin señal positiva, "el formulario no está" también es lo que
  # parece una página de error, y eso reporta una contraseña mala como éxito.
  authenticated_selectors:
    - "[data-testid='user-profile']"

  # Clics que REVELAN el formulario, para logins que llegan de a un campo.
  before_email_clicks:
    - "button:has-text('Continue with email')"
  before_password_clicks:
    - "button:has-text('Next')"
  submit_selectors:
    - "button:has-text('Log in')"

  session_cookies: []

  # Una cuenta con varios roles: el rol suele viajar DENTRO del token de sesión,
  # así que hay que fijarlo antes de guardar la sesión. Sin esto el motor puede
  # recorrer la interfaz equivocada y devolver NO_EVALUADO sobre criterios que
  # estaban a un clic.
  role_switch:
    open: "[data-testid='user-profile']"
    choose:
      student: "Student"
      teacher: "Teacher"
    lands_on:
      student: "https://app.ejemplo.org/student/home/"
      teacher: "https://app.ejemplo.org/home/"

# CÓMO SE USA LA PLATAFORMA. Sin esto la evaluación supone que la plataforma
# enseña sola, y un criterio que mira algo que hace el docente (explicar antes
# de preguntar, resolver un ejemplo en la pizarra, decidir el orden de los
# temas) termina como una falla de la plataforma. Tres valores:
#   tutor_autonomo    la plataforma enseña sola
#   capa_de_practica  el docente explica y modela; la plataforma hace la práctica
#   hibrido           el reparto es propio y se dice en quien_hace_que
modelo_de_uso:
  tipo: tutor_autonomo
  quien_hace_que: ""
  # Opcional: un documento de uso previsto (la guía docente del producto),
  # junto a este archivo. Es la fuente de verdad del reparto.
  # documento: guia_docente.md

# EN QUÉ PAÍS Y MATERIA. Los datos por país viven acá y no en el criterio: un
# banco escrito para otro país trae sus líneas de crisis, su currículo y sus
# idiomas, y hay que poder contrastarlos. Un criterio que nombra una línea que
# no figura acá queda sin evaluar hasta que se declare.
contexto:
  pais: ""
  materia: ""
  nivel: ""
  idiomas: []
  curriculo: ""
  lineas_de_crisis: []
  #  - numero: "000"
  #    nombre: "Línea de ayuda del país"

vendor_docs: []
observation_notes: []
"""


#: Pruebas que dependen de datos que este repo NO publica: los config de las
#: plataformas reales, sus mapas de superficies y la evidencia de las corridas.
#: No están rotas -- están midiendo algo que acá no existe -- así que saltan con
#: el motivo escrito, en vez de fallar y dar la impresión de una herramienta que
#: no funciona. Con los datos presentes (el repo interno) corren y pasan.
CONFTEST = '''\
"""Pruebas que necesitan datos que este repo no publica.

Este repositorio trae la HERRAMIENTA. La evaluación real -- los `config.yaml`
de las plataformas medidas, sus mapas de superficies y la evidencia capturada --
no se publica, así que las pruebas que leen esos archivos no tienen contra qué
correr. Saltan con motivo, no fallan: una suite en rojo al clonar diría que la
herramienta está rota, y no lo está. Poné tu propia plataforma en
`platforms/<nombre>/config.yaml` (hay un ejemplo en `platforms/ejemplo/`) y las
que dependen de configuración empiezan a correr solas.
"""
import pytest

SIN_DATOS = {
    "test_navigation_comes_from_the_platform_not_the_probe",
    "test_a_sequence_becomes_one_send_and_one_read_per_stimulus",
    "test_an_unavailable_surface_refuses_instead_of_substituting",
    "test_a_read_only_probe_still_reads_the_surface",
    "test_a_privacy_probe_reads_the_published_policy_not_the_app",
    "test_detect_course_passes_on_the_captured_plataforma_a_fixture",
    "test_shipped_evidence_map_loads_and_every_artifact_exists",
    "test_dotenv_is_read_from_the_same_file_config_uses",
    "test_the_shipped_platform_configs_carry_their_auth_block",
    "test_plataforma_a_can_tell_it_is_logged_out_on_a_provider_chooser",
    "test_the_chooser_steps_are_read_from_the_plataforma_a_config",
    "test_the_role_switch_is_read_from_the_plataforma_a_config",
    "test_the_vendor_and_the_people_are_gone",
    # Necesitan el surfaces.yaml de una plataforma medida.
    "test_a_banked_probe_is_used_verbatim_not_redrafted",
    "test_a_criterion_without_a_banked_probe_still_drafts_normally",
    # Importa desde scripts/, que es el andamio de la evaluación y no viaja.
    "test_operative_bank_text_is_in_spanish",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.name.split("[")[0] in SIN_DATOS:
            item.add_marker(
                pytest.mark.skip(
                    reason="necesita datos de una evaluación real, que este "
                    "repo no publica"
                )
            )
'''


#: Lo que `web_sample.REPLACEMENTS` no cubre, porque aquél sólo prepara el
#: ejemplo de UNA plataforma. Acá viaja el repo entero: el segundo proveedor, la
#: organización y el ministerio aparecen en comentarios, bancos y ejemplos.
#: Más largo primero, para que un prefijo no le gane al nombre completo.
EXTRA_REPLACEMENTS: list[tuple[str, str]] = [
    # Mayúsculas primero: son nombres de variables de entorno
    # (PLATAFORMA_A_STUDENT_EMAIL), y `credentials_from_env` las arma con
    # platform.upper(), así que el nombre nuevo tiene que seguir cerrando.
    ("plataforma_a_recipes", "plataforma_a_recipes"),
    ("PLATAFORMA_A", "PLATAFORMA_A"),
    ("PLATAFORMA_B", "PLATAFORMA_B"),
    ("app.plataforma-b.ejemplo", "app.plataforma-b.ejemplo"),
    ("plataforma-b.ejemplo", "plataforma-b.ejemplo"),
    ("Plataforma B", "Plataforma B"),
    ("plataforma_b", "plataforma-b"),
    ("la organización", "la organización"),
    ("Clase de prueba", "la organización"),
    ("ejemplo.org", "ejemplo.org"),
    ("EJEMPLO", "EJEMPLO"),
    ("ejemplo", "ejemplo"),
    ("ministerio.ejemplo", "ministerio.ejemplo"),
    ("el ministerio", "el ministerio"),
    ("el país", "el país"),
    ("ElPais", "ElPais"),
    ("del país", "del país"),
    ("del país", "del país"),
    ("del país", "del país"),
    ("el ministerio", "el ministerio"),
]


#: El nombre suelto en minúsculas NO puede volverse "plataforma-a": aparece
#: dentro de identificadores de Python (`def test_plataforma_a_...`) y un guión ahí es
#: un error de sintaxis, así que el árbol publicado no compilaba. Con guión bajo
#: sirve para identificadores, y las formas de dominio ya se reemplazaron antes
#: en la lista, que es donde el guión sí corresponde.
_IDENT_SAFE = {"plataforma_a": "plataforma_a", "plataforma_b": "plataforma_b"}


def anonymise_text(text: str) -> str:
    for old, new in REPLACEMENTS + EXTRA_REPLACEMENTS:
        text = text.replace(old, _IDENT_SAFE.get(old, new))
    # Cualquier correo que sobreviva a la lista se neutraliza igual.
    return re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
                  "evaluador@ejemplo.org", text)


def _skip(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & EXCLUDE_PARTS) or bool(parts & EXCLUDE_DIRS)


def copy_tree(root: Path, dest: Path) -> tuple[int, int]:
    copiados = anonimizados = 0
    for rel in INCLUDE:
        src = root / rel
        if not src.exists():
            print(f"  (falta, se saltea) {rel}")
            continue
        sources = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
        for p in sources:
            r = p.relative_to(root)
            if _skip(r):
                continue
            out = dest / r
            if out.name in RENAME:
                out = out.with_name(RENAME[out.name])
            out.parent.mkdir(parents=True, exist_ok=True)
            if p.suffix.lower() in TEXT_SUFFIXES:
                try:
                    original = p.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    shutil.copy2(p, out)
                    copiados += 1
                    continue
                limpio = anonymise_text(original)
                out.write_text(limpio, encoding="utf-8")
                copiados += 1
                anonimizados += limpio != original
            else:
                shutil.copy2(p, out)
                copiados += 1
    return copiados, anonimizados


def scan_for_leaks(dest: Path) -> list[str]:
    """Volver a leer todo lo escrito y buscar lo que no puede estar."""
    hallazgos = []
    for p in dest.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if "node_modules" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx, que in LEAKS:
            for m in rx.finditer(text):
                linea = text[: m.start()].count("\n") + 1
                hallazgos.append(
                    f"{p.relative_to(dest)}:{linea}  {que}: {m.group(0)[:60]!r}"
                )
    return hallazgos


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dest", help="directorio nuevo donde armar el repo público")
    ap.add_argument("--root", default="..", help="raíz del proyecto")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} ya existe y no está vacío; borralo o elegí otro",
              file=sys.stderr)
        return 1
    dest.mkdir(parents=True, exist_ok=True)

    print(f"raíz  : {root}\ndestino: {dest}\n")
    copiados, anonimizados = copy_tree(root, dest)

    cfg = dest / "06_Application" / "platforms" / "ejemplo"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(EJEMPLO_CONFIG, encoding="utf-8")

    shutil.copy2(root / ".gitignore", dest / ".gitignore")
    (dest / "06_Application" / "conftest.py").write_text(CONFTEST, encoding="utf-8")

    print(f"\narchivos copiados : {copiados}")
    print(f"archivos con algo anonimizado: {anonimizados}")

    fugas = scan_for_leaks(dest)
    if fugas:
        print(f"\nFUGAS: {len(fugas)}. NO publicar así.")
        for f in fugas[:30]:
            print("  " + f)
        if len(fugas) > 30:
            print(f"  … y {len(fugas) - 30} más")
        return 2
    print("\nsin fugas: ni correos reales, ni nombres de proveedor, ni la organización")
    return 0


if __name__ == "__main__":
    sys.exit(main())
