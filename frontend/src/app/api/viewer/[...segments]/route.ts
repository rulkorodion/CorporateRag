import { backendFetch, passThrough, proxyFailure } from "@/lib/server-api";

type RouteContext = { params: Promise<{ segments: string[] }> };

export async function GET(request: Request, context: RouteContext) {
  const { segments } = await context.params;
  const safePath = segments.map(encodeURIComponent).join("/");
  const query = new URL(request.url).searchParams.toString();
  const range = request.headers.get("range");
  try {
    return passThrough(await backendFetch(
      `/api/viewer/${safePath}${query ? `?${query}` : ""}`,
      { headers: range ? { Range: range } : undefined },
    ));
  } catch (error) {
    return proxyFailure(error);
  }
}
