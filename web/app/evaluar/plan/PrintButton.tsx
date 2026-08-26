"use client";

import { useState } from "react";

/**
 * Descargar el PLAN como PDF.
 *
 * Usa el motor de impresión del navegador en vez de traer una librería de PDF:
 * el plan YA es la maqueta buena, con sus pasos, su consejo y sus capturas, así que la
 * hoja de impresión (`@media print` en globals.css) lo convierte en documento
 * entregable sin mantener un segundo renderizador que se desincronice del
 * primero. El usuario elige "Guardar como PDF" en el diálogo.
 *
 * ANTES DE IMPRIMIR HAY QUE CARGAR LAS IMÁGENES. Las capturas del recorrido son `lazy`, y una
 * imagen diferida que nunca entró al viewport NO se carga sola al imprimir: el
 * PDF salía con la evidencia en blanco. Verificado en vivo -- `currentSrc`
 * vacío en la captura del hallazgo, que es justamente lo que el documento
 * existe para probar. Así que se fuerzan a `eager`, se espera su decodificación,
 * y recién entonces se abre el diálogo.
 */
export default function PrintButton() {
  const [preparing, setPreparing] = useState(false);

  async function printWithEvidence() {
    setPreparing(true);
    try {
      const imgs = Array.from(document.querySelectorAll("img"));
      await Promise.all(
        imgs.map((img) => {
          img.loading = "eager";
          if (img.complete && img.naturalWidth > 0) return Promise.resolve();
          // `decode()` espera al pixel, no sólo al byte. Un fallo individual no
          // puede bloquear la impresión: se imprime lo que haya.
          return img.decode().catch(() => undefined);
        })
      );
    } finally {
      setPreparing(false);
      window.print();
    }
  }

  return (
    <button type="button" className="btn" onClick={printWithEvidence} disabled={preparing}>
      {preparing ? "Preparando…" : "Descargar el plan en PDF"}
    </button>
  );
}
