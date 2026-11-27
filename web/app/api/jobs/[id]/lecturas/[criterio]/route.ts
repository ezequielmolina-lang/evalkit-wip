import { NextResponse } from "next/server";
import { editReading } from "../../../../../lib/api";
import type { ReadingEdit } from "../../../../../lib/types";

type Ctx = { params: Promise<{ id: string; criterio: string }> };

/**
 * A person corrects the reading of ONE criterion's rule, or picks one of its
 * samples, before approving the plan. Its own route on purpose: correcting the
 * yardstick is not editing a probe, and it is not an approval.
 *
 * The body carries who decides (`by`) and either `changes` (the fields that
 * change) or `sample` (which sample stands). The engine refuses a correction
 * with no name, with a field that does not exist or that changes nothing.
 */
export async function POST(req: Request, { params }: Ctx) {
  const { id, criterio } = await params;
  const body = (await req.json().catch(() => null)) as Partial<ReadingEdit> | null;
  const by = String(body?.by ?? "").trim();
  if (!by) {
    return NextResponse.json(
      { error: "Falta quién corrige: la lectura de una regla la firma quien la decide." },
      { status: 400 }
    );
  }
  const hasChanges = !!body?.changes && Object.keys(body.changes).length > 0;
  const hasSample = typeof body?.sample === "number";
  if (hasChanges === hasSample) {
    return NextResponse.json(
      { error: "Mandar los campos que cambian o la muestra elegida: una de las dos cosas." },
      { status: 400 }
    );
  }
  const edit: ReadingEdit = hasSample
    ? { by, sample: body!.sample }
    : { by, changes: body!.changes };
  try {
    return NextResponse.json(await editReading(id, criterio, edit));
  } catch (e) {
    return NextResponse.json(
      { error: String(e instanceof Error ? e.message : e) },
      { status: 502 }
    );
  }
}
