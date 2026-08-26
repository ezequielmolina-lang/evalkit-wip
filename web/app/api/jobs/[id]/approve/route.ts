import { NextResponse } from "next/server";
import { approvePlan } from "../../../../lib/api";

type Ctx = { params: Promise<{ id: string }> };

/**
 * Sends the reviewed plan. This does NOT approve gated probes — the engine
 * reads requiresSignoff from its own stored copy, not from this payload, so a
 * flipped flag here changes nothing. Signoff is a separate, explicit call.
 */
export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json().catch(() => null);
  if (!body?.plan) {
    return NextResponse.json({ error: "Falta el plan revisado." }, { status: 400 });
  }
  try {
    return NextResponse.json(await approvePlan(id, body.plan));
  } catch (e) {
    return NextResponse.json(
      { error: String(e instanceof Error ? e.message : e) },
      { status: 502 }
    );
  }
}
