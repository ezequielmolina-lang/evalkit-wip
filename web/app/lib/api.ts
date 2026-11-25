/**
 * Client for the Python job service.
 *
 * The engine lives in a container (Playwright needs a real browser; a framework
 * build plus a five-judge council plus a browser run takes minutes, which a
 * serverless function cannot hold). So every operation is a job: start it, then
 * poll. Nothing here blocks on a long request.
 */

// Port 8811, not the conventional 8080: a llama.cpp server already listens on
// 8080 on this machine (the offline-model work). Defaulting there meant the web
// would happily talk to llama.cpp and report its 404s as engine failures.
export const ENGINE_URL = process.env.ENGINE_URL ?? "http://127.0.0.1:8811";

export type Stage = "recon" | "framework" | "probes" | "awaiting_approval" | "plan_ready" | "run" | "score" | "done" | "error";

export interface JobProgress {
  stage: Stage;
  /** What is in flight right now — a criterion id, a probe id. */
  current?: string;
  done: number;
  total: number;
  message?: string;
}

export interface JobState {
  jobId: string;
  stage: Stage;
  progress: JobProgress;
  /** Real, metered USD spend so far (agent walk + framework + council + score). */
  spendUsd?: number;
  /** Present once the framework stage completes. */
  plan?: unknown;
  /** Present once scoring completes. */
  report?: unknown;
  /** Populated only on stage === "error". */
  error?: {
    stage: Stage;
    item?: string;
    message: string;
    /** True when the run died because the session dropped — NOT a platform failure. */
    sessionLost?: boolean;
  };
}

export interface StartJobInput {
  objective: string;
  areas: string[];
  platform: { url: string; username?: string; password?: string };
  documents?: { name: string; text: string }[];
}

/**
 * Shared service token. The engine is not public: it can start paid runs and
 * read every job, so it authenticates its one legitimate caller — this app.
 *
 * Server-side only. There is deliberately no NEXT_PUBLIC_ prefix: a token in
 * a NEXT_PUBLIC_ variable is compiled into the browser bundle and readable by
 * anyone who opens devtools, which would defeat the point entirely.
 */
const SERVICE_TOKEN = process.env.EVALKIT_SERVICE_TOKEN;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  if (typeof window !== "undefined") {
    throw new Error(
      "El cliente del motor sólo puede usarse desde el servidor: el token de servicio " +
        "no debe llegar al navegador. Llamá a través de una route handler."
    );
  }
  const res = await fetch(`${ENGINE_URL}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(SERVICE_TOKEN ? { authorization: `Bearer ${SERVICE_TOKEN}` } : {}),
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  if (res.status === 401) {
    throw new Error(
      "El motor rechazó la credencial del servicio. Revisá que EVALKIT_SERVICE_TOKEN " +
        "sea el mismo valor en la web y en el motor."
    );
  }
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`El motor respondió ${res.status}. ${body.slice(0, 300)}`);
  }
  return res.json() as Promise<T>;
}

/**
 * Credentials go straight to the engine and are never persisted by the web
 * layer. The engine encrypts them at rest and never echoes them back, so a
 * job state is safe to render.
 */
export const startJob = (input: StartJobInput) =>
  call<{ job_id: string }>("/jobs", { method: "POST", body: JSON.stringify(input) });

export const getJob = (id: string) => call<JobState>(`/jobs/${id}`);

/** Sends the reviewed plan. Gated probes are NOT approved by this call. */
export const approvePlan = (id: string, plan: unknown) =>
  call<JobState>(`/jobs/${id}/approve`, { method: "POST", body: JSON.stringify({ plan }) });

/** One gated probe, signed off individually and on the record. */
export const signoffProbe = (id: string, probeId: string) =>
  call<JobState>(`/jobs/${id}/signoff`, {
    method: "POST",
    body: JSON.stringify({ probe_id: probeId }),
  });

export const getReport = (id: string) => call<unknown>(`/jobs/${id}/report`);

/** One row of the chronological trail the report's appendix renders. */
export interface LedgerRow {
  probeId: string;
  testId: string;
  runIndex: number;
  kind: string;
  /** Basename, never a disk path — it is what `evidenceUrl` accepts. */
  artifact: string;
  sha256: string;
  timestamp: string;
  note: string;
  url: string;
}

/** Every captured artefact, in order. The appendix exists so a sceptical
 *  reader can walk the whole argument, not the part that flatters us. */
export const getLedger = (id: string) =>
  call<{ rows: LedgerRow[] }>(`/jobs/${id}/ledger`);

/**
 * Browser-facing URL for a captured artefact — OUR proxy, not the engine.
 * The report loads these with <img src>, and the browser holds no service
 * token; the proxy attaches it server-side.
 */
export const evidenceUrl = (id: string, artifact: string) =>
  `/api/jobs/${encodeURIComponent(id)}/evidence/${encodeURIComponent(artifact)}`;

export async function engineUp(): Promise<boolean> {
  try {
    await call<{ ok: boolean }>("/healthz");
    return true;
  } catch {
    return false;
  }
}

export const STAGE_LABEL: Record<Stage, string> = {
  recon: "Conectando y recorriendo la plataforma",
  framework: "Armando la rúbrica",
  probes: "Generando pruebas y revisándolas",
  awaiting_approval: "Esperando tu revisión",
  plan_ready: "Plan listo para tu revisión",
  run: "Recorriendo la plataforma",
  score: "Puntuando la evidencia",
  done: "Listo",
  error: "Se detuvo",
};
