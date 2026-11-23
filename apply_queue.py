"""
apply_queue.py — works through 'queued' opportunities, best score first, using job_form_filler.

Before the browser opens, for every opportunity:
  • resume.pdf must exist (otherwise nothing is attempted at all)
  • still queued, not expired, not a duplicate of something already applied to
  • GPA filter: the title, notes and job page are read; if the posting requires more than your GPA
    (from config.py) or a "Very Good" / "Excellent" grade, it is set to `rejected` with the reason
  • a dedicated CV is built and must pass its quality check

A job-board root (Workable, Greenhouse, Lever, Zoho Recruit, Manatal …) is first resolved to the direct
internship / entry-level posting; offshore-only postings are rejected.
In the browser, the bot waits (browser open) until you confirm the form is visible — Nafath / SMS / login
included — and warns when the page has no form fields and no Apply button, so you can reject it (r) at once.

Then the form is filled, and the bot STOPS for your review: you press Submit yourself (or ask it to).
The browser and every tab it opened are closed after each application, before the next one starts.

Usage:
    python apply_queue.py            # up to 5 applications
    python apply_queue.py --limit 2
    python apply_queue.py --id 42    # one specific opportunity
    python apply_queue.py --no-ask   # no terminal questions: unknown fields left blank for you in the browser
                                     # (also walks multi-page wizards: Next / Save and Continue by itself)
    python apply_queue.py --auto-advance   # keep the questions, but click Next between wizard steps by itself

Every application also gets a tailored cover letter (cover_letter.py, facts only): the PDF goes to
"Cover letter" uploads, the text to "Cover letter" boxes; the transcript goes to "Transcript" uploads.
The bot pauses once, on the final Submit / Review step (or earlier if a required field blocks Next).
"""

import argparse
import gc
import os
import random
import re
import sys
import time
from datetime import date

import app_db
import ats_resolver
from app_db import DuplicateApplication, already_applied, connect, set_status
from company_filters import ats_board, offshore_reason
from config import APPLY_DELAY_S, GPA, GPA_SCALE, RESUME_PATH

MY_GPA_5 = float(GPA) if GPA_SCALE == "5" else float(GPA) + 1      # on the 5-point scale
MY_GPA_4 = MY_GPA_5 - 1                                               # Saudi convention: 4-scale ≈ 5-scale − 1

# A grade word only counts when it sits next to a GPA / grade word, so "very good English" is not a GPA rule.
_GRADE_CONTEXT = r"(?:gpa|cgpa|grade|average|cumulative|academic standing|المعدل|معدل|تقدير|بتقدير)"
_HIGH_GRADE = r"(?:very good|excellent|جيد جد(?:ا|اً|ًا)|ممتاز)"
GRADE_WORD_RE = re.compile(rf"{_GRADE_CONTEXT}[^.\n]{{0,40}}{_HIGH_GRADE}|{_HIGH_GRADE}[^.\n]{{0,25}}{_GRADE_CONTEXT}", re.I)
# "GPA of 3.75/5", "GPA 3.75 out of 5", "المعدل لا يقل عن 3.75 من 5", "2.75/4", "minimum GPA: 3.5"
GPA_NUM_RE = re.compile(
    rf"{_GRADE_CONTEXT}[^0-9\n]{{0,40}}([1-4](?:\.\d{{1,2}})?)\s*(?:/|out of|من|of)?\s*([45](?:\.0+)?)?(?![\d.])", re.I)


def gpa_requirement_reason(text: str) -> str | None:
    """Returns why this posting's GPA rule excludes you, or None if it doesn't (or says nothing)."""
    t = re.sub(r"\s+", " ", text or "")
    for m in GPA_NUM_RE.finditer(t):
        value = float(m.group(1))
        scale = m.group(2)
        if scale and scale.startswith("4"):
            if value > MY_GPA_4 + 1e-9:
                return f"requires GPA {value}/4 (yours ≈ {MY_GPA_4:.2f}/4)"
        elif value > MY_GPA_5 + 1e-9:                     # no scale written → Saudi default (5-point)
            return f"requires GPA {value}/{scale[0] if scale else 5} (yours {MY_GPA_5}/5)"
    m = GRADE_WORD_RE.search(t)
    if m:
        return f"requires a high grade: “{m.group(0).strip()[:60]}”"
    return None


def use_posting_url(con, r, url: str) -> str | None:
    """Stores the resolved posting URL on the row. Returns a reason when another row already uses it."""
    new_key = app_db.canonical_key({**dict(r), "apply_url": url, "apply_type": "external"})
    other = con.execute("SELECT id, company FROM opportunities WHERE id!=? AND (canonical_key=? OR apply_url=?)",
                        (r["id"], new_key, url)).fetchone()
    if other:
        return f"posting URL already used by #{other['id']} ({other['company']})"
    con.execute("UPDATE opportunities SET apply_url=?, canonical_key=?, notes=TRIM(notes || ' ' || ?) WHERE id=?",
                (url, new_key, "board resolved to posting", r["id"]))
    return None


def pick(con, limit: int, opp_id: int | None):
    if opp_id:
        return con.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchall()
    return con.execute("SELECT * FROM opportunities WHERE status='queued' AND is_expired=0 "
                       "AND apply_type='external' ORDER BY score DESC, id LIMIT ?", (limit,)).fetchall()


def pre_checks(r) -> bool:
    """Status, expiry and duplicate checks. Returns True if the opportunity may proceed."""
    with connect() as con:
        if r["status"] not in ("queued", "reviewed", "new"):
            print(f"↷ #{r['id']} is '{r['status']}', skipping.")
            return False
        if r["deadline_date"] and r["deadline_date"] < date.today().isoformat():
            set_status(con, r["id"], "rejected", "expired before applying")
            print(f"↷ #{r['id']} expired, skipped.")
            return False
        dup = already_applied(con, r, exclude_id=r["id"])
        if dup:
            set_status(con, r["id"], "rejected", f"duplicate of applied #{dup['id']}")
            print(f"↷ #{r['id']} duplicates applied #{dup['id']} ({dup['company']}), skipped.")
            return False
    return True


def record(opp_id: int, outcome: str) -> None:
    from config import VERIFICATION_BLOCK_NOTE
    with connect() as con:
        try:
            if outcome == "verification":
                set_status(con, opp_id, "manual_required", VERIFICATION_BLOCK_NOTE)
            elif outcome == "mobile":
                set_status(con, opp_id, "manual_required", "portal wants the mobile app — apply from your phone")
            elif outcome in ("submitted", "manual"):
                set_status(con, opp_id, "applied", "submitted by tool" if outcome == "submitted" else "submitted by you")
                print("📌 Recorded as applied.")
            elif outcome == "error":
                set_status(con, opp_id, "manual_required", "filler error")
            elif outcome == "no_form":
                set_status(con, opp_id, "rejected", "no application form on the page (rejected by you)")
                print("🗑 Recorded as rejected (no application form).")
            elif outcome == "manual_later":
                set_status(con, opp_id, "manual_required", "left for manual work (from the form check)")
            else:                                          # not submitted
                keep = input("Not submitted. Keep queued (q), needs manual work (m) or reject (r)? ").strip().lower()
                if keep == "m":
                    set_status(con, opp_id, "manual_required", "left unfinished in filler")
                elif keep == "r":
                    set_status(con, opp_id, "rejected", "rejected during review")
        except DuplicateApplication as e:
            print(f"⚠️ {e}")


def process_single(r, n, total, auto_advance, no_ask, watch):
    """Process a single opportunity — extracted for batch processing."""
    # Import cv_tailor functions here (late import because Playwright is needed)
    from cv_tailor import fetch_jd, tailor
    from cover_letter import make_cover_letter

    with connect() as con:
        r = con.execute("SELECT * FROM opportunities WHERE id=?", (r["id"],)).fetchone()   # fresh state
    if not pre_checks(r):
        return None

    print(f"\n[{n}/{total}] #{r['id']}  score {r['score']}  {r['company']} — {r['title']}\n    {r['apply_url']}")

    # 0) a job-board root (e.g. apply.workable.com/mozn-ai/) → the direct internship / entry-level posting
    apply_url, jd_text = r["apply_url"], ""
    board = ats_board(apply_url or "")
    if board and board[0] in ats_resolver.RESOLVABLE and not ats_resolver._is_posting_url(board[0], apply_url):
        found = ats_resolver.find_posting(apply_url)
        if found["status"] == "found":
            print(f"🎯 Board → posting: {found['title']} ({found.get('location', '')[:40]})\n    {found['apply_url']}")
            with connect() as con:
                note = use_posting_url(con, r, found["apply_url"])
            if note:
                print(f"ℹ️ {note}; keeping the board URL")
            else:
                apply_url, jd_text = found["apply_url"], found.get("text", "")
        elif found["status"] in ("none_suitable", "empty"):
            with connect() as con:
                set_status(con, r["id"], "reviewed", f"un-queued: {found['reason']}")
            print(f"↷ #{r['id']} {r['company']}: {found['reason']} — moved back to reviewed")
            return None

    # 1) read the posting once (headless), then the GPA filter — before any visible browser opens
    jd_url = r["linkedin_url"] or r["source_url"] or apply_url or ""
    if not jd_text and apply_url and apply_url != r["apply_url"]:
        jd_url = apply_url
    if jd_url.startswith("http") and not jd_text:
        try:
            jd_text = fetch_jd(jd_url)
        except Exception as e:
            print(f"ℹ️ couldn't read the posting ({type(e).__name__}); checking title and notes only")
    reason = gpa_requirement_reason(" \n ".join([r["title"] or "", r["notes"] or "", jd_text]))
    if reason:
        with connect() as con:
            set_status(con, r["id"], "rejected", f"GPA filter: {reason}")
        print(f"⛔ Skipped #{r['id']} {r['company']}: {reason}")
        return None

    # 1b) offshore-only posting (India, Pakistan … with no Saudi office or role) → not for you
    off = offshore_reason(jd_text, r["apply_url"] or "") if jd_text else offshore_reason("", r["apply_url"] or "")
    if off:
        with connect() as con:
            set_status(con, r["id"], "rejected", f"offshore: {off}")
        print(f"⛔ Skipped #{r['id']} {r['company']}: {off}")
        return None

    # 2) a dedicated CV, validated before use
    cv = tailor(r["company"], r["title"], jd_text=jd_text)
    if not cv["ok"]:
        with connect() as con:
            set_status(con, r["id"], "manual_required", "tailored CV failed QA: " + "; ".join(cv["problems"])[:300])
        print(f"⛔ CV quality check failed, not applying: {cv['problems']}")
        return None
    print(f"📄 CV: {cv['pdf']}  (projects: {', '.join(cv['projects'])}; missing JD skills: "
          f"{', '.join(cv['gaps'][:6]) or '-'})")
    try:
        letter = make_cover_letter(r["company"], r["title"], cv)
    except Exception as e:
        letter = {"ok": False, "problems": [f"{type(e).__name__}: {str(e)[:80]}"]}
    if letter.get("ok"):
        print(f"✉️  Cover letter: {letter['pdf'] or letter['txt']}")
    else:
        print(f"ℹ️ no cover letter this time ({'; '.join(letter.get('problems', []))[:120]}) — cover-letter fields left for you")

    # 3) fill the form; job_form_filler pauses for your review and closes the browser afterwards
    from job_form_filler import ResumeMissing, VerificationBlocked, run, set_auto_advance, set_no_ask
    if auto_advance or no_ask:
        set_auto_advance(True)
    if no_ask:
        set_no_ask(True)

    try:
        outcome = run(apply_url, cv["pdf"], cover_letter=letter, watch=watch)
    except ResumeMissing as e:
        print(f"❌ {e}")
        outcome = "error"
    except VerificationBlocked as e:
        outcome = "verification"
        print(f"⛔ {e}")
    except KeyboardInterrupt:
        print("\n⏹ Stopped by you.")
        raise
    except Exception as e:
        outcome = "error"
        print(f"⚠️ Filler error: {type(e).__name__}: {str(e)[:200]}")
    record(r["id"], outcome)
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--id", type=int)
    ap.add_argument("--auto-advance", action="store_true",
                    help="multi-page forms: click Next / Save and Continue by itself after each filled step")
    ap.add_argument("--no-ask", action="store_true",
                    help="fill only what profile/memory knows; leave unknown fields blank, no terminal questions")
    ap.add_argument("--batch-tabs", type=int, default=1, metavar="N",
                    help="open up to N companies simultaneously in separate browser contexts (default: 1)")
    ap.add_argument("--watch", action="store_true",
                    help="capture DOM snapshots of blank fields for analysis")
    args = ap.parse_args()

    if not os.path.isfile(RESUME_PATH):
        sys.exit(f"❌ {RESUME_PATH} is missing. Auto-apply is disabled until it exists next to these scripts.")

    from cv_tailor import fetch_jd, tailor                                  # imported late: Playwright needed
    from cover_letter import make_cover_letter

    with connect() as con:
        rows = pick(con, args.limit, args.id)
    if not rows:
        print("Nothing queued. Queue one with: python tracker.py status <id> queued")
        return

    batch_size = max(1, args.batch_tabs)
    if batch_size > 1:
        print(f"🚀 Batch mode: processing up to {batch_size} companies in parallel")

    # Pre-filter rows that pass basic checks
    valid_rows = []
    for n, r in enumerate(rows, 1):
        with connect() as con:
            r = con.execute("SELECT * FROM opportunities WHERE id=?", (r["id"],)).fetchone()
        if not pre_checks(r):
            continue
        valid_rows.append((n, r))

    if not valid_rows:
        return

    # Process in batches of batch_size
    for i in range(0, len(valid_rows), batch_size):
        batch = valid_rows[i:i + batch_size]

        if len(batch) == 1 or batch_size == 1:
            # Sequential processing
            for n, r in batch:
                process_single(r, n, len(valid_rows), args.auto_advance, args.no_ask, args.watch)
        else:
            # Parallel processing using threads (each gets its own Playwright context)
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as executor:
                futures = {
                    executor.submit(process_single, r, n, len(valid_rows), args.auto_advance, args.no_ask, args.watch): (n, r)
                    for n, r in batch
                }
                for future in concurrent.futures.as_completed(futures):
                    n, r = futures[future]
                    try:
                        future.result()
                    except KeyboardInterrupt:
                        print("\n⏹ Stopped by you. Progress so far is saved.")
                        raise
                    except Exception as e:
                        print(f"⚠️ Batch task error for #{r['id']}: {type(e).__name__}: {e}")

        # Wait between batches (not between parallel items)
        if i + batch_size < len(valid_rows):
            wait = random.uniform(*APPLY_DELAY_S)
            print(f"⏳ {wait:.0f}s before the next batch")
            time.sleep(wait)


if __name__ == "__main__":
    main()
