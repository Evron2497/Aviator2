import os
import time
import random
import math
import sqlite3
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime

import requests
from dotenv import load_dotenv

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    render_template_string,
)

# ============================================================
# CONFIGURATION
# ============================================================

# Load .env from the same directory as this app.py.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

# Database: use an absolute path when DB_NAME is relative so the app
# always opens the same database regardless of the working directory.
_db_name = os.getenv("DB_NAME", "aviator_live.db").strip()
DB_NAME = (
    _db_name
    if os.path.isabs(_db_name)
    else os.path.join(BASE_DIR, _db_name)
)

BETTING_WINDOW = 7.0
MIN_WITHDRAWAL = 1000.0
MAX_WITHDRAWAL = 250000.0
CRASH_DISPLAY_TIME = 2.5
STARTING_BALANCE = 0.0

# ============================================================
# NEXUS PAY CONFIGURATION
# ============================================================

# Keep all Nexus credentials in .env. Never hard-code them in app.py.
NEXUS_PAY_PUBLIC_KEY = os.getenv("NEXUS_PAY_PUBLIC_KEY", "").strip()
NEXUS_PAY_SECRET_KEY = os.getenv("NEXUS_PAY_SECRET_KEY", "").strip()
NEXUS_PAY_API_KEY = os.getenv("NEXUS_PAY_API_KEY", "").strip()

NEXUS_PAY_BASE_URL = os.getenv(
    "NEXUS_PAY_BASE_URL",
    "https://makamescopay.com"
).strip().rstrip("/")

try:
    NEXUS_PAY_TIMEOUT = int(
        os.getenv("NEXUS_PAY_TIMEOUT", "30")
    )
except (TypeError, ValueError):
    NEXUS_PAY_TIMEOUT = 30

if NEXUS_PAY_TIMEOUT < 5:
    NEXUS_PAY_TIMEOUT = 5

# # Optional callback secret. Leave blank until Nexus provides a webhook
# # signing/verification secret and its exact callback specification.
# NEXUS_CALLBACK_SECRET = os.getenv("NEXUS_CALLBACK_SECRET", "").strip()
# NEXUS_WEBHOOK_SECRET = os.getenv("NEXUS_WEBHOOK_SECRET", "").strip()
# NEXUS_WEBHOOK_PATH = os.getenv("NEXUS_WEBHOOK_PATH", "/api/nexus/webhook").strip() or "/api/nexus/webhook"
# NEXUS_WEBHOOK_URL = os.getenv("NEXUS_WEBHOOK_URL", "").strip()
NEXUS_CALLBACK_SECRET = os.getenv("NEXUS_CALLBACK_SECRET", "").strip()
NEXUS_WEBHOOK_SECRET = os.getenv("NEXUS_WEBHOOK_SECRET", "").strip()
NEXUS_WEBHOOK_PATH = os.getenv(
    "NEXUS_WEBHOOK_PATH",
    "/api/nexus/webhook"
).strip() or "/api/nexus/webhook"

NEXUS_WEBHOOK_URL = os.getenv("NEXUS_WEBHOOK_URL", "").strip()
# # ============================================================


app = Flask(__name__)

FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()

if not FLASK_SECRET_KEY:
    # Development fallback only. Production should always provide
    # FLASK_SECRET_KEY in .env or the process environment.
