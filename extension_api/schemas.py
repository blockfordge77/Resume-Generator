from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    identifier: str = Field(..., description="Username or email")
    password: str
    ttl_days: int = Field(default=30, ge=1, le=180)


class UserSummary(BaseModel):
    id: str
    username: str
    full_name: str
    email: str
    is_admin: bool
    status: str


class AuthResponse(BaseModel):
    token: str
    user: UserSummary


class ProfileSummary(BaseModel):
    id: str
    name: str
    region: str
    default_template_id: str = ''


class TemplateSummary(BaseModel):
    id: str
    name: str
    layout_style: str = ''
    skill_style: str = ''


class JobListItem(BaseModel):
    id: str
    company: str
    job_title: str
    region: str
    link: str
    note: str
    description_preview: str
    matching_profile_ids: list[str]
    matching_profile_names: list[str]
    applied_profile_ids: list[str]
    remaining_profile_ids: list[str]
    is_open: bool
    reports_count: int = 0
    flagged: bool = False


class JobDetailResponse(BaseModel):
    id: str
    company: str
    job_title: str
    region: str
    link: str
    note: str
    description: str
    matching_profile_ids: list[str]
    matching_profile_names: list[str]
    applied_profile_ids: list[str]
    remaining_profile_ids: list[str]
    is_open: bool
    reports_count: int = 0
    flagged: bool = False


class JobReportRequest(BaseModel):
    reason: str = Field(..., min_length=2)
    job_id: str = ''
    company: str = ''
    job_title: str = ''
    link: str = ''
    description: str = ''
    note: str = ''
    region: str = 'US'


class ResumeGenerateRequest(BaseModel):
    job_id: str
    profile_id: str = ''
    template_id: str = ''
    custom_prompt: str = ''
    target_role: str = ''
    use_ai: bool = True
    clean_generation: bool = True
    save_generated: bool = True


class ResumeAnswerRequest(BaseModel):
    questions: list[str]
    use_ai: bool = True


class ResumeSummary(BaseModel):
    saved_resume_id: str
    created_at: str
    profile_id: str
    profile_name: str
    job_id: str
    company: str
    job_title: str
    target_role: str
    ats_score: int
    download_filename: str


class WeeklyReportResponse(BaseModel):
    week_start: str
    week_end: str
    applications_total: int
    schedules_total: int
    daily_counts: dict[str, int]
    daily_schedule_counts: dict[str, int]
    recent_applications: list[dict[str, Any]]


class DashboardResponse(BaseModel):
    user: UserSummary
    next_job: JobListItem | None
    open_jobs_count: int
    generated_resumes_count: int
    weekly_report: WeeklyReportResponse


class ResumeGenerateResponse(BaseModel):
    mode: str
    saved_resume_id: str | None = None
    ats_score: int
    target_role: str
    profile_id: str
    template_id: str
    resume: dict[str, Any]
    job_tech_analysis: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []


class DownloadFormatResponse(BaseModel):
    available_formats: list[str]


class WeekQuery(BaseModel):
    week_start: date | None = None
