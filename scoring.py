"""
scoring.py — deadline parsing, expiry, recency and a transparent 0–100 relevance score.

Every score comes with its reasons ("+30 AI/data title, +20 co-op, ...") so the ranking can be
audited and the weights tuned in one place.
"""

import csv
import re
from datetime import date, datetime, timedelta

from app_db import already_applied, connect, normalize_text, set_status, to_dict
from config import DAILY_QUEUE_LIMIT, MIN_SCORE, RANKED_CSV, RECENT_DAYS, TARGET_SECTOR_TERMS

try:
    import dateparser                       # pip install dateparser  (understands Arabic month names)
except ImportError:
    dateparser = None

AI_TERMS = ["ai", "artificial intelligence", "machine learning", "ml", "deep learning", "data",
            "nlp", "computer vision", "llm", "genai", "ذكاء اصطناعي", "بيانات", "تعلم الاله",
            "تعلم عميق", "رؤيه حاسوبيه", "analytics", "تحليل"]
TECH_TERMS = ["software", "developer", "engineer", "cyber", "security", "cloud", "it", "tech",
              "برمجيات", "مطور", "تقنيه", "امن سيبراني", "حاسب"]
COOP_TERMS = ["co op", "coop", "cooperative", "تعاوني", "تدريب تعاوني"]
INTERN_TERMS = ["intern", "internship", "trainee", "تدريب", "متدرب", "graduate program"]
RIYADH_TERMS = ["riyadh", "الرياض"]
KSA_TERMS = ["saudi", "ksa", "السعوديه", "المملكه", "jeddah", "dammam", "khobar", "جده", "الدمام"]
OUTSIDE_TERMS = ["dubai", "uae", "abu dhabi", "cairo", "egypt", "qatar", "doha", "london", "remote only"]


def _has(text: str, terms: list[str]) -> bool:
    norm = f" {normalize_text(text)} "
    return any(re.search(rf"(?<!\w){re.escape(normalize_text(t).strip())}(?!\w)", norm) for t in terms)


def _to_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_deadline(deadline: str, posted: str = "") -> date | None:
    """'2026-11-15', '15/11/2026', '15 نوفمبر', 'Nov 15' → date. A year-less date is placed after the posting date."""
    if not deadline or not deadline.strip():
        return None
    s = deadline.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    if dateparser:
        base = datetime.combine(_to_date(posted) or date.today(), datetime.min.time())
        d = dateparser.parse(s, languages=["ar", "en"],
                             settings={"RELATIVE_BASE": base, "PREFER_DATES_FROM": "future"})
        if d:
            return d.date()
    return None                                # unknown → treated as "no deadline", never as expired


def is_recent(row: dict, days: int = RECENT_DAYS) -> bool:
    d = _to_date(row.get("posted_date") or "") or _to_date(row.get("first_seen") or "")
    return d is not None and d >= date.today() - timedelta(days=days)


def score(row: dict) -> tuple[int, str]:
    title, company = row.get("title") or "", row.get("company") or ""
    both = f"{company} {title}"
    s, why = 0, []

    def add(points: int, reason: str):
        nonlocal s
        s += points
        why.append(f"{points:+d} {reason}")

    if _has(title, AI_TERMS):
        add(30, "AI/data role")
    elif _has(title, TECH_TERMS):
        add(15, "tech role")

    if _has(title, COOP_TERMS):
        add(20, "co-op")                      # matches the 110-day COOP requirement directly
    elif _has(title, INTERN_TERMS):
        add(15, "internship/trainee")

    if _has(both, TARGET_SECTOR_TERMS):
        add(15, "target sector/company")

    loc = row.get("location") or ""
    if _has(loc, RIYADH_TERMS) or _has(title, RIYADH_TERMS):
        add(10, "Riyadh")
    elif _has(loc, KSA_TERMS):
        add(5, "Saudi Arabia")
    elif _has(loc, OUTSIDE_TERMS):
        add(-25, "outside KSA (COOP is in person)")

    apply_points = {"external": 10, "email": 6, "easy_apply_or_unknown": 3}
    if row.get("apply_type") in apply_points:
        add(apply_points[row["apply_type"]], f"apply via {row['apply_type']}")

    posted = _to_date(row.get("posted_date") or "") or _to_date(row.get("first_seen") or "")
    if posted:
        age = (date.today() - posted).days
        if age <= 3:
            add(10, "posted ≤3 days ago")
        elif age <= 7:
            add(5, "posted this week")

    dl = _to_date(row.get("deadline_date") or "")
    if dl and 0 <= (dl - date.today()).days <= 7:
        add(5, "deadline within 7 days")

    n_sources = len([x for x in (row.get("sources") or "").split(",") if x])
    if n_sources > 1:
        add(3 * (n_sources - 1), f"seen in {n_sources} sources")

    return max(0, min(100, s)), ", ".join(why)


# ---------------------------------------------------------------------------
# Refresh, expire, rank, queue
# ---------------------------------------------------------------------------

MANUAL_NOTES = {
    "email": "apply by email",
    "easy_apply_or_unknown": "LinkedIn Easy Apply / unknown — apply inside LinkedIn",
    "telegram_only": "no link in the post — open the source message",
    "error": "apply link could not be resolved",
}


def refresh_and_queue() -> dict:
    """Re-scores every open opportunity, rejects expired ones, and moves the best recent ones forward:
    external links → 'queued' (up to DAILY_QUEUE_LIMIT), anything needing your hands → 'manual_required'."""
    st = {"scored": 0, "expired": 0, "queued": 0, "manual_required": 0, "stale": 0, "duplicates": 0}
    today = date.today()
    with connect() as con:
        open_rows = con.execute("SELECT * FROM opportunities WHERE status IN "
                                "('new','reviewed','queued','manual_required')").fetchall()
        for r in open_rows:
            d = to_dict(r)
            dl = parse_deadline(d["deadline"] or "", d["posted_date"] or "")
            d["deadline_date"] = dl.isoformat() if dl else ""
            expired = bool(dl and dl < today)
            sc, why = score(d)
            con.execute("UPDATE opportunities SET deadline_date=?, is_expired=?, score=?, score_reasons=? WHERE id=?",
                        (d["deadline_date"], int(expired), sc, why, r["id"]))
            st["scored"] += 1
            if expired and r["status"] != "manual_required":
                set_status(con, r["id"], "rejected", f"expired (deadline {d['deadline_date']})")
                st["expired"] += 1

        queued_now = con.execute("SELECT COUNT(*) FROM opportunities WHERE status='queued'").fetchone()[0]
        candidates = con.execute("SELECT * FROM opportunities WHERE status IN ('new','reviewed') "
                                 "AND is_expired=0 ORDER BY score DESC, first_seen DESC").fetchall()
        for r in candidates:
            if not is_recent(to_dict(r)):
                st["stale"] += 1
                continue
            if r["score"] < MIN_SCORE:
                continue
            dup = already_applied(con, r, exclude_id=r["id"])
            if dup:
                set_status(con, r["id"], "rejected", f"duplicate of applied #{dup['id']}")
                st["duplicates"] += 1
                continue
            if r["apply_type"] == "external":
                if queued_now < DAILY_QUEUE_LIMIT:
                    set_status(con, r["id"], "queued", f"score {r['score']}")
                    queued_now += 1
                    st["queued"] += 1
            else:
                set_status(con, r["id"], "manual_required", MANUAL_NOTES.get(r["apply_type"], "needs review"))
                st["manual_required"] += 1
    return st


def export_ranked(path: str = RANKED_CSV) -> int:
    """Writes every open, unexpired, recent opportunity, best first."""
    with connect() as con:
        rows = [to_dict(r) for r in con.execute(
            "SELECT * FROM opportunities WHERE status NOT IN ('applied','rejected') AND is_expired=0 "
            "ORDER BY score DESC, first_seen DESC")]
    rows = [r for r in rows if is_recent(r)]
    cols = ["id", "score", "status", "company", "title", "apply_type", "apply_url", "deadline_date",
            "location", "sources", "posted_date", "first_seen", "score_reasons", "source_url", "linkedin_url"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return len(rows)
