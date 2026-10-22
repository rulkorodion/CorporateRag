import type { RelatedImage, Source } from "@/lib/api";
import type { DocumentViewerTarget } from "@/components/document-viewer";
import { basename } from "@/lib/format";

export function DirectImages({ images, sources, onOpen }: { images: RelatedImage[]; sources: Source[]; onOpen: (target: DocumentViewerTarget) => void }) {
  const visualDocuments = new Set(
    sources
      .filter(
        (source) =>
          source.provenance.source_fallback === "standalone_visual_filename" &&
          /\.(png|jpe?g)$/i.test(source.relative_path),
      )
      .map((source) => `${source.document_id}:${source.version_id}`),
  );
  const visible = images.filter((image) => visualDocuments.has(`${image.document_id}:${image.version_id}`));
  if (!visible.length) return null;

  return (
    <div className="mt-6 grid gap-4">
      {visible.map((image) => {
        const src = `/api/viewer/assets/${encodeURIComponent(image.document_id)}/${encodeURIComponent(image.version_id)}/${encodeURIComponent(image.asset_id)}/content`;
        return (
          <button type="button" key={image.asset_id} onClick={() => onOpen({ type: "asset", documentId: image.document_id, versionId: image.version_id, assetId: image.asset_id, title: image.local_path })} className="overflow-hidden rounded-2xl border border-[#e1e7f0] bg-[#f7f9fc] text-left transition hover:border-[#aac0ce] hover:shadow-[0_8px_20px_rgba(52,81,101,.07)]">
            {/* Deliberately preserve the original image resolution; the API serves the source asset. */}
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={src} alt={basename(image.local_path)} className="max-h-[620px] w-full object-contain" loading="lazy" />
            <span className="block border-t border-[#e4e9f1] px-4 py-3 text-xs text-[#7184a8]">
              {basename(image.local_path)}
            </span>
          </button>
        );
      })}
    </div>
  );
}
