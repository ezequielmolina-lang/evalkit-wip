/** @type {import('next').NextConfig} */
export default {
  // Carpeta de salida configurable SÓLO por entorno, para `npm run build:check`.
  //
  // `next build` escribe en `.next`, que es la misma carpeta que usa `next dev`.
  // Compilar para verificar mientras la app está levantada le pisa los archivos
  // al servidor de desarrollo, que empieza a devolver 500 hasta reiniciarlo. Por
  // eso existe build:check: compila a `.next-check` y no toca nada vivo.
  //
  // El script decía `next build --distDir .next-check`, y esa bandera no existe:
  // `distDir` es una opción de ESTE archivo, no de la línea de comandos, así que
  // el script fallaba siempre con "unknown option '--distDir'". Ahora el script
  // pone EVALKIT_DIST_DIR y la opción se lee acá. Sin la variable no cambia nada:
  // `next dev`, `next build` y `next start` siguen usando `.next`.
  ...(process.env.EVALKIT_DIST_DIR
    ? { distDir: process.env.EVALKIT_DIST_DIR }
    : {}),
};
