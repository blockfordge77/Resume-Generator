"""One-shot migration of legacy JSON data into the SQLite DB.

Triggered automatically by Storage on first run when the DB is empty
and at least one of the legacy JSON files exists.
"""
from __future__ import annotations

import json
from datetime import datetime
from json import JSONDecodeError, JSONDecoder
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    GeneratedResumeRow,
    JobRow,
    OpenAICallRow,
    ProfileRow,
    SettingsRow,
    TemplateRow,
    UserRow,
)


_LEGACY_FILES = (
    'profiles.json',
    'jobs.json',
    'generated_resumes.json',
    'users.json',
    'templates.json',
    'settings.json',
    'openai_calls.json',
)


def has_legacy_data(data_dir: Path) -> bool:
    return any((data_dir / name).exists() for name in _LEGACY_FILES)


def db_is_empty(session: Session) -> bool:
    for model in (ProfileRow, JobRow, GeneratedResumeRow, UserRow, TemplateRow, SettingsRow):
        if session.scalar(select(model).limit(1)) is not None:
            return False
    return True


def parse_iso(value: str) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except JSONDecodeError:
        return _recover_json(text)


def _recover_json(text: str) -> Any:
    decoder = JSONDecoder()
    values: list[Any] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        try:
            value, next_index = decoder.raw_decode(text, index)
        except JSONDecodeError:
            break
        values.append(value)
        index = next_index
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    merged_list: list[Any] = []
    for value in values:
        if isinstance(value, list):
            merged_list.extend(value)
        elif isinstance(value, dict):
            merged_list.append(value)
    return merged_list or values[-1]


def migrate(
    session: Session,
    data_dir: Path,
    *,
    normalize_profile: Callable[[dict], dict],
    normalize_job: Callable[[dict], dict],
    normalize_user: Callable[[dict], dict],
    normalize_template: Callable[[dict], dict],
    normalize_generated: Callable[[dict], dict],
    normalize_settings: Callable[[dict], dict],
    job_compare_key: Callable[[str], str],
    normalize_url: Callable[[str], str],
) -> dict[str, int]:
    """Import legacy JSON data into the DB. Returns counts per table."""
    counts = {'profiles': 0, 'jobs': 0, 'generated_resumes': 0, 'users': 0, 'templates': 0, 'settings': 0, 'openai_calls': 0}

    profiles_raw = _read_json(data_dir / 'profiles.json') or []
    if isinstance(profiles_raw, list):
        for raw in profiles_raw:
            if not isinstance(raw, dict):
                continue
            payload = normalize_profile(raw)
            pid = str(payload.get('id', '')).strip()
            if not pid:
                continue
            session.merge(ProfileRow(
                id=pid,
                name=str(payload.get('name', '')).strip(),
                email=str(payload.get('email', '')).strip(),
                region=payload.get('region', 'ANY'),
                active=True,
                deleted_at=None,
                created_by_user_id='',
                created_by_username='',
                data=payload,
            ))
            counts['profiles'] += 1

    jobs_raw = _read_json(data_dir / 'jobs.json') or []
    if isinstance(jobs_raw, list):
        for raw in jobs_raw:
            if not isinstance(raw, dict):
                continue
            payload = normalize_job(raw)
            jid = str(payload.get('id', '')).strip()
            if not jid:
                continue
            session.merge(JobRow(
                id=jid,
                company=str(payload.get('company', '')).strip(),
                job_title=str(payload.get('job_title', '')).strip(),
                region=payload.get('region', 'ANY'),
                status=payload.get('status', 'approved'),
                normalized_url=normalize_url(payload.get('link', '')),
                company_key=job_compare_key(payload.get('company', '')),
                title_key=job_compare_key(payload.get('job_title', '')),
                scrape_status=payload.get('scrape_status', 'done'),
                submitted_at=parse_iso(payload.get('submitted_at', '')),
                created_by_user_id=str(payload.get('created_by_user_id', '')).strip(),
                data=payload,
            ))
            counts['jobs'] += 1

    generated_raw = _read_json(data_dir / 'generated_resumes.json') or []
    if isinstance(generated_raw, list):
        for raw in generated_raw:
            if not isinstance(raw, dict):
                continue
            payload = normalize_generated(raw)
            sid = str(payload.get('saved_resume_id', '')).strip()
            if not sid:
                continue
            session.merge(GeneratedResumeRow(
                id=sid,
                profile_id=str(payload.get('profile_id', '')).strip(),
                job_id=str(payload.get('job_id', '')).strip(),
                job_company=str(payload.get('job_company', '')).strip(),
                job_title=str(payload.get('job_title', '')).strip(),
                created_by_user_id=str(payload.get('created_by_user_id', '')).strip(),
                created_by_username=str(payload.get('created_by_username', '')).strip(),
                created_at_ts=parse_iso(payload.get('created_at', '')),
                data=payload,
            ))
            counts['generated_resumes'] += 1

    users_raw = _read_json(data_dir / 'users.json') or []
    if isinstance(users_raw, list):
        seen_usernames: set[str] = set()
        for raw in users_raw:
            if not isinstance(raw, dict):
                continue
            payload = normalize_user(raw)
            uid = str(payload.get('id', '')).strip()
            uname = str(payload.get('username', '')).strip().lower()
            if not uid or not uname or uname in seen_usernames:
                continue
            seen_usernames.add(uname)
            session.merge(UserRow(
                id=uid,
                username=uname,
                full_name=str(payload.get('full_name', '')).strip(),
                email=str(payload.get('email', '')).strip(),
                status=str(payload.get('status', 'pending')).strip(),
                is_admin=bool(payload.get('is_admin', False)),
                data=payload,
            ))
            counts['users'] += 1

    templates_raw = _read_json(data_dir / 'templates.json') or []
    if isinstance(templates_raw, list):
        for raw in templates_raw:
            if not isinstance(raw, dict):
                continue
            payload = normalize_template(raw)
            tid = str(payload.get('id', '')).strip()
            if not tid:
                continue
            session.merge(TemplateRow(
                id=tid,
                name=str(payload.get('name', '')).strip(),
                data=payload,
            ))
            counts['templates'] += 1

    settings_raw = _read_json(data_dir / 'settings.json')
    if isinstance(settings_raw, dict):
        normalized_settings = normalize_settings(settings_raw)
        session.merge(SettingsRow(key='app', data=normalized_settings))
        counts['settings'] += 1

    openai_raw = _read_json(data_dir / 'openai_calls.json') or []
    if isinstance(openai_raw, list):
        for raw in openai_raw:
            if not isinstance(raw, dict):
                continue
            session.add(OpenAICallRow(
                user_id=str(raw.get('user_id', '')).strip(),
                recorded_at=parse_iso(raw.get('recorded_at', '')) or datetime.utcnow(),
                data=raw,
            ))
            counts['openai_calls'] += 1

    return counts
