import { afterEach, describe, expect, it, vi } from "vitest";

import { createClientId } from "@/lib/client-id";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("createClientId", () => {
  it("uses randomUUID when the page provides it", () => {
    const randomUUID = vi.fn(() => "secure-context-id");
    vi.stubGlobal("crypto", { randomUUID });

    expect(createClientId()).toBe("secure-context-id");
    expect(randomUUID).toHaveBeenCalledOnce();
  });

  it("creates distinct IDs when randomUUID is unavailable over plain HTTP", () => {
    vi.stubGlobal("crypto", {});
    vi.spyOn(Date, "now").mockReturnValue(1_789_000_000_000);

    const first = createClientId();
    const second = createClientId();

    expect(first).toMatch(/^exchange-[a-z0-9]+-[a-z0-9]+$/);
    expect(second).toMatch(/^exchange-[a-z0-9]+-[a-z0-9]+$/);
    expect(second).not.toBe(first);
  });
});
