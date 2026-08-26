"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { STAGE_LABEL } from "../../lib/api";
import type { JobState, Stage } from "../../lib/api";

// `recon` first, because that is the order the engine runs them in. A stage
// missing here is not a cosmetic gap: `ORDER.indexOf(stage)` returns -1 and the
// progress bar marks NO step as reached, so a job that is working looks stuck.
const ORDER: Stage[] = ["recon", "framework", "probes", "awaiting_approval", "run", "score", "done"];

/**
 * Demo timeline used until a job id is present in the URL. It exists so the
 * screen can be reviewed without a running engine; with ?job=<id> the page
 * polls the real service instead.
 */
const DEMO: { stage: Stage; current: string; done: number; total: number; ms: number }[] = [
  { stage: "framework", current: "seleccionando criterios del banco", done: 3, total: 7, ms: 1400 },
  { stage: "framework", current: "clasificando observabilidad", done: 7, total: 7, ms: 1200 },
  { stage: "probes", current: "PT-D9-H1 · panel de 5 jueces", done: 1, total: 3, ms: 1600 },
  { stage: "probes", current: "EMC-8.1 · ronda 2 de revisión", done: 2, total: 3, ms: 1500 },
  { stage: "awaiting_approval", current: "", done: 3, total: 3, ms: 2600 },
  { stage: "run", current: "PT-D9-H1 · sondeo 1 de 3", done: 1, total: 9, ms: 1300 },
  { stage: "run", current: "PT-D9-H1 · sondeo 3 de 3", done: 3, total: 9, ms: 1300 },
  { stage: "run", current: "EMC-8.1 · sondeo 2 de 3", done: 5, total: 9, ms: 1300 },
  { stage: "score", current: "puntuando con evidencia citada", done: 7, total: 9, ms: 1500 },
  { stage: "done", current: "", done: 9, total: 9, ms: 0 },
];

export default function Corriendo() {
  const [job, setJob] = useState<JobState | null>(null);
  const [demoIdx, setDemoIdx] = useState(0);
  const [log, setLog] = useState<string[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const jobId = typeof window !== "undefined"
    ? new URLSearchParams(window.location.search).get("job")
    : null;

  // Real job: poll the engine. Demo: walk the scripted timeline.
  useEffect(() => {
    if (jobId) {
      let alive = true;
      const tick = async () => {
        try {
          // Through our own route handler, never the engine directly: the
          // service token must not reach the browser bundle.
          const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
          if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error ?? `HTTP ${r.status}`);
          const s = (await r.json()) as JobState;
          if (!alive) return;
          setJob(s);
          if (s.stage !== "done" && s.stage !== "error") timer.current = setTimeout(tick, 2000);
        } catch (e) {
          if (alive)
            setJob({
              jobId,
              stage: "error",
              progress: { stage: "error", done: 0, total: 0 },
              error: { stage: "error", message: String(e) },
            });
        }
      };
      tick();
      return () => {
        alive = false;
        if (timer.current) clearTimeout(timer.current);
      };
    }
    if (demoIdx >= DEMO.length - 1) return;
    const step = DEMO[demoIdx];
    timer.current = setTimeout(() => setDemoIdx((i) => i + 1), step.ms);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [jobId, demoIdx]);

  const d = DEMO[demoIdx];
  const stage: Stage = job?.stage ?? d.stage;
  const current = job?.progress.current ?? d.current;
  const done = job?.progress.done ?? d.done;
  // El mensaje de la etapa es la narración real del motor ("Consejo de jueces:
  // 3 de 16 pruebas revisadas", "Conectando a ... con la cuenta de prueba"):
  // mucho más mostrable que la etiqueta sola.
  const message = job?.progress.message ?? "";
  const total = job?.progress.total ?? d.total;

  useEffect(() => {
    const detail = message || current;
    const line = `${STAGE_LABEL[stage]}${detail ? ` — ${detail}` : ""}`;
    setLog((l) => (l[l.length - 1] === line ? l : [...l, line]));
  }, [stage, current, message]);

  const idx = ORDER.indexOf(stage);
  const pct = total ? Math.round((done / total) * 100) : 0;
  const waiting = stage === "awaiting_approval";
  const finished = stage === "done";
  const failed = stage === "error";

  return (
    <>
      <nav>
        <div className="wrap inner">
          <Link href="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
            eval<span>kit</span>
          </Link>
          <div className="navlinks">
            <span style={{ fontSize: 13 }}>Paso 3 de 4 · Ejecución</span>
          </div>
        </div>
      </nav>

      <div className="wrap" style={{ paddingTop: 44, paddingBottom: 90, maxWidth: 820 }}>
        <div className="eyebrow">
          {jobId ? `Trabajo ${jobId}` : "Vista de ejemplo"}
          {typeof job?.spendUsd === "number" && job.spendUsd > 0 && (
            <span className="spend-live">gastado: ${job.spendUsd.toFixed(2)}</span>
          )}
        </div>
        <h2 style={{ marginTop: 12 }}>
          {failed ? "Se detuvo" : finished ? "Terminó" : STAGE_LABEL[stage]}
        </h2>
        <p className="lead" style={{ fontSize: 16 }}>
          {waiting
            ? "El plan está listo. Todavía no se ejecutó ninguna prueba: revisalo y aprobalo para seguir."
            : failed
              ? "El trabajo se detuvo antes de terminar. Abajo está en qué paso y por qué."
              : "Esto toma varios minutos. Podés cerrar la pestaña: el trabajo sigue corriendo."}
        </p>

        {/* stage rail */}
        <div className="rail">
          {/* Todas menos `done`: era slice(0, 5) cuando ORDER tenía seis
              entradas, así que al insertar `recon` adelante el riel dejó de
              mostrar "puntuando". Derivarlo de la lista lo mantiene alineado
              cuando se agregue la próxima etapa. */}
          {ORDER.filter((s) => s !== "done").map((s, i) => (
            <div
              key={s}
              className={`rail-step ${i < idx ? "past" : ""} ${i === idx ? "now" : ""}`}
            >
              <div className="rail-dot" />
              <span>{STAGE_LABEL[s]}</span>
            </div>
          ))}
        </div>

        {!waiting && !failed && (
          <div className="prog">
            <div className="prog-bar">
              <div className="prog-fill" style={{ width: `${finished ? 100 : pct}%` }} />
            </div>
            <div className="prog-meta">
              <span>{message || current || "—"}</span>
              <span className="mono">
                {done}/{total}
              </span>
            </div>
          </div>
        )}

        {failed && job?.error && (
          <div className="err">
            <div className="err-head">Se detuvo en: {STAGE_LABEL[job.error.stage] ?? job.error.stage}</div>
            {job.error.item && <div className="mono">{job.error.item}</div>}
            <p>{job.error.message}</p>
            {job.error.sessionLost && (
              <p className="err-note">
                La sesión se cayó durante el recorrido. Esto <strong>no</strong> significa que la
                plataforma haya reprobado el criterio: significa que no se pudo observar. Los
                pasos posteriores no se registraron como evidencia.
              </p>
            )}
          </div>
        )}

        {waiting && (
          <div className="cta-row">
            {/* Con el id: sin él, el revisor cae en el plan de EJEMPLO y
                aprueba algo que no es su trabajo. */}
            <Link
              className="btn"
              href={jobId ? `/evaluar/plan?job=${encodeURIComponent(jobId)}` : "/evaluar/plan"}
            >
              Revisar el plan
            </Link>
          </div>
        )}
        {finished && (
          <div className="cta-row">
            <Link
              className="btn"
              href={jobId ? `/evaluar/informe?job=${encodeURIComponent(jobId)}` : "/evaluar/informe"}
            >
              Ver el informe
            </Link>
          </div>
        )}

        <div className="logbox">
          <div className="panel-title" style={{ padding: "12px 16px", borderBottom: "1px solid var(--line)" }}>
            Registro
          </div>
          <ul>
            {log.map((l, i) => (
              <li key={i} className={i === log.length - 1 && !finished && !failed ? "live" : ""}>
                {l}
              </li>
            ))}
          </ul>
        </div>

        {!jobId && (
          <p className="fhint" style={{ marginTop: 16 }}>
            Vista de ejemplo con tiempos simulados. Con <span className="mono">?job=&lt;id&gt;</span>{" "}
            esta pantalla consulta el motor real.
          </p>
        )}
      </div>
    </>
  );
}
