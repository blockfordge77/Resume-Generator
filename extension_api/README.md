# Extension API

This folder adds a separate **user-focused API layer** for browser extensions or side-panel clients.
It is intentionally isolated from the existing Streamlit app so the original app keeps working the same way.

## What it does

It reuses the same:
- `core/storage.py`
- `core/resume_engine.py`
- `core/export_engine.py`
- shared `data/` directory

So both the Streamlit app and the extension API can work **at the same time** against the same project data.

## Run locally

From the project root:

### Windows

```bat
start_extension_api.bat
```

### macOS / Linux

```bash
./start_extension_api.sh
```

Default URL:

```text
http://127.0.0.1:8010
```

## Auth

Use the same app credentials.

- `POST /api/ext/auth/login`
- `POST /api/ext/auth/logout`
- `GET /api/ext/auth/me`

The API issues and reuses the same auth token logic already present in `core/storage.py`.

## Main endpoints

- `GET /api/ext/dashboard`
- `GET /api/ext/jobs`
- `GET /api/ext/jobs/next`
- `GET /api/ext/jobs/{job_id}`
- `POST /api/ext/jobs/report`
- `GET /api/ext/weekly-report`
- `POST /api/ext/resumes/generate`
- `GET /api/ext/resumes`
- `GET /api/ext/resumes/{saved_resume_id}`
- `GET /api/ext/resumes/{saved_resume_id}/download?fmt=pdf`
- `POST /api/ext/resumes/{saved_resume_id}/answers`

## Design goal

This is a **new sub-feature**.
It does not replace the existing app UI and does not require changing the Streamlit flow.
