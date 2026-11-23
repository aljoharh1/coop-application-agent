"""
run.py — one command to start applying.

    python run.py                 # set up (first time), load opportunities, apply to up to 5
    python run.py --limit 10      # more per session
    python run.py --discover      # also search LinkedIn / Google / Telegram first (slower)
    python run.py --report        # only print today's report

What it does, in order:
  1. Installs missing Python packages and the Playwright browser (first run only).
  2. Loads .env (ANTHROPIC_API_KEY …) and checks the key without printing it.
  3. Builds resume.pdf (the general CV) if it doesn't exist yet.
  4. Loads the verified opportunities into applications.db (no duplicates).
  5. Applies to queued opportunities, best first. For each one it builds and validates a dedicated CV,
     fills the form, skips Nafath / SMS-code portals (→ manual_required), and asks you before Submit.
  6. Writes reports/report_<date>.md.
"""

import argparse
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)                                   # works no matter which folder the terminal is in
sys.path.insert(0, str(HERE))
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")     # Arabic text and symbols in the Windows terminal
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

REQUIRED_MODULES = {"playwright": "playwright", "playwright_stealth": "playwright-stealth", "anthropic": "anthropic",
                    "pypdf": "pypdf", "spellchecker": "pyspellchecker", "dotenv": "python-dotenv",
                    "dateparser": "dateparser", "bs4": "beautifulsoup4", "requests": "requests",
                    "telethon": "telethon"}
BROWSER_MARKER = HERE / ".playwright_installed"


def step(title: str) -> None:
    print(f"\n━━ {title} ━━", flush=True)


def ensure_packages() -> None:
    missing = [pkg for mod, pkg in REQUIRED_MODULES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        step(f"Installing {len(missing)} package(s): {', '.join(missing)}")
        # only the missing ones — never upgrade what already works
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
        importlib.invalidate_caches()
    from importlib.metadata import version
    pw = version("playwright")
    if not BROWSER_MARKER.exists() or BROWSER_MARKER.read_text().strip() != pw:
        step(f"Installing the browser for Playwright {pw} (one time, ~150 MB)")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        BROWSER_MARKER.write_text(pw)


def ensure_resume() -> None:
    from config import RESUME_PATH
    if Path(RESUME_PATH).is_file():
        return
    step("Building resume.pdf (general CV)")
    from cv_tailor import tailor
    r = tailor("General", "AI Co-op", "agents multi-agent machine learning genai rag")
    if not r["ok"]:
        sys.exit(f"❌ General CV failed its quality check: {r['problems']}")
    Path(RESUME_PATH).write_bytes(Path(r["pdf"]).read_bytes())
    print(f"✓ {RESUME_PATH}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    step("Checking setup")
    ensure_packages()
    import config
    import check_setup
    check_setup.main()                    # never prints the key; without a valid key the bot still runs
    print(f"Co-op period: {config.COOP_START} → {config.COOP_END} | GPA on CV: {config.GPA_ON_CV} | "
          f"Nafath/SMS portals: {'skipped' if not (config.CAN_USE_NAFATH and config.CAN_RECEIVE_SMS_OTP) else 'allowed'}")

    from tracker import build_report, write_report
    if a.report:
        print(build_report())
        return

    ensure_resume()

    step("Loading opportunities")
    from seed_opportunities import seed
    print(seed())
    if a.discover:
        subprocess.call([sys.executable, "daily_pipeline.py"], cwd=HERE)

    from scoring import refresh_and_queue
    refresh_and_queue()                    # expires past deadlines, re-scores, queues new finds

    step(f"Applying (up to {a.limit}) — you confirm every Submit")
    import apply_queue
    sys.argv = ["apply_queue.py", "--limit", str(a.limit)]
    try:
        apply_queue.main()
    except KeyboardInterrupt:
        print("\n⏹ Stopped by you. Progress is saved; run the same command to continue.")
    finally:
        step("Report")
        print(f"📄 {write_report()}")
        os.system("")                     # enables ANSI on older Windows consoles
        subprocess.call([sys.executable, "tracker.py", "list", "--limit", "25"], cwd=HERE)


if __name__ == "__main__":
    main()
