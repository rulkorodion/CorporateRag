import { describe, expect, it } from "vitest";

import { viewerURLs } from "@/components/document-viewer";

describe("document viewer routing", () => {
  it("constructs only ID-based viewer URLs", () => {
    const target = { type: "document" as const, documentId: "doc-1", versionId: "v-1", title: "manual.pdf", citedPage: 2 };
    expect(viewerURLs(target).metadata).toBe("/api/viewer/documents/doc-1/v-1/metadata");
    expect(viewerURLs(target).content).toBe("/api/viewer/documents/doc-1/v-1/content");
    expect(viewerURLs(target).onlyofficeConfig).toBe("/api/viewer/documents/doc-1/v-1/onlyoffice-config");
  });
});
