import { NextResponse } from "next/server";
import { ENGINE_URL } from "../../lib/api";

/**
 * Proxy to the engine's POST /extract.
 *
 * The browser cannot talk to the engine directly — the service token would
 * have to ship in the bundle. The file rides through here as multipart and the
 * token is attached server-side, exactly like every other engine call.
 *
 * Nothing is stored on this side: the engine returns the extracted text and it
 * goes straight back to the form, which holds it until the job is created.
 */
export async function POST(req: Request) {
  const form = await req.formData().catch(() => null);
  const file = form?.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "No llegó ningún archivo." }, { status: 400 });
  }

  const out = new FormData();
  out.append("file", file, file.name);

  try {
    const res = await fetch(`${ENGINE_URL}/extract`, {
      method: "POST",
      headers: {
        // Sin content-type a mano: fetch pone el boundary del multipart.
        ...(process.env.EVALKIT_SERVICE_TOKEN
          ? { authorization: `Bearer ${process.env.EVALKIT_SERVICE_TOKEN}` }
          : {}),
      },
      body: out,
      cache: "no-store",
    });
    const text = await res.text();
    let body: unknown;
    try {
      body = JSON.parse(text);
    } catch {
      body = { error: text.slice(0, 400) || `El motor respondió ${res.status}.` };
    }
    if (!res.ok) {
      const detail =
        (body as { detail?: { message?: string } | string })?.detail ??
        (body as { error?: string })?.error;
      const message =
        typeof detail === "string"
          ? detail
          : detail?.message ?? `El motor respondió ${res.status}.`;
      return NextResponse.json({ error: message }, { status: res.status });
    }
    return NextResponse.json(body);
  } catch (e) {
    return NextResponse.json(
      { error: String(e instanceof Error ? e.message : e) },
      { status: 502 }
    );
  }
}
