import { NextRequest, NextResponse } from "next/server";

import { adminFromRequest, requestIsSameOrigin } from "@/lib/admin-auth";
import { requestImmediateUpdate } from "@/lib/admin-storage";

export async function POST(request: NextRequest) {
  const admin = adminFromRequest(request);
  if (!admin) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!requestIsSameOrigin(request)) {
    return NextResponse.json({ error: "invalid_origin" }, { status: 403 });
  }
  await requestImmediateUpdate(admin.username);
  return NextResponse.json({ ok: true, message: "Обновление поставлено в очередь." }, { status: 202 });
}

