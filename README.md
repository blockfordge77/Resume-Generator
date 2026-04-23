# Tailored Resume Studio v16

A local Streamlit app for ATS-focused resume generation, editable drafts, PDF/DOCX export, job-list management, and role-based access control.

## New in v16
- Login and access requests
- Admin-approved users only
- Role-based access control
- Admin-only profile, template, settings, and user-management pages
- Assigned profiles per user
- Shared job list with approved jobs visible to all users
- Pending job queue for admin review
- Batch job presave with background link scraping
- Generated resumes filtered per creator for non-admin users

## Bootstrap admin login
- Username: `admin`
- Password: `admin123`

Change it after the first login in **User Access**.

## Run
### Windows
Double-click `start.bat`

### macOS / Linux
```bash
./start.sh
```

## Notes
- The app saves data in the local `data/` folder.
- Background job scraping fills pending job details when a valid public job URL is available.
- Some job boards may block automated scraping.
- Resume generation remains isolated per run and uses only the active profile, active job data, and active prompts.
