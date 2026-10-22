"use client";

import {
  AlertCircle,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  FolderOpen,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Upload,
  X,
} from "lucide-react";
import Image from "next/image";
import { useRouter } from "next/navigation";
import { ChangeEvent, DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import type { UpdateState, UploadJob, UploadStatus } from "@/lib/admin-storage";

const ACCEPTED = ".pdf,.doc,.docx,.rtf,.xlsx,.png,.jpg,.jpeg,.mp4";
const fileIcons: Record<string, string> = {
  pdf: "/icons/file-pdf.png",
  doc: "/icons/file-doc.png",
  docx: "/icons/file-doc.png",
  rtf: "/icons/file-doc.png",
  xlsx: "/icons/file-xls.png",
  png: "/icons/file-video.png",
  jpg: "/icons/file-video.png",
  jpeg: "/icons/file-video.png",
  mp4: "/icons/file-video.png",
};

type UploadsResponse = { jobs: UploadJob[]; update: UpdateState };
type Replacement = { file: File; category: string };

function extension(name: string): string {
  return name.split(".").at(-1)?.toLowerCase() ?? "";
}

function formatBytes(value: number): string {
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} КБ`;
  return `${(value / 1024 / 1024).toFixed(value < 10 * 1024 * 1024 ? 1 : 0)} МБ`;
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

const statusView: Record<UploadStatus, { label: string; className: string; icon: typeof Clock3 }> = {
  queued: { label: "Ожидает", className: "bg-[#edf3f6] text-[#55788d]", icon: Clock3 },
  processing: { label: "Обрабатывается", className: "bg-[#eef1f8] text-[#68789b]", icon: LoaderCircle },
  indexed: { label: "Добавлен", className: "bg-[#edf6f1] text-[#4f7f67]", icon: CheckCircle2 },
  error: { label: "Ошибка", className: "bg-[#fff0f0] text-[#a2575e]", icon: AlertCircle },
};

function StatusBadge({ status }: { status: UploadStatus }) {
  const view = statusView[status];
  const Icon = view.icon;
  return (
    <span className={`inline-flex h-7 items-center gap-1.5 rounded-full px-2.5 text-[11px] font-semibold ${view.className}`}>
      <Icon className={`size-3.5 ${status === "processing" ? "animate-spin" : ""}`} />
      {view.label}
    </span>
  );
}

export function AdminDashboard({
  initialCategories,
  initialJobs,
  initialUpdate,
}: {
  initialCategories: string[];
  initialJobs: UploadJob[];
  initialUpdate: UpdateState;
}) {
  const router = useRouter();
  const inputRef = useRef<HTMLInputElement>(null);
  const categoryRef = useRef<HTMLDivElement>(null);
  const [categories] = useState<string[]>(initialCategories);
  const [category, setCategory] = useState("");
  const [categoryOpen, setCategoryOpen] = useState(false);
  const [categorySearch, setCategorySearch] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [jobs, setJobs] = useState<UploadJob[]>(initialJobs);
  const [update, setUpdate] = useState<UpdateState>(initialUpdate);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [notice, setNotice] = useState<{ type: "success" | "error"; text: string } | null>(null);
  const [replacement, setReplacement] = useState<Replacement | null>(null);

  const refresh = useCallback(async () => {
    const response = await fetch("/api/admin/uploads", { cache: "no-store" });
    if (response.status === 401) {
      router.replace("/admin/login");
      return;
    }
    if (response.ok) {
      const data = (await response.json()) as UploadsResponse;
      setJobs(data.jobs);
      setUpdate(data.update);
    }
  }, [router]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 5_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!categoryOpen) return;
    function closeOnOutsideClick(event: PointerEvent) {
      if (categoryRef.current && !categoryRef.current.contains(event.target as Node)) {
        setCategoryOpen(false);
      }
    }
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setCategoryOpen(false);
    }
    document.addEventListener("pointerdown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [categoryOpen]);

  const totalSize = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);
  const filteredCategories = useMemo(() => {
    const query = categorySearch.trim().toLocaleLowerCase("ru-RU");
    return categories
      .filter((item) => !query || item.toLocaleLowerCase("ru-RU").includes(query))
      .slice(0, 80);
  }, [categories, categorySearch]);
  const newCategory = categorySearch.trim();
  const newCategoryAlreadyExists = categories.some(
    (item) => item.toLocaleLowerCase("ru-RU") === newCategory.toLocaleLowerCase("ru-RU"),
  );

  function addFiles(incoming: FileList | File[]) {
    const next = Array.from(incoming);
    setFiles((current) => {
      const seen = new Set(current.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
      return [...current, ...next.filter((file) => !seen.has(`${file.name}:${file.size}:${file.lastModified}`))].slice(0, 20);
    });
    setNotice(null);
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  async function uploadOne(file: File, selectedCategory: string, replace = false): Promise<boolean> {
    const form = new FormData();
    form.set("file", file);
    form.set("category", selectedCategory);
    form.set("replace", String(replace));
    const response = await fetch("/api/admin/uploads", { method: "POST", body: form });
    const data = (await response.json().catch(() => null)) as { error?: string; message?: string } | null;
    if (response.status === 409 && data?.error === "file_exists" && !replace) {
      setReplacement({ file, category: selectedCategory });
      return false;
    }
    if (!response.ok) throw new Error(data?.message ?? `Не удалось загрузить «${file.name}».`);
    return true;
  }

  async function uploadSelected() {
    const selectedCategory = category.trim();
    if (!selectedCategory) {
      setNotice({ type: "error", text: "Выберите или укажите раздел базы знаний." });
      return;
    }
    if (!files.length) {
      setNotice({ type: "error", text: "Сначала выберите документы." });
      return;
    }
    setUploading(true);
    setNotice(null);
    const completed: File[] = [];
    try {
      for (const file of files) {
        if (await uploadOne(file, selectedCategory)) completed.push(file);
        else break;
      }
      setFiles((current) => current.filter((file) => !completed.includes(file)));
      if (completed.length) {
        setNotice({ type: "success", text: `${completed.length} файл(а) принято. Обработка запустится автоматически.` });
      }
      await refresh();
    } catch (caught) {
      setFiles((current) => current.filter((file) => !completed.includes(file)));
      setNotice({ type: "error", text: caught instanceof Error ? caught.message : "Не удалось загрузить файлы." });
    } finally {
      setUploading(false);
    }
  }

  async function confirmReplacement() {
    if (!replacement) return;
    setUploading(true);
    setNotice(null);
    try {
      await uploadOne(replacement.file, replacement.category, true);
      setFiles((current) => current.filter((file) => file !== replacement.file));
      setReplacement(null);
      setNotice({ type: "success", text: "Новая версия файла принята и будет безопасно обработана." });
      await refresh();
    } catch (caught) {
      setNotice({ type: "error", text: caught instanceof Error ? caught.message : "Не удалось заменить файл." });
    } finally {
      setUploading(false);
    }
  }

  async function triggerUpdate() {
    setTriggering(true);
    try {
      const response = await fetch("/api/admin/update", { method: "POST" });
      const body = (await response.json().catch(() => null)) as { message?: string } | null;
      if (!response.ok) throw new Error(body?.message ?? "Не удалось запустить обновление.");
      setUpdate({
        status: "running",
        message: "Запускается инкрементальное обновление базы знаний.",
        startedAt: new Date().toISOString(),
      });
      setNotice({ type: "success", text: body?.message ?? "Обновление поставлено в очередь." });
    } catch (caught) {
      setUpdate({
        status: "error",
        message: caught instanceof Error ? caught.message : "Не удалось запустить обновление.",
        finishedAt: new Date().toISOString(),
      });
    } finally {
      setTriggering(false);
    }
  }

  return (
    <main className="mx-auto w-full max-w-[1120px] px-5 pb-14 pt-5 sm:px-8 sm:pt-6">
      <div>
        <h1 className="text-[29px] font-semibold tracking-[-.035em] text-[#294c64] sm:text-[34px]">Управление документами</h1>
        <p className="mt-2 max-w-2xl text-[14px] leading-6 text-[#718691]">Добавляйте документы в очередь. Система проверит файл и обновит только затронутую часть базы знаний.</p>
      </div>

      <div
        className={`grid transition-[grid-template-rows,margin-top,opacity] duration-300 ease-out ${notice ? "mt-7 grid-rows-[1fr] opacity-100" : "mt-0 grid-rows-[0fr] opacity-0"}`}
        aria-live="polite"
      >
        <div className="overflow-hidden">
          {notice && (
            <div className={`flex items-start gap-2.5 rounded-[17px] border px-4 py-3 text-[13px] leading-5 ${notice.type === "success" ? "border-[#cfe1d7] bg-[#f3f9f6] text-[#527461]" : "border-[#efd1d4] bg-[#fff7f7] text-[#98525a]"}`}>
              {notice.type === "success" ? <CheckCircle2 className="mt-0.5 size-4 shrink-0" /> : <AlertCircle className="mt-0.5 size-4 shrink-0" />}
              {notice.text}
            </div>
          )}
        </div>
      </div>

      <div className="mt-7 grid gap-6 lg:grid-cols-[minmax(0,1.55fr)_minmax(290px,.75fr)]">
        <section className="rounded-[30px] border border-[#dce6ea] bg-white/95 p-5 shadow-[0_14px_42px_rgba(55,86,108,.08)] sm:p-7">
          <div>
            <h2 className="text-[19px] font-semibold text-[#294c64]">Добавить документы</h2>
            <p className="mt-1 text-[12px] text-[#8396a0]">До 20 файлов за раз, не более 100 МБ каждый</p>
          </div>

          <div ref={categoryRef} className="relative mt-6">
            <p className="text-[12px] font-semibold uppercase tracking-[.08em] text-[#718894]">Раздел базы</p>
            <button
              type="button"
              onClick={() => {
                setCategoryOpen((current) => !current);
                setCategorySearch("");
              }}
              className={`mt-2 flex h-12 w-full items-center gap-3 rounded-[16px] border bg-[#fbfcfd] px-4 text-left transition ${categoryOpen ? "border-[#91aebe] ring-3 ring-[#4f7c9c]/10" : "border-[#dce6ea] hover:border-[#b8cbd4]"}`}
              aria-haspopup="listbox"
              aria-expanded={categoryOpen}
            >
              <FolderOpen className="size-[18px] shrink-0 text-[#7e98a6]" />
              <span className={`min-w-0 flex-1 truncate text-[14px] font-medium ${category ? "text-[#36586d]" : "text-[#9caeb7]"}`}>
                {category || "Выберите раздел"}
              </span>
              <ChevronDown className={`size-4 shrink-0 text-[#8ca0aa] transition ${categoryOpen ? "rotate-180" : ""}`} />
            </button>

            {categoryOpen && (
              <div className="absolute left-0 right-0 top-[78px] z-30 overflow-hidden rounded-[20px] border border-[#d9e4e9] bg-white p-2 shadow-[0_18px_45px_rgba(50,78,96,.16)]">
                <div className="flex h-11 items-center gap-2.5 rounded-[14px] bg-[#f4f7f8] px-3.5">
                  <Search className="size-4 shrink-0 text-[#8499a4]" />
                  <input
                    autoFocus
                    value={categorySearch}
                    onChange={(event) => setCategorySearch(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent text-[13px] text-[#36586d] outline-none placeholder:text-[#9baab1]"
                    placeholder="Найти или создать раздел"
                  />
                  {categorySearch && (
                    <button type="button" onClick={() => setCategorySearch("")} className="grid size-7 place-items-center rounded-full text-[#91a2aa] hover:bg-white hover:text-[#5b7483]" aria-label="Очистить поиск">
                      <X className="size-3.5" />
                    </button>
                  )}
                </div>

                <div className="mt-2 max-h-56 overflow-y-auto overscroll-contain p-1" role="listbox">
                  {filteredCategories.map((item) => (
                    <button
                      type="button"
                      key={item}
                      onClick={() => {
                        setCategory(item);
                        setCategoryOpen(false);
                      }}
                      className={`flex min-h-10 w-full items-center gap-2.5 rounded-[12px] px-3 py-2 text-left text-[13px] transition ${category === item ? "bg-[#edf3f6] font-semibold text-[#3f687f]" : "text-[#5f7582] hover:bg-[#f4f7f8]"}`}
                      role="option"
                      aria-selected={category === item}
                    >
                      <FolderOpen className="size-4 shrink-0 text-[#7f9aa8]" />
                      <span className="min-w-0 flex-1 truncate" title={item}>{item}</span>
                      {category === item && <Check className="size-4 shrink-0 text-[#4f7c9c]" />}
                    </button>
                  ))}

                  {!filteredCategories.length && !newCategory && (
                    <p className="px-3 py-5 text-center text-[12px] text-[#93a3ab]">Разделы не найдены</p>
                  )}

                  {newCategory && !newCategoryAlreadyExists && (
                    <button
                      type="button"
                      onClick={() => {
                        setCategory(newCategory);
                        setCategoryOpen(false);
                      }}
                      className="mt-1 flex min-h-11 w-full items-center gap-2.5 rounded-[13px] border border-dashed border-[#b9ccd5] bg-[#f8fafb] px-3 py-2 text-left text-[13px] font-medium text-[#4f7185] transition hover:border-[#86a7b7] hover:bg-[#f0f5f7]"
                    >
                      <Plus className="size-4 shrink-0 text-[#4f7c9c]" />
                      <span className="min-w-0 truncate">Создать раздел «{newCategory}»</span>
                    </button>
                  )}
                </div>
              </div>
            )}
          </div>

          <div
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }}
            onDrop={drop}
            onClick={() => inputRef.current?.click()}
            className={`mt-5 grid min-h-[190px] cursor-pointer place-items-center rounded-[24px] border border-dashed p-6 text-center transition ${dragging ? "border-[#4f7c9c] bg-[#edf3f6]" : "border-[#bfd0d9] bg-[#f9fbfc] hover:border-[#8fabb9] hover:bg-[#f5f9fa]"}`}
          >
            <input ref={inputRef} type="file" multiple accept={ACCEPTED} className="hidden" onChange={(event: ChangeEvent<HTMLInputElement>) => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }} />
            <div>
              <span className="mx-auto grid size-12 place-items-center rounded-full bg-[#4f7c9c] text-white shadow-[0_10px_24px_rgba(79,124,156,.24)]"><Upload className="size-5" /></span>
              <p className="mt-4 text-[14px] font-semibold text-[#36586d]">Перетащите файлы сюда</p>
              <p className="mt-1 text-[12px] text-[#8a9da7]">или нажмите, чтобы выбрать на компьютере</p>
              <p className="mt-3 text-[10px] uppercase tracking-[.08em] text-[#a0afb7]">PDF · DOCX · XLSX · PNG · JPG</p>
            </div>
          </div>

          {files.length > 0 && (
            <div className="mt-5 space-y-2">
              {files.map((file) => (
                <div key={`${file.name}:${file.size}:${file.lastModified}`} className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 rounded-[16px] border border-[#e1e8eb] bg-white px-3 py-2.5">
                  <Image src={fileIcons[extension(file.name)] ?? "/icons/file-doc.png"} alt="" width={512} height={512} className="size-8 object-contain" />
                  <div className="min-w-0"><p className="truncate text-[13px] font-medium text-[#35566a]">{file.name}</p><p className="mt-0.5 text-[10px] text-[#92a2aa]">{formatBytes(file.size)}</p></div>
                  <button onClick={(event) => { event.stopPropagation(); setFiles((current) => current.filter((item) => item !== file)); }} className="grid size-8 place-items-center rounded-full text-[#8ea0a9] transition hover:bg-[#f0f4f6] hover:text-[#4f6572]" aria-label={`Убрать ${file.name}`}><X className="size-4" /></button>
                </div>
              ))}
              <div className="flex items-center justify-between pt-2 text-[11px] text-[#879aa4]"><span>{files.length} файл(а)</span><span>{formatBytes(totalSize)}</span></div>
            </div>
          )}

          <Button onClick={uploadSelected} disabled={uploading || !files.length} className="mt-5 h-12 w-full rounded-[17px]">
            {uploading ? <LoaderCircle className="size-[18px] animate-spin" /> : <Upload className="size-[18px]" />}
            Загрузить в базу знаний
          </Button>
        </section>

        <aside className="grid gap-6 lg:grid-rows-2">
          <section className="flex h-full flex-col rounded-[28px] border border-[#dce6ea] bg-white/95 p-6 shadow-[0_14px_42px_rgba(55,86,108,.07)]">
            <div className="flex items-center gap-3">
              <span className={`grid size-10 place-items-center rounded-[15px] ${update.status === "error" ? "bg-[#fff0f0] text-[#a2575e]" : update.status === "success" ? "bg-[#edf6f1] text-[#4f7f67]" : "bg-[#edf3f6] text-[#4f7c9c]"}`}>
                {update.status === "running" ? <LoaderCircle className="size-5 animate-spin" /> : update.status === "error" ? <AlertCircle className="size-5" /> : update.status === "success" ? <CheckCircle2 className="size-5" /> : <Clock3 className="size-5" />}
              </span>
              <div><h2 className="text-[15px] font-semibold text-[#35566a]">Обновление базы</h2><p className="mt-0.5 text-[11px] text-[#91a1a9]">Автоматически после загрузки</p></div>
            </div>
            <p className="mt-4 text-[12px] leading-5 text-[#718691]">{update.message}</p>
            {(update.finishedAt || update.startedAt) && <p className="mt-2 text-[10px] text-[#9aabb3]">{formatDate(update.finishedAt ?? update.startedAt ?? "")}</p>}
            <div className="mt-auto pt-5">
              <Button variant="outline" onClick={triggerUpdate} disabled={triggering || update.status === "running"} className="h-10 w-full rounded-[16px] text-[12px]">
                {triggering ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />} Обработать сейчас
              </Button>
            </div>
          </section>

          <section className="flex h-full items-center rounded-[28px] border border-[#dce6ea] bg-[#f7fafb]/95 p-6">
            <div className="flex gap-3"><ShieldCheck className="mt-0.5 size-5 shrink-0 text-[#4f7c9c]" /><div><h3 className="text-[13px] font-semibold text-[#456477]">Безопасное обновление</h3><p className="mt-1 text-[11px] leading-5 text-[#81939d]">Старая версия документа остаётся доступной, пока новая не будет полностью обработана и добавлена в поиск.</p></div></div>
          </section>
        </aside>
      </div>

      <section className="mt-6 rounded-[30px] border border-[#dce6ea] bg-white/95 p-5 shadow-[0_14px_42px_rgba(55,86,108,.06)] sm:p-7">
        <div className="flex items-end justify-between gap-4"><div><h2 className="text-[19px] font-semibold text-[#294c64]">Последние загрузки</h2><p className="mt-1 text-[12px] text-[#8799a3]">Статус подготовки и появления документов в поиске</p></div><button onClick={() => void refresh()} className="grid size-9 place-items-center rounded-full text-[#79909d] transition hover:bg-[#edf3f6] hover:text-[#4f7c9c]" aria-label="Обновить список"><RefreshCw className="size-4" /></button></div>
        {!jobs.length ? (
          <div className="mt-5 rounded-[20px] border border-dashed border-[#cfdbdf] bg-[#fafcfc] px-5 py-9 text-center text-[12px] text-[#91a2aa]">Загруженных через интерфейс документов пока нет.</div>
        ) : (
          <div className="mt-5 space-y-2.5">
            {jobs.map((job) => (
              <div key={job.id} className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 rounded-[17px] border border-[#e1e8eb] bg-[#fcfdfd] px-3.5 py-3 sm:grid-cols-[auto_minmax(0,1fr)_auto_auto]">
                <Image src={fileIcons[extension(job.fileName)] ?? "/icons/file-doc.png"} alt="" width={512} height={512} className="size-8 object-contain" />
                <div className="min-w-0"><p className="truncate text-[13px] font-semibold text-[#35566a]">{job.fileName}</p><p className="mt-0.5 truncate text-[10px] text-[#8ca0aa]">{job.category} · {formatBytes(job.sizeBytes)}</p>{job.status === "error" && <p className="mt-1 truncate text-[10px] text-[#a7656b]" title={job.message}>{job.message}</p>}</div>
                <span className="hidden whitespace-nowrap text-[10px] text-[#9aabb3] sm:block">{formatDate(job.updatedAt)}</span>
                <StatusBadge status={job.status} />
              </div>
            ))}
          </div>
        )}
      </section>

      <Dialog open={Boolean(replacement)} onOpenChange={(open) => { if (!open && !uploading) setReplacement(null); }}>
        <DialogContent className="max-w-[460px] rounded-[28px] p-7 sm:p-8">
          <span className="grid size-11 place-items-center rounded-[16px] bg-[#fff1eb] text-[#a66b50]"><AlertCircle className="size-5" /></span>
          <DialogTitle className="mt-5 text-[21px] font-semibold tracking-[-.02em] text-[#294c64]">Заменить существующий файл?</DialogTitle>
          <DialogDescription className="mt-2 text-[13px] leading-6 text-[#748994]">В разделе уже есть файл «{replacement?.file.name}». Предыдущая рабочая версия сохранится в registry до успешной обработки новой.</DialogDescription>
          <div className="mt-6 flex flex-col-reverse gap-2.5 sm:flex-row sm:justify-end"><Button variant="outline" className="rounded-[15px]" onClick={() => setReplacement(null)} disabled={uploading}>Отмена</Button><Button className="rounded-[15px]" onClick={confirmReplacement} disabled={uploading}>{uploading && <LoaderCircle className="size-4 animate-spin" />}Заменить файл</Button></div>
        </DialogContent>
      </Dialog>
    </main>
  );
}
