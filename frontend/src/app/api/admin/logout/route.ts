import { NextRequest, NextResponse } from "next/server";

import { ADMIN_COOKIE_NAME, adminCookieSecure, requestIsSameOrigin } from "@/lib/admin-auth";

export async function POST(request: NextRequest) {
  if (!requestIsSameOrigin(request)) {
    return NextResponse.json({ error: "invalid_origin" }, { status: 403 });
  }
  const response = NextResponse.json({ ok: true });
  response.cookies.set(ADMIN_COOKIE_NAME, "", {
    httpOnly: true,
    sameSite: "strict",
    secure: adminCookieSecure(request),
    path: "/",
    maxAge: 0,
  });
  return response;
}

