import { backendFetch, passThrough, proxyFailure } from "@/lib/server-api";

export async function POST(request: Request) {
  try {
    const body = await request.text();
    const response = await backendFetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      signal: request.signal,
    });
    return passThrough(response);
  } catch (error) {
    return proxyFailure(error);
  }
}
