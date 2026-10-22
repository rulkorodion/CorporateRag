from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..context.models import RelatedImage
from ..generation.models import CitationReference, SourceAttachment, UsedSource


class ScheduleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet_names: list[str] = Field(default_factory=list, max_length=29)
    record_kinds: list[Literal["product", "service"]] = Field(
        default_factory=list, max_length=2
    )
    product_kinds: list[Literal["round_tube", "profile", "sheet"]] = Field(
        default_factory=list, max_length=3
    )
    dimensions: list[str] = Field(default_factory=list, max_length=4)
    steel: str | None = Field(default=None, max_length=40)
    date_from: date | None = None
    date_to: date | None = None
    date_explicit: bool = False
    period_mode: Literal[
        "default_future",
        "explicit",
        "open_from",
        "open_after",
        "open_until",
        "all_available",
    ] = "default_future"
    include_undated: bool = False
    products_only: bool = False
    query_type: Literal[
        "product_search",
        "production_plan",
        "stand_schedule",
        "count",
        "service_events",
        "summary",
        "date_bounds",
        "date_list",
    ] = "production_plan"
    response_mode: Literal["full"] = "full"


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    candidate_limit: int | None = Field(default=None, ge=1, le=100)
    top_k: int | None = Field(default=None, ge=1, le=20)
    max_context_tokens: int | None = Field(default=None, ge=500, le=32000)
    max_chunks: int | None = Field(default=None, ge=1, le=20)
    num_predict: int | None = Field(default=None, ge=64, le=8192)
    schedule_page: int | None = Field(default=None, ge=1, le=10000)
    schedule_snapshot_version: str | None = Field(
        default=None,
        min_length=16,
        max_length=64,
        pattern=r"^[0-9a-fA-F]+$",
    )
    schedule_selection: ScheduleSelection | None = None

    @model_validator(mode="after")
    def validate_schedule_page(self) -> "AnswerRequest":
        supplied = (
            self.schedule_page is not None,
            self.schedule_snapshot_version is not None,
            self.schedule_selection is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "schedule_page, schedule_snapshot_version and schedule_selection "
                "must be provided together"
            )
        return self


class APILatency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedding_queue_wait_seconds: float = 0.0
    embedding_execution_seconds: float = 0.0
    retrieval_seconds: float = 0.0
    reranker_queue_wait_seconds: float = 0.0
    reranker_seconds: float = 0.0
    reranker_execution_seconds: float = 0.0
    context_builder_seconds: float = 0.0
    llm_queue_wait_seconds: float = 0.0
    queue_wait_seconds: float = 0.0
    generation_seconds: float = 0.0
    ttft_seconds: float | None = None
    total_seconds: float = 0.0


class APITelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_chunks: int = 0
    reranked_chunks: int = 0
    context_chunks: int = 0
    context_sources: int = 0
    context_estimated_tokens: int = 0
    context_budget_tokens: int = 0
    max_chunks_budget: int = 0
    context_characters: int = 0
    context_source_characters: int = 0
    context_render_overhead_characters: int = 0
    prompt_estimated_tokens: int = 0
    prompt_eval_count: int | None = None
    prompt_estimate_error_tokens: int | None = None
    prompt_estimate_ratio: float | None = None
    prompt_eval_duration_seconds: float | None = None
    eval_count: int | None = None
    eval_duration_seconds: float | None = None
    load_duration_seconds: float | None = None
    total_duration_seconds: float | None = None
    done_reason: str | None = None
    output_limit_tokens: int | None = None
    embedding_queue_position: int = 0
    reranker_queue_position: int = 0
    llm_queue_position: int = 0
    context_stats: dict[str, int] = Field(default_factory=dict)


class SchedulePagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_question: str
    selection: ScheduleSelection
    snapshot_version: str
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=1)
    has_previous: bool
    has_next: bool
    snapshot_changed: bool = False


class AnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    answer_kind: Literal["rag", "rolling_schedule"] = "rag"
    answer: str
    status: str
    citation_validation: str
    citations: list[CitationReference] = Field(default_factory=list)
    sources: list[UsedSource] = Field(default_factory=list)
    source_attachments: list[SourceAttachment] = Field(default_factory=list)
    related_images: list[RelatedImage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    latency: APILatency
    telemetry: APITelemetry = Field(default_factory=APITelemetry)
    model: str
    context_id: str
    schedule_pagination: SchedulePagination | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    error: str
    message: str
    stage: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str
    metrics: dict[str, Any]
    stages: dict[str, dict[str, Any]]
