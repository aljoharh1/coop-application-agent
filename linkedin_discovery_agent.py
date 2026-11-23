"""
linkedin_discovery_agent.py
وكيل اكتشاف فرص التدريب في الرياض من صفحات LinkedIn العامة (Guest mode – بدون تسجيل دخول).

لماذا وضع الضيف؟
- صفحات /jobs/search العامة لا تحتاج حساباً، فلا يتعرض حسابك الشخصي للتقييد.
- إن ظهر Authwall أو CAPTCHA تتوقف الأداة (page.pause) لتقرري بنفسك.

المتطلبات: نفس مجلد job_form_filler.py
    pip install playwright playwright-stealth
    playwright install chromium

التشغيل:
    python linkedin_discovery_agent.py              # بحث + جلب روابط التقديم الخارجية
    python linkedin_discovery_agent.py --no-details # بحث فقط (أسرع وأخف)
"""

import csv
import random
import re
import sys
import urllib.parse
from datetime import datetime

from playwright.sync_api import Page, sync_playwright

# إعادة استخدام أدوات محرك التعبئة
from job_form_filler import apply_stealth, detect_captcha
from opportunities_store import OUTPUT_CSV, merge_and_save

# ---------------------------------------------------------------------------
# الإعدادات
# ---------------------------------------------------------------------------

KEYWORDS = ["AI Intern", "Data Science Co-op", "تدريب ذكاء اصطناعي"]
LOCATION = "Riyadh, Saudi Arabia"
USER_DATA_DIR = "./browser_profile_guest"   # منفصل عن ملف أداة التعبئة (لا يحمل جلسة LinkedIn)

MAX_JOBS_PER_KEYWORD = 75         # سقف مقصود: حجم معقول لا يثير أنظمة الحماية
MAX_SCROLL_ROUNDS = 25
MAX_DETAIL_VISITS = 60            # سقف زيارات صفحات تفاصيل الوظائف في الجلسة كلها

# فترات التوقف (ثوانٍ) — "قوية" عمداً
SCROLL_DELAY = (3.0, 7.0)
BETWEEN_KEYWORDS_DELAY = (25.0, 50.0)
DETAIL_DELAY = (8.0, 16.0)
LONG_BREAK_EVERY = 10             # استراحة طويلة كل N زيارة تفاصيل
LONG_BREAK = (45.0, 90.0)
RATE_LIMIT_COOLDOWN = (180.0, 300.0)

SEARCH_URL = "https://www.linkedin.com/jobs/search?keywords={kw}&location={loc}&f_TPR=r2592000"  # آخر 30 يوماً


# ---------------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------------

def wait(page: Page, rng: tuple[float, float], label: str = "") -> None:
    s = random.uniform(*rng)
    print(f"   ⏳ {label} {s:.1f} ث")
    page.wait_for_timeout(s * 1000)


class RateLimitGuard:
    """يراقب استجابات الشبكة؛ عند 429 أو 999 (رمز الحظر الخاص بـ LinkedIn) يفرض تبريداً طويلاً."""
    def __init__(self, page: Page):
        self.hit = False
        page.on("response", self._on_response)

    def _on_response(self, resp):
        if "linkedin.com" in resp.url and resp.status in (429, 999):
            self.hit = True

    def cooldown_if_needed(self, page: Page) -> bool:
        if not self.hit:
            return False
        print("\n⚠️  LinkedIn أعاد رمز تقييد (429/999). تبريد طويل...")
        wait(page, RATE_LIMIT_COOLDOWN, "تبريد")
        self.hit = False
        return True


def human_gate(page: Page, stage: str) -> None:
    """يوقف التنفيذ عند Authwall أو CAPTCHA أو نافذة تسجيل الدخول المانعة."""
    while True:
        reason = None
        if "authwall" in page.url or "/login" in page.url or "checkpoint" in page.url:
            reason = f"تحويل إلى صفحة تسجيل الدخول/التحقق: {page.url}"
        elif (c := detect_captcha(page)):
            reason = f"CAPTCHA ← {c}"
        if not reason:
            return
        print(f"\n🛑 توقف ({stage}): {reason}")
        print("   تعاملي معها يدوياً، أو أغلقي النافذة لإنهاء الجلسة. ثم اضغطي Resume.\n")
        page.pause()


def dismiss_signin_modal(page: Page) -> None:
    """إغلاق نافذة "Sign in" المنبثقة التي تظهر للضيوف (لا تمنع القراءة)."""
    for sel in ["button.modal__dismiss", "button[aria-label='Dismiss']",
                "button.contextual-sign-in-modal__modal-dismiss"]:
        btn = page.locator(sel)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(random.randint(600, 1400))
            return


def clean_job_url(href: str) -> tuple[str, str]:
    """يزيل معاملات التتبع ويُرجع (الرابط النظيف، رقم الوظيفة)."""
    path = urllib.parse.urlsplit(href).path
    m = re.search(r"-(\d{8,})/?$", path) or re.search(r"/(\d{8,})/?$", path)
    job_id = m.group(1) if m else ""
    url = f"https://www.linkedin.com/jobs/view/{job_id}/" if job_id else href.split("?")[0]
    return url, job_id


# ---------------------------------------------------------------------------
# 1–4) البحث والتمرير والاستخراج
# ---------------------------------------------------------------------------

def scroll_and_load(page: Page, guard: RateLimitGuard) -> None:
    """تمرير تدريجي حتى يتوقف ظهور نتائج جديدة أو نبلغ السقف."""
    cards = page.locator("ul.jobs-search__results-list > li")
    last_count, stagnant = 0, 0

    for rnd in range(1, MAX_SCROLL_ROUNDS + 1):
        # تمرير على دفعات بمسافات متفاوتة بدلاً من القفز للأسفل مباشرة
        for _ in range(random.randint(2, 4)):
            page.mouse.wheel(0, random.randint(500, 1100))
            page.wait_for_timeout(random.randint(300, 900))

        dismiss_signin_modal(page)

        more = page.locator("button.infinite-scroller__show-more-button")
        if more.count() and more.first.is_visible() and more.first.is_enabled():
            more.first.click()

        wait(page, SCROLL_DELAY, f"تمرير {rnd}:")
        if guard.cooldown_if_needed(page):
            continue
        human_gate(page, "أثناء التمرير")

        count = cards.count()
        print(f"   📄 النتائج المحمّلة: {count}")
        if count >= MAX_JOBS_PER_KEYWORD:
            break
        if page.locator("div.see-more-jobs__viewed-all").is_visible():
            print("   ✔ وصلنا لنهاية النتائج.")
            break
        stagnant = stagnant + 1 if count == last_count else 0
        if stagnant >= 3:
            print("   ✔ لا نتائج جديدة.")
            break
        last_count = count


def extract_cards(page: Page, keyword: str) -> list[dict]:
    raw = page.evaluate(
        """() => Array.from(document.querySelectorAll('ul.jobs-search__results-list > li')).map(li => {
            const t = s => (li.querySelector(s)?.innerText || '').trim();
            return {
                title: t('h3.base-search-card__title'),
                company: t('h4.base-search-card__subtitle'),
                location: t('span.job-search-card__location'),
                posted: li.querySelector('time')?.getAttribute('datetime') || '',
                href: li.querySelector('a.base-card__full-link')?.href || ''
            };
        })"""
    )
    jobs = []
    for r in raw[:MAX_JOBS_PER_KEYWORD]:
        if not r["href"] or not r["title"]:
            continue
        url, job_id = clean_job_url(r["href"])
        jobs.append({
            "job_id": job_id,
            "title": r["title"],
            "company": r["company"],
            "location": r["location"],
            "posted_date": r["posted"],
            "linkedin_url": url,
            "apply_url": "",
            "apply_type": "",
            "keyword": keyword,
        })
    return jobs


def search_keyword(page: Page, guard: RateLimitGuard, keyword: str) -> list[dict]:
    url = SEARCH_URL.format(kw=urllib.parse.quote(keyword), loc=urllib.parse.quote(LOCATION))
    print(f"\n🔎 البحث: '{keyword}' في {LOCATION}")
    page.goto(url, wait_until="domcontentloaded")
    wait(page, (4.0, 8.0), "تحميل:")
    human_gate(page, "بعد فتح البحث")
    dismiss_signin_modal(page)

    if page.locator("ul.jobs-search__results-list > li").count() == 0:
        print("   لا نتائج لهذه الكلمة.")
        return []

    scroll_and_load(page, guard)
    jobs = extract_cards(page, keyword)
    print(f"   ✅ استُخرجت {len(jobs)} وظيفة.")
    return jobs


# ---------------------------------------------------------------------------
# رابط التقديم المباشر (من صفحة التفاصيل)
# ---------------------------------------------------------------------------

def resolve_apply_url(page: Page, job: dict) -> None:
    """
    صفحات الوظائف العامة تحوي غالباً رابط التقديم الخارجي داخل <code id="applyUrl">.
    إن لم يوجد، فالوظيفة على الأرجح Easy Apply (داخل LinkedIn وتتطلب تسجيل دخول).
    """
    page.goto(job["linkedin_url"], wait_until="domcontentloaded")
    page.wait_for_timeout(random.randint(1500, 3000))
    human_gate(page, f"تفاصيل {job['job_id']}")
    dismiss_signin_modal(page)

    raw = page.evaluate("() => document.querySelector('code#applyUrl')?.innerHTML || ''")
    m = re.search(r'"(https?://[^"]+)"', raw)
    if m:
        ext = urllib.parse.unquote(m.group(1))
        # بعض الروابط تمر عبر مُعيد توجيه بمعامل url=
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(ext).query)
        job["apply_url"] = q.get("url", [ext])[0]
        job["apply_type"] = "external"
    else:
        job["apply_url"] = job["linkedin_url"]
        job["apply_type"] = "easy_apply_or_unknown"


# ---------------------------------------------------------------------------
# 5) الحفظ
# ---------------------------------------------------------------------------

def save_csv(jobs: list[dict]) -> None:
    """دمج في الملف المشترك (لا يمسح نتائج وكيل Google ولا يكرر الوظائف)."""
    for j in jobs:
        j["source"] = "linkedin"
    added, total = merge_and_save(jobs, OUTPUT_CSV)
    print(f"\n💾 {added} جديدة — الإجمالي في {OUTPUT_CSV}: {total}")


# ---------------------------------------------------------------------------
# التشغيل
# ---------------------------------------------------------------------------

def run(fetch_details: bool = True) -> None:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
            slow_mo=40,
            viewport={"width": 1366, "height": 850},
            locale="en-US",
            timezone_id="Asia/Riyadh",
        )
        page = context.pages[0] if context.pages else context.new_page()
        apply_stealth(context, page)
        guard = RateLimitGuard(page)

        all_jobs: dict[str, dict] = {}
        for i, kw in enumerate(KEYWORDS):
            for job in search_keyword(page, guard, kw):
                key = job["job_id"] or job["linkedin_url"]
                if key in all_jobs:                      # إزالة التكرار بين الكلمات المفتاحية
                    all_jobs[key]["keyword"] += f" | {kw}"
                else:
                    all_jobs[key] = job
            save_csv(list(all_jobs.values()))            # حفظ تدريجي: لا تضيع النتائج عند الانقطاع
            if i < len(KEYWORDS) - 1:
                wait(page, BETWEEN_KEYWORDS_DELAY, "استراحة بين الكلمات:")

        if fetch_details:
            pending = list(all_jobs.values())[:MAX_DETAIL_VISITS]
            print(f"\n🔗 جلب روابط التقديم لـ {len(pending)} وظيفة...")
            for n, job in enumerate(pending, 1):
                print(f" [{n}/{len(pending)}] {job['company']} — {job['title']}")
                try:
                    resolve_apply_url(page, job)
                except Exception as e:
                    print(f"   تعذّر: {e}")
                    job["apply_url"], job["apply_type"] = job["linkedin_url"], "error"
                guard.cooldown_if_needed(page)
                wait(page, DETAIL_DELAY, "بين الوظائف:")
                if n % LONG_BREAK_EVERY == 0:
                    save_csv(list(all_jobs.values()))
                    wait(page, LONG_BREAK, "استراحة طويلة:")

        save_csv(list(all_jobs.values()))
        context.close()


if __name__ == "__main__":
    run(fetch_details="--no-details" not in sys.argv)
