import { describe, expect, it } from "vitest";

import { groupSources } from "@/components/source-list";
import type { Source } from "@/lib/api";

function source(number: number, documentId: string, versionId: string): Source {
  return {
    citation: `[${number}]`, source_number: number, chunk_id: `chunk-${number}`,
    document_id: documentId, version_id: versionId, relative_path: "folder/manual.docx",
    page_start: null, page_end: null, section_title: null, heading_path: [], source_type: "native",
    provenance: {}, source_element_ids: [`element-${number}`], related_image_ids: [],
  };
}

describe("source grouping", () => {
  it("shows one card per document version while retaining all citation fragments", () => {
    const groups = groupSources(
      [source(1, "doc", "v1"), source(2, "doc", "v1"), source(3, "doc", "v2")],
      [],
    );
    expect(groups).toHaveLength(2);
    expect(groups[0].sources.map((item) => item.source_number)).toEqual([1, 2]);
    expect(groups[1].sources.map((item) => item.source_number)).toEqual([3]);
  });
});
