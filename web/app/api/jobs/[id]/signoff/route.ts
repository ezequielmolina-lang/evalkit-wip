import { NextResponse } from "next/server";
import { signoffProbe } from "../../../../lib/api";

type Ctx = { params: Promise<{ id: string }> };

/** One gated probe, signed off individually and on the record. Never in bulk. */
export async function POST(req: Request, { params }: Ctx) {
  const { id } = await params;
  const body = await req.json().catch(() => null);
  const probeId = body?.probe_id ?? body?.probeId;
  if (!probeId) {
    return NextResponse.json(
      { error: "Falta probe_id: las pruebas bloqueadas se firman de a una." },
      { status: 400 }
    );
  }
  try {
    return NextResponse.json(await signoffProbe(id, String(probeId)));
  } catch (e) {
    return NextResponse.json(
      { error: String(e instanceof Error ? e.message : e) },
      { status: 502 }
    );
  }
}
