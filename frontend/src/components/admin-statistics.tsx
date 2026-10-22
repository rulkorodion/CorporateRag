"use client";

import { LoaderCircle, RefreshCw, TriangleAlert } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import type { AnalyticsSummary } from "@/lib/admin-analytics";
import { formatCount, formatDuration, formatPercent } from "@/lib/admin-analytics";

const MOSCOW_TIME_ZONE = "Europe/Moscow";

const periods = [
  { days: 1, label: "24 часа" },
  { days: 7, label: "7 дней" },
  { days: 30, label: "30 дней" },
] as const;

const errorLabels: Record<string, string> = {
  queue_full: "Очередь заполнена",
  queue_timeout: "Истекло время ожидания в очереди",
  llm_timeout: "Модель не успела сформировать ответ",
  ollama_unavailable: "Модель временно недоступна",
  llm_error: "Ошибка при формировании ответа",
  reranker_unavailable: "Ошибка проверки найденных фрагментов",
  invalid_request: "Некорректный запрос",
  pipeline_unavailable: "Компоненты базы знаний временно недоступны",
  request_cancelled: "Запрос отменён пользователем",
};

const stageLabels: Record<string, string> = {
  embedding: "Подготовка запроса",
  retrieval: "Поиск по базе",
  reranker: "Проверка найденных фрагментов",
  context_builder: "Подготовка источников",
  llm: "Формирование ответа",
  pipeline: "Обработка запроса",
};

function formatDateTime(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: MOSCOW_TIME_ZONE,
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function formatAxisDate(value: string, hourly: boolean): string {
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: MOSCOW_TIME_ZONE,
    ...(hourly
      ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" }
      : { day: "2-digit", month: "2-digit" }),
  }).format(new Date(value));
}

function formatTooltipDate(value: string, hourly: boolean): string {
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone: MOSCOW_TIME_ZONE,
    day: "2-digit",
    month: "2-digit",
    ...(hourly ? { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } : {}),
  }).format(new Date(value));
}

function requestWord(value: number): string {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "запросов";
  if (mod10 === 1) return "запрос";
  if (mod10 >= 2 && mod10 <= 4) return "запроса";
  return "запросов";
}

function errorWord(value: number): string {
  const mod100 = value % 100;
  const mod10 = value % 10;
  if (mod100 >= 11 && mod100 <= 14) return "ошибок";
  if (mod10 === 1) return "ошибка";
  if (mod10 >= 2 && mod10 <= 4) return "ошибки";
  return "ошибок";
}

function niceAxisMaximum(value: number): number {
  if (value <= 5) return 5;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalized = value / magnitude;
  const rounded = normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return rounded * magnitude;
}

function SummaryCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <article className="rounded-[24px] border border-[#dce6ea] bg-white/95 p-5 shadow-[0_12px_34px_rgba(55,86,108,.055)]">
      <p className="text-[11px] font-semibold uppercase tracking-[.08em] text-[#8497a1]">{label}</p>
      <p className="mt-3 text-[27px] font-semibold tracking-[-.035em] text-[#294c64]">{value}</p>
      <p className="mt-1 text-[11px] leading-5 text-[#8a9ca5]">{detail}</p>
    </article>
  );
}

function ActivityChart({ series, hourly }: { series: AnalyticsSummary["series"]; hourly: boolean }) {
  const maximum = niceAxisMaximum(Math.max(0, ...series.map((item) => item.requests)));
  const ticks = [maximum, Math.round(maximum / 2), 0];
  return (
    <div className="mt-7 w-full min-w-0 pb-1">
      <div className="w-full min-w-0">
        <div className="flex h-[250px]">
          <div className="relative w-10 shrink-0 text-[10px] tabular-nums text-[#96a6ae]" aria-hidden="true">
            {ticks.map((tick, index) => (
              <span
                key={`${tick}-${index}`}
                className={`absolute right-3 ${index === 1 ? "-translate-y-1/2" : ""}`}
                style={index === ticks.length - 1 ? { bottom: 0 } : { top: `${index * 50}%` }}
              >
                {tick}
              </span>
            ))}
          </div>

          <div className="relative min-w-0 flex-1">
            {[0, 50, 100].map((position) => (
              <div key={position} className="absolute inset-x-0 border-t border-dashed border-[#dfe8ec]" style={{ top: `${position}%` }} />
            ))}

            <div className="absolute inset-0 grid gap-1.5 sm:gap-2" style={{ gridTemplateColumns: `repeat(${Math.max(1, series.length)}, minmax(0, 1fr))` }}>
              {series.map((item) => {
                const height = item.requests ? Math.max(3, item.requests / maximum * 100) : 1;
                return (
                  <div key={item.bucket} className="group relative flex h-full min-w-0 items-end justify-center focus-within:z-20 hover:z-20">
                    <button
                      type="button"
                      className="absolute inset-0 cursor-default focus:outline-none"
                      aria-label={`${formatTooltipDate(item.bucket, hourly)}: ${item.requests} ${requestWord(item.requests)}`}
                    />
                    <div
                      className="pointer-events-none relative w-full max-w-8 rounded-t-[8px] bg-[#dbe7ec] transition-[height,background-color,transform] duration-150 group-hover:-translate-y-0.5 group-hover:bg-[#5e89a5] group-focus-within:-translate-y-0.5 group-focus-within:bg-[#5e89a5]"
                      style={{ height: `${height}%` }}
                    >
                      {item.requests > 0 && (
                        <div className="absolute inset-x-0 bottom-0 rounded-t-[8px] bg-[#5e89a5]" style={{ height: `${item.completed / item.requests * 100}%` }} />
                      )}
                      {(item.failed + item.cancelled) > 0 && <div className="absolute inset-x-0 top-0 h-1 rounded-t-[8px] bg-[#d58b82]" />}
                    </div>
                    <div className="pointer-events-none absolute left-1/2 top-3 z-30 min-w-max -translate-x-1/2 -translate-y-1 rounded-[12px] bg-[#294c64] px-3 py-2 text-center text-[11px] font-medium text-white opacity-0 shadow-[0_8px_24px_rgba(41,76,100,.22)] transition group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:translate-y-0 group-focus-within:opacity-100">
                      <strong className="block text-[14px]">{item.requests} {requestWord(item.requests)}</strong>
                      <span className="mt-0.5 block text-[9px] font-normal text-white/75">{formatTooltipDate(item.bucket, hourly)} · МСК</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>

        <div className="ml-10 mt-3 grid gap-0.5 sm:gap-1" style={{ gridTemplateColumns: `repeat(${Math.max(1, series.length)}, minmax(0, 1fr))` }}>
          {series.map((item, index) => {
            const showLabel = hourly || series.length <= 8 || index % 5 === 0 || index === series.length - 1;
            return (
              <span key={item.bucket} className="min-w-0 text-center text-[8px] font-medium tabular-nums tracking-[-.035em] text-[#8fa1aa] sm:text-[9px]">
                {showLabel ? formatAxisDate(item.bucket, hourly) : ""}
              </span>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <section className="mt-7 rounded-[30px] border border-[#dce6ea] bg-white/95 px-6 py-16 text-center shadow-[0_14px_42px_rgba(55,86,108,.06)]">
      <h2 className="text-[19px] font-semibold text-[#294c64]">Статистика начнёт накапливаться после обновления</h2>
      <p className="mx-auto mt-2 max-w-md text-[13px] leading-6 text-[#7c909a]">
        Здесь появятся количество запросов, скорость ответа и ошибки. Старые вопросы из логов автоматически не импортируются.
      </p>
    </section>
  );
}

export function AdminStatistics({ preview = false }: { preview?: boolean }) {
  const router = useRouter();
  const [days, setDays] = useState(1);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (selectedDays: number) => {
    setLoading(true);
    setError(null);
    try {
      const suffix = preview ? "&preview=1" : "";
      const response = await fetch(`/api/admin/statistics?days=${selectedDays}${suffix}`, { cache: "no-store" });
      if (response.status === 401) {
        router.replace("/admin/login");
        return;
      }
      const payload = (await response.json().catch(() => null)) as AnalyticsSummary | { message?: string } | null;
      if (!response.ok) throw new Error(payload && "message" in payload ? payload.message : "Не удалось загрузить статистику.");
      setData(payload as AnalyticsSummary);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Не удалось загрузить статистику.");
    } finally {
      setLoading(false);
    }
  }, [preview, router]);

  useEffect(() => {
    void load(days);
  }, [days, load]);

  const chartSeries = useMemo(() => data?.series ?? [], [data]);

  return (
    <main className="mx-auto w-full max-w-[1280px] px-5 pb-14 pt-5 sm:px-8 sm:pt-6">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-[29px] font-semibold tracking-[-.035em] text-[#294c64] sm:text-[34px]">Статистика базы знаний</h1>
          <p className="mt-2 max-w-2xl text-[14px] leading-6 text-[#718691]">Нагрузка, скорость и качество ответов без сохранения текста вопросов и документов.</p>
        </div>
        <div className="flex w-fit items-center rounded-[17px] border border-[#dce6ea] bg-white/90 p-1 shadow-[0_8px_24px_rgba(55,86,108,.05)]">
          {periods.map((period) => (
            <button
              key={period.days}
              type="button"
              onClick={() => setDays(period.days)}
              className={`h-9 rounded-[13px] px-3 text-[11px] font-semibold transition sm:px-3.5 ${days === period.days ? "bg-[#4f7c9c] text-white shadow-[0_6px_16px_rgba(79,124,156,.22)]" : "text-[#758b97] hover:bg-[#f2f6f7] hover:text-[#466a7f]"}`}
            >
              {period.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="mt-7 flex items-start justify-between gap-4 rounded-[18px] border border-[#efd1d4] bg-[#fff7f7] px-4 py-3.5 text-[13px] text-[#98525a]">
          <div className="flex items-start gap-2.5"><TriangleAlert className="mt-0.5 size-4 shrink-0" /><span>{error}</span></div>
          <button type="button" onClick={() => void load(days)} className="shrink-0 font-semibold hover:text-[#7d3f47]">Повторить</button>
        </div>
      )}

      {loading && !data ? (
        <div className="mt-7 grid min-h-[360px] place-items-center rounded-[30px] border border-[#dce6ea] bg-white/90">
          <div className="text-center text-[#718691]"><LoaderCircle className="mx-auto size-6 animate-spin text-[#4f7c9c]" /><p className="mt-3 text-[12px]">Загружаем статистику…</p></div>
        </div>
      ) : data && data.totals.requests === 0 ? <EmptyState /> : data ? (
        <>
          <div className={`mt-7 grid gap-4 sm:grid-cols-2 lg:grid-cols-4 transition-opacity ${loading ? "opacity-55" : "opacity-100"}`}>
            <SummaryCard label="Запросы" value={formatCount(data.totals.requests)} detail={`${formatCount(data.totals.with_sources)} с найденными источниками`} />
            <SummaryCard label="Успешность" value={formatPercent(data.totals.success_rate)} detail={`${data.totals.failed} ${errorWord(data.totals.failed)} · ${data.totals.cancelled} отменено`} />
            <SummaryCard label="Среднее время ответа" value={formatDuration(data.latency.total_seconds.average)} detail={`95% ответов сформированы не дольше ${formatDuration(data.latency.total_seconds.p95)}`} />
            <SummaryCard label="До первого текста" value={formatDuration(data.latency.ttft_seconds.average)} detail={`В 95% случаев текст появился не позже ${formatDuration(data.latency.ttft_seconds.p95)}`} />
          </div>

          <section className="mt-6 rounded-[30px] border border-[#dce6ea] bg-white/95 p-5 shadow-[0_14px_42px_rgba(55,86,108,.06)] sm:p-7">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="text-[18px] font-semibold text-[#294c64]">Активность</h2>
                <p className="mt-1 text-[11px] text-[#8799a3]">{days === 1 ? "Запросы по часам за последние 24 часа" : `Запросы по дням за последние ${days} дней`} · московское время</p>
              </div>
              <button type="button" onClick={() => void load(days)} disabled={loading} className="grid size-9 place-items-center rounded-full text-[#79909d] transition hover:bg-[#edf3f6] hover:text-[#4f7c9c] disabled:opacity-50" aria-label="Обновить статистику">
                <RefreshCw className={`size-4 ${loading ? "animate-spin" : ""}`} />
              </button>
            </div>

            <ActivityChart series={chartSeries} hourly={days === 1} />

            <div className="mt-5 flex flex-wrap gap-4 border-t border-[#edf1f3] pt-4 text-[10px] text-[#7f929c]">
              <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-[#5e89a5]" />Успешные запросы</span>
              <span className="flex items-center gap-1.5"><i className="size-2 rounded-full bg-[#d58b82]" />Ошибки и отменённые запросы</span>
            </div>
          </section>

          {data.errors.length > 0 && (
            <section className="mt-6 rounded-[28px] border border-[#eadcdd] bg-white/95 p-5 sm:p-7">
              <div>
                <h2 className="text-[16px] font-semibold text-[#5c5558]">Ошибки за период</h2>
                <p className="mt-1 text-[10px] text-[#9b8c8e]">Содержимое пользовательских запросов не сохраняется</p>
              </div>

              <div className="mt-5 overflow-hidden rounded-[18px] border border-[#f0e5e6]">
                <div className="hidden grid-cols-[minmax(0,1.7fr)_minmax(180px,1fr)_190px] gap-4 bg-[#fbf7f7] px-4 py-2.5 text-[10px] font-semibold uppercase tracking-[.06em] text-[#9b898c] sm:grid">
                  <span>Ошибка</span><span>Где произошла</span><span>Дата и время</span>
                </div>
                <div className="divide-y divide-[#f0e8e9]">
                  {data.errors.map((item, index) => (
                    <div key={`${item.error}:${item.stage ?? ""}:${item.occurred_at}:${index}`} className="grid gap-2 px-4 py-4 text-[12px] sm:grid-cols-[minmax(0,1.7fr)_minmax(180px,1fr)_190px] sm:items-center sm:gap-4">
                      <p className="font-semibold leading-5 text-[#6f5e61]">{errorLabels[item.error] ?? item.error}</p>
                      <p className="text-[#89777a]"><span className="sm:hidden">Этап: </span>{item.stage ? stageLabels[item.stage] ?? item.stage : "Весь запрос"}</p>
                      <p className="tabular-nums text-[#9b898c]">{formatDateTime(item.occurred_at)} МСК</p>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          )}
        </>
      ) : null}
    </main>
  );
}
