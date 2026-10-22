import "server-only";

import { createHash, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";

export type UploadStatus = "queued" | "processing" | "indexed" | "error";

export type UploadJob = {
  id: string;
  fileName: string;
  category: string;
  relativePath: string;
  sizeBytes: number;
  sha256: string;
  replace: boolean;
  status: UploadStatus;
  message: string;
  createdAt: string;
  updatedAt: string;
};

export type UpdateState = {
  status: "idle" | "running" | "success" | "error";
  message: string;
  startedAt?: string;
  finishedAt?: string;
};

const DEFAULT_EXTENSIONS = [".pdf", ".docx", ".doc", ".rtf", ".xlsx", ".png", ".jpg", ".jpeg", ".mp4"];
const SAFE_SEGMENT = /^[^<>:"|?*\u0000-\u001f]+$/u;

function uploadRoot(): string {
  return path.resolve(
    /* turbopackIgnore: true */ process.env.ADMIN_UPLOAD_ROOT ?? path.join(process.cwd(), ".admin-upload"),
  );
}

function knowledgeBaseRoot(): string {
  return path.resolve(
    /* turbopackIgnore: true */ process.env.ADMIN_KNOWLEDGE_BASE_PATH ??
      process.env.KNOWLEDGE_BASE_PATH ??
      path.join(process.cwd(), "..", "База Знаний"),
  );
}

function allowedExtensions(): Set<string> {
  const configured = process.env.ADMIN_ALLOWED_EXTENSIONS?.split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  return new Set(configured?.length ? configured : DEFAULT_EXTENSIONS);
}

export function maxUploadBytes(): number {
  const configured = Number(process.env.ADMIN_MAX_UPLOAD_BYTES ?? 104_857_600);
  return Number.isSafeInteger(configured) && configured > 0 ? configured : 104_857_600;
}

function normalizeSegment(value: string, label: string): string {
  const normalized = value.normalize("NFC").trim();
  if (
    !normalized ||
    normalized === "." ||
    normalized === ".." ||
    normalized.length > 180 ||
    !SAFE_SEGMENT.test(normalized) ||
    normalized.includes("/") ||
    normalized.includes("\\")
  ) {
    throw new Error(`Некорректное ${label}.`);
  }
  return normalized;
}

export function normalizeCategory(value: string): string {
  const parts = value
    .replaceAll("\\", "/")
    .split("/")
    .filter((part) => part.trim());
  if (!parts.length || parts.length > 6) throw new Error("Выберите раздел базы знаний.");
  const normalized = parts.map((part) => normalizeSegment(part, "название раздела")).join("/");
  if (normalized.length > 500) throw new Error("Путь раздела слишком длинный.");
  return normalized;
}

export function normalizeFileName(value: string): string {
  const name = normalizeSegment(value, "имя файла");
  if (name.startsWith(".") || name.startsWith("~$")) throw new Error("Недопустимое имя файла.");
  const extension = path.extname(name).toLowerCase();
  if (!allowedExtensions().has(extension)) {
    throw new Error(`Формат ${extension || "без расширения"} не поддерживается.`);
  }
  return name;
}

function hasExpectedSignature(extension: string, bytes: Buffer): boolean {
  if (extension === ".pdf") return bytes.subarray(0, 5).toString("ascii") === "%PDF-";
  if (extension === ".png") return bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  if ([".jpg", ".jpeg"].includes(extension)) return bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff;
  if ([".docx", ".xlsx"].includes(extension)) return bytes[0] === 0x50 && bytes[1] === 0x4b;
  if (extension === ".doc") return bytes.subarray(0, 8).equals(Buffer.from([0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1]));
  if (extension === ".rtf") return bytes.subarray(0, 5).toString("ascii").toLowerCase() === "{\\rtf";
  if (extension === ".mp4") return bytes.subarray(4, 8).toString("ascii") === "ftyp";
  return false;
}

async function ensureDirectories(): Promise<void> {
  const root = uploadRoot();
  await Promise.all([
    fs.mkdir(path.join(root, "queue"), { recursive: true }),
    fs.mkdir(path.join(root, "jobs"), { recursive: true }),
    fs.mkdir(path.join(root, "trigger"), { recursive: true }),
    fs.mkdir(path.join(root, "runtime"), { recursive: true }),
  ]);
}

async function writeJsonAtomic(target: string, value: unknown): Promise<void> {
  const temporary = `${target}.${randomUUID()}.tmp`;
  await fs.writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await fs.rename(temporary, target);
}

export async function listCategories(): Promise<string[]> {
  const root = knowledgeBaseRoot();
  const result: string[] = [];
  async function walk(directory: string, relative: string, depth: number): Promise<void> {
    if (depth > 3 || result.length >= 400) return;
    let entries;
    try {
      entries = await fs.readdir(directory, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name, "ru"))) {
      if (!entry.isDirectory() || entry.name.startsWith(".")) continue;
      const child = relative ? `${relative}/${entry.name}` : entry.name;
      result.push(child);
      await walk(path.join(directory, entry.name), child, depth + 1);
      if (result.length >= 400) return;
    }
  }
  await walk(root, "", 1);
  return result;
}

export async function listJobs(): Promise<{ jobs: UploadJob[]; update: UpdateState }> {
  await ensureDirectories();
  const root = uploadRoot();
  const names = await fs.readdir(path.join(root, "jobs"));
  const jobs: UploadJob[] = [];
  for (const name of names.filter((item) => item.endsWith(".json"))) {
    try {
      jobs.push(JSON.parse(await fs.readFile(path.join(root, "jobs", name), "utf8")) as UploadJob);
    } catch {
      // Ignore an incomplete or externally damaged status record.
    }
  }
  jobs.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  let update: UpdateState = { status: "idle", message: "Обновления ещё не запускались." };
  try {
    update = JSON.parse(await fs.readFile(path.join(root, "runtime", "update-status.json"), "utf8")) as UpdateState;
  } catch {
    // Initial state.
  }
  return { jobs: jobs.slice(0, 100), update };
}

export async function queueUpload(input: {
  file: File;
  category: string;
  replace: boolean;
  username: string;
}): Promise<UploadJob> {
  await ensureDirectories();
  const fileName = normalizeFileName(input.file.name);
  const category = normalizeCategory(input.category);
  const sizeBytes = input.file.size;
  if (sizeBytes <= 0) throw new Error("Файл пуст.");
  if (sizeBytes > maxUploadBytes()) {
    throw new Error(`Файл превышает лимит ${Math.floor(maxUploadBytes() / 1024 / 1024)} МБ.`);
  }
  const bytes = Buffer.from(await input.file.arrayBuffer());
  const extension = path.extname(fileName).toLowerCase();
  if (!hasExpectedSignature(extension, bytes)) throw new Error("Содержимое файла не соответствует его расширению.");

  const relativePath = `${category}/${fileName}`;
  const existingPath = path.resolve(knowledgeBaseRoot(), ...relativePath.split("/"));
  const rootWithSeparator = `${knowledgeBaseRoot()}${path.sep}`;
  if (!existingPath.startsWith(rootWithSeparator)) throw new Error("Некорректный путь назначения.");
  try {
    await fs.access(existingPath);
    if (!input.replace) {
      const error = new Error("Файл с таким именем уже существует.") as Error & { code?: string };
      error.code = "FILE_EXISTS";
      throw error;
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }

  const { jobs } = await listJobs();
  if (jobs.some((job) => job.relativePath === relativePath && ["queued", "processing"].includes(job.status))) {
    const error = new Error("Этот файл уже ожидает обработки.") as Error & { code?: string };
    error.code = "ALREADY_QUEUED";
    throw error;
  }

  const id = randomUUID();
  const now = new Date().toISOString();
  const job: UploadJob = {
    id,
    fileName,
    category,
    relativePath,
    sizeBytes,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    replace: input.replace,
    status: "queued",
    message: "Файл принят и ожидает обработки.",
    createdAt: now,
    updatedAt: now,
  };
  const root = uploadRoot();
  const queueDirectory = path.join(root, "queue", id);
  await fs.mkdir(queueDirectory, { mode: 0o700 });
  try {
    await fs.writeFile(path.join(queueDirectory, "payload"), bytes, { flag: "wx", mode: 0o600 });
    await writeJsonAtomic(path.join(root, "jobs", `${id}.json`), job);
    await writeJsonAtomic(path.join(queueDirectory, "request.json"), { ...job, uploadedBy: input.username });
  } catch (error) {
    await fs.rm(queueDirectory, { recursive: true, force: true });
    throw error;
  }
  return job;
}

export async function requestImmediateUpdate(username: string): Promise<void> {
  await ensureDirectories();
  await writeJsonAtomic(path.join(uploadRoot(), "trigger", "update.request"), {
    requestedAt: new Date().toISOString(),
    requestedBy: username,
  });
}
