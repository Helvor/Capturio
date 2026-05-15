import uuid
from datetime import datetime
from sqlalchemy import String, Text, Boolean, Integer, DateTime, BigInteger
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class Photo(Base):
    __tablename__ = "photos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    filepath: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False)
    download_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exif_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    exif_camera: Mapped[str | None] = mapped_column(String(256), nullable=True)
    exif_lens: Mapped[str | None] = mapped_column(String(256), nullable=True)
    exif_focal_length: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exif_shutter_speed: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exif_aperture: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exif_iso: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exif_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exif_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
