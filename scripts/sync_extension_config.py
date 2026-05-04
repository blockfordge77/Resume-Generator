from __future__ import annotations

import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if load_dotenv:
    load_dotenv(ROOT / '.env')

base_url = os.getenv('EXTENSION_API_BASE_URL', '').strip()
host = os.getenv('EXTENSION_API_HOST', '127.0.0.1').strip() or '127.0.0.1'
port = os.getenv('EXTENSION_API_PORT', '8010').strip() or '8010'
if not base_url:
    base_url = f'http://{host}:{port}'
config_path = ROOT / 'browser_extension' / 'config.js'
config_path.write_text(
    'window.TAILORRESUME_EXTENSION_CONFIG = ' + json.dumps({'API_BASE_URL': base_url}, ensure_ascii=False, indent=2) + ';\n',
    encoding='utf-8',
)
print(f'Wrote {config_path} with API_BASE_URL={base_url}')
