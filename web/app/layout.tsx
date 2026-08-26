import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "evalkit — evaluación reproducible de plataformas de aprendizaje",
  description:
    "Convertí tu criterio en pruebas concretas, corrélas contra la plataforma, y obtené un diagnóstico donde cada puntaje cita la evidencia que lo sostiene.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="es">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600&family=Inter:wght@400;500;600;700&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
