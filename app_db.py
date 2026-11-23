"""
app_db.py — persistent application database (SQLite).

One row per *real-world* opportunity, however many sources reported it.
Statuses: new → reviewed → queued → applied   (or rejected / manual_required at any point)

Duplicate protection, in three layers:
  1. canonical_key  — apply URL (external/email), LinkedIn job id, or company+title fingerprint
  2. fingerprint    — normalized company + title tokens; a new row whose fingerprint matches an
                      existing one is merged into it, even when the URLs differ
  3. a partial UNIQUE index: at most one 'applied' row per fingerprint, enforced by SQLite itself,
     plus already_applied() checks by URL and fingerprint before any application starts.
"""

import csv
import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH, OPPORTUNITIES_CSV
from opportunities_store import normalize_url

STATUSES = ("new", "reviewed", "queued", "applied", "rejected", "manual_required")
# When two rows merge, the more advanced status wins
_STATUS_RANK = {"applied": 6, "queued": 5, "manual_required": 4, "reviewed": 3, "new": 2, "rejected": 1}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS opportunities (
    id              INTEGER PRIMARY KEY,
    canonical_key   TEXT UNIQUE NOT NULL,
    fingerprint     TEXT NOT NULL,
    company         TEXT, title TEXT,
    apply_url       TEXT, apply_type TEXT,
    sources         TEXT DEFAULT '',
    location        TEXT, posted_date TEXT,
    deadline        TEXT, deadline_date TEXT,
    linkedin_url    TEXT, source_url TEXT,
    status          TEXT NOT NULL DEFAULT 'new' CHECK (status IN {STATUSES}),
    score           INTEGER DEFAULT 0, score_reasons TEXT DEFAULT '',
    is_expired      INTEGER DEFAULT 0,
    first_seen      TEXT, last_seen TEXT, status_updated TEXT,
    applied_at      TEXT, notes TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_opp_fp ON opportunities(fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS one_application_per_fingerprint
    ON opportunities(fingerprint) WHERE status = 'applied';

CREATE TABLE IF NOT EXISTS source_map (          -- discovery-agent job_id → opportunity
    job_id TEXT PRIMARY KEY, opp_id INTEGER NOT NULL, source TEXT
);
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY, opp_id INTEGER, old_status TEXT, new_status TEXT, at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS outreach (
    id INTEGER PRIMARY KEY,
    company_fp TEXT UNIQUE NOT NULL, company TEXT,
    email TEXT, contact_form_url TEXT, draft_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','sent_manually','replied','declined','no_response')),
    created_at TEXT, updated_at TEXT, notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started TEXT, finished TEXT, step TEXT, ok INTEGER, detail TEXT
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


@contextmanager
def connect(path: str = DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Normalization & keys
# ---------------------------------------------------------------------------

_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ـ": ""})
_COMPANY_NOISE = {"company", "co", "ltd", "llc", "inc", "the", "group", "شركه", "مؤسسه", "مجموعه",
                  "limited", "corp", "corporation", "ksa", "sa"}
_TITLE_NOISE = {"riyadh", "الرياض", "saudi", "arabia", "ksa", "السعوديه", "2026", "2027", "remote",
                "onsite", "hybrid", "full", "time"}


def normalize_text(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if unicodedata.category(c) != "Mn")
    s = s.translate(_AR).lower()
    s = re.sub(r"\(.*?\)", " ", s)                       # drop "(Riyadh)" style suffixes
    return re.sub(r"[^\w\s]", " ", s)


def company_fp(company: str) -> str:
    tokens = [t for t in normalize_text(company).split() if t not in _COMPANY_NOISE]
    return " ".join(tokens)


def fingerprint(company: str, title: str) -> str:
    title_tokens = sorted({t for t in normalize_text(title).split() if t not in _TITLE_NOISE})
    return f"{company_fp(company)}|{' '.join(title_tokens)}"


def canonical_key(row: dict) -> str:
    url = (row.get("apply_url") or "").strip()
    if row.get("apply_type") in ("external", "email") and url:
        return "url:" + (url.lower() if url.startswith("mailto:") else normalize_url(url))
    li = row.get("linkedin_url") or ""
    m = re.search(r"/jobs/view/(\d+)", li)
    if m:
        return f"li:{m.group(1)}"
    return "fp:" + fingerprint(row.get("company", ""), row.get("title", ""))


def _meaningful_fp(fp: str) -> bool:
    company, title = fp.split("|", 1)
    return bool(company) and company != "unknown" and len(title.split()) >= 1


# ---------------------------------------------------------------------------
# Status changes
# ---------------------------------------------------------------------------

class DuplicateApplication(Exception):
    pass


def set_status(con, opp_id: int, status: str, note: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}; use one of {STATUSES}")
    row = con.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchone()
    if row is None:
        raise KeyError(f"opportunity #{opp_id} not found")
    if row["status"] == status:
        return
    if status == "applied":
        dup = already_applied(con, row, exclude_id=opp_id)
        if dup:
            raise DuplicateApplication(f"already applied as #{dup['id']} ({dup['company']} — {dup['title']})")
    try:
        con.execute(
            "UPDATE opportunities SET status=?, status_updated=?, applied_at=COALESCE(?, applied_at),"
            " notes=TRIM(notes || ' ' || ?) WHERE id=?",
            (status, now(), now() if status == "applied" else None, note, opp_id))
    except sqlite3.IntegrityError as e:              # partial unique index fired
        raise DuplicateApplication(str(e)) from e
    con.execute("INSERT INTO status_history(opp_id, old_status, new_status, at, note) VALUES (?,?,?,?,?)",
                (opp_id, row["status"], status, now(), note))


def already_applied(con, row, exclude_id: int | None = None):
    """Returns the applied row that duplicates `row` (same apply URL or same fingerprint), or None."""
    url_key = canonical_key(dict(row)) if row["apply_url"] else None
    q = ("SELECT * FROM opportunities WHERE status='applied' AND id != ? AND "
         "(fingerprint = ? OR canonical_key = ? OR (apply_url != '' AND apply_url = ?))")
    return con.execute(q, (exclude_id or -1, row["fingerprint"], url_key or "", row["apply_url"] or "")).fetchone()


# ---------------------------------------------------------------------------
# Sync from the discovery CSV
# ---------------------------------------------------------------------------

def _merge_into(con, keep_id: int, drop_id: int) -> None:
    keep = con.execute("SELECT * FROM opportunities WHERE id=?", (keep_id,)).fetchone()
    drop = con.execute("SELECT * FROM opportunities WHERE id=?", (drop_id,)).fetchone()
    status = max((keep["status"], drop["status"]), key=_STATUS_RANK.get)
    sources = ",".join(sorted(set(filter(None, (keep["sources"] + "," + drop["sources"]).split(",")))))
    fill = {c: keep[c] or drop[c] for c in ("apply_url", "linkedin_url", "source_url", "deadline",
                                             "location", "posted_date")}
    con.execute("DELETE FROM opportunities WHERE id=?", (drop_id,))   # free unique slots first
    con.execute(
        "UPDATE opportunities SET status=?, sources=?, apply_url=?, linkedin_url=?, source_url=?,"
        " deadline=?, location=?, posted_date=?, first_seen=MIN(first_seen, ?),"
        " applied_at=COALESCE(applied_at, ?) WHERE id=?",
        (status, sources, *fill.values(), drop["first_seen"], drop["applied_at"], keep_id))
    con.execute("UPDATE source_map SET opp_id=? WHERE opp_id=?", (keep_id, drop_id))
    con.execute("UPDATE status_history SET opp_id=? WHERE opp_id=?", (keep_id, drop_id))
    con.execute("INSERT INTO status_history(opp_id, old_status, new_status, at, note) VALUES (?,?,?,?,?)",
                (keep_id, keep["status"], status, now(), f"merged duplicate #{drop_id}"))


def upsert(con, row: dict) -> tuple[int, bool]:
    """Insert or merge one discovery row. Returns (opp_id, created)."""
    key, fp = canonical_key(row), fingerprint(row.get("company", ""), row.get("title", ""))
    source, job_id = row.get("source") or "unknown", row.get("job_id") or key

    mapped = con.execute("SELECT opp_id FROM source_map WHERE job_id=?", (job_id,)).fetchone()
    existing = None
    if mapped:
        existing = con.execute("SELECT * FROM opportunities WHERE id=?", (mapped["opp_id"],)).fetchone()
    if existing is None:
        existing = con.execute("SELECT * FROM opportunities WHERE canonical_key=?", (key,)).fetchone()
    if existing is None and _meaningful_fp(fp):
        existing = con.execute("SELECT * FROM opportunities WHERE fingerprint=? ORDER BY id LIMIT 1",
                               (fp,)).fetchone()

    if existing is None:
        cur = con.execute(
            "INSERT INTO opportunities(canonical_key, fingerprint, company, title, apply_url, apply_type,"
            " sources, location, posted_date, deadline, linkedin_url, source_url, first_seen, last_seen,"
            " status_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, fp, row.get("company"), row.get("title"), row.get("apply_url", ""), row.get("apply_type", ""),
             source, row.get("location", ""), row.get("posted_date", ""), row.get("deadline", ""),
             row.get("linkedin_url", ""), row.get("source_url", ""),
             row.get("first_seen") or now(), now(), now()))
        opp_id, created = cur.lastrowid, True
    else:
        opp_id, created = existing["id"], False
        sources = ",".join(sorted(set(filter(None, (existing["sources"] + "," + source).split(",")))))
        better_type = row.get("apply_type") == "external" and existing["apply_type"] != "external"
        con.execute(
            "UPDATE opportunities SET sources=?, last_seen=?,"
            " apply_url = CASE WHEN ? THEN ? ELSE COALESCE(NULLIF(apply_url,''), ?) END,"
            " apply_type = CASE WHEN ? THEN 'external' ELSE COALESCE(NULLIF(apply_type,''), ?) END,"
            " posted_date=COALESCE(NULLIF(posted_date,''), ?), deadline=COALESCE(NULLIF(deadline,''), ?),"
            " location=COALESCE(NULLIF(location,''), ?), linkedin_url=COALESCE(NULLIF(linkedin_url,''), ?),"
            " source_url=COALESCE(NULLIF(source_url,''), ?) WHERE id=?",
            (sources, now(), better_type, row.get("apply_url", ""), row.get("apply_url", ""),
             better_type, row.get("apply_type", ""), row.get("posted_date", ""), row.get("deadline", ""),
             row.get("location", ""), row.get("linkedin_url", ""), row.get("source_url", ""), opp_id))
        # A LinkedIn row that just resolved to an external URL may now collide with a Google/Telegram row
        if better_type:
            new_key = canonical_key({**dict(existing), "apply_url": row["apply_url"], "apply_type": "external"})
            other = con.execute("SELECT id FROM opportunities WHERE canonical_key=? AND id!=?",
                                (new_key, opp_id)).fetchone()
            if other:
                _merge_into(con, other["id"], opp_id)
                opp_id = other["id"]

    con.execute("INSERT OR REPLACE INTO source_map(job_id, opp_id, source) VALUES (?,?,?)",
                (job_id, opp_id, source))
    return opp_id, created


def sync_from_csv(path: str = OPPORTUNITIES_CSV) -> dict:
    stats = {"rows": 0, "created": 0, "merged": 0}
    if not os.path.exists(path):
        return stats
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with connect() as con:
        for r in rows:
            if not (r.get("title") or "").strip():
                continue
            _, created = upsert(con, r)
            stats["rows"] += 1
            stats["created" if created else "merged"] += 1
    return stats


def log_run(step: str, ok: bool, detail: str, started: str) -> None:
    with connect() as con:
        con.execute("INSERT INTO runs(started, finished, step, ok, detail) VALUES (?,?,?,?,?)",
                    (started, now(), step, int(ok), detail[:2000]))


def to_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


if __name__ == "__main__":
    print(json.dumps(sync_from_csv(), indent=2))
