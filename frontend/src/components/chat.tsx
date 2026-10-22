"use client";

import { RotateCcw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { AnswerMarkdown } from "@/components/answer-markdown";
import { ChatComposer } from "@/components/chat-composer";
import { DirectImages } from "@/components/direct-images";
import { DocumentViewer, type DocumentViewerTarget } from "@/components/document-viewer";
import { KnowledgeRequestCard } from "@/components/knowledge-request-card";
import { LoadingAnswer } from "@/components/loading-answer";
import { SourceList } from "@/components/source-list";
import { Button } from "@/components/ui/button";
import {
  askQuestionStream,
  streamStatusText,
  type AnswerResponse,
} from "@/lib/api";
import { createClientId } from "@/lib/client-id";
import { attachmentFor, citedSheet } from "@/lib/format";

type Exchange = {
  id: string;
  question: string;
  status: "loading" | "streaming" | "complete" | "error";
  response?: AnswerResponse;
  partialAnswer?: string;
  progress?: string;
  error?: string;
};

export function Chat() {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [viewerTarget, setViewerTarget] = useState<DocumentViewerTarget | null>(null);
  const answerRefs = useRef(new Map<string, HTMLDivElement>());
  const controllers = useRef(new Map<string, AbortController>());
  const lastScrolledId = useRef<string | null>(null);

  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
  }, []);

  useEffect(() => {
    const latest = exchanges.at(-1);
    if (!latest || lastScrolledId.current === latest.id) return;
    lastScrolledId.current = latest.id;
    const frame = requestAnimationFrame(() => {
      const target = answerRefs.current.get(latest.id);
      if (!target) return;
      const desired = window.scrollY + target.getBoundingClientRect().top - 24;
      const maximum = Math.max(
        0,
        document.documentElement.scrollHeight - window.innerHeight,
      );
      window.scrollTo({ top: Math.min(desired, maximum), behavior: "smooth" });
    });
    return () => cancelAnimationFrame(frame);
  }, [exchanges.length, exchanges]);

  const submit = useCallback(async (question: string) => {
    const text = question.trim();
    if (!text || controllers.current.size > 0) return;
    const requestText = text;
    const id = createClientId();
    const controller = new AbortController();
    controllers.current.set(id, controller);
    setExchanges((current) => [...current, {
      id,
      question: text,
      status: "loading",
      progress: "Запрос принят",
    }]);
    let streamed = "";
    let frame: number | null = null;
    const flush = () => {
      frame = null;
      setExchanges((current) => current.map((item) => (
        item.id === id
          ? { ...item, status: "streaming", partialAnswer: streamed }
          : item
      )));
    };
    try {
      const response = await askQuestionStream(requestText, {
        signal: controller.signal,
        onStatus: (event) => {
          const progress = streamStatusText(event);
          setExchanges((current) => current.map((item) => (
            item.id === id ? { ...item, progress } : item
          )));
        },
        onDelta: (delta) => {
          streamed += delta;
          if (frame === null) frame = requestAnimationFrame(flush);
        },
      });
      if (frame !== null) cancelAnimationFrame(frame);
      setExchanges((current) => current.map((item) => (item.id === id ? { ...item, status: "complete", response } : item)));
    } catch (error) {
      if (frame !== null) cancelAnimationFrame(frame);
      const message = error instanceof Error ? error.message : "Не удалось получить ответ.";
      setExchanges((current) => current.map((item) => (item.id === id ? { ...item, status: "error", error: message } : item)));
    } finally {
      controllers.current.delete(id);
    }
  }, []);

  const loadSchedulePage = useCallback(async (
    exchangeId: string,
    response: AnswerResponse,
    page: number,
  ) => {
    const pagination = response.schedule_pagination;
    if (!pagination || controllers.current.size > 0) return;
    if (page < 1 || page > pagination.total_pages || page === pagination.page) return;

    const controller = new AbortController();
    controllers.current.set(exchangeId, controller);
    setExchanges((current) => current.map((item) => (
      item.id === exchangeId
        ? {
            ...item,
            status: "loading",
            progress: `Открываю страницу ${page} из ${pagination.total_pages}`,
            partialAnswer: "",
          }
        : item
    )));
    let streamed = "";
    let frame: number | null = null;
    const flush = () => {
      frame = null;
      setExchanges((current) => current.map((item) => (
        item.id === exchangeId
          ? { ...item, status: "streaming", partialAnswer: streamed }
          : item
      )));
    };
    try {
      const nextResponse = await askQuestionStream(
        pagination.selection_question,
        {
          signal: controller.signal,
          onStatus: (event) => {
            const progress = streamStatusText(event);
            setExchanges((current) => current.map((item) => (
              item.id === exchangeId ? { ...item, progress } : item
            )));
          },
          onDelta: (delta) => {
            streamed += delta;
            if (frame === null) frame = requestAnimationFrame(flush);
          },
        },
        {
          page,
          snapshotVersion: pagination.snapshot_version,
          selection: pagination.selection,
        },
      );
      if (frame !== null) cancelAnimationFrame(frame);
      setExchanges((current) => current.map((item) => (
        item.id === exchangeId
          ? {
              ...item,
              status: "complete",
              response: nextResponse,
              partialAnswer: "",
              error: undefined,
            }
          : item
      )));
    } catch (error) {
      if (frame !== null) cancelAnimationFrame(frame);
      const message = error instanceof Error ? error.message : "Не удалось открыть страницу.";
      setExchanges((current) => current.map((item) => (
        item.id === exchangeId
          ? { ...item, status: "complete", response, error: message, partialAnswer: "" }
          : item
      )));
    } finally {
      controllers.current.delete(exchangeId);
    }
  }, []);

  const cancel = useCallback((id: string) => {
    controllers.current.get(id)?.abort();
    setExchanges((current) => current.map((item) => (
      item.id === id ? { ...item, progress: "Отменяю запрос" } : item
    )));
  }, []);

  const answerInProgress = exchanges.some(
    (exchange) => exchange.status === "loading" || exchange.status === "streaming",
  );

  const openSource = useCallback((response: AnswerResponse, sourceNumber: number) => {
    const source = response.sources.find((item) => item.source_number === sourceNumber);
    if (!source) return;
    const attachment = attachmentFor(source, response.source_attachments ?? []);
    if (attachment.external_url) {
      window.open(attachment.external_url, "_blank", "noopener,noreferrer");
      return;
    }
    setViewerTarget({
      type: "document",
      documentId: attachment.document_id ?? source.document_id,
      versionId: attachment.version_id ?? source.version_id,
      title: attachment.display_relative_path,
      citedPage: source.page_start ?? undefined,
      citedSheet: citedSheet(source),
    });
  }, []);

  return (
    <>
      <main className="mx-auto flex w-full max-w-[1020px] flex-1 flex-col px-5 pb-[84px] pt-8 sm:px-8 sm:pb-[92px] sm:pt-10">
        {!exchanges.length && (
          <div className="grid min-h-[47vh] place-items-center text-center">
            <div className="max-w-md">
              <h2 className="text-[20px] font-semibold text-[#17366e]">Что вы хотите узнать?</h2>
              <p className="mt-2 text-[14px] leading-6 text-[#788bad]">Задайте вопрос по внутренним документам компании — ответ будет содержать ссылки на использованные источники.</p>
            </div>
          </div>
        )}

        <div className="space-y-8 sm:space-y-10">
          {exchanges.map((exchange) => (
            <article key={exchange.id} className="space-y-6">
              <div className="ml-auto w-fit max-w-[78%] rounded-[25px] border border-[#d8e3e8] bg-[#edf3f6] px-6 py-4 text-[16px] leading-7 text-[#294c64] shadow-[0_5px_16px_rgba(55,86,108,.04)] sm:max-w-[62%]">
                {exchange.question}
              </div>
              <div
                ref={(node) => {
                  if (node) answerRefs.current.set(exchange.id, node);
                  else answerRefs.current.delete(exchange.id);
                }}
                className="relative scroll-mt-5"
              >
                {exchange.status === "loading" && (
                  <LoadingAnswer
                    status={exchange.progress}
                    onCancel={() => cancel(exchange.id)}
                  />
                )}
                {exchange.status === "streaming" && (
                  <div className="answer-card">
                    <AnswerMarkdown answer={exchange.partialAnswer ?? ""} idPrefix={`${exchange.id}-preview`} />
                    <div className="mt-5 flex items-center justify-between gap-4 border-t border-[#e5ebee] pt-4">
                      <p className="text-[11px] leading-5 text-[#8295a0]">
                        {exchange.progress ?? "Формирую и проверяю ответ"}
                      </p>
                      <Button type="button" variant="ghost" size="sm" className="shrink-0 rounded-full" onClick={() => cancel(exchange.id)}>
                        Отменить
                      </Button>
                    </div>
                  </div>
                )}
                {exchange.status === "error" && (
                  <div className="answer-card">
                    <p className="text-[15px] leading-7 text-[#6a3c45]">{exchange.error}</p>
                    <Button variant="ghost" size="sm" className="mt-4 -ml-3" onClick={() => submit(exchange.question)}>
                      <RotateCcw className="size-4" /> Повторить
                    </Button>
                  </div>
                )}
                {exchange.status === "complete" && exchange.response && (
                  <div className="answer-card">
                    <AnswerMarkdown answer={exchange.response.answer} idPrefix={exchange.id} onCitationClick={(number) => openSource(exchange.response!, number)} />
                    {exchange.response.schedule_pagination && exchange.response.schedule_pagination.total_pages > 1 && (
                      <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-[#e5ebee] pt-4">
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={!exchange.response.schedule_pagination.has_previous || answerInProgress}
                          onClick={() => loadSchedulePage(
                            exchange.id,
                            exchange.response!,
                            exchange.response!.schedule_pagination!.page - 1,
                          )}
                        >
                          Назад
                        </Button>
                        <span className="text-xs text-[#718795]">
                          Страница {exchange.response.schedule_pagination.page} из {exchange.response.schedule_pagination.total_pages}
                        </span>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          disabled={!exchange.response.schedule_pagination.has_next || answerInProgress}
                          onClick={() => loadSchedulePage(
                            exchange.id,
                            exchange.response!,
                            exchange.response!.schedule_pagination!.page + 1,
                          )}
                        >
                          Далее
                        </Button>
                      </div>
                    )}
                    {exchange.error && (
                      <p className="mt-3 text-xs text-[#8b4b55]">{exchange.error}</p>
                    )}
                    <DirectImages images={exchange.response.related_images} sources={exchange.response.sources} onOpen={setViewerTarget} />
                    <SourceList sources={exchange.response.sources} attachments={exchange.response.source_attachments ?? []} idPrefix={exchange.id} onOpen={setViewerTarget} />
                    {exchange.response.answer_kind !== "rolling_schedule" && exchange.response.warnings.length > 0 && exchange.response.sources.length === 0 && (
                      <p className="mt-5 text-xs text-[#8292ad]">В ответе не использованы подтверждённые источники.</p>
                    )}
                    {exchange.response.answer_kind !== "rolling_schedule" && exchange.response.sources.length === 0 && (
                      <KnowledgeRequestCard
                        question={exchange.question}
                        answer={exchange.response.answer}
                        requestId={exchange.response.request_id}
                      />
                    )}
                  </div>
                )}
              </div>
            </article>
          ))}
        </div>

        <p className="mt-auto pt-8 text-center text-[11px] leading-4 text-[#8497a4] sm:text-xs">
          База знаний может ошибаться. Для точных данных проверяйте прикреплённые источники.
        </p>
      </main>

      <ChatComposer onSubmit={submit} disabled={answerInProgress} />
      <DocumentViewer
        key={viewerTarget ? `${viewerTarget.type}:${viewerTarget.documentId}:${viewerTarget.versionId}:${viewerTarget.type === "asset" ? viewerTarget.assetId : viewerTarget.citedPage ?? viewerTarget.citedSheet ?? "start"}` : "closed"}
        target={viewerTarget}
        onClose={() => setViewerTarget(null)}
      />
    </>
  );
}
