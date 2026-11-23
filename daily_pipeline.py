"""
daily_pipeline.py — the daily scheduler.

Order:  1 LinkedIn agent → 2 Google agent → 3 Telegram agent
        → sync CSV into applications.db (merge + dedupe across sources)
        → expire / score / rank / queue → ranked_opportunities.csv → daily report
        (+ on Sundays: startup discovery and a few outreach drafts)

Each agent runs as its own process, so one failing agent never stops the others.
Nothing is ever submitted or sent by this script; applying is a separate, attended step (apply_queue.py).

Usage:
    python daily_pipeline.py                     # run once now
    python daily_pipeline.py --skip linkedin     # skip any of: linkedin google telegram
    python daily_pipeline.py --loop              # stay running, fire every day at DAILY_RUN_TIME
    python daily_pipeline.py --weekly            # force the weekly startup/outreach steps today

Recommended on Windows — Task Scheduler instead of --loop (survives restarts):
    schtasks /Create /SC DAILY /ST 08:37 /TN "COOP Daily Pipeline" ^
      /TR "cmd /c cd /d C:\\path\\to\\coop_agent && python daily_pipeline.py >> logs\\scheduler.log 2>&1"
  LinkedIn and Google agents open a visible browser and may pause for a CAPTCHA, so schedule it for a
  time you're usually at your computer.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

from app_db import log_run, now, sync_from_csv
from config import AGENT_TIMEOUT_S, DAILY_RUN_TIME, STARTUP_DISCOVERY_WEEKDAY, WEEKLY_OUTREACH_DRAFTS

AGENTS = [
    ("linkedin", "linkedin_discovery_agent.py", []),
    ("google", "google_discovery_agent.py", ["d"]),
    ("telegram", "telegram_discovery_agent.py", []),
]
HERE = os.path.dirname(os.path.abspath(__file__))


def run_script(name: str, script: str, args: list[str], timeout: int = AGENT_TIMEOUT_S) -> bool:
    """Runs a module in its own process. Console is inherited (not captured) so the agents' interactive
    prompts — Telegram login, CAPTCHA "Resume", y/n questions — appear immediately. To keep a log,
    redirect the pipeline itself (see the schtasks line above)."""
    started = now()
    print(f"\n━━━ {name} ━━━", flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.run([sys.executable, script, *args], cwd=HERE, env=env, timeout=timeout)
        ok = proc.returncode == 0
        log_run(name, ok, f"exit code {proc.returncode}", started)
        if not ok:
            print(f"⚠️ {name} exited with code {proc.returncode} — continuing.")
        return ok
    except subprocess.TimeoutExpired:
        log_run(name, False, f"timed out after {timeout}s", started)
    except Exception as e:
        log_run(name, False, f"{type(e).__name__}: {e}", started)
    print(f"⚠️ {name} failed — continuing with the next step.")
    return False


def step(name: str, fn):
    started = now()
    try:
        result = fn()
        log_run(name, True, str(result), started)
        print(f"✅ {name}: {result}")
        return result
    except Exception as e:
        log_run(name, False, f"{type(e).__name__}: {e}", started)
        print(f"❌ {name}: {e}")
        return None


def run_once(skip: set[str], weekly: bool) -> None:
    print(f"🚀 Pipeline started {now()}")
    for name, script, args in AGENTS:
        if name not in skip:
            run_script(name, script, args)

    from scoring import export_ranked, refresh_and_queue
    from tracker import write_report

    step("sync", sync_from_csv)
    step("rank & queue", refresh_and_queue)
    step("export ranked", lambda: f"{export_ranked()} rows → ranked_opportunities.csv")

    if weekly or date.today().weekday() == STARTUP_DISCOVERY_WEEKDAY:
        run_script("startups", "startup_discovery.py", [])
        run_script("outreach", "outreach.py", ["--limit", str(WEEKLY_OUTREACH_DRAFTS)])

    path = step("report", write_report)
    print(f"\n🏁 Done {now()}. Report: {path}")
    print("   Next: review it, then `python apply_queue.py` when you're ready to apply.")


def seconds_until(hhmm: str) -> float:
    h, m = map(int, hhmm.split(":"))
    target = datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= datetime.now():
        target += timedelta(days=1)
    return (target - datetime.now()).total_seconds()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", nargs="*", default=[], choices=[a[0] for a in AGENTS])
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--weekly", action="store_true")
    a = ap.parse_args()

    if not a.loop:
        run_once(set(a.skip), a.weekly)
        return
    while True:
        wait = seconds_until(DAILY_RUN_TIME)
        print(f"⏰ Next run at {DAILY_RUN_TIME} (in {wait / 3600:.1f} h). Ctrl+C to stop.")
        time.sleep(wait)
        try:
            run_once(set(a.skip), a.weekly)
        except Exception as e:                     # never let one bad day kill the scheduler
            print(f"❌ Run failed: {e}")


if __name__ == "__main__":
    main()
