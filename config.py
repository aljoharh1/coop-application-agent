"""
config.py — single place for every setting the pipeline modules share.
Edit this file; the other modules import from it.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env() -> Path | None:
    """Loads KEY=VALUE pairs from a .env file into os.environ. Every module imports config first, so every
    script — and every subprocess started by daily_pipeline — sees the same keys.

    Search order: $COOP_ENV_FILE, then .env next to these scripts, then .env in the current folder.
    Values in .env win over older system variables (e.g. an outdated key set earlier with setx)."""
    candidates = [os.getenv("COOP_ENV_FILE"), BASE_DIR / ".env", Path.cwd() / ".env"]
    for c in candidates:
        if not c or not Path(c).is_file():
            continue
        path = Path(c)
        try:
            from dotenv import load_dotenv               # pip install python-dotenv
            load_dotenv(path, override=True, encoding="utf-8-sig")
        except ImportError:                              # fallback parser: same rules, no dependency
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.removeprefix("export ").partition("=")
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
        return path
    return None


ENV_FILE = _load_env()

# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
OPPORTUNITIES_CSV = "riyadh_ai_opportunities.csv"   # raw output of the 3 discovery agents
DB_PATH = "applications.db"                          # persistent application database (SQLite)
STARTUPS_CSV = "startups.csv"
RANKED_CSV = "ranked_opportunities.csv"
REPORTS_DIR = "reports"
OUTREACH_DIR = "outreach_drafts"
LOGS_DIR = "logs"
RESUME_PATH = "resume.pdf"                           # no resume → no auto-apply, no attachment
CV_FACTS_PATH = "cv_facts.json"                      # verified facts every tailored CV is built from
CV_OUTPUT_DIR = "tailored_cvs"                       # one validated PDF per company

# Documents attached automatically to matching upload fields (kept out of git — see .gitignore).
# The tailored CV is attached to CV/resume fields; a missing file is simply left for you to upload.
# Configure your own document paths in profile.json under the "documents" key, or place them
# in my_docs/ with the filenames referenced in profile.json.
DOCUMENTS_DIR = BASE_DIR / "my_docs"
DOCUMENTS = {
    "national_id": str(DOCUMENTS_DIR / os.getenv("COOP_DOC_NATIONAL_ID", "national_id.pdf")),
    "iban_letter": str(DOCUMENTS_DIR / os.getenv("COOP_DOC_IBAN", "iban_letter.pdf")),
    "transcript": str(DOCUMENTS_DIR / os.getenv("COOP_DOC_TRANSCRIPT", "transcript.pdf")),
    "university_letter": str(DOCUMENTS_DIR / os.getenv("COOP_DOC_UNI_LETTER", "university_letter.pdf")),
}

# ---------------------------------------------------------------------------
# Contact constraints
# ---------------------------------------------------------------------------
# The phone number stays on the CV and in forms so recruiters can call.
# Set via profile.json or the COOP_PHONE environment variable.
PHONE = os.getenv("COOP_PHONE", "")
# The SIM now receives SMS OTP and Nafath can be approved on the phone. While these are True, any portal
# that asks for an SMS one-time code or redirects to Nafath PAUSES the filler so you can complete the login
# yourself and press Enter. If either capability is ever turned off, the application is abandoned cleanly.
CAN_RECEIVE_SMS_OTP = True
CAN_USE_NAFATH = True
VERIFICATION_BLOCK_NOTE = "Requires SMS OTP / Nafath - left for you to complete manually"

# ---------------------------------------------------------------------------
# GPA policy
# ---------------------------------------------------------------------------
GPA = os.getenv("COOP_GPA", "3.50")
GPA_SCALE = os.getenv("COOP_GPA_SCALE", "5")
GPA_ON_CV = False            # never printed on CVs or cover letters
GPA_FILL_ONLY_IF_REQUIRED = True   # web forms: filled only when the GPA field is marked mandatory

# Unknown form fields: True = ask once in the terminal (and remember the answer);
# False = fill only what profile/memory already knows, leave the rest blank for you to fill in the browser.
# Also switchable per run: `--no-ask` on job_form_filler.py / apply_queue.py, or COOP_NO_ASK=1 in .env.
ASK_UNKNOWN_FIELDS = os.getenv("COOP_NO_ASK", "").strip().lower() not in ("1", "true", "yes")
# Multi-page wizards (Workday, Oracle, Taleo, Bain …): click Next / Save and Continue automatically after each
# filled step. Always on with --no-ask; otherwise switch on with --auto-advance or COOP_AUTO_ADVANCE=1 in .env.
AUTO_ADVANCE = os.getenv("COOP_AUTO_ADVANCE", "").strip().lower() in ("1", "true", "yes")
MAX_WIZARD_STEPS = 8

# ---------------------------------------------------------------------------
# Official co-op period
# ---------------------------------------------------------------------------
COOP_START = os.getenv("COOP_START_DATE", "2026-12-20")
COOP_END = os.getenv("COOP_END_DATE", "2027-06-17")
COOP_DURATION = os.getenv("COOP_DURATION", "110 working days (660 hours)")

# ---------------------------------------------------------------------------
# Filtering & ranking
# ---------------------------------------------------------------------------
RECENT_DAYS = 21          # only opportunities posted (or first seen) within this window are processed
MIN_SCORE = 45            # below this an opportunity stays "new" for manual review
DAILY_QUEUE_LIMIT = 20    # max opportunities moved to "queued" per pipeline run
APPLY_DELAY_S = (3, 7) # pause between applications in apply_queue.py

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
DAILY_RUN_TIME = "08:37"              # used by `daily_pipeline.py --loop`
AGENT_TIMEOUT_S = 2 * 60 * 60         # agents may pause for CAPTCHA; allow time for you to step in
STARTUP_DISCOVERY_WEEKDAY = 6         # Python weekday: 6 = Sunday (start of the Saudi work week)
WEEKLY_OUTREACH_DRAFTS = 5

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
MODEL_FAST = os.getenv("RPA_MODEL", "claude-haiku-5-5")              # extraction, normalization
MODEL_WRITER = os.getenv("RPA_WRITER_MODEL", "claude-sonnet-5-5")    # outreach drafts, company research

# ---------------------------------------------------------------------------
# Candidate profile (facts only — outreach drafts may not claim anything not listed here)
# ---------------------------------------------------------------------------
# All personal fields are loaded from profile.json at runtime.
# See profile.example.json for the expected schema.
def _load_candidate() -> dict:
    """Load candidate profile from profile.json, falling back to generic placeholders."""
    import json as _json
    _p = BASE_DIR / "profile.json"
    if _p.is_file():
        try:
            return _json.loads(_p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "name": os.getenv("COOP_NAME", "Jane Doe"),
        "email": os.getenv("COOP_EMAIL", "jane.doe@example.com"),
        "linkedin": os.getenv("COOP_LINKEDIN", ""),
        "github": os.getenv("COOP_GITHUB", ""),
        "university": os.getenv("COOP_UNIVERSITY", "Example University"),
        "degree": os.getenv("COOP_DEGREE", "B.Sc. in Computer Science"),
        "skills": ["Python", "SQL"],
        "certifications": [],
        "projects": {},
    }

CANDIDATE = _load_candidate()

# Which project to lead with, by company category (outreach + cover notes).
# Configure in profile.json under "project_by_category" and "default_project".
PROJECT_BY_CATEGORY: dict = CANDIDATE.get("project_by_category", {})
DEFAULT_PROJECT: str = CANDIDATE.get("default_project", "")

# Target-sector signals used by scoring (lower-case, matched against company + title).
# Override in profile.json under "target_sector_terms".
TARGET_SECTOR_TERMS: list = CANDIDATE.get("target_sector_terms", [
    "bank", "fintech", "telecom", "cloud", "ai", "data", "ml", "machine learning",
])
