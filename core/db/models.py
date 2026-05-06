from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""


def _utcnow() -> datetime:
    return datetime.utcnow()


class ProfileRow(Base):
    __tablename__ = 'profiles'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default='', index=True)
    email: Mapped[str] = mapped_column(String(255), default='', index=True)
    region: Mapped[str] = mapped_column(String(16), default='ANY', index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None)
    created_by_user_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    created_by_username: Mapped[str] = mapped_column(String(128), default='')
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class JobRow(Base):
    __tablename__ = 'jobs'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    company: Mapped[str] = mapped_column(String(255), default='', index=True)
    job_title: Mapped[str] = mapped_column(String(255), default='', index=True)
    region: Mapped[str] = mapped_column(String(16), default='ANY', index=True)
    status: Mapped[str] = mapped_column(String(32), default='approved', index=True)
    normalized_url: Mapped[str] = mapped_column(String(2048), default='', index=True)
    company_key: Mapped[str] = mapped_column(String(255), default='', index=True)
    title_key: Mapped[str] = mapped_column(String(255), default='', index=True)
    scrape_status: Mapped[str] = mapped_column(String(32), default='done', index=True)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None, index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class GeneratedResumeRow(Base):
    __tablename__ = 'generated_resumes'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    profile_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    job_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    job_company: Mapped[str] = mapped_column(String(255), default='', index=True)
    job_title: Mapped[str] = mapped_column(String(255), default='', index=True)
    created_by_user_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    created_by_username: Mapped[str] = mapped_column(String(128), default='')
    created_at_ts: Mapped[Optional[datetime]] = mapped_column(DateTime, default=None, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class UserRow(Base):
    __tablename__ = 'users'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default='', index=True)
    email: Mapped[str] = mapped_column(String(255), default='', index=True)
    status: Mapped[str] = mapped_column(String(32), default='pending', index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_admin_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class TemplateRow(Base):
    __tablename__ = 'templates'

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default='')
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SettingsRow(Base):
    __tablename__ = 'settings'

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OpenAICallRow(Base):
    __tablename__ = 'openai_calls'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), default='', index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
