// Compila la web a `.next-check` para verificar que compila, sin tocar `.next`.
//
// POR QUÉ ES UN SCRIPT Y NO UNA LÍNEA EN package.json. La carpeta de salida se
// le pasa a Next por una variable de entorno (ver next.config.mjs), y no hay una
// forma de fijar una variable en un script de npm que sirva igual en Windows y en
// Linux: `VAR=x cmd` no existe en cmd.exe y `set VAR=x&& cmd` no existe en sh. El
// equipo desarrolla en Windows y el CI corre en Linux, así que tiene que andar en
// los dos. La alternativa era sumar `cross-env` como dependencia; unas líneas de
// Node no justifican una.
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const nextBin = require.resolve("next/dist/bin/next");

// UNA VERIFICACIÓN NO PUEDE DEJAR EL REPO DISTINTO. Al compilar con otra carpeta
// de salida, Next reescribe dos archivos versionados: en `next-env.d.ts` cambia
// la referencia a `./.next-check/types/routes.d.ts`, y en `tsconfig.json` agrega
// `.next-check/types/**/*.ts` al `include`. La primera vez que se corrió este
// script dejó los dos modificados, y el `tsc --noEmit` siguiente habría leído
// los tipos de la compilación de prueba en lugar de los reales. Se guardan los
// bytes antes y se devuelven después, pase lo que pase con el build.
const REESCRITOS_POR_NEXT = ["next-env.d.ts", "tsconfig.json"];
const antes = new Map(REESCRITOS_POR_NEXT.map((f) => [f, readFileSync(f)]));

let status = 1;
try {
  const result = spawnSync(process.execPath, [nextBin, "build"], {
    stdio: "inherit",
    env: { ...process.env, EVALKIT_DIST_DIR: ".next-check" },
  });
  status = result.status ?? 1;
} finally {
  for (const [archivo, bytes] of antes) {
    if (!readFileSync(archivo).equals(bytes)) writeFileSync(archivo, bytes);
  }
}

// Se propaga el código de salida: un build que falla tiene que fallar el script,
// o `npm run build:check` diría que todo está bien con la web rota.
process.exit(status);
