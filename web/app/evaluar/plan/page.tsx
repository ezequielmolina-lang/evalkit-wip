"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { SAMPLE_PLAN } from "../../lib/samplePlan";
import PrintButton from "./PrintButton";
import { OBS_LABEL, OBS_HELP, byObservability, scoredPoints, outsidePoints } from "../../lib/types";
import type { EvalPlan, Observability, ReconInfo } from "../../lib/types";

const OBS_CLASS: Record<Observability, string> = {
  OBSERVABLE_EN_PLATAFORMA: "o-plat",
  REQUIERE_DOCUMENTACION: "o-doc",
  REQUIERE_DATOS: "o-dat",
  REQUIERE_ESTUDIO: "o-est",
};

const SRC_LABEL: Record<string, string> = {
  bank: "instrumento publicado",
  document: "tu documento",
  generated: "SIN RESPALDO",
};

/** Stages during which the plan does not exist yet, so we keep polling.
 *
 * `recon` belongs here for the same reason the other two do, and leaving it out
 * is worse than it looks: polling would stop on the FIRST stage of every real
 * job, so the screen would sit on "construyendo la rúbrica" forever while the
 * engine worked normally behind it. */
const BUILDING = new Set(["recon", "framework", "probes"]);

/** Nav + page frame, so the waiting and error states are not bare text. */
function Shell({ step, children }: { step: string; children: React.ReactNode }) {
  return (
    <>
      <nav>
        <div className="wrap inner">
          <Link href="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
            eval<span>kit</span>
          </Link>
          <div className="navlinks">
            <span style={{ fontSize: 13 }}>{step}</span>
          </div>
        </div>
      </nav>
      <div className="wrap" style={{ paddingTop: 44, paddingBottom: 90 }}>{children}</div>
    </>
  );
}

function PlanScreen() {
  const router = useRouter();
  const jobId = useSearchParams().get("job");

  // No ?job means nobody started an evaluation: this is the worked example.
  // It is LABELLED as such. Rendering sample data unlabelled is precisely how
  // these screens came to look live while the engine had never been called.
  const [plan, setPlan] = useState<EvalPlan | null>(jobId ? null : SAMPLE_PLAN);
  const [stage, setStage] = useState<string>(jobId ? "framework" : "sample");
  const [error, setError] = useState("");
  const [approving, setApproving] = useState(false);
  const [s, setS] = useState(SAMPLE_PLAN.sampling);
  const [spend, setSpend] = useState(0);
  const [recon, setRecon] = useState<ReconInfo | null>(null);
  // Criterios y pruebas que el revisor sacó. El título prometía "podés sacar
  // criterios, editar pruebas" y no había ni un control: el motor aceptaba
  // planes editados desde siempre, la pantalla nunca los dejó editar. Lo
  // quitado NO se ejecuta ni se paga.
  const [dropped, setDropped] = useState<Set<string>>(new Set());
  const toggleDrop = (id: string) =>
    setDropped((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  useEffect(() => {
    if (!jobId) return;
    let alive = true;

    const tick = async () => {
      try {
        const r = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
        const d = await r.json();
        if (!alive) return;
        if (!r.ok) throw new Error(d?.error ?? `El servidor respondió ${r.status}.`);
        setStage(d.stage);
        if (typeof d.spendUsd === "number") setSpend(d.spendUsd);
        if (d.recon) setRecon(d.recon as ReconInfo);
        if (d.plan) setPlan(d.plan as EvalPlan);
        if (d.stage === "error") {
          setError(d.error?.message ?? "El motor se detuvo sin explicar por qué.");
          return;
        }
        // The plan arrives at awaiting_approval; before that keep polling.
        if (BUILDING.has(d.stage)) setTimeout(tick, 2000);
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      }
    };
    tick();
    return () => {
      alive = false;
    };
  }, [jobId]);

  const approve = useCallback(async () => {
    if (!jobId || !plan) return;
    setApproving(true);
    setError("");
    try {
      // The EDITED plan is what gets sent — that is the point of this screen.
      // What the reviewer dropped is filtered out here, so it never runs and
      // never costs. Gated probes are not approved by this call: the engine
      // reads requiresSignoff from its own copy, so a flag flipped here changes
      // nothing.
      const edited = {
        ...plan,
        sampling: s,
        criteria: plan.criteria.filter((c) => !dropped.has(c.id)),
        probes: plan.probes.filter(
          (p) => !dropped.has(p.probeId) && !dropped.has(p.criterionId)
        ),
      };
      const r = await fetch(`/api/jobs/${jobId}/approve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ plan: edited }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d?.error ?? `El servidor respondió ${r.status}.`);
      router.push(`/evaluar/corriendo?job=${encodeURIComponent(jobId)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setApproving(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, plan, s, dropped, router]);

  // Once the real plan lands, its sampling replaces the placeholder.
  useEffect(() => {
    if (plan?.sampling) setS(plan.sampling);
  }, [plan]);

  if (error) {
    return (
      <Shell step="Paso 2 de 4 · Plan">
        <div className="err-box" role="alert">
          {error}
        </div>
        <div className="cta-row" style={{ marginTop: 26 }}>
          <Link className="btn btn-ghost" href="/evaluar">
            Volver a configuración
          </Link>
        </div>
      </Shell>
    );
  }

  if (!plan) {
    return (
      <Shell step="Paso 2 de 4 · Plan">
        <div className="eyebrow">Armando el plan</div>
        <h2 style={{ marginTop: 12 }}>
          {stage === "recon"
            ? "Conectando y recorriendo la plataforma."
            : stage === "probes"
              ? "Generando las pruebas y revisándolas."
              : "Construyendo la rúbrica."}
        </h2>
        <p className="lead" style={{ fontSize: 16 }}>
          {stage === "recon"
            ? "Se inicia sesión con la cuenta de prueba para ver qué tiene la plataforma. El recorrido es de sólo lectura y de ahí sale el plan."
            : "Todavía no se ejecutó ninguna prueba. Cuando el plan esté listo vas a poder corregirlo acá antes de que se ejecute."}
        </p>
      </Shell>
    );
  }

  const { criteria, probes } = plan;
  const counts = byObservability(criteria);
  // Objeciones cuyo blanco es el CRITERIO: hallazgos sobre el instrumento, no
  // sobre las pruebas. No dispararon redibujados (no pueden arreglarse
  // redibujando) y se muestran aparte: son lo que le decís al autor del
  // instrumento, no al proveedor.
  const instrumentFindings = probes.flatMap((p) =>
    (p.judges ?? [])
      .filter((j) => j.objectionTarget === "criterio" && j.verdict !== "approve")
      .map((j) => ({ probe: p, judge: j }))
  );
  const observable = criteria.filter((c) => c.observability === "OBSERVABLE_EN_PLATAFORMA");
  const notObservable = criteria.filter((c) => c.observability !== "OBSERVABLE_EN_PLATAFORMA");

  return (
    <>
      <nav>
        <div className="wrap inner">
          <Link href="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
            eval<span>kit</span>
          </Link>
          <div className="navlinks">
            <span style={{ fontSize: 13 }}>Paso 2 de 4 · Plan</span>
          </div>
        </div>
      </nav>

      <div className="wrap" style={{ paddingTop: 44, paddingBottom: 90 }}>
        <div className="report-head">
          <div>
            <div className="eyebrow">Revisá antes de ejecutar</div>
            <h2 style={{ marginTop: 12 }}>Este es el plan. Corregilo.</h2>
          </div>
          {/* El plan es largo y es lo que se lleva a una reunión: sale en PDF
              por el mismo camino que el informe. Los controles de edición se
              ocultan al imprimir (ver @media print). */}
          {jobId && <PrintButton />}
        </div>
        <p className="lead" style={{ fontSize: 16 }}>
          Todavía no se ejecutó ninguna prueba. Podés sacar criterios, editar pruebas y cambiar
          el muestreo. Lo que apruebes acá es lo que se ejecuta.
        </p>

        {!jobId && (
          <div className="note-box">
            <b>Plan de ejemplo.</b> No hay ninguna evaluación en curso: los criterios y
            números de abajo salen del banco de instrumentos, no de haber evaluado una
            plataforma. Para obtener un plan real, empezá una evaluación desde{" "}
            <Link href="/evaluar">configuración</Link>.
          </div>
        )}

        {jobId && spend > 0 && (
          <div className="note-box">
            <b>Armar este plan costó ${spend.toFixed(2)}.</b> Medido por el producto
            llamada por llamada — reconocimiento, rúbrica y consejo de jueces — no
            estimado. Ejecutar las pruebas suma su propio costo, que también se mide.
          </div>
        )}

        {/* Lo que el recorrido ENCONTRÓ, con sus fotos. Esto existía desde la
            primera corrida viva y nunca se mostraba: el producto exploraba la
            plataforma y se guardaba la prueba. La rúbrica de abajo sale de
            este recorrido, y eso hay que poder verlo, no creerlo. */}
        {jobId && recon && (
          <section style={{ padding: "44px 0 0" }}>
            <div className="eyebrow">Antes de proponer nada</div>
            <h3 style={{ fontSize: 22, marginTop: 8 }}>
              evalkit recorrió la plataforma. Esto encontró.
            </h3>
            <p className="fhint" style={{ marginTop: 8, maxWidth: 680 }}>
              Entró con la cuenta de prueba, en modo sólo lectura — mira y captura,
              no puede escribirle a nadie. El plan de abajo sale de este recorrido.
            </p>

            {recon.surfaces?.surfaces && (
              <div className="crit-list" style={{ marginTop: 18 }}>
                {Object.entries(recon.surfaces.surfaces).map(([key, surf]) => (
                  <div className="crit" key={key}>
                    <div className="crit-head">
                      <span className="mono">{key}</span>
                      <span className="obs o-approved">encontrada</span>
                    </div>
                    <h4 style={{ marginTop: 6 }}>{surf.name ?? key}</h4>
                    {(surf.steps?.length ?? 0) > 0 && (
                      <ol className="steps-list">
                        {surf.steps!.map((st, i) => (
                          <li key={i}>{st}</li>
                        ))}
                      </ol>
                    )}
                  </div>
                ))}
                {Object.entries(recon.surfaces.unavailable ?? {}).map(([key, why]) => (
                  <div className="crit" key={key}>
                    <div className="crit-head">
                      <span className="mono">{key}</span>
                      <span className="obs o-objected">no disponible</span>
                    </div>
                    <p className="fhint" style={{ marginTop: 8 }}>{why}</p>
                  </div>
                ))}
              </div>
            )}

            {recon.shots.length > 0 && (
              <>
                <p className="fhint" style={{ marginTop: 22 }}>
                  Las capturas del recorrido — clic para abrir en tamaño real:
                </p>
                <div className="shots-grid">
                  {recon.shots.map((name) => {
                    const src = `/api/jobs/${encodeURIComponent(jobId)}/evidence/${encodeURIComponent(name)}`;
                    const caption = name
                      .replace(/^\d{8}T\d+_/, "")
                      .replace(/\.png$/, "")
                      .replace(/_/g, " ");
                    return (
                      <a className="shot" key={name} href={src} target="_blank" rel="noreferrer">
                        {/* `decoding="async"`: son capturas de 1440px a
                            resolucion completa (~750 KB cada una) usadas como
                            miniaturas. Decodificarlas sincronicamente congelo
                            el compositor con diez en pantalla. */}
                        {/* eslint-disable-next-line @next/next/no-img-element */}
                        <img src={src} alt={caption} loading="lazy" decoding="async" />
                        <figcaption className="fhint">{caption}</figcaption>
                      </a>
                    );
                  })}
                </div>
              </>
            )}
          </section>
        )}

        {/* ---------- two-axis summary ---------- */}
        <div className="stats" style={{ marginTop: 34 }}>
          <div className="stat">
            <div className="n">{counts.OBSERVABLE_EN_PLATAFORMA}</div>
            <div className="k">se verifican recorriendo la plataforma</div>
          </div>
          <div className="stat">
            <div className="n">{counts.REQUIERE_DOCUMENTACION}</div>
            <div className="k">requieren un documento del proveedor</div>
          </div>
          <div className="stat">
            <div className="n">{counts.REQUIERE_DATOS}</div>
            <div className="k">requieren acceso a datos</div>
          </div>
          <div className="stat">
            <div className="n">{counts.REQUIERE_ESTUDIO}</div>
            <div className="k">requieren un estudio de impacto</div>
          </div>
        </div>
        <p className="fhint" style={{ marginTop: 12 }}>
          {scoredPoints(criteria)} puntos entran al denominador · {outsidePoints(criteria)} quedan
          fuera y se reportan como <strong>NO EVALUADO</strong> con el pedido
          correspondiente. Un criterio que no se puede verificar no se descarta: se
          convierte en algo que le pedís al proveedor.
        </p>

        {/* ---------- sampling ---------- */}
        <section style={{ padding: "48px 0 0" }}>
          <h3 style={{ fontSize: 24 }}>Muestreo</h3>
          <p className="fhint" style={{ marginTop: 8, maxWidth: 640 }}>
            Cuánto hay que mirar para que el resultado signifique algo. Estas decisiones
            viven acá, a la vista y editables — no adentro del texto de un criterio, donde
            parecerían escritas por el autor del instrumento.
          </p>

          <div className="sampling">
            <div className="samp-group">
              <span className="samp-tag live">Controla la corrida</span>
              <div className="grid2" style={{ marginTop: 4 }}>
                <label className="field">
                  <span className="flabel">Sondeos por criterio</span>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={s.repeats}
                    onChange={(e) => setS({ ...s, repeats: +e.target.value })}
                  />
                  <span className="fhint">
                    Cuántas veces se ejecuta cada prueba contra la plataforma. La
                    plataforma no es determinista.
                  </span>
                </label>
                <label className="field">
                  <span className="flabel">Juicios por evidencia</span>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={s.repeatJudging}
                    onChange={(e) => setS({ ...s, repeatJudging: +e.target.value })}
                  />
                  <span className="fhint">
                    Cuántas veces se puntúa la misma evidencia guardada. Mide el ruido del
                    puntuador.
                  </span>
                </label>
              </div>
            </div>

            <div className="samp-group">
              <span className="samp-tag scope">Describe el alcance</span>
              <p className="fhint" style={{ marginTop: 6, marginBottom: 2 }}>
                Estos campos quedan registrados en el informe para que se sepa sobre qué se
                evaluó, pero <strong>hoy no multiplican el recorrido</strong>: las pruebas
                se generan por criterio, no por lección. Decirlo es más útil que una
                perilla que no mueve nada.
              </p>
              <div className="grid2">
              <label className="field">
                <span className="flabel">Materias</span>
                <input
                  className="input"
                  value={s.subjects.join(", ")}
                  onChange={(e) => setS({ ...s, subjects: e.target.value.split(/,\s*/) })}
                />
              </label>
              <label className="field">
                <span className="flabel">Grados</span>
                <input
                  className="input"
                  value={s.grades.join(", ")}
                  onChange={(e) => setS({ ...s, grades: e.target.value.split(/,\s*/) })}
                />
              </label>
            </div>
            <label className="field">
              <span className="flabel">Lecciones por grado</span>
              <input
                className="input"
                type="number"
                min={1}
                value={s.lessonsPerGrade}
                onChange={(e) => setS({ ...s, lessonsPerGrade: +e.target.value })}
                style={{ maxWidth: 200 }}
              />
            </label>
            </div>

            <div className="rationale">
              <span className="flabel">Por qué esto</span>
              <p>{s.rationale}</p>
            </div>
          </div>
        </section>

        {/* ---------- criteria ---------- */}
        <section style={{ padding: "48px 0 0" }}>
          <h3 style={{ fontSize: 24 }}>
            Rúbrica · {criteria.length - criteria.filter((c) => dropped.has(c.id)).length}
            {dropped.size > 0 && (
              <span className="fhint" style={{ fontWeight: 400 }}>
                {" "}
                de {criteria.length} criterios (quitaste algunos)
              </span>
            )}
          </h3>
          {jobId && (
            <p className="fhint" style={{ marginTop: 6, maxWidth: 680 }}>
              Sacá lo que no aplique a tu plataforma: lo quitado no se ejecuta ni se
              paga. Lo que dejes acá es lo que se corre.
            </p>
          )}

          <div className="crit-list">
            {[...observable, ...notObservable].map((c) => (
              <div className={`crit ${dropped.has(c.id) ? "dropped" : ""}`} key={c.id}>
                <div className="crit-head">
                  <div>
                    <span className="mono">{c.id}</span>
                    <span className="crit-area">{c.area}</span>
                  </div>
                  <div className="chips">
                    <span className={`obs ${OBS_CLASS[c.observability]}`}>
                      {OBS_LABEL[c.observability]}
                    </span>
                    {jobId && (
                      <button
                        type="button"
                        className="drop-btn"
                        onClick={() => toggleDrop(c.id)}
                      >
                        {dropped.has(c.id) ? "Restaurar" : "Quitar"}
                      </button>
                    )}
                  </div>
                </div>
                <h4>{c.name}</h4>
                <div className="anchors">
                  <div>
                    <span className="alab ok">Cumple</span> {c.passCriteria}
                  </div>
                  <div>
                    <span className="alab bad">No cumple</span> {c.failCriteria}
                  </div>
                </div>

                {c.evidenciaRequerida && (
                  <div className="ask">
                    <strong>{OBS_HELP[c.observability]}</strong> Pedir: {c.evidenciaRequerida}
                  </div>
                )}

                <div className="prov">
                  <span className={c.source.type === "generated" ? "prov-gen" : "prov-ok"}>
                    {SRC_LABEL[c.source.type]}
                  </span>
                  {c.source.type !== "generated" && (
                    <span className="mono prov-loc">
                      {c.source.framework} · {c.source.location}
                    </span>
                  )}
                  <span className="pts">{c.points} pts</span>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* ---------- probes ---------- */}
        <section style={{ padding: "48px 0 0" }}>
          <h3 style={{ fontSize: 24 }}>Pruebas · {probes.length}</h3>
          <p className="fhint" style={{ marginTop: 8 }}>
            Sólo se generan pruebas para los criterios verificables en la plataforma. Cada
            una pasó por cinco jueces con responsabilidades distintas.
          </p>

          <div className="crit-list">
            {probes.map((p) => (
              <div
                className={`crit ${p.requiresSignoff ? "gated" : ""} ${
                  dropped.has(p.probeId) || dropped.has(p.criterionId) ? "dropped" : ""
                }`}
                key={p.probeId}
              >
                <div className="crit-head">
                  <span className="mono">{p.probeId}</span>
                  <span className="chips">
                    {typeof p.roundsUsed === "number" && (
                      <span className="obs o-round">
                        consejo: {p.roundsUsed} ronda{p.roundsUsed === 1 ? "" : "s"}
                      </span>
                    )}
                    {typeof p.approved === "boolean" &&
                      (p.approved ? (
                        <span className="obs o-approved">aprobada por el consejo</span>
                      ) : (
                        <span className="obs o-objected">con objeciones</span>
                      ))}
                    {jobId && !dropped.has(p.criterionId) && (
                      <button
                        type="button"
                        className="drop-btn"
                        onClick={() => toggleDrop(p.probeId)}
                      >
                        {dropped.has(p.probeId) ? "Restaurar" : "Quitar"}
                      </button>
                    )}
                    {p.requiresSignoff && <span className="obs o-gate">Requiere tu firma</span>}
                  </span>
                </div>
                <ol className="steps-list">
                  {p.steps.map((st) => (
                    <li key={st.n}>
                      {st.action}
                      {st.capture && <em> — captura: {st.capture}</em>}
                    </li>
                  ))}
                </ol>
                {/* CÓMO se decidió: la ronda final del consejo, un veredicto por
                    asiento. Es la trazabilidad que hace mostrable el proceso —
                    no "el sistema aprobó", sino quién, qué dijo y contra qué. */}
                {(p.judges?.length ?? 0) > 0 && (
                  <details className="council">
                    <summary>Qué dijo el consejo ({p.judges!.length} asientos)</summary>
                    <ul>
                      {p.judges!.map((j) => (
                        <li key={j.judge}>
                          <span className="mono">{j.judge}</span>{" "}
                          <span
                            className={
                              j.verdict === "approve" ? "v-word ok" : "v-word obj"
                            }
                          >
                            {j.verdict === "approve" ? "aprueba" : j.verdict}
                          </span>
                          {j.objectionTarget === "criterio" && (
                            <span className="obs o-instr">apunta al criterio</span>
                          )}
                          {j.verdict !== "approve" && (j.reasons?.[0] ?? "") && (
                            <div className="fhint">{j.reasons[0]}</div>
                          )}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
                {p.signoffReason && <div className="gate-note">{p.signoffReason}</div>}
              </div>
            ))}
          </div>
        </section>

        {instrumentFindings.length > 0 && (
          <section style={{ padding: "40px 0 0" }}>
            <h3 style={{ fontSize: 22 }}>
              Hallazgos sobre el instrumento · {instrumentFindings.length}
            </h3>
            <p className="fhint" style={{ marginTop: 8, maxWidth: 660 }}>
              El consejo encontró defectos en los CRITERIOS mismos — no en las
              pruebas. Van dirigidos al autor del instrumento, no al proveedor, y
              no bloquearon ninguna prueba: la herramienta también audita la vara
              con la que mide.
            </p>
            <div className="crit-list">
              {instrumentFindings.map(({ probe, judge }, i) => (
                <div className="crit" key={`${probe.probeId}-${judge.judge}-${i}`}>
                  <div className="crit-head">
                    <span className="mono">{probe.criterionId}</span>
                    <span className="obs o-instr">señalado por {judge.judge}</span>
                  </div>
                  <p className="fhint" style={{ marginTop: 8 }}>
                    {judge.reasons?.[0] ?? "(sin motivo registrado)"}
                  </p>
                </div>
              ))}
            </div>
          </section>
        )}

        {error && (
          <div className="err-box" role="alert" style={{ marginTop: 32 }}>
            {error}
          </div>
        )}

        <div className="cta-row" style={{ marginTop: 44 }}>
          {/* With no job there is nothing to approve. A disabled primary button
              still LOOKS like the way forward — someone read this screen as a
              real plan, clicked, and nothing happened. Offer the real next
              step instead of a dead control. */}
          {jobId ? (
            <button className="btn" onClick={approve} disabled={approving}>
              {approving ? "Enviando el plan…" : "Aprobar y ejecutar"}
            </button>
          ) : (
            <Link className="btn" href="/evaluar">
              Empezar una evaluación real
            </Link>
          )}
          <Link className="btn btn-ghost" href="/evaluar">
            Volver a configuración
          </Link>
        </div>
        <p className="fhint" style={{ marginTop: 12 }}>
          {jobId
            ? "La prueba marcada requiere tu firma explícita: no se ejecuta con el resto."
            : "Nada de esta pantalla se puede ejecutar: son pruebas de ejemplo del banco, no pruebas pensadas para tu plataforma."}
        </p>
      </div>
    </>
  );
}

export default function Plan() {
  // useSearchParams needs a Suspense boundary above it.
  return (
    <Suspense fallback={<Shell step="Paso 2 de 4 · Plan"><p className="lead">Cargando…</p></Shell>}>
      <PlanScreen />
    </Suspense>
  );
}
