import { describe, expect, it } from "vitest";

import type { Source } from "@/lib/api";
import { attachmentFor, basename, extension, sourceLocation } from "@/lib/format";

const source: Source = {
  citation: "[1]",
  source_number: 1,
  chunk_id: "chunk-1",
  document_id: "doc-1",
  version_id: "version-1",
  relative_path: "folder/manual.pdf",
  page_start: 3,
  page_end: 5,
  section_title: "Раздел",
  heading_path: ["Раздел"],
  source_type: "native",
  provenance: {},
  source_element_ids: [],
  related_image_ids: [],
};

describe("source formatting", () => {
  it("formats Windows and POSIX paths", () => {
    expect(basename("folder\\manual.pdf")).toBe("manual.pdf");
    expect(extension("folder/manual.xlsx")).toBe("XLSX");
  });

  it("renders page ranges", () => {
    expect(sourceLocation(source)).toBe("стр. 3–5");
  });

  it("falls back to the indexed file without inventing metadata", () => {
    expect(attachmentFor(source, [])).toMatchObject({
      display_relative_path: "folder/manual.pdf",
      download_relative_path: "folder/manual.pdf",
      indexed_document_id: "doc-1",
    });
  });

  it("prefers the backend-provided video attachment", () => {
    expect(
      attachmentFor(source, [
        {
          source_number: 1,
          display_relative_path: "videos/instruction.mp4",
          download_relative_path: "videos/instruction.mp4",
          media_type: "video",
          indexed_relative_path: "folder/manual.pdf",
          indexed_document_id: "doc-1",
        },
      ]).display_relative_path,
    ).toBe("videos/instruction.mp4");
  });
});
