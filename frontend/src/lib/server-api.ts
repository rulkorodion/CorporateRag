import "server-only";

const baseURL = (process.env.RAG_API_URL ?? "http://127.0.0.1:8010").replace(/\/$/, "");
const timeoutMs = Number(process.env.RAG_API_TIMEOUT_MS ?? 450_000);

export async function backendFetch(path: string, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  const signal = init?.signal
    ? AbortSignal.any([controller.signal, init.signal])
    : controller.signal;
  try {
    return await fetch(`${baseURL}${path}`, {
      ...init,
      cache: "no-store",
      signal,
    });
  } finally {
    clearTimeout(timeout);
  }
}

export function proxyFailure(error: unknown): Response {
  const timedOut = error instanceof DOMException && error.name === "AbortError";
  return Response.json(
    {
      error: timedOut ? "gateway_timeout" : "backend_unavailable",
      message: timedOut
        ? "Backend response timed out"
        : "Backend is temporarily unavailable",
    },
    { status: timedOut ? 504 : 503 },
  );
}

export async function passThrough(response: Response): Promise<Response> {
  const headers = new Headers();
  for (const name of [
    "content-type",
    "content-disposition",
    "content-length",
    "content-range",
    "accept-ranges",
    "cache-control",
    "x-accel-buffering",
    "x-request-id",
  ]) {
    const value = response.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(response.body, { status: response.status, headers });
}
