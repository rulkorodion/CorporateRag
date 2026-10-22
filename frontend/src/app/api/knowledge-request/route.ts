import { NextRequest, NextResponse } from "next/server";
import nodemailer from "nodemailer";

import { requestIsSameOrigin } from "@/lib/admin-auth";

export const runtime = "nodejs";

const attempts = new Map<string, { count: number; resetAt: number }>();
const WINDOW_MS = 60 * 60 * 1000;
const MAX_REQUESTS = 5;
const PRIMARY_RECIPIENT = "rodioshax@yandex.ru";

function clientKey(request: NextRequest): string {
  return (
    request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ||
    request.headers.get("x-real-ip") ||
    "unknown"
  );
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function recipients(): string[] {
  const configuredPrimary = process.env.KNOWLEDGE_REQUEST_RECIPIENT?.trim() || PRIMARY_RECIPIENT;
  const director = process.env.KNOWLEDGE_REQUEST_DIRECTOR_RECIPIENT?.trim();
  return [...new Set([configuredPrimary, director].filter((value): value is string => Boolean(value)))];
}

export async function POST(request: NextRequest) {
  if (!requestIsSameOrigin(request)) {
    return NextResponse.json(
      { error: "invalid_origin", message: "Недопустимый источник запроса." },
      { status: 403 },
    );
  }

  const now = Date.now();
  const key = clientKey(request);
  const current = attempts.get(key);
  if (current && current.resetAt > now && current.count >= MAX_REQUESTS) {
    return NextResponse.json(
      { error: "rate_limited", message: "Заявка уже отправлялась. Попробуйте позднее." },
      { status: 429 },
    );
  }

  const body = (await request.json().catch(() => null)) as {
    question?: unknown;
    answer?: unknown;
    requestId?: unknown;
  } | null;
  const question = typeof body?.question === "string" ? body.question.trim() : "";
  const answer = typeof body?.answer === "string" ? body.answer.trim() : "";
  const requestId = typeof body?.requestId === "string" ? body.requestId.trim() : "";
  if (!question || question.length > 4_000 || answer.length > 12_000 || requestId.length > 128) {
    return NextResponse.json(
      { error: "invalid_request", message: "Не удалось подготовить заявку." },
      { status: 422 },
    );
  }

  const host = process.env.SMTP_HOST?.trim();
  const user = process.env.SMTP_USER?.trim();
  const password = process.env.SMTP_PASSWORD;
  const port = Number(process.env.SMTP_PORT ?? 465);
  const secure = (process.env.SMTP_SECURE ?? "true").trim().toLowerCase() === "true";
  if (!host || !user || !password || !Number.isInteger(port) || port < 1 || port > 65_535) {
    return NextResponse.json(
      { error: "mail_not_configured", message: "Отправка заявок пока не настроена." },
      { status: 503 },
    );
  }

  const transporter = nodemailer.createTransport({
    host,
    port,
    secure,
    auth: { user, pass: password },
    connectionTimeout: 10_000,
    greetingTimeout: 10_000,
    socketTimeout: 20_000,
  });
  const escapedQuestion = escapeHtml(question);
  const escapedAnswer = escapeHtml(answer || "Модель не сформировала подтверждённый ответ.");
  const escapedRequestId = escapeHtml(requestId || "не указан");

  try {
    await transporter.sendMail({
      from: process.env.SMTP_FROM?.trim() || `База знаний <${user}>`,
      to: recipients(),
      subject: "Заявка на добавление документа в базу знаний",
      text: [
        "Пользователь не нашёл подтверждённую информацию в корпоративной базе знаний.",
        "",
        `Вопрос: ${question}`,
        "",
        `Ответ системы: ${answer || "Модель не сформировала подтверждённый ответ."}`,
        "",
        `Request ID: ${requestId || "не указан"}`,
        `Дата: ${new Date().toISOString()}`,
      ].join("\n"),
      html: `
        <h2>Заявка на добавление документа</h2>
        <p>Пользователь не нашёл подтверждённую информацию в корпоративной базе знаний.</p>
        <p><strong>Вопрос:</strong><br>${escapedQuestion}</p>
        <p><strong>Ответ системы:</strong><br>${escapedAnswer}</p>
        <p style="color:#718691;font-size:12px">Request ID: ${escapedRequestId}<br>Дата: ${new Date().toISOString()}</p>
      `,
      disableFileAccess: true,
      disableUrlAccess: true,
    });
  } catch {
    return NextResponse.json(
      { error: "mail_failed", message: "Не удалось отправить заявку. Попробуйте ещё раз позднее." },
      { status: 502 },
    );
  }

  attempts.set(key, {
    count: current && current.resetAt > now ? current.count + 1 : 1,
    resetAt: current && current.resetAt > now ? current.resetAt : now + WINDOW_MS,
  });
  return NextResponse.json({ ok: true, message: "Заявка отправлена." });
}
