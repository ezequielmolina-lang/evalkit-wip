import { NextResponse } from "next/server";
import { getJob } from "../../../lib/api";

type Ctx = { params: Promise<{ id: string }> };

/** Polled every couple of seconds by the run screen. Never cached. */
export async function GET(_req: Request, { params }: Ctx) {
  const { id } = await params;
  try {
    return NextResponse.json(await getJob(id), {
      headers: { "cache-control": "no-store" },
    });
  } catch (e) {
    return NextResponse.json(
      { error: String(e instanceof Error ? e.message : e) },
      { status: 502 }
    );
  }
}
