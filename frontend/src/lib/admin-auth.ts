import "server-only";

import { createHmac, pbkdf2Sync, timingSafeEqual } from "node:crypto";

import { cookies } from "next/headers";

export const ADMIN_COOKIE_NAME = "corporate_rag_admin_session";

type SessionPayload = {
  username: string;
  expiresAt: number;
};

function sessionSecret(): string | null {
  const value = process.env.ADMIN_SESSION_SECRET?.trim();
  return value && value.length >= 32 ? value : null;
}

function sign(value: string, secret: string): string {
  return createHmac("sha256", secret).update(value).digest("base64url");
}

function parseCookieHeader(header: string | null): string | null {
  if (!header) return null;
  for (const part of header.split(";")) {
    const [name, ...value] = part.trim().split("=");
    if (name === ADMIN_COOKIE_NAME) return value.join("=");
  }
  return null;
}

export function createAdminSession(username: string): { value: string; maxAge: number } {
  const secret = sessionSecret();
  if (!secret) throw new Error("Admin session secret is not configured");
  const maxAge = Math.max(900, Number(process.env.ADMIN_SESSION_TTL_SECONDS ?? 28_800));
  const payload: SessionPayload = {
    username,
    expiresAt: Math.floor(Date.now() / 1000) + maxAge,
  };
  const encoded = Buffer.from(JSON.stringify(payload)).toString("base64url");
  return { value: `${encoded}.${sign(encoded, secret)}`, maxAge };
}

export function verifyAdminSession(value: string | null): SessionPayload | null {
  const secret = sessionSecret();
  if (!secret || !value) return null;
  const [encoded, signature, extra] = value.split(".");
  if (!encoded || !signature || extra) return null;
  const expected = sign(encoded, secret);
  const actualBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expected);
  if (
    actualBuffer.length !== expectedBuffer.length ||
    !timingSafeEqual(actualBuffer, expectedBuffer)
  ) {
    return null;
  }
  try {
    const payload = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as SessionPayload;
    if (
      typeof payload.username !== "string" ||
      typeof payload.expiresAt !== "number" ||
      payload.expiresAt <= Math.floor(Date.now() / 1000)
    ) {
      return null;
    }
    return payload;
  } catch {
    return null;
  }
}

export async function currentAdmin(): Promise<SessionPayload | null> {
  const store = await cookies();
  return verifyAdminSession(store.get(ADMIN_COOKIE_NAME)?.value ?? null);
}

export function adminFromRequest(request: Request): SessionPayload | null {
  return verifyAdminSession(parseCookieHeader(request.headers.get("cookie")));
}

export function verifyAdminCredentials(username: string, password: string): boolean {
  const expectedUsername = process.env.ADMIN_USERNAME?.trim();
  const stored = process.env.ADMIN_PASSWORD_HASH?.trim();
  if (!expectedUsername || !stored || username !== expectedUsername) return false;
  const [algorithm, roundsText, salt, expected, extra] = stored.split(":");
  if (algorithm !== "pbkdf2_sha256" || extra) return false;
  const rounds = Number(roundsText);
  if (!Number.isSafeInteger(rounds) || rounds < 100_000 || !salt || !expected) return false;
  try {
    const expectedBuffer = Buffer.from(expected, "base64url");
    const actualBuffer = pbkdf2Sync(password, salt, rounds, expectedBuffer.length, "sha256");
    return timingSafeEqual(actualBuffer, expectedBuffer);
  } catch {
    return false;
  }
}

export function requestIsSameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (!origin) return false;
  try {
    const originUrl = new URL(origin);
    const forwardedHost = request.headers.get("x-forwarded-host");
    const host = forwardedHost ?? request.headers.get("host") ?? new URL(request.url).host;
    const forwardedProtocol = request.headers.get("x-forwarded-proto");
    const protocol = forwardedProtocol ? `${forwardedProtocol}:` : new URL(request.url).protocol;
    return originUrl.host === host && originUrl.protocol === protocol;
  } catch {
    return false;
  }
}

export function adminCookieSecure(request: Request): boolean {
  const configured = process.env.ADMIN_COOKIE_SECURE?.trim().toLowerCase();
  if (configured === "true") return true;
  if (configured === "false") return false;
  return (request.headers.get("x-forwarded-proto") ?? new URL(request.url).protocol.replace(":", "")) === "https";
}
