"""Bookings table (Postgres in real life, SQLite in tests)."""
import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Status(enum.StrEnum):
    PENDING = "PENDING"        # saved, seats not confirmed yet
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"          # seats never taken (or given back)
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


def now():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    event_id: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    def to_dict(self):
        return {"id": self.id, "user_id": self.user_id, "event_id": self.event_id,
                "quantity": self.quantity, "status": self.status,
                "created_at": self.created_at.isoformat() if self.created_at else None}


def make_session_factory(url):
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["connect_args"] = {"connect_timeout": 2}
    engine = create_engine(url, **kwargs)
    return engine, sessionmaker(engine, expire_on_commit=False)
