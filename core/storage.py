from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from datetime import datetime
from json import JSONDecodeError, JSONDecoder
from pathlib import Path
from typing import Any

_ALLOWED_REGIONS = {'ANY', 'US', 'EU', 'LATAM'}


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


class Storage:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.profiles_path = self.data_dir / 'profiles.json'
        self.templates_path = self.data_dir / 'templates.json'
        self.generated_resumes_path = self.data_dir / 'generated_resumes.json'
        self.settings_path = self.data_dir / 'settings.json'
        self.users_path = self.data_dir / 'users.json'
        self.jobs_path = self.data_dir / 'jobs.json'
        self._lock = threading.RLock()
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        with self._lock:
            if not self.profiles_path.exists():
                self._write_json(self.profiles_path, _default_profiles())
            else:
                self._write_json(self.profiles_path, self._normalize_profiles(self._read_json(self.profiles_path)))

            if not self.templates_path.exists():
                self._write_json(self.templates_path, _default_templates())
            else:
                self._write_json(self.templates_path, self._normalize_templates(self._read_json(self.templates_path)))

            if not self.generated_resumes_path.exists():
                self._write_json(self.generated_resumes_path, [])
            else:
                self._write_json(self.generated_resumes_path, self._normalize_generated_resumes(self._read_json(self.generated_resumes_path)))

            if not self.settings_path.exists():
                self._write_json(self.settings_path, _default_settings())
            else:
                self._write_json(self.settings_path, self._normalize_settings(self._read_json(self.settings_path)))

            users = self._normalize_users(self._read_json(self.users_path)) if self.users_path.exists() else _default_users()
            if not any(user.get('is_admin') and user.get('status') == 'approved' for user in users):
                users.extend(_default_users())
            deduped_users: list[dict] = []
            seen_usernames: set[str] = set()
            for user in users:
                username = str(user.get('username', '')).strip().lower()
                if not username or username in seen_usernames:
                    continue
                seen_usernames.add(username)
                deduped_users.append(user)
            self._write_json(self.users_path, deduped_users)

            jobs = self._normalize_jobs(self._read_json(self.jobs_path)) if self.jobs_path.exists() else []
            self._write_json(self.jobs_path, jobs)

    def _read_json(self, path: Path) -> Any:
        if not path.exists():
            return []
        text = path.read_text(encoding='utf-8').strip()
        if not text:
            return []
        try:
            return json.loads(text)
        except JSONDecodeError:
            recovered = self._recover_json_payload(path, text)
            backup_path = path.with_suffix(path.suffix + f'.corrupt.{datetime.utcnow().strftime("%Y%m%d%H%M%S")}.bak')
            try:
                if not backup_path.exists():
                    backup_path.write_text(text, encoding='utf-8')
            except Exception:
                pass
            try:
                self._write_json(path, recovered)
            except Exception:
                pass
            return recovered

    def _write_json(self, path: Path, data: Any) -> None:
        tmp_path = path.with_suffix(path.suffix + '.tmp')
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
        tmp_path.replace(path)

    def _recover_json_payload(self, path: Path, text: str) -> Any:
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
            raise
        if len(values) == 1:
            return values[0]

        if path.name == 'settings.json':
            dict_values = [value for value in values if isinstance(value, dict)]
            return dict_values[-1] if dict_values else {}

        merged_list: list[Any] = []
        for value in values:
            if isinstance(value, list):
                merged_list.extend(value)
            elif isinstance(value, dict):
                merged_list.append(value)
        if merged_list:
            return merged_list
        return values[-1]

    def make_id(self, prefix: str) -> str:
        return f'{prefix}_{uuid.uuid4().hex[:10]}'

    # Profiles
    def get_profiles(self) -> list[dict]:
        with self._lock:
            return self._normalize_profiles(self._read_json(self.profiles_path))

    def get_profile_by_id(self, profile_id: str) -> dict | None:
        for item in self.get_profiles():
            if item.get('id') == profile_id:
                return item
        return None

    def upsert_profile(self, payload: dict) -> None:
        with self._lock:
            payload = self._normalize_profiles([payload])[0]
            items = self.get_profiles()
            for index, item in enumerate(items):
                if item.get('id') == payload.get('id'):
                    items[index] = payload
                    self._write_json(self.profiles_path, items)
                    return
            items.append(payload)
            self._write_json(self.profiles_path, items)

    def delete_profile(self, profile_id: str) -> None:
        with self._lock:
            items = [item for item in self.get_profiles() if item.get('id') != profile_id]
            self._write_json(self.profiles_path, items)

    # Templates
    def get_templates(self) -> list[dict]:
        with self._lock:
            return self._normalize_templates(self._read_json(self.templates_path))

    def get_template_by_id(self, template_id: str) -> dict | None:
        for item in self.get_templates():
            if item.get('id') == template_id:
                return item
        return None

    def upsert_template(self, payload: dict) -> None:
        with self._lock:
            items = self.get_templates()
            normalized = self._normalize_templates([payload])[0]
            for index, item in enumerate(items):
                if item.get('id') == normalized.get('id'):
                    items[index] = normalized
                    self._write_json(self.templates_path, items)
                    return
            items.append(normalized)
            self._write_json(self.templates_path, items)

    def delete_template(self, template_id: str) -> None:
        with self._lock:
            items = [item for item in self.get_templates() if item.get('id') != template_id]
            self._write_json(self.templates_path, items)
            profiles = self.get_profiles()
            changed = False
            for profile in profiles:
                if str(profile.get('default_template_id', '')).strip() == str(template_id).strip():
                    profile['default_template_id'] = ''
                    changed = True
            if changed:
                self._write_json(self.profiles_path, self._normalize_profiles(profiles))

    # Generated resumes
    def get_generated_resumes(self) -> list[dict]:
        with self._lock:
            return self._normalize_generated_resumes(self._read_json(self.generated_resumes_path))

    def save_generated_resume(self, payload: dict) -> None:
        with self._lock:
            items = self.get_generated_resumes()
            items.append(payload)
            self._write_json(self.generated_resumes_path, self._normalize_generated_resumes(items))

    def update_generated_resume(self, saved_resume_id: str, patch: dict) -> None:
        with self._lock:
            items = self.get_generated_resumes()
            for index, item in enumerate(items):
                if item.get('saved_resume_id') == saved_resume_id:
                    items[index] = item | patch
                    self._write_json(self.generated_resumes_path, self._normalize_generated_resumes(items))
                    return

    # Settings
    def get_app_settings(self) -> dict:
        with self._lock:
            return self._normalize_settings(self._read_json(self.settings_path))

    def save_app_settings(self, payload: dict) -> None:
        with self._lock:
            self._write_json(self.settings_path, self._normalize_settings(payload))

    # Users
    def get_users(self) -> list[dict]:
        with self._lock:
            return self._normalize_users(self._read_json(self.users_path))

    def get_user_by_id(self, user_id: str) -> dict | None:
        for item in self.get_users():
            if item.get('id') == user_id:
                return item
        return None

    def get_user_by_username(self, username: str) -> dict | None:
        needle = str(username or '').strip().lower()
        for item in self.get_users():
            if item.get('username', '').lower() == needle:
                return item
        return None

    def upsert_user(self, payload: dict) -> None:
        with self._lock:
            items = self.get_users()
            normalized = self._normalize_users([payload])[0]
            for index, item in enumerate(items):
                if item.get('id') == normalized.get('id'):
                    items[index] = normalized
                    self._write_json(self.users_path, items)
                    return
            items.append(normalized)
            self._write_json(self.users_path, items)

    def update_user(self, user_id: str, patch: dict) -> None:
        with self._lock:
            items = self.get_users()
            for index, item in enumerate(items):
                if item.get('id') == user_id:
                    items[index] = self._normalize_users([item | patch])[0]
                    self._write_json(self.users_path, items)
                    return

    # Jobs
    def get_jobs(self, include_pending: bool = True) -> list[dict]:
        with self._lock:
            items = self._normalize_jobs(self._read_json(self.jobs_path))
        if include_pending:
            return items
        return [item for item in items if item.get('status') == 'approved']

    def get_job_by_id(self, job_id: str) -> dict | None:
        for item in self.get_jobs(include_pending=True):
            if item.get('id') == job_id:
                return item
        return None

    def upsert_job(self, payload: dict) -> None:
        with self._lock:
            items = self.get_jobs(include_pending=True)
            normalized = self._normalize_jobs([payload])[0]
            for index, item in enumerate(items):
                if item.get('id') == normalized.get('id'):
                    items[index] = normalized
                    self._write_json(self.jobs_path, items)
                    return
            items.append(normalized)
            self._write_json(self.jobs_path, items)

    def update_job(self, job_id: str, patch: dict) -> None:
        with self._lock:
            items = self.get_jobs(include_pending=True)
            for index, item in enumerate(items):
                if item.get('id') == job_id:
                    items[index] = self._normalize_jobs([item | patch])[0]
                    self._write_json(self.jobs_path, items)
                    return

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            items = [item for item in self.get_jobs(include_pending=True) if item.get('id') != job_id]
            self._write_json(self.jobs_path, items)

    def bulk_upsert_jobs(self, payloads: list[dict]) -> int:
        with self._lock:
            items = self.get_jobs(include_pending=True)
            by_id = {str(item.get('id', '')).strip(): index for index, item in enumerate(items)}
            count = 0
            for normalized in self._normalize_jobs(payloads):
                job_id = str(normalized.get('id', '')).strip()
                if not job_id:
                    continue
                if job_id in by_id:
                    items[by_id[job_id]] = normalized
                else:
                    by_id[job_id] = len(items)
                    items.append(normalized)
                count += 1
            if count:
                self._write_json(self.jobs_path, items)
            return count

    def bulk_update_jobs(self, patches_by_id: dict[str, dict]) -> int:
        with self._lock:
            if not patches_by_id:
                return 0
            items = self.get_jobs(include_pending=True)
            changed = 0
            for index, item in enumerate(items):
                job_id = str(item.get('id', '')).strip()
                if job_id and job_id in patches_by_id:
                    items[index] = self._normalize_jobs([item | (patches_by_id[job_id] or {})])[0]
                    changed += 1
            if changed:
                self._write_json(self.jobs_path, items)
            return changed

    def find_duplicate_job(self, company: str, job_title: str, exclude_job_id: str = '') -> dict | None:
        company_key = _job_compare_key(company)
        title_key = _job_compare_key(job_title)
        if not company_key or not title_key:
            return None
        for item in self.get_jobs(include_pending=True):
            if exclude_job_id and item.get('id') == exclude_job_id:
                continue
            if _job_compare_key(item.get('company', '')) == company_key and _job_compare_key(item.get('job_title', '')) == title_key:
                return item
        return None

    def claim_next_pending_job_for_scrape(self) -> dict | None:
        with self._lock:
            items = self.get_jobs(include_pending=True)
            for index, item in enumerate(items):
                if item.get('scrape_status') == 'queued' and str(item.get('link', '')).strip():
                    items[index] = item | {
                        'scrape_status': 'processing',
                        'scrape_started_at': datetime.utcnow().isoformat() + 'Z',
                    }
                    self._write_json(self.jobs_path, items)
                    return items[index]
        return None

    def complete_job_scrape(self, job_id: str, patch: dict) -> None:
        self.update_job(job_id, patch)

    def _normalize_profiles(self, items: Any) -> list[dict]:
        normalized: list[dict] = []
        for item in items or []:
            work_history = []
            for raw_job in item.get('work_history', []) or []:
                work_history.append({
                    'company_name': raw_job.get('company_name', ''),
                    'duration': raw_job.get('duration', ''),
                    'location': raw_job.get('location', ''),
                    'bullets': [str(b).strip() for b in raw_job.get('bullets', []) if str(b).strip()],
                    'legacy_role': raw_job.get('legacy_role', raw_job.get('role', '')),
                })
            normalized.append({
                'id': item.get('id', ''),
                'name': item.get('name', ''),
                'email': item.get('email', ''),
                'phone': item.get('phone', ''),
                'location': item.get('location', ''),
                'linkedin': item.get('linkedin', ''),
                'portfolio': item.get('portfolio', ''),
                'default_template_id': str(item.get('default_template_id', '')).strip(),
                'summary_seed': item.get('summary_seed', ''),
                'technical_skills': [str(s).strip() for s in item.get('technical_skills', []) if str(s).strip()],
                'region': _normalize_market_region(item.get('region', item.get('market_region', ''))),
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
            })
        return normalized

    def _normalize_templates(self, items: Any) -> list[dict]:
        defaults = _template_defaults()
        normalized: list[dict] = []
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        for index, item in enumerate(items or []):
            merged = defaults | item
            merged['section_order'] = item.get('section_order', defaults['section_order'])
            if merged.get('skill_style') == 'grouped':
                merged['skill_style'] = 'grouped_bullets'
            template_id = str(merged.get('id') or '').strip() or self.make_id('template')
            if template_id in seen_ids:
                template_id = self.make_id('template')
            seen_ids.add(template_id)
            merged['id'] = template_id

            base_name = str(merged.get('name', '')).strip() or f'Template {index + 1}'
            candidate_name = base_name
            suffix = 2
            while candidate_name.lower() in seen_names:
                candidate_name = f"{base_name} ({suffix})"
                suffix += 1
            seen_names.add(candidate_name.lower())
            merged['name'] = candidate_name
            normalized.append(merged)
        return normalized


    def _normalize_generated_resumes(self, items: Any) -> list[dict]:
        normalized: list[dict] = []
        for item in items or []:
            resume = item.get('resume', {}) if isinstance(item.get('resume', {}), dict) else {}
            grouped_skills = resume.get('grouped_skills', {}) if isinstance(resume.get('grouped_skills', {}), dict) else {}
            normalized_resume = {
                'name': str(resume.get('name', '')).strip(),
                'headline': str(resume.get('headline', '')).strip(),
                'summary': str(resume.get('summary', '')).strip(),
                'fit_keywords': [str(v).strip() for v in resume.get('fit_keywords', []) if str(v).strip()],
                'technical_skills': [str(v).strip() for v in resume.get('technical_skills', []) if str(v).strip()],
                'grouped_skills': {
                    str(key).strip() or 'Other Relevant': [str(v).strip() for v in values or [] if str(v).strip()]
                    for key, values in grouped_skills.items()
                    if [str(v).strip() for v in values or [] if str(v).strip()]
                },
                'work_history': [
                    {
                        'company_name': str(work.get('company_name', '')).strip(),
                        'duration': str(work.get('duration', '')).strip(),
                        'location': str(work.get('location', '')).strip(),
                        'role': str(work.get('role', '')).strip(),
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
            normalized.append({
                'saved_resume_id': str(item.get('saved_resume_id', '')).strip() or self.make_id('resume'),
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
                'ats_score': int(item.get('ats_score', 0) or 0),
                'download_filename': str(item.get('download_filename', '')).strip() or 'resume.pdf',
                'download_mode': str(item.get('download_mode', 'browser') or 'browser').strip(),
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
            })
        return normalized

    def _normalize_settings(self, item: Any) -> dict:
        defaults = _default_settings()
        source = item if isinstance(item, dict) else {}
        merged = defaults | source
        merged['default_prompt'] = str(merged.get('default_prompt', '')).strip()
        merged['download_output_dir'] = str(merged.get('download_output_dir', 'saved_resumes')).strip() or 'saved_resumes'
        merged['always_clean_generation'] = True
        return merged

    def _normalize_users(self, items: Any) -> list[dict]:
        normalized: list[dict] = []
        for item in items or []:
            username = str(item.get('username', '')).strip().lower()
            if not username:
                continue
            normalized.append({
                'id': item.get('id') or self.make_id('user'),
                'username': username,
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
            })
        return normalized

    def _normalize_jobs(self, items: Any) -> list[dict]:
        normalized: list[dict] = []
        for item in items or []:
            normalized.append({
                'id': item.get('id') or self.make_id('job'),
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
            })
        return normalized


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


def _default_profiles() -> list[dict]:
    return []


def _default_templates() -> list[dict]:
    return [
        {
            'id': 'template_ats_classic',
            'name': 'ATS Classic',
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
        },
        {
            'id': 'template_ats_compact',
            'name': 'ATS Compact',
            'font_family': 'Arial, sans-serif',
            'accent_color': '#0f172a',
            'text_color': '#111827',
            'muted_color': '#475569',
            'background_color': '#ffffff',
            'section_order': ['technical_skills', 'summary', 'work_history', 'education_history'],
            'custom_css': '',
            'layout_style': 'ats_compact',
            'header_style': 'minimal',
            'skill_style': 'grouped_bullets',
            'density': 'tight',
            'show_role_headline': True,
        },
        {
            'id': 'template_ats_technical',
            'name': 'ATS Technical',
            'font_family': 'Arial, sans-serif',
            'accent_color': '#1d4ed8',
            'text_color': '#111827',
            'muted_color': '#6b7280',
            'background_color': '#ffffff',
            'section_order': ['summary', 'technical_skills', 'work_history', 'education_history'],
            'custom_css': '',
            'layout_style': 'ats_technical',
            'header_style': 'rule',
            'skill_style': 'grouped_bullets',
            'density': 'normal',
            'show_role_headline': True,
        },
    ]


def _default_settings() -> dict:
    return {
        'default_prompt': '',
        'always_clean_generation': True,
        'download_output_dir': 'saved_resumes',
    }


def _default_users() -> list[dict]:
    password_fields = build_password_record('admin123')
    return [
        {
            'id': 'user_admin_default',
            'username': 'admin',
            'full_name': 'Administrator',
            'email': '',
            'password_hash': password_fields['password_hash'],
            'password_salt': password_fields['password_salt'],
            'is_admin': True,
            'status': 'approved',
            'assigned_profile_ids': [],
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'approved_at': datetime.utcnow().isoformat() + 'Z',
            'approved_by_user_id': 'system',
            'force_password_change': True,
        }
    ]


def _job_compare_key(value: str) -> str:
    return ''.join(ch.lower() if ch.isalnum() else ' ' for ch in str(value or '')).strip()
