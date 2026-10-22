from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from .config import Settings, configure_logging
from .db import create_schema, make_engine, make_session_factory
from .pipeline import IngestionPipeline, IngestionSummary
from .storage import NormalizedStorage
from .chunking.pipeline import ChunkingPipeline
from .chunking.chunker import StructuralChunker
from .chunking.overrides import ChunkingOverrides
from .chunking.settings import ChunkingSettings
from .chunking.storage import ChunkStorage
from .context.builder import ContextBuilder
from .context.repository import AssetRepository, ChunkRepository
from .context.settings import ContextBuilderSettings
from .generation.base import LLMError
from .generation.ollama import OllamaClient
from .generation.pipeline import RAGPipeline, RetrievalContextProvider
from .generation.service import RAGAnswerService
from .generation.settings import GenerationSettings
from .incremental.ocr import IncrementalOCRProcessor
from .incremental.preparer import IncrementalDocumentPreparer
from .incremental.service import IncrementalUpdater
from .registry_models import Document, DocumentVersion
from .scanner import FileScanner
from .ocr.cache import OCRCache
from .ocr.canonicalize import OCRImportCanonicalizer
from .ocr.importer import OCRImporter
from .ocr.manifest import OCRManifestGenerator, load_manifest
from .ocr.service import OCRService
from .ocr.settings import MonkeyOCRSettings
from .retrieval.dense import QwenDenseEmbedder
from .retrieval.indexer import HybridIndexer
from .retrieval.rerank_service import RerankerUnavailableError, RerankSearchService
from .retrieval.reranker import QwenCrossEncoderReranker
from .retrieval.search import HybridSearchService
from .retrieval.settings import RetrievalSettings
from .retrieval.sparse import BM25SparseEmbedder
from .retrieval.store import QdrantStore
from .rolling_schedule.settings import RollingScheduleSettings
from .rolling_schedule.store import RollingScheduleStore, read_status
from .rolling_schedule.sync import RollingScheduleSyncService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Corporate knowledge-base ingestion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--knowledge-base", type=Path)
        command_parser.add_argument("--storage", type=Path)
        command_parser.add_argument("--database-url")
        command_parser.add_argument("--json", action="store_true", dest="as_json")

    for command, help_text in (
        ("ingest", "Scan, register, and normalize new or changed files"),
        ("scan", "Scan and update the registry without format parsing"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        add_common(command_parser)
        if command == "ingest":
            command_parser.add_argument(
                "--reprocess", action="store_true", help="Normalize current versions again"
            )
    chunk_parser = subparsers.add_parser(
        "chunk", help="Build deterministic structural RetrievalChunk files"
    )
    add_common(chunk_parser)
    index_parser = subparsers.add_parser(
        "index", help="Build or synchronize the dense + BM25 Qdrant index"
    )
    add_common(index_parser)
    index_parser.add_argument("--qdrant-url")
    update_parser = subparsers.add_parser(
        "update", help="Safely process and index only new, changed, or deleted documents"
    )
    add_common(update_parser)
    update_parser.add_argument("--qdrant-url")
    update_parser.add_argument(
        "--dry-run", action="store_true", help="Show the incremental plan without writes"
    )
    update_parser.add_argument(
        "--force",
        action="append",
        default=[],
        metavar="PATH_OR_DOCUMENT_ID",
        help="Reprocess one exact document ID or relative-path glob; repeatable",
    )
    update_parser.add_argument(
        "--allow-empty-registry",
        action="store_true",
        help="Allow an intentional first full import into an empty registry",
    )
    update_parser.add_argument(
        "--bootstrap-ocr-fingerprints",
        action="store_true",
        help="One-time render-only migration for reusing unchanged OCR pages across versions",
    )
    rolling_parser = subparsers.add_parser(
        "rolling-sync",
        help="Download, validate, and atomically update the rolling schedule snapshot",
    )
    add_common(rolling_parser)
    rolling_parser.add_argument(
        "--force", action="store_true", help="Ignore the daily update interval"
    )
    rolling_parser.add_argument(
        "--source-file",
        type=Path,
        help="Use a local XLSX instead of downloading (validation/testing)",
    )
    rolling_status_parser = subparsers.add_parser(
        "rolling-status", help="Print rolling schedule synchronization status"
    )
    add_common(rolling_status_parser)
    search_parser = subparsers.add_parser(
        "search", help="Search RetrievalChunk with dense, sparse, or hybrid retrieval"
    )
    add_common(search_parser)
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--mode", choices=("dense", "sparse", "hybrid", "rerank", "context"), default="hybrid"
    )
    search_parser.add_argument("--top-k", type=int, default=10)
    search_parser.add_argument(
        "--candidates",
        type=int,
        help="Hybrid candidates passed to the reranker (default: configured value)",
    )
    search_parser.add_argument("--qdrant-url")
    search_parser.add_argument("--document-id")
    search_parser.add_argument("--source-type", choices=("native", "ocr", "mixed"))
    search_parser.add_argument("--chunk-type", choices=("text", "table"))
    search_parser.add_argument("--context-max-tokens", type=int, default=6000)
    search_parser.add_argument("--context-max-chunks", type=int, default=10)
    search_parser.add_argument("--max-chunks-per-document", type=int, default=3)
    search_parser.add_argument("--neighbor-expansion-limit", type=int, default=2)
    answer_parser = subparsers.add_parser(
        "answer", help="Run retrieval, reranking, context building, and grounded generation"
    )
    add_common(answer_parser)
    answer_parser.add_argument("query")
    answer_parser.add_argument("--candidates", type=int)
    answer_parser.add_argument("--top-k", type=int)
    answer_parser.add_argument("--qdrant-url")
    answer_parser.add_argument("--document-id")
    answer_parser.add_argument("--source-type", choices=("native", "ocr", "mixed"))
    answer_parser.add_argument("--chunk-type", choices=("text", "table"))
    answer_parser.add_argument("--context-max-tokens", type=int, default=6000)
    answer_parser.add_argument("--context-max-chunks", type=int, default=10)
    answer_parser.add_argument("--max-chunks-per-document", type=int, default=3)
    answer_parser.add_argument("--neighbor-expansion-limit", type=int, default=2)
    answer_parser.add_argument(
        "--show-context", action="store_true", help="Include the complete ContextPackage"
    )
    manifest_parser = subparsers.add_parser(
        "ocr-manifest", help="Build a manifest for unresolved scan_candidate pages"
    )
    add_common(manifest_parser)
    manifest_parser.add_argument("--output", type=Path)

    import_parser = subparsers.add_parser(
        "ocr-import", help="Validate and integrate completed external OCR JSON"
    )
    add_common(import_parser)
    import_parser.add_argument("--input", type=Path)
    import_parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate identity, SHA, model, and settings without writing cache or normalized data",
    )
    import_parser.add_argument(
        "--allow-ambiguous",
        action="store_true",
        help="Allow only explicitly user-reviewed ambiguous pages to be imported",
    )

    canonicalize_parser = subparsers.add_parser(
        "ocr-prepare-import",
        help="Create a strict import-ready JSONL copy from an OCR batch with temporary IDs",
    )
    add_common(canonicalize_parser)
    canonicalize_parser.add_argument("--input", type=Path, required=True)
    canonicalize_parser.add_argument("--output", type=Path, required=True)
    canonicalize_parser.add_argument(
        "--allow-ambiguous",
        action="store_true",
        help="Allow canonicalization of explicitly user-reviewed ambiguous pages",
    )

    run_parser = subparsers.add_parser(
        "ocr-run", help="Run one future MonkeyOCR manifest shard on a configured GPU worker"
    )
    add_common(run_parser)
    run_parser.add_argument("--manifest", type=Path)
    run_parser.add_argument("--shard-index", type=int, required=True)
    run_parser.add_argument("--shard-count", type=int, default=1)
    run_parser.add_argument("--allow-monkeyocr", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {}
    if getattr(args, "knowledge_base", None) is not None:
        overrides["knowledge_base_path"] = args.knowledge_base
    if getattr(args, "storage", None) is not None:
        overrides["storage_path"] = args.storage
    if getattr(args, "database_url", None) is not None:
        overrides["database_url"] = args.database_url
    return Settings(**overrides)


def format_summary(summary: IngestionSummary) -> str:
    extensions = ", ".join(f"{key}: {value}" for key, value in summary.by_extension.items())
    lines = [
        "Ingestion summary",
        f"  Found files: {summary.found_files}",
        f"  By extension: {extensions or '-'}",
        f"  New: {summary.new}",
        f"  Changed: {summary.changed}",
        f"  Unchanged: {summary.unchanged}",
        f"  Successfully normalized: {summary.normalized}",
        f"  Unsupported (registered only): {summary.unsupported}",
        f"  Errors: {summary.errors}",
        f"  Legacy .doc normalized/failed: {summary.legacy_doc_normalized}/{summary.legacy_doc_failed}",
        f"  Legacy .rtf normalized/failed: {summary.legacy_rtf_normalized}/{summary.legacy_rtf_failed}",
        "  PDF pages: " + ", ".join(
            f"{page_type}={summary.pdf_page_types.get(page_type, 0)}"
            for page_type in ("text", "mixed", "scan_candidate", "visual_or_slide", "ambiguous")
        ),
        f"  PDF pages needing OCR: {summary.pdf_needs_ocr}",
        f"  Extracted images: {summary.extracted_images}",
        f"  MP4 linked/missing/not_required: {summary.mp4_linked}/{summary.mp4_missing}/{summary.mp4_not_required}",
    ]
    if summary.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in summary.warnings)
    if summary.error_details:
        lines.append("Errors:")
        lines.extend(f"  - {error}" for error in summary.error_details)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    settings = settings_from_args(args)
    configure_logging(settings.log_level)
    if args.command in {"rolling-sync", "rolling-status"}:
        rolling_settings = RollingScheduleSettings.from_environment(
            settings.storage_root
        )
        if args.command == "rolling-status":
            payload = read_status(rolling_settings.status_path)
            payload.setdefault("database_path", str(rolling_settings.database_path))
            payload["database_available"] = rolling_settings.database_path.is_file()
            if payload["database_available"]:
                payload["sheet_diagnostics"] = [
                    asdict(item)
                    for item in RollingScheduleStore(
                        rolling_settings.database_path,
                        rolling_settings.status_path,
                    ).sheet_diagnostics()
                ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if payload["database_available"] else 1
        report = RollingScheduleSyncService(rolling_settings).sync(
            force=args.force,
            source_file=args.source_file,
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 1 if report.status == "failed" else 0
    if args.command in {"ingest", "scan"}:
        engine = make_engine(settings.database_url)
        create_schema(engine)
        pipeline = IngestionPipeline(settings, make_session_factory(engine))
        summary = pipeline.run(
            scan_only=args.command == "scan",
            reprocess=bool(getattr(args, "reprocess", False)),
        )
        if args.as_json:
            print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        else:
            print(format_summary(summary))
        return 1 if summary.errors else 0

    if args.command == "chunk":
        chunk_summary = ChunkingPipeline(
            normalized_root=settings.normalized_root,
            chunk_storage=ChunkStorage(settings.chunk_storage_root),
            settings=ChunkingSettings(),
            overrides=ChunkingOverrides.load(settings.chunk_overrides_file),
        ).run()
        print(json.dumps(chunk_summary.to_dict(), ensure_ascii=False, indent=2))
        return 1 if chunk_summary.errors or chunk_summary.missing_source_elements else 0

    if args.command == "update":
        if args.dry_run:
            url = make_url(settings.database_url)
            if url.get_backend_name() == "sqlite" and url.database:
                database_path = Path(url.database)
                if not database_path.is_absolute():
                    database_path = (Path.cwd() / database_path).resolve()
                if not database_path.is_file():
                    raise FileNotFoundError(
                        f"dry-run requires an existing registry and will not create one: {database_path}"
                    )
        engine = make_engine(settings.database_url)
        if not args.dry_run:
            create_schema(engine)
        session_factory = make_session_factory(engine)
        with session_factory() as session:
            document_count = int(session.scalar(select(func.count(Document.id))) or 0)
        if args.bootstrap_ocr_fingerprints:
            if args.dry_run:
                raise ValueError(
                    "--bootstrap-ocr-fingerprints cannot be combined with --dry-run"
                )
            if document_count == 0:
                raise RuntimeError(
                    "OCR fingerprint bootstrap requires the existing document registry"
                )
            normalized_storage = NormalizedStorage(settings.storage_root)
            fingerprint_ocr = IncrementalOCRProcessor(
                storage=normalized_storage,
                knowledge_base_root=settings.knowledge_base_root,
                cache=OCRCache(settings.ocr_cache_root),
                settings=MonkeyOCRSettings(),
                project_root=settings.resolve_path(Path(".")),
            )
            scanned = {
                item.relative_path: item
                for item in FileScanner(settings.knowledge_base_root).scan()
            }
            summary: dict[str, object] = {
                "documents_checked": 0,
                "ocr_pages_fingerprinted": 0,
                "ocr_page_fingerprints_reused": 0,
                "errors": [],
            }
            with session_factory() as session:
                for document in session.scalars(select(Document)):
                    if not document.current_version_id:
                        continue
                    version = session.get(DocumentVersion, document.current_version_id)
                    source = scanned.get(document.relative_path)
                    if (
                        version is None
                        or not version.normalized_path
                        or source is None
                        or source.extension != ".pdf"
                    ):
                        continue
                    summary["documents_checked"] = int(summary["documents_checked"]) + 1
                    try:
                        if source.sha256 != document.current_sha256:
                            raise RuntimeError("source SHA differs from active registry version")
                        normalized_document = normalized_storage.load(version.normalized_path)
                        created, reused = fingerprint_ocr.bootstrap_completed_page_hashes(
                            normalized_document,
                            source.absolute_path,
                        )
                        summary["ocr_pages_fingerprinted"] = (
                            int(summary["ocr_pages_fingerprinted"]) + created
                        )
                        summary["ocr_page_fingerprints_reused"] = (
                            int(summary["ocr_page_fingerprints_reused"]) + reused
                        )
                    except Exception as exc:
                        errors = summary["errors"]
                        assert isinstance(errors, list)
                        errors.append(
                            f"{document.relative_path}: {type(exc).__name__}: {exc}"
                        )
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1 if summary["errors"] else 0
        if args.dry_run:
            # Planning needs only the scanner and registry. Avoid even
            # constructing Qdrant/embedding/sparse/OCR backends in this mode.
            updater = IncrementalUpdater(
                knowledge_base_root=settings.knowledge_base_root,
                storage_root=settings.storage_root,
                session_factory=session_factory,
                preparer=None,  # type: ignore[arg-type]
                indexer=None,  # type: ignore[arg-type]
                store=None,  # type: ignore[arg-type]
                monkeyocr_enabled=settings.monkeyocr_enabled,
            )
            result = updater.run(dry_run=True, force=args.force)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 1 if result.errors else 0
        if document_count == 0 and not args.allow_empty_registry:
            raise RuntimeError(
                "incremental update refused an empty registry; restore the existing "
                "document_registry.db or pass --allow-empty-registry for an intentional first import"
            )
        retrieval_overrides = {}
        if args.qdrant_url:
            retrieval_overrides["qdrant_url"] = args.qdrant_url
        retrieval_settings = RetrievalSettings(**retrieval_overrides).resolve_paths(
            settings.resolve_path(Path("."))
        )
        store = QdrantStore(retrieval_settings)
        dense = QwenDenseEmbedder(retrieval_settings)
        sparse = BM25SparseEmbedder(retrieval_settings)
        normalized_storage = NormalizedStorage(settings.storage_root)
        chunk_storage = ChunkStorage(settings.chunk_storage_root)
        chunking_settings = ChunkingSettings()
        chunking_overrides = ChunkingOverrides.load(settings.chunk_overrides_file)
        monkey_settings = MonkeyOCRSettings()
        ingestion = IngestionPipeline(settings, session_factory)
        ocr = IncrementalOCRProcessor(
            storage=normalized_storage,
            knowledge_base_root=settings.knowledge_base_root,
            cache=OCRCache(settings.ocr_cache_root),
            settings=monkey_settings,
            project_root=settings.resolve_path(Path(".")),
        )
        preparer = IncrementalDocumentPreparer(
            ingestion=ingestion,
            normalized_storage=normalized_storage,
            chunk_storage=chunk_storage,
            chunker=StructuralChunker(chunking_settings, chunking_overrides),
            chunking_settings=chunking_settings,
            ocr=ocr,
        )
        updater = IncrementalUpdater(
            knowledge_base_root=settings.knowledge_base_root,
            storage_root=settings.storage_root,
            session_factory=session_factory,
            preparer=preparer,
            indexer=HybridIndexer(retrieval_settings, store, dense, sparse),
            store=store,
            monkeyocr_enabled=settings.monkeyocr_enabled,
        )
        try:
            result = updater.run(dry_run=args.dry_run, force=args.force)
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
            return 1 if result.errors else 0
        finally:
            dense.release()
            store.close()

    if args.command in {"index", "search"}:
        retrieval_overrides = {}
        if getattr(args, "qdrant_url", None):
            retrieval_overrides["qdrant_url"] = args.qdrant_url
        retrieval_settings = RetrievalSettings(**retrieval_overrides).resolve_paths(
            settings.resolve_path(Path("."))
        )
        store = QdrantStore(retrieval_settings)
        dense = QwenDenseEmbedder(retrieval_settings)
        sparse = BM25SparseEmbedder(retrieval_settings)
        try:
            if args.command == "index":
                result = HybridIndexer(
                    retrieval_settings, store, dense, sparse
                ).run(settings.chunk_storage_root)
                print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
                return 1 if result.errors else 0
            filters = {
                "document_id": args.document_id,
                "source_type": args.source_type,
                "chunk_type": args.chunk_type,
            }
            hybrid = HybridSearchService(
                retrieval_settings, store, dense, sparse
            )
            if args.mode in {"rerank", "context"}:
                reranker = QwenCrossEncoderReranker(retrieval_settings)
                release_dense = (
                    dense.release
                    if retrieval_settings.reranker_release_dense_before_load
                    else None
                )
                try:
                    results = RerankSearchService(
                        retrieval_settings,
                        hybrid,
                        reranker,
                        release_candidate_backend=release_dense,
                    ).search(
                        args.query,
                        top_k=args.top_k,
                        candidate_limit=args.candidates,
                        filters=filters,
                    )
                except RerankerUnavailableError as exc:
                    print(
                        json.dumps({"error": str(exc)}, ensure_ascii=False),
                        file=sys.stderr,
                    )
                    return 2
            else:
                results = hybrid.search(
                    args.query,
                    mode=args.mode,
                    top_k=args.top_k,
                    filters=filters,
                )
            if args.mode == "context":
                package = ContextBuilder(
                    ChunkRepository(settings.chunk_storage_root),
                    AssetRepository(settings.normalized_root, settings.knowledge_base_root),
                    settings=ContextBuilderSettings(
                        max_context_tokens=args.context_max_tokens,
                        max_chunks=args.context_max_chunks,
                        max_chunks_per_document=args.max_chunks_per_document,
                        neighbor_expansion_limit=args.neighbor_expansion_limit,
                    ),
                ).build(args.query, results)
                print(json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2))
            else:
                print(
                    json.dumps(
                        [result.model_dump(mode="json") for result in results],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return 0
        finally:
            store.close()

    if args.command == "answer":
        retrieval_overrides = {}
        if args.qdrant_url:
            retrieval_overrides["qdrant_url"] = args.qdrant_url
        retrieval_settings = RetrievalSettings(**retrieval_overrides).resolve_paths(
            settings.resolve_path(Path("."))
        )
        candidate_limit = args.candidates or retrieval_settings.reranker_candidate_limit
        top_k = args.top_k or retrieval_settings.reranker_final_top_k
        store = QdrantStore(retrieval_settings)
        ollama = OllamaClient(GenerationSettings())
        try:
            dense = QwenDenseEmbedder(retrieval_settings)
            sparse = BM25SparseEmbedder(retrieval_settings)
            hybrid = HybridSearchService(retrieval_settings, store, dense, sparse)
            reranker = QwenCrossEncoderReranker(retrieval_settings)
            rerank_service = RerankSearchService(
                retrieval_settings,
                hybrid,
                reranker,
                release_candidate_backend=(
                    dense.release
                    if retrieval_settings.reranker_release_dense_before_load
                    else None
                ),
            )
            context_builder = ContextBuilder(
                ChunkRepository(settings.chunk_storage_root),
                AssetRepository(settings.normalized_root, settings.knowledge_base_root),
                settings=ContextBuilderSettings(
                    max_context_tokens=args.context_max_tokens,
                    max_chunks=args.context_max_chunks,
                    max_chunks_per_document=args.max_chunks_per_document,
                    neighbor_expansion_limit=args.neighbor_expansion_limit,
                ),
            )
            provider = RetrievalContextProvider(
                rerank_service,
                context_builder,
                top_k=top_k,
                candidate_limit=candidate_limit,
                filters={
                    "document_id": args.document_id,
                    "source_type": args.source_type,
                    "chunk_type": args.chunk_type,
                },
            )
            pipeline = RAGPipeline(
                provider,
                RAGAnswerService(ollama, ollama.settings),
            )
            try:
                answer, context = pipeline.answer_with_context(args.query)
            except (LLMError, RerankerUnavailableError) as exc:
                print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
                return 2
            if args.as_json:
                payload: object = answer.model_dump(mode="json")
                if args.show_context:
                    payload = {
                        "answer": payload,
                        "context": context.model_dump(mode="json"),
                    }
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(answer.answer)
                if answer.used_sources:
                    print("\nSources:")
                    for source in answer.used_sources:
                        location = source.relative_path
                        if source.page_start is not None:
                            location += f", page {source.page_start}"
                            if source.page_end and source.page_end != source.page_start:
                                location += f"-{source.page_end}"
                        print(f"  {source.citation} {location} ({source.chunk_id})")
                if answer.warnings:
                    print("\nWarnings:")
                    for warning in answer.warnings:
                        print(f"  - {warning}")
                if args.show_context:
                    print("\nContext:\n" + context.rendered_context)
                print(
                    f"\nLatency: total={answer.latency.total_seconds:.2f}s, "
                    f"retrieval/context={answer.latency.retrieval_context_seconds:.2f}s, "
                    f"LLM={answer.latency.llm_seconds:.2f}s"
                )
            return 3 if answer.citation_validation == "invalid" else 0
        finally:
            ollama.close()
            store.close()

    monkey_settings = MonkeyOCRSettings()
    cache = OCRCache(settings.ocr_cache_root)
    if args.command == "ocr-manifest":
        output = args.output or settings.ocr_manifest_file
        if not output.is_absolute():
            output = settings.resolve_path(output)
        generator = OCRManifestGenerator(
            normalized_root=settings.normalized_root,
            knowledge_base_root=settings.knowledge_base_root,
            cache=cache,
            settings=monkey_settings,
        )
        tasks, summary = generator.generate()
        generator.save(output, tasks, summary)
        payload = {**summary.to_dict(), "manifest": str(output)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ocr-import":
        input_path = args.input or settings.ocr_import_root
        if not input_path.is_absolute():
            input_path = settings.resolve_path(input_path)
        importer = OCRImporter(
            storage=NormalizedStorage(settings.storage_root),
            knowledge_base_root=settings.knowledge_base_root,
            cache=cache,
            settings=monkey_settings,
            allow_ambiguous=args.allow_ambiguous,
        )
        summary = importer.import_path(input_path, validate_only=args.validate_only)
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 1 if summary.rejected else 0

    if args.command == "ocr-prepare-import":
        input_path = args.input if args.input.is_absolute() else settings.resolve_path(args.input)
        output_path = args.output if args.output.is_absolute() else settings.resolve_path(args.output)
        summary = OCRImportCanonicalizer(
            normalized_root=settings.normalized_root,
            knowledge_base_root=settings.knowledge_base_root,
            settings=monkey_settings,
        ).canonicalize(
            input_path,
            output_path,
            allow_ambiguous=args.allow_ambiguous,
        )
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
        return 1 if summary.rejected else 0

    if not args.allow_monkeyocr or not settings.monkeyocr_enabled:
        raise SystemExit(
            "MonkeyOCR is disabled. Set OCR_MONKEYOCR_ENABLED=true and pass --allow-monkeyocr "
            "only on a prepared GPU worker."
        )
    manifest_path = args.manifest or settings.ocr_manifest_file
    if not manifest_path.is_absolute():
        manifest_path = settings.resolve_path(manifest_path)
    service = OCRService(
        cache=cache,
        settings=monkey_settings,
        project_root=settings.resolve_path(Path(".")),
    )
    result_integrator = OCRImporter(
        storage=NormalizedStorage(settings.storage_root),
        knowledge_base_root=settings.knowledge_base_root,
        cache=cache,
        settings=monkey_settings,
    )
    payload = service.process_manifest(
        load_manifest(manifest_path),
        knowledge_base_root=settings.knowledge_base_root,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        allow_monkeyocr=True,
        on_completed=result_integrator.integrate_completed,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return (
        1
        if payload["counts"]["FAILED"]
        or payload["counts"]["PARTIAL"]
        or payload["integration"]["failed"]
        else 0
    )


if __name__ == "__main__":
    sys.exit(main())
