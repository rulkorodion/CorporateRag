import { afterEach, describe, expect, it, vi } from "vitest";

import {
  askQuestionStream,
  friendlyError,
  hasScheduleResultTable,
  isExplicitFullScheduleRequest,
  resolveSchedulePageQuestion,
  streamStatusText,
  withoutSchedulePage,
} from "@/lib/api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("friendly API errors", () => {
  it("explains an overloaded queue", () => {
    expect(friendlyError(429)).toContain("слишком много запросов");
  });

  it("explains temporary backend unavailability", () => {
    expect(friendlyError(503)).toContain("временно");
  });

  it("does not expose technical errors", () => {
    expect(friendlyError()).not.toMatch(/traceback|exception/i);
  });
});

describe("streaming answers", () => {
  it("shows a dedicated status while checking rolling schedules", () => {
    expect(streamStatusText({
      type: "status",
      status: "checking",
      stage: "rolling_schedule",
    })).toBe("Проверяю актуальный график прокатов");
  });

  it("shows question-understanding status before schedule search", () => {
    expect(streamStatusText({
      type: "status",
      status: "understanding",
      stage: "rolling_schedule",
    })).toBe("Понимаю вопрос о плане производства");
  });

  it("delivers queue status and deltas, then returns the validated final payload", async () => {
    const final = {
      request_id: "request-1",
      answer: "Проверенный ответ [1].",
      status: "completed",
      citation_validation: "valid",
      citations: [],
      sources: [],
      source_attachments: [],
      related_images: [],
      warnings: [],
      latency: { ttft_seconds: 0.4 },
      telemetry: { prompt_eval_count: 120 },
      model: "fake",
      context_id: "context-1",
    };
    const events = [
      { type: "status", status: "queued", stage: "llm", queue_position: 3 },
      { type: "status", status: "queued", stage: "llm", queue_position: 2 },
      { type: "status", status: "queued", stage: "llm", queue_position: 1 },
      { type: "delta", delta: "Черновой " },
      { type: "delta", delta: "ответ" },
      { type: "final", data: final },
    ];
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      `${events.map((event) => JSON.stringify(event)).join("\n")}\n`,
      { status: 200, headers: { "content-type": "application/x-ndjson" } },
    )));
    const statuses: string[] = [];
    const deltas: string[] = [];

    const response = await askQuestionStream("Вопрос", {
      onStatus: (event) => statuses.push(streamStatusText(event)),
      onDelta: (delta) => deltas.push(delta),
    });

    expect(statuses).toEqual([
      "Вопрос в очереди — 3",
      "Вопрос в очереди — 2",
      "Вопрос в очереди — 1",
    ]);
    expect(deltas.join("")).toBe("Черновой ответ");
    expect(response).toEqual(final);
  });

  it("turns a client abort into a safe cancellation message", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener(
          "abort",
          () => reject(new DOMException("aborted", "AbortError")),
          { once: true },
        );
      })
    )));
    const controller = new AbortController();
    const pending = askQuestionStream("Вопрос", { signal: controller.signal });
    controller.abort();

    await expect(pending).rejects.toMatchObject({
      status: 499,
      message: "Запрос отменён.",
    });
  });

  it("does not accept a stream that ends without a validated final event", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(
      `${JSON.stringify({ type: "delta", delta: "непроверенный текст" })}\n`,
      { status: 200 },
    )));

    await expect(askQuestionStream("Вопрос")).rejects.toThrow(
      "Поток ответа завершился раньше времени.",
    );
  });
});

describe("rolling schedule pagination", () => {
  it("sends an explicit page and snapshot version without chat history", async () => {
    const final = {
      request_id: "request-page",
      answer_kind: "rolling_schedule",
      answer: "Страница 2",
      status: "completed",
      citation_validation: "valid",
      citations: [],
      sources: [],
      source_attachments: [],
      related_images: [],
      warnings: [],
      latency: {},
      telemetry: {},
      model: "structured",
      context_id: "rolling-schedule:abc",
    };
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      void _url;
      void init;
      return new Response(
        `${JSON.stringify({ type: "final", data: final })}\n`,
        { status: 200 },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    await askQuestionStream(
      "График прокатов Ситко",
      {},
      {
        page: 2,
        snapshotVersion: "a".repeat(64),
        selection: {
          sheet_names: ["Ситко"],
          record_kinds: [],
          product_kinds: [],
          dimensions: [],
          steel: null,
          date_from: "2026-10-15",
          date_to: null,
          date_explicit: false,
          period_mode: "default_future",
          include_undated: true,
          products_only: true,
          query_type: "stand_schedule",
          response_mode: "full",
        },
      },
    );

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toEqual({
      question: "График прокатов Ситко",
      schedule_page: 2,
      schedule_snapshot_version: "a".repeat(64),
      schedule_selection: {
        sheet_names: ["Ситко"],
        record_kinds: [],
        product_kinds: [],
        dimensions: [],
        steel: null,
        date_from: "2026-10-15",
        date_to: null,
        date_explicit: false,
        period_mode: "default_future",
        include_undated: true,
        products_only: true,
        query_type: "stand_schedule",
        response_mode: "full",
      },
    });
  });

  it("reuses the previous full selection for a page-only follow-up", () => {
    const previous = "Покажи весь график стана 159 с 15 по 20 октября, страница 1";
    expect(isExplicitFullScheduleRequest(previous)).toBe(true);
    expect(withoutSchedulePage(previous)).not.toContain("страница 1");
    expect(resolveSchedulePageQuestion("страница 2", previous)).toBe(
      "Покажи весь график стана 159 с 15 по 20 октября, страница 2",
    );
  });

  it("does not invent a selection when no full query exists", () => {
    expect(resolveSchedulePageQuestion("страница 2", null)).toBe("страница 2");
  });

  it("recognizes a backend schedule table without requiring the word full", () => {
    expect(hasScheduleResultTable(
      "Вступление\n\n| Стан / площадка | Дата | Номенклатура |",
    )).toBe(true);
    expect(hasScheduleResultTable("Краткая сводка без таблицы")).toBe(false);
  });
});
