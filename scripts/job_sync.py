"""PM2 poller — fetches new jobs from the remote API and saves them to app.db.

Runs as a persistent loop: fetch → sleep 1 hour → repeat.

Usage (PM2):
    pm2 start scripts/job_sync.py --name job-sync --interpreter python3
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "app.db"
LOG_PATH = PROJECT_ROOT / "data" / "job_sync.log"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JOB_API_URL = "http://69.169.109.18:8052/api/jobs"
API_TOKEN = "spear_job_sync_2025"
SLEEP_SECONDS = 3600  # 1 hour

BOOTSTRAP_CURSOR = "job_65fde3a9dd"
CURSOR_SETTINGS_KEY = "job_sync_cursor"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [job_sync] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("job_sync")

# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _get_cursor() -> str:
    from core.db import session_scope
    from core.db.models import SettingsRow

    with session_scope(DB_PATH) as session:
        row = session.get(SettingsRow, CURSOR_SETTINGS_KEY)
        if row and row.data:
            return str(row.data.get("last_id") or "").strip()
    return BOOTSTRAP_CURSOR


def _save_cursor(last_id: str) -> None:
    from core.db import session_scope
    from core.db.models import SettingsRow

    with session_scope(DB_PATH) as session:
        session.merge(SettingsRow(key=CURSOR_SETTINGS_KEY, data={"last_id": last_id}))


def _upsert_jobs(jobs: list[dict]) -> int:
    from core.storage import Storage

    store = Storage(PROJECT_ROOT / "data")
    return store.bulk_upsert_jobs(jobs)


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------

def _fetch_jobs(cursor_id: str) -> list[dict]:
    payload = json.dumps({"id": cursor_id}).encode("utf-8")
    req = urllib.request.Request(
        JOB_API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("jobs") or []


# ---------------------------------------------------------------------------
# Single sync cycle
# ---------------------------------------------------------------------------

def _run_once() -> None:
    log.info("sync started — %s", datetime.now(timezone.utc).isoformat())

    cursor = _get_cursor()
    log.info("cursor: %s", cursor)

    try:
        jobs = _fetch_jobs(cursor)
    except urllib.error.URLError as exc:
        log.error("fetch failed: %s", exc)
        return
    except Exception as exc:
        log.error("unexpected fetch error: %s", exc)
        return

    if not jobs:
        log.info("no new jobs")
        return

    jobs = [j for j in jobs if str(j.get("company") or "").strip() and str(j.get("job_title") or "").strip()]

    if not jobs:
        log.info("all fetched jobs had empty company or job_title — skipping")
        return

    log.info("fetched %d new job(s)", len(jobs))

    try:
        saved = _upsert_jobs(jobs)
        log.info("upserted %d job(s) into app.db", saved)
    except Exception as exc:
        log.error("db write failed: %s", exc)
        return

    last_id = str(jobs[-1].get("id") or "").strip()
    if last_id:
        _save_cursor(last_id)
        log.info("cursor advanced to %s", last_id)

    log.info("sync done")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("job_sync process started — interval %ds", SLEEP_SECONDS)
    while True:
        try:
            _run_once()
        except Exception as exc:
            log.error("unhandled error in sync cycle: %s", exc)
        log.info("sleeping %ds until next sync", SLEEP_SECONDS)
        time.sleep(SLEEP_SECONDS)
