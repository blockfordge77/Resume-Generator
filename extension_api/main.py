from __future__ import annotations

import os
import sys
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Annotated
from zipfile import ZIP_DEFLATED, ZipFile
from fastapi.responses import HTMLResponse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.resume_engine import analyze_ats_score, generate_application_answers, generate_resume_content

from .schemas import (
    AuthResponse,
    DashboardResponse,
    DownloadFormatResponse,
    JobDetailResponse,
    JobListItem,
    JobReportRequest,
    LoginRequest,
    ProfileSummary,
    ResumeAnswerRequest,
    ResumeGenerateRequest,
    ResumeGenerateResponse,
    ResumeSummary,
    TemplateSummary,
    UserSummary,
    WeeklyReportResponse,
)
from .services import (
    authenticate_user,
    available_download_formats,
    build_resume_download,
    build_weekly_report_for_user,
    choose_profile_for_job,
    choose_template,
    create_or_report_job,
    get_accessible_profiles,
    get_job_detail_for_user,
    get_next_job_for_user,
    get_saved_resume_for_user,
    get_templates,
    get_user_generated_resumes,
    get_user_jobs,
    make_user_summary,
    save_generated_resume_for_user,
    storage,
)

app = FastAPI(
    title='Resume Generator Extension API',
    version='1.0.0',
    description='User-focused API layer for extension and side-panel clients. Shares the same data store as the Streamlit app.',
)

allowed_origins_raw = os.getenv('EXTENSION_API_ALLOW_ORIGINS', '*').strip()
allowed_origins = ['*'] if allowed_origins_raw == '*' else [part.strip() for part in allowed_origins_raw.split(',') if part.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

security = HTTPBearer(auto_error=False)


Credential = Annotated[HTTPAuthorizationCredentials | None, Depends(security)]


def _resolve_current_user(credentials: Credential) -> dict:
    if not credentials or not str(credentials.credentials or '').strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing auth token.')
    user = storage.get_user_by_auth_token(str(credentials.credentials).strip())
    if not user or user.get('status') != 'approved':
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid or expired auth token.')
    return user


CurrentUser = Annotated[dict, Depends(_resolve_current_user)]



def _extension_base_url() -> str:
    value = os.getenv('EXTENSION_API_BASE_URL', '').strip()
    if value:
        return value.rstrip('/')
    host = os.getenv('EXTENSION_API_HOST', '127.0.0.1').strip() or '127.0.0.1'
    port = os.getenv('EXTENSION_API_PORT', '8010').strip() or '8010'
    return f'http://{host}:{port}'


def _browser_extension_dir() -> Path:
    return PROJECT_ROOT / 'browser_extension'


def _browser_extension_zip_bytes() -> bytes:
    ext_dir = _browser_extension_dir()
    buf = BytesIO()
    with ZipFile(buf, 'w', compression=ZIP_DEFLATED) as zf:
        for path in ext_dir.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(ext_dir).as_posix())
    return buf.getvalue()



@app.get('/api/ext/health')
def health() -> dict:
    return {'ok': True, 'service': 'extension_api'}


@app.get('/api/ext/extension/config')
def extension_config() -> dict:
    return {
        'api_base_url': _extension_base_url(),
        'download_url': f"{_extension_base_url()}/api/ext/extension/download",
        'install_url': f"{_extension_base_url()}/api/ext/extension/install",
    }


@app.get('/api/ext/extension/download')
def download_extension() -> StreamingResponse:
    data = _browser_extension_zip_bytes()
    return StreamingResponse(BytesIO(data), media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="tailorresume_browser_extension.zip"'})


@app.get('/api/ext/extension/install', response_class=HTMLResponse)
def extension_install_page() -> HTMLResponse:
    base = _extension_base_url()
    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>TailorResume Extension Install</title>
<style>body{{font-family:Arial,sans-serif;max-width:820px;margin:32px auto;padding:0 16px;color:#111}}a.button{{display:inline-block;background:#2563eb;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;margin-right:10px}}code{{background:#f3f4f6;padding:2px 6px;border-radius:4px}}</style></head><body>
<h1>TailorResume Browser Extension</h1>
<p>Download the extension package and load it in Chrome or Edge.</p>
<p><a class='button' href='{base}/api/ext/extension/download'>Download extension zip</a><a class='button' href='chrome://extensions'>Open Chrome extensions</a></p>
<ol><li>Download the zip above and extract it.</li><li>Open <code>chrome://extensions</code> or <code>edge://extensions</code>.</li><li>Enable <strong>Developer mode</strong>.</li><li>Click <strong>Load unpacked</strong> and choose the extracted <code>browser_extension</code> folder.</li></ol>
<p>A true <strong>Add to Chrome</strong> button is only possible after publishing the extension to the Chrome Web Store.</p>
</body></html>"""
    return HTMLResponse(content=html)


@app.post('/api/ext/auth/login', response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user = authenticate_user(payload.identifier, payload.password)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid username/email or password.')
    token = storage.issue_auth_token(str(user.get('id', '')).strip(), ttl_days=payload.ttl_days)
    return AuthResponse(token=token, user=UserSummary(**make_user_summary(user)))


@app.post('/api/ext/auth/logout')
def logout(current_user: CurrentUser, credentials: Credential) -> dict:
    if credentials and str(credentials.credentials or '').strip():
        storage.revoke_auth_token(str(credentials.credentials).strip())
    return {'ok': True, 'message': 'Signed out.'}


@app.get('/api/ext/auth/me', response_model=UserSummary)
def me(current_user: CurrentUser) -> UserSummary:
    return UserSummary(**make_user_summary(current_user))


@app.get('/api/ext/profiles', response_model=list[ProfileSummary])
def profiles(current_user: CurrentUser) -> list[ProfileSummary]:
    return [
        ProfileSummary(
            id=str(profile.get('id', '')).strip(),
            name=str(profile.get('name', '')).strip(),
            region=str(profile.get('region', '')).strip(),
            default_template_id=str(profile.get('default_template_id', '')).strip(),
        )
        for profile in get_accessible_profiles(current_user)
    ]


@app.get('/api/ext/templates', response_model=list[TemplateSummary])
def templates(current_user: CurrentUser) -> list[TemplateSummary]:
    return [
        TemplateSummary(
            id=str(template.get('id', '')).strip(),
            name=str(template.get('name', '')).strip(),
            layout_style=str(template.get('layout_style', '')).strip(),
            skill_style=str(template.get('skill_style', '')).strip(),
        )
        for template in get_templates()
    ]


@app.get('/api/ext/jobs', response_model=list[JobListItem])
def list_jobs(
    current_user: CurrentUser,
    search: str = '',
    only_open: bool = True,
) -> list[JobListItem]:
    return [JobListItem(**job) for job in get_user_jobs(current_user, only_open=only_open, search=search)]


@app.get('/api/ext/jobs/next', response_model=JobListItem | None)
def next_job(current_user: CurrentUser, current_job_id: str = '') -> JobListItem | None:
    job = get_next_job_for_user(current_user, current_job_id=current_job_id)
    return JobListItem(**job) if job else None


@app.get('/api/ext/jobs/{job_id}', response_model=JobDetailResponse)
def job_detail(job_id: str, current_user: CurrentUser) -> JobDetailResponse:
    job = get_job_detail_for_user(current_user, job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Job not found or not available for this user.')
    return JobDetailResponse(**job)


@app.post('/api/ext/jobs/report')
def report_job(payload: JobReportRequest, current_user: CurrentUser) -> dict:
    try:
        return create_or_report_job(current_user, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.get('/api/ext/weekly-report', response_model=WeeklyReportResponse)
def weekly_report(current_user: CurrentUser, week_start: date | None = Query(default=None)) -> WeeklyReportResponse:
    return WeeklyReportResponse(**build_weekly_report_for_user(current_user, week_start_value=week_start))


@app.get('/api/ext/dashboard', response_model=DashboardResponse)
def dashboard(current_user: CurrentUser) -> DashboardResponse:
    next_job_payload = get_next_job_for_user(current_user)
    jobs = get_user_jobs(current_user, only_open=True)
    generated = get_user_generated_resumes(current_user)
    return DashboardResponse(
        user=UserSummary(**make_user_summary(current_user)),
        next_job=JobListItem(**next_job_payload) if next_job_payload else None,
        open_jobs_count=len(jobs),
        generated_resumes_count=len(generated),
        weekly_report=WeeklyReportResponse(**build_weekly_report_for_user(current_user)),
    )


@app.post('/api/ext/resumes/generate', response_model=ResumeGenerateResponse)
def generate_resume(payload: ResumeGenerateRequest, current_user: CurrentUser) -> ResumeGenerateResponse:
    job = storage.get_job_by_id(payload.job_id)
    if not job or str(job.get('status', '')).strip() != 'approved':
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Approved job not found.')
    try:
        profile = choose_profile_for_job(current_user, job, profile_id=payload.profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    template = choose_template(profile, template_id=payload.template_id)
    settings = storage.get_app_settings()
    target_role = str(payload.target_role or job.get('job_title', '')).strip()
    result = generate_resume_content(
        profile=profile,
        job_description=str(job.get('description', '')).strip(),
        target_role=target_role,
        custom_prompt=payload.custom_prompt,
        default_prompt=str(settings.get('default_prompt', '')).strip(),
        use_ai=payload.use_ai,
        clean_generation=payload.clean_generation,
    )
    resume = result.get('resume', {}) if isinstance(result, dict) else {}
    ats = analyze_ats_score(resume, str(job.get('description', '')).strip(), target_role=target_role)
    saved_resume_id = None
    if payload.save_generated:
        saved_resume_id = save_generated_resume_for_user(current_user, profile, template, job, target_role, result)
    return ResumeGenerateResponse(
        mode=str(result.get('mode', '')).strip(),
        saved_resume_id=saved_resume_id,
        ats_score=int(ats.get('overall_score', 0) or 0),
        target_role=target_role,
        profile_id=str(profile.get('id', '')).strip(),
        template_id=str(template.get('id', '')).strip(),
        resume=resume,
        job_tech_analysis=result.get('job_tech_analysis') if isinstance(result, dict) else None,
        attempts=result.get('attempts', []) if isinstance(result, dict) else [],
    )


@app.get('/api/ext/resumes', response_model=list[ResumeSummary])
def list_generated_resumes(
    current_user: CurrentUser,
    week_start: date | None = Query(default=None),
    profile_id: str = '',
    job_id: str = '',
    search: str = '',
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ResumeSummary]:
    items = get_user_generated_resumes(current_user)
    profile_lookup = {str(profile.get('id', '')).strip(): str(profile.get('name', '')).strip() for profile in get_accessible_profiles(current_user)}
    if week_start:
        week_end = week_start + __import__('datetime').timedelta(days=6)
        items = [
            item for item in items
            if week_start.isoformat() <= str(item.get('created_date', '')).strip() <= week_end.isoformat()
        ]
    if profile_id:
        items = [item for item in items if str(item.get('profile_id', '')).strip() == str(profile_id).strip()]
    if job_id:
        items = [item for item in items if str(item.get('job_id', '')).strip() == str(job_id).strip()]
    needle = str(search or '').strip().lower()
    if needle:
        filtered: list[dict] = []
        for item in items:
            haystack = ' '.join([
                str(item.get('job_company', '')),
                str(item.get('job_title', '')),
                str(item.get('target_role', '')),
                str(item.get('job_description', '')),
                str((item.get('resume', {}) or {}).get('headline', '')),
                str((item.get('resume', {}) or {}).get('summary', '')),
            ]).lower()
            if needle in haystack:
                filtered.append(item)
        items = filtered
    items.sort(key=lambda row: (str(row.get('created_at', '')).strip(), str(row.get('saved_resume_id', '')).strip()), reverse=True)
    response: list[ResumeSummary] = []
    for item in items[:limit]:
        response.append(ResumeSummary(
            saved_resume_id=str(item.get('saved_resume_id', '')).strip(),
            created_at=str(item.get('created_at', '')).strip(),
            profile_id=str(item.get('profile_id', '')).strip(),
            profile_name=profile_lookup.get(str(item.get('profile_id', '')).strip(), str(item.get('profile_id', '')).strip()),
            job_id=str(item.get('job_id', '')).strip(),
            company=str(item.get('job_company', '')).strip(),
            job_title=str(item.get('job_title', '')).strip(),
            target_role=str(item.get('target_role', '')).strip(),
            ats_score=int(item.get('ats_score', 0) or 0),
            download_filename=str(item.get('download_filename', '')).strip() or 'resume.pdf',
        ))
    return response


@app.get('/api/ext/resumes/{saved_resume_id}')
def get_resume(saved_resume_id: str, current_user: CurrentUser) -> dict:
    item = get_saved_resume_for_user(current_user, saved_resume_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Saved resume not found.')
    return item


@app.get('/api/ext/resumes/{saved_resume_id}/formats', response_model=DownloadFormatResponse)
def get_resume_formats(saved_resume_id: str, current_user: CurrentUser) -> DownloadFormatResponse:
    item = get_saved_resume_for_user(current_user, saved_resume_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Saved resume not found.')
    profile = storage.get_profile_by_id(str(item.get('profile_id', '')).strip())
    template = storage.get_template_by_id(str(item.get('template_id', '')).strip())
    return DownloadFormatResponse(available_formats=available_download_formats(item, profile, template))


@app.get('/api/ext/resumes/{saved_resume_id}/download')
def download_resume(saved_resume_id: str, current_user: CurrentUser, fmt: str = Query(default='pdf')):
    item = get_saved_resume_for_user(current_user, saved_resume_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Saved resume not found.')
    profile = storage.get_profile_by_id(str(item.get('profile_id', '')).strip())
    template = storage.get_template_by_id(str(item.get('template_id', '')).strip())
    if not profile or not template:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Profile or template missing for this resume.')
    try:
        content_bytes, filename, media_type = build_resume_download(item, profile, template, fmt)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StreamingResponse(BytesIO(content_bytes), media_type=media_type, headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.post('/api/ext/resumes/{saved_resume_id}/answers')
def generate_answers(saved_resume_id: str, payload: ResumeAnswerRequest, current_user: CurrentUser) -> dict:
    item = get_saved_resume_for_user(current_user, saved_resume_id)
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Saved resume not found.')
    result = generate_application_answers(
        resume=item.get('resume', {}) or {},
        job_description=str(item.get('job_description', '')).strip(),
        questions=payload.questions,
        target_role=str(item.get('target_role', '')).strip(),
        use_ai=payload.use_ai,
    )
    return result
