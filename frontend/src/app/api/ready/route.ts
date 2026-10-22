import { backendFetch, passThrough, proxyFailure } from "@/lib/server-api";

export async function GET() {
  try {
    return passThrough(await backendFetch("/ready"));
  } catch (error) {
    return proxyFailure(error);
  }
}
