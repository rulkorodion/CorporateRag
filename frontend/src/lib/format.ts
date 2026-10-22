import type { Source, SourceAttachment } from "@/lib/api";

export function basename(path: string): string {
  return path.replaceAll("\\", "/").split("/").filter(Boolean).at(-1) ?? path;
}

export function extension(path: string): string {
  const suffix = basename(path).split(".").at(-1)?.toUpperCase();
  return suffix && suffix.length <= 5 ? suffix : "FILE";
}

export function sourceLocation(source: Source): string {
  if (source.page_start !== null) {
    const pages =
      source.page_end !== null && source.page_end !== source.page_start
        ? `${source.page_start}–${source.page_end}`
        : `${source.page_start}`;
    return `стр. ${pages}`;
  }
  if (source.section_title) return source.section_title;
  if (source.heading_path.length) return source.heading_path.at(-1) ?? "";
  return "документ";
}

export function attachmentFor(
  source: Source,
  attachments: SourceAttachment[],
): SourceAttachment {
  return (
    attachments.find((item) => item.source_number === source.source_number) ?? {
      source_number: source.source_number,
      display_relative_path: source.relative_path,
      download_relative_path: source.relative_path,
      media_type: "document",
      indexed_relative_path: source.relative_path,
      indexed_document_id: source.document_id,
      document_id: source.document_id,
      version_id: source.version_id,
    }
  );
}

export function citedSheet(source: Source): string | undefined {
  const explicit = source.provenance.sheet_name;
  if (typeof explicit === "string" && explicit.trim()) return explicit.trim();
  if (/\.xlsx$/i.test(source.relative_path)) {
    return source.section_title ?? source.heading_path[0] ?? undefined;
  }
  return undefined;
}
