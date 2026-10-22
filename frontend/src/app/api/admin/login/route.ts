import { NextRequest, NextResponse } from "next/server";

import {
  ADMIN_COOKIE_NAME,
  adminCookieSecure,
  createAdminSession,
  requestIsSameOrigin,
  verifyAdminCredentials,
} from "@/lib/admin-auth";

const attempts = new Map<string, { count: number; resetAt: number }>();
const WINDOW_MS = 15 * 60 * 1000;
const MAX_ATTEMPTS = 5;

function clientKey(request: NextRequest): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    request.headers.get("x-real-ip") ||
    "unknown"
  );
}

export async function POST(request: NextRequest) {
  if (!requestIsSameOrigin(request)) {
    return NextResponse.json({ error: "invalid_origin", message: "Недопустимый источник запроса." }, { status: 403 });
  }
  if (!process.env.ADMIN_USERNAME || !process.env.ADMIN_PASSWORD_HASH || !process.env.ADMIN_SESSION_SECRET) {
    return NextResponse.json({ error: "not_configured", message: "Доступ редактора ещё не настроен." }, { status: 503 });
  }
  const key = clientKey(request);
  const now = Date.now();
  const current = attempts.get(key);
  if (current && current.resetAt > now && current.count >= MAX_ATTEMPTS) {
    return NextResponse.json(
      { error: "rate_limited", message: "Слишком много попыток. Повторите через 15 минут." },
      { status: 429 },
    );
  }
  const body = (await request.json().catch(() => null)) as { username?: unknown; password?: unknown } | null;
  const username = typeof body?.username === "string" ? body.username.trim() : "";
  const password = typeof body?.password === "string" ? body.password : "";
  if (!verifyAdminCredentials(username, password)) {
    attempts.set(key, {
      count: current && current.resetAt > now ? current.count + 1 : 1,
      resetAt: current && current.resetAt > now ? current.resetAt : now + WINDOW_MS,
    });
    return NextResponse.json({ error: "invalid_credentials", message: "Неверный логин или пароль." }, { status: 401 });
  }
  attempts.delete(key);
  const session = createAdminSession(username);
  const response = NextResponse.json({ ok: true });
  response.cookies.set(ADMIN_COOKIE_NAME, session.value, {
    httpOnly: true,
    sameSite: "strict",
    secure: adminCookieSecure(request),
    path: "/",
    maxAge: session.maxAge,
  });
  return response;
}

