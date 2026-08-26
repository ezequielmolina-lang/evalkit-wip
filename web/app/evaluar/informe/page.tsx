import Link from "next/link";
import { AREAS_RESULT, ROWS, RUN_META } from "../../lib/sampleReport";
import type { AreaResult, ResultRow, Verdict } from "../../lib/sampleReport";
import { OBS_LABEL } from "../../lib/types";
import type { ReconInfo } from "../../lib/types";
import { getJob, getLedger, getReport } from "../../lib/api";
import type { LedgerRow } from "../../lib/api";
import PrintButton from "./PrintButton";

/** The engine's report, which is built in exactly these three pieces. */
interface EngineReport {
  areas: AreaResult[];
  rows: ResultRow[];
  meta: typeof RUN_META;
}

const V_CLASS: Record<Verdict, string> = {
  CUMPLE: "v-ok",
  PARCIAL: "v-mid",
  NO_CUMPLE: "v-bad",
  NO_EVALUADO: "v-none",
};
const V_LABEL: Record<Verdict, string> = {
  CUMPLE: "Cumple",
  PARCIAL: "Parcial",
  NO_CUMPLE: "No cumple",
  NO_EVALUADO: "No evaluado",
};

/** Never print a percentage for an area nothing could be observed in. */
function cell(a: (typeof AREAS_RESULT)[number]) {
  if (a.pct === null) return `no evaluado (0 de ${a.total})`;
  return `${a.earned}/${a.possible} · ${a.pct}% (${a.observed} de ${a.total} observables)`;
}

export default async function Informe({
  searchParams,
}: {
  searchParams: Promise<{ job?: string }>;
}) {
  const { job } = await searchParams;

  // A server component, so the engine is called directly with the service
  // token — it never has to reach the browser.
  let areasResult: AreaResult[] = AREAS_RESULT;
  let rowsResult: ResultRow[] = ROWS;
  let meta = RUN_META;
  let loadError = "";

  // El apéndice: el rastro entero, para que un lector escéptico pueda recorrer
  // el argumento completo. Cada pieza ya existía y no se mostraba.
  let ledger: LedgerRow[] = [];
  let probes: {
    probeId: string; criterionId: string; roundsUsed?: number; approved?: boolean;
    requiresSignoff?: boolean; signedOff?: boolean;
    steps: { n: number; action: string; capture?: string }[];
    judges?: { judge: string; verdict: string; reasons: string[]; objectionTarget?: string }[];
  }[] = [];
  // ReconInfo, no un tipo inline: escrito a mano acá TypeScript lo colapsaba a
  // `never` y el apéndice no compilaba. Es el mismo dato que muestra el plan.
  let recon: ReconInfo | null = null;
  let spendUsd = 0;

  if (job) {
    try {
      const rep = (await getReport(job)) as EngineReport;
      areasResult = rep.areas ?? [];
      rowsResult = rep.rows ?? [];
      meta = rep.meta ?? RUN_META;
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
    // El apéndice es un extra: si falla, el informe sigue siendo el informe.
    try {
      ledger = (await getLedger(job)).rows;
    } catch { /* sin rastro, el apéndice omite esa sección */ }
    try {
      const st = await getJob(job);
      spendUsd = st.spendUsd ?? 0;
      const pl = st.plan as { probes?: typeof probes } | undefined;
      probes = pl?.probes ?? [];
      recon = (st as { recon?: ReconInfo }).recon ?? null;
    } catch { /* idem */ }
  }

  const scored = rowsResult.filter((r) => r.verdict !== "NO_EVALUADO");
  const pending = rowsResult.filter((r) => r.verdict === "NO_EVALUADO");
  const earned = scored.reduce((a, r) => a + r.earned, 0);
  const possible = scored.reduce((a, r) => a + r.points, 0);
  const outside = pending.reduce((a, r) => a + r.points, 0);

  return (
    <>
      <nav>
        <div className="wrap inner">
          <Link href="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
            eval<span>kit</span>
          </Link>
          <div className="navlinks">
            <span style={{ fontSize: 13 }}>Paso 4 de 4 · Informe</span>
          </div>
        </div>
      </nav>

      <div className="wrap" style={{ paddingTop: 44, paddingBottom: 90 }}>
        <div className="eyebrow">Diagnóstico</div>
        <h2 style={{ marginTop: 12 }}>{meta.platform}</h2>
        <div className="report-head">
          <p className="fhint" style={{ marginTop: 10 }}>
            {meta.date} · {meta.sampling}
          </p>
          <PrintButton />
        </div>

        {loadError && (
          <div className="err-box" role="alert">
            No se pudo leer el informe del trabajo <span className="mono">{job}</span>. {loadError}
          </div>
        )}

        {!job && !loadError && (
          <div className="note-box">
            <b>Informe de ejemplo.</b> No estás viendo el resultado de una evaluación: estos
            veredictos y hashes ilustran el formato. Para un informe real, empezá una
            evaluación desde <Link href="/evaluar">configuración</Link>.
          </div>
        )}

        {/* headline, stated precisely */}
        <div className="headline">
          <div>
            <div className="big-n">
              {earned}
              <span className="of">/{possible}</span>
            </div>
            <div className="k">
              puntos sobre lo que <strong>se pudo verificar</strong> recorriendo la plataforma
            </div>
          </div>
          <div className="headline-side">
            <div className="big-n small">{outside}</div>
            <div className="k">
              puntos <strong>fuera del denominador</strong>: {pending.length} criterios que ningún
              recorrido puede resolver
            </div>
          </div>
        </div>
        <p className="fhint" style={{ marginTop: 12 }}>
          El puntaje no dice «la plataforma saca {earned} de {possible + outside}». Dice qué se
          verificó, sobre qué, y qué quedó pendiente y por qué.
        </p>

        {/* per area */}
        <section style={{ padding: "44px 0 0" }}>
          <h3 style={{ fontSize: 22 }}>Por área</h3>
          <table className="tbl">
            <thead>
              <tr>
                <th>Área</th>
                <th>Resultado</th>
              </tr>
            </thead>
            <tbody>
              {areasResult.map((a) => (
                <tr key={a.code} className={a.pct === null ? "row-none" : ""}>
                  <td>
                    <span className="mono">{a.code}</span> {a.name}
                  </td>
                  <td className="mono">{cell(a)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="fhint" style={{ marginTop: 10 }}>
            Un área donde nada fue observable dice <strong>«no evaluado»</strong>, nunca 0%. Un
            proveedor que oculta una superficie no debe puntuar mejor que uno que la expone.
          </p>
        </section>

        {/* verified */}
        <section style={{ padding: "44px 0 0" }}>
          <h3 style={{ fontSize: 22 }}>Lo que se verificó · {scored.length}</h3>
          <div className="crit-list">
            {scored.map((r) => (
              <div className="crit" key={r.id}>
                <div className="crit-head">
                  <div>
                    <span className="mono">{r.id}</span>
                    <span className="crit-area">{r.area}</span>
                  </div>
                  <span className={`verdict ${V_CLASS[r.verdict]}`}>{V_LABEL[r.verdict]}</span>
                </div>
                <h4>{r.name}</h4>

                {r.runs && r.runs > 1 ? (
                  <div className={`runs ${r.unanimous ? "" : "split"}`}>
                    {r.runs} sondeos ·{" "}
                    {r.unanimous ? "unánimes" : "NO unánimes — se reporta el veredicto modal"}
                  </div>
                ) : r.runs === 1 ? (
                  // Saying nothing lets a reader assume stability was checked.
                  // One run measures no agreement at all — say so.
                  <div className="runs">1 sondeo · unanimidad no medida</div>
                ) : null}

                {r.rationale && <p className="rat">{r.rationale}</p>}

                {r.evidence.length > 0 && (
                  <div className="ev-list">
                    {r.evidence.map((e) => (
                      <div className="ev" key={e.file}>
                        {/* La captura SE VE. El informe mostraba el nombre del
                            archivo y su hash y escondía la imagen: la prueba de
                            un hallazgo es la foto, no su metadato. El hash sigue
                            abajo — es lo que la vuelve verificable. */}
                        {e.kind === "captura" && job ? (
                          <a
                            className="ev-shot"
                            href={`/api/jobs/${encodeURIComponent(job)}/evidence/${encodeURIComponent(e.file)}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={`/api/jobs/${encodeURIComponent(job)}/evidence/${encodeURIComponent(e.file)}`}
                              alt={e.note ?? e.file}
                              loading="lazy"
                              decoding="async"
                            />
                          </a>
                        ) : (
                          <div className={`ev-thumb ${e.kind === "captura" ? "img" : "txt"}`}>
                            {e.kind === "captura" ? "captura" : "texto"}
                          </div>
                        )}
                        <div>
                          <div className="mono ev-name">{e.file}</div>
                          <div className="fhint" style={{ marginTop: 2 }}>
                            {e.capturedAt} · sha256 {e.sha256}
                          </div>
                          {e.note && <div className="fhint">{e.note}</div>}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                <div className="prov">
                  <span className="prov-ok">instrumento publicado</span>
                  <span className="mono prov-loc">
                    {r.source.framework} · {r.source.location}
                  </span>
                  <span className="pts">
                    {r.earned}/{r.points} pts
                  </span>
                </div>
              </div>
            ))}
          </div>
        </section>

        {/* vendor requests */}
        <section style={{ padding: "44px 0 0" }}>
          <h3 style={{ fontSize: 22 }}>Qué pedirle al proveedor · {pending.length}</h3>
          <p className="fhint" style={{ marginTop: 8, maxWidth: 660 }}>
            Estos criterios importan para tu objetivo pero ningún recorrido puede resolverlos.
            No se descartaron: se convierten en pedidos concretos.
          </p>

          <div className="ask-box">
            <div className="ask-warn">
              <strong>Revisá esta lista antes de enviarla.</strong> Los pedidos los redacta un
              modelo y no se verifican contra ninguna fuente. Un pedido mal formulado sale con
              el nombre del ministerio.
            </div>
            {pending.map((r) => (
              <div className="ask-row" key={r.id}>
                <div className="ask-head">
                  <span className="mono">{r.id}</span>
                  <span className="obs o-doc">{OBS_LABEL[r.observability]}</span>
                  <span className="pts">{r.points} pts fuera</span>
                </div>
                <div className="ask-name">{r.name}</div>
                {/* Un "Pedir:" vacío es peor que no mostrarlo: parece un
                    campo roto. Y para un criterio OBSERVABLE no corresponde
                    pedirle nada al proveedor — no se evaluó porque la prueba
                    no llegó a correr, y eso es lo honesto que hay que decir. */}
                {r.evidenciaRequerida ? (
                  <div className="ask-req">Pedir: {r.evidenciaRequerida}</div>
                ) : r.observability === "OBSERVABLE_EN_PLATAFORMA" ? (
                  <div className="ask-req">
                    Verificable recorriendo la plataforma: quedó sin evaluar porque
                    su prueba no llegó a ejecutarse, no por falta de información
                    del proveedor.
                  </div>
                ) : (
                  <div className="ask-req">
                    El instrumento no especifica qué evidencia lo resolvería.
                    Definilo antes de pedírselo al proveedor.
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>

        {/* ---------------- APÉNDICE ----------------
            «Que todos los pasos se puedan inspeccionar al final.» Ninguna de
            estas piezas es nueva: el recorrido, las rondas del consejo, cada
            artefacto con su hash y su hora ya existían y no se mostraban. Un
            lector escéptico tiene que poder recorrer el argumento entero sin
            preguntarnos nada. */}
        {job && (ledger.length > 0 || probes.length > 0 || recon) && (
          <section className="appendix" style={{ padding: "56px 0 0" }}>
            <h3 style={{ fontSize: 24 }}>Apéndice · el rastro completo</h3>
            <p className="fhint" style={{ marginTop: 8, maxWidth: 720 }}>
              Todo lo que se hizo, en orden, con lo que lo prueba. Está acá para
              que puedas discutir cualquier conclusión sin creernos nada.
            </p>

            {recon?.surfaces?.surfaces && (
              <div className="app-block">
                <h4>A · Lo que se encontró al recorrer la plataforma</h4>
                {Object.entries(recon.surfaces.surfaces).map(([k, surf]) => (
                  <div className="app-item" key={k}>
                    <span className="mono">{k}</span> {surf.name ?? ""}
                    {(surf.steps?.length ?? 0) > 0 && (
                      <ol className="steps-list">
                        {surf.steps!.map((st, i) => <li key={i}>{st}</li>)}
                      </ol>
                    )}
                  </div>
                ))}
                {Object.entries(recon.surfaces.unavailable ?? {}).map(([k, why]) => (
                  <div className="app-item" key={k}>
                    <span className="mono">{k}</span>{" "}
                    <span className="obs o-objected">no disponible</span>
                    <p className="fhint">{why}</p>
                  </div>
                ))}
              </div>
            )}

            {probes.length > 0 && (
              <div className="app-block">
                <h4>B · Cada prueba, y qué dijo el consejo</h4>
                {probes.map((p) => (
                  <div className="app-item" key={p.probeId}>
                    <div className="crit-head">
                      <span className="mono">{p.probeId} · {p.criterionId}</span>
                      <span className="chips">
                        {typeof p.roundsUsed === "number" && (
                          <span className="obs o-round">
                            {p.roundsUsed} ronda{p.roundsUsed === 1 ? "" : "s"}
                          </span>
                        )}
                        {p.requiresSignoff && (
                          <span className="obs o-gate">
                            {p.signedOff ? "firmada" : "requería firma — no se ejecutó"}
                          </span>
                        )}
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
                    {(p.judges?.length ?? 0) > 0 && (
                      <ul className="app-judges">
                        {p.judges!.map((j) => (
                          <li key={j.judge}>
                            <span className="mono">{j.judge}</span>{" "}
                            <span className={j.verdict === "approve" ? "v-word ok" : "v-word obj"}>
                              {j.verdict === "approve" ? "aprueba" : j.verdict}
                            </span>
                            {j.objectionTarget === "criterio" && (
                              <span className="obs o-instr">apunta al criterio</span>
                            )}
                            {j.verdict !== "approve" && j.reasons?.[0] && (
                              <div className="fhint">{j.reasons[0]}</div>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                ))}
              </div>
            )}

            {ledger.length > 0 && (
              <div className="app-block">
                <h4>C · Cada artefacto capturado · {ledger.length}</h4>
                <p className="fhint">
                  En orden. El hash es de los bytes guardados: si el archivo
                  cambiara, deja de coincidir.
                </p>
                <table className="tbl app-ledger">
                  <thead>
                    <tr><th>Hora</th><th>Prueba</th><th>Paso</th><th>Archivo</th><th>sha256</th></tr>
                  </thead>
                  <tbody>
                    {ledger.map((row, i) => (
                      <tr key={`${row.artifact}-${i}`}>
                        <td className="mono">{row.timestamp.replace("T", " ").slice(0, 19)}</td>
                        <td className="mono">{row.testId || row.probeId}</td>
                        <td>{row.note}</td>
                        <td className="mono">{row.artifact}</td>
                        <td className="mono sha">{row.sha256.slice(0, 16)}…</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {spendUsd > 0 && (
              <div className="app-block">
                <h4>D · Lo que costó</h4>
                <p className="fhint">
                  US${spendUsd.toFixed(2)} medidos llamada por llamada — recorrido,
                  rúbrica, consejo de jueces y puntuación. No es una estimación.
                </p>
              </div>
            )}
          </section>
        )}

        {/* provenance of the run itself */}
        <section style={{ padding: "44px 0 0" }}>
          <h3 style={{ fontSize: 22 }}>Cómo se produjo este informe</h3>
          <div className="honest" style={{ marginTop: 16 }}>
            <p>
              <strong>Modelo:</strong> {meta.model} · <strong>huella del juez:</strong>{" "}
              <span className="mono">{meta.judgeSha}</span> ·{" "}
              <strong>huella de la evidencia:</strong>{" "}
              <span className="mono">{meta.evidenceSha}</span>
            </p>
            {/* What this run actually covered, how whole the panel was, and
                whether the probes ran with the wording that was approved. All
                three were computed and stored from the start and none reached
                the page: the report is the artefact that gets forwarded and
                disputed a year later, so a caveat that lives only on the
                review screen protects nobody. Each renders only when the
                engine had something to say -- an older report has no
                stepsDivergence, and "not recorded" must not print as "did not
                diverge". */}
            {meta.coverage && (
              <p style={{ marginTop: 12 }}>
                <strong>Alcance:</strong> {meta.coverage}
              </p>
            )}
            {meta.councilQuorum && (
              <p style={{ marginTop: 12 }}>
                <strong>Consejo:</strong> {meta.councilQuorum}
              </p>
            )}
            {meta.stepsDivergence && (
              <p style={{ marginTop: 12 }}>
                <strong>Pasos:</strong> {meta.stepsDivergence}
              </p>
            )}
            <p style={{ marginTop: 12 }}>{meta.note}</p>
            <p style={{ marginTop: 12 }}>
              <strong>Esto no es una recomendación de compra</strong> y no mide aprendizaje.
              Mide qué hace la plataforma cuando se la recorre, contra el criterio que definiste.
            </p>
          </div>
        </section>

        <div className="cta-row">
          <button className="btn">Descargar informe</button>
          <Link className="btn btn-ghost" href="/evaluar/plan">
            Ver el plan que se ejecutó
          </Link>
        </div>
      </div>
    </>
  );
}
