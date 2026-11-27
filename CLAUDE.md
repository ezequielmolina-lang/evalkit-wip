# CLAUDE.md

Contexto para Claude Code en este repo. Corto a propósito: lo que está acá es lo
que se paga caro cuando se olvida.

## Qué es esto

evalkit recorre una plataforma de aprendizaje y entrega un diagnóstico donde
**cada puntaje cita la evidencia que lo sostiene**: una captura y un texto de
página, hasheados, reabribles un año después. El usuario es quien decide con
plata pública y después tiene que defender la decisión.

Dos mitades: `06_Application/` es el motor (Python, FastAPI) y `web/` es la
interfaz (Next.js) que lo maneja por HTTP. El motor no depende del front.

## Comandos

```bash
cd 06_Application
python -m pip install -r requirements.txt -r requirements-service.txt
python -m playwright install chromium     # los 7 tests de navegador lo necesitan
python -m pytest -q                       # ~950 en menos de un minuto
python -m pytest --real-platform          # además, las que necesitan datos no publicados
python -m lxp_eval.service.selfcheck      # ¿esta máquina puede correr un trabajo?
python evalkit_app.py                     # motor + web + navegador, un comando

cd ../web && npm ci && npx tsc --noEmit && npm run build
```

`requirements.lock` tiene las versiones exactas y es lo que se instala en
contenedor y en CI. `requirements.txt` flota a propósito: es el detector de
deriva.

## Invariantes

No se negocian. Cada una está acá porque romperla ya costó una corrida, plata o
una afirmación que no se podía sostener.

- **Un criterio que nadie pudo observar no es un cero.** Es `NO_EVALUADO`, con
  la evidencia que habría hecho falta. Un proveedor que esconde una superficie
  no puede puntuar mejor que uno que la expone.
- **El juez sólo cita artefactos que recibió.** Una ruta inventada no respalda
  un veredicto, y el código tiene que hacerlo imposible, no improbable.
- **Una consecuencia externa real no corre sin firma humana**, y la firma es
  por prueba, no por plan: datos de menores reales, escritura en una cuenta
  real, plata real, y sólo contra un entorno real; un demo o cuentas de prueba
  no frenan nada. Las pruebas de crisis y de seguridad se marcan, se avisan al
  proveedor y CORREN: iniciar una corrida es el consentimiento para ese estrés,
  y una prueba de seguridad que no se corre no mide nada. El aviso se contrafirma
  siempre; lo único que esa firma por prueba frena es la consecuencia externa
  real en un entorno real (`service/jobs.py:_holds_run`, `generator.py`
  `holdsRun` + `real_environment`).
- **Con una corrida no se afirma unanimidad.** Se dice cuántas hubo. «No medido»
  es una respuesta válida y frecuente.
- **Agregar una plataforma es editar `platforms/<nombre>/config.yaml`**, nunca
  tocar código. Si para entrar a una plataforma hace falta un `if`, el `if` va
  en la forma del config, no en el nombre del proveedor.
- **Nada de `asyncio.create_task` pelado.** Pasa por `service/watchdog.py`, que
  guarda la referencia fuerte, engancha el done-callback y vigila el heartbeat.
  Un trabajo que CUELGA es peor que uno que falla.
- **400 y 401 no se reintentan.** Fallan igual para siempre, y un `400
  invalid_request_error` es como el modelo reporta un parámetro mal construido:
  eso se muestra, no se tapa. El reintento largo de `service/retry.py` es para
  500/429/conexión, encima del que ya hace el SDK.
- **El servicio escucha en 127.0.0.1.** Maneja credenciales ajenas y gasta la
  API key de quien lo corre. Exponerlo es una decisión explícita de su dueño.
- **Secretos: sólo del entorno.** `EVALKIT_SECRET_KEY` y `EVALKIT_SERVICE_TOKEN`
  no se leen de `.env` a propósito (una clave vieja en un archivo arranca el
  servicio y después no descifra). Nunca en el repo, nunca en una respuesta,
  nunca en un log: `selfcheck` imprime el largo del token, jamás el valor.
- **No se commitea evidencia.** Capturas, `evidence_ledger/`, y sobre todo
  `**/restricted/`, que es el corpus de autolesión. El `.gitignore` lo cubre;
  no lo aflojes.

## Convenciones

- **Los docstrings explican POR QUÉ, con el caso real.** Ese es el estilo del
  repo y es lo que lo hace mantenible: "esto falló el 2026-07-20 así, por eso
  existe". Un docstring que repite la firma de la función no agrega nada.
- **Español para todo lo que ve un usuario**: mensajes, informes, CLI, web. Los
  docstrings internos están mezclados; seguí el del archivo que estás tocando.
- **Los tests construyen lo que leen.** Nada de binarios en git: los PDF de
  `test_extract.py` se autoríen con la librería real, porque un `.docx` en un
  diff no lo revisa nadie y se pudre en silencio.
- **Una dependencia nueva se justifica en el requirements**, en el comentario de
  al lado. Hay dos lectores de PDF y el archivo dice por qué.

## Lo que los tests NO ven

Vale tenerlo presente antes de decir "listo, la suite está en verde":

- Casi toda la suite corre contra **dobles**, así que ve la lógica y no ve el
  mundo. Los defectos más caros (una API que rechazó un parámetro, un login que
  era un selector de proveedor y no un formulario, un rol equivocado, una
  captura truncada a 8058 caracteres) aparecieron todos **con la suite en
  verde**.
- `tests/test_driver_contract.py` es la excepción: Chromium de verdad sobre una
  página local. Sin navegador instalado se saltea solo; con
  `EVALKIT_REQUIRE_BROWSER=1` (lo que hace el CI) falla ruidosamente.
- `evalkit.replay` reproduce una corrida grabada por el runner real, y es ciega
  a todo lo anterior a la captura por construcción.
- Las marcadas `needs_real_platform` necesitan datos que este repo no publica.

Si tocás el driver, la sesión, el login o el recorrido: la suite en verde **no
es evidencia**. Decilo en vez de sugerir lo contrario.
