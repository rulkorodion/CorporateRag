export type Citation = {
  citation: string;
  source_number: number;
  chunk_id: string;
  document_id: string;
  relative_path: string;
  page_start: number | null;
  page_end: number | null;
  section_title: string | null;
};

export type Source = Citation & {
  version_id: string;
  heading_path: string[];
  source_type: "native" | "ocr" | "mixed";
  provenance: Record<string, unknown>;
  source_element_ids: string[];
  related_image_ids: string[];
};

export type SourceAttachment = {
  source_number: number;
  display_relative_path: string;
  download_relative_path: string;
  media_type: "document" | "video";
  indexed_relative_path: string;
  indexed_document_id: string;
  document_id?: string | null;
  version_id?: string | null;
  external_url?: string | null;
};

export type ViewerMetadata = {
  document_id: string;
  version_id: string;
  file_name: string;
  file_type: string;
  media_type: string;
  viewer_kind: "pdf" | "office" | "spreadsheet" | "image" | "video";
  sha256?: string;
  page_count?: number | null;
};

export type RelatedImage = {
  asset_id: string;
  document_id: string;
  version_id: string;
  local_path: string;
  source_page: number | null;
  visual_role: string;
  visual_classification_status: string;
  path_scope: "storage" | "knowledge_base";
};

export type AnswerResponse = {
  request_id: string;
  answer_kind?: "rag" | "rolling_schedule";
  answer: string;
  status: string;
  citation_validation: string;
  citations: Citation[];
  sources: Source[];
  source_attachments: SourceAttachment[];
  related_images: RelatedImage[];
  warnings: string[];
  latency: Record<string, number>;
  telemetry: Record<string, unknown>;
  model: string;
  context_id: string;
  schedule_pagination?: SchedulePagination | null;
};

export type SchedulePagination = {
  selection_question: string;
  selection: ScheduleSelection;
  snapshot_version: string;
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
  has_previous: boolean;
  has_next: boolean;
  snapshot_changed: boolean;
};

export type ScheduleSelection = {
  sheet_names: string[];
  record_kinds: Array<"product" | "service">;
  product_kinds: Array<"round_tube" | "profile" | "sheet">;
  dimensions: string[];
  steel: string | null;
  date_from: string | null;
  date_to: string | null;
  date_explicit: boolean;
  period_mode: "default_future" | "explicit" | "open_from" | "open_after" | "open_until" | "all_available";
  include_undated: boolean;
  products_only: boolean;
  query_type: "product_search" | "production_plan" | "stand_schedule" | "count" | "service_events" | "summary" | "date_bounds" | "date_list";
  response_mode: "full";
};

export type SchedulePageRequest = {
  page: number;
  snapshotVersion: string;
  selection: ScheduleSelection;
};

export type AnswerStreamStatus = {
  type: "status";
  status: string;
  stage?: string;
  request_id?: string;
  queue_position?: number;
  queue_wait_seconds?: number;
};

export type AnswerStreamCallbacks = {
  signal?: AbortSignal;
  onStatus?: (event: AnswerStreamStatus) => void;
  onDelta?: (delta: string) => void;
};

export class ChatAPIError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
  }
}

export function friendlyError(status?: number, code?: string): string {
  if (status === 429 || code === "queue_full") {
    return "Сейчас слишком много запросов. Подождите немного и попробуйте ещё раз.";
  }
  if (status === 503 || code === "queue_timeout") {
    return "База знаний временно занята или недоступна. Попробуйте повторить вопрос через минуту.";
  }
  if (status === 504 || code === "llm_timeout") {
    return "Ответ занял слишком много времени. Попробуйте повторить вопрос.";
  }
  if (status === 499 || code === "request_cancelled") {
    return "Запрос отменён.";
  }
  return "Не удалось связаться с базой знаний. Проверьте соединение и попробуйте ещё раз.";
}

export function streamStatusText(event: AnswerStreamStatus): string {
  if (event.status === "queued") {
    const position = event.queue_position;
    return position
      ? `Вопрос в очереди — ${position}`
      : "Вопрос в очереди";
  }
  if (event.stage === "rolling_schedule" && event.status === "understanding") {
    return "Понимаю вопрос о плане производства";
  }
  if (event.stage === "rolling_schedule") return "Проверяю актуальный график прокатов";
  if (event.stage === "embedding") return "Ищу подходящие документы";
  if (event.stage === "reranker") return "Выбираю самые точные источники";
  if (event.stage === "context") return "Готовлю подтверждённый контекст";
  if (event.stage === "llm" && event.status === "generating") return "Формирую ответ";
  if (event.stage === "validation") return "Проверяю ссылки на источники";
  return "Запрос принят";
}

const PAGE_ONLY_RE = /^(?:покажи\s+)?(?:страница|стр\.)\s*(\d{1,3})[.!?]?$/i;
const PAGE_SUFFIX_RE = /\s*[,;]?\s*(?:страница|стр\.)\s*\d{1,3}[.!?]?\s*$/i;

export function isExplicitFullScheduleRequest(question: string): boolean {
  const normalized = question.trim().toLocaleLowerCase("ru");
  const asksToList = /(покажи|выведи|перечисли|дай)/.test(normalized);
  const requestsAll = /(весь|всю|все|полн|целиком)/.test(normalized);
  const hasScheduleSubject = /(график|план|позици|список)/.test(normalized);
  return asksToList && requestsAll && hasScheduleSubject;
}

export function hasScheduleResultTable(answer: string): boolean {
  return answer.includes("| Стан / площадка | Дата | Номенклатура |");
}

export function withoutSchedulePage(question: string): string {
  return question.replace(PAGE_SUFFIX_RE, "").trim();
}

export function resolveSchedulePageQuestion(question: string, previousFullQuery: string | null): string {
  const normalized = question.trim();
  const page = normalized.match(PAGE_ONLY_RE)?.[1];
  if (!page || !previousFullQuery) return normalized;
  return `${withoutSchedulePage(previousFullQuery)}, страница ${page}`;
}

export async function askQuestionStream(
  question: string,
  callbacks: AnswerStreamCallbacks = {},
  schedulePage?: SchedulePageRequest,
): Promise<AnswerResponse> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 440_000);
  const abort = () => controller.abort();
  callbacks.signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await fetch("/api/answer/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/x-ndjson",
      },
      body: JSON.stringify({
        question,
        ...(schedulePage
          ? {
              schedule_page: schedulePage.page,
              schedule_snapshot_version: schedulePage.snapshotVersion,
              schedule_selection: schedulePage.selection,
            }
          : {}),
      }),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      const body = (await response.json().catch(() => null)) as { error?: string } | null;
      throw new ChatAPIError(
        friendlyError(response.status, body?.error),
        response.status,
      );
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let final: AnswerResponse | null = null;

    const consume = (line: string) => {
      if (!line.trim()) return;
      const event = JSON.parse(line) as
        | AnswerStreamStatus
        | { type: "delta"; delta: string }
        | { type: "final"; data: AnswerResponse }
        | { type: "error"; error?: string; status_code?: number };
      if (event.type === "status") callbacks.onStatus?.(event);
      else if (event.type === "delta") callbacks.onDelta?.(event.delta);
      else if (event.type === "final") final = event.data;
      else if (event.type === "error") {
        throw new ChatAPIError(
          friendlyError(event.status_code, event.error),
          event.status_code,
        );
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) consume(line);
      if (done) break;
    }
    if (buffer.trim()) consume(buffer);
    if (!final) throw new ChatAPIError("Поток ответа завершился раньше времени.");
    return final;
  } catch (error) {
    if (error instanceof ChatAPIError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      if (callbacks.signal?.aborted) throw new ChatAPIError(friendlyError(499), 499);
      throw new ChatAPIError(friendlyError(504), 504);
    }
    throw new ChatAPIError(friendlyError());
  } finally {
    callbacks.signal?.removeEventListener("abort", abort);
    globalThis.clearTimeout(timeout);
  }
}

export async function askQuestion(question: string): Promise<AnswerResponse> {
  const controller = new AbortController();
  const timeout = globalThis.setTimeout(() => controller.abort(), 185_000);
  try {
    const response = await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
      signal: controller.signal,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ChatAPIError(friendlyError(response.status, data.error), response.status);
    }
    return data as AnswerResponse;
  } catch (error) {
    if (error instanceof ChatAPIError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ChatAPIError(friendlyError(504), 504);
    }
    throw new ChatAPIError(friendlyError());
  } finally {
    globalThis.clearTimeout(timeout);
  }
}
