from __future__ import annotations

import re
import time
from collections.abc import Callable
from threading import Event

from ..context.models import ContextPackage, ContextSource
from ..context.tokens import StableTokenCounter
from .base import LLMBackend
from .models import (
    CitationReference,
    GenerationLatency,
    LLMGenerationRequest,
    LLMGenerationResult,
    RAGAnswer,
    UsedSource,
)
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .settings import GenerationSettings


CITATION_RE = re.compile(r"\[(\d+)\]")
INSUFFICIENT_PATTERNS = (
    re.compile(
        r"^\s*в\s+предоставленных\s+(?:источниках|документах)\s+нет\s+информации\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*предоставленные\s+(?:источники|документы)\s+не\s+"
        r"(?:содержат|включают)\s+(?:информации|инструкций|сведений)\b",
        re.IGNORECASE,
    ),
)
INSUFFICIENT_ANSWER = (
    "В базе знаний не найдено достаточно подтверждённой информации по этому вопросу."
)


def is_insufficient_context_answer(content: str) -> bool:
    stripped = content.lstrip()
    beginning = stripped[:500]
    return any(pattern.search(beginning) for pattern in INSUFFICIENT_PATTERNS)


class RAGAnswerService:
    def __init__(self, backend: LLMBackend, settings: GenerationSettings) -> None:
        self.backend = backend
        self.settings = settings

    def generate(
        self,
        context: ContextPackage,
        *,
        retrieval_context_seconds: float = 0.0,
        num_predict: int | None = None,
    ) -> RAGAnswer:
        started = time.perf_counter()
        request = self._request(context, num_predict=num_predict)
        generated = self.backend.generate(request)
        llm_seconds = time.perf_counter() - started
        return self._finalize(
            context,
            generated,
            request=request,
            retrieval_context_seconds=retrieval_context_seconds,
            llm_seconds=llm_seconds,
        )

    def generate_stream(
        self,
        context: ContextPackage,
        *,
        on_delta: Callable[[str], None],
        cancel_event: Event | None = None,
        retrieval_context_seconds: float = 0.0,
        num_predict: int | None = None,
    ) -> RAGAnswer:
        backend = self.backend
        if not hasattr(backend, "stream"):
            raise TypeError("configured LLM backend does not support streaming")
        request = self._request(context, num_predict=num_predict)
        started = time.perf_counter()
        stream = getattr(backend, "stream")
        generated = stream(
            request,
            on_delta=on_delta,
            cancel_event=cancel_event,
        )
        llm_seconds = time.perf_counter() - started
        return self._finalize(
            context,
            generated,
            request=request,
            retrieval_context_seconds=retrieval_context_seconds,
            llm_seconds=llm_seconds,
            redact_invalid=True,
        )

    @staticmethod
    def _request(
        context: ContextPackage, *, num_predict: int | None
    ) -> LLMGenerationRequest:
        return LLMGenerationRequest(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=build_user_prompt(context),
            num_predict=num_predict,
        )

    def _finalize(
        self,
        context: ContextPackage,
        generated: LLMGenerationResult,
        *,
        request: LLMGenerationRequest,
        retrieval_context_seconds: float,
        llm_seconds: float,
        redact_invalid: bool = False,
    ) -> RAGAnswer:
        sources_by_number = {source.source_number: source for source in context.sources}
        insufficient = is_insufficient_context_answer(generated.content)
        mentioned = [] if insufficient else [int(value) for value in CITATION_RE.findall(generated.content)]
        citation_numbers = list(dict.fromkeys(mentioned))
        unknown = [number for number in citation_numbers if number not in sources_by_number]
        known = [number for number in citation_numbers if number in sources_by_number]
        warnings: list[str] = []
        if insufficient:
            validation = "valid"
            status = "completed"
            warnings.append(
                "insufficient_context:no_relevant_sources"
                if context.sources
                else "insufficient_context:no_sources"
            )
        elif unknown:
            warnings.append(
                "unknown_citations:" + ",".join(f"[{number}]" for number in unknown)
            )
            validation = "invalid"
            status = "invalid_citations"
        elif not known and context.sources:
            warnings.append("no_citations_returned_for_nonempty_context")
            validation = "warning"
            status = "citation_warning"
        else:
            validation = "valid"
            status = "completed"
            if not context.sources:
                warnings.append("insufficient_context:no_sources")

        answer_content = INSUFFICIENT_ANSWER if insufficient else generated.content
        if redact_invalid and validation == "invalid":
            answer_content = (
                "Не удалось сформировать ответ с корректными ссылками на источники. "
                "Пожалуйста, повторите вопрос."
            )
            known = []
            warnings.append("streamed_preview_discarded_after_citation_validation")

        used_sources = [self._used_source(sources_by_number[number]) for number in known]
        citations = [
            CitationReference(
                citation=f"[{source.source_number}]",
                source_number=source.source_number,
                chunk_id=source.chunk_id,
                document_id=source.document_id,
                relative_path=source.relative_path,
                page_start=source.page_start,
                page_end=source.page_end,
                section_title=source.section_title,
            )
            for source in used_sources
        ]
        # User-facing images are intentionally limited to standalone JPG/PNG
        # documents that retrieval found and the answer actually cited. Embedded
        # screenshots remain traceable in chunks/context, but are no longer
        # emitted as answer attachments.
        standalone_visual_sources = [
            source
            for source in used_sources
            if source.provenance.get("source_fallback")
            == "standalone_visual_filename"
            and source.relative_path.casefold().endswith((".jpg", ".jpeg", ".png"))
        ]
        allowed_image_ids = {
            asset_id
            for source in standalone_visual_sources
            for asset_id in source.related_image_ids
        }
        related_images = [
            image for image in context.related_images if image.asset_id in allowed_image_ids
        ]
        total_seconds = retrieval_context_seconds + llm_seconds
        prompt_estimated_tokens = StableTokenCounter().count(
            request.system_prompt + "\n\n" + request.user_prompt
        )
        return RAGAnswer(
            query=context.query,
            answer=answer_content,
            status=status,
            citation_validation=validation,
            citations=citations,
            used_sources=used_sources,
            related_images=related_images,
            context_id=context.context_id,
            model=generated.model,
            generation_version=self.settings.generation_version,
            generation_settings_hash=self.settings.settings_hash,
            latency=GenerationLatency(
                retrieval_context_seconds=round(retrieval_context_seconds, 4),
                llm_seconds=round(llm_seconds, 4),
                total_seconds=round(total_seconds, 4),
                ollama_total_seconds=generated.total_duration_seconds,
                ollama_load_seconds=generated.load_duration_seconds,
            ),
            prompt_eval_count=generated.prompt_eval_count,
            prompt_eval_duration_seconds=generated.prompt_eval_duration_seconds,
            eval_count=generated.eval_count,
            eval_duration_seconds=generated.eval_duration_seconds,
            load_duration_seconds=generated.load_duration_seconds,
            total_duration_seconds=generated.total_duration_seconds,
            prompt_estimated_tokens=prompt_estimated_tokens,
            context_estimated_tokens=context.token_count,
            context_characters=len(context.rendered_context),
            done_reason=generated.done_reason,
            warnings=warnings,
        )

    @staticmethod
    def _used_source(source: ContextSource) -> UsedSource:
        return UsedSource(
            citation=source.citation,
            source_number=source.source_number,
            chunk_id=source.chunk_id,
            document_id=source.document_id,
            version_id=source.version_id,
            relative_path=source.relative_path,
            page_start=source.page_start,
            page_end=source.page_end,
            heading_path=list(source.heading_path),
            section_title=source.section_title,
            source_type=source.source_type,
            provenance=dict(source.provenance),
            source_element_ids=list(source.source_element_ids),
            related_image_ids=list(source.related_image_ids),
        )
