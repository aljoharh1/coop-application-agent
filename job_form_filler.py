"""
job_form_filler.py
أداة RPA شخصية لتعبئة نماذج التوظيف المتكررة بأسلوب مهذّب (Polite) مع تدخّل بشري (Human-in-the-loop).

التثبيت:
    pip install playwright playwright-stealth
    playwright install chromium

التشغيل:
    python job_form_filler.py "https://example.com/careers/apply/123"
    python job_form_filler.py "https://example.com/careers/apply/123" --no-ask   # بلا أسئلة في الطرفية

ملاحظات:
- الأداة لا تحل أي CAPTCHA؛ عند اكتشافها تتوقف وتفتح Playwright Inspector، تحلّينها يدوياً ثم تضغطين Resume.
- الإرسال النهائي (Submit) لا يتم إلا بعد تأكيد صريح منكِ في الطرفية.
"""

import random
import sys

from playwright.sync_api import Page, Locator, sync_playwright, TimeoutError as PWTimeout

import re

import os

from config import (AUTO_ADVANCE, MAX_WIZARD_STEPS, ASK_UNKNOWN_FIELDS, CAN_RECEIVE_SMS_OTP, CAN_USE_NAFATH, COOP_END, COOP_START, DOCUMENTS, GPA,
                    GPA_FILL_ONLY_IF_REQUIRED, PHONE, RESUME_PATH, VERIFICATION_BLOCK_NOTE)
from memory_manager import (YES_WORDS, _match_option, handle_unknown_question, profile_answer, profile_key,
                            profile_value, PROFILE as MM_PROFILE)
from playwright.sync_api import Error as PWError

# ---------------------------------------------------------------------------
# 1) الإعدادات
# ---------------------------------------------------------------------------

USER_DATA_DIR = "./browser_profile"   # ملف تعريف دائم: يحفظ تسجيلات الدخول بين الجلسات
DELAY_RANGE_S = (2.0, 7.0)            # توقف عشوائي بين كل تفاعل (ثوانٍ)
KEY_DELAY_MS = (60, 220)              # تأخير عشوائي بين ضغطات المفاتيح (ملي ثانية)
TYPO_PAUSE_CHANCE = 0.06              # احتمال "تفكير" قصير أثناء الكتابة

# Profile data comes from profile.json (via memory_manager), so forms and memory always agree.
PROFILE = {
    "full_name": profile_value("full_name"),
    "email": profile_value("email"),
    "linkedin": profile_value("linkedin"),
    "github": profile_value("github"),
    "phone": profile_value("phone") or PHONE,
    "location": f"{profile_value('city')}, {profile_value('country')}",
    "university": profile_value("university"),
    "major": profile_value("major"),
    "years_experience": profile_value("years_experience") or "0",
    "gpa": profile_value("gpa") or GPA,   # used only when the form marks the GPA field as required
    "cv_path": RESUME_PATH,
    "transcript_path": "",          # set in run(): profile.json "documents" → config → docs/ folder
    "cover_letter_path": "",        # set in run() from the tailored cover letter (cover_letter.py)
    "cover_letter_text": "",
}

# Field map: alternative selectors per field (name / id / placeholder / aria-label / autocomplete).
# action: type | smart (native <select> → best option from profile.json, text input → typed) | upload
# "hint" is the question the smart action asks profile.json about.
def _sel(*words: str, tags: str = "input") -> list[str]:
    out = []
    for tag in tags.split(","):
        for w in words:
            out += [f"{tag}[name*='{w}' i]", f"{tag}[id*='{w}' i]"]
            if tag == "input":
                out += [f"input[placeholder*='{w}' i]", f"input[aria-label*='{w}' i]"]
    return out


FIELD_MAP = [
    {"key": "full_name", "action": "type",
     "selectors": ["input[autocomplete='name']", "#fullName", "input[name*='fullname' i]", "input[name*='full_name' i]",
                   "input[name='name' i]", "input[placeholder*='full name' i]", "input[aria-label*='full name' i]"]},
    {"key": "email", "action": "type",
     "selectors": ["input[type='email']", "input[autocomplete='email']", "input[name*='email' i]"]},
    {"key": "linkedin", "action": "type",
     "selectors": _sel("linkedin", "linked_in", "linked-in") + ["input[placeholder*='linkedin.com' i]"]},
    {"key": "github", "action": "type", "selectors": _sel("github")},
    {"key": "location", "action": "type",
     "selectors": ["input[name*='city' i]", "input[autocomplete='address-level2']", "input[name*='location' i]:not([name*='job' i])"]},
    {"key": "university", "action": "smart", "hint": "University",
     "selectors": _sel("university", "institution", "college", tags="input,select")
                  + ["input[name*='school' i]:not([name*='high' i])", "select[name*='school' i]:not([name*='high' i])"]},
    {"key": "major", "action": "smart", "hint": "Major",
     "selectors": _sel("major", "field_of_study", "fieldofstudy", "field-of-study", "specializ", "speciality",
                       "discipline", tags="input,select")},
    {"key": "years_experience", "action": "smart", "hint": "Years of experience",
     "selectors": _sel("years_experience", "yearsofexperience", "years-of-experience", "experience_years",
                       "total_experience", "totalexperience", tags="input,select")
                  + ["input[placeholder*='years of experience' i]", "input[aria-label*='years of experience' i]",
                     "input[type='number'][name*='experience' i]", "select[name*='experience' i]"]},
    {"key": "phone", "action": "type",
     "selectors": ["input[type='tel']", "input[autocomplete='tel']", "input[name*='phone' i]", "input[name*='mobile' i]"]},
    {"key": "gpa", "action": "gpa_if_required",
     "selectors": ["input[name*='gpa' i]", "input[id*='gpa' i]", "input[name*='cgpa' i]", "input[placeholder*='gpa' i]",
                   "input[aria-label*='gpa' i]"]},
    {"key": "cv_path", "action": "upload",
     "selectors": ["input[type='file'][name*='resume' i]", "input[type='file'][name*='cv' i]:not([name*='cover' i])",
                   "input[type='file'][id*='resume' i]", "input[type='file'][id*='cv' i]:not([id*='cover' i])"]},
    {"key": "transcript_path", "action": "upload",
     "selectors": ["input[type='file'][name*='transcript' i]", "input[type='file'][id*='transcript' i]",
                   "input[type='file'][aria-label*='transcript' i]"]},
    {"key": "cover_letter_path", "action": "upload",
     "selectors": ["input[type='file'][name*='cover' i]", "input[type='file'][id*='cover' i]",
                   "input[type='file'][aria-label*='cover letter' i]"]},
    {"key": "cover_letter_text", "action": "type",
     "selectors": ["textarea[name*='cover' i]", "textarea[id*='cover' i]", "textarea[aria-label*='cover letter' i]",
                   "textarea[placeholder*='cover letter' i]"]},
    # other upload fields are matched by their label in resolve_unknown_fields (see DOCUMENT_RULES)
]

# --no-ask: fill only what profile/memory knows; unknown fields stay blank for you, no terminal questions.
ASK_UNKNOWN = ASK_UNKNOWN_FIELDS


AUTO_ADVANCE_STEPS = AUTO_ADVANCE


def set_auto_advance(on: bool = True) -> None:
    """Multi-page wizards: click Next / Save and Continue by itself after each filled step."""
    global AUTO_ADVANCE_STEPS
    AUTO_ADVANCE_STEPS = on


def set_no_ask(no_ask: bool = True) -> None:
    """Switches terminal questions off (True) or on (False) for the rest of this run."""
    global ASK_UNKNOWN
    ASK_UNKNOWN = not no_ask


# Fields with no usable label or name: search bars, hidden widgets' helpers, decorative inputs.
# They are skipped silently — never asked about, never listed as manual work.
UNNAMED_LABELS = {"", "(بدون اسم)", "بدون اسم", "unnamed_field", "unnamed", "no_name", "noname", "none", "null",
                  "undefined", "field", "input", "text"}


AUTO_ID_RE = re.compile(r"^(?:[a-z]{1,10}[-_:]+)?\d[0-9a-z_-]*$|^[a-z]{1,3}\d+$|^[0-9a-f]{8}-[0-9a-f-]{20,}$")


def is_unnamed(field: dict) -> bool:
    label = re.sub(r"[\s*:؟?]+", " ", (field.get("label") or "")).strip().lower()
    ident = re.sub(r"[\s*:؟?]+", " ", (field.get("ident") or "")).strip().lower()
    if label.startswith("rpa-"):                      # a radio group with no name/legend falls back to our own tag
        label = ""
    if ident.startswith("rpa-"):
        ident = ""
    if field.get("type") == "search":
        return True
    if label in UNNAMED_LABELS and ident in UNNAMED_LABELS:
        return True
    # no human label, only a machine name such as "input-23", "field_5", ":r4:", "mui-12" or a uuid
    return label == ident and bool(AUTO_ID_RE.match(ident))


# حقول يُسمح بتجاهلها دون إيقاف (مثل حقول البحث أو النشرة البريدية)
IGNORED_FIELD_HINTS = ["search", "newsletter", "csrf", "token", "بحث"]   # مطابقة جزئية
IGNORED_FIELD_EXACT = {"q", "s"}                                  # أسماء قصيرة تُطابق حرفياً فقط

# حقول لا تُعبّأ آلياً أبداً، حتى لو كانت في الذاكرة: موافقات وإقرارات ورفع ملفات
CONSENT_HINTS = ["agree", "terms", "consent", "privacy", "certify", "declare", "acknowledge",
                 "أوافق", "أقر", "الشروط", "الخصوصية", "إقرار"]
MANUAL_ONLY_TYPES = {"file", "password"}   # passwords are never typed or stored by the tool

# Upload fields → which document to attach (first matching rule wins; anything else is left for you)
COVER_LETTER_RE = re.compile(r"cover(ing)?\s*letter|motivation(al)?\s*letter|letter of (motivation|interest|intent)|"
                             r"خطاب (التغطية|تغطية|تعريفي|الدافع)|رسالة (تعريفية|تغطية|الدافع)", re.I)
DOCUMENT_RULES = [
    (COVER_LETTER_RE, "cover_letter"),                    # before "cv": "Upload CV / cover letter" fields vary
    (re.compile(r"\b(cv|resume|résumé|curriculum)\b|السيرة|سيرة ذاتية", re.I), "cv"),
    (re.compile(r"national id|\bid card\b|identity|iqama|هوية|الهوية|بطاقة الأحوال|السجل المدني", re.I), "national_id"),
    (re.compile(r"\biban\b|bank (letter|certificate|account)|آيبان|ايبان|الآيبان|حساب بنكي|شهادة بنكية|خطاب البنك", re.I), "iban_letter"),
    (re.compile(r"transcript|academic record|grades|السجل الأكاديمي|سجل أكاديمي|كشف الدرجات", re.I), "transcript"),
    (re.compile(r"(university|training|co-?op|approval|nomination|recommendation)\s*letter|letter from (the )?university|"
                r"خطاب (الجامعة|التدريب|الترشيح|التوجيه)|خطاب طلب|إفادة", re.I), "university_letter"),
]


HERE = os.path.dirname(os.path.abspath(__file__))
DOC_FOLDERS = ("docs", "documents", "my_docs")
DOC_GLOBS = {"transcript": ("*ranscript*.pdf", "*السجل*.pdf", "*كشف*.pdf"),
             "national_id": ("*National*ID*.pdf", "*هوية*.pdf"),
             "iban_letter": ("*IBAN*.pdf", "*ايبان*.pdf", "*آيبان*.pdf"),
             "university_letter": ("*Approval*.pdf", "*خطاب*.pdf")}


def document_path(key: str) -> str:
    """profile.json "documents" → config.DOCUMENTS → a matching file in docs/, documents/ or my_docs/."""
    import glob
    if key == "cv":
        return PROFILE.get("cv_path") or ""
    if key == "cover_letter":
        return PROFILE.get("cover_letter_path") or ""
    docs = MM_PROFILE.get("documents") if isinstance(MM_PROFILE.get("documents"), dict) else {}
    for cand in (docs.get(key, ""), DOCUMENTS.get(key, "")):
        if cand:
            path = cand if os.path.isabs(cand) else os.path.join(HERE, cand)
            if os.path.isfile(path):
                return path
    for folder in DOC_FOLDERS:
        for pattern in DOC_GLOBS.get(key, ()):
            hits = sorted(glob.glob(os.path.join(HERE, folder, pattern)))
            if hits:
                return hits[0]
    return ""


def pick_document(label: str, ident: str = "") -> str | None:
    """Returns the file to upload for this field, or None when the field isn't clearly one of your documents."""
    text = f"{label} {ident}"
    for rx, key in DOCUMENT_RULES:
        if rx.search(text):
            path = document_path(key)
            return path if path and os.path.isfile(path) else None
    return None
FILL_TYPES = {"date", "time", "datetime-local", "month", "week", "color", "range"}

# مؤشرات وجود CAPTCHA
CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[title*='recaptcha' i]",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",   # Cloudflare Turnstile
    "div.g-recaptcha",
    "div.h-captcha",
    "div.cf-turnstile",
    "[id*='captcha' i]",
    "[class*='captcha' i]",
]
CAPTCHA_TEXT_HINTS = ["verify you are human", "i'm not a robot", "تحقق من أنك إنسان", "لست روبوت"]

# SMS one-time codes and Nafath (see config.py: CAN_USE_NAFATH / CAN_RECEIVE_SMS_OTP).
# Detection always runs; when the capability is ON the filler PAUSES for you to complete the login
# yourself and press Enter, when it is OFF the application is abandoned cleanly (VerificationBlocked).
NAFATH_URL_HINTS = ["iam.gov.sa", "nafath", "sso.gov.sa", "absher.sa", "/nafath"]
NAFATH_TEXT_HINTS = ["nafath", "نفاذ", "النفاذ الوطني", "national single sign-on", "absher", "أبشر"]
OTP_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name*='otp' i]", "input[id*='otp' i]",
    "input[name*='verification' i][type='text']", "input[name*='verificationcode' i]",
    "input[name*='smscode' i]", "input[id*='smscode' i]",
]
OTP_TEXT_HINTS = ["verification code", "one-time password", "one time password", "otp", "sms code",
                  "code sent to your mobile", "code sent to your phone", "enter the code",
                  "رمز التحقق", "رمز التفعيل", "كلمة المرور لمرة واحدة", "تم إرسال رمز", "أدخل الرمز"]
GPA_LABEL_RE = re.compile(r"\b(c?gpa|grade point|cumulative average)\b|المعدل|معدل تراكمي", re.I)
REQUIRED_MARK_RE = re.compile(r"\*|required|mandatory|إلزامي|مطلوب")


class VerificationBlocked(Exception):
    """Raised when the portal needs an SMS one-time code or Nafath, which can't be completed right now."""


# ---------------------------------------------------------------------------
# 2) أدوات التأخير والكتابة البشرية
# ---------------------------------------------------------------------------

def polite_delay(page: Page, low: float = DELAY_RANGE_S[0], high: float = DELAY_RANGE_S[1]) -> None:
    """توقف عشوائي بين التفاعلات لتخفيف الضغط على الخادم ومحاكاة الإيقاع البشري."""
    seconds = random.uniform(low, high)
    print(f"   ⏳ انتظار {seconds:.1f} ث")
    page.wait_for_timeout(seconds * 1000)   # أفضل من time.sleep لأنه لا يجمّد حلقة أحداث Playwright


class FieldNotVisible(Exception):
    """The input exists in the page but is hidden (common in custom widgets); the caller skips it."""


def human_type(page: Page, locator: Locator, text: str) -> None:
    """
    Types into a visible text field without reversing it:
      • longer than 12 characters → fill all but the last character, then type the last one
        (fires the site's keyboard/input events without per-character typing, which reversed text in
        right-to-left or React fields and slowed the terminal);
      • 12 characters or fewer → one press_sequentially call with a human-like delay.
    The value is checked afterwards and corrected with fill() if the site mangled it.
    """
    if not locator.is_visible():
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        if not locator.is_visible():
            raise FieldNotVisible("field is hidden")
    locator.click(timeout=5000)
    locator.fill("")                                  # clear any previous value
    if len(text) > 12:
        locator.fill(text[:-1])
        locator.press_sequentially(text[-1], delay=40)
    else:
        locator.press_sequentially(text, delay=random.randint(*KEY_DELAY_MS))
    try:
        if locator.input_value(timeout=2000) != text:   # reversed / partial → set it directly
            locator.fill(text)
    except Exception:
        pass                                            # contenteditable etc.: nothing to compare


# ---------------------------------------------------------------------------
# 3) آلية التدخل البشري (Human-in-the-loop)
# ---------------------------------------------------------------------------

def detect_captcha(page: Page) -> str | None:
    """يُرجع وصفاً لما اكتُشف، أو None إن لم توجد CAPTCHA ظاهرة."""
    for sel in CAPTCHA_SELECTORS:
        loc = page.locator(sel)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return f"عنصر مطابق للمحدّد: {sel}"
        except Exception:
            continue
    try:
        body = page.inner_text("body", timeout=2000).lower()
        for hint in CAPTCHA_TEXT_HINTS:
            if hint.lower() in body:
                return f"نص مطابق: '{hint}'"
    except PWTimeout:
        pass
    return None


def detect_verification(page: Page) -> str | None:
    """Returns a reason if the page needs Nafath or an SMS one-time code; None otherwise."""
    url = (page.url or "").lower()
    if any(h in url for h in NAFATH_URL_HINTS):
        return f"Nafath / national SSO redirect ({url.split('?')[0]})"
    try:
        body = page.inner_text("body", timeout=2000).lower()
    except Exception:
        body = ""
    # a login wall mentioning Nafath (not just a footer link) — require a visible button/link with that text
    for hint in NAFATH_TEXT_HINTS:
        loc = page.get_by_role("button", name=re.compile(hint, re.I))
        try:
            if hint in body and loc.count() and loc.first.is_visible():
                return f"Nafath login required (button: '{hint}')"
        except Exception:
            pass
    to_phone = re.search(r"mobile|phone|sms|text message|جوال|الجوال|رسالة نصية|الهاتف", body)
    to_email = re.search(r"e-?mail|inbox|البريد|الإيميل|ايميل", body)
    if to_email and not to_phone:
        return None                     # an e-mail code: you enter it yourself at the manual pause
    for sel in OTP_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible() and (to_phone or not to_email):
                return f"SMS one-time code field ({sel})"
        except Exception:
            continue
    if any(h in body for h in OTP_TEXT_HINTS) and to_phone:
        return "SMS verification step"
    return None


_CONFIRMED_VERIFICATIONS: set = set()


def _safe(fn, *args, default=None):
    """Runs a page check; a page that is mid-navigation (login redirect, SSO popup) never crashes the run."""
    try:
        return fn(*args)
    except PWError:
        return default


def _manual_verification_pause(stage: str, reason: str) -> None:
    """Nafath / SMS code / login: the browser stays open and the bot waits as long as you need.
    Enter = done (the form is visible); s = skip this portal for now (marked manual_required)."""
    print("\n" + "━" * 60)
    print(f"🔐 [{stage}] {reason}")
    print("   المتصفح سيبقى مفتوحاً. أكملي نفاذ / رمز SMS / تسجيل الدخول في المتصفح بدون استعجال.")
    print("   Enter = انتهيت والاستمارة ظاهرة   |   s = تخطّي هذه البوابة الآن (تُسجّل manual_required)")
    print("━" * 60)
    while True:
        try:
            ans = input("> ").strip().lower()
        except EOFError:                                   # no terminal attached: can't wait for you
            raise VerificationBlocked(f"{VERIFICATION_BLOCK_NOTE} ({reason}) - no terminal to confirm")
        if ans == "s":
            raise VerificationBlocked(f"{VERIFICATION_BLOCK_NOTE} ({reason}) - skipped by you")
        if ans in ("", "y", "ok", "تم"):
            break
    print("▶ استمرار التعبئة...")


def guard_verification(page: Page, stage: str) -> None:
    """SMS OTP / Nafath / login wall: always wait for you with the browser open (never closes it).
    Each verification step is confirmed once per page, so the bot doesn't ask again in a loop."""
    reason = _safe(detect_verification, page)
    if not reason:
        return
    key = ((page.url or "").split("?")[0], reason)
    if key in _CONFIRMED_VERIFICATIONS:
        return
    _manual_verification_pause(stage, reason)
    _CONFIRMED_VERIFICATIONS.add(key)
    _CONFIRMED_VERIFICATIONS.add(((page.url or "").split("?")[0], _safe(detect_verification, page)))


START_DATE_RE = re.compile(r"start date|starting date|available from|availability date|from date|training start|"
                           r"تاريخ (بداية|بدء) التدريب|تاريخ البدء|تاريخ البداية|بداية التدريب|من تاريخ", re.I)
END_DATE_RE = re.compile(r"end date|ending date|to date|training end|تاريخ (نهاية|انتهاء) التدريب|تاريخ الانتهاء|"
                         r"تاريخ النهاية|نهاية التدريب|إلى تاريخ", re.I)


def coop_date_kind(label: str, ident: str = "") -> str | None:
    text = f"{label} {ident}"
    if re.search(r"birth|ميلاد|graduat|تخرج|expiry|انتهاء الهوية", text, re.I):
        return None
    if START_DATE_RE.search(text):
        return "start"
    if END_DATE_RE.search(text):
        return "end"
    return None


def is_gpa_field(label: str, ident: str = "") -> bool:
    return bool(GPA_LABEL_RE.search(f"{label} {ident}"))


def field_is_required(page: Page, locator: Locator | None = None, label: str = "", required_attr: bool = False) -> bool:
    if required_attr or REQUIRED_MARK_RE.search(label or ""):
        return True
    if locator is not None:
        try:
            return bool(locator.evaluate(
                "el => el.required || el.getAttribute('aria-required') === 'true' || "
                "/\\*|required|mandatory|إلزامي|مطلوب/i.test((el.labels && el.labels[0] && el.labels[0].innerText) || '')"))
        except Exception:
            return False
    return False


SCAN_FIELDS_JS = """(handled) => {
    const run = Date.now();
    const skip = ['hidden','submit','button','reset','image'];
    const visible = el => {
        const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    };
    const byIds = ids => ids.split(/\\s+/).map(i => document.getElementById(i)?.innerText || '').join(' ').trim();
    const ownLabel = el => {
        if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
        const w = el.closest('label'); if (w) return w.innerText.trim();
        const lb = el.getAttribute('aria-labelledby'); if (lb) return byIds(lb);
        return (el.getAttribute('aria-label') || el.placeholder || '').trim();
    };
    const CONTROLS = 'input:not([type=hidden]), select, textarea, [role="combobox"], [aria-haspopup="listbox"]';
    const nearLabel = el => {          // <div class="label">Major</div><div><input></div> — only if the box holds one control
        let c = el.parentElement;
        for (let d = 0; c && d < 4; d++, c = c.parentElement) {
            const controls = [...c.querySelectorAll(CONTROLS)].filter(x => x !== el && !x.contains(el) && !el.contains(x));
            if (controls.length) return '';
            const l = c.querySelector('label, legend, [class*="label" i], [class*="Label"], [class*="question" i]');
            if (l && !l.contains(el)) { const t = (l.innerText || '').trim(); if (t && t.length < 160) return t; }
        }
        return '';
    };
    const PLACEHOLDER = /^(select|choose|please select|pick|search|type to search|start typing|select\\.\\.\\.|اختر|اختاري|--|—|-)?\\s*$/i;
    const groupLabel = el => {
        const g = el.closest('fieldset, [role="radiogroup"], [role="group"]');
        if (g) {
            const lg = g.querySelector('legend'); if (lg) return lg.innerText.trim();
            if (g.getAttribute('aria-label')) return g.getAttribute('aria-label').trim();
            if (g.getAttribute('aria-labelledby')) return byIds(g.getAttribute('aria-labelledby'));
        }
        return el.name || '';
    };
    const out = [], radios = {};
    let i = 0;
    for (const el of document.querySelectorAll('input, select, textarea')) {
        const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : el.tagName.toLowerCase();
        if (skip.includes(type) || el.disabled || !visible(el)) continue;
        el.dataset.rpaId = `rpa-${run}-${i++}`;
        if (type === 'radio') {
            const key = el.name || el.dataset.rpaId;
            if (handled.includes(key)) continue;
            radios[key] ??= {rpa_id: el.dataset.rpaId, ident: key, label: groupLabel(el) || key,
                             type, required: false, empty: true, options: []};
            radios[key].options.push({label: ownLabel(el) || el.value, value: el.value, rpa_id: el.dataset.rpaId});
            if (el.checked) radios[key].empty = false;
            if (el.required) radios[key].required = true;
            continue;
        }
        const label = ownLabel(el) || nearLabel(el);
        const ident = el.name || el.id || el.getAttribute('aria-label') || el.placeholder || label || '';
        if (handled.includes(ident)) continue;
        // react-select / Workday-style searchable dropdowns: an <input role=combobox> whose value lives elsewhere
        const isCombo = type !== 'select' && (el.getAttribute('role') === 'combobox' ||
                        ['list', 'both'].includes(el.getAttribute('aria-autocomplete')));
        let filled = type === 'checkbox' ? el.checked : !!el.value;
        if (isCombo && !filled) {
            const box = el.closest('[class*="container" i], [class*="control" i], [class*="select" i]');
            const v = box && box.querySelector('[class*="singleValue" i], [class*="single-value" i], [class*="selected" i] [class*="value" i]');
            filled = !!(v && (v.innerText || '').trim());
        }
        out.push({
            rpa_id: el.dataset.rpaId, ident, label: label || ident || '(بدون اسم)', type: isCombo ? 'combobox_input' : type,
            required: el.required || el.getAttribute('aria-required') === 'true',
            empty: !filled,
            options: type === 'select'
                ? Array.from(el.options).filter(o => o.value !== '').map(o => ({label: o.text.trim(), value: o.value}))
                : []
        });
    }
    // custom <div> dropdowns (MUI, Headless UI, Workday buttons …): role=combobox / aria-haspopup=listbox
    for (const el of document.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]')) {
        if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) || el.querySelector('input:not([type=hidden]), select')) continue;
        if (!visible(el) || el.getAttribute('aria-disabled') === 'true' || el.closest('[role="search"]')) continue;
        el.dataset.rpaId = `rpa-${run}-${i++}`;
        const label = ownLabel(el) || nearLabel(el);
        const ident = el.id || el.getAttribute('name') || el.getAttribute('aria-label') || label || '';
        if (handled.includes(ident)) continue;
        const txt = (el.innerText || el.textContent || '').trim();
        out.push({rpa_id: el.dataset.rpaId, ident, label: label || ident || '(بدون اسم)', type: 'combobox',
                  required: el.getAttribute('aria-required') === 'true',
                  empty: !txt || PLACEHOLDER.test(txt) || txt === label, options: []});
    }
    return out.concat(Object.values(radios));
}"""


def detect_unknown_fields(page: Page, handled_ids: set[str]) -> list[dict]:
    """
    يمسح الحقول الظاهرة والفارغة غير المغطاة في FIELD_MAP، ويُرجع لكل حقل:
    rpa_id (وسم مؤقت لتحديد العنصر)، ident، label (نص السؤال)، type، required، options.
    مجموعات radio تُعاد كحقل واحد بخياراتها.
    """
    fields = page.evaluate(SCAN_FIELDS_JS, list(handled_ids))
    unknown, unnamed = [], 0
    for f in fields:
        ident = (f["ident"] or "").lower()
        label = (f["label"] or "").lower()
        if not f["empty"]:
            continue
        if is_unnamed(f):                              # no label / name → skip silently, never ask
            handled_ids.add(f["ident"] or "")            # "" too: the scan then skips every nameless input
            unnamed += 1
            continue
        if ident in IGNORED_FIELD_EXACT or any(h in ident or h in label for h in IGNORED_FIELD_HINTS):
            continue
        unknown.append(f)
    if unnamed:
        print(f"   ↷ skipped {unnamed} unlabeled field(s) (search bars / unnamed inputs)")
    return unknown


def apply_answer(page: Page, field: dict, answer: str) -> None:
    """يكتب الإجابة في الحقل حسب نوعه."""
    loc = page.locator(f'[data-rpa-id="{field["rpa_id"]}"]')
    t = field["type"]
    if t == "select":
        try:
            loc.select_option(label=answer)
        except Exception:
            loc.select_option(value=answer)
    elif t == "radio":
        a = answer.strip().lower()
        opt = (next((o for o in field["options"] if o["label"].strip().lower() == a), None)
               or next((o for o in field["options"] if a in o["label"].lower()), None))
        if not opt:
            raise ValueError(f"لا يوجد خيار يطابق '{answer}'")
        page.locator(f'[data-rpa-id="{opt["rpa_id"]}"]').check()
    elif t == "checkbox":
        loc.set_checked(answer.strip().lower() in YES_WORDS)
    elif t in ("combobox", "combobox_input"):
        choose_combobox(page, loc, answer, typed=(t == "combobox_input"), label=field.get("label", ""))
    elif t in FILL_TYPES:
        loc.fill(answer)
    else:
        human_type(page, loc, answer)


OPTION_SELECTOR = ('[role="option"]:visible, [role="listbox"] li:visible, .select2-results__option:visible, '
                   '[data-automation-id="promptOption"]:visible, [class*="menu" i] [class*="option" i]:visible')


def _visible_option_texts(page: Page) -> tuple[Locator, list[str]]:
    opts = page.locator(OPTION_SELECTOR)
    try:
        texts = [t.strip() for t in opts.all_inner_texts()[:150]]
    except PWError:
        texts = []
    return opts, texts


def read_combobox_options(page: Page, field: dict) -> list[str]:
    """Opens a custom dropdown just long enough to read its options."""
    loc = page.locator(f'[data-rpa-id="{field["rpa_id"]}"]')
    try:
        loc.click(timeout=4000)
        for _ in range(6):
            page.wait_for_timeout(300)
            _, texts = _visible_option_texts(page)
            if texts:
                break
        page.keyboard.press("Escape")
        return [t for t in dict.fromkeys(texts) if t]
    except PWError:
        return []


def choose_combobox(page: Page, loc: Locator, answer: str, typed: bool = False, label: str = "") -> None:
    """Picks an option in a custom dropdown. Searchable ones get the answer typed first; when the first answer
    finds nothing, the other profile.json alternatives are tried ("Artificial Intelligence", "Other" …)."""
    tries = [answer]
    key = profile_key(label) if label else None
    if key and isinstance(MM_PROFILE.get(key), list):
        tries += [str(x) for x in MM_PROFILE[key] if str(x) != answer]
    for attempt in tries[:6]:
        loc.click(timeout=5000)
        page.wait_for_timeout(400)
        if typed:
            try:
                loc.fill("")
                loc.press_sequentially(attempt[:30], delay=60)
            except PWError:
                pass
        texts: list[str] = []
        for _ in range(8):                                  # options may load asynchronously
            page.wait_for_timeout(300)
            opts, texts = _visible_option_texts(page)
            if texts:
                break
        hit = _match_option(attempt, texts) if texts else None
        if hit is not None:
            opts.nth(texts.index(hit)).click(timeout=5000)
            return
        page.keyboard.press("Escape")
    raise ValueError(f"no dropdown option matches '{answer}'")


def resolve_unknown_fields(page: Page, fields: list[dict], handled_ids: set[str]) -> list[dict]:
    """
    يمرّر كل حقل غير معروف إلى مدير الذاكرة ويُعبّئه.
    يُرجع الحقول التي يجب أن تُعبّأ يدوياً (موافقات، ملفات، إجابات تخطّيتِها، أو أخطاء).
    """
    manual = []
    for f in fields:
        handled_ids.add(f["ident"])        # يمنع إعادة طرح الحقل نفسه في الدورة التالية
        label = f["label"]

        if f["type"] == "file":                            # attach the matching document, if we have one
            doc = pick_document(label, f["ident"])
            if doc:
                try:
                    page.locator(f'[data-rpa-id="{f["rpa_id"]}"]').set_input_files(doc)
                    print(f"   📎 {label[:50]} ← {os.path.basename(doc)}")
                    polite_delay(page)
                    continue
                except Exception as e:
                    print(f"   ⚠️ could not attach {os.path.basename(doc)}: {e}")
            manual.append(f)
            continue

        if f["type"] in MANUAL_ONLY_TYPES or any(h in label.lower() for h in CONSENT_HINTS):
            manual.append(f)
            continue

        if f["type"] in ("textarea", "text") and COVER_LETTER_RE.search(label) and PROFILE.get("cover_letter_text"):
            try:
                page.locator(f'[data-rpa-id="{f["rpa_id"]}"]').fill(PROFILE["cover_letter_text"])
                print(f"   ✏️ cover letter pasted: {label[:50]}")
                polite_delay(page)
            except PWError as e:
                print(f"   ⚠️ could not paste the cover letter: {type(e).__name__}")
                manual.append(f)
            continue

        date_kind = coop_date_kind(label, f["ident"])
        if date_kind and f["type"] in ("date", "text", "textarea"):
            iso = COOP_START if date_kind == "start" else COOP_END
            value = iso if f["type"] == "date" else "/".join(reversed(iso.split("-")))   # 20/12/2026
            try:
                apply_answer(page, f, value)
                print(f"   ✏️ co-op {date_kind} date = {value}")
                polite_delay(page)
            except Exception as e:
                print(f"   ⚠️ could not fill {date_kind} date: {e}")
                manual.append(f)
            continue

        if is_gpa_field(label, f["ident"]):                # GPA: only when the form requires it
            if GPA_FILL_ONLY_IF_REQUIRED and not field_is_required(page, label=label, required_attr=f["required"]):
                print(f"   ↷ GPA field is optional — left blank: {label}")
                continue
            try:
                apply_answer(page, f, GPA if f["type"] != "select" else
                             next((o["label"] for o in f["options"] if GPA in o["label"]), GPA))
                print(f"   ✏️ GPA (required field) = {GPA}")
                polite_delay(page)
            except Exception as e:
                print(f"   ⚠️ could not fill GPA: {e}")
                manual.append(f)
            continue

        options = f["options"] or ([{"label": "Yes"}, {"label": "No"}] if f["type"] == "checkbox" else None)
        if f["type"] == "combobox" and not options:        # custom dropdown: read its options first
            options = [{"label": t} for t in read_combobox_options(page, f)] or None
        answer = handle_unknown_question(label, "select" if f["type"] == "combobox" else f["type"], options,
                                         ask=ASK_UNKNOWN)
        if answer is None:
            manual.append(f)
            continue

        if detect_captcha(page):           # لا نلمس الصفحة إن ظهرت CAPTCHA أثناء انتظار إجابتك
            manual.append(f)
            continue
        try:
            apply_answer(page, f, answer)
            polite_delay(page)
        except Exception as e:
            print(f"   ⚠️ تعذّرت تعبئة '{label}': {e}")
            manual.append(f)
    return manual


def checkpoint(page: Page, handled_ids: set[str], stage: str) -> None:
    """
    1) CAPTCHA ← توقف تام (page.pause) حتى تحلّيها.
    2) حقول غير معروفة ← مدير الذاكرة أولاً (من الذاكرة أو بسؤالك في الطرفية).
    3) ما تبقّى (موافقات، ملفات، ما تخطّيتِه) ← توقف تام للتعبئة اليدوية.
    تُعاد الدورة بعد كل خطوة لالتقاط الحقول الشرطية التي تظهر بعد إجابة سابقة.
    """
    transient = 0
    while True:
        try:
            done = _checkpoint_pass(page, handled_ids, stage)
        except PWError as e:                    # the page navigated (login redirect / SSO) mid-scan
            msg = str(e).lower()
            if transient < 5 and any(k in msg for k in ("context was destroyed", "navigat", "detached", "timeout")):
                transient += 1
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except PWError:
                    page.wait_for_timeout(2000)
                continue
            raise
        if done:
            return


def _checkpoint_pass(page: Page, handled_ids: set[str], stage: str) -> bool:
    """One scan of the page. Returns True when there is nothing left to do."""
    if True:
        guard_verification(page, stage)        # Nafath / SMS code / login → wait for you, browser stays open
        captcha = detect_captcha(page)
        if captcha:
            print(f"\n🛑 [{stage}] CAPTCHA مكتشفة ← {captcha}")
            print("   حلّيها يدوياً ثم اضغطي Resume في Playwright Inspector.\n")
            page.pause()
            return False

        unknown = detect_unknown_fields(page, handled_ids)
        if not unknown:
            return True

        manual = resolve_unknown_fields(page, unknown, handled_ids)
        if manual:
            print(f"\n📝 [{stage}] حقول تُركت لكِ لتعبئتها في المتصفح:")
            for f in manual:
                req = " (إلزامي)" if f["required"] else ""
                print(f"   - {f['label'][:80]} [{f['type']}]{req}")
            if ASK_UNKNOWN:
                print("   عبّئيها في المتصفح ثم اضغطي Resume.\n")
                page.pause()
            else:                                      # --no-ask: no pause; you fill them before Submit
                print("   (--no-ask) عبّئيها قبل الإرسال — سيتوقف البوت للمراجعة في النهاية.\n")
        return False


# ---------------------------------------------------------------------------
# 4) منطق التعبئة
# ---------------------------------------------------------------------------

def find_first(page: Page, selectors: list[str], visible_only: bool = False) -> Locator | None:
    """First element matching any selector. visible_only skips hidden copies (custom widgets often keep a
    hidden <input> next to the visible one)."""
    for sel in selectors:
        loc = page.locator(sel)
        n = loc.count()
        if not n:
            continue
        if not visible_only:
            return loc.first
        for i in range(min(n, 10)):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    return None


def field_identifier(locator: Locator) -> str:
    return locator.evaluate(
        "el => el.name || el.id || el.getAttribute('aria-label') || el.placeholder || '(بدون اسم)'"
    )


def fill_known_fields(page: Page) -> set[str]:
    handled: set[str] = set()
    for field in FIELD_MAP:
        value = PROFILE.get(field["key"])
        is_upload = field["action"] == "upload"
        # file inputs are usually hidden behind a styled button: never require visibility or scroll for them
        loc = find_first(page, field["selectors"], visible_only=not is_upload)
        if loc is None or not value:
            continue

        handled.add(field_identifier(loc))
        try:                                                # already filled (by the first scan or by you): leave it
            if loc.evaluate("e => e.type === 'file' ? !!(e.files && e.files.length) : "
                            "e.tagName === 'SELECT' ? e.selectedIndex > 0 && !!e.value : !!e.value"):
                continue
        except PWError:
            pass

        guard_verification(page, f"before {field['key']}")
        # فحص قبل كل تفاعل: إن ظهرت CAPTCHA فجأة نتوقف قبل اللمس
        if detect_captcha(page):
            checkpoint(page, handled, f"قبل تعبئة {field['key']}")

        action = field["action"]
        if action == "smart":
            try:
                tag = loc.evaluate("e => e.tagName")
            except PWError:
                continue
            if tag == "SELECT":
                opts = loc.evaluate("e => [...e.options].filter(o => o.value !== '').map(o => o.text.trim())")
                choice = profile_answer(field["hint"], opts)
                if not choice:
                    print(f"   ↷ {field['key']}: no matching option — left for you")
                    continue
                value, action = choice, "select"
            else:
                action = "type"
        if action == "gpa_if_required":
            if GPA_FILL_ONLY_IF_REQUIRED and not field_is_required(page, loc):
                print("↷  GPA field is optional — left blank")
                continue
            action = "type"
        print(f"✏️  {field['key']}")
        try:
            if action == "type":
                human_type(page, loc, value)
            elif action == "select":
                loc.select_option(label=value, timeout=5000)
            elif action == "check":
                loc.check(timeout=5000)
            elif action == "upload":
                loc.set_input_files(value, timeout=10000)    # works on hidden inputs, no scrolling
        except FieldNotVisible:
            print(f"   ↷ {field['key']}: hidden field, skipped")
            continue
        except Exception as e:
            print(f"   ⚠️ {field['key']}: {type(e).__name__} — left for you")
            continue

        polite_delay(page)
    return handled


# ---------------------------------------------------------------------------
# Multi-page wizards (Workday, Oracle, Taleo, SuccessFactors, Bain …)
# ---------------------------------------------------------------------------

NAV_BUTTONS_JS = r"""() => {
    const visible = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const NEXT = /^(next( page| step)?|continue( to next step)?|continue to next|save (and|&) continue|save (and|&) next|save & continue|save & next|proceed|review|go to review|التالي|متابعة|استمرار|حفظ و ?متابعة|حفظ و ?التالي)$/i;
    const SUBMIT = /^(submit( (my )?application)?|send( (my )?application)?|apply|apply now|finish|complete( (my )?application)?|review (and|&) submit|confirm (and|&) submit|إرسال|ارسال|تقديم|تقديم الطلب|إرسال الطلب|إنهاء)$/i;
    const BAD = el => el.closest('[class*="carousel" i], [class*="slider" i], [class*="swiper" i], [class*="pagination" i], nav, header, [role="tablist"], [aria-roledescription="carousel"]');
    const out = {next: [], submit: []};
    const run = Date.now(); let i = 0;
    for (const el of document.querySelectorAll('button, input[type="submit"], input[type="button"], [role="button"], a')) {
        if (el.tagName === 'A' && !/btn|button/i.test(el.className || '') && el.getAttribute('role') !== 'button') continue;
        if (!visible(el) || el.disabled || el.getAttribute('aria-disabled') === 'true' || BAD(el)) continue;
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').replace(/[›»→>]+$/, '').trim();
        const auto = [el.getAttribute('data-automation-id'), el.id, el.name, el.getAttribute('data-testid')].join(' ');
        let kind = null;
        if (SUBMIT.test(t) || (/submit/i.test(auto) && !NEXT.test(t))) kind = 'submit';
        else if (NEXT.test(t) || /pagefooternextbutton|bottom-navigation-next|(^|[-_ ])next([-_ ]|$)|continue|save.?continue|save.?next/i.test(auto)) kind = 'next';
        if (!kind) continue;
        el.dataset.rpaNav = `nav-${run}-${i++}`;
        out[kind].push({id: el.dataset.rpaNav, text: (t || auto).slice(0, 40)});
    }
    return out;
}"""

REQUIRED_EMPTY_JS = r"""() => {
    const visible = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const labelOf = el => {
        if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return l.innerText.trim(); }
        const w = el.closest('label'); if (w) return w.innerText.trim();
        const lb = el.getAttribute('aria-labelledby');
        if (lb) return lb.split(/\s+/).map(i => document.getElementById(i)?.innerText || '').join(' ').trim();
        return (el.getAttribute('aria-label') || el.placeholder || el.name || el.id || '').trim();
    };
    const out = [], radios = new Set();
    for (const el of document.querySelectorAll('input, select, textarea, [role="combobox"]')) {
        const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : el.tagName.toLowerCase();
        if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || el.disabled) continue;
        const req = el.required || el.getAttribute('aria-required') === 'true';
        const invalid = el.getAttribute('aria-invalid') === 'true';
        if (!req && !invalid) continue;
        // custom radios / checkboxes / uploads hide the real input: judge by their label or wrapper instead
        const shown = visible(el) || [el.closest('label'), el.parentElement].some(x => x && visible(x));
        if (!shown) continue;
        let empty;
        if (type === 'radio') {
            if (!el.name || radios.has(el.name)) continue;
            radios.add(el.name);
            empty = !document.querySelector(`input[type=radio][name="${CSS.escape(el.name)}"]:checked`);
        } else if (type === 'checkbox') empty = !el.checked;
        else if (type === 'file') empty = !(el.files && el.files.length);
        else if (!['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) empty = !(el.innerText || '').trim() || /^(select|choose|اختر)/i.test((el.innerText || '').trim());
        else empty = !el.value;
        if (empty || invalid) out.push(`${(labelOf(el) || '(unlabeled)').slice(0, 70)} [${type}]${invalid ? ' (marked invalid)' : ''}`);
    }
    return out.slice(0, 15);
}"""

PAGE_FINGERPRINT_JS = r"""() => {
    const names = [...document.querySelectorAll('input, select, textarea')].filter(e => e.offsetParent !== null)
        .map(e => e.name || e.id || e.type).join('|');
    const head = [...document.querySelectorAll('h1, h2, h3, [aria-current="step"], [class*="active" i][class*="step" i]')]
        .slice(0, 6).map(e => (e.innerText || '').trim().slice(0, 60)).join('|');
    return location.href.split('#')[0] + '§' + head + '§' + names;
}"""

ERRORS_JS = r"""() => [...document.querySelectorAll('[role="alert"], [aria-live="assertive"], [class*="error" i]:not(input):not(select):not(textarea), [class*="invalid" i]:not(input)')]
    .filter(e => e.offsetParent !== null).map(e => (e.innerText || '').trim()).filter(t => t && t.length < 200).slice(0, 6)"""


def nav_buttons(page: Page) -> dict:
    return page.evaluate(NAV_BUTTONS_JS)


def required_empty(page: Page) -> list[str]:
    return page.evaluate(REQUIRED_EMPTY_JS)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "s"


def advance_step(context, page: Page, step: int) -> tuple[str, Page]:
    """After a step is filled: decides whether to click Next. Returns ('next' | 'final' | 'stop', page).
    'final' = a Submit button (or no Next button) is on the page; 'stop' = you chose to stop / it couldn't advance."""
    nav = _safe(nav_buttons, page, default={"next": [], "submit": []}) or {"next": [], "submit": []}
    if nav["submit"] or not nav["next"]:
        return "final", page
    if step >= MAX_WIZARD_STEPS:
        print(f"\n⏸️ {MAX_WIZARD_STEPS} steps filled — stopping here for your review.")
        return "stop", page
    while True:
        blockers = _safe(required_empty, page, default=[]) or []
        if blockers:
            print(f"\n🛑 Step {step}: required field(s) still empty — Next would fail:")
            for b in blockers:
                print(f"   - {b}")
            print("   عبّئيها في المتصفح (المتصفح مفتوح).")
            ans = _ask("Enter = عبّأتها، اضغط Next | s = توقّف هنا للمراجعة: ")
            if ans == "s":
                return "stop", page
            continue
        if not AUTO_ADVANCE_STEPS:
            ans = _ask(f"Step {step} filled. Enter = click “{nav['next'][0]['text']}” and fill the next step | s = stop here: ")
            if ans == "s":
                return "stop", page
        before = _safe(page.evaluate, PAGE_FINGERPRINT_JS, default="")
        btn = page.locator(f'[data-rpa-nav="{nav["next"][-1]["id"]}"]')
        print(f"➡️  Step {step} → clicking “{nav['next'][-1]['text']}”")
        try:
            btn.scroll_into_view_if_needed(timeout=3000)
            btn.click(timeout=8000)
        except PWError as e:
            print(f"   ⚠️ couldn't click Next ({type(e).__name__})")
        changed = False
        for _ in range(30):                                   # up to ~15 s for the next step to load
            page.wait_for_timeout(500)
            page = _active_page(context, page)
            now = _safe(page.evaluate, PAGE_FINGERPRINT_JS, default=None)
            if now is not None and now != before:
                changed = True
                break
        if changed:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PWError:
                pass
            page.wait_for_timeout(1200)                       # let the step's fields render
            return "next", page
        errors = _safe(page.evaluate, ERRORS_JS, default=[]) or []
        print(f"\n🛑 Step {step}: the page did not move on after Next.")
        for e in errors:
            print(f"   ! {e[:150]}")
        ans = _ask("صحّحي في المتصفح ثم Enter = إعادة المحاولة | s = توقّف هنا للمراجعة: ")
        if ans == "s":
            return "stop", page
        nav = _safe(nav_buttons, page, default={"next": [], "submit": []}) or {"next": [], "submit": []}
        if nav["submit"] or not nav["next"]:
            return "final", page


def fill_wizard(context, page: Page) -> Page:
    """Fills step after step (up to MAX_WIZARD_STEPS). Returns the page where the final review happens."""
    step = 1
    while True:
        seen: set[str] = set()
        checkpoint(page, seen, f"step {step}")
        handled = fill_known_fields(page) | seen
        checkpoint(page, handled, f"step {step} (re-check)")
        state, page = advance_step(context, page, step)
        if state != "next":
            return page
        step += 1
        guard_verification(page, f"step {step}")


def confirm_and_submit(page: Page) -> str:
    """Mandatory review pause after filling. The browser stays open until you answer.
    Returns 'manual' (you submitted it yourself), 'submitted' (you asked the bot to click Submit)
    or 'not_submitted'."""
    submit = find_first(page, ["button[type='submit']", "input[type='submit']", "button:has-text('Submit')",
                               "button:has-text('Apply')", "button:has-text('إرسال')", "button:has-text('تقديم')"],
                        visible_only=True)
    if submit is not None:
        try:
            submit.scroll_into_view_if_needed(timeout=3000)
            submit.highlight()
        except Exception:
            pass
    print("\n" + "━" * 60)
    print("⏸️  انتهت التعبئة. راجعي كل الحقول والمرفقات في المتصفح، واضغطي Submit بنفسك.")
    print("   s = أرسلتُ الطلب بنفسي   |   b = اضغط Submit عني   |   n = لم أرسله (أتخطاه الآن)")
    while True:
        ans = input("اختيارك (s / b / n): ").strip().lower()
        if ans in ("s", "y", "تم"):
            return "manual"
        if ans == "n":
            return "not_submitted"
        if ans == "b":
            if submit is None:
                print("لم أجد زر إرسال ظاهراً؛ اضغطيه بنفسك ثم اكتبي s.")
                continue
            polite_delay(page)
            submit.click()
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            print("✅ تم الضغط على Submit.")
            return "submitted"


# ---------------------------------------------------------------------------
# 5) تهيئة المتصفح والتشغيل
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Navigation — the about:blank stall
# ---------------------------------------------------------------------------
# page.goto(wait_until="domcontentloaded") hangs forever when the URL never settles: a DNS failure, a redirect
# loop, or a portal that lands on a login wall whose "load" event never fires. The browser then sits on
# about:blank and the whole run looks frozen. Fix: try domcontentloaded first, fall back to "load", and if the
# page still isn't real, retry once after a short wait. Never let a bad navigation block the queue.
MOBILE_REDIRECT_RE = re.compile(
    r"open in (the )?(app|mobile)|download (the )?(app|mobile)|install the app|get the app|"
    r"mobile app|app store|play store|google play|/store/apps|/m/|mobile\.|m\.site", re.I)
MOBILE_URL_RE = re.compile(r"/m/|mobile\.|m\.site|\.m\.", re.I)


def is_mobile_redirect(page: Page, url: str) -> bool:
    """A portal that wants a phone app instead of a browser form can't be filled here — skip it cleanly."""
    if MOBILE_URL_RE.search(url or ""):
        return True
    try:
        body = page.inner_text("body", timeout=2000).lower()
        has_form = page.locator("input:visible, select:visible, textarea:visible, button:visible").count() > 2
    except Exception:
        body, has_form = "", True
    # only when the page offers nothing to fill: most career sites mention their app in the footer
    return bool(MOBILE_REDIRECT_RE.search(body or "")) and not has_form


def open_page(page: Page, url: str) -> str:
    """Returns 'ok', 'mobile' (skipped) or raises if the page never loads."""
    err = None
    for wait in ("domcontentloaded", "load"):
        try:
            page.goto(url, wait_until=wait, timeout=45000)
            err = None
            break
        except PWTimeout as e:
            print(f"ℹ️ goto timed out on {wait} — retrying with a longer wait")
            err = e
            try:
                page.wait_for_timeout(3000)
            except Exception:
                pass
    if err is not None:
        raise err
    page.wait_for_timeout(1500)
    final = (page.url or "").strip()
    if not final.startswith("http"):
        raise RuntimeError(f"browser stuck on {final!r} (about:blank) — the URL never loaded")
    if is_mobile_redirect(page, final):
        print(f"📱 mobile/app redirect detected ({final[:80]}) — skipping, filling this one by phone")
        return "mobile"
    return "ok"


FORM_PRESENCE_JS = r"""() => {
    const visible = el => { const r = el.getBoundingClientRect(), s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const SEARCH = /search|query|keyword|newsletter|subscribe|بحث/i;
    let inputs = 0;
    for (const el of document.querySelectorAll('input, select, textarea, [role="combobox"]')) {
        const type = el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : el.tagName.toLowerCase();
        if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || !visible(el)) continue;
        const id = [el.name, el.id, el.placeholder, el.getAttribute('aria-label')].join(' ');
        if (SEARCH.test(id) || el.closest('[role="search"], form[action*="search" i], form[role="search"]')) continue;
        inputs++;
    }
    const APPLY = /\b(apply|start (your )?application|submit (your )?(cv|resume|application)|easy apply|i'?m interested|register)\b|قدم|قدّم|تقديم|التقديم|سجل/i;
    let apply = 0;
    for (const el of document.querySelectorAll('a, button, [role="button"], input[type="submit"], input[type="button"]')) {
        const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
        if (t && t.length < 60 && APPLY.test(t) && visible(el)) apply++;
    }
    const FORM_FRAMES = /greenhouse|lever|workable|smartrecruiters|ashby|zoho|bamboohr|recruitee|teamtailor|myworkday|successfactors|taleo|icims|oraclecloud|jisr|docs\.google\.com\/forms|forms\.gle|typeform|jotform|forms\.office|microsoft\.com\/forms/i;
    const frames = [...document.querySelectorAll('iframe')].filter(f => FORM_FRAMES.test(f.src || '') && visible(f)).length;
    return {inputs, apply, frames};
}"""


def form_presence(page: Page) -> dict:
    """Visible form fields (site search bars excluded), Apply buttons/links and embedded application forms,
    counted across the page and its frames."""
    total = {"inputs": 0, "apply": 0, "frames": 0}
    for fr in page.frames:
        try:
            got = fr.evaluate(FORM_PRESENCE_JS)
        except PWError:
            continue
        for k in total:
            total[k] += got.get(k, 0)
    return total


def _active_page(context, current: Page) -> Page:
    """The tab to work in: the current one, unless it closed or the form opened in a new tab / popup."""
    pages = [p for p in context.pages if not p.is_closed()]
    if not pages:
        return current
    if current.is_closed():
        current = pages[-1]
    if _safe(form_presence, current, default={"inputs": 0})["inputs"] == 0:
        for p in reversed(pages):
            if p is not current and _safe(form_presence, p, default={"inputs": 0})["inputs"] > 0:
                print(f"↪ switching to the tab with the form: {(p.url or '')[:80]}")
                return p
    return current


def wait_for_form(context, page: Page) -> tuple[str, Page]:
    """Waits — with the browser open — until you confirm the application form is visible.
    Warns when the page has no form fields and no Apply button, so a dead page can be rejected at once.
    Returns ('ok' | 'no_form' | 'manual_later', page). Raises VerificationBlocked only if you choose to skip."""
    forced = False
    while True:
        page = _active_page(context, page)
        pres = _safe(form_presence, page, default=None)
        if pres is None:                                # mid-navigation: give it a moment
            page.wait_for_timeout(2000)
            continue
        verif = _safe(detect_verification, page)
        no_form = pres["inputs"] == 0 and pres["frames"] == 0
        print("\n" + "━" * 60)
        if verif:
            print(f"🔐 {verif}")
            print("   المتصفح سيبقى مفتوحاً — أكملي التحقق / تسجيل الدخول في المتصفح.")
            prompt = "Enter = الاستمارة ظاهرة الآن | w = انتظار وإعادة الفحص | s = تخطّي البوابة (manual) | r = رفض: "
        elif no_form and not pres["apply"]:
            print("⚠️  NO APPLICATION FORM DETECTED on this page — 0 form fields and no Apply button/link.")
            print("    لم يُعثر على أي استمارة تقديم أو زر Apply في هذه الصفحة (غالباً صفحة وظائف ميتة أو قائمة فقط).")
            print("    r = رفضها الآن (rejected)  |  m = تحتاج عملاً يدوياً لاحقاً  |  w = انتظار وإعادة الفحص  |  Enter = متابعة على أي حال")
            prompt = "اختيارك (r / m / w / Enter): "
        elif no_form:
            print(f"ℹ️  No form fields yet, but {pres['apply']} Apply button/link(s) found — click Apply in the browser "
                  "(log in if asked) until the form shows.")
            prompt = "Enter = الاستمارة ظاهرة الآن | w = إعادة الفحص | r = رفض | m = يدوي لاحقاً: "
        else:
            print(f"✅ Form detected ({pres['inputs']} fields{', embedded form' if pres['frames'] else ''}). "
                  "سجّلي الدخول أو أغلقي النوافذ المنبثقة إن لزم.")
            prompt = "Enter = ابدأ التعبئة | r = رفض | m = يدوي لاحقاً: "
        try:
            ans = input(prompt).strip().lower()
        except EOFError:
            raise VerificationBlocked("no terminal to confirm the form is visible")
        if ans == "r":
            return "no_form", page
        if ans == "m":
            return "manual_later", page
        if ans == "s" and verif:
            raise VerificationBlocked(f"{VERIFICATION_BLOCK_NOTE} ({verif}) - skipped by you")
        if ans == "w":
            page.wait_for_timeout(4000)
            continue
        if verif or no_form:                             # you pressed Enter: check again before filling
            page.wait_for_timeout(1500)
            page = _active_page(context, page)
            again = _safe(form_presence, page, default={"inputs": 0, "frames": 0})
            still_verif = _safe(detect_verification, page)
            if (again["inputs"] or again["frames"]) and not still_verif:
                return "ok", page
            if forced:
                if still_verif:
                    _CONFIRMED_VERIFICATIONS.add(((page.url or "").split("?")[0], still_verif))
                return "ok", page
            print("   ما زالت الصفحة بلا استمارة / التحقق ظاهر. Enter مرة أخرى = متابعة على أي حال، w = انتظار.")
            forced = True
            continue
        return "ok", page


def apply_stealth(context, page: Page) -> None:
    """يدعم الإصدار 2.x (Stealth class) والإصدار 1.x (stealth_sync) من playwright-stealth."""
    try:
        from playwright_stealth import Stealth          # v2.x
        Stealth().apply_stealth_sync(context)
        return
    except Exception:
        pass
    try:
        from playwright_stealth import stealth_sync      # v1.x
        stealth_sync(page)
    except Exception as e:
        print(f"ℹ️ playwright-stealth unavailable ({type(e).__name__}); continuing without it")


class ResumeMissing(Exception):
    pass


def _safe_close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


def _launch_context(p, launch: dict):
    """Opens the persistent browser profile. Recovers from the two failures seen in practice:
    a missing browser build, and a stale profile lock left by a crashed run."""
    import glob
    import subprocess
    for attempt in range(2):
        try:
            return p.chromium.launch_persistent_context(USER_DATA_DIR, **launch)
        except Exception as e:
            msg = str(e)
            if attempt == 1:
                raise
            if "Executable doesn't exist" in msg:
                subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
            elif "ProcessSingleton" in msg or "profile" in msg.lower() or "SingletonLock" in msg:
                for f in glob.glob(os.path.join(USER_DATA_DIR, "Singleton*")):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                print("ℹ️ removed a stale browser-profile lock from a previous crash; retrying")
            else:
                raise


def run(url: str, resume_path: str | None = None, ask: bool | None = None, cover_letter: dict | None = None,
        auto_advance: bool | None = None, watch: bool = False) -> str:
    """Fills one application with the given (tailored) CV. Returns the confirm_and_submit() outcome.
    ask=False (or --no-ask): unknown fields are left blank instead of asked about in the terminal.
    Other outcomes: 'mobile', 'no_form' (you rejected a page without a form), 'manual_later'.
    Nafath / SMS / login pages: waits for you with the browser open; VerificationBlocked only if you skip.""" 
    import os
    if ask is not None:
        set_no_ask(not ask)
    if auto_advance is not None:
        set_auto_advance(auto_advance)
    elif not ASK_UNKNOWN:
        set_auto_advance(True)                        # --no-ask always walks through wizard steps by itself
    PROFILE["transcript_path"] = document_path("transcript")
    PROFILE["cover_letter_path"] = (cover_letter or {}).get("pdf", "") if (cover_letter or {}).get("ok") else ""
    PROFILE["cover_letter_text"] = (cover_letter or {}).get("text", "") if (cover_letter or {}).get("ok") else ""
    resume = resume_path or RESUME_PATH
    if not os.path.isfile(resume):
        raise ResumeMissing(f"{resume} not found — refusing to auto-apply without a resume.")
    PROFILE["cv_path"] = resume
    with sync_playwright() as p:
        launch = dict(headless=False,                 # وضع مرئي لمتابعة العمليات
                      slow_mo=50,                     # إبطاء طفيف لكل أمر Playwright
                      viewport={"width": 1366, "height": 850},
                      locale="en-US",
                      timezone_id="Asia/Riyadh")
        context = _launch_context(p, launch)
        # a persistent profile can reopen old tabs: keep exactly one
        page = context.pages[0] if context.pages else context.new_page()
        for extra in context.pages[1:]:
            _safe_close(extra)
        apply_stealth(context, page)

        try:
            print(f"🌐 فتح: {url}")
            status = open_page(page, url)
            if status == "mobile":
                return "mobile"
            # wait (browser open) until you confirm the form is visible; warns when there is no form at all
            status, page = wait_for_form(context, page)
            if status != "ok":
                return status                               # 'no_form' or 'manual_later'
            try:
                # every step of the form (one page, or a Workday / Oracle / Taleo wizard); stops at the Submit page
                page = fill_wizard(context, page)
            except (VerificationBlocked, KeyboardInterrupt, ResumeMissing):
                raise
            except Exception as e:                          # never drop an open form because of one widget
                print(f"\n⚠️ Auto-fill stopped ({type(e).__name__}: {str(e)[:150]}). The browser stays open — "
                      "finish the form yourself, then answer below.")
                page = _active_page(context, page)

            outcome = confirm_and_submit(page)
            if outcome == "submitted":
                guard_verification(page, "after submit")   # some portals ask for an SMS code to finish
            return outcome
        finally:
            for pg in list(context.pages):                  # popups / extra tabs the portal opened
                _safe_close(pg)
            _safe_close(context)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Fill one application form (stops before Submit for your review).")
    ap.add_argument("url", help="application form URL")
    ap.add_argument("resume", nargs="?", default=None, help="CV PDF (default: resume.pdf)")
    ap.add_argument("--no-ask", action="store_true",
                    help="never ask in the terminal: fill what profile/memory knows, leave unknown fields blank")
    ap.add_argument("--auto-advance", action="store_true",
                    help="multi-page forms: click Next / Save and Continue by itself after each step")
    args = ap.parse_args()
    if args.auto_advance:
        set_auto_advance(True)
    if args.no_ask:
        set_no_ask(True)
    print("⚠️ التشغيل المباشر لا يسجّل الطلب في قاعدة البيانات؛ استخدمي apply_queue.py لمنع التقديم المكرر.")
    if not ASK_UNKNOWN:
        print("ℹ️ --no-ask: الحقول غير المعروفة تُترك فارغة لتعبئتها في المتصفح، بلا أسئلة في الطرفية.")
    try:
        print(run(args.url, args.resume))
    except ResumeMissing as e:
        sys.exit(f"❌ {e}")
    except VerificationBlocked as e:
        sys.exit(f"⛔ {e}")
