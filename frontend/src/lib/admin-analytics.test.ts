import { describe, expect, it } from "vitest";

import { demoAnalyticsSummary, formatDuration, formatPercent } from "@/lib/admin-analytics";

describe("admin analytics formatting", () => {
  it("formats sub-second, regular and minute durations", () => {
    expect(formatDuration(0.125)).toBe("125 мс");
    expect(formatDuration(8.25)).toBe("8,3 с");
    expect(formatDuration(65)).toBe("1 мин 5 с");
    expect(formatDuration(null)).toBe("—");
  });

  it("formats success ratio as a percentage", () => {
    expect(formatPercent(0.982)).toBe("98,2%");
    expect(formatPercent(null)).toBe("—");
  });

  it("builds privacy-safe preview data for local visual review", () => {
    const preview = demoAnalyticsSummary(7);
    expect(preview.demo).toBe(true);
    expect(preview.series.length).toBe(8);
    expect(preview.privacy.stores_question_text).toBe(false);
    expect(preview.privacy.stores_answer_text).toBe(false);
    expect(preview.privacy.stores_source_text).toBe(false);
  });
});
