import { NextRequest, NextResponse } from "next/server";

import { adminFromRequest } from "@/lib/admin-auth";
import { listCategories } from "@/lib/admin-storage";

export async function GET(request: NextRequest) {
  if (!adminFromRequest(request)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  return NextResponse.json({ categories: await listCategories() });
}

