# TailorResume — DOCX Style Resume Generator

TailorResume is a local Streamlit app for generating job-matched resume PDFs from an uploaded DOCX resume. The uploaded DOCX is the source of truth for layout and style. The app changes only allowed resume content and keeps the original document structure as much as possible.

## Current important behavior

- Resume generation is **DOCX-style based**.
- Each profile **must upload a DOCX resume** before it can be saved or used.
- If a profile has no uploaded resume, the app shows:

```text
no resume so must upload resume
```

- Generated output is **PDF only** for the user.
- The app may create a temporary DOCX internally, but the user workflow is PDF generation/download/preview.
- There is **no Template Settings page**. The uploaded DOCX replaces the old template system.
- Generated content should not add bold formatting by itself.
- Section titles, tables, separators, spacing, and outer layout should stay from the uploaded DOCX.

## Required project structure

`app.py` imports modules from `core`, so the project should be structured like this:

```text
project-folder/
  app.py
  .env
  requirements.txt
  core/
    __init__.py
    docx_resume_export.py
    resume_engine.py
    storage.py
  data/
    profiles.json
    users.json
    jobs.json
    settings.json
    generated_resumes.json
    profile_resumes/
```

If you received flat files from a ZIP, place these files into `core/`:

```text
core/docx_resume_export.py
core/resume_engine.py
core/storage.py
```

Then keep `app.py` in the project root.

## Install

Python 3.10+ is recommended.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Recommended `requirements.txt`:

```text
streamlit>=1.44.0
python-dotenv>=1.0.1
openai>=1.35.0
python-docx>=1.1.0
docx2pdf>=0.1.8
pywin32>=306; platform_system == "Windows"
PyMuPDF>=1.24.0
```

## Environment

Create `.env` in the project root:

```text
OPENAI_API_KEY=your_openai_api_key_here
```

If no API key is configured, the app can fall back to demo/local generation logic depending on the code path.

## Run

```bash
streamlit run app.py
```

Open the Streamlit URL shown in the terminal.

## Default admin login

```text
Username: admin
Password: admin123
```

Change this password after first login.

## Profile rules

A profile contains candidate information and the uploaded DOCX resume.

Important profile rules:

- Upload resume format: **DOCX only**.
- New profile cannot save without DOCX upload.
- Existing profile cannot be used for generation if the DOCX is missing.
- Uploaded resumes are stored under:

```text
data/profile_resumes/<profile_id>/
```

The app checks saved upload metadata and also falls back to scanning the profile resume folder so upload status survives reloads when the file exists.

## DOCX placeholder rules

The uploaded resume can contain placeholders. These placeholders are replaced during generation while preserving the DOCX style.

### Headline

```text
__headline__
```

Replaced with the generated resume headline only.

Also supported:

```text
___headline___
__resume_title__
___resume_title___
```

### Experience role/title

```text
__role__
```

Replaced with generated experience `role_title` values in order.

Also supported:

```text
___title___
```

If the DOCX does not contain `__role__` or `___title___`, existing role/title text should not be changed.

## Role and duration one-line rule

Use `__role__` plus **normal keyboard spaces** plus right-side text plus `|`. The `|` marks the end of the role/date alignment area and is removed from the generated output.

Example template line:

```text
__role__                                      Mar 2024 - Present|
```

Generated line:

```text
Senior JavaScript Software Engineer          Mar 2024 - Present
```

Important:

- `|` is removed from the output.
- Left side is generated role/title.
- Right side is duration or any text before `|`.
- Long role -> spaces reduce.
- Short role -> spaces increase.
- Same-length role -> spaces stay the same.
- The role/date line must use **normal spaces only** between `__role__` and the right-side text.
- Do **not** use Tab, Shift+Tab, Shift+Space, non-breaking spaces, text boxes, manual indents, or hidden alignment characters in the `__role__ ... |` line.

The spacing calculation is:

```text
role_slot_width = len("__role__") + number_of_normal_spaces_before_right_text
new_spaces = max(0, role_slot_width - len(generated_role))
output = generated_role + new_spaces + right_text
```

Example:

```text
Template: __role__          2020-2021|
Role slot: len("__role__") + 10 spaces = 18
Generated role: Java Engineer = 13
New spaces: 18 - 13 = 5
Output: Java Engineer     2020-2021
```

If a generated role is physically too long for the page width, Word/WPS may still wrap it. In that case, use one of these:

- shorter generated headline/role
- smaller font in the DOCX style
- wider role line area
- move duration to the next line

## Section detection

The exporter detects common resume sections, including table-based section titles.

Supported summary/profile section titles include:

```text
ABOUT ME
PROFILE
PROFESSIONAL PROFILE
PROFILE SUMMARY
PROFESSIONAL SUMMARY
CAREER PROFILE
CAREER SUMMARY
```

Supported experience/education/skills headings include common variants like:

```text
PROFESSIONAL EXPERIENCE
EXPERIENCE
WORK EXPERIENCE
EDUCATION
SKILLS
TECHNICAL SKILLS
```

For table-style resumes, section titles can be inside a table while content is outside the table. The exporter reads the DOCX in visual order so outside content is updated under the correct table title.

## Content replacement rules

Generated resume changes only these areas:

- headline/title placeholder
- summary/profile content
- skills content
- experience bullets/content
- experience role/title only when `__role__` or `___title___` is present

The app should not intentionally change:

- candidate name
- email
- phone
- location
- LinkedIn/portfolio
- company names unless generated content explicitly maps them
- dates/durations unless the DOCX placeholder area includes them as right-side text
- section title styling
- table borders/layout
- separators and decorative lines

## Skills formatting

Generated Skills content should be normal weight by default.

Even if the original DOCX skills paragraph had bold text, generated skills should not be forced bold unless the DOCX section title itself is bold. Section titles keep their style; generated content should remain plain.

## PDF generation on Windows + WPS

The app supports multiple PDF export backends. Configure this in **App Settings**:

```text
PDF backend order: docx2pdf, word, libreoffice, wps_custom
```

For WPS, configure a custom command:

```text
"C:\Path\to\wps_export.bat" "{input}" "{output}"
```

`{input}` is the temporary DOCX path. `{output}` is the PDF path to create.

Recommended Windows backends:

1. Microsoft Word / docx2pdf if Word is installed
2. WPS custom command if WPS is installed
3. LibreOffice if installed and available in PATH

## PDF preview

The preview should use image rendering first, not only browser PDF embedding.

Important behavior:

- PDF pages are rendered to images with PyMuPDF for reliable Streamlit preview.
- If image preview fails, the app can fall back to embedded PDF preview.
- If preview still fails, use the PDF download button and open the file directly with WPS/Adobe/Edge.

## Data storage

This app stores local JSON data in:

```text
data/
```

Main files:

```text
data/users.json
data/profiles.json
data/jobs.json
data/settings.json
data/generated_resumes.json
data/profile_resumes/
```

This is suitable for local/internal desktop use. For a public multi-user deployment, replace JSON storage with SQLite/PostgreSQL and harden authentication/session handling.

## Important limitations

DOCX/PDF layout is not the same as plain text layout.

Spacing can be affected by:

- Word/WPS paragraph alignment
- justified paragraphs
- tabs and tab stops
- separate DOCX runs
- table cell width
- font width
- page margins
- PDF conversion backend

For role/date lines, use this pattern with **normal spaces only**:

```text
__role__                                      Mar 2024 - Present|
```

Do not use tabs, Shift+Tab, Shift+Space, non-breaking spaces, manual indents, or text boxes in the `__role__ ... |` line. The exporter calculates the final gap from the number of normal spaces in this line.

## Troubleshooting

### Profile says no resume after reload

Check that the DOCX still exists in:

```text
data/profile_resumes/<profile_id>/
```

If the file was deleted or the app folder was moved incorrectly, upload the DOCX again.

### PDF preview is blank

Install/update PyMuPDF:

```bash
pip install PyMuPDF>=1.24.0
```

Then restart Streamlit.

### PDF generation fails on Windows + WPS

Use App Settings and configure `wps_custom` command. Confirm the command works from Command Prompt with real input/output files.

### Role/date line wraps

The generated role is too long for the physical width of the DOCX line. Reduce the role length, widen the line area, or decrease font size.

## Recommended resume template markers

Use this for headline:

```text
__headline__
```

Use this for each experience role line. Type the gap with **normal keyboard spaces only**:

```text
__role__                                      Jan 2020 - Dec 2021|
```

Do not use Tab, Shift+Tab, Shift+Space, or non-breaking spaces in this line.

Use normal section headings like:

```text
ABOUT ME
PROFESSIONAL EXPERIENCE
EDUCATION
SKILLS
```

The app will keep the DOCX style and replace the matching content areas.
