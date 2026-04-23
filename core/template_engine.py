from __future__ import annotations

import re
from html import escape

SECTION_LABELS = {
    "summary": "Professional Summary",
    "technical_skills": "Technical Skills",
    "work_history": "Professional Experience",
    "education_history": "Education",
}

SKILL_GROUP_FALLBACK = {
    "Languages": ["Python", "TypeScript", "JavaScript", "Go", "Java", "Kotlin", "PHP", "Ruby", "SQL"],
    "Frontend": ["React", "Next.js", "Vue", "Angular", "Tailwind", "HTML", "CSS", "Redux"],
    "Backend": ["FastAPI", "Flask", "Django", "Node.js", "Express", "NestJS", "REST APIs", "GraphQL", "Microservices"],
    "Data": ["PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "Kafka"],
    "Cloud / DevOps": ["Docker", "Kubernetes", "AWS", "GCP", "Azure", "CI/CD", "GitHub Actions", "Jenkins"],
    "Testing": ["Pytest", "Jest", "Playwright", "Cypress", "React Testing Library"],
    "AI / Automation": ["OpenAI API", "LLM", "RAG", "Prompt Engineering", "AI Agents"],
}


def render_resume_html(resume: dict, template: dict, profile: dict) -> str:
    section_order = template.get(
        "section_order",
        ["summary", "technical_skills", "work_history", "education_history"],
    )
    density = template.get("density", "normal")
    header_style = template.get("header_style", "rule")
    skill_style = template.get("skill_style", "grouped_bullets")
    layout_style = template.get("layout_style", "ats_classic")
    emphasis_keywords = _effective_bold_keywords(resume)

    sections = {
        "summary": _summary_section(resume, emphasis_keywords),
        "technical_skills": _skills_section(resume, skill_style, emphasis_keywords),
        "work_history": _work_history_section(resume, template, emphasis_keywords),
        "education_history": _education_section(resume, emphasis_keywords),
    }

    ordered_sections = "\n".join(sections[key] for key in section_order if key in sections)
    header_rule = (
        f"border-bottom: 2px solid {template.get('accent_color', '#1f4e79')}; padding-bottom: 12px;"
        if header_style == "rule"
        else "border-bottom: 0; padding-bottom: 8px;"
    )
    spacing = "20px" if density == "normal" else "14px"
    list_spacing = "6px" if density == "normal" else "3px"
    section_gap = "10px" if density == "normal" else "8px"
    font_family = template.get("font_family", "Arial, sans-serif")
    custom_css = template.get("custom_css", "")
    use_flat = layout_style in {"ats_classic", "ats_compact", "ats_technical"}

    meta_parts = [
        escape(profile.get("email", "")),
        escape(profile.get("phone", "")) if profile.get("phone") else "",
        escape(profile.get("location", "")) if profile.get("location") else "",
        escape(profile.get("linkedin", "")) if profile.get("linkedin") else "",
        escape(profile.get("portfolio", "")) if profile.get("portfolio") else "",
    ]
    meta_line = " | ".join(part for part in meta_parts if part)

    return f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{escape(profile.get('name', 'Resume'))}</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{
                font-family: {font_family};
                background: #ffffff;
                margin: 0;
                padding: 10px 12px;
                color: {template.get('text_color', '#111827')};
            }}
            .resume {{
                width: 100%;
                max-width: none;
                margin: 0 auto;
                background: {template.get('background_color', '#ffffff')};
                color: {template.get('text_color', '#111827')};
                padding: 14px 18px 18px;
                border-radius: {"0" if use_flat else "12px"};
                box-shadow: {"none" if use_flat else "0 10px 30px rgba(15, 23, 42, 0.08)"};
            }}
            .header {{
                {header_rule}
                margin-bottom: {spacing};
            }}
            .name {{
                font-size: 31px;
                font-weight: 800;
                letter-spacing: 0.2px;
                margin-bottom: 4px;
                text-align: center;
            }}
            .headline {{
                font-size: 18px;
                font-weight: 700;
                color: {template.get('accent_color', '#1f4e79')};
                margin-bottom: 6px;
                text-align: center;
            }}
            .meta {{
                color: {template.get('muted_color', '#4b5563')};
                font-size: 13px;
                line-height: 1.45;
                text-align: center;
                word-break: break-word;
            }}
            .section {{ margin-top: {spacing}; }}
            .section-title {{
                font-size: 13px;
                font-weight: 800;
                color: {template.get('accent_color', '#1f4e79')};
                letter-spacing: 0.9px;
                text-transform: uppercase;
                margin-bottom: {section_gap};
            }}
            .summary, .skill-line, .skill-group-item, .job-role, .job-headline, li, .edu-item {{
                text-align: justify;
                text-justify: inter-word;
            }}
            .summary, .skill-line {{
                font-size: 14px;
                line-height: 1.58;
                word-break: break-word;
            }}
            .chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
            .chip {{
                border: 1px solid rgba(15, 23, 42, 0.12);
                padding: 5px 10px;
                border-radius: 999px;
                font-size: 12px;
                background: #ffffff;
            }}
            .skill-groups {{ margin: 0; padding-left: 18px; }}
            .skill-group-item {{ font-size: 13px; line-height: 1.55; margin-bottom: {list_spacing}; }}
            .job {{ margin-bottom: 16px; }}
            .job-company-line {{
                font-size: 15px;
                line-height: 1.35;
                margin-bottom: 2px;
                word-break: break-word;
            }}
            .job-company {{ font-weight: 800; }}
            .job-meta-inline {{
                color: {template.get('muted_color', '#4b5563')};
                font-size: 12px;
                font-weight: 400;
            }}
            .job-role {{ font-weight: 700; margin-top: 1px; font-size: 13px; line-height: 1.35; }}
            .job-headline {{ color: {template.get('muted_color', '#4b5563')}; font-style: italic; font-size: 12px; margin-top: 2px; line-height: 1.4; }}
            ul {{ margin: 7px 0 0 18px; padding: 0; }}
            li {{ margin-bottom: {list_spacing}; line-height: 1.52; font-size: 13px; }}
            .edu-item {{ margin-bottom: 10px; line-height: 1.55; font-size: 13px; }}
            .edu-meta, .muted {{ color: {template.get('muted_color', '#4b5563')}; }}
            {custom_css}
        </style>
    </head>
    <body>
        <div class="resume">
            <div class="header">
                <div class="name">{escape(profile.get('name', ''))}</div>
                <div class="headline">{_highlight_html(resume.get('headline', ''), emphasis_keywords)}</div>
                <div class="meta">{meta_line}</div>
            </div>
            {ordered_sections}
        </div>
    </body>
    </html>
    """


def render_resume_markdown(resume: dict, profile: dict) -> str:
    emphasis_keywords = _effective_bold_keywords(resume)
    lines = [
        f"# {profile.get('name', '')}",
        f"**{_highlight_markdown(resume.get('headline', ''), emphasis_keywords)}**",
        "",
        " | ".join(
            item
            for item in [
                profile.get("email", ""),
                profile.get("phone", ""),
                profile.get("location", ""),
                profile.get("linkedin", ""),
                profile.get("portfolio", ""),
            ]
            if item
        ),
        "",
        "## Professional Summary",
        _highlight_markdown(resume.get("summary", ""), emphasis_keywords),
        "",
        "## Technical Skills",
    ]

    for group in _resolve_skill_groups(resume):
        lines.append(f"- **{group['category']}:** {_highlight_markdown(', '.join(group['items']), emphasis_keywords)}")
    lines.extend(["", "## Professional Experience"])

    for job in resume.get("work_history", []):
        header_meta = " | ".join(item for item in [job.get("duration", ""), job.get("location", "")] if item)
        lines.extend(
            [
                f"### {job.get('company_name', '')}{' | ' + header_meta if header_meta else ''}",
                _highlight_markdown(job.get("role_title", ""), emphasis_keywords),
            ]
        )
        if job.get("role_headline"):
            lines.append(f"_{_highlight_markdown(job.get('role_headline', ''), emphasis_keywords)}_")
        for bullet in job.get("bullets", []):
            lines.append(f"- {_highlight_markdown(bullet, emphasis_keywords)}")
        lines.append("")

    lines.append("## Education")
    for item in resume.get("education_history", []):
        lines.append(
            f"- {item.get('university', '')} | {item.get('degree', '')} | {item.get('duration', '')} | {item.get('location', '')}"
        )
    lines.append("")
    return "\n".join(lines)


def _section_wrapper(section_key: str, body: str) -> str:
    return f"""
    <div class="section">
        <div class="section-title">{SECTION_LABELS[section_key]}</div>
        {body}
    </div>
    """


def _summary_section(resume: dict, emphasis_keywords: list[str]) -> str:
    return _section_wrapper(
        "summary",
        f'<div class="summary">{_highlight_html(resume.get("summary", ""), emphasis_keywords)}</div>',
    )


def _skills_section(resume: dict, skill_style: str, emphasis_keywords: list[str]) -> str:
    skills = [_highlight_html(skill, emphasis_keywords) for skill in resume.get("technical_skills", [])]
    if skill_style == "chips":
        body = '<div class="chips">' + ''.join(f'<span class="chip">{item}</span>' for item in skills) + "</div>"
    elif skill_style == "pipe":
        body = f'<div class="skill-line">{" | ".join(skills)}</div>'
    elif skill_style in {"grouped", "grouped_bullets"}:
        groups = _resolve_skill_groups(resume)
        if skill_style == "grouped_bullets":
            body = '<ul class="skill-groups">' + ''.join(
                f'<li class="skill-group-item"><strong>{escape(group["category"])}:</strong> {_highlight_html(", ".join(group["items"]), emphasis_keywords)}</li>'
                for group in groups
                if group.get("items")
            ) + '</ul>'
        else:
            body = ''.join(
                f'<div class="skill-line"><strong>{escape(group["category"])}:</strong> {_highlight_html(", ".join(group["items"]), emphasis_keywords)}</div>'
                for group in groups
                if group.get("items")
            )
    else:
        body = f'<div class="skill-line">{_highlight_html(", ".join(resume.get("technical_skills", [])), emphasis_keywords)}</div>'
    return _section_wrapper("technical_skills", body)


def _work_history_section(resume: dict, template: dict, emphasis_keywords: list[str]) -> str:
    show_role_headline = bool(template.get("show_role_headline", True))
    jobs_html = []
    for job in resume.get("work_history", []):
        bullets_html = "".join(f"<li>{_highlight_html(bullet, emphasis_keywords)}</li>" for bullet in job.get("bullets", []))
        meta_parts = [escape(part) for part in [job.get("duration", ""), job.get("location", "")] if str(part).strip()]
        meta_right = f' <span class="job-meta-inline">| {" | ".join(meta_parts)}</span>' if meta_parts else ""
        role_headline = (
            f'<div class="job-headline">{_highlight_html(job.get("role_headline", ""), emphasis_keywords)}</div>'
            if show_role_headline and job.get("role_headline")
            else ""
        )
        jobs_html.append(
            f"""
            <div class="job">
                <div class="job-company-line"><span class="job-company">{escape(job.get('company_name', ''))}</span>{meta_right}</div>
                <div class="job-role">{_highlight_html(job.get('role_title', ''), emphasis_keywords)}</div>
                {role_headline}
                <ul>{bullets_html}</ul>
            </div>
            """
        )
    return _section_wrapper("work_history", "".join(jobs_html))


def _education_section(resume: dict, emphasis_keywords: list[str]) -> str:
    rows = []
    for item in resume.get("education_history", []):
        meta_line = " | ".join(
            escape(part)
            for part in [item.get("duration", ""), item.get("location", "")]
            if str(part).strip()
        )
        rows.append(
            f'<div class="edu-item"><strong>{escape(item.get("university", ""))}</strong><br/>{_highlight_html(item.get("degree", ""), emphasis_keywords)}<br/><span class="edu-meta">{meta_line}</span></div>'
        )
    return _section_wrapper("education_history", "".join(rows))


def _resolve_skill_groups(resume: dict) -> list[dict]:
    groups = resume.get("skill_groups") or []
    normalized = []
    for group in groups:
        category = str(group.get("category", "")).strip()
        items = [str(item).strip() for item in group.get("items", []) if str(item).strip()]
        if category and items:
            normalized.append({"category": category, "items": items})
    if normalized:
        return normalized
    return _group_skills(resume.get("technical_skills", []))


def _group_skills(skills: list[str]) -> list[dict]:
    lookup = {skill.lower(): skill for skill in skills}
    grouped: list[dict] = []
    used: set[str] = set()
    for group, candidates in SKILL_GROUP_FALLBACK.items():
        items = []
        for candidate in candidates:
            if candidate.lower() in lookup and lookup[candidate.lower()] not in used:
                items.append(lookup[candidate.lower()])
                used.add(lookup[candidate.lower()])
        if items:
            grouped.append({"category": group, "items": items})
    extras = [skill for skill in skills if skill not in used]
    if extras:
        grouped.append({"category": "Other Relevant", "items": extras})
    return grouped


def _effective_bold_keywords(resume: dict) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    manual = resume.get("bold_keywords", []) or []
    auto_keywords = resume.get("fit_keywords", []) if resume.get("auto_bold_fit_keywords", True) else []
    for item in [*manual, *auto_keywords]:
        clean = str(item).strip()
        if not clean:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(clean)
    return ordered


def _highlight_html(text: str, keywords: list[str]) -> str:
    if not keywords:
        return escape(text)
    parts = []
    last = 0
    pattern = _keyword_pattern(keywords)
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last:
            parts.append(escape(text[last:start]))
        parts.append(f"<strong>{escape(text[start:end])}</strong>")
        last = end
    if last < len(text):
        parts.append(escape(text[last:]))
    return "".join(parts)


def _highlight_markdown(text: str, keywords: list[str]) -> str:
    if not keywords:
        return text
    parts = []
    last = 0
    pattern = _keyword_pattern(keywords)
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last:
            parts.append(text[last:start])
        parts.append(f"**{text[start:end]}**")
        last = end
    if last < len(text):
        parts.append(text[last:])
    return "".join(parts)


def _keyword_pattern(keywords: list[str]) -> re.Pattern[str]:
    ordered = sorted({item.strip() for item in keywords if str(item).strip()}, key=len, reverse=True)
    if not ordered:
        return re.compile(r"(?!x)x")
    escaped_terms = [re.escape(item) for item in ordered]
    return re.compile(rf"(?<![A-Za-z0-9])(?:{'|'.join(escaped_terms)})(?![A-Za-z0-9])", re.IGNORECASE)
