"""
opportunities_store.py
مخزن مشترك لملف riyadh_ai_opportunities.csv يستخدمه وكيلا LinkedIn و Google.

- دمج بدل الكتابة فوق الملف أو الإضافة العمياء: كل وكيل يضيف ويحدّث صفوفه دون مسح صفوف الآخر.
- إزالة التكرار بمفتاح ثابت (job_id) فلا تتكرر الوظيفة عند تشغيل الوكيل أكثر من مرة.
- first_seen يُسجَّل عند أول اكتشاف ولا يتغير، لتعرفي ما الجديد.
- كتابة ذرّية بترميز utf-8-sig (العربية تظهر سليمة في Excel، وعلامة BOM مرة واحدة في رأس الملف فقط).
"""

import csv
import hashlib
import os
import tempfile
import urllib.parse
from datetime import datetime

OUTPUT_CSV = "riyadh_ai_opportunities.csv"

FIELDS = ["company", "title", "apply_url", "apply_type", "source", "location", "posted_date",
          "deadline", "linkedin_url", "source_url", "job_id", "keyword", "first_seen", "scraped_at"]
# apply_type: external (قابل للتغذية لأداة التعبئة) | email | easy_apply_or_unknown | telegram_only | error


def normalize_url(url: str) -> str:
    """يزيل معاملات التتبع والأجزاء ويوحّد الشكل؛ يُستخدم لتوليد مفتاح ثابت."""
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parts.scheme or "https", parts.netloc.lower(), path, "", ""))


def stable_id(prefix: str, url: str) -> str:
    return f"{prefix}-{hashlib.sha1(normalize_url(url).encode()).hexdigest()[:12]}"


def _key(job: dict) -> str:
    return job.get("job_id") or normalize_url(job.get("apply_url") or job.get("linkedin_url", ""))


def load_jobs(path: str = OUTPUT_CSV) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]


def merge_and_save(new_jobs: list[dict], path: str = OUTPUT_CSV) -> tuple[int, int]:
    """يدمج الوظائف الجديدة مع الموجودة ويحفظ. يُرجع (عدد المضاف، الإجمالي)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    by_key = {_key(j): j for j in load_jobs(path)}
    added = 0

    for job in new_jobs:
        k = _key(job)
        if not k:
            continue
        if k in by_key:
            old = by_key[k]
            for f in FIELDS:
                if f in ("first_seen", "keyword"):
                    continue
                if job.get(f):                       # لا نستبدل قيمة موجودة بقيمة فارغة
                    old[f] = job[f]
            kws = {x.strip() for x in (old.get("keyword", "") + "|" + job.get("keyword", "")).split("|") if x.strip()}
            old["keyword"] = " | ".join(sorted(kws))
            old["scraped_at"] = now
        else:
            by_key[k] = {**job, "first_seen": now, "scraped_at": now}
            added += 1

    jobs = list(by_key.values())
    # الخارجية أولاً (قابلة للتغذية لأداة التعبئة)، ثم الأحدث
    jobs.sort(key=lambda j: j.get("posted_date") or (j.get("first_seen") or "")[:10], reverse=True)
    jobs.sort(key=lambda j: not (j.get("apply_type") or "").startswith("external"))

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(jobs)
    os.replace(tmp, path)
    return added, len(jobs)
