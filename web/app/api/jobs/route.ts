import { NextResponse } from "next/server";
import { startJob } from "../../lib/api";
import type { StartJobInput } from "../../lib/api";

/**
 * Proxy to the engine's POST /jobs.
 *
 * The browser never talks to the engine directly: the service token would have
 * to ship in the bundle, and the engine can start paid runs. Everything goes
 * through here, where the token stays server-side.
 */
export async function POST(req: Request) {
  let body: StartJobInput;
  try {
    body = (await req.json()) as StartJobInput;
  } catch {
    return NextResponse.json({ error: "El cuerpo no es JSON válido." }, { status: 400 });
  }

  if (!body?.objective?.trim()) {
    return NextResponse.json(
      { error: "Falta el objetivo: de ahí sale la rúbrica." },
      { status: 400 }
    );
  }
  if (!body?.platform?.url?.trim()) {
    return NextResponse.json({ error: "Falta la URL de la plataforma." }, { status: 400 });
  }

  try {
    const out = await startJob(body);
    return NextResponse.json(out, { status: 201 });
  } catch (e) {
    // The message may name the engine URL but never a credential — the engine
    // does not echo them and neither does this layer.
    return NextResponse.json({ error: String(e instanceof Error ? e.message : e) }, { status: 502 });
  }
}
