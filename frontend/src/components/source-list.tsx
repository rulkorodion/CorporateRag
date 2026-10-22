"use client";

import { Eye, File } from "lucide-react";
import Image from "next/image";

import type { Source, SourceAttachment } from "@/lib/api";
import type { DocumentViewerTarget } from "@/components/document-viewer";
import { attachmentFor, basename, citedSheet, extension, sourceLocation } from "@/lib/format";

const fileIcons: Record<string, string> = {
  PDF: "/icons/file-pdf.png",
  DOC: "/icons/file-doc.png",
  DOCX: "/icons/file-doc.png",
  XLS: "/icons/file-xls.png",
  XLSX: "/icons/file-xls.png",
  PNG: "/icons/file-video.png",
  JPG: "/icons/file-video.png",
  JPEG: "/icons/file-video.png",
  MP4: "/icons/file-video.png",
};

type SourceGroup = {
  key: string;
  sources: Source[];
  attachment: SourceAttachment;
};

export function groupSources(
  sources: Source[],
  attachments: SourceAttachment[],
): SourceGroup[] {
  const groups = new Map<string, SourceGroup>();
  for (const source of [...sources].sort((a, b) => a.source_number - b.source_number)) {
    const attachment = attachmentFor(source, attachments);
    const documentId = attachment.document_id ?? source.document_id;
    const versionId = attachment.version_id ?? source.version_id;
    const key = `${documentId}:${versionId}:${attachment.display_relative_path}`;
    const current = groups.get(key);
    if (current) current.sources.push(source);
    else groups.set(key, { key, sources: [source], attachment });
  }
  return [...groups.values()];
}

export function SourceList({
  sources,
  attachments,
  idPrefix,
  onOpen,
}: {
  sources: Source[];
  attachments: SourceAttachment[];
  idPrefix: string;
  onOpen: (target: DocumentViewerTarget) => void;
}) {
  if (!sources.length) return null;
  const groups = groupSources(sources, attachments);

  return (
    <section className="mt-7 border-t border-[#e5eaf2] pt-6" aria-label="Источники">
      <h3 className="mb-4 text-[15px] font-semibold text-[#18376f]">Источники</h3>
      <div className="space-y-2.5">
        {groups.map(({ key, sources: grouped, attachment }) => {
          const source = grouped[0];
          const ext = extension(attachment.display_relative_path);
          const documentId = attachment.document_id ?? source.document_id;
          const versionId = attachment.version_id ?? source.version_id;
          const locations = [...new Set(grouped.map(sourceLocation).filter((value) => value !== "документ"))];
          const location = locations.length
            ? locations.slice(0, 2).join(" · ") + (locations.length > 2 ? ` · ещё ${locations.length - 2}` : "")
            : grouped.length > 1 ? `${grouped.length} фрагментов` : "документ";
          return (
            <button
              type="button"
              id={`${idPrefix}-source-${source.source_number}`}
              key={key}
              onClick={() => {
                if (attachment.external_url) {
                  window.open(attachment.external_url, "_blank", "noopener,noreferrer");
                  return;
                }
                onOpen({
                  type: "document",
                  documentId,
                  versionId,
                  title: attachment.display_relative_path,
                  citedPage: source.page_start ?? undefined,
                  citedSheet: citedSheet(source),
                });
              }}
              className="group grid min-h-[56px] scroll-mt-28 grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-2.5 rounded-[14px] border border-[#dfe7eb] bg-white px-3 transition hover:border-[#aac0ce] hover:shadow-[0_8px_20px_rgba(52,81,101,.07)] max-sm:grid-cols-[auto_minmax(0,1fr)_auto] max-sm:gap-2"
              title={attachment.display_relative_path}
            >
              <span className="grid size-8 shrink-0 place-items-center">
                {attachment.media_type === "video" ? (
                  <Image src="/icons/file-video.png" alt="Видео" width={512} height={512} className="size-[30px] object-contain" />
                ) : fileIcons[ext] ? (
                  <Image src={fileIcons[ext]} alt={ext} width={512} height={512} className="size-[30px] object-contain" />
                ) : (
                  <File className="size-6 text-[#7083a7]" />
                )}
              </span>
              <span className="min-w-0 truncate text-[14px] font-medium text-[#27477f]">
                <span className="mr-1.5 text-[#8393af]">{grouped.map((item) => `[${item.source_number}]`).join(" ")}</span>
                {basename(attachment.display_relative_path)}
              </span>
              <span className="whitespace-nowrap text-[13px] text-[#7387ad] max-sm:col-start-2 max-sm:row-start-2 max-sm:pb-2">
                {location}
              </span>
              <span className="grid size-8 place-items-center rounded-lg text-[#4f7c9c] transition group-hover:bg-[#edf3f6] max-sm:col-start-3 max-sm:row-span-2 max-sm:row-start-1">
                <Eye className="size-[17px]" />
                <span className="sr-only">Открыть источник</span>
              </span>
              {grouped.slice(1).map((item) => <span key={item.source_number} id={`${idPrefix}-source-${item.source_number}`} className="sr-only" />)}
            </button>
          );
        })}
      </div>
    </section>
  );
}
