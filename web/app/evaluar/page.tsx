"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

const AREAS = [
  { code: "D1", name: "Diseño instruccional", hint: "Secuencia, carga cognitiva, práctica" },
  { code: "D3", name: "Práctica y repaso", hint: "Intercalado, repaso espaciado" },
  { code: "D4", name: "Evaluación formativa", hint: "Retroalimentación, dominio" },
  { code: "D9", name: "Tutor de IA", hint: "Confiabilidad, seguridad, calidad pedagógica" },
  { code: "D10", name: "Panel del docente", hint: "Visibilidad, alertas, accionabilidad" },
  { code: "PRIV", name: "Privacidad y cumplimiento", hint: "Datos de menores, contratos" },
  { code: "INCL", name: "Inclusión y accesibilidad", hint: "Idioma, discapacidad, conectividad" },
  { code: "EVID", name: "Evidencia de eficacia", hint: "Estudios previos, niveles ESSA" },
];

export default function Setup() {
  const router = useRouter();
  const [areas, setAreas] = useState<string[]>(["D9", "PRIV"]);
  const [hasCreds, setHasCreds] = useState(true);
  // Documentos REALES. Antes esto era una lista de nombres inventados: hacer
  // clic en la caja agregaba "norma_ministerial_1.pdf" sin que existiera
  // ningun archivo. Ahora cada entrada trae el texto extraido por el motor.
  const [files, setFiles] = useState<{ name: string; text: string; chars: number }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");

  const [url, setUrl] = useState("");
  const [objective, setObjective] = useState("");
  const [country, setCountry] = useState("SV");
  const [deployment, setDeployment] = useState("pre");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const toggle = (c: string) =>
    setAreas((a) => (a.includes(c) ? a.filter((x) => x !== c) : [...a, c]));

  async function onPickFiles(list: FileList | null) {
    if (!list || list.length === 0) return;
    setUploadError("");
    setUploading(true);
    try {
      for (const file of Array.from(list)) {
        const fd = new FormData();
        fd.append("file", file);
        const r = await fetch("/api/extract", { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d?.error ?? `No se pudo leer ${file.name}.`);
        setFiles((prev) => [
          ...prev.filter((f) => f.name !== d.name),
          { name: d.name, text: d.text, chars: d.chars },
        ]);
      }
    } catch (e) {
      setUploadError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  }

  /**
   * Starts a REAL job. This button used to be a `<Link>` to the plan screen,
   * which rendered sample data — the screens looked live while the engine had
   * never been asked anything. Nothing downstream can be real without the job
   * id this call returns.
   */
  async function armarPlan() {
    setError("");
    if (!objective.trim()) return setError("Falta el objetivo: de ahí sale la rúbrica.");
    if (!url.trim()) return setError("Falta la URL de la plataforma.");
    if (areas.length === 0) return setError("Elegí al menos un área para evaluar.");

    setBusy(true);
    try {
      // The engine's input has no country/deployment field, and dropping them
      // silently would lose the two facts that decide which anchors apply.
      // They ride in the objective, which is what the rubric is built from.
      const ctx =
        `${objective.trim()}\n\nPaís de operación: ${country || "sin definir"}. ` +
        (deployment === "live"
          ? "La plataforma YA está en uso por alumnos reales."
          : "Evaluación previa al despliegue: todavía no hay alumnos usándola.");

      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          objective: ctx,
          areas,
          platform: {
            url: url.trim(),
            ...(hasCreds && username ? { username, password } : {}),
          },
          // El motor extrae criterios de estos documentos CITANDO su origen.
          documents: files.map((f) => ({ name: f.name, text: f.text })),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error ?? `El servidor respondió ${res.status}.`);
      const jobId = data.job_id ?? data.jobId;
      if (!jobId) throw new Error("El motor no devolvió un identificador de trabajo.");
      router.push(`/evaluar/plan?job=${encodeURIComponent(jobId)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <>
      <nav>
        <div className="wrap inner">
          <Link href="/" className="brand" style={{ textDecoration: "none", color: "inherit" }}>
            eval<span>kit</span>
          </Link>
          <div className="navlinks">
            <span style={{ fontSize: 13 }}>Paso 1 de 4 · Configuración</span>
          </div>
        </div>
      </nav>

      <div className="wrap" style={{ paddingTop: 44, paddingBottom: 80, maxWidth: 860 }}>
        <div className="eyebrow">Nueva evaluación</div>
        <h2 style={{ marginTop: 12 }}>¿Qué plataforma vamos a evaluar?</h2>
        <p className="lead" style={{ fontSize: 16 }}>
          Nada se ejecuta todavía. Con esto se arma un plan —rúbrica, pruebas y
          muestreo— que vas a poder revisar y corregir antes de que toque la plataforma.
        </p>

        {/* ---- plataforma ---- */}
        <div className="fieldset">
          <h3>La plataforma</h3>

          <label className="field">
            <span className="flabel">URL de acceso</span>
            <input
              className="input"
              placeholder="https://app.plataforma.com"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>

          <label className="field">
            <span className="flabel">¿Qué es y para quién?</span>
            <textarea
              className="input"
              rows={3}
              placeholder="Un tutor de IA de matemática para 1.º a 6.º grado en escuelas públicas del país."
              value={objective}
              onChange={(e) => setObjective(e.target.value)}
            />
            <span className="fhint">
              De acá sale la rúbrica. Cuanto más concreto el objetivo, más preciso el marco.
            </span>
          </label>

          <div className="grid2">
            <label className="field">
              <span className="flabel">País donde va a operar</span>
              <select
                className="input"
                value={country}
                onChange={(e) => setCountry(e.target.value)}
              >
                <option value="SV">el país</option>
                <option value="PE">Perú</option>
                <option value="CL">Chile</option>
                <option value="EC">Ecuador</option>
                <option value="CO">Colombia</option>
                <option value="">Otro / todavía no definido</option>
              </select>
              <span className="fhint">
                No es un dato decorativo: fija las anclas del país. La rúbrica del país
                exige la Línea 132/134 (ISNA); en otro país esa línea no existe y el
                criterio quedaría mal escrito sin que nadie lo note.
              </span>
            </label>

            <label className="field">
              <span className="flabel">¿La plataforma ya está en uso?</span>
              <select
                className="input"
                value={deployment}
                onChange={(e) => setDeployment(e.target.value)}
              >
                <option value="pre">Todavía no — evaluación previa al despliegue</option>
                <option value="live">Sí, ya hay alumnos usándola</option>
              </select>
              <span className="fhint">
                Evaluar antes del despliegue es el estándar: no hay alumnos ni orientadores
                reales, así que los sondeos de seguridad se corren sin riesgo para nadie.
              </span>
            </label>
          </div>

          <div className="field">
            <label className="check">
              <input
                type="checkbox"
                checked={hasCreds}
                onChange={(e) => setHasCreds(e.target.checked)}
              />
              <span>La plataforma requiere usuario y contraseña</span>
            </label>
          </div>

          {hasCreds && (
            <div className="creds">
              <div className="grid2">
                <label className="field">
                  <span className="flabel">Usuario (cuenta de prueba)</span>
                  <input
                    className="input"
                    placeholder="alumno.demo@…"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                  />
                </label>
                <label className="field">
                  <span className="flabel">Contraseña</span>
                  <input
                    className="input"
                    type="password"
                    placeholder="••••••••"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </label>
              </div>
              <p className="fhint">
                Se guardan cifradas y nunca se muestran de nuevo. Usá cuentas de prueba o
                demo que el proveedor haya entregado para la evaluación, no cuentas de
                alumnos reales.
              </p>
            </div>
          )}
        </div>

        {/* ---- criterios ---- */}
        <div className="fieldset">
          <h3>Qué querés evaluar</h3>
          <p className="fhint" style={{ marginTop: 6, marginBottom: 16 }}>
            Estas áreas vienen del banco de instrumentos. Si tu objetivo pide algo que no
            está acá, se genera igual y queda marcado como sin respaldo.
          </p>

          <div className="areas">
            {AREAS.map((a) => (
              <button
                type="button"
                key={a.code}
                className={`area ${areas.includes(a.code) ? "on" : ""}`}
                onClick={() => toggle(a.code)}
              >
                <span className="area-name">{a.name}</span>
                <span className="area-hint">{a.hint}</span>
              </button>
            ))}
          </div>

          <label className="field" style={{ marginTop: 22 }}>
            <span className="flabel">Tus propios criterios (opcional)</span>
            <label className="drop">
              <input
                type="file"
                multiple
                accept=".pdf,.docx,.xlsx,.xlsm,.txt,.md,.csv"
                onChange={(e) => {
                  onPickFiles(e.target.files);
                  e.target.value = "";
                }}
                style={{ display: "none" }}
              />
              {uploading
                ? "Leyendo el documento…"
                : "Adjuntar una norma, un pliego de licitación o tu propia rúbrica"}
              <span className="fhint" style={{ display: "block", marginTop: 6 }}>
                PDF, Word o Excel · Los criterios se extraen citando el documento y la
                página. Una norma tuya pesa más que cualquier cosa que generemos.
              </span>
            </label>
            {uploadError && (
              <div className="err-box" role="alert" style={{ marginTop: 12 }}>
                {uploadError}
              </div>
            )}
            {files.length > 0 && (
              <ul className="filelist">
                {files.map((f) => (
                  <li key={f.name}>
                    {f.name}{" "}
                    <span className="fhint">
                      · {f.chars.toLocaleString("es")} caracteres leídos
                    </span>{" "}
                    <button
                      type="button"
                      className="drop-btn"
                      onClick={() =>
                        setFiles((prev) => prev.filter((x) => x.name !== f.name))
                      }
                    >
                      Quitar
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </label>
        </div>

        {error && (
          <div className="err-box" role="alert">
            {error}
          </div>
        )}

        <div className="cta-row">
          <button className="btn" onClick={armarPlan} disabled={busy}>
            {busy ? "Armando el plan…" : "Armar el plan"}
          </button>
          <span className="fhint">
            {areas.length} área{areas.length === 1 ? "" : "s"} seleccionada
            {areas.length === 1 ? "" : "s"}
            {busy && " · el motor está construyendo la rúbrica y revisando las pruebas"}
          </span>
        </div>
      </div>
    </>
  );
}
