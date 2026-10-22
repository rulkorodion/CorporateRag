import { NextRequest, NextResponse } from "next/server";

import { adminFromRequest, requestIsSameOrigin } from "@/lib/admin-auth";
import { listJobs, maxUploadBytes, queueUpload } from "@/lib/admin-storage";

export const runtime = "nodejs";

export async function GET(request: NextRequest) {
  if (!adminFromRequest(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json(await listJobs());
}

export async function POST(request: NextRequest) {
  const admin = adminFromRequest(request);
  if (!admin) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!requestIsSameOrigin(request)) {
    return NextResponse.json({ error: "invalid_origin", message: "Недопустимый источник запроса." }, { status: 403 });
  }
  const contentLength = Number(request.headers.get("content-length") ?? 0);
  if (contentLength > maxUploadBytes() + 1024 * 1024) {
    return NextResponse.json({ error: "too_large", message: "Файл превышает допустимый размер." }, { status: 413 });
  }
  try {
    const form = await request.formData();
    const file = form.get("file");
    const category = form.get("category");
    const replace = form.get("replace") === "true";
    if (!(file instanceof File) || typeof category !== "string") {
      return NextResponse.json({ error: "invalid_upload", message: "Выберите файл и раздел." }, { status: 422 });
    }
    const job = await queueUpload({ file, category, replace, username: admin.username });
    return NextResponse.json({ job }, { status: 201 });
  } catch (error) {
    const code = (error as Error & { code?: string }).code;
    const status = code === "FILE_EXISTS" || code === "ALREADY_QUEUED" ? 409 : 422;
    return NextResponse.json(
      {
        error: code === "FILE_EXISTS" ? "file_exists" : code === "ALREADY_QUEUED" ? "already_queued" : "invalid_upload",
        message: error instanceof Error ? error.message : "Не удалось принять файл.",
      },
      { status },
    );
  }
}

