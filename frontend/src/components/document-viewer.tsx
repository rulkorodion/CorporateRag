"use client";

import { FileWarning, LoaderCircle, Minus, Plus } from "lucide-react";
import Image from "next/image";
import { useEffect, useId, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { ViewerMetadata } from "@/lib/api";
import { basename } from "@/lib/format";

export type DocumentViewerTarget =
  | { type: "document"; documentId: string; versionId: string; title: string; citedPage?: number; citedSheet?: string }
  | { type: "asset"; documentId: string; versionId: string; assetId: string; title: string };

type OnlyOfficePayload = { document_server_url: string; config: Record<string, unknown> };
type OnlyOfficeEditor = { destroyEditor?: () => void };
type OnlyOfficeAPI = { DocEditor: new (elementId: string, config: Record<string, unknown>) => OnlyOfficeEditor };

declare global { interface Window { DocsAPI?: OnlyOfficeAPI } }

const scriptLoads = new Map<string, Promise<void>>();

function targetBase(target: DocumentViewerTarget): string {
  const document = encodeURIComponent(target.documentId);
  const version = encodeURIComponent(target.versionId);
  return target.type === "asset"
    ? `/api/viewer/assets/${document}/${version}/${encodeURIComponent(target.assetId)}`
    : `/api/viewer/documents/${document}/${version}`;
}

export function viewerURLs(target: DocumentViewerTarget) {
  const base = targetBase(target);
  return {
    metadata: `${base}/metadata`,
    content: `${base}/content`,
    onlyofficeConfig: target.type === "document" ? `${base}/onlyoffice-config` : undefined,
  };
}

function loadOnlyOfficeScript(serverURL: string): Promise<void> {
  const source = `${serverURL.replace(/\/$/, "")}/web-apps/apps/api/documents/api.js`;
  const current = scriptLoads.get(source);
  if (current) return current;
  const loading = new Promise<void>((resolve, reject) => {
    if (window.DocsAPI) { resolve(); return; }
    const script = document.createElement("script");
    script.src = source;
    script.async = true;
    script.onload = () => window.DocsAPI ? resolve() : reject(new Error("ONLYOFFICE API недоступен."));
    script.onerror = () => reject(new Error("Не удалось загрузить ONLYOFFICE Document Server."));
    document.head.appendChild(script);
  });
  scriptLoads.set(source, loading);
  loading.catch(() => scriptLoads.delete(source));
  return loading;
}

function ViewerError({ message }: { message: string }) {
  return <div className="grid h-full place-items-center p-8 text-center"><div className="max-w-md"><FileWarning className="mx-auto size-10 text-[#a26b72]" /><p className="mt-4 text-[15px] font-medium text-[#563a47]">Не удалось открыть документ</p><p className="mt-2 text-sm leading-6 text-[#8391a8]">{message}</p></div></div>;
}

function OnlyOfficeViewer({ configURL }: { configURL: string }) {
  const rawId = useId();
  const elementId = useMemo(() => `onlyoffice-${rawId.replace(/[^a-zA-Z0-9_-]/g, "")}`, [rawId]);
  const [error, setError] = useState<string>();
  const [editorStarted, setEditorStarted] = useState(false);
  useEffect(() => {
    let disposed = false;
    let editor: OnlyOfficeEditor | undefined;
    void (async () => {
      const response = await fetch(configURL, { cache: "no-store" });
      const body = await response.json().catch(() => null) as (OnlyOfficePayload & { detail?: string }) | null;
      if (!response.ok || !body) throw new Error(body?.detail ?? "ONLYOFFICE пока недоступен.");
      await loadOnlyOfficeScript(body.document_server_url);
      if (disposed || !window.DocsAPI) return;
      editor = new window.DocsAPI.DocEditor(elementId, body.config);
      setEditorStarted(true);
    })().catch((reason: unknown) => {
      if (!disposed) setError(reason instanceof Error ? reason.message : "Не удалось открыть документ в ONLYOFFICE.");
    });
    return () => { disposed = true; editor?.destroyEditor?.(); };
  }, [configURL, elementId]);
  if (error) return <ViewerError message={error} />;
  return <div className="relative h-full min-h-0 w-full bg-[#eef2f4]">
    {!editorStarted && <div className="pointer-events-none absolute inset-0 z-20 grid place-items-center text-sm text-[#7286a5]"><div><LoaderCircle className="mx-auto mb-3 size-7 animate-spin" />Открываем документ в ONLYOFFICE…</div></div>}
    <div id={elementId} className="absolute inset-0 z-10 h-full w-full" />
  </div>;
}

function ImageViewer({ url, title }: { url: string; title: string }) {
  const [scale, setScale] = useState(1);
  return <div className="flex h-full min-h-0 flex-col"><div className="flex justify-center gap-2 border-b border-[#dfe7ec] bg-white px-3 py-2"><Button variant="ghost" size="icon" onClick={() => setScale((value) => Math.max(0.5, value - 0.2))} aria-label="Уменьшить"><Minus className="size-4" /></Button><span className="min-w-12 self-center text-center text-xs text-[#6f829d]">{Math.round(scale * 100)}%</span><Button variant="ghost" size="icon" onClick={() => setScale((value) => Math.min(4, value + 0.2))} aria-label="Увеличить"><Plus className="size-4" /></Button></div><div className="min-h-0 flex-1 overflow-auto bg-[#edf1f3] p-4"><div className="grid min-h-full place-items-center"><Image unoptimized src={url} alt={title} width={1600} height={1200} style={{ transform: `scale(${scale})` }} className="h-auto max-h-full w-auto max-w-full origin-center object-contain transition-transform" /></div></div></div>;
}

export function DocumentViewer({ target, onClose }: { target: DocumentViewerTarget | null; onClose: () => void }) {
  const [metadata, setMetadata] = useState<ViewerMetadata>();
  const [error, setError] = useState<string>();
  const urls = useMemo(() => target ? viewerURLs(target) : null, [target]);
  useEffect(() => {
    if (!target) return;
    let disposed = false;
    fetch(viewerURLs(target).metadata, { cache: "no-store" }).then(async (response) => {
      const body = await response.json().catch(() => null) as (ViewerMetadata & { detail?: string }) | null;
      if (!response.ok || !body) throw new Error(body?.detail ?? "Документ не найден.");
      return body;
    }).then((value) => { if (!disposed) setMetadata(value); }).catch((reason: unknown) => { if (!disposed) setError(reason instanceof Error ? reason.message : "Документ недоступен."); });
    return () => { disposed = true; };
  }, [target]);
  if (!target) return null;
  const location = target.type === "document" && target.citedPage
    ? `стр. ${target.citedPage}`
    : target.type === "document" && target.citedSheet ? `лист «${target.citedSheet}»` : undefined;
  const onlyOffice = metadata && ["pdf", "office", "spreadsheet"].includes(metadata.viewer_kind);
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent className="flex h-[94vh] w-[96vw] max-w-none flex-col overflow-hidden rounded-[22px] bg-white p-0 sm:p-0">
    <header className="flex shrink-0 items-center gap-3 border-b border-[#dfe7ec] px-5 py-4 pr-16"><div className="min-w-0 flex-1"><DialogTitle className="truncate text-[16px] font-semibold text-[#17366e]">{basename(target.title)}</DialogTitle><DialogDescription className="mt-1 flex gap-2 text-xs text-[#8091a9]"><span>{metadata?.file_type.toUpperCase() ?? "Документ"}</span>{location && <><span>·</span><span>{location}</span></>}</DialogDescription></div></header>
    <div className="min-h-0 flex-1 overflow-hidden">
      {error && <ViewerError message={error} />}
      {!error && !metadata && <div className="grid h-full place-items-center text-sm text-[#7286a5]"><div><LoaderCircle className="mx-auto mb-3 size-7 animate-spin" />Получаем сведения о документе…</div></div>}
      {!error && metadata && onlyOffice && urls?.onlyofficeConfig && <OnlyOfficeViewer configURL={urls.onlyofficeConfig} />}
      {!error && metadata?.viewer_kind === "image" && urls && <ImageViewer url={urls.content} title={target.title} />}
      {!error && metadata?.viewer_kind === "video" && urls && <div className="grid h-full min-h-0 place-items-center overflow-auto bg-black p-3"><video src={urls.content} controls preload="metadata" playsInline className="block max-h-full max-w-full object-contain" /></div>}
    </div>
  </DialogContent></Dialog>;
}
