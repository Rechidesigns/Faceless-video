"""Database: Postgres in production (DATABASE_URL), SQLite file locally."""
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, create_engine, inspect, text
from sqlalchemy import true as sa_true
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from . import config


def _url() -> str:
    u = config.env("DATABASE_URL")
    if not u:
        return f"sqlite:///{(config.DATA / 'app.db').as_posix()}"
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    if u.startswith("postgresql://"):
        u = "postgresql+psycopg://" + u[len("postgresql://"):]
    return u


URL = _url()
IS_SQLITE = URL.startswith("sqlite")
engine = create_engine(
    URL, pool_pre_ping=True,
    **({"connect_args": {"check_same_thread": False, "timeout": 30}} if IS_SQLITE
       else {"pool_size": 5, "max_overflow": 5}),
)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, server_default=sa_true())  # admin approval
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # for OTP verification later
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = site default
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class AuthSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    expires_at: Mapped[float] = mapped_column(Float)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True)
    platform: Mapped[str] = mapped_column(String(20), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    data: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class Video(Base):
    __tablename__ = "videos"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, index=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    blob_key: Mapped[str | None] = mapped_column(Text, nullable=True)   # object key in the bucket
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    data: Mapped[dict] = mapped_column(JSON)                            # full job record (script, log, publish…)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


# Columns added after the first release: (table, column, SQL type + default). Existing rows get the default.
_ADDED_COLUMNS = [
    ("users", "is_approved", "BOOLEAN NOT NULL DEFAULT TRUE"),
]


def init() -> None:
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, col, ddl in _ADDED_COLUMNS:
            if col not in {c["name"] for c in insp.get_columns(table)}:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))


@contextmanager
def session():
    with Session() as s:
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
