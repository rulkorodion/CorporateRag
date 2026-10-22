from __future__ import annotations

import html
import sys
import uuid
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

import streamlit as st

from corporate_rag_ingestion.config import configure_logging
from corporate_rag_ingestion.generation.base import LLMError
from corporate_rag_ingestion.generation.models import (
    RAGAnswer,
    SourceAttachment,
    UsedSource,
)
from corporate_rag_ingestion.retrieval.rerank_service import RerankerUnavailableError
from corporate_rag_ingestion.ui.files import (
    asset_file,
    mime_type,
    safe_path,
)
from corporate_rag_ingestion.ui.presentation import (
    InlineImage,
    answer_blocks,
    pdf_clip_rect,
)
from corporate_rag_ingestion.ui.runtime import PersistentRAGRuntime, RuntimeSettings


st.set_page_config(
    page_title="Корпоративная база знаний",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)
configure_logging("INFO")

st.markdown(
    """
    <style>
      .block-container {max-width: 1180px; padding-top: 2rem;}
      .rag-meta {color: #667085; font-size: .88rem; margin-top: .3rem;}
      .source-card {border-left: 3px solid #5b72e8; padding: .2rem .8rem; margin: .7rem 0;}
      .source-title {font-weight: 650;}
      [data-testid="stChatMessage"] {padding: .5rem 0;}
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource(show_spinner=False)
def load_runtime() -> PersistentRAGRuntime:
    runtime = PersistentRAGRuntime(runtime_settings=RuntimeSettings())
    runtime.warmup()
    return runtime


@st.cache_data(show_spinner=False, max_entries=32)
def read_source_bytes(path_text: str, modified_ns: int) -> bytes:
    del modified_ns
    return Path(path_text).read_bytes()


@st.cache_data(show_spinner=False, max_entries=128)
def render_pdf_asset(
    pdf_path_text: str,
    modified_ns: int,
    page_number: int,
    bbox: tuple[float, float, float, float],
    coordinate_origin: str | None,
) -> tuple[bytes, int]:
    """Render the original PDF region at high DPI without modifying the PDF."""

    del modified_ns
    import fitz

    document = fitz.open(pdf_path_text)
    try:
        page = document.load_page(page_number - 1)
        left, top, right, bottom = bbox
        from corporate_rag_ingestion.schemas import BoundingBox

        clip = fitz.Rect(
            *pdf_clip_rect(
                BoundingBox(
                    left=left,
                    top=top,
                    right=right,
                    bottom=bottom,
                    coordinate_origin=coordinate_origin,
                ),
                page.rect.height,
            )
        )
        clip &= page.rect
        if clip.is_empty or clip.width < 1 or clip.height < 1:
            raise ValueError("empty PDF image region")
        pixmap = page.get_pixmap(matrix=fitz.Matrix(3, 3), clip=clip, alpha=False)
        return pixmap.tobytes("png"), pixmap.width
    finally:
        document.close()


def render_source(
    runtime: PersistentRAGRuntime,
    source: UsedSource,
    attachment: SourceAttachment,
    *,
    message_id: str,
) -> None:
    location = attachment.display_relative_path
    if source.page_start is not None:
        pages = str(source.page_start)
        if source.page_end is not None and source.page_end != source.page_start:
            pages += f"–{source.page_end}"
        location += f", стр. {pages}"
    if source.section_title:
        location += f" · {source.section_title}"
    st.markdown(
        f'<div class="source-card"><div class="source-title">'
        f'{html.escape(source.citation)} {html.escape(location)}'
        f'</div><div class="rag-meta">chunk: {html.escape(source.chunk_id)}</div></div>',
        unsafe_allow_html=True,
    )
    path = safe_path(runtime.knowledge_base_root, attachment.download_relative_path)
    if path is not None:
        if attachment.media_type == "video":
            st.video(str(path))
            st.caption(
                "Ответ найден по транскрипту: "
                f"{attachment.indexed_relative_path}. Источником показано исходное видео."
            )
        st.download_button(
            "Скачать видео" if attachment.media_type == "video" else "Скачать исходный файл",
            data=read_source_bytes(str(path), path.stat().st_mtime_ns),
            file_name=path.name,
            mime=mime_type(path),
            key=f"file-{message_id}-{source.source_number}-{source.chunk_id}",
            use_container_width=False,
        )
    else:
        st.caption("Исходный файл не найден по сохранённому пути.")


def render_inline_image(
    runtime: PersistentRAGRuntime,
    image: InlineImage,
    *,
    message_id: str,
) -> None:
    path = (
        safe_path(runtime.knowledge_base_root, image.local_path)
        if image.path_scope == "knowledge_base"
        else asset_file(runtime.storage_root, image.local_path)
    )
    if path is None:
        return
    image_data: str | bytes = str(path)
    width = image.width
    original_document = safe_path(
        runtime.knowledge_base_root, image.document_relative_path
    )
    if (
        original_document is not None
        and original_document.suffix.casefold() == ".pdf"
        and image.source_page is not None
        and image.bbox is not None
    ):
        try:
            image_data, width = render_pdf_asset(
                str(original_document),
                original_document.stat().st_mtime_ns,
                image.source_page,
                (
                    image.bbox.left,
                    image.bbox.top,
                    image.bbox.right,
                    image.bbox.bottom,
                ),
                image.bbox.coordinate_origin,
            )
        except Exception:
            image_data = str(path)
    if width is None:
        try:
            from PIL import Image

            with Image.open(path) as opened:
                width = opened.width
        except Exception:
            width = 900
    display_width = max(180, min(width, 900))
    caption = f"Источник [{image.source_number}]"
    if image.source_page is not None:
        caption += f" · стр. {image.source_page}"
    st.image(image_data, caption=caption, width=display_width)
    st.download_button(
        "Открыть изображение в исходном качестве",
        data=(
            image_data
            if isinstance(image_data, bytes)
            else read_source_bytes(str(path), path.stat().st_mtime_ns)
        ),
        file_name=path.name,
        mime=mime_type(path),
        key=f"image-{message_id}-{image.asset_id}",
    )


def render_answer(runtime: PersistentRAGRuntime, payload: dict, message_id: str) -> None:
    answer = RAGAnswer.model_validate(payload)
    if not answer.source_attachments:
        answer = runtime.presentation.enrich_attachments(answer)
    blocks = answer_blocks(answer.answer)
    inline_images = runtime.presentation.inline_images(answer, blocks)
    for index, block in enumerate(blocks):
        st.markdown(block)
        for image in inline_images.get(index, []):
            render_inline_image(runtime, image, message_id=message_id)
    st.markdown(
        f'<div class="rag-meta">{answer.model} · '
        f'{answer.latency.total_seconds:.1f} с · citations: '
        f'{html.escape(answer.citation_validation)}</div>',
        unsafe_allow_html=True,
    )
    if answer.warnings:
        for warning in answer.warnings:
            st.warning(warning)

    with st.expander(f"Источники и файлы ({len(answer.used_sources)})"):
        attachments = {
            attachment.source_number: attachment
            for attachment in answer.source_attachments
        }
        for source in answer.used_sources:
            attachment = attachments.get(source.source_number)
            if attachment is None:
                attachment = runtime.presentation.source_attachment(source)
            render_source(
                runtime,
                source,
                attachment,
                message_id=message_id,
            )


st.title("Корпоративная база знаний")
st.caption(
    "Ответы формируются только по документам базы. Цитаты, файлы и изображения "
    "привязаны к реально использованным источникам."
)

with st.sidebar:
    st.subheader("Параметры поиска")
    top_k = st.slider("Результатов после reranker", 3, 10, 8)
    max_chunks = st.slider("Источников в контексте", 3, 10, 8)
    candidate_limit = st.slider("Кандидатов hybrid retrieval", 20, 50, 40, 5)
    max_context_tokens = st.slider("Бюджет контекста", 2000, 10000, 6000, 500)
    st.caption(
        "Embedding постоянно работает на CPU, reranker — на локальной GPU, "
        "LLM — через Ollama-туннель."
    )
    if st.button("Очистить диалог", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

try:
    with st.spinner("Однократная загрузка embedding и reranker…"):
        runtime = load_runtime()
except Exception as exc:
    st.error(f"Не удалось подготовить RAG runtime: {exc}")
    st.stop()

if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["content"])
        else:
            render_answer(runtime, message["answer"], message["id"])

question = st.chat_input("Задайте вопрос по корпоративной базе знаний")
if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        try:
            with st.spinner("Ищу документы, проверяю источники и формирую ответ…"):
                answer, _ = runtime.answer(
                    question,
                    candidate_limit=candidate_limit,
                    top_k=top_k,
                    max_context_tokens=max_context_tokens,
                    max_chunks=max_chunks,
                )
            message_id = uuid.uuid4().hex
            payload = answer.model_dump(mode="json")
            st.session_state.messages.append(
                {"role": "assistant", "answer": payload, "id": message_id}
            )
            render_answer(runtime, payload, message_id)
        except (LLMError, RerankerUnavailableError, ValueError) as exc:
            st.error(str(exc))
        except Exception as exc:
            st.exception(exc)
