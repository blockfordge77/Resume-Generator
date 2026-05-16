"""Minimal job feed API — deploy this on the source VPS that owns jobs.json.

Start:
    uvicorn api_server.main:app --host 0.0.0.0 --port 8052

Auth: every request must include header  Authorization: Bearer spear_job_sync_2025
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_TOKEN = "spear_job_sync_2025"
JOBS_FILE = Path(__file__).resolve().parents[1] / "data" / "jobs.json"

app = FastAPI(title="Job Feed API", docs_url=None, redoc_url=None)


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_auth(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[len("Bearer "):] != API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def get_jobs_after(request: Request) -> JSONResponse:
    """Return all jobs that appear after the job with the given id in jobs.json.

    Body: { "id": "job_65fde3a9dd" }

    If id is missing, null, or not found the full list is returned (bootstrap).
    """
    _check_auth(request)

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass

    cursor_id: str = str(body.get("id") or "").strip()

    if not JOBS_FILE.exists():
        return JSONResponse({"jobs": [], "count": 0})

    jobs: list[dict] = json.loads(JOBS_FILE.read_text(encoding="utf-8"))

    if not cursor_id:
        return JSONResponse({"jobs": jobs, "count": len(jobs)})

    # Find the index of the cursor job
    cursor_index = next((i for i, j in enumerate(jobs) if j.get("id") == cursor_id), None)

    if cursor_index is None:
        # Unknown id — return everything so the caller can self-correct
        return JSONResponse({"jobs": jobs, "count": len(jobs)})

    result = jobs[cursor_index + 1:]
    return JSONResponse({"jobs": result, "count": len(result)})
