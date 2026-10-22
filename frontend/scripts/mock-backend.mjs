import { createReadStream, existsSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const imageRelativePath = "Доставки/образец доверенности на получение ТМЦ.png";
const imagePath = resolve(root, "База Знаний", ...imageRelativePath.split("/"));

function source(number, path, extension, page = null) {
  return {
    citation: `[${number}]`,
    source_number: number,
    chunk_id: `chunk-${number}`,
    document_id: `doc-${number}`,
    version_id: `version-${number}`,
    relative_path: path,
    page_start: page,
    page_end: page,
    heading_path: ["Корпоративные инструкции"],
    section_title: extension === "XLSX" ? "Стандартные сроки" : "Порядок действий",
    source_type: number === 1 ? "ocr" : "native",
    provenance: {},
    source_element_ids: [],
    related_image_ids: [],
  };
}

function attachment(item) {
  return {
    source_number: item.source_number,
    display_relative_path: item.relative_path,
    download_relative_path: item.relative_path,
    media_type: "document",
    indexed_relative_path: item.relative_path,
    indexed_document_id: item.document_id,
  };
}

function answer(question) {
  const visual = /покажи|изображ|фото|доверенност/i.test(question);
  const insufficient = /пароль|нет ответа/i.test(question);
  if (insufficient) {
    return {
      request_id: crypto.randomUUID(),
      answer: "В предоставленных источниках нет информации, достаточной для ответа на этот вопрос.",
      status: "citation_warning",
      citation_validation: "warning",
      citations: [], sources: [], source_attachments: [], related_images: [],
      warnings: ["no_citations_returned_for_nonempty_context"],
      latency: { total_seconds: 1.2 }, model: "mock", context_id: "context-empty",
    };
  }
  if (visual) {
    const item = source(1, imageRelativePath, "PNG");
    item.provenance = { source_fallback: "standalone_visual_filename" };
    item.related_image_ids = ["image-1"];
    return {
      request_id: crypto.randomUUID(),
      answer: "Найден запрошенный визуальный материал — образец доверенности на получение ТМЦ [1].",
      status: "completed", citation_validation: "valid",
      citations: [item], sources: [item], source_attachments: [attachment(item)],
      related_images: [{ asset_id: "image-1", document_id: "doc-1", version_id: "version-1", local_path: imageRelativePath, source_page: null, visual_role: "standalone_visual", visual_classification_status: "completed", path_scope: "knowledge_base" }],
      warnings: [], latency: { total_seconds: 2.1 }, model: "mock", context_id: "context-visual",
    };
  }
  const items = [
    source(1, "Регламенты/Регламент заказа полиграфической продукции.pdf", "PDF", 6),
    source(2, "О компании/Брендбук КТЗ. Правила использования фирменного стиля.docx", "DOCX"),
    source(3, "Маркетинг/Таблица стандартных сроков изготовления продукции.xlsx", "XLSX", 1),
  ];
  return {
    request_id: crypto.randomUUID(),
    answer: "Чтобы заказать визитку, оформите заявку через внутренний процесс **«Заказ полиграфической продукции»** в DocumentFlow [1].\n\nВ заявке укажите ФИО, должность, контактные данные и необходимое количество визиток. Макет должен соответствовать брендбуку КТЗ и согласовывается с отделом маркетинга перед печатью [2].\n\nСтандартный срок изготовления — **3 рабочих дня** после утверждения макета [3].",
    status: "completed", citation_validation: "valid",
    citations: items, sources: items, source_attachments: items.map(attachment), related_images: [],
    warnings: [], latency: { total_seconds: 3.4 }, model: "mock", context_id: "context-main",
  };
}

createServer((request, response) => {
  const url = new URL(request.url ?? "/", "http://127.0.0.1:18010");
  if (url.pathname === "/health") return json(response, 200, { status: "ok", metrics: {}, stages: {} });
  if (url.pathname === "/ready") return json(response, 200, { ready: true });
  if (url.pathname === "/api/files/asset" && existsSync(imagePath)) {
    response.writeHead(200, { "Content-Type": "image/png" });
    return createReadStream(imagePath).pipe(response);
  }
  if (url.pathname === "/api/files/source") return json(response, 200, { opened: url.searchParams.get("path") });
  if (url.pathname === "/api/answer" && request.method === "POST") {
    let body = "";
    request.on("data", (chunk) => (body += chunk));
    request.on("end", () => {
      const question = JSON.parse(body).question ?? "";
      if (/429/.test(question)) return json(response, 429, { error: "queue_full" });
      if (/503/.test(question)) return json(response, 503, { error: "queue_timeout" });
      setTimeout(() => json(response, 200, answer(question)), 450);
    });
    return;
  }
  json(response, 404, { error: "not_found" });
}).listen(18010, "127.0.0.1", () => console.log("Mock RAG API: http://127.0.0.1:18010"));

function json(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(payload));
}
