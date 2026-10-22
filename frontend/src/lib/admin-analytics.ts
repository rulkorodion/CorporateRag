export type MetricSummary = {
  average: number | null;
  p50: number | null;
  p95: number | null;
  max: number | null;
};

export type AnalyticsSummary = {
  period: { days: number; from: string; to: string };
  totals: {
    requests: number;
    completed: number;
    failed: number;
    cancelled: number;
    success_rate: number | null;
    with_sources: number;
    without_sources: number;
    sources_total: number;
    citations_total: number;
    warnings_total: number;
  };
  latency: Record<
    | "total_seconds"
    | "ttft_seconds"
    | "queue_wait_seconds"
    | "generation_seconds"
    | "embedding_seconds"
    | "retrieval_seconds"
    | "reranker_seconds"
    | "context_builder_seconds",
    MetricSummary
  >;
  tokens: {
    prompt_total: number;
    output_total: number;
    prompt_average: number | null;
    output_average: number | null;
    context_average: number | null;
  };
  citation_validation: Record<string, number>;
  models: Array<{ model: string; requests: number }>;
  errors: Array<{
    error: string;
    stage: string | null;
    occurred_at: string;
  }>;
  series: Array<{
    bucket: string;
    requests: number;
    completed: number;
    failed: number;
    cancelled: number;
  }>;
  privacy: {
    stores_question_text: false;
    stores_answer_text: false;
    stores_source_text: false;
    retention_days: number;
  };
  demo?: boolean;
};

export function formatDuration(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  if (value < 1) return `${Math.round(value * 1_000)} мс`;
  if (value >= 60) {
    const minutes = Math.floor(value / 60);
    const seconds = Math.round(value % 60);
    return `${minutes} мин ${seconds} с`;
  }
  return `${value.toLocaleString("ru-RU", {
    minimumFractionDigits: value < 10 ? 1 : 0,
    maximumFractionDigits: 1,
  })} с`;
}

export function formatCount(value: number): string {
  return new Intl.NumberFormat("ru-RU", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

export function formatPercent(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toLocaleString("ru-RU", { maximumFractionDigits: 1 })}%`;
}

export function demoAnalyticsSummary(days: number): AnalyticsSummary {
  const now = new Date();
  const hourly = days === 1;
  const count = hourly ? 24 : days + 1;
  const series = Array.from({ length: count }, (_, index) => {
    const bucket = new Date(now);
    if (hourly) bucket.setHours(now.getHours() - (count - index - 1), 0, 0, 0);
    else bucket.setDate(now.getDate() - (count - index - 1));
    const shape = [4, 7, 5, 9, 12, 8, 14, 11, 16, 13, 18, 15];
    const requests = Math.max(0, Math.round(shape[index % shape.length] * Math.min(days, 7) / 3));
    return {
      bucket: bucket.toISOString(),
      requests,
      completed: Math.max(0, requests - (index % 9 === 0 ? 1 : 0)),
      failed: index % 9 === 0 && requests ? 1 : 0,
      cancelled: 0,
    };
  });
  return {
    period: {
      days,
      from: new Date(now.getTime() - days * 86_400_000).toISOString(),
      to: now.toISOString(),
    },
    totals: {
      requests: 184,
      completed: 181,
      failed: 2,
      cancelled: 1,
      success_rate: 181 / 184,
      with_sources: 163,
      without_sources: 18,
      sources_total: 421,
      citations_total: 506,
      warnings_total: 7,
    },
    latency: {
      total_seconds: { average: 11.8, p50: 8.2, p95: 28.4, max: 44.1 },
      ttft_seconds: { average: 4.1, p50: 3.6, p95: 8.9, max: 14.2 },
      queue_wait_seconds: { average: 1.8, p50: 0, p95: 7.4, max: 12.1 },
      generation_seconds: { average: 7.3, p50: 5.2, p95: 19.6, max: 32.4 },
      embedding_seconds: { average: 0.12, p50: 0.11, p95: 0.18, max: 0.24 },
      retrieval_seconds: { average: 0.19, p50: 0.16, p95: 0.31, max: 0.48 },
      reranker_seconds: { average: 1.61, p50: 1.55, p95: 2.08, max: 2.64 },
      context_builder_seconds: { average: 0.03, p50: 0.02, p95: 0.06, max: 0.09 },
    },
    tokens: {
      prompt_total: 677_562,
      output_total: 34_776,
      prompt_average: 3_744.5,
      output_average: 192.1,
      context_average: 3_391.4,
    },
    citation_validation: { valid: 169, warning: 12 },
    models: [{ model: "smtek/Qwen3.8-27B:IQ2_XXS-12gb", requests: 181 }],
    errors: [
      { error: "queue_timeout", stage: "llm", occurred_at: now.toISOString() },
      {
        error: "ollama_unavailable",
        stage: "llm",
        occurred_at: new Date(now.getTime() - 47 * 60_000).toISOString(),
      },
    ],
    series,
    privacy: {
      stores_question_text: false,
      stores_answer_text: false,
      stores_source_text: false,
      retention_days: 90,
    },
    demo: true,
  };
}
