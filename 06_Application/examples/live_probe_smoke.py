"""Prueba end-to-end del camino automatizado: sesión -> probe headless -> evidencia.

Esto es lo que reemplaza al demo manual con la extensión de Chrome: después del
login (una sola vez, a mano si la plataforma usa SSO), el proceso corre headless
y sin humano.

    # 1) primera vez, o cuando la sesión venció: login (abre navegador visible)
    python examples/live_probe_smoke.py --platform plataforma_a --role student --login

    # 2) después, todo headless:
    python examples/live_probe_smoke.py --platform plataforma_a --role student \
        --url https://app.plataforma-a.ejemplo/

Los pasos del probe usan el verbo-set determinístico del driver (goto / click /
fill / press / wait / expect_text). Un paso que el driver no entiende FALLA en
vez de saltearse: un paso no ejecutado dejaría la página en otro estado y la
captura siguiente sería evidencia falsa.

Las credenciales salen del entorno (PLATAFORMA_A_STUDENT_EMAIL / PLATAFORMA_A_STUDENT_PASSWORD,
etc.). Nunca se imprimen ni se guardan.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lxp_eval.browser_agents.agent_driver import driver_kind_for, make_driver
from lxp_eval.browser_agents.claude_browser_agent import ClaudeBrowserAgent
from lxp_eval.browser_agents.playwright_driver import PlaywrightDriver
from lxp_eval.evalkit.ledger import EvidenceLedger
from lxp_eval.evalkit.probes import Probe
from lxp_eval.evalkit.runner import run_probe
from lxp_eval.evalkit.session import (
    SessionExpired,
    SessionStore,
    auto_login,
    interactive_login,
    known_platforms,
    login_url_for,
)

RUNS = Path(__file__).resolve().parents[1] / "runs" / "live_probe_smoke"


def smoke_probe(url: str) -> Probe:
    """One hand-written probe: open the app and prove we are inside it."""
    return Probe(
        probe_id="SMOKE-1",
        test_id="0.0.1",
        goal="Comprobar que la sesión guardada abre la plataforma sin humano",
        steps=[f"goto {url}", "wait 3"],
        verdict_rule="PASA si la página carga autenticada (no es una URL de login)",
        capture=["screenshot", "page_text"],
    )


async def do_login(platform: str, role: str) -> Path:
    """Try the automatic login; fall back to a visible browser for SSO/2FA."""
    store = SessionStore(platform, role)
    login_url = login_url_for(platform)

    # 1) automatic (headless, no human)
    driver = PlaywrightDriver(RUNS / "shots", headless=True, platform=platform)
    await driver.launch()
    try:
        if await auto_login(platform, role, driver.page, context=driver.context):
            path = await driver.save_storage_state(store.path)
            print(f"Login automático OK -> {path.name}")
            return path
        print("Login automático no prosperó (probablemente SSO). Abro navegador visible...")
    finally:
        await driver.close()

    # 2) interactive (headful, human logs in once)
    driver = PlaywrightDriver(RUNS / "shots", headless=False, platform=platform)
    await driver.launch()
    try:
        return await interactive_login(
            login_url, store, page=driver.page, context=driver.context, timeout_s=300
        )
    finally:
        await driver.close()


async def do_run(platform: str, role: str, url: str, driver_kind: str = "auto") -> None:
    store = SessionStore(platform, role)
    if not store.exists():
        raise SystemExit(
            f"No hay sesión guardada para {platform}/{role}. "
            f"Corré primero:  --platform {platform} --role {role} --login"
        )

    probe = smoke_probe(url)
    if driver_kind == "auto":
        driver_kind = driver_kind_for(probe.steps)
    print(f"Driver: {driver_kind} (pasos {'DSL' if driver_kind == 'dsl' else 'en prosa'})")

    ledger = EvidenceLedger(RUNS / "ledger.jsonl")
    driver = PlaywrightDriver(
        RUNS / "shots", headless=True, storage_state=store.path, platform=platform
    )
    await driver.launch()
    try:
        await driver.navigate(url)
        agent = ClaudeBrowserAgent(driver) if driver_kind == "agent" else None
        probe_driver = make_driver(driver_kind, driver=driver, agent=agent)
        run = await run_probe(probe, probe_driver, ledger, platform=platform)
    except SessionExpired as exc:
        raise SystemExit(f"Sesión vencida: {exc}\nVolvé a correr con --login.") from exc
    finally:
        await driver.close()

    print(f"Probe {run.probe_id} OK — {len(run.artifacts)} artefactos:")
    for a in run.artifacts:
        print(f"  {a}")
    print(f"Ledger: {ledger.path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", default="plataforma_a", choices=known_platforms())
    ap.add_argument("--role", default="student", choices=("student", "teacher"))
    ap.add_argument("--url", default=None, help="URL a abrir en el probe")
    ap.add_argument("--login", action="store_true", help="establecer/renovar la sesión")
    ap.add_argument(
        "--driver", default="auto", choices=("auto", "agent", "dsl"),
        help="auto: DSL si todos los pasos parsean, si no el agente",
    )
    args = ap.parse_args()

    if args.login:
        asyncio.run(do_login(args.platform, args.role))
        return
    asyncio.run(
        do_run(args.platform, args.role, args.url or login_url_for(args.platform), args.driver)
    )


if __name__ == "__main__":
    main()
