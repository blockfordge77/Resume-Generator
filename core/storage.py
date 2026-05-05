"""Persistent storage layer.

Uses SQLite (via SQLAlchemy 2.x) under the hood. Each entity is stored as
one row keyed by id, with denormalized index columns for common queries
plus a JSON ``data`` column holding the full normalized record. This keeps
the public API identical to the previous JSON-backed implementation while
giving us indexed lookups, atomic writes, and proper transactions.

Legacy ``data/*.json`` files are imported automatically on the first run
that finds an empty database. The JSON files are left on disk untouched —
they can be deleted later once the migration is verified.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import session_scope
from .db import migration as _migration
from .db.models import (
    GeneratedResumeRow,
    JobRow,
    OpenAICallRow,
    ProfileRow,
    SettingsRow,
    TemplateRow,
    UserRow,
)


_ALLOWED_REGIONS = {'ANY', 'US', 'EU', 'LATAM'}
OPENAI_MODEL_OPTIONS = ['gpt-5-nano', 'gpt-5.1', 'gpt-5-mini']
DEFAULT_OPENAI_MODEL = 'gpt-5-nano'

_URL_TRACKING_PARAMS = frozenset({
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'ref', 'source', 'src', 'referral', 'referer', 'fbclid', 'gclid',
    'msclkid', 'yclid', 'mc_eid', 'trk', 'trkCampaign', 'sc_campaign',
    'icid', 'cid', 'sid', 'lid', 'pid', 'aid', 'eid', 'iid', 'rid',
})


def _normalize_market_region(value: str) -> str:
    raw = str(value or '').strip().upper()
    if not raw or raw in {'ALL', 'GLOBAL', 'ANYWHERE', 'REMOTE'}:
        return 'ANY'
    return raw if raw in _ALLOWED_REGIONS else raw


def build_password_record(password: str) -> dict[str, str]:
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), salt.encode('utf-8'), 200_000).hex()
    return {'password_salt': salt, 'password_hash': hashed}


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not salt or not expected_hash:
        return False
    candidate = hashlib.pbkdf2_hmac('sha256', str(password).encode('utf-8'), str(salt).encode('utf-8'), 200_000).hex()
    return secrets.compare_digest(candidate, str(expected_hash))


def _hash_auth_token(token: str) -> str:
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _job_compare_key(value: str) -> str:
    return ''.join(ch.lower() if ch.isalnum() else ' ' for ch in str(value or '')).strip()


def normalize_job_url(url: str) -> str:
    """Return a canonical form of a job URL with tracking params stripped."""
    raw = str(url or '').strip()
    if not raw:
        return ''
    try:
        from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
        parsed = urlparse(raw)
        clean_params = [(k, v) for k, v in parse_qsl(parsed.query) if k.lower() not in _URL_TRACKING_PARAMS]
        netloc = parsed.netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        normalized = urlunparse((
            parsed.scheme.lower(),
            netloc,
            parsed.path.rstrip('/'),
            parsed.params,
            urlencode(clean_params),
            '',
        ))
        return normalized
    except Exception:
        return raw


def _parse_iso_datetime(value: str) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None)
    except Exception:
        return None


# ---------- defaults ----------

def _template_defaults() -> dict:
    return {
        'font_family': 'Arial, sans-serif',
        'accent_color': '#1f4e79',
        'text_color': '#111827',
        'muted_color': '#4b5563',
        'background_color': '#ffffff',
        'section_order': ['summary', 'technical_skills', 'work_history', 'education_history'],
        'custom_css': '',
        'layout_style': 'ats_classic',
        'header_style': 'rule',
        'skill_style': 'grouped_bullets',
        'density': 'normal',
        'show_role_headline': True,
    }


def _default_templates() -> list[dict]:
    return [
        {**_template_defaults(), 'id': 'template_ats_classic', 'name': 'ATS Classic'},
        {
            **_template_defaults(),
            'id': 'template_ats_compact',
            'name': 'ATS Compact',
            'accent_color': '#0f172a',
            'muted_color': '#475569',
            'section_order': ['technical_skills', 'summary', 'work_history', 'education_history'],
            'layout_style': 'ats_compact',
            'header_style': 'minimal',
            'density': 'tight',
        },
        {
            **_template_defaults(),
            'id': 'template_ats_technical',
            'name': 'ATS Technical',
            'accent_color': '#1d4ed8',
            'muted_color': '#6b7280',
            'layout_style': 'ats_technical',
        },
    ]


def _default_settings() -> dict:
    return {
        'default_prompt': '',
        'always_clean_generation': True,
        'download_output_dir': 'saved_resumes',
        'openai_model': DEFAULT_OPENAI_MODEL,
        'saved_prompts': [],
    }


def _default_admin_user() -> dict:
    pw = build_password_record('admin123')
    now = datetime.utcnow().isoformat() + 'Z'
    return {
        'id': 'user_admin_default',
        'username': 'admin',
        'full_name': 'Administrator',
        'email': '',
        'password_hash': pw['password_hash'],
        'password_salt': pw['password_salt'],
        'is_admin': True,
        'status': 'approved',
        'assigned_profile_ids': [],
        'created_at': now,
        'approved_at': now,
        'approved_by_user_id': 'system',
        'force_password_change': True,
    }


# ---------- normalizers ----------

def _normalize_uploaded_resume(item: Any) -> dict:
    source = item if isinstance(item, dict) else {}
    path = str(source.get('path', source.get('storage_path', ''))).strip()
    relative_path = str(source.get('relative_path', '')).strip()
    if not path and not relative_path:
        return {}
    return {
        'filename': str(source.get('filename', '')).strip(),
        'content_type': str(source.get('content_type', '')).strip(),
        'size_bytes': int(source.get('size_bytes', 0) or 0),
        'path': path,
        'relative_path': relative_path,
        'uploaded_at': str(source.get('uploaded_at', '')).strip(),
        'extracted_text': str(source.get('extracted_text', '') or ''),
    }


def _normalize_profile(item: dict) -> dict:
    work_history = []
    for raw_job in item.get('work_history', []) or []:
        work_history.append({
            'company_name': raw_job.get('company_name', ''),
            'duration': raw_job.get('duration', ''),
            'location': raw_job.get('location', ''),
            'bullets': [str(b).strip() for b in raw_job.get('bullets', []) if str(b).strip()],
            'legacy_role': raw_job.get('legacy_role', raw_job.get('role', '')),
        })
    return {
        'id': item.get('id', ''),
        'name': item.get('name', ''),
        'email': item.get('email', ''),
        'phone': item.get('phone', ''),
        'location': item.get('location', ''),
        'linkedin': item.get('linkedin', ''),
        'portfolio': item.get('portfolio', ''),
        'default_template_id': str(item.get('default_template_id', '')).strip(),
        'summary_seed': item.get('summary_seed', ''),
        'uploaded_resume': _normalize_uploaded_resume(item.get('uploaded_resume', {})),
        'technical_skills': [str(s).strip() for s in item.get('technical_skills', []) if str(s).strip()],
        'region': _normalize_market_region(item.get('region', item.get('market_region', ''))),
        'active': bool(item.get('active', True)),
        'created_by_user_id': str(item.get('created_by_user_id', '')).strip(),
        'created_by_username': str(item.get('created_by_username', '')).strip(),
        'work_history': work_history,
        'education_history': [
            {
                'university': edu.get('university', ''),
                'degree': edu.get('degree', ''),
                'duration': edu.get('duration', ''),
                'location': edu.get('location', ''),
            }
            for edu in item.get('education_history', []) or []
        ],
    }


def _normalize_template(item: dict, *, fallback_index: int = 0) -> dict:
    defaults = _template_defaults()
    merged = defaults | item
    merged['section_order'] = item.get('section_order', defaults['section_order'])
    if merged.get('skill_style') == 'grouped':
        merged['skill_style'] = 'grouped_bullets'
    merged['id'] = str(merged.get('id') or '').strip() or f'template_{uuid.uuid4().hex[:10]}'
    merged['name'] = str(merged.get('name', '')).strip() or f'Template {fallback_index + 1}'
    return merged


def _normalize_user(item: dict) -> dict:
    return {
        'id': item.get('id') or f'user_{uuid.uuid4().hex[:10]}',
        'username': str(item.get('username', '')).strip().lower(),
        'full_name': str(item.get('full_name', '')).strip(),
        'email': str(item.get('email', '')).strip(),
        'password_hash': str(item.get('password_hash', '')).strip(),
        'password_salt': str(item.get('password_salt', '')).strip(),
        'is_admin': bool(item.get('is_admin', False)),
        'status': str(item.get('status', 'pending') or 'pending').strip(),
        'assigned_profile_ids': [str(v).strip() for v in item.get('assigned_profile_ids', []) if str(v).strip()],
        'created_at': str(item.get('created_at', '')),
        'approved_at': str(item.get('approved_at', '')),
        'approved_by_user_id': str(item.get('approved_by_user_id', '')).strip(),
        'force_password_change': bool(item.get('force_password_change', False)),
        'auth_tokens': _normalize_auth_tokens(item.get('auth_tokens', []) or []),
    }


def _normalize_auth_tokens(items: Any) -> list[dict]:
    normalized: list[dict] = []
    now = datetime.utcnow()
    for item in items or []:
        token_hash = str((item or {}).get('token_hash', '')).strip()
        if not token_hash:
            continue
        expires_at = str((item or {}).get('expires_at', '')).strip()
        revoked_at = str((item or {}).get('revoked_at', '')).strip()
        expires_dt = _parse_iso_datetime(expires_at)
        revoked_dt = _parse_iso_datetime(revoked_at)
        if revoked_dt:
            continue
        if expires_dt and expires_dt < now:
            continue
        normalized.append({
            'token_id': str((item or {}).get('token_id', '')).strip() or f'tok_{uuid.uuid4().hex[:10]}',
            'token_hash': token_hash,
            'created_at': str((item or {}).get('created_at', '')).strip(),
            'expires_at': expires_at,
            'last_seen_at': str((item or {}).get('last_seen_at', '')).strip(),
            'revoked_at': '',
        })
    normalized.sort(key=lambda entry: entry.get('created_at', ''), reverse=True)
    return normalized[:10]


def _normalize_job(item: dict) -> dict:
    return {
        'id': item.get('id') or f'job_{uuid.uuid4().hex[:10]}',
        'company': str(item.get('company', '')).strip(),
        'job_title': str(item.get('job_title', '')).strip(),
        'description': str(item.get('description', '')).strip(),
        'link': str(item.get('link', '')).strip(),
        'region': _normalize_market_region(item.get('region', item.get('market_region', ''))),
        'note': str(item.get('note', '')).strip(),
        'status': str(item.get('status', 'pending') or 'pending').strip(),
        'source': str(item.get('source', 'manual') or 'manual').strip(),
        'scrape_status': str(item.get('scrape_status', 'done' if item.get('description') else 'queued') or 'queued').strip(),
        'scrape_error': str(item.get('scrape_error', '')).strip(),
        'created_by_user_id': str(item.get('created_by_user_id', '')).strip(),
        'created_by_username': str(item.get('created_by_username', '')).strip(),
        'submitted_at': str(item.get('submitted_at', '')).strip(),
        'approved_at': str(item.get('approved_at', '')).strip(),
        'approved_by_user_id': str(item.get('approved_by_user_id', '')).strip(),
        'approved_by_username': str(item.get('approved_by_username', '')).strip(),
        'scrape_started_at': str(item.get('scrape_started_at', '')).strip(),
        'scraped_at': str(item.get('scraped_at', '')).strip(),
        'reports': _normalize_job_reports(item.get('reports', []) or []),
        'flagged': bool(item.get('flagged', False)),
        'admin_applied': bool(item.get('admin_applied', False)),
        'admin_applied_at': str(item.get('admin_applied_at', '')).strip(),
        'admin_applied_by_user_id': str(item.get('admin_applied_by_user_id', '')).strip(),
        'admin_applied_by_username': str(item.get('admin_applied_by_username', '')).strip(),
    }


def _normalize_job_reports(items: Any) -> list[dict]:
    normalized: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        reason = str(item.get('reason', '')).strip()
        if not reason:
            continue
        normalized.append({
            'reason': reason,
            'reported_by_user_id': str(item.get('reported_by_user_id', '')).strip(),
            'reported_by_username': str(item.get('reported_by_username', '')).strip(),
            'reported_at': str(item.get('reported_at', '')).strip(),
            'source': str(item.get('source', 'user') or 'user').strip(),
        })
    return normalized


def _normalize_generated_resume(item: dict) -> dict:
    resume = item.get('resume', {}) if isinstance(item.get('resume', {}), dict) else {}
    normalized_groups: list[dict] = []
    raw_groups = resume.get('skill_groups')
    if isinstance(raw_groups, list):
        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            category = str(group.get('category', '')).strip() or 'Other Relevant'
            items_clean = [str(v).strip() for v in group.get('items', []) or [] if str(v).strip()]
            if items_clean:
                normalized_groups.append({'category': category, 'items': items_clean})
    elif isinstance(raw_groups, dict):
        for key, values in raw_groups.items():
            items_clean = [str(v).strip() for v in values or [] if str(v).strip()]
            if items_clean:
                normalized_groups.append({'category': str(key).strip() or 'Other Relevant', 'items': items_clean})
    if not normalized_groups:
        legacy_grouped = resume.get('grouped_skills')
        if isinstance(legacy_grouped, dict):
            for key, values in legacy_grouped.items():
                items_clean = [str(v).strip() for v in values or [] if str(v).strip()]
                if items_clean:
                    normalized_groups.append({'category': str(key).strip() or 'Other Relevant', 'items': items_clean})
    normalized_resume = {
        'name': str(resume.get('name', '')).strip(),
        'headline': str(resume.get('headline', '')).strip(),
        'summary': str(resume.get('summary', '')).strip(),
        'fit_keywords': [str(v).strip() for v in resume.get('fit_keywords', []) if str(v).strip()],
        'technical_skills': [str(v).strip() for v in resume.get('technical_skills', []) if str(v).strip()],
        'skill_groups': normalized_groups,
        'bold_keywords': [str(v).strip() for v in resume.get('bold_keywords', []) if str(v).strip()],
        'auto_bold_fit_keywords': bool(resume.get('auto_bold_fit_keywords', False)),
        'work_history': [
            {
                'company_name': str(work.get('company_name', '')).strip(),
                'duration': str(work.get('duration', '')).strip(),
                'location': str(work.get('location', '')).strip(),
                'role_title': str(work.get('role_title', work.get('role', ''))).strip(),
                'role_headline': str(work.get('role_headline', '')).strip(),
                'bullets': [str(v).strip() for v in work.get('bullets', []) if str(v).strip()],
            }
            for work in resume.get('work_history', []) or []
        ],
        'education_history': [
            {
                'university': str(edu.get('university', '')).strip(),
                'degree': str(edu.get('degree', '')).strip(),
                'duration': str(edu.get('duration', '')).strip(),
                'location': str(edu.get('location', '')).strip(),
            }
            for edu in resume.get('education_history', []) or []
        ],
    }
    interview_schedule = item.get('interview_schedule', {}) if isinstance(item.get('interview_schedule', {}), dict) else {}
    created_at = str(item.get('created_at', '')).strip()
    created_date = str(item.get('created_date', '')).strip() or (created_at[:10] if len(created_at) >= 10 else '')
    return {
        'saved_resume_id': str(item.get('saved_resume_id', '')).strip() or f'resume_{uuid.uuid4().hex[:10]}',
        'created_at': created_at,
        'created_date': created_date,
        'created_by_user_id': str(item.get('created_by_user_id', '')).strip(),
        'created_by_username': str(item.get('created_by_username', '')).strip(),
        'profile_id': str(item.get('profile_id', '')).strip(),
        'template_id': str(item.get('template_id', '')).strip(),
        'job_id': str(item.get('job_id', '')).strip(),
        'job_company': str(item.get('job_company', '')).strip(),
        'job_title': str(item.get('job_title', item.get('target_role', ''))).strip(),
        'job_link': str(item.get('job_link', '')).strip(),
        'job_description': str(item.get('job_description', '')).strip(),
        'job_region': _normalize_market_region(item.get('job_region', item.get('region', ''))),
        'target_role': str(item.get('target_role', item.get('job_title', ''))).strip(),
        'resume': normalized_resume,
        'answers': [
            {
                'question': str(a.get('question', '')).strip(),
                'answer': str(a.get('answer', '')).strip(),
                'note': str(a.get('note', '')).strip(),
            }
            for a in (item.get('answers', []) or []) if isinstance(a, dict)
        ],
        'ats_score': int(item.get('ats_score', 0) or 0),
        'download_filename': str(item.get('download_filename', '')).strip() or 'resume.pdf',
        'download_mode': str(item.get('download_mode', 'browser') or 'browser').strip(),
        'saved_pdf_path': str(item.get('saved_pdf_path', '')).strip(),
        'company_message': str(item.get('company_message', '')).strip(),
        'company_message_status': str(item.get('company_message_status', 'pending') or 'pending').strip(),
        'company_message_updated_at': str(item.get('company_message_updated_at', '')).strip(),
        'interview_schedule': {
            'interviewer_name': str(interview_schedule.get('interviewer_name', '')).strip(),
            'interview_time': str(interview_schedule.get('interview_time', '')).strip(),
            'meeting_link': str(interview_schedule.get('meeting_link', '')).strip(),
            'note': str(interview_schedule.get('note', '')).strip(),
            'submitted_at': str(interview_schedule.get('submitted_at', '')).strip(),
            'review_status': str(interview_schedule.get('review_status', 'not_submitted') or 'not_submitted').strip(),
            'reviewed_at': str(interview_schedule.get('reviewed_at', '')).strip(),
            'reviewed_by_user_id': str(interview_schedule.get('reviewed_by_user_id', '')).strip(),
            'reviewed_by_username': str(interview_schedule.get('reviewed_by_username', '')).strip(),
            'review_note': str(interview_schedule.get('review_note', '')).strip(),
        },
    }


def _normalize_settings(item: Any) -> dict:
    defaults = _default_settings()
    source = item if isinstance(item, dict) else {}
    merged = defaults | source
    merged['default_prompt'] = str(merged.get('default_prompt', '')).strip()
    merged['download_output_dir'] = str(merged.get('download_output_dir', 'saved_resumes')).strip() or 'saved_resumes'
    merged['always_clean_generation'] = True
    raw_model = str(merged.get('openai_model', '') or '').strip()
    merged['openai_model'] = raw_model if raw_model in OPENAI_MODEL_OPTIONS else DEFAULT_OPENAI_MODEL
    raw_prompts = merged.get('saved_prompts', [])
    normalized_prompts = []
    for p in raw_prompts if isinstance(raw_prompts, list) else []:
        if not isinstance(p, dict):
            continue
        name = str(p.get('name', '')).strip()
        text = str(p.get('text', '')).strip()
        if name:
            normalized_prompts.append({
                'id': str(p.get('id') or f'prompt_{uuid.uuid4().hex[:10]}'),
                'name': name,
                'text': text,
            })
    merged['saved_prompts'] = normalized_prompts
    return merged


def _normalize_openai_call(entry: dict) -> dict:
    usage = entry.get('usage', {}) if isinstance(entry.get('usage', {}), dict) else {}
    cost = entry.get('cost', {}) if isinstance(entry.get('cost', {}), dict) else {}
    return {
        'user_id': str(entry.get('user_id', '')).strip(),
        'kind': str(entry.get('kind', '')).strip(),
        'recorded_at': str(entry.get('recorded_at', '')).strip(),
        'flow_id': str(entry.get('flow_id', '')).strip(),
        'call_kind': str(entry.get('call_kind', '')).strip(),
        'status': str(entry.get('status', '')).strip() or 'success',
        'attempt': int(entry.get('attempt', 0) or 0),
        'model': str(entry.get('model', '')).strip(),
        'schema_name': str(entry.get('schema_name', '')).strip(),
        'response_id': str(entry.get('response_id', '')).strip(),
        'duration_ms': int(entry.get('duration_ms', 0) or 0),
        'developer_message_chars': int(entry.get('developer_message_chars', 0) or 0),
        'input_estimated_tokens_local': int(entry.get('input_estimated_tokens_local', 0) or 0),
        'output_text_chars': int(entry.get('output_text_chars', 0) or 0),
        'output_estimated_tokens_local': int(entry.get('output_estimated_tokens_local', 0) or 0),
        'output_text': str(entry.get('output_text', '') or ''),
        'output_text_truncated': bool(entry.get('output_text_truncated', False)),
        'output_pretty': str(entry.get('output_pretty', '') or ''),
        'output_pretty_truncated': bool(entry.get('output_pretty_truncated', False)),
        'usage': {
            'input_tokens': int(usage.get('input_tokens', 0) or 0),
            'output_tokens': int(usage.get('output_tokens', 0) or 0),
            'total_tokens': int(usage.get('total_tokens', 0) or 0),
            'cached_input_tokens': int(usage.get('cached_input_tokens', 0) or 0),
            'reasoning_output_tokens': int(usage.get('reasoning_output_tokens', 0) or 0),
            'billable_input_tokens': int(usage.get('billable_input_tokens', 0) or 0),
        },
        'cost': {
            'input_cost_usd': cost.get('input_cost_usd'),
            'cached_input_cost_usd': cost.get('cached_input_cost_usd'),
            'output_cost_usd': cost.get('output_cost_usd'),
            'reasoning_output_cost_usd': cost.get('reasoning_output_cost_usd'),
            'total_cost_usd': cost.get('total_cost_usd'),
            'pricing_source': cost.get('pricing_source', {}) if isinstance(cost.get('pricing_source', {}), dict) else {},
        },
        'payload_summary': entry.get('payload_summary', {}) if isinstance(entry.get('payload_summary', {}), dict) else {},
        'error': str(entry.get('error', '')).strip(),
        'post_validation': entry.get('post_validation', {}) if isinstance(entry.get('post_validation', {}), dict) else {},
    }


# ---------- Storage ----------

class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'app.db'
        self._lock = threading.RLock()
        self._bootstrap()

    # ---- bootstrap ----

    def _bootstrap(self) -> None:
        with session_scope(self.db_path) as session:
            if _migration.db_is_empty(session) and _migration.has_legacy_data(self.data_dir):
                _migration.migrate(
                    session,
                    self.data_dir,
                    normalize_profile=_normalize_profile,
                    normalize_job=_normalize_job,
                    normalize_user=_normalize_user,
                    normalize_template=_normalize_template,
                    normalize_generated=_normalize_generated_resume,
                    normalize_settings=_normalize_settings,
                    job_compare_key=_job_compare_key,
                    normalize_url=normalize_job_url,
                )
            self._ensure_default_templates(session)
            self._ensure_default_settings(session)
            self._ensure_default_admin(session)

    @staticmethod
    def _ensure_default_templates(session: Session) -> None:
        if session.scalar(select(TemplateRow).limit(1)) is not None:
            return
        for index, raw in enumerate(_default_templates()):
            payload = _normalize_template(raw, fallback_index=index)
            session.merge(TemplateRow(id=payload['id'], name=payload['name'], data=payload))

    @staticmethod
    def _ensure_default_settings(session: Session) -> None:
        existing = session.get(SettingsRow, 'app')
        if existing is None:
            session.merge(SettingsRow(key='app', data=_normalize_settings({})))

    @staticmethod
    def _ensure_default_admin(session: Session) -> None:
        admin_exists = session.scalar(
            select(UserRow.id).where(UserRow.is_admin.is_(True), UserRow.status == 'approved').limit(1)
        )
        if admin_exists is not None:
            return
        admin = _normalize_user(_default_admin_user())
        session.merge(UserRow(
            id=admin['id'],
            username=admin['username'],
            email=admin['email'],
            status=admin['status'],
            is_admin=True,
            data=admin,
        ))

    @staticmethod
    def make_id(prefix: str) -> str:
        return f'{prefix}_{uuid.uuid4().hex[:10]}'

    # ---- profiles ----

    def get_profiles(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.scalars(
                select(ProfileRow).where(ProfileRow.deleted_at.is_(None)).order_by(ProfileRow.name)
            ).all()
            return [self._profile_row_to_dict(row) for row in rows]

    def get_profile_by_id(self, profile_id: str) -> dict | None:
        pid = str(profile_id or '').strip()
        if not pid:
            return None
        with session_scope(self.db_path) as session:
            row = session.get(ProfileRow, pid)
            if row is None or row.deleted_at is not None:
                return None
            return self._profile_row_to_dict(row)

    def upsert_profile(self, payload: dict) -> None:
        normalized = _normalize_profile(payload)
        if not normalized.get('id'):
            normalized['id'] = self.make_id('profile')
        with session_scope(self.db_path) as session:
            existing = session.get(ProfileRow, normalized['id'])
            # Preserve original creator on update
            if existing is not None:
                if existing.created_by_user_id and not normalized.get('created_by_user_id'):
                    normalized['created_by_user_id'] = existing.created_by_user_id
                if existing.created_by_username and not normalized.get('created_by_username'):
                    normalized['created_by_username'] = existing.created_by_username
            session.merge(ProfileRow(
                id=normalized['id'],
                name=normalized.get('name', ''),
                email=normalized.get('email', ''),
                region=normalized['region'],
                active=bool(normalized.get('active', True)),
                deleted_at=None,
                created_by_user_id=normalized.get('created_by_user_id', ''),
                created_by_username=normalized.get('created_by_username', ''),
                data=normalized,
            ))

    def delete_profile(self, profile_id: str) -> None:
        pid = str(profile_id or '').strip()
        if not pid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(ProfileRow, pid)
            if row is not None:
                session.delete(row)

    @staticmethod
    def _profile_row_to_dict(row: ProfileRow) -> dict:
        data = dict(row.data or {})
        data['id'] = row.id
        data['active'] = bool(row.active)
        # Columns are the source of truth; let them win over JSON if they diverge
        if row.name:
            data['name'] = row.name
        if row.email:
            data['email'] = row.email
        if row.created_by_user_id:
            data['created_by_user_id'] = row.created_by_user_id
        if row.created_by_username:
            data['created_by_username'] = row.created_by_username
        return _normalize_profile(data)

    # ---- templates ----

    def get_templates(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.scalars(select(TemplateRow).order_by(TemplateRow.id)).all()
            return [_normalize_template(dict(row.data or {}) | {'id': row.id, 'name': row.name}, fallback_index=i)
                    for i, row in enumerate(rows)]

    def get_template_by_id(self, template_id: str) -> dict | None:
        tid = str(template_id or '').strip()
        if not tid:
            return None
        with session_scope(self.db_path) as session:
            row = session.get(TemplateRow, tid)
            if row is None:
                return None
            return _normalize_template(dict(row.data or {}) | {'id': row.id, 'name': row.name})

    def upsert_template(self, payload: dict) -> None:
        normalized = _normalize_template(payload)
        with session_scope(self.db_path) as session:
            session.merge(TemplateRow(id=normalized['id'], name=normalized['name'], data=normalized))

    def delete_template(self, template_id: str) -> None:
        tid = str(template_id or '').strip()
        if not tid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(TemplateRow, tid)
            if row is not None:
                session.delete(row)
            for prof in session.scalars(select(ProfileRow)).all():
                data = dict(prof.data or {})
                if str(data.get('default_template_id', '')).strip() == tid:
                    data['default_template_id'] = ''
                    prof.data = data

    # ---- generated resumes ----

    def get_generated_resumes(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.scalars(select(GeneratedResumeRow).order_by(GeneratedResumeRow.created_at_ts)).all()
            return [_normalize_generated_resume(dict(row.data or {})) for row in rows]

    def save_generated_resume(self, payload: dict) -> None:
        normalized = _normalize_generated_resume(payload)
        with session_scope(self.db_path) as session:
            session.merge(GeneratedResumeRow(
                id=normalized['saved_resume_id'],
                profile_id=normalized['profile_id'],
                job_id=normalized['job_id'],
                job_company=normalized.get('job_company', ''),
                job_title=normalized.get('job_title', ''),
                created_by_user_id=normalized['created_by_user_id'],
                created_by_username=normalized.get('created_by_username', ''),
                created_at_ts=_parse_iso_datetime(normalized['created_at']),
                data=normalized,
            ))

    def update_generated_resume(self, saved_resume_id: str, patch: dict) -> None:
        sid = str(saved_resume_id or '').strip()
        if not sid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(GeneratedResumeRow, sid)
            if row is None:
                return
            normalized = _normalize_generated_resume(dict(row.data or {}) | (patch or {}))
            row.data = normalized
            row.profile_id = normalized['profile_id']
            row.job_id = normalized['job_id']
            row.job_company = normalized.get('job_company', '')
            row.job_title = normalized.get('job_title', '')
            row.created_by_user_id = normalized['created_by_user_id']
            row.created_by_username = normalized.get('created_by_username', '')
            row.created_at_ts = _parse_iso_datetime(normalized['created_at'])

    # ---- settings ----

    def get_app_settings(self) -> dict:
        with session_scope(self.db_path) as session:
            row = session.get(SettingsRow, 'app')
            return _normalize_settings(dict(row.data or {}) if row else {})

    def save_app_settings(self, payload: dict) -> None:
        normalized = _normalize_settings(payload)
        with session_scope(self.db_path) as session:
            session.merge(SettingsRow(key='app', data=normalized))

    # ---- users ----

    def get_users(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.scalars(select(UserRow).order_by(UserRow.username)).all()
            return [_normalize_user(dict(row.data or {})) for row in rows]

    def get_user_by_id(self, user_id: str) -> dict | None:
        uid = str(user_id or '').strip()
        if not uid:
            return None
        with session_scope(self.db_path) as session:
            row = session.get(UserRow, uid)
            return _normalize_user(dict(row.data or {})) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        needle = str(username or '').strip().lower()
        if not needle:
            return None
        with session_scope(self.db_path) as session:
            row = session.scalar(select(UserRow).where(UserRow.username == needle))
            return _normalize_user(dict(row.data or {})) if row else None

    def upsert_user(self, payload: dict) -> None:
        normalized = _normalize_user(payload)
        if not normalized.get('username'):
            return
        with session_scope(self.db_path) as session:
            session.merge(UserRow(
                id=normalized['id'],
                username=normalized['username'],
                full_name=normalized['full_name'],
                email=normalized['email'],
                status=normalized['status'],
                is_admin=normalized['is_admin'],
                data=normalized,
            ))

    def update_user(self, user_id: str, patch: dict) -> None:
        uid = str(user_id or '').strip()
        if not uid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(UserRow, uid)
            if row is None:
                return
            normalized = _normalize_user(dict(row.data or {}) | (patch or {}))
            row.data = normalized
            row.full_name = normalized['full_name']
            row.email = normalized['email']
            row.status = normalized['status']
            row.is_admin = normalized['is_admin']

    def delete_user(self, user_id: str) -> None:
        uid = str(user_id or '').strip()
        if not uid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(UserRow, uid)
            if row is not None:
                session.delete(row)

    # ---- auth tokens ----

    def issue_auth_token(self, user_id: str, ttl_days: int = 30) -> str:
        raw_token = secrets.token_urlsafe(32)
        now = datetime.utcnow()
        token_record = {
            'token_id': self.make_id('tok'),
            'token_hash': _hash_auth_token(raw_token),
            'created_at': now.isoformat() + 'Z',
            'expires_at': (now + timedelta(days=max(1, int(ttl_days or 30)))).isoformat() + 'Z',
            'last_seen_at': now.isoformat() + 'Z',
            'revoked_at': '',
        }
        with session_scope(self.db_path) as session:
            row = session.get(UserRow, str(user_id or '').strip())
            if row is None:
                return raw_token
            data = dict(row.data or {})
            tokens = _normalize_auth_tokens(data.get('auth_tokens', []) or [])
            tokens.insert(0, token_record)
            data['auth_tokens'] = tokens[:10]
            row.data = _normalize_user(data)
        return raw_token

    def get_user_by_auth_token(self, raw_token: str) -> dict | None:
        token_hash = _hash_auth_token(raw_token)
        now = datetime.utcnow()
        matched_user_id: str | None = None
        with session_scope(self.db_path) as session:
            for row in session.scalars(select(UserRow)).all():
                data = dict(row.data or {})
                tokens = data.get('auth_tokens', []) or []
                refreshed: list[dict] = []
                changed = False
                for token in tokens:
                    expires_at = _parse_iso_datetime(token.get('expires_at', ''))
                    revoked_at = _parse_iso_datetime(token.get('revoked_at', ''))
                    if revoked_at or (expires_at and expires_at < now):
                        changed = True
                        continue
                    if token.get('token_hash') == token_hash:
                        token = token | {'last_seen_at': now.isoformat() + 'Z'}
                        matched_user_id = row.id
                        changed = True
                    refreshed.append(token)
                if changed:
                    data['auth_tokens'] = refreshed
                    row.data = _normalize_user(data)
        if matched_user_id:
            return self.get_user_by_id(matched_user_id)
        return None

    def revoke_auth_token(self, raw_token: str) -> None:
        token_hash = _hash_auth_token(raw_token)
        with session_scope(self.db_path) as session:
            for row in session.scalars(select(UserRow)).all():
                data = dict(row.data or {})
                tokens = data.get('auth_tokens', []) or []
                refreshed = []
                changed = False
                for token in tokens:
                    if token.get('token_hash') == token_hash and not str(token.get('revoked_at', '')).strip():
                        token = token | {'revoked_at': datetime.utcnow().isoformat() + 'Z'}
                        changed = True
                    refreshed.append(token)
                if changed:
                    data['auth_tokens'] = refreshed
                    row.data = _normalize_user(data)

    def revoke_all_auth_tokens_for_user(self, user_id: str) -> None:
        uid = str(user_id or '').strip()
        if not uid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(UserRow, uid)
            if row is None:
                return
            data = dict(row.data or {})
            tokens = data.get('auth_tokens', []) or []
            now_iso = datetime.utcnow().isoformat() + 'Z'
            refreshed = []
            for token in tokens:
                if not str(token.get('revoked_at', '')).strip():
                    token = token | {'revoked_at': now_iso}
                refreshed.append(token)
            data['auth_tokens'] = refreshed
            row.data = _normalize_user(data)

    # ---- jobs ----

    def get_jobs(self, include_pending: bool = True) -> list[dict]:
        with session_scope(self.db_path) as session:
            stmt = select(JobRow)
            if not include_pending:
                stmt = stmt.where(JobRow.status == 'approved')
            rows = session.scalars(stmt.order_by(JobRow.submitted_at)).all()
            return [_normalize_job(dict(row.data or {})) for row in rows]

    def get_job_by_id(self, job_id: str) -> dict | None:
        jid = str(job_id or '').strip()
        if not jid:
            return None
        with session_scope(self.db_path) as session:
            row = session.get(JobRow, jid)
            return _normalize_job(dict(row.data or {})) if row else None

    def upsert_job(self, payload: dict) -> None:
        normalized = _normalize_job(payload)
        with session_scope(self.db_path) as session:
            self._write_job_row(session, normalized)

    def update_job(self, job_id: str, patch: dict) -> None:
        jid = str(job_id or '').strip()
        if not jid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(JobRow, jid)
            if row is None:
                return
            normalized = _normalize_job(dict(row.data or {}) | (patch or {}))
            self._write_job_row(session, normalized, existing=row)

    def delete_job(self, job_id: str) -> None:
        jid = str(job_id or '').strip()
        if not jid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(JobRow, jid)
            if row is not None:
                session.delete(row)

    def bulk_upsert_jobs(self, payloads: list[dict]) -> int:
        count = 0
        with session_scope(self.db_path) as session:
            for raw in payloads or []:
                normalized = _normalize_job(raw)
                if not normalized.get('id'):
                    continue
                self._write_job_row(session, normalized)
                count += 1
        return count

    def bulk_update_jobs(self, patches_by_id: dict[str, dict]) -> int:
        if not patches_by_id:
            return 0
        count = 0
        with session_scope(self.db_path) as session:
            for jid, patch in patches_by_id.items():
                row = session.get(JobRow, str(jid).strip())
                if row is None:
                    continue
                normalized = _normalize_job(dict(row.data or {}) | (patch or {}))
                self._write_job_row(session, normalized, existing=row)
                count += 1
        return count

    def find_duplicate_job(self, company: str, job_title: str, exclude_job_id: str = '') -> dict | None:
        company_key = _job_compare_key(company)
        title_key = _job_compare_key(job_title)
        if not company_key or not title_key:
            return None
        with session_scope(self.db_path) as session:
            stmt = select(JobRow).where(JobRow.company_key == company_key, JobRow.title_key == title_key)
            if exclude_job_id:
                stmt = stmt.where(JobRow.id != str(exclude_job_id).strip())
            row = session.scalar(stmt.limit(1))
            return _normalize_job(dict(row.data or {})) if row else None

    def find_job_by_url(self, url: str, exclude_job_id: str = '') -> dict | None:
        normalized = normalize_job_url(url)
        if not normalized:
            return None
        with session_scope(self.db_path) as session:
            stmt = select(JobRow).where(JobRow.normalized_url == normalized)
            if exclude_job_id:
                stmt = stmt.where(JobRow.id != str(exclude_job_id).strip())
            row = session.scalar(stmt.limit(1))
            return _normalize_job(dict(row.data or {})) if row else None

    def claim_next_pending_job_for_scrape(self) -> dict | None:
        with session_scope(self.db_path) as session:
            row = session.scalar(
                select(JobRow).where(JobRow.scrape_status == 'queued').order_by(JobRow.submitted_at).limit(1)
            )
            if row is None:
                return None
            data = dict(row.data or {})
            if not str(data.get('link', '')).strip():
                return None
            data['scrape_status'] = 'processing'
            data['scrape_started_at'] = datetime.utcnow().isoformat() + 'Z'
            normalized = _normalize_job(data)
            self._write_job_row(session, normalized, existing=row)
            return normalized

    def complete_job_scrape(self, job_id: str, patch: dict) -> None:
        self.update_job(job_id, patch)

    @staticmethod
    def _write_job_row(session: Session, normalized: dict, *, existing: JobRow | None = None) -> None:
        row = existing or session.get(JobRow, normalized['id'])
        if row is None:
            row = JobRow(id=normalized['id'])
            session.add(row)
        row.data = normalized
        row.company = normalized.get('company', '')
        row.job_title = normalized.get('job_title', '')
        row.region = normalized.get('region', 'ANY')
        row.status = normalized.get('status', 'approved')
        row.normalized_url = normalize_job_url(normalized.get('link', ''))
        row.company_key = _job_compare_key(normalized.get('company', ''))
        row.title_key = _job_compare_key(normalized.get('job_title', ''))
        row.scrape_status = normalized.get('scrape_status', 'done')
        row.submitted_at = _parse_iso_datetime(normalized.get('submitted_at', ''))
        row.created_by_user_id = str(normalized.get('created_by_user_id', '')).strip()

    def add_job_report(self, job_id: str, report: dict) -> None:
        jid = str(job_id or '').strip()
        if not jid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(JobRow, jid)
            if row is None:
                return
            data = dict(row.data or {})
            reports = list(data.get('reports', []) or [])
            reports.append(report)
            normalized = _normalize_job(data | {'reports': reports, 'flagged': True})
            self._write_job_row(session, normalized, existing=row)

    def clear_job_reports(self, job_id: str) -> None:
        jid = str(job_id or '').strip()
        if not jid:
            return
        with session_scope(self.db_path) as session:
            row = session.get(JobRow, jid)
            if row is None:
                return
            normalized = _normalize_job(dict(row.data or {}) | {'reports': [], 'flagged': False})
            self._write_job_row(session, normalized, existing=row)

    # ---- openai usage logs ----

    def record_openai_call(self, user_id: str, kind: str = '', details: dict | None = None) -> None:
        cleaned = str(user_id or '').strip()
        if not cleaned:
            return
        details = details if isinstance(details, dict) else {}
        entry = _normalize_openai_call({
            **details,
            'user_id': cleaned,
            'kind': kind,
            'recorded_at': datetime.utcnow().isoformat() + 'Z',
        })
        with session_scope(self.db_path) as session:
            session.add(OpenAICallRow(
                user_id=entry['user_id'],
                recorded_at=_parse_iso_datetime(entry['recorded_at']) or datetime.utcnow(),
                data=entry,
            ))

    def get_openai_calls(self) -> list[dict]:
        with session_scope(self.db_path) as session:
            rows = session.scalars(select(OpenAICallRow).order_by(OpenAICallRow.recorded_at)).all()
            return [_normalize_openai_call(dict(row.data or {})) for row in rows]
