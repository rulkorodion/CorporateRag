import { backendFetch, passThrough, proxyFailure } from "@/lib/server-api";

export async function GET() {
  try {
    return passThrough(await backendFetch("/health"));
  } catch (error) {
    return proxyFailure(error);
  }
}
