from __future__ import annotations

import os
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env', override=False)

from core.export_engine import build_export_bundle
from core.resume_engine import analyze_ats_score, generate_application_answers, generate_resume_content
from core.storage import Storage, _normalize_market_region, verify_password

storage = Storage(PROJECT_ROOT / 'data')


DAY_KEYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']


def now_iso() -> str:
    return datetime.utcnow().isoformat() + 'Z'


def make_user_summary(user: dict) -> dict[str, Any]:
    return {
        'id': str(user.get('id', '')).strip(),
        'username': str(user.get('username', '')).strip(),
        'full_name': str(user.get('full_name', '')).strip(),
        'email': str(user.get('email', '')).strip(),
        'is_admin': bool(user.get('is_admin', False)),
        'status': str(user.get('status', '')).strip(),
    }


def authenticate_user(identifier: str, password: str) -> dict | None:
    needle = str(identifier or '').strip().lower()
    if not needle or not password:
        return None
    for user in storage.get_users():
        if user.get('status') != 'approved':
            continue
        if user.get('username', '').lower() != needle and user.get('email', '').lower() != needle:
            continue
        if verify_password(password, user.get('password_salt', ''), user.get('password_hash', '')):
            return user
    return None


def get_accessible_profiles(user: dict) -> list[dict]:
    profiles = storage.get_profiles()
    if user.get('is_admin'):
        return profiles
    allowed = set(str(v).strip() for v in user.get('assigned_profile_ids', []) if str(v).strip())
    return [profile for profile in profiles if str(profile.get('id', '')).strip() in allowed]


def get_profile_map(user: dict) -> dict[str, dict]:
    return {str(profile.get('id', '')).strip(): profile for profile in get_accessible_profiles(user)}


def get_templates() -> list[dict]:
    return storage.get_templates()


def get_template_map() -> dict[str, dict]:
    return {str(template.get('id', '')).strip(): template for template in get_templates()}


def regions_match(profile_region: str, job_region: str) -> bool:
    p = _normalize_market_region(profile_region)
    j = _normalize_market_region(job_region)
    return p == 'ANY' or j == 'ANY' or p == j


def get_user_generated_resumes(user: dict) -> list[dict]:
    items = storage.get_generated_resumes()
    if user.get('is_admin'):
        return items
    user_id = str(user.get('id', '')).strip()
    return [item for item in items if str(item.get('created_by_user_id', '')).strip() == user_id]


def get_matching_profiles_for_job(user: dict, job: dict) -> list[dict]:
    profiles = get_accessible_profiles(user)
    return [profile for profile in profiles if regions_match(profile.get('region', ''), job.get('region', ''))]


def get_applied_profile_ids_for_job(user: dict, job_id: str) -> set[str]:
    applied: set[str] = set()
    for item in get_user_generated_resumes(user):
        if str(item.get('job_id', '')).strip() == str(job_id).strip():
            applied.add(str(item.get('profile_id', '')).strip())
    return applied


def get_user_jobs(user: dict, only_open: bool = True, search: str = '') -> list[dict]:
    approved_jobs = storage.get_jobs(include_pending=False)
    jobs: list[dict] = []
    needle = str(search or '').strip().lower()
    for job in approved_jobs:
        matching_profiles = get_matching_profiles_for_job(user, job)
        if not matching_profiles:
            continue
        matching_profile_ids = [str(profile.get('id', '')).strip() for profile in matching_profiles]
        matching_profile_names = [str(profile.get('name', '')).strip() for profile in matching_profiles]
        applied_profile_ids = sorted(get_applied_profile_ids_for_job(user, str(job.get('id', '')).strip()) & set(matching_profile_ids))
        remaining_profile_ids = [profile_id for profile_id in matching_profile_ids if profile_id not in applied_profile_ids]
        is_open = bool(remaining_profile_ids)
        if only_open and not is_open:
            continue
        if needle:
            haystack = ' '.join([
                str(job.get('company', '')),
                str(job.get('job_title', '')),
                str(job.get('description', '')),
                str(job.get('note', '')),
                str(job.get('link', '')),
                ' '.join(matching_profile_names),
            ]).lower()
            if needle not in haystack:
                continue
        jobs.append({
            'id': str(job.get('id', '')).strip(),
            'company': str(job.get('company', '')).strip(),
            'job_title': str(job.get('job_title', '')).strip(),
            'region': _normalize_market_region(job.get('region', '')),
            'link': str(job.get('link', '')).strip(),
            'note': str(job.get('note', '')).strip(),
            'description_preview': str(job.get('description', '')).strip()[:500],
            'description': str(job.get('description', '')).strip(),
            'matching_profile_ids': matching_profile_ids,
            'matching_profile_names': matching_profile_names,
            'applied_profile_ids': applied_profile_ids,
            'remaining_profile_ids': remaining_profile_ids,
            'is_open': is_open,
            'reports_count': len(job.get('reports', []) or []),
            'flagged': bool(job.get('flagged', False)),
        })
    return jobs


def get_next_job_for_user(user: dict, current_job_id: str = '') -> dict | None:
    jobs = get_user_jobs(user, only_open=True)
    if not jobs:
        return None
    if not current_job_id:
        return jobs[0]
    ids = [job['id'] for job in jobs]
    if current_job_id not in ids:
        return jobs[0]
    current_index = ids.index(current_job_id)
    return jobs[(current_index + 1) % len(jobs)]


def get_job_detail_for_user(user: dict, job_id: str) -> dict | None:
    for item in get_user_jobs(user, only_open=False):
        if item['id'] == str(job_id).strip():
            return item
    return None


def choose_profile_for_job(user: dict, job: dict, profile_id: str = '') -> dict:
    matching_profiles = get_matching_profiles_for_job(user, job)
    if not matching_profiles:
        raise ValueError('No accessible profiles match this job region.')
    requested = str(profile_id or '').strip()
    if requested:
        for profile in matching_profiles:
            if str(profile.get('id', '')).strip() == requested:
                return profile
        raise ValueError('Selected profile is not available for this job.')

    applied_profile_ids = get_applied_profile_ids_for_job(user, str(job.get('id', '')).strip())
    for profile in matching_profiles:
        if str(profile.get('id', '')).strip() not in applied_profile_ids:
            return profile
    return matching_profiles[0]


def choose_template(profile: dict, template_id: str = '') -> dict:
    templates = get_templates()
    template_map = get_template_map()
    requested = str(template_id or '').strip()
    if requested and requested in template_map:
        return template_map[requested]
    profile_default = str(profile.get('default_template_id', '')).strip()
    if profile_default and profile_default in template_map:
        return template_map[profile_default]
    return templates[0] if templates else {
        'id': '',
        'name': 'Default',
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


def save_generated_resume_for_user(user: dict, profile: dict, template: dict, job: dict, target_role: str, resume_result: dict) -> str:
    resume = deepcopy(resume_result.get('resume', {}) if isinstance(resume_result, dict) else {})
    ats = analyze_ats_score(resume, str(job.get('description', '')).strip(), target_role=target_role)
    saved_resume_id = storage.make_id('resume')
    created_at = now_iso()
    storage.save_generated_resume({
        'saved_resume_id': saved_resume_id,
        'created_at': created_at,
        'created_date': created_at[:10],
        'created_by_user_id': str(user.get('id', '')).strip(),
        'created_by_username': str(user.get('username', '')).strip(),
        'profile_id': str(profile.get('id', '')).strip(),
        'template_id': str(template.get('id', '')).strip(),
        'job_id': str(job.get('id', '')).strip(),
        'job_company': str(job.get('company', '')).strip(),
        'job_title': str(job.get('job_title', '')).strip(),
        'job_link': str(job.get('link', '')).strip(),
        'job_description': str(job.get('description', '')).strip(),
        'job_region': _normalize_market_region(job.get('region', '')),
        'target_role': str(target_role or job.get('job_title', '')).strip(),
        'resume': resume,
        'ats_score': int(ats.get('overall_score', 0) or 0),
        'download_filename': f"{(profile.get('name', 'resume') or 'resume').strip().replace(' ', '_')}.pdf",
        'download_mode': 'browser',
        'company_message': '',
        'company_message_status': 'pending',
    })
    return saved_resume_id


def get_saved_resume_for_user(user: dict, saved_resume_id: str) -> dict | None:
    user_id = str(user.get('id', '')).strip()
    for item in storage.get_generated_resumes():
        if str(item.get('saved_resume_id', '')).strip() != str(saved_resume_id).strip():
            continue
        if user.get('is_admin') or str(item.get('created_by_user_id', '')).strip() == user_id:
            return item
    return None


def build_weekly_report_for_user(user: dict, week_start_value: date | None = None) -> dict[str, Any]:
    today = datetime.utcnow().date()
    monday = week_start_value or (today - timedelta(days=today.weekday()))
    if not isinstance(monday, date):
        monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    daily_counts = {key: 0 for key in DAY_KEYS}
    daily_schedule_counts = {key: 0 for key in DAY_KEYS}
    recent_applications: list[dict[str, Any]] = []

    for item in get_user_generated_resumes(user):
        created_date_raw = str(item.get('created_date', '')).strip() or str(item.get('created_at', '')).strip()[:10]
        try:
            created_day = date.fromisoformat(created_date_raw)
        except Exception:
            continue
        if created_day < monday or created_day > sunday:
            continue
        key = DAY_KEYS[created_day.weekday()]
        daily_counts[key] += 1
        schedule = item.get('interview_schedule', {}) or {}
        if str(schedule.get('review_status', '')).strip() not in {'', 'not_submitted'}:
            daily_schedule_counts[key] += 1
        recent_applications.append({
            'saved_resume_id': str(item.get('saved_resume_id', '')).strip(),
            'created_at': str(item.get('created_at', '')).strip(),
            'company': str(item.get('job_company', '')).strip(),
            'job_title': str(item.get('job_title', '')).strip(),
            'profile_id': str(item.get('profile_id', '')).strip(),
            'ats_score': int(item.get('ats_score', 0) or 0),
        })

    recent_applications.sort(key=lambda row: row.get('created_at', ''), reverse=True)
    return {
        'week_start': monday.isoformat(),
        'week_end': sunday.isoformat(),
        'applications_total': sum(daily_counts.values()),
        'schedules_total': sum(daily_schedule_counts.values()),
        'daily_counts': daily_counts,
        'daily_schedule_counts': daily_schedule_counts,
        'recent_applications': recent_applications[:25],
    }


def create_or_report_job(user: dict, payload: dict[str, Any]) -> dict[str, Any]:
    reason = str(payload.get('reason', '')).strip()
    if not reason:
        raise ValueError('Reason is required.')
    job_id = str(payload.get('job_id', '')).strip()
    report = {
        'reason': reason,
        'reported_by_user_id': str(user.get('id', '')).strip(),
        'reported_by_username': str(user.get('username', '')).strip(),
        'reported_at': now_iso(),
        'source': 'extension',
    }
    if job_id:
        job = storage.get_job_by_id(job_id)
        if not job:
            raise ValueError('Job not found.')
        storage.add_job_report(job_id, report)
        updated = storage.get_job_by_id(job_id) or job
        return {'job_id': job_id, 'status': str(updated.get('status', '')).strip(), 'flagged': bool(updated.get('flagged', False)), 'reports_count': len(updated.get('reports', []) or [])}

    job_link = str(payload.get('link', '')).strip()
    company = str(payload.get('company', '')).strip()
    job_title = str(payload.get('job_title', '')).strip() or 'Reported job'
    description = str(payload.get('description', '')).strip()
    note = str(payload.get('note', '')).strip()
    region = _normalize_market_region(payload.get('region', 'US'))

    duplicate = storage.find_duplicate_job(company, job_title)
    if duplicate:
        storage.add_job_report(str(duplicate.get('id', '')).strip(), report)
        updated = storage.get_job_by_id(str(duplicate.get('id', '')).strip()) or duplicate
        return {'job_id': str(updated.get('id', '')).strip(), 'status': str(updated.get('status', '')).strip(), 'flagged': bool(updated.get('flagged', False)), 'reports_count': len(updated.get('reports', []) or []), 'duplicate': True}

    job_id = storage.make_id('job')
    submitted_at = now_iso()
    storage.upsert_job({
        'id': job_id,
        'company': company,
        'job_title': job_title,
        'description': description,
        'link': job_link,
        'region': region,
        'note': note,
        'status': 'pending',
        'source': 'extension_report',
        'scrape_status': 'done' if description else ('queued' if job_link else 'done'),
        'created_by_user_id': str(user.get('id', '')).strip(),
        'created_by_username': str(user.get('username', '')).strip(),
        'submitted_at': submitted_at,
        'reports': [report],
        'flagged': True,
    })
    return {'job_id': job_id, 'status': 'pending', 'flagged': True, 'reports_count': 1, 'duplicate': False}


def available_download_formats(saved_resume: dict, profile: dict | None, template: dict | None) -> list[str]:
    formats = ['pdf']
    if profile and template:
        formats.append('docx')
    return formats


def build_resume_download(saved_resume: dict, profile: dict, template: dict, fmt: str) -> tuple[bytes, str, str]:
    bundle = build_export_bundle(resume=saved_resume.get('resume', {}), profile=profile, template=template)
    fmt_clean = str(fmt or 'pdf').strip().lower()
    if fmt_clean not in {'pdf', 'docx', 'html', 'markdown'}:
        raise ValueError('Unsupported format.')
    content = bundle[fmt_clean]
    if isinstance(content, str):
        content_bytes = content.encode('utf-8')
    else:
        content_bytes = content
    base_name = Path(str(saved_resume.get('download_filename', '')).strip() or 'resume.pdf').stem
    filename = f'{base_name}.{fmt_clean if fmt_clean != "markdown" else "md"}'
    media_type = {
        'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'html': 'text/html; charset=utf-8',
        'markdown': 'text/markdown; charset=utf-8',
    }[fmt_clean]
    return content_bytes, filename, media_type
