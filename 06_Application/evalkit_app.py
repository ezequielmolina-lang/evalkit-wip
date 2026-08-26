"""Un solo comando: levanta evalkit y abre el navegador.

    python evalkit_app.py

POR QUÉ EXISTE. Hasta ahora arrancar evalkit era: exportar variables, levantar
uvicorn en una terminal, `npm run dev` en otra, y saber que hay una contraseña
de sitio. Eso lo puede hacer su autor y nadie más — y el proyecto va a ser
abierto. Un desconocido que clona el repo tiene que poder correr UNA cosa.

DECISIONES DELIBERADAS:

* **Se ata a 127.0.0.1, nunca a 0.0.0.0.** Este servicio maneja credenciales de
  plataformas ajenas y puede gastar la API key de quien lo corre. Escuchar en
  todas las interfaces por comodidad es cómo un servicio local termina expuesto
  en una red de oficina. Si alguien quiere exponerlo, que sea una decisión suya
  y explícita, no el default.

* **El token de servicio se genera solo si falta.** El motor se niega a arrancar
  sin `EVALKIT_SERVICE_TOKEN` (un secreto ausente es una mala configuración, no
  un modo permisivo). Para una instalación nueva eso sería un muro sin sentido:
  se genera uno aleatorio para esta sesión y se le pasa a los dos procesos.

* **No inventa la API key.** Si no hay ninguna configurada, la app arranca
  igual y lo dice en su pantalla de configuración: es ahí donde el usuario pega
  la suya. Fallar al arrancar por eso obligaría a editar archivos, que es
  justamente lo que este comando viene a eliminar.

* **Los hijos se matan al salir.** Un uvicorn huérfano deja el puerto tomado y
  la próxima corrida falla con un error que no tiene nada que ver.
"""
from __future__ import annotations

import atexit
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR.parent / "web"

ENGINE_PORT = int(os.getenv("EVALKIT_ENGINE_PORT", "8811"))
WEB_PORT = int(os.getenv("EVALKIT_WEB_PORT", "3210"))

_children: list[subprocess.Popen] = []


def _free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _stop_children() -> None:
    for p in _children:
        if p.poll() is None:
            p.terminate()
    for p in _children:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def _wait_until_up(port: int, what: str, timeout: float = 90.0) -> bool:
    """Espera a que el puerto conteste. Se sondea el puerto y no se duerme un
    tiempo fijo: una máquina lenta no tiene por qué fallar, y una rápida no
    tiene por qué esperar de más."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _free(port):
            return True
        time.sleep(0.25)
        if any(p.poll() is not None for p in _children):
            return False
    print(f"  {what} no respondió en {timeout:.0f}s")
    return False


def main() -> int:
    env = dict(os.environ)

    # Un secreto ausente es mala configuración para un servicio expuesto, pero
    # para una instalación local nueva es sólo fricción: se genera y se comparte
    # entre los dos procesos, que es exactamente su propósito.
    if not env.get("EVALKIT_SERVICE_TOKEN"):
        env["EVALKIT_SERVICE_TOKEN"] = secrets.token_urlsafe(32)
        print("· token de servicio generado para esta sesión")
    if not env.get("EVALKIT_SECRET_KEY"):
        from lxp_eval.service.crypto import generate_key

        env["EVALKIT_SECRET_KEY"] = generate_key()
        print("· clave de cifrado generada para esta sesión")
    # Sin esto la web pide una contraseña de sitio que en local no protege
    # nada (ver la nota en web/middleware.ts). El lanzador ata todo a 127.0.0.1.
    env["EVALKIT_LOCAL"] = "1"
    env.setdefault("ENGINE_URL", f"http://127.0.0.1:{ENGINE_PORT}")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    if not _free(ENGINE_PORT):
        print(f"✗ el puerto {ENGINE_PORT} ya está ocupado — ¿hay otro evalkit corriendo?")
        return 1

    atexit.register(_stop_children)

    print(f"· motor      http://127.0.0.1:{ENGINE_PORT}")
    _children.append(subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "lxp_eval.service.app:create_app",
         "--factory", "--host", "127.0.0.1", "--port", str(ENGINE_PORT)],
        cwd=APP_DIR, env=env,
    ))
    if not _wait_until_up(ENGINE_PORT, "el motor"):
        print("✗ el motor no arrancó")
        return 1

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm or not (WEB_DIR / "package.json").exists():
        print("✗ falta npm o la carpeta web/ — el motor quedó corriendo solo")
        print(f"  API: http://127.0.0.1:{ENGINE_PORT}")
        _children[0].wait()
        return 0

    if not (WEB_DIR / "node_modules").exists():
        print("· instalando dependencias de la web (una sola vez)…")
        subprocess.run([npm, "install"], cwd=WEB_DIR, env=env, check=False)

    print(f"· interfaz   http://127.0.0.1:{WEB_PORT}")
    _children.append(subprocess.Popen(
        [npm, "run", "dev", "--", "--port", str(WEB_PORT)],
        cwd=WEB_DIR, env=env,
    ))
    if _wait_until_up(WEB_PORT, "la interfaz"):
        url = f"http://127.0.0.1:{WEB_PORT}"
        print(f"\n  evalkit está andando: {url}")
        print("  (Ctrl+C para cortar)\n")
        webbrowser.open(url)

    try:
        while all(p.poll() is None for p in _children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n· cortando…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
