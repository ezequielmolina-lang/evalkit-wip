// Shapes shared by the setup form and the plan review screen.
// These mirror the Python engine's real structures so the wiring is a
// serialisation job, not a redesign:
//   observability  -> framework.OBSERVABILITY_CLASSES
//   provenance     -> framework.CriterionSource
//   repeats        -> probes.Probe.repeats        (platform non-determinism)
//   repeatJudging  -> probes.Probe.repeat_judging (scorer sampling noise)

export type Observability =
  | "OBSERVABLE_EN_PLATAFORMA"
  | "REQUIERE_DOCUMENTACION"
  | "REQUIERE_DATOS"
  | "REQUIERE_ESTUDIO";

export const OBS_LABEL: Record<Observability, string> = {
  OBSERVABLE_EN_PLATAFORMA: "En la plataforma",
  REQUIERE_DOCUMENTACION: "Requiere documento",
  REQUIERE_DATOS: "Requiere datos",
  REQUIERE_ESTUDIO: "Requiere estudio",
};

export const OBS_HELP: Record<Observability, string> = {
  OBSERVABLE_EN_PLATAFORMA: "Se resuelve recorriendo la plataforma.",
  REQUIERE_DOCUMENTACION: "Hay que pedirle un documento al proveedor.",
  REQUIERE_DATOS: "Hace falta acceso a datos que la plataforma no expone.",
  REQUIERE_ESTUDIO: "Requiere un diseño de investigación, no un recorrido.",
};

export type SourceType = "bank" | "document" | "generated";

export interface Provenance {
  type: SourceType;
  framework: string;
  originalId: string;
  location: string;
}

export interface Criterion {
  id: string;
  area: string;
  name: string;
  passCriteria: string;
  failCriteria: string;
  points: number;
  observability: Observability;
  /** Only for the three non-observable classes: what would settle it. */
  evidenciaRequerida?: string;
  source: Provenance;
}

export interface ProbeStep {
  n: number;
  action: string;
  capture?: string;
}

/** What the recon walk found: the surfaces map plus its screenshots. */
export interface ReconInfo {
  surfaces: {
    surfaces?: Record<
      string,
      { name?: string; steps?: string[]; no_session?: boolean }
    >;
    unavailable?: Record<string, string>;
  } | null;
  shots: string[];
}

/** One council seat's verdict on a probe, as the engine serializes it. */
export interface JudgeVoice {
  judge: string;
  verdict: string;
  reasons: string[];
  /** "probe" | "criterio" | "no_aplica" — a "criterio" objection is a finding
   *  about the INSTRUMENT (the criterion's own wording), not about the probe:
   *  it never triggers a redraft and deserves its own display. */
  objectionTarget?: string;
}

export interface Probe {
  probeId: string;
  criterionId: string;
  steps: ProbeStep[];
  /** True when the safety council gated it: cannot run without a signature. */
  requiresSignoff?: boolean;
  signoffReason?: string;
  /** How many rounds the council sat on this probe (1 = approved first try). */
  roundsUsed?: number;
  /** Unanimous approval of the voting seats in the final round. */
  approved?: boolean;
  /** The final round, one verdict per seat — the "how it does it" a demo needs. */
  judges?: JudgeVoice[];
}

/**
 * The sampling plan. This is where scope decisions live — visibly and
 * editably — instead of inside criterion text where they would look as
 * though the instrument's author wrote them.
 */
export interface SamplingPlan {
  subjects: string[];
  grades: string[];
  lessonsPerGrade: number;
  /** Re-probe the platform N times. A tutor is not deterministic. */
  repeats: number;
  /** Re-judge the same saved evidence N times, to measure scorer noise. */
  repeatJudging: number;
  rationale: string;
}

export interface EvalPlan {
  criteria: Criterion[];
  probes: Probe[];
  sampling: SamplingPlan;
}

export function scoredPoints(cs: Criterion[]) {
  return cs
    .filter((c) => c.observability === "OBSERVABLE_EN_PLATAFORMA")
    .reduce((a, c) => a + c.points, 0);
}

export function outsidePoints(cs: Criterion[]) {
  return cs
    .filter((c) => c.observability !== "OBSERVABLE_EN_PLATAFORMA")
    .reduce((a, c) => a + c.points, 0);
}

export function byObservability(cs: Criterion[]) {
  const out = {
    OBSERVABLE_EN_PLATAFORMA: 0,
    REQUIERE_DOCUMENTACION: 0,
    REQUIERE_DATOS: 0,
    REQUIERE_ESTUDIO: 0,
  } as Record<Observability, number>;
  cs.forEach((c) => (out[c.observability] += 1));
  return out;
}
