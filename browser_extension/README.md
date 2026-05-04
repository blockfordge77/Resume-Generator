# TailorResume Companion Extension UI

This is a lightweight Chrome/Edge extension UI that works with the separate `extension_api` service.

## Load in Chrome / Edge

1. Start the existing services:
   - Streamlit app as usual
   - Extension API via `start_extension_api.bat` or `start_extension_api.sh`
2. Open `chrome://extensions` or `edge://extensions`
3. Enable **Developer mode**
4. Click **Load unpacked**
5. Select this `browser_extension` folder
6. Open the extension from the browser toolbar

## Default API URL

`http://127.0.0.1:8010`

You can change it in the extension **Settings** tab.

## Main screens

- Dashboard
- Jobs
- Weekly
- Resumes
- Settings

## Dashboard actions

- Next Job
- See Job Description
- Generate Resume
- Report Job
- Open Link


## API URL from .env

The extension itself cannot read the server `.env` directly inside Chrome.
So this project includes `browser_extension/config.js`, which is generated from the root `.env` by the sync script.

If you change `EXTENSION_API_BASE_URL` in `.env`, run the sync step again or start the extension API script, which now refreshes `browser_extension/config.js` automatically.

## Direct add to Chrome

A local extension cannot be installed with a real **Add to Chrome** button unless it is published to the Chrome Web Store.
For local/private use, load it through `chrome://extensions` → **Load unpacked**.
You can also download a zip from the extension API install page.
