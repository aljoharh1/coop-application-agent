"""
tracker.py — application tracking & reporting.

    python tracker.py list [--status queued] [--limit 30]
    python tracker.py show 42
    python tracker.py status 42 reviewed|queued|applied|rejected|manual_required|new ["note"]
    python tracker.py outreach                         # list outreach drafts
    python tracker.py outreach-status 7 sent_manually|replied|declined|no_response
    python tracker.py report                           # writes reports/report_YYYY-MM-DD.md
"""

import argparse
import os
import sys
from datetime import date, timedelta

from app_db import STATUSES, DuplicateApplication, connect, set_status
from config import REPORTS_DIR
from scoring import is_recent


def cmd_list(status: str | None, limit: int) -> None:
    q = "SELECT * FROM opportunities"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY CASE status WHEN 'queued' THEN 0 WHEN 'manual_required' THEN 1 WHEN 'new' THEN 2 ELSE 3 END," \
         " score DESC LIMIT ?"
    with connect() as con:
        rows = con.execute(q, (*args, limit)).fetchall()
    for r in rows:
        exp = " EXPIRED" if r["is_expired"] else ""
        print(f"#{r['id']:<4} {r['score']:>3}  {r['status']:<15} {r['company'][:24]:<24} {r['title'][:45]:<45}"
              f" [{r['sources']}]{exp}")


def cmd_show(opp_id: int) -> None:
    with connect() as con:
        r = con.execute("SELECT * FROM opportunities WHERE id=?", (opp_id,)).fetchone()
        if not r:
            sys.exit("not found")
        for k in r.keys():
            if r[k] not in (None, ""):
                print(f"{k:>15}: {r[k]}")
        print("\n  history:")
        for h in con.execute("SELECT * FROM status_history WHERE opp_id=? ORDER BY id", (opp_id,)):
            print(f"    {h['at']}  {h['old_status']} → {h['new_status']}  {h['note'] or ''}")


def cmd_status(opp_id: int, status: str, note: str) -> None:
    with connect() as con:
        try:
            set_status(con, opp_id, status, note)
        except DuplicateApplication as e:
            sys.exit(f"❌ Not changed: {e}")
    print(f"#{opp_id} → {status}")


def cmd_outreach(list_only: bool = True, oid: int | None = None, status: str | None = None) -> None:
    with connect() as con:
        if not list_only:
            con.execute("UPDATE outreach SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                        (status, oid))
            print(f"outreach #{oid} → {status}")
            return
        for o in con.execute("SELECT * FROM outreach ORDER BY created_at DESC"):
            print(f"#{o['id']:<4} {o['status']:<14} {o['company'][:30]:<30} {o['email'] or o['contact_form_url'] or ''}"
                  f"\n      {o['draft_path']}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _table(rows, cols, headers) -> list[str]:
    if not rows:
        return ["_none_", ""]
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(r[c] if r[c] is not None else "").replace("|", "/")[:70]
                                     for c in cols) + " |")
    return out + [""]


def build_report() -> str:
    today = date.today()
    week_ago = (today - timedelta(days=7)).isoformat()
    with connect() as con:
        by_status = {s: 0 for s in STATUSES}
        by_status.update({r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM opportunities GROUP BY status")})
        by_source: dict[str, int] = {}
        for (srcs,) in con.execute("SELECT sources FROM opportunities"):
            for s in filter(None, (srcs or "").split(",")):
                by_source[s] = by_source.get(s, 0) + 1
        multi = con.execute("SELECT COUNT(*) FROM opportunities WHERE sources LIKE '%,%'").fetchone()[0]
        new_today = con.execute("SELECT COUNT(*) FROM opportunities WHERE first_seen >= ?",
                                (today.isoformat(),)).fetchone()[0]
        expired = con.execute("SELECT COUNT(*) FROM opportunities WHERE is_expired=1").fetchone()[0]
        queued = con.execute("SELECT * FROM opportunities WHERE status='queued' ORDER BY score DESC").fetchall()
        manual = con.execute("SELECT * FROM opportunities WHERE status='manual_required' AND is_expired=0 "
                             "ORDER BY score DESC LIMIT 20").fetchall()
        soon = con.execute("SELECT * FROM opportunities WHERE status NOT IN ('applied','rejected') "
                           "AND deadline_date BETWEEN ? AND ? ORDER BY deadline_date",
                           (today.isoformat(), (today + timedelta(days=7)).isoformat())).fetchall()
        applied_week = con.execute("SELECT * FROM opportunities WHERE status='applied' AND applied_at >= ? "
                                   "ORDER BY applied_at DESC", (week_ago,)).fetchall()
        applied_total = by_status["applied"]
        new_rows = [r for r in con.execute("SELECT * FROM opportunities WHERE status='new' AND is_expired=0 "
                                           "ORDER BY score DESC LIMIT 40")
                    if is_recent({k: r[k] for k in r.keys()})][:10]
        outreach = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM outreach GROUP BY status")}
        runs = con.execute("SELECT * FROM runs WHERE started >= ? ORDER BY id", (today.isoformat(),)).fetchall()

    startups = 0
    from config import STARTUPS_CSV
    if os.path.exists(STARTUPS_CSV):
        with open(STARTUPS_CSV, encoding="utf-8-sig") as f:
            startups = max(0, sum(1 for _ in f) - 1)

    total = sum(by_status.values())
    lines = [f"# COOP Search Report — {today.isoformat()}", "",
             "## Funnel", "",
             f"- Opportunities tracked: **{total}** (new today: {new_today}, seen in 2+ sources: {multi}, expired: {expired})",
             "- " + " · ".join(f"{s}: **{by_status[s]}**" for s in STATUSES),
             f"- Applications sent: **{applied_total}** total, **{len(applied_week)}** in the last 7 days",
             "- By source: " + (" · ".join(f"{k}: {v}" for k, v in sorted(by_source.items())) or "—"),
             f"- Companies in startups.csv: {startups} · outreach drafts: "
             + (" · ".join(f"{k}: {v}" for k, v in outreach.items()) or "none"), ""]
    if runs:
        lines += ["## Today's pipeline", ""]
        lines += [f"- {'✅' if r['ok'] else '❌'} {r['step']}: {r['detail'][:160]}" for r in runs] + [""]
    lines += ["## Deadlines in the next 7 days", ""]
    lines += _table(soon, ["id", "deadline_date", "status", "company", "title"], ["#", "Deadline", "Status", "Company", "Title"])
    lines += ["## Queued for auto-fill (`python apply_queue.py`)", ""]
    lines += _table(queued, ["id", "score", "company", "title", "apply_url"], ["#", "Score", "Company", "Title", "Link"])
    lines += ["## Needs your hands (manual_required)", ""]
    lines += _table(manual, ["id", "score", "company", "title", "apply_type", "apply_url"],
                    ["#", "Score", "Company", "Title", "Type", "Link / message"])
    lines += ["## Top new, not yet queued (score below threshold or awaiting review)", ""]
    lines += _table(new_rows, ["id", "score", "company", "title", "score_reasons"], ["#", "Score", "Company", "Title", "Why"])
    lines += ["## Applied in the last 7 days", ""]
    lines += _table(applied_week, ["id", "applied_at", "company", "title"], ["#", "Applied", "Company", "Title"])
    return "\n".join(lines)


def write_report() -> str:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"report_{date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_report())
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--status", choices=STATUSES); p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("show"); p.add_argument("id", type=int)
    p = sub.add_parser("status"); p.add_argument("id", type=int); p.add_argument("status", choices=STATUSES); p.add_argument("note", nargs="?", default="")
    sub.add_parser("outreach")
    p = sub.add_parser("outreach-status"); p.add_argument("id", type=int)
    p.add_argument("status", choices=["draft", "sent_manually", "replied", "declined", "no_response"])
    sub.add_parser("report")
    a = ap.parse_args()

    if a.cmd == "list":
        cmd_list(a.status, a.limit)
    elif a.cmd == "show":
        cmd_show(a.id)
    elif a.cmd == "status":
        cmd_status(a.id, a.status, a.note)
    elif a.cmd == "outreach":
        cmd_outreach()
    elif a.cmd == "outreach-status":
        cmd_outreach(False, a.id, a.status)
    elif a.cmd == "report":
        path = write_report()
        print(build_report())
        print(f"\n📄 {path}")


if __name__ == "__main__":
    main()
