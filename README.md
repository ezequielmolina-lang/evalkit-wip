# evalkit

> **Work in progress.** Funciona de punta a punta y hay una corrida completa
> contra una plataforma real detrás de los ejemplos, pero está en desarrollo
> activo: la interfaz se mueve, los formatos pueden cambiar sin aviso y hay
> partes a medio hacer y anotadas como tales en el código. Úsalo para entender
> el enfoque y probarlo, no como algo estable todavía.
>
> Lo más honesto que se puede decir hoy: el motor corre, entrega evidencia
> hasheada y se niega a afirmar más de lo que midió. Alrededor de eso todavía
> falta.
>
> **Qué se está probando ahora.** Un tutor de IA de matemática para 3.er grado
> usado en escuelas públicas, con alumnos hispanohablantes. El foco está en tres
> cosas: si el tutor **alucina** (¿confirma un recuerdo compartido que nunca
> pasó?, ¿acepta que 89+47=126?), si **mantiene estado** entre turnos y entre
> sesiones, y qué hace **ante una señal de crisis** del alumno — esto último
> detrás de una compuerta que exige firma humana y que todavía no se ejecutó.
> Los ejemplos de este repo salen de esa corrida, anonimizada.

Evalúa una plataforma de aprendizaje recorriéndola, y entrega un diagnóstico
donde **cada puntaje cita la evidencia que lo sostiene**: una captura y un texto
de página, hasheados, que se pueden reabrir y volver a verificar un año después.

Está pensado para quien tiene que decidir con plata pública y después defender
la decisión: un ministerio, una fundación, un equipo de compras. No reemplaza un
piloto ni mide aprendizaje. Mide qué hace la plataforma cuando se la recorre,
contra el criterio que vos definís.

## Cómo funciona

```
recon → rúbrica → consejo de jueces → tu aprobación → recorrido → puntuación → informe
```

1. **Recon.** Entra a la plataforma y arma el mapa de superficies: dónde está el
   tutor, dónde el contenido, dónde la política publicada. Va primero a
   propósito: si el acceso está roto, se sabe antes de gastar un peso en el
   modelo.
2. **Rúbrica.** Traduce tu objetivo a criterios, y marca cuáles se pueden
   verificar recorriendo y cuáles necesitan un documento del proveedor.
3. **Consejo.** Varios jueces revisan cada prueba antes de que exista, y las que
   tocan crisis, datos de menores o escrituras reales quedan **bloqueadas
   esperando una firma humana**.
4. **Tu aprobación.** Ves el plan, sacás criterios, editás pruebas. Lo que
   aprobás es lo que corre.
5. **Recorrido y puntuación.** Un navegador real ejecuta las pruebas, cada paso
   deja evidencia hasheada en un ledger de sólo-agregar, y un juez puntúa contra
   esa evidencia — citando los artefactos, nunca inventándolos.

## Las reglas que se sostienen

- **Un criterio que nadie pudo observar no es un cero.** Se reporta
  `NO_EVALUADO` con la evidencia que habría hecho falta. Un proveedor que
  esconde una superficie no puede puntuar mejor que uno que la expone.
- **Una prueba con compuerta no corre sin firma**, y firmar no es aprobar el
  plan: es una firma por prueba.
- **El juez sólo puede citar artefactos que realmente recibió.** Una ruta
  inventada no respalda un veredicto.
- **Con una sola corrida no se afirma unanimidad.** Se dice cuántas corridas
  hubo; «no medido» es una respuesta válida.

## Correrlo

```bash
cd 06_Application
python -m pip install -r requirements.txt -r requirements-service.txt
python -m playwright install chromium      # sólo para corridas reales

export ANTHROPIC_API_KEY="tu-clave"
python evalkit_app.py                      # levanta motor + web y abre el navegador
```

Tu clave nunca sale de tu máquina. El único costo es lo que te cobre tu
proveedor de modelo.

### Agregar tu plataforma

Copiá `06_Application/platforms/ejemplo/config.yaml` a
`platforms/<tu-plataforma>/config.yaml` y editalo. Agregar una plataforma es una
edición de configuración, nunca un cambio de código.

Ese archivo tiene los comentarios que más caro salieron: qué pasa cuando el
login es un selector de proveedor y no un formulario, por qué hace falta una
señal *positiva* de sesión iniciada, y por qué el rol hay que fijarlo antes de
guardar la sesión.

## Los tests

```bash
cd 06_Application && python -m pytest -q
```

929 pasan. **17 saltan**, y el motivo está escrito: leen los datos de una
evaluación real —los `config.yaml` de las plataformas medidas, sus mapas de
superficies, la evidencia capturada— que este repositorio no publica. Poné tu
propia plataforma y las que dependen de configuración empiezan a correr solas.

Vale decir qué NO cubren esos tests: corren contra clientes falsos, así que ven
la lógica y no ven el mundo. Los errores más caros de este proyecto —una API que
rechazó un parámetro, un login sin formulario, un rol equivocado, una captura
truncada— aparecieron todos corriendo contra una plataforma de verdad, con la
suite en verde.

## Qué no está acá

La herramienta se publica; la evaluación no. Este repositorio no incluye los
`config.yaml` de las plataformas medidas, su evidencia, los informes ni la
comparación entre proveedores. Los ejemplos que trae salen de una corrida real
**anonimizada**: los veredictos, los fundamentos y la forma de la evidencia son
los que produjo el motor; el proveedor y las personas, no.

## Licencia

MIT.
