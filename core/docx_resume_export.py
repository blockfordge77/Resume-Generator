from __future__ import annotations

import base64
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Iterable, List, Tuple

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.table import Table
from docx.enum.text import WD_ALIGN_PARAGRAPH


SECTION_ALIASES = {
    "summary": {
        "summary",
        "professional summary",
        "profile",
        "about",
        "about me",
        "professional profile",
        "profile summary",
        "career profile",
        "career summary",
    },
    "skills": {
        "skills",
        "technical skills",
        "core skills",
        "core competencies",
        "technologies",
        "technology stack",
    },
    "experience": {
        "experience",
        "work experience",
        "professional experience",
        "employment history",
        "work history",
    },
    "education": {
        "education",
        "education history",
        "academic background",
    },
    "projects": {"projects", "selected projects"},
    "certifications": {"certifications", "certificates", "licenses"},
}
ALL_SECTION_TITLES = {title for titles in SECTION_ALIASES.values() for title in titles}
TITLE_PLACEHOLDERS = {"___resume_title___", "___headline___"}
EXP_ROLE_PLACEHOLDERS = {"___title___", "__role__"}
SUMMARY_PLACEHOLDERS = {"___summary___", "___professional_summary___"}
SKILL_PLACEHOLDERS = {"___skills___", "___technical_skills___"}
EXPERIENCE_PLACEHOLDERS = {"___experience___", "___work_experience___", "___professional_experience___"}


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _strip_markdown_emphasis(value: object) -> str:
    """Remove generated Markdown emphasis; the uploaded DOCX owns styling."""
    text = str(value or "")
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(?![a-z_]+__)(.*?)__", r"\1", text)
    return text


def _plain_resume_text(value: object) -> str:
    return _clean_text(_strip_markdown_emphasis(value))


def _preserve_template_line_text(value: object) -> str:
    """Strip generated Markdown but keep spaces/tabs already in the template line."""
    return _strip_markdown_emphasis(value).replace("\r", "")


def _paragraph_contains_any_marker(paragraph: Paragraph, markers: set[str]) -> bool:
    text = paragraph.text or ""
    text_lower = text.lower()
    return any(marker.lower() in text_lower for marker in markers)


def _first_meaningful_run_or_none(paragraph: Paragraph):
    for run in paragraph.runs:
        if str(run.text or "").strip():
            return run
    return _first_run_or_none(paragraph)


def _paragraphs_in_container(container) -> Iterable[Paragraph]:
    """Yield paragraphs in the real DOCX visual order, including tables.

    python-docx exposes ``container.paragraphs`` and ``container.tables`` as two
    separate lists. Iterating paragraphs first and tables second breaks resumes
    where section headers live inside one-cell tables, such as:

        <table>ABOUT ME</table>
        <paragraph>summary text</paragraph>
        <table>PROFESSIONAL EXPERIENCE</table>

    In that layout, the section title must be discovered before the following
    outside-table content. This function walks the underlying OOXML children in
    order and recursively yields paragraphs from table cells at the exact table
    position, so section replacement can update content outside the title table
    without damaging the table styling.
    """
    # For Document, the useful ordered block container is document._body, not
    # the outer <w:document> root. For table cells/headers/footers, _element
    # already points at the correct block container.
    if hasattr(container, "_body"):
        parent_elm = getattr(container._body, "_element", None)
    else:
        parent_elm = getattr(container, "_element", None)
        if parent_elm is None:
            parent_elm = getattr(container, "element", None)

    if parent_elm is None:
        for paragraph in getattr(container, "paragraphs", []) or []:
            yield paragraph
        for table in getattr(container, "tables", []) or []:
            for row in table.rows:
                for cell in row.cells:
                    yield from _paragraphs_in_container(cell)
        return

    for child in parent_elm.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, container)
        elif child.tag == qn("w:tbl"):
            table = Table(child, container)
            seen_cells: set[int] = set()
            for row in table.rows:
                for cell in row.cells:
                    # Merged cells can appear more than once in python-docx.
                    cell_key = id(cell._tc)
                    if cell_key in seen_cells:
                        continue
                    seen_cells.add(cell_key)
                    yield from _paragraphs_in_container(cell)


def _all_story_paragraphs(doc: Document) -> list[Paragraph]:
    paragraphs = list(_paragraphs_in_container(doc))
    seen_parts = {id(doc.part)}
    for section in doc.sections:
        for container in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            try:
                part_id = id(container.part)
            except Exception:
                continue
            if part_id in seen_parts:
                continue
            seen_parts.add(part_id)
            paragraphs.extend(list(_paragraphs_in_container(container)))
    return paragraphs


def _all_body_paragraphs(doc: Document) -> list[Paragraph]:
    return list(_paragraphs_in_container(doc))


def _first_run_or_none(paragraph: Paragraph):
    return paragraph.runs[0] if paragraph.runs else None


def _clear_paragraph_keep_ppr(paragraph: Paragraph) -> None:
    for child in list(paragraph._p):
        if child.tag != qn("w:pPr"):
            paragraph._p.remove(child)


def _copy_run_format(source_run, target_run) -> None:
    if source_run is None:
        return
    try:
        if source_run._r.rPr is not None:
            target_run._r.insert(0, deepcopy(source_run._r.rPr))
    except Exception:
        pass


def _run_is_effectively_bold(run) -> bool:
    """Detect bold from direct formatting or character styles.

    Austin-style DOCX files store skill words with the Strong character style
    (sometimes exposed as a numeric style id such as 13), not always as
    run.bold=True. This helper lets generated replacement skills inherit that
    bold content style instead of becoming plain text.
    """
    if run is None:
        return False
    try:
        if run.bold is True:
            return True
    except Exception:
        pass
    try:
        style_name = str(getattr(getattr(run, "style", None), "name", "") or "").strip().lower()
        if "strong" in style_name or "bold" in style_name:
            return True
    except Exception:
        pass
    try:
        rpr = run._r.rPr
        if rpr is not None:
            bold = rpr.find(qn("w:b"))
            if bold is not None and str(bold.get(qn("w:val"), "1")).lower() not in {"0", "false", "off", "none"}:
                return True
            rstyle = rpr.find(qn("w:rStyle"))
            style_id = str(rstyle.get(qn("w:val"), "") if rstyle is not None else "").strip().lower()
            # WPS can preserve Strong as style id 13 in exported DOCX.
            if style_id in {"strong", "bold", "13"}:
                return True
    except Exception:
        pass
    return False


def _paragraph_uses_bold(paragraph: Paragraph) -> bool:
    for run in paragraph.runs:
        if str(run.text or "").strip() and _run_is_effectively_bold(run):
            return True
    return False


def _paragraphs_use_bold(paragraphs: Iterable[Paragraph]) -> bool:
    return any(_paragraph_uses_bold(paragraph) for paragraph in paragraphs)


def _force_run_bold(run) -> None:
    try:
        run.bold = True
    except Exception:
        pass
    try:
        rpr = run._r.get_or_add_rPr()
        if rpr.find(qn("w:b")) is None:
            rpr.append(OxmlElement("w:b"))
        if rpr.find(qn("w:bCs")) is None:
            rpr.append(OxmlElement("w:bCs"))
    except Exception:
        pass


def _force_run_not_bold(run) -> None:
    """Force a generated run to render as non-bold.

    Removing ``<w:b>`` is not enough. If the uploaded DOCX uses a bold
    paragraph/character style for the sample Skills text, deleting direct bold
    formatting lets Word/WPS inherit bold again from that style. For generated
    Skills content we need an explicit false bold override: ``<w:b w:val="0"/>``
    and ``<w:bCs w:val="0"/>``.
    """
    try:
        run.bold = False
    except Exception:
        pass
    try:
        # Clear character style such as Strong/Bold. This matters because WPS
        # can store bold as a character style even when run.bold is None.
        run.style = None
    except Exception:
        try:
            run.style = "Default Paragraph Font"
        except Exception:
            pass
    try:
        rpr = run._r.get_or_add_rPr()
        for tag in ("w:rStyle", "w:b", "w:bCs"):
            for node in list(rpr.findall(qn(tag))):
                rpr.remove(node)
        b = OxmlElement("w:b")
        b.set(qn("w:val"), "0")
        rpr.append(b)
        bcs = OxmlElement("w:bCs")
        bcs.set(qn("w:val"), "0")
        rpr.append(bcs)
    except Exception:
        pass


def _force_paragraph_not_bold(paragraph: Paragraph) -> None:
    """Force all text in a paragraph to normal weight.

    This is used only for generated Skills-section content. It also writes
    false bold overrides into the paragraph's default run properties because
    some DOCX templates make an entire skills paragraph bold through pPr/rPr or
    a paragraph style, not through individual runs.
    """
    try:
        ppr = paragraph._p.get_or_add_pPr()
        rpr = ppr.find(qn("w:rPr"))
        if rpr is None:
            rpr = OxmlElement("w:rPr")
            ppr.append(rpr)
        for tag in ("w:rStyle", "w:b", "w:bCs"):
            for node in list(rpr.findall(qn(tag))):
                rpr.remove(node)
        b = OxmlElement("w:b")
        b.set(qn("w:val"), "0")
        rpr.append(b)
        bcs = OxmlElement("w:bCs")
        bcs.set(qn("w:val"), "0")
        rpr.append(bcs)
    except Exception:
        pass
    for run in paragraph.runs:
        _force_run_not_bold(run)


def _force_skills_section_not_bold(doc: Document) -> None:
    """Make generated Skills body content normal-weight, regardless of template.

    Section titles keep their original styling. Only paragraphs between the
    Skills heading and the next section heading are sanitized.
    """
    paragraphs = _all_body_paragraphs(doc)
    section_range = _find_section_range(paragraphs, "skills")
    if not section_range:
        return
    start, end = section_range
    for paragraph in paragraphs[start + 1:end]:
        if _clean_text(paragraph.text) and not _is_decorative_or_blank_paragraph(paragraph):
            _force_paragraph_not_bold(paragraph)


def _coerce_skill_groups(raw_groups) -> list[dict]:
    """Normalize generated skill_groups into [{category, items}].

    The generator should return a list, but sometimes a result can contain a
    JSON string like '[{"category":"Frontend","items":["React"]}]'. The DOCX
    exporter must never print that raw JSON block into the resume. It renders
    clean category lines. Skills-section content is never bolded.
    """
    if isinstance(raw_groups, str):
        text = raw_groups.strip()
        if not text:
            return []
        try:
            raw_groups = json.loads(text)
        except Exception:
            return []

    if isinstance(raw_groups, dict):
        normalized: list[dict] = []
        for category, items in raw_groups.items():
            if isinstance(items, str):
                items = [part.strip() for part in items.split(",") if part.strip()]
            if isinstance(items, (list, tuple, set)):
                clean_items = [_plain_resume_text(item) for item in items if _plain_resume_text(item)]
                if clean_items:
                    normalized.append({"category": _plain_resume_text(category), "items": clean_items})
        return normalized

    if not isinstance(raw_groups, (list, tuple)):
        return []

    normalized: list[dict] = []
    for group in raw_groups:
        if not isinstance(group, dict):
            continue
        category = _plain_resume_text(group.get("category", ""))
        items = group.get("items", []) or []
        if isinstance(items, str):
            items = [part.strip() for part in items.split(",") if part.strip()]
        if not isinstance(items, (list, tuple, set)):
            continue
        clean_items = [_plain_resume_text(item) for item in items if _plain_resume_text(item)]
        if category and clean_items:
            normalized.append({"category": category, "items": clean_items})
    return normalized


def _coerce_technical_skills(raw_skills) -> list[str]:
    if isinstance(raw_skills, str):
        text = raw_skills.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            raw_skills = parsed if isinstance(parsed, list) else raw_skills
        except Exception:
            raw_skills = [part.strip() for part in text.split(",") if part.strip()]
    if not isinstance(raw_skills, (list, tuple, set)):
        return []
    return [_plain_resume_text(item) for item in raw_skills if _plain_resume_text(item)]


def _technical_skill_keywords(resume: dict) -> list[str]:
    """Return only generated technical_skills terms for keyword bolding.

    skill_groups is the visible Skills-section content. It must stay normal
    weight in the generated DOCX/PDF, even when its item names are also present
    in technical_skills. The only places where technical_skills terms are bolded
    are Summary and Experience bullets.

    Rules:
    - Skills section/skill_groups output: no generated bold at all.
    - Summary: bold only exact terms from technical_skills.
    - Experience bullets: bold only exact terms from technical_skills.
    - Do not use skill_groups, fit_keywords, or uploaded template bold style as
      a bold source.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    raw_skills: list[str] = _coerce_technical_skills(resume.get("technical_skills", []) or [])

    for item in raw_skills:
        clean = _plain_resume_text(item)
        if len(clean) < 2:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            ordered.append(clean)
    return ordered


def _effective_bold_keywords(resume: dict) -> list[str]:
    # Backward-compatible alias. DOCX export now intentionally ignores
    # fit_keywords/bold_keywords and uses only generated technical_skills.
    return _technical_skill_keywords(resume)

def _keyword_pattern(keywords: list[str]) -> re.Pattern[str]:
    ordered = sorted({str(item).strip() for item in keywords if str(item).strip()}, key=len, reverse=True)
    if not ordered:
        return re.compile(r"(?!x)x")
    escaped_terms = [re.escape(item) for item in ordered]
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(escaped_terms) + r")(?![A-Za-z0-9])", re.IGNORECASE)


def _add_generated_run(paragraph: Paragraph, text: str, source_run=None, *, force_no_bold: bool = False, force_bold: bool = False):
    run = paragraph.add_run(_preserve_template_line_text(text))
    _copy_run_format(source_run, run)
    if force_no_bold or force_bold:
        _force_run_not_bold(run)
    if force_bold:
        _force_run_bold(run)
    return run


def _add_text_with_keyword_bold(paragraph: Paragraph, text: str, source_run=None, keywords: list[str] | None = None, *, force_no_bold: bool = False) -> None:
    text = _preserve_template_line_text(text)
    keywords = keywords or []
    if not keywords:
        _add_generated_run(paragraph, text, source_run, force_no_bold=force_no_bold)
        return
    pattern = _keyword_pattern(keywords)
    last = 0
    for match in pattern.finditer(text):
        start, end = match.span()
        if start > last:
            _add_generated_run(paragraph, text[last:start], source_run, force_no_bold=True)
        _add_generated_run(paragraph, text[start:end], source_run, force_no_bold=True, force_bold=True)
        last = end
    if last < len(text):
        _add_generated_run(paragraph, text[last:], source_run, force_no_bold=True)

def _set_paragraph_text(paragraph: Paragraph, text: str, source_run=None, force_bold: bool = False, force_no_bold: bool = False, keywords: list[str] | None = None) -> None:
    source_run = source_run if source_run is not None else _first_run_or_none(paragraph)
    _clear_paragraph_keep_ppr(paragraph)
    if keywords:
        _add_text_with_keyword_bold(paragraph, text, source_run, keywords, force_no_bold=True)
        return
    run = paragraph.add_run(_preserve_template_line_text(text))
    _copy_run_format(source_run, run)
    if force_no_bold:
        _force_run_not_bold(run)
    elif force_bold:
        _force_run_bold(run)


def _insert_paragraph_after(paragraph: Paragraph, text: str, like_paragraph: Paragraph | None = None, force_bold: bool = False, force_no_bold: bool = False, keywords: list[str] | None = None) -> Paragraph:
    like_paragraph = like_paragraph or paragraph
    new_p = OxmlElement("w:p")
    if like_paragraph._p.pPr is not None:
        new_p.append(deepcopy(like_paragraph._p.pPr))
    paragraph._p.addnext(new_p)
    new_paragraph = Paragraph(new_p, paragraph._parent)
    source_run = _first_run_or_none(like_paragraph)
    if keywords:
        _add_text_with_keyword_bold(new_paragraph, text, source_run, keywords, force_no_bold=True)
        return new_paragraph
    run = new_paragraph.add_run(_preserve_template_line_text(text))
    _copy_run_format(source_run, run)
    if force_no_bold:
        _force_run_not_bold(run)
    elif force_bold:
        _force_run_bold(run)
    return new_paragraph


def _delete_paragraph(paragraph: Paragraph) -> None:
    parent = paragraph._element.getparent()
    if parent is not None:
        parent.remove(paragraph._element)


def _replace_paragraph_with_lines(paragraph: Paragraph, lines: list[str], force_bold: bool = False, force_no_bold: bool = False, keywords: list[str] | None = None) -> None:
    lines = [str(line or "").strip() for line in lines if str(line or "").strip()]
    if not lines:
        _set_paragraph_text(paragraph, "", force_bold=force_bold, force_no_bold=force_no_bold)
        return
    source_run = _first_run_or_none(paragraph)
    _set_paragraph_text(paragraph, lines[0], source_run, force_bold=force_bold, force_no_bold=force_no_bold, keywords=keywords)
    cursor = paragraph
    for line in lines[1:]:
        cursor = _insert_paragraph_after(cursor, line, like_paragraph=paragraph, force_bold=force_bold, force_no_bold=force_no_bold, keywords=keywords)


def _normalized_heading_text(paragraph: Paragraph) -> str:
    text = _clean_text(paragraph.text).lower().strip(":")
    return text


def _is_section_heading(paragraph: Paragraph) -> bool:
    text = _normalized_heading_text(paragraph)
    if text in ALL_SECTION_TITLES:
        return True
    style_name = str(getattr(paragraph.style, "name", "") or "").lower()
    return bool(text) and "heading" in style_name and len(text.split()) <= 4


def _section_name(paragraph: Paragraph) -> str:
    text = _normalized_heading_text(paragraph)
    for key, aliases in SECTION_ALIASES.items():
        if text in aliases:
            return key
    return ""


def _find_section_range(paragraphs: list[Paragraph], section_key: str) -> tuple[int, int] | None:
    start = -1
    for idx, paragraph in enumerate(paragraphs):
        if _section_name(paragraph) == section_key:
            start = idx
            break
    if start < 0:
        return None
    end = len(paragraphs)
    for idx in range(start + 1, len(paragraphs)):
        if _is_section_heading(paragraphs[idx]):
            end = idx
            break
    return start, end


def _is_decorative_or_blank_paragraph(paragraph: Paragraph) -> bool:
    text = (paragraph.text or "").strip()
    if not text:
        return True
    return bool(re.fullmatch(r"[_\-—–=]{5,}", text))


def _replace_section_body(doc: Document, section_key: str, lines: list[str], keywords: list[str] | None = None) -> bool:
    paragraphs = _all_body_paragraphs(doc)
    section_range = _find_section_range(paragraphs, section_key)
    if not section_range:
        return False
    start, end = section_range
    body = paragraphs[start + 1:end]
    content_body = [p for p in body if _clean_text(p.text) and not _is_decorative_or_blank_paragraph(p)]
    # Generated Skills content should stay normal-weight. Some Austin/WPS DOCX
    # files use bold/Strong style for original skills text; do not copy that
    # bold character style into generated Skills content. Section titles keep
    # their existing table/paragraph styling because only body paragraphs change.
    force_bold = False
    force_no_bold = section_key == "skills"
    active_keywords = None if section_key == "skills" else keywords
    if content_body:
        anchor = content_body[0]
        for p in content_body[1:]:
            _delete_paragraph(p)
        _replace_paragraph_with_lines(anchor, lines, force_bold=force_bold, force_no_bold=force_no_bold, keywords=active_keywords)
    else:
        anchor = paragraphs[start]
        cursor = anchor
        for line in [line for line in lines if str(line).strip()]:
            cursor = _insert_paragraph_after(cursor, line, like_paragraph=anchor, force_bold=force_bold, force_no_bold=force_no_bold, keywords=active_keywords)
    return True


def _replace_placeholders(doc: Document, placeholders: set[str], lines: list[str], force_bold: bool | None = None, force_no_bold: bool = False, keywords: list[str] | None = None) -> bool:
    changed = False
    lowered = {item.lower() for item in placeholders}
    for paragraph in _all_story_paragraphs(doc):
        text = _clean_text(paragraph.text).lower()
        if text in lowered:
            paragraph_force_bold = _paragraph_uses_bold(paragraph) if force_bold is None else bool(force_bold)
            _replace_paragraph_with_lines(paragraph, lines, force_bold=paragraph_force_bold, force_no_bold=force_no_bold, keywords=keywords)
            changed = True
    return changed


def _replace_paragraph_inline_markers(paragraph: Paragraph, replacements: dict[str, str]) -> bool:
    """Replace markers inside a paragraph without rebuilding the whole line.

    This preserves same-line elements such as dates, tabs, spacing, and run
    formatting. If a marker is split across runs, we fall back to one run while
    keeping the paragraph style and exact line text.
    """
    if not replacements:
        return False

    cleaned_replacements = {marker: _plain_resume_text(value) for marker, value in replacements.items()}
    original = paragraph.text or ""
    desired = original
    for marker, value in cleaned_replacements.items():
        desired = re.sub(re.escape(marker), value, desired, flags=re.IGNORECASE)

    if desired == original:
        return False

    changed_in_runs = False
    for run in paragraph.runs:
        run_text = run.text or ""
        new_text = run_text
        for marker, value in cleaned_replacements.items():
            new_text = re.sub(re.escape(marker), value, new_text, flags=re.IGNORECASE)
        if new_text != run_text:
            run.text = _preserve_template_line_text(new_text)
            changed_in_runs = True

    if changed_in_runs and (paragraph.text or "") == desired:
        return True

    # Marker likely spans multiple runs. Keep the whole line exactly, including
    # tabs/spaces/date text, but use the first meaningful run as formatting.
    _set_paragraph_text(paragraph, desired, _first_meaningful_run_or_none(paragraph))
    return True


def _replace_inline_placeholders(doc: Document, replacements: dict[str, str]) -> bool:
    changed = False
    for paragraph in _all_story_paragraphs(doc):
        if _replace_paragraph_inline_markers(paragraph, replacements):
            changed = True
    return changed



_ROLE_MARKER_PATTERN = re.compile(r"(___title___|__role__)", re.IGNORECASE)


def _visual_width(value: str) -> int:
    """Plain character width for the role/date alignment rule.

    A tab is counted as one flexible gap character, not as a Word tab stop.
    The generated output removes tab jumps and writes only the recalculated
    number of normal spaces.
    """
    return len(str(value or ""))


def _normalize_flexible_gap(value: str) -> str:
    """Convert all gap whitespace to normal spaces before measuring."""
    return re.sub(r"[\t\u00a0 ]", " ", str(value or ""))


def _safe_line_text(value: object) -> str:
    text = _preserve_template_line_text(value)
    text = text.replace("\r", "").replace("\n", " ").replace("\t", " ")
    return text


def _set_keep_lines(paragraph: Paragraph) -> None:
    """Keep the role/date paragraph together without adding hidden spacing."""
    try:
        paragraph.paragraph_format.keep_together = True
    except Exception:
        pass
    try:
        ppr = paragraph._p.get_or_add_pPr()
        if ppr.find(qn("w:keepLines")) is None:
            ppr.append(OxmlElement("w:keepLines"))
    except Exception:
        pass


def _remove_existing_tabs(ppr) -> None:
    try:
        tabs = ppr.find(qn("w:tabs"))
        if tabs is not None:
            ppr.remove(tabs)
    except Exception:
        pass


def _content_width_twips(paragraph: Paragraph) -> int:
    """Return usable paragraph width in twips for a right tab stop."""
    try:
        section = paragraph.part.document.sections[0]
        return int(section.page_width.twips - section.left_margin.twips - section.right_margin.twips)
    except Exception:
        return 10466


def _set_role_line_right_tab(paragraph: Paragraph) -> None:
    """Make the role/duration placeholder line flex visually in Word/WPS/PDF.

    The pipe marker means: keep the right-side text on the same line and
    aligned to the right edge. Character-count spaces break once the generated
    role length changes, especially with justified paragraphs, so this removes
    the fixed-space behavior and sets a right tab stop instead. The user can
    still type spaces in the DOCX; during export `__role__ ... |` becomes
    `role<TAB>right_text`.
    """
    try:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    except Exception:
        pass
    try:
        ppr = paragraph._p.get_or_add_pPr()
        jc = ppr.find(qn("w:jc"))
        if jc is None:
            jc = OxmlElement("w:jc")
            ppr.append(jc)
        jc.set(qn("w:val"), "left")
        _remove_existing_tabs(ppr)
        tabs = OxmlElement("w:tabs")
        tab = OxmlElement("w:tab")
        tab.set(qn("w:val"), "right")
        tab.set(qn("w:pos"), str(_content_width_twips(paragraph)))
        tabs.append(tab)
        ppr.append(tabs)
    except Exception:
        pass


def _append_tab(paragraph: Paragraph, source_run=None) -> None:
    run = paragraph.add_run()
    _copy_run_format(source_run, run)
    try:
        run.add_tab()
    except Exception:
        run.text = "\t"


def _split_role_bounds(text: str):
    """Split a role placeholder line into bounded pieces.

    The controlled area starts at the first underscore of __role__/___title___
    and ends at the guide pipe '|'. The pipe is removed from output.

    Supported examples:
      __role__          2020-2021|  -> role + adjusted spaces + 2020-2021
      __role__          |2020-2021  -> role + adjusted spaces + 2020-2021
    """
    match = _ROLE_MARKER_PATTERN.search(text or "")
    if not match:
        return None

    pipe_abs = text.find("|", match.end())
    if pipe_abs < 0:
        return {
            "match": match,
            "before": text[:match.start()],
            "marker": match.group(0),
            "gap": "",
            "right_text": text[match.end():],
            "right_start": match.end(),
            "after": "",
            "has_pipe": False,
        }

    bounded = text[match.end():pipe_abs]
    gap_match = re.match(r"([ \t\u00a0]*)(.*)", bounded, flags=re.DOTALL)
    gap = gap_match.group(1) if gap_match else ""
    right_text = gap_match.group(2) if gap_match else bounded

    if right_text:
        # Template: __role__<gap>right_text|
        right_start = match.end() + len(gap)
        after = text[pipe_abs + 1:]
    else:
        # Template: __role__<gap>|right_text
        right_start = pipe_abs + 1
        right_text = text[right_start:]
        after = ""

    return {
        "match": match,
        "before": text[:match.start()],
        "marker": match.group(0),
        "gap": gap,
        "right_text": right_text,
        "right_start": right_start,
        "after": after,
        "has_pipe": True,
    }


def _role_gap_length(marker: str, original_gap: str, role: str) -> int:
    """Exact flexible spacing formula requested by the user.

    role_slot = marker + original spaces up to the right-side text / pipe.
    If generated role is longer, reduce spaces. If shorter, add spaces.
    If generated role is too long, use zero spaces.
    """
    gap = _normalize_flexible_gap(original_gap)
    return max(0, _visual_width(marker + gap) - _visual_width(role))


def _format_role_bounded_line(original: str, role_value: str) -> str:
    text = str(original or "").replace("\r", "")
    parts = _split_role_bounds(text)
    if not parts:
        return text

    role = _safe_line_text(role_value).strip()
    if not role:
        return text

    if not parts["has_pipe"]:
        desired = text
        for item in EXP_ROLE_PLACEHOLDERS:
            desired = re.sub(re.escape(item), role, desired, flags=re.IGNORECASE)
        return desired.replace("\t", " ")

    gap_len = _role_gap_length(parts["marker"], parts["gap"], role)
    return (
        parts["before"]
        + role
        + (" " * gap_len)
        + _safe_line_text(parts["right_text"]).replace("|", "")
        + _safe_line_text(parts["after"]).replace("|", "")
    )


def _run_spans(paragraph: Paragraph) -> list[tuple[int, int, object]]:
    spans: list[tuple[int, int, object]] = []
    cursor = 0
    for run in paragraph.runs:
        text = run.text or ""
        start = cursor
        cursor += len(text)
        spans.append((start, cursor, run))
    return spans


def _run_at_index(paragraph: Paragraph, index: int):
    fallback = _first_meaningful_run_or_none(paragraph)
    for start, end, run in _run_spans(paragraph):
        if start <= index < end:
            return run
    return fallback


def _append_formatted_run(paragraph: Paragraph, text: str, source_run=None) -> None:
    if text == "":
        return
    run = paragraph.add_run(_safe_line_text(text))
    _copy_run_format(source_run, run)


def _replace_role_paragraph_bounded(paragraph: Paragraph, role_value: str) -> bool:
    """Replace one role placeholder and remove fixed Word/WPS tab jumps.

    This rebuilds the role paragraph as clean runs:
      before + role + calculated normal spaces + right-side text + after

    Rebuilding is intentional. It drops <w:tab/> runs that made the PDF show a
    long gap even when character-count math was correct.
    """
    original = paragraph.text or ""
    desired = _format_role_bounded_line(original, role_value)
    if desired == original:
        return False

    parts = _split_role_bounds(original)
    if not parts:
        _set_paragraph_text(paragraph, desired, _first_meaningful_run_or_none(paragraph))
        _set_keep_lines(paragraph)
        return True

    role = _safe_line_text(role_value).strip()
    if not role:
        return False

    if not parts["has_pipe"]:
        _set_paragraph_text(paragraph, desired, _run_at_index(paragraph, parts["match"].start()))
        _set_keep_lines(paragraph)
        return True

    role_run = _run_at_index(paragraph, parts["match"].start())
    right_run = _run_at_index(paragraph, parts["right_start"])
    before_run = _run_at_index(paragraph, 0)
    after_run = _run_at_index(paragraph, len(original) - 1)

    _clear_paragraph_keep_ppr(paragraph)
    _set_role_line_right_tab(paragraph)
    _append_formatted_run(paragraph, parts["before"], before_run)
    _append_formatted_run(paragraph, role, role_run)
    # Pipe-bounded role lines are visually flexible. A right tab keeps the
    # duration/meta text on one line while automatically reducing the gap for
    # long generated roles and increasing it for shorter generated roles.
    _append_tab(paragraph, role_run)
    _append_formatted_run(paragraph, str(parts["right_text"] or "").replace("|", ""), right_run)
    _append_formatted_run(paragraph, str(parts["after"] or "").replace("|", ""), after_run)
    _set_keep_lines(paragraph)
    return True


def _replace_role_placeholders(doc: Document, resume: dict) -> bool:
    """Replace __role__/___title___ placeholders using generated roles in order.

    The exact one-line rule is:
    - start at the first "_" of "__role__" / "___title___";
    - end at the next "|";
    - rebuild only that bounded field as generated role + adjusted spacing +
      the original right-side text;
    - remove the "|";
    - preserve everything outside that bounded field.
    """
    jobs = resume.get("work_history", []) or []
    role_values = [_plain_resume_text(job.get("role_title", "")) for job in jobs]
    role_values = [value for value in role_values if value]
    fallback_role = role_values[0] if role_values else _plain_resume_text(resume.get("headline", ""))
    if not fallback_role:
        return False

    # Use generated experience roles in document order for every role placeholder.
    # This handles role placeholders in headers/name lines too. If the template
    # has more placeholders than generated jobs, reuse the last generated role.
    role_index = 0
    changed = False

    for paragraph in _all_story_paragraphs(doc):
        if not _paragraph_contains_any_marker(paragraph, EXP_ROLE_PLACEHOLDERS):
            continue

        if role_values:
            role_value = role_values[min(role_index, len(role_values) - 1)]
            role_index += 1
        else:
            role_value = fallback_role

        if _replace_role_paragraph_bounded(paragraph, role_value):
            changed = True

    return changed


def _skill_lines(resume: dict) -> list[str]:
    groups = _coerce_skill_groups(resume.get("skill_groups", []) or resume.get("grouped_skills", []))
    lines: list[str] = []
    for group in groups:
        category = _plain_resume_text(group.get("category", ""))
        items = [_plain_resume_text(item) for item in group.get("items", []) if _plain_resume_text(item)]
        if category and items:
            # Clean display format. Entire skill_groups output stays normal-weight.
            lines.append(f"{category}: {', '.join(items)}")
    if lines:
        return lines

    skills = _coerce_technical_skills(resume.get("technical_skills", []) or [])
    if not skills:
        return []
    return [", ".join(skills)]


def _experience_lines(resume: dict) -> list[str]:
    lines: list[str] = []
    for job_index, job in enumerate(resume.get("work_history", []) or []):
        if job_index:
            lines.append("")
        meta = " | ".join(
            item for item in [
                _plain_resume_text(job.get("company_name", "")),
                _plain_resume_text(job.get("duration", "")),
                _plain_resume_text(job.get("location", "")),
            ] if item
        )
        if meta:
            lines.append(meta)
        role = _plain_resume_text(job.get("role_title", ""))
        if role:
            lines.append(role)
        for bullet in job.get("bullets", []) or []:
            bullet_text = _plain_resume_text(bullet)
            if bullet_text:
                lines.append(f"• {bullet_text}")
    return lines


def _paragraph_has_tab(paragraph: Paragraph) -> bool:
    return "\t" in (paragraph.text or "")


def _paragraph_has_numbering(paragraph: Paragraph) -> bool:
    try:
        ppr = paragraph._p.pPr
        return bool(ppr is not None and ppr.numPr is not None)
    except Exception:
        return False


def _is_bullet_paragraph(paragraph: Paragraph) -> bool:
    text = (paragraph.text or "").strip()
    if not text:
        return False
    if _paragraph_contains_any_marker(paragraph, EXP_ROLE_PLACEHOLDERS):
        return False
    if _paragraph_has_tab(paragraph):
        # Role/company/date/location rows often use tabs. They are not bullets.
        return False
    if text.endswith(":") and len(text.split()) <= 5:
        return False

    style_name = str(getattr(paragraph.style, "name", "") or "").lower()
    if "heading" in style_name:
        return False
    if text.startswith(("•", "-", "*", "‣", "◦")):
        return True
    if "bullet" in style_name:
        return True
    # WPS/Word sometimes marks real bullets as List Paragraph. Treat list-style
    # paragraphs as bullets only when they look like full sentence content.
    if "list" in style_name and len(text.split()) >= 6:
        return True
    if _paragraph_has_numbering(paragraph) and len(text.split()) >= 6:
        return True
    return False


def _replace_experience_bullets_and_titles(doc: Document, resume: dict) -> bool:
    paragraphs = _all_body_paragraphs(doc)
    section_range = _find_section_range(paragraphs, "experience")
    if not section_range:
        return False
    start, end = section_range
    body = paragraphs[start + 1:end]
    if not body:
        return False

    changed = False
    jobs = resume.get("work_history", []) or []
    # Role/title text is intentionally protected. It changes only when the
    # uploaded DOCX contains __role__ or ___title___, handled globally by
    # _replace_role_placeholders() so inline dates/spaces stay on the same line.

    bullet_groups: list[list[Paragraph]] = []
    current_group: list[Paragraph] = []
    for paragraph in body:
        if _is_bullet_paragraph(paragraph):
            current_group.append(paragraph)
        else:
            if current_group:
                bullet_groups.append(current_group)
                current_group = []
    if current_group:
        bullet_groups.append(current_group)

    if not bullet_groups:
        return changed

    for idx, group in enumerate(bullet_groups):
        if idx >= len(jobs):
            continue
        bullets = [_plain_resume_text(bullet) for bullet in jobs[idx].get("bullets", []) if _plain_resume_text(bullet)]
        if not bullets:
            continue
        first = group[0]
        for old_paragraph in group[1:]:
            _delete_paragraph(old_paragraph)
        keywords = _effective_bold_keywords(resume)
        _set_paragraph_text(first, bullets[0], keywords=keywords, force_no_bold=True)
        cursor = first
        for bullet in bullets[1:]:
            cursor = _insert_paragraph_after(cursor, bullet, like_paragraph=first, keywords=keywords, force_no_bold=True)
        changed = True

    return changed


def apply_resume_to_docx(docx_path: Path, resume: dict) -> None:
    doc = Document(str(docx_path))

    headline = _plain_resume_text(resume.get("headline", ""))
    summary = _plain_resume_text(resume.get("summary", ""))
    skills = _skill_lines(resume)
    experience = _experience_lines(resume)
    technical_skill_keywords = _technical_skill_keywords(resume)

    if headline:
        _replace_inline_placeholders(doc, {"___resume_title___": headline, "___headline___": headline, "__resume_title__": headline, "__headline__": headline})
    _replace_role_placeholders(doc, resume)
    if summary:
        if not _replace_placeholders(doc, SUMMARY_PLACEHOLDERS, [summary], force_bold=False, force_no_bold=True, keywords=technical_skill_keywords):
            _replace_section_body(doc, "summary", [summary], keywords=technical_skill_keywords)
    if skills:
        if not _replace_placeholders(doc, SKILL_PLACEHOLDERS, skills, force_bold=False, force_no_bold=True, keywords=None):
            _replace_section_body(doc, "skills", skills, keywords=None)
        # Skills content is always normal text. Do this after replacement so
        # paragraph styles, copied run styles, and WPS Strong styles cannot make
        # React/Next.js/etc. bold inside the Skills section.
        _force_skills_section_not_bold(doc)
    if experience:
        if not _replace_placeholders(doc, EXPERIENCE_PLACEHOLDERS, experience, force_bold=False, force_no_bold=True, keywords=technical_skill_keywords):
            _replace_experience_bullets_and_titles(doc, resume)

    doc.save(str(docx_path))


def find_soffice() -> str | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("soffice.exe"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/snap/bin/libreoffice",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def find_wps() -> str | None:
    candidates = [
        shutil.which("wps"),
        shutil.which("wps.exe"),
        r"C:\Program Files\WPS Office\office6\wps.exe",
        r"C:\Program Files (x86)\WPS Office\office6\wps.exe",
        r"C:\Program Files\Kingsoft\WPS Office\office6\wps.exe",
        r"C:\Program Files (x86)\Kingsoft\WPS Office\office6\wps.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def export_pdf_via_docx2pdf(docx_path: Path, pdf_path: Path) -> tuple[bool, str]:
    try:
        from docx2pdf import convert  # type: ignore
        convert(str(docx_path), str(pdf_path))
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True, "PDF created via docx2pdf"
        return False, "docx2pdf ran but no PDF file was created"
    except Exception as exc:
        return False, f"docx2pdf failed: {exc!r}"


def export_pdf_via_word(docx_path: Path, pdf_path: Path) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Word COM export is only supported on Windows"
    word = None
    doc = None
    try:
        import pythoncom  # type: ignore
        import win32com.client as win32  # type: ignore
        pythoncom.CoInitialize()
        word = win32.gencache.EnsureDispatch("Word.Application")
        word.Visible = False
        try:
            word.DisplayAlerts = 0
        except Exception:
            pass
        doc = word.Documents.Open(str(docx_path), ReadOnly=True, AddToRecentFiles=False, ConfirmConversions=False)
        doc.ExportAsFixedFormat(
            OutputFileName=str(pdf_path),
            ExportFormat=17,
            OpenAfterExport=False,
            OptimizeFor=0,
            CreateBookmarks=1,
            DocStructureTags=True,
            BitmapMissingFonts=True,
            UseISO19005_1=False,
        )
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True, "PDF created via Microsoft Word"
        return False, "Word export ran but no PDF file was created"
    except Exception as exc:
        return False, f"Word export failed: {exc!r}"
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        try:
            import pythoncom  # type: ignore
            pythoncom.CoUninitialize()
        except Exception:
            pass


def export_pdf_via_libreoffice(docx_path: Path, pdf_path: Path) -> tuple[bool, str]:
    soffice = find_soffice()
    if not soffice:
        return False, "LibreOffice not found"
    temp_dir = Path(tempfile.mkdtemp(prefix="lo_pdf_"))
    try:
        result = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(temp_dir), str(docx_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        generated = temp_dir / f"{docx_path.stem}.pdf"
        if generated.exists() and generated.stat().st_size > 0:
            shutil.copy2(generated, pdf_path)
            return True, "PDF created via LibreOffice"
        message = result.stderr.strip() or result.stdout.strip() or "LibreOffice conversion failed"
        return False, f"LibreOffice export failed: {message}"
    except Exception as exc:
        return False, f"LibreOffice export failed: {exc!r}"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def export_pdf_via_wps_custom(docx_path: Path, pdf_path: Path, pdf_cfg: dict) -> tuple[bool, str]:
    command_template = str(pdf_cfg.get("wps_pdf_command", "") or "").strip()
    if not command_template:
        if find_wps():
            return False, "WPS found, but no WPS custom PDF command is configured"
        return False, "WPS not found and no WPS custom PDF command is configured"
    command = command_template.format(input=str(docx_path), output=str(pdf_path))
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return True, "PDF created via WPS custom command"
        message = result.stderr.strip() or result.stdout.strip() or "WPS custom command failed"
        return False, f"WPS export failed: {message}"
    except Exception as exc:
        return False, f"WPS export failed: {exc!r}"


def default_backend_order() -> list[str]:
    if sys.platform == "win32":
        return ["docx2pdf", "word", "libreoffice", "wps_custom"]
    return ["libreoffice", "wps_custom", "docx2pdf", "word"]


def export_pdf(docx_path: Path, pdf_path: Path, pdf_cfg: dict | None = None) -> tuple[bool, str]:
    pdf_cfg = pdf_cfg or {}
    order = pdf_cfg.get("backend_order")
    if isinstance(order, str):
        order = [item.strip() for item in order.split(",") if item.strip()]
    if not isinstance(order, list) or not order:
        order = default_backend_order()
    backend_map = {
        "docx2pdf": lambda: export_pdf_via_docx2pdf(docx_path, pdf_path),
        "word": lambda: export_pdf_via_word(docx_path, pdf_path),
        "libreoffice": lambda: export_pdf_via_libreoffice(docx_path, pdf_path),
        "wps_custom": lambda: export_pdf_via_wps_custom(docx_path, pdf_path, pdf_cfg),
    }
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    messages: list[str] = []
    for backend in order:
        key = str(backend or "").strip().lower()
        fn = backend_map.get(key)
        if not fn:
            messages.append(f"{backend}: unknown backend")
            continue
        ok, message = fn()
        messages.append(f"{backend}: {message}")
        if ok:
            return True, message
    return False, " | ".join(messages)


def pdf_backend_status(pdf_cfg: dict | None = None) -> list[str]:
    pdf_cfg = pdf_cfg or {}
    lines = []
    try:
        from docx2pdf import convert  # noqa: F401
        lines.append("docx2pdf: OK - package available")
    except Exception as exc:
        lines.append(f"docx2pdf: NO - {exc!r}")
    lines.append("word: OK - Windows only; checked during export" if sys.platform == "win32" else "word: NO - not Windows")
    soffice = find_soffice()
    lines.append(f"libreoffice: OK - {soffice}" if soffice else "libreoffice: NO - not found")
    wps = find_wps()
    if str(pdf_cfg.get("wps_pdf_command", "") or "").strip():
        lines.append("wps_custom: OK - command configured")
    elif wps:
        lines.append(f"wps_custom: NO - WPS found at {wps}, but command is not configured")
    else:
        lines.append("wps_custom: NO - WPS not found")
    return lines


def _uploaded_resume_path(profile: dict) -> Path | None:
    upload = profile.get("uploaded_resume") if isinstance(profile.get("uploaded_resume"), dict) else {}
    path_value = str(upload.get("path", "") or upload.get("storage_path", "") or "").strip()
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    return path if path.exists() else None


def build_pdf_preview_html(pdf_bytes: bytes, message: str = "") -> str:
    if not pdf_bytes:
        return f"""
        <div style='font-family:Arial,sans-serif;padding:20px;border:1px solid #fca5a5;border-radius:12px;background:#fff1f2;color:#7f1d1d;'>
          <h3 style='margin-top:0;'>PDF preview is unavailable</h3>
          <p>{html.escape(message or 'The PDF exporter did not return a PDF file.')}</p>
        </div>
        """
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    return f"""
    <div style='font-family:Arial,sans-serif;'>
      <iframe title='Generated resume PDF preview' src='data:application/pdf;base64,{encoded}' style='width:100%;height:1120px;border:1px solid #e5e7eb;border-radius:12px;background:#fff;'></iframe>
      <p style='font-size:12px;color:#64748b;margin-top:8px;'>Read-only PDF preview generated from the uploaded DOCX style. {html.escape(message)}</p>
    </div>
    """


def build_docx_style_pdf_bundle(resume: dict, profile: dict, output_dir: Path | str, pdf_cfg: dict | None = None) -> dict[str, bytes | str]:
    source_docx = _uploaded_resume_path(profile)
    if not source_docx:
        raise FileNotFoundError("no resume so must upload resume")
    output_dir = Path(output_dir)
    temp_dir = Path(tempfile.mkdtemp(prefix="tailorresume_docx_pdf_"))
    try:
        working_docx = temp_dir / "styled_resume.docx"
        pdf_path = temp_dir / "styled_resume.pdf"
        shutil.copy2(source_docx, working_docx)
        apply_resume_to_docx(working_docx, resume)
        ok, message = export_pdf(working_docx, pdf_path, pdf_cfg or {})
        pdf_bytes = pdf_path.read_bytes() if ok and pdf_path.exists() else b""
        docx_bytes = working_docx.read_bytes()
        return {
            "pdf": pdf_bytes,
            "html": build_pdf_preview_html(pdf_bytes, message),
            "markdown": "",
            "docx": docx_bytes,
            "pdf_message": message,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
