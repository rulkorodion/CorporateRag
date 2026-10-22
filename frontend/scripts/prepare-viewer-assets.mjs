import { copyFile, mkdir } from "node:fs/promises";
import path from "node:path";

const publicDirectory = path.resolve("public/vendor/pdfjs");
await mkdir(publicDirectory, { recursive: true });
await copyFile(
  path.resolve("node_modules/pdfjs-dist/build/pdf.worker.min.mjs"),
  path.join(publicDirectory, "pdf.worker.min.mjs"),
);
