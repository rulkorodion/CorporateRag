import { NextRequest, NextResponse } from "next/server";

import { demoAnalyticsSummary } from "@/lib/admin-analytics";
import { adminFromRequest } from "@/lib/admin-auth";
import { backendFetch, passThrough, proxyFailure } from "@/lib/server-api";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  if (!adminFromRequest(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  const requestedDays = Number(request.nextUrl.searchParams.get("days") ?? 7);
  const days = Number.isInteger(requestedDays) && requestedDays >= 1 && requestedDays <= 365
    ? requestedDays
    : 7;
  const preview = request.nextUrl.searchParams.get("preview") === "1";

  if (preview && process.env.NODE_ENV !== "production") {
    return NextResponse.json(demoAnalyticsSummary(days), {
      headers: { "Cache-Control": "no-store" },
    });
  }

  try {
    const response = await backendFetch(`/api/analytics/summary?days=${days}`, {
      signal: request.signal,
    });
    return passThrough(response);
  } catch (error) {
    return proxyFailure(error);
  }
}
