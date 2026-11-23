"""
google_discovery_agent.py
وكيل بحث Google لالتقاط أحدث فرص التدريب التعاوني فور فهرستها، عبر Google Dorks
على أنظمة التوظيف (ATS) مع فلتر الوقت tbs=qdr.

التشغيل (نفس مجلد job_form_filler.py):
    python google_discovery_agent.py        # آخر 24 ساعة (افتراضي)
    python google_discovery_agent.py w      # آخر أسبوع  (h = آخر ساعة)

ملاحظات:
- Google يحظر الطلبات الآلية المتكررة بسرعة؛ لذلك: 3 صفحات كحد أقصى، توقف طويل بينها،
  ومرة أو مرتان يومياً تكفي مع qdr:d.
- عند صفحة /sorry/ أو CAPTCHA أو صفحة الموافقة على ملفات تعريف الارتباط تتوقف الأداة (page.pause) لتتدخلي.
- النتائج تُدمج في riyadh_ai_opportunities.csv عبر opportunities_store دون مسح نتائج LinkedIn.
"""

import random
import sys
import urllib.parse

from playwright.sync_api import Page, sync_playwright

from job_form_filler import apply_stealth, detect_captcha, polite_delay
from opportunities_store import merge_and_save, normalize_url, stable_id

USER_DATA_DIR = "./browser_profile_google"
MAX_PAGES = 3
PAGE_DELAY = (12.0, 25.0)          # بين صفحات النتائج (ثوانٍ)
TIME_LABELS = {"h": "آخر ساعة", "d": "آخر 24 ساعة", "w": "آخر أسبوع", "m": "آخر شهر"}

# أنظمة التوظيف (ATS). كل نطاق ← أقل عمق مسار يدل على وظيفة بعينها لا صفحة قائمة
ATS_SITES = {
    "jobs.lever.co": 2,                 # /company/<uuid>
    "boards.greenhouse.io": 3,          # /company/jobs/<id>
    "job-boards.greenhouse.io": 3,      # النطاق الأحدث لـ Greenhouse
    "jobs.smartrecruiters.com": 2,      # /Company/<id>-title  (careers.* صفحات تعريفية للشركات)
    "apply.workable.com": 3,            # /company/j/<id>
}

KEYWORDS = ('("Saudi Arabia" OR "Riyadh") ("AI" OR "Data" OR "Machine Learning" OR "ذكاء اصطناعي") '
            '("Intern" OR "Internship" OR "Co-op" OR "تدريب تعاوني")')


def build_query() -> str:
    sites = " OR ".join(f"site:{s}" for s in ATS_SITES)
    return f"({sites}) {KEYWORDS}"


def human_gate(page: Page) -> None:
    """يوقف التنفيذ عند صفحة الحظر /sorry/ أو CAPTCHA أو صفحة الموافقة."""
    while True:
        url = page.url
        if "/sorry/" in url:
            reason = "Google رصد حركة آلية (صفحة /sorry/)"
        elif "consent.google" in url:
            reason = "صفحة الموافقة على ملفات تعريف الارتباط"
        elif (c := detect_captcha(page)) or page.locator('form[action*="Captcha" i]').count():
            reason = f"CAPTCHA ← {c or 'نموذج تحقق'}"
        else:
            return
        print(f"\n🛑 {reason}. تعاملي معها في المتصفح ثم اضغطي Resume.")
        print("   (إن تكررت صفحة /sorry/ فالأفضل الإيقاف والعودة بعد ساعات.)\n")
        page.pause()


def unwrap(href: str) -> str:
    """روابط Google قد تكون بالشكل /url?q=<الرابط الحقيقي>."""
    p = urllib.parse.urlsplit(href)
    if p.netloc.endswith("google.com") and p.path == "/url":
        return urllib.parse.parse_qs(p.query).get("q", [href])[0]
    return href


def company_from_url(url: str) -> str:
    seg = urllib.parse.urlsplit(url).path.strip("/").split("/")[0]
    return urllib.parse.unquote(seg).replace("-", " ").replace("_", " ").strip().title() or "Unknown"


def parse_results(page: Page, time_filter: str) -> list[dict]:
    """يستخرج كل رابط يحوي عنواناً (h3) داخل نتائج البحث — أثبت من الاعتماد على div.g."""
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('#search a'))
            .filter(a => a.querySelector('h3'))
            .map(a => ({href: a.href, title: a.querySelector('h3').innerText.trim()}))"""
    )
    jobs = []
    for r in raw:
        url = unwrap(r["href"])
        host = urllib.parse.urlsplit(url).netloc.lower()
        depth = len([s for s in urllib.parse.urlsplit(url).path.split("/") if s])
        if host not in ATS_SITES or depth < ATS_SITES[host]:
            continue                                    # إعلانات، صفحات قوائم، نطاقات أخرى
        clean = normalize_url(url)
        jobs.append({
            "company": company_from_url(clean),
            "title": r["title"],
            "apply_url": clean,
            "apply_type": "external",
            "source": "google",
            "location": "",                             # غير مؤكد من نتيجة البحث
            "posted_date": "",                          # qdr يعني تاريخ الفهرسة لا النشر
            "linkedin_url": "",
            "job_id": stable_id("G", clean),            # ثابت: لا تكرار بين التشغيلات
            "keyword": f"google:qdr:{time_filter}",
        })
    return jobs


def search_google(page: Page, time_filter: str = "d") -> list[dict]:
    q = urllib.parse.quote(build_query())
    print(f"🌐 بحث Google ({TIME_LABELS.get(time_filter, time_filter)})")
    found: dict[str, dict] = {}

    for n in range(MAX_PAGES):
        url = f"https://www.google.com/search?q={q}&tbs=qdr:{time_filter}&hl=en&num=10&start={n * 10}"
        page.goto(url, wait_until="domcontentloaded")
        polite_delay(page, 3.0, 6.0)
        human_gate(page)

        batch = parse_results(page, time_filter)
        new = [j for j in batch if j["job_id"] not in found]
        for j in new:
            found[j["job_id"]] = j
        print(f"   📄 صفحة {n + 1}: {len(new)} وظيفة جديدة")

        has_next = page.locator("a#pnnext").count() > 0
        if not new or not has_next:
            break
        polite_delay(page, *PAGE_DELAY)

    return list(found.values())


def run_google_agent(time_filter: str = "d") -> None:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            slow_mo=40,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Asia/Riyadh",
        )
        page = context.pages[0] if context.pages else context.new_page()
        apply_stealth(context, page)

        jobs = search_google(page, time_filter)
        if jobs:
            for j in jobs:
                print(f"   • {j['company']} — {j['title']}")
            added, total = merge_and_save(jobs)
            print(f"\n💾 أُضيفت {added} فرصة جديدة (الإجمالي في الملف: {total}).")
        else:
            print("لا توجد فرص جديدة في Google حالياً.")
        context.close()


if __name__ == "__main__":
    tf = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in TIME_LABELS else "d"
    run_google_agent(tf)
