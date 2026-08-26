import { ENGINE_URL } from "../../../../../lib/api";

type Ctx = { params: Promise<{ id: string; artifact: string }> };

/**
 * Streams a captured artefact (screenshot, transcript) from the engine.
 *
 * This exists because the report renders evidence with plain <img src>, which
 * the browser fetches directly — and the browser has no service token, nor
 * should it. So the proxy attaches the token server-side and passes the bytes
 * through.
 */
export async function GET(_req: Request, { params }: Ctx) {
  const { id, artifact } = await params;
  const token = process.env.EVALKIT_SERVICE_TOKEN;

  const upstream = await fetch(
    `${ENGINE_URL}/jobs/${encodeURIComponent(id)}/evidence/${encodeURIComponent(artifact)}`,
    { headers: token ? { authorization: `Bearer ${token}` } : {}, cache: "no-store" }
  ).catch(() => null);

  if (!upstream || !upstream.ok) {
    return new Response("No se pudo recuperar la evidencia.", {
      status: upstream?.status ?? 502,
    });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": upstream.headers.get("content-type") ?? "application/octet-stream",
      // Evidence is immutable once captured and sealed with its hash.
      "cache-control": "private, max-age=3600",
    },
  });
}
