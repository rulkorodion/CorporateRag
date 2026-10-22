from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "document"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    relative_path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    folder_path: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("document_version.id", use_alter=True), nullable=True
    )
    role: Mapped[str] = mapped_column(String(32), default="document", nullable=False)
    indexable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="registered", nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class DocumentVersion(Base):
    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        Index("ix_document_version_sha256", "sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    modified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="registered", nullable=False)
    normalized_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DocumentRelation(Base):
    __tablename__ = "document_relation"
    __table_args__ = (
        UniqueConstraint(
            "source_document_id", "target_document_id", "relation_type",
            name="uq_document_relation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_document_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("document.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
