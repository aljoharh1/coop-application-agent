"""
cleanup_db.py — removes junk from applications.db. Safe to run any time (idempotent); also runs automatically
at the start of resolve_careers_and_queue.py.

  1. directory / listing rows (StartupBlink, Data Center Map, Clutch …)          → rejected
  2. offshore-only companies (known list, or a South-Asian / SE-Asian domain)     → rejected
     (UAE / Egypt / Jordan domains are only rejected by the live check in resolve_careers_and_queue.py,
      because many of those firms do have a Riyadh office)
  3. rows queued on a page that is not a careers page (vendor marketing sites such as
     manatal.com/?utm_source=…, service pages like /solutions/permanent-recruitment/) → back to 'reviewed'
  4. page-title noise in company names ("About GoAgile" → "GoAgile", "Denodo Offices" → "Denodo");
     a rename that collides with an existing company marks the row as a duplicate

Rows you applied to are only touched by rule 2, and the note says they had been marked applied.
Every change is written to status_history / notes, so nothing is lost.

Usage:
    python cleanup_db.py            # apply
    python cleanup_db.py --dry-run  # show what would change
"""

import argparse
import sqlite3
import sys
import urllib.parse

import app_db
from company_filters import (CAREER_WORD_RE, ats_board, clean_company_name, is_directory, not_careers_reason,
                             offshore_reason, root_domain)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

STRICT_OFFSHORE_TLDS = (".in", ".co.in", ".pk", ".com.pk", ".bd", ".com.bd", ".lk", ".np", ".ph", ".com.ph", ".vn")
OPEN_STATUSES = ("new", "reviewed", "queued", "manual_required")


def _site_of(row) -> str:
    for u in (row["source_url"], row["apply_url"]):
        if u and u.startswith("http") and "linkedin.com" not in u:
            return u
    return ""


def _careers_fallback(row) -> str:
    """Where a demoted row should point: its job board / careers page if that is genuine, else the home page."""
    src = row["source_url"] or ""
    if src and (ats_board(src) or CAREER_WORD_RE.search(urllib.parse.urlsplit(src).path)) and not not_careers_reason(src):
        return src
    if src.startswith("http"):
        p = urllib.parse.urlsplit(src)
        return f"{p.scheme}://{p.netloc}"
    return ""


def _set_status(con, row, status: str, note: str, dry: bool) -> None:
    if not dry:
        app_db.set_status(con, row["id"], status, note[:300])


def _repoint(con, row, url: str, dry: bool) -> None:
    if dry:
        return
    key = app_db.canonical_key({**dict(row), "apply_url": url, "apply_type": "careers_page"})
    if con.execute("SELECT 1 FROM opportunities WHERE id!=? AND canonical_key=?", (row["id"], key)).fetchone():
        key = row["canonical_key"]
    con.execute("UPDATE opportunities SET apply_url=?, apply_type='careers_page', canonical_key=? WHERE id=?",
                (url, key, row["id"]))


def cleanup(dry_run: bool = False, verbose: bool = True) -> dict:
    stats = {"directory": 0, "offshore": 0, "not_careers": 0, "renamed": 0, "duplicate": 0}
    lines = []
    with app_db.connect() as con:
        rows = con.execute("SELECT * FROM opportunities WHERE status IN (?,?,?,?,?)",
                           (*OPEN_STATUSES, "applied")).fetchall()
        for row in rows:
            row = con.execute("SELECT * FROM opportunities WHERE id=?", (row["id"],)).fetchone()
            site = _site_of(row)
            name = row["company"] or ""

            # 2) offshore-only (also applies to rows marked applied)
            dom = root_domain(site) if site else ""
            off = offshore_reason("", site) or (
                f"offshore domain ({dom}); no Saudi office or role" if dom.endswith(STRICT_OFFSHORE_TLDS) else None)
            if off:
                note = f"offshore: {off}" + (" (had been marked applied)" if row["status"] == "applied" else "")
                _set_status(con, row, "rejected", note, dry_run)
                stats["offshore"] += 1
                lines.append(f"🌍 #{row['id']} {name}: rejected — {off}")
                continue
            if row["status"] == "applied":
                continue

            # 1) directories / listings
            if is_directory(site, name):
                _set_status(con, row, "rejected", "directory / listing site, not an employer", dry_run)
                stats["directory"] += 1
                lines.append(f"🗂 #{row['id']} {name}: rejected — directory / listing site")
                continue

            # 3) queued on a page that is not a careers page
            if row["status"] == "queued" and row["apply_url"] and not row["apply_url"].startswith("mailto:"):
                why = not_careers_reason(row["apply_url"])
                if why:
                    back = _careers_fallback(row)
                    _repoint(con, row, back, dry_run)
                    _set_status(con, row, "reviewed", f"un-queued: {why} ({row['apply_url'][:80]})", dry_run)
                    stats["not_careers"] += 1
                    lines.append(f"↩ #{row['id']} {name}: back to reviewed — {why}")
                    row = con.execute("SELECT * FROM opportunities WHERE id=?", (row["id"],)).fetchone()

            # 4) names
            new = clean_company_name(name, site)
            if new and new != name:
                fp = app_db.fingerprint(new, row["title"] or "")
                key = app_db.canonical_key({**dict(row), "company": new})
                other = con.execute("SELECT id, company FROM opportunities WHERE id!=? AND "
                                    "(canonical_key=? OR (fingerprint=? AND status!='rejected'))",
                                    (row["id"], key, fp)).fetchone()
                if other:
                    if row["status"] in ("new", "reviewed"):
                        _set_status(con, row, "rejected", f"duplicate of #{other['id']} ({other['company']})", dry_run)
                        stats["duplicate"] += 1
                        lines.append(f"⧉ #{row['id']} {name}: rejected — duplicate of #{other['id']} {other['company']}")
                    continue
                if not dry_run:
                    try:
                        con.execute("UPDATE opportunities SET company=?, fingerprint=?, canonical_key=?, "
                                    "notes=TRIM(notes || ' ' || ?) WHERE id=?",
                                    (new, fp, key, f"[renamed from “{name}”]", row["id"]))
                    except sqlite3.IntegrityError:
                        continue
                stats["renamed"] += 1
                lines.append(f"✎ #{row['id']} “{name}” → “{new}”")
        if dry_run:
            con.rollback()

    if verbose:
        for line in lines:
            print(line)
    summary = ", ".join(f"{k} {v}" for k, v in stats.items() if v) or "nothing to clean"
    print(f"🧹 DB cleanup{' (dry run)' if dry_run else ''}: {summary}")
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    cleanup(dry_run=a.dry_run, verbose=not a.quiet)
