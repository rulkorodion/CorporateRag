from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..context.models import RelatedImage


class LLMGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str
    user_prompt: str
    num_predict: int | None = Field(default=None, ge=1)
    response_format: str | dict[str, Any] | None = None


class LLMGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    model: str
    done_reason: str | None = None
    total_duration_seconds: float | None = None
    load_duration_seconds: float | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration_seconds: float | None = None
    eval_count: int | None = None
    eval_duration_seconds: float | None = None


class CitationReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation: str
    source_number: int = Field(ge=1)
    chunk_id: str
    document_id: str
    relative_path: str
    page_start: int | None = None
    page_end: int | None = None
    section_title: str | None = None


class UsedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation: str
    source_number: int = Field(ge=1)
    chunk_id: str
    document_id: str
    version_id: str
    relative_path: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: list[str] = Field(default_factory=list)
    section_title: str | None = None
    source_type: Literal["native", "ocr", "mixed"]
    provenance: dict[str, Any] = Field(default_factory=dict)
    source_element_ids: list[str] = Field(default_factory=list)
    related_image_ids: list[str] = Field(default_factory=list)


class SourceAttachment(BaseModel):
    """User-facing source while preserving the indexed source for traceability."""

    model_config = ConfigDict(extra="forbid")

    source_number: int = Field(ge=1)
    display_relative_path: str
    download_relative_path: str
    media_type: Literal["document", "video"] = "document"
    indexed_relative_path: str
    indexed_document_id: str
    document_id: str | None = None
    version_id: str | None = None
    external_url: str | None = None


class GenerationLatency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retrieval_context_seconds: float = Field(default=0.0, ge=0)
    llm_seconds: float = Field(default=0.0, ge=0)
    total_seconds: float = Field(default=0.0, ge=0)
    ollama_total_seconds: float | None = Field(default=None, ge=0)
    ollama_load_seconds: float | None = Field(default=None, ge=0)


class RAGAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    query: str
    answer: str
    status: Literal["completed", "citation_warning", "invalid_citations"]
    citation_validation: Literal["valid", "warning", "invalid"]
    citations: list[CitationReference] = Field(default_factory=list)
    used_sources: list[UsedSource] = Field(default_factory=list)
    source_attachments: list[SourceAttachment] = Field(default_factory=list)
    related_images: list[RelatedImage] = Field(default_factory=list)
    context_id: str
    model: str
    generation_version: str
    generation_settings_hash: str
    latency: GenerationLatency
    prompt_eval_count: int | None = None
    prompt_eval_duration_seconds: float | None = None
    eval_count: int | None = None
    eval_duration_seconds: float | None = None
    load_duration_seconds: float | None = None
    total_duration_seconds: float | None = None
    prompt_estimated_tokens: int = 0
    context_estimated_tokens: int = 0
    context_characters: int = 0
    done_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class OllamaMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str = ""


class OllamaChatResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    model: str
    message: OllamaMessage
    done: bool = True
    done_reason: str | None = None
    total_duration: int | None = None
    load_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    eval_count: int | None = None
    eval_duration: int | None = None
