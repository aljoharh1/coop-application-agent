"""
resolve_careers_and_queue.py — turns the top 'reviewed' company leads into queued applications.

For the top N reviewed companies (default 80, ranked by the harvest Fit Score from
Riyadh_400_AI_Companies_Ranked.xlsx, or the DB score when a company is not in the sheet), it:

  0. first runs cleanup_db.py: directory/listing rows and offshore-only companies are rejected,
     junk names are cleaned, and rows that were queued on a service/vendor page go back to 'reviewed';
  1. finds the company's real careers / apply URL with requests + BeautifulSoup (no browser):
     website links ("Careers", "Jobs", "Join us", "وظائف", ...), common paths (/careers, /jobs, ...),
     job-board systems embedded or linked from the site, and, when a lead has no website, one DuckDuckGo
     lookup (only results on the company's own domain or on its job board are accepted);
  2. on a job board (Workable, Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Breezy, BambooHR,
     Zoho Recruit, Manatal) it resolves down to the direct internship / co-op (or entry-level) posting in
     Saudi Arabia and its /apply URL — a board with no such posting is NOT queued;
  3. skips:
       • offshore-only companies / postings (India, Pakistan, UAE … with no Saudi office or role)
       • closed postings ("no open positions", past deadline, "انتهى التقديم")
       • any GPA rule above your GPA (from config.py) or a "Very Good"/"Excellent" grade (same check as apply_queue.py)
       • dead careers pages: no form, no Apply button, no job links (an e-mail-only page is reported, not queued)
       • Nafath / SMS-OTP portals only when config.py says you can't do them (CAN_USE_NAFATH / CAN_RECEIVE_SMS_OTP)
       • unreachable sites and pages that robots.txt disallows;
  4. updates each qualifying row: apply_url, apply_type='external', status='queued'.

--recheck-queued re-verifies rows this script queued earlier (and queued job-board roots such as
apply.workable.com/mozn-ai/): a better posting URL replaces the old one; offshore → rejected;
closed / dead / no matching posting → back to 'reviewed' with the reason in the notes.

Nothing is submitted and nobody is contacted: queued rows still go through apply_queue.py, which
re-reads the posting, re-checks the GPA rule and stops before Submit for your review.

Usage:
    python resolve_careers_and_queue.py                   # top 80 reviewed companies
    python resolve_careers_and_queue.py --top 40
    python resolve_careers_and_queue.py --recheck-queued  # re-verify what is already queued
    python resolve_careers_and_queue.py --dry-run         # show what would change, write nothing
    python resolve_careers_and_queue.py --no-search       # never use the DuckDuckGo fallback
"""

import argparse
import csv
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import app_db
import ats_resolver
import cleanup_db
from apply_queue import gpa_requirement_reason
from company_filters import (ats_board, clean_company_name, is_ats_vendor_page, is_directory, not_careers_reason,
                             offshore_reason, root_domain)
from config import CAN_RECEIVE_SMS_OTP, CAN_USE_NAFATH

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")      # Windows console + Arabic names
except Exception:
    pass

BASE = Path(__file__).resolve().parent
XLSX = BASE / "Riyadh_400_AI_Companies_Ranked.xlsx"
REPORT_DIR = BASE / "reports"
TODAY = date.today()
SKIP_VERIFICATION_PORTALS = not (CAN_USE_NAFATH or CAN_RECEIVE_SMS_OTP)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
TIMEOUT = 12
MAX_FETCHES_PER_COMPANY = 10

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

# Boards that need a browser (no public API): kept as-is when linked from a careers page
OTHER_ATS_HOSTS = ("myworkdayjobs.com", "successfactors.com", "successfactors.eu", "hr.cloud.sap", "taleo.net",
                   "oraclecloud.com", "icims.com", "menaitech.com", "jobvite.com", "avature.net", "eightfold.ai",
                   "applytojob.com", "pinpointhq.com", "personio.de", "personio.com", "teamtailor.com",
                   "freshteam.com", "hire.trakstar.com", "rippling.com", "polymer.co", "gohire.io", "jisr.net")
# never a company's own careers page
NOISE_HOSTS = (
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com", "tiktok.com",
    "wikipedia.org", "glassdoor.", "indeed.", "bayt.com", "naukrigulf.com", "gulftalent.com", "crunchbase.com",
    "tracxn.com", "zawya.com", "argaam.com", "google.com", "apple.com/app", "play.google.com", "wa.me",
    "whatsapp.com", "t.me", "snapchat.com", "startupblink.com", "datacentermap.com", "clutch.co",
    "goodfirms.co", "duckduckgo.com", "bing.com", "yelp.", "tripadvisor.", "zoominfo.com", "rocketreach.co",
)
CAREER_LINK_RE = re.compile(
    r"\b(careers?|jobs?|join[\s_-]*(us|our[\s_-]*team)|work[\s_-]*with[\s_-]*us|vacanc\w*|we.?re[\s_-]*hiring|"
    r"open[\s_-]*positions|job[\s_-]*openings)\b|وظائف|الوظائف|التوظيف|انضم|فرص\s*العمل|الفرص\s*الوظيفية", re.I)
CAREER_PATHS = ("/careers", "/careers/", "/career", "/jobs", "/en/careers", "/en/career", "/join-us",
                "/careers.html", "/ar/careers", "/about/careers", "/company/careers")
INTERN_RE = re.compile(
    r"\b(intern(ship)?s?|co-?op|coop|cooperative\s+training|trainee(ship)?|graduate\s+(program|programme|development)|"
    r"summer\s+training|students?\s+program)\b|تدريب\s*تعاوني|التدريب\s*التعاوني|متدرب|برنامج\s*تدريب|حديثي\s*التخرج", re.I)
CLOSED_RE = re.compile(
    r"no\s+(current\s+)?(open|available)\s+(positions|roles|vacancies|jobs|opportunities)|"
    r"no\s+(current\s+)?(openings|vacancies)|there\s+are\s+(currently\s+)?no\s+(open\s+)?(positions|jobs|vacancies|openings)|"
    r"(applications?|registration|this\s+(job|position|posting))\s+(is|are|has\s+been)\s+(now\s+)?closed|"
    r"no\s+longer\s+(accepting\s+applications|available|open)|position\s+has\s+been\s+filled|job\s+(has\s+)?expired|"
    r"لا\s*توجد\s*(وظائف|فرص|شواغر)|انتهى\s*(التقديم|التسجيل)|تم\s*إغلاق\s*(التقديم|التسجيل|الوظيفة)|اغلاق\s*التقديم|التقديم\s*مغلق", re.I)
VERIFY_RE = re.compile(r"\bnafath\b|نفاذ|النفاذ\s*الوطني|\babsher\b|أبشر|sms\s*(otp|verification\s+code)|"
                       r"one[\s-]*time\s+password\s+(sent\s+)?(to|via)\s+(your\s+)?(mobile|phone)", re.I)
DEADLINE_RE = re.compile(
    r"(deadline|closing\s+date|apply\s+(by|before)|last\s+date|آخر\s*موعد|تاريخ\s*الإغلاق|ينتهي\s*التقديم)"
    r"[^0-9A-Za-z؀-ۿ]{0,15}(?P<d>[0-9]{1,4}[/.\-][0-9]{1,2}[/.\-][0-9]{2,4}|[0-9]{1,2}\s+[A-Za-z]{3,9}\s+[0-9]{4}|"
    r"[A-Za-z]{3,9}\s+[0-9]{1,2},?\s+[0-9]{4})", re.I)
JS_SHELL_RE = re.compile(r'id=["\'](root|app|__next|__nuxt)["\']|ng-app|data-reactroot', re.I)
# what a live careers page offers: an Apply button / link, job links, or a real form
APPLY_TEXT_RE = re.compile(
    r"\b(apply(\s+(now|here|online|today))?|apply\s+for\s+(this|the)\s+(job|position|role)|start\s+(your\s+)?application|"
    r"submit\s+(your\s+)?(cv|resume|application)|upload\s+(your\s+)?(cv|resume)|send\s+(us\s+)?(your\s+)?(cv|resume)|"
    r"view\s+(all\s+)?(jobs|openings|positions|vacancies|opportunities)|see\s+(all\s+)?(open\s+)?(positions|roles|jobs)|"
    r"open\s+positions|current\s+openings|job\s+openings|explore\s+(jobs|roles|opportunities))\b|"
    r"قدم\s*الآن|قدّم\s*الآن|التقديم|تقديم\s*طلب|أرسل\s*سيرتك|ارسل\s*سيرتك|الوظائف\s*المتاحة|الشواغر", re.I)
JOB_LINK_PATH_RE = re.compile(r"/(job|jobs|position|positions|vacanc(y|ies)|opening|openings|opportunit(y|ies)|"
                              r"careers?)/[\w%.-]{4,}", re.I)
SEARCH_FIELD_RE = re.compile(r"search|query|keyword|newsletter|subscribe|بحث", re.I)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Lead:
    id: int
    company: str
    score: int
    website: str = ""
    known_url: str = ""          # careers / apply link already on file
    status: str = "reviewed"


@dataclass
class Result:
    lead: Lead
    # queue | offshore | directory | closed | gpa | verification | no_form | email_only | no_match
    # | not_found | unreachable | robots
    outcome: str = "not_found"
    url: str = ""
    kind: str = ""                # internship_posting | ats_posting | ats_board | careers_page
    detail: str = ""
    fetched: list = field(default_factory=list)
    texts: list = field(default_factory=list)    # page text samples, for the offshore check


def host(url: str) -> str:
    h = urllib.parse.urlsplit(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def is_noise(url: str) -> bool:
    u = url.lower()
    return any(n in u for n in NOISE_HOSTS)


def is_ats(url: str) -> bool:
    if not url or is_ats_vendor_page(url):
        return False
    return bool(ats_board(url)) or any(a in url.lower() for a in OTHER_ATS_HOSTS)


def name_tokens(company: str) -> list[str]:
    stop = {"saudi", "arabia", "company", "technologies", "technology", "solutions", "group", "riyadh", "ksa",
            "about", "contact", "the", "and", "for", "systems", "services", "digital", "home", "offices"}
    return [t for t in re.findall(r"[a-z0-9]{3,}", company.lower()) if t not in stop]


# ---------------------------------------------------------------------------
# Polite fetching
# ---------------------------------------------------------------------------

class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self.robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=8)
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                    rp.parse(r.text.splitlines())
                else:
                    rp = None                       # no robots.txt → allowed
            except requests.RequestException:
                rp = None
            self.robots[base] = rp
        rp = self.robots[base]
        return True if rp is None else rp.can_fetch(UA, url)

    def get(self, url: str, res: Result) -> requests.Response | None:
        if len(res.fetched) >= MAX_FETCHES_PER_COMPANY:
            return None
        if not self.allowed(url):
            res.fetched.append(f"robots-disallowed {url}")
            return None
        time.sleep(random.uniform(0.8, 2.2))
        try:
            r = self.s.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            res.fetched.append(f"ERR {type(e).__name__} {url}")
            return None
        res.fetched.append(f"{r.status_code} {url}")
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "text/html").lower():
            return None
        r.encoding = r.encoding or r.apparent_encoding
        return r


def page_text(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def links(soup: BeautifulSoup, base_url: str) -> list[tuple[str, str]]:
    out, seen = [], set()
    for tag in soup.find_all(["a", "iframe"]):
        href = tag.get("href") or tag.get("src") or ""
        if not href or href.startswith(("#", "javascript:", "tel:")):
            continue
        url = urllib.parse.urljoin(base_url, href.strip())
        if not url.startswith(("http", "mailto:")) or url in seen:
            continue
        seen.add(url)
        label = " ".join([tag.get_text(" ", strip=True), tag.get("title") or "", tag.get("aria-label") or ""])
        out.append((url.split("#")[0] if "#/" not in url else url, label[:150]))
    return out


def past_deadline(text: str) -> str | None:
    for m in DEADLINE_RE.finditer(text):
        raw = m.group("d")
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%y",
                    "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y", "%B %d, %Y", "%b %d, %Y"):
            try:
                d = datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
            if d < TODAY:
                return f"deadline {d.isoformat()} has passed"
            break
    return None


def assess(text: str, url: str = "") -> tuple[str, str] | None:
    """Returns (outcome, reason) when the page must be skipped, else None."""
    if SKIP_VERIFICATION_PORTALS and VERIFY_RE.search(text):
        return "verification", "portal requires Nafath / SMS OTP (config: you can't do these right now)"
    m = CLOSED_RE.search(text)
    if m:
        return "closed", f"closed: “{m.group(0)[:60]}”"
    pd = past_deadline(text)
    if pd:
        return "closed", pd
    g = gpa_requirement_reason(text)
    if g:
        return "gpa", f"GPA filter: {g}"
    return None


def apply_signals(soup: BeautifulSoup, page_links: list[tuple[str, str]], final: str) -> dict:
    """What a careers page offers to an applicant."""
    inputs = 0
    for el in soup.find_all(["input", "textarea", "select"]):
        t = (el.get("type") or el.name).lower()
        if t in ("hidden", "submit", "button", "reset", "image", "search"):
            continue
        ident = " ".join(str(el.get(k) or "") for k in ("name", "id", "placeholder", "aria-label"))
        if SEARCH_FIELD_RE.search(ident) or el.find_parent(attrs={"role": "search"}):
            continue
        inputs += 1
    buttons = [b for b in soup.find_all(["button", "a"]) if APPLY_TEXT_RE.search(b.get_text(" ", strip=True)[:80])]
    final_path = urllib.parse.urlsplit(final).path.rstrip("/")
    job_links = [u for u, label in page_links
                 if u.startswith("http") and root_domain(u) == root_domain(final) and JOB_LINK_PATH_RE.search(u)
                 and urllib.parse.urlsplit(u).path.rstrip("/") != final_path]
    emails = [u[7:].split("?")[0] for u, _ in page_links if u.lower().startswith("mailto:")
              and re.search(r"career|job|hr|recruit|talent|cv|hiring|توظيف", u, re.I)]
    return {"inputs": inputs, "apply": len(buttons), "job_links": len(job_links), "emails": emails,
            "ats": next((u for u, _ in page_links if is_ats(u) and not is_noise(u)), None)}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def search_website(f: Fetcher, lead: Lead, res: Result) -> str:
    """One DuckDuckGo HTML lookup. Accepts the company's own site or its job-board page only."""
    q = f"{lead.company} Riyadh careers"
    try:
        time.sleep(random.uniform(2, 4))
        r = f.s.post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=TIMEOUT)
        res.fetched.append(f"{r.status_code} search: {q}")
        if r.status_code != 200:
            return ""
    except requests.RequestException as e:
        res.fetched.append(f"ERR search {type(e).__name__}")
        return ""
    soup = BeautifulSoup(r.text, "html.parser")
    toks = name_tokens(lead.company)
    for a in soup.select("a.result__a"):
        href = a.get("href", "")
        if "uddg=" in href:
            href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("uddg", [""])[0])
        if not href.startswith("http") or is_noise(href) or is_directory(href) or not_careers_reason(href):
            continue
        h = host(href)
        if toks and any(t in h.replace("-", "") or t in href.lower() for t in toks):
            return href if is_ats(href) or CAREER_LINK_RE.search(href) else f"https://{h}"
    return ""


def candidate_careers_urls(f: Fetcher, start: str, res: Result) -> list[str]:
    """Careers-ish URLs found from a website: links on the home page first, then common paths."""
    found: list[tuple[int, str]] = []
    r = f.get(start, res)
    if r is not None:
        soup = BeautifulSoup(r.text, "html.parser")
        site = root_domain(host(r.url))
        home_links = links(soup, r.url)
        res.texts.append(page_text(soup)[:6000])                       # address / offices for the offshore check
        for url, label in home_links:
            if is_noise(url) or url.lower().startswith("mailto:") or not_careers_reason(url):
                continue
            same_site = root_domain(host(url)) == site
            if is_ats(url):
                found.append((0, url))
            elif same_site and (CAREER_LINK_RE.search(label) or CAREER_LINK_RE.search(urllib.parse.urlsplit(url).path)):
                found.append((1, url))
        parts = urllib.parse.urlsplit(r.url)
    else:
        parts = urllib.parse.urlsplit(start)
    base = f"{parts.scheme}://{parts.netloc}"
    if not found:
        found += [(2, base + p) for p in CAREER_PATHS[:5]]
    found.sort(key=lambda x: x[0])
    out = []
    for _, u in found:
        if u not in out and u.rstrip("/") != start.rstrip("/"):
            out.append(u)
    return out[:4]


def resolve_board(board_url: str, res: Result, f: Fetcher) -> bool:
    """Job board → direct posting. Returns True when a decision was reached."""
    r = ats_resolver.find_posting(board_url, session=f.s)
    st = r["status"]
    res.fetched.append(f"board {st}: {board_url}")
    if st == "found":
        res.texts.append(r.get("location", "") + " " + r.get("text", "")[:3000])
        res.outcome, res.url, res.kind = "queue", r["apply_url"], "ats_posting"
        res.detail = f"{r.get('kind', 'posting')}: {r.get('title', '')[:60]} ({r.get('location', '')[:40]})"
        return True
    if st == "is_posting":
        return False                                     # inspect the posting page as a normal page
    if st in ("none_suitable", "empty"):
        res.outcome, res.url, res.detail = "no_match", board_url, r.get("reason", "")
        return True
    return False                                         # unsupported / error → treat the board as a page


def inspect_careers_page(f: Fetcher, url: str, res: Result) -> bool:
    """Reads one careers page. Fills res and returns True when a decision was reached."""
    if not_careers_reason(url):
        return False
    if ats_board(url) and resolve_board(url, res, f):
        return True
    r = f.get(url, res)
    if r is None:
        return False
    raw = r.text
    soup = BeautifulSoup(raw, "html.parser")
    page_links = links(soup, r.url)
    final = r.url
    if not_careers_reason(final):                      # redirected to a product / vendor page
        return False
    sig = apply_signals(soup, page_links, final)
    text = page_text(soup)
    res.texts.append(text[:6000])
    sp = urllib.parse.urlsplit(final)
    path_hit = CAREER_LINK_RE.search(sp.netloc.replace(".", " ") + " " + sp.path.replace("-", " ")) or is_ats(final)
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")
    js_shell = len(text) < 400 and bool(JS_SHELL_RE.search(raw))
    # the page must actually be about jobs: a 200 at /careers that shows the home page is a soft-404
    looks_careers = (is_ats(final) or CAREER_LINK_RE.search(title) or len(CAREER_LINK_RE.findall(text[:4000])) >= 2
                     or (path_hit and js_shell))
    if not looks_careers:
        return False

    # 1) an internship / co-op posting on this page?
    excluded = None
    tried = 0
    for link, label in page_links:
        if (tried >= 3 or is_noise(link) or link.lower().startswith("mailto:") or not_careers_reason(link)
                or link.rstrip("/") == final.rstrip("/")):
            continue
        if INTERN_RE.search(label) or INTERN_RE.search(urllib.parse.urlsplit(link).path.replace("-", " ")):
            tried += 1
            if ats_board(link) and resolve_board(link, res, f) and res.outcome == "queue":
                return True
            pr = f.get(link, res)
            if pr is None:
                continue
            ptext = page_text(BeautifulSoup(pr.text, "html.parser"))
            bad = assess(ptext, pr.url)
            off = offshore_reason(ptext, pr.url) if not bad else None
            if bad or off:
                excluded = bad or ("offshore", off)
                excluded = (excluded[0], f"{excluded[1]} (posting: {label[:60]})", pr.url)
                continue                               # try the next internship link
            res.texts.append(ptext[:4000])
            res.outcome, res.url, res.kind, res.detail = "queue", pr.url, "internship_posting", label[:80]
            return True

    # 2) the page itself: closed / GPA / Nafath?
    bad = assess(text, final)
    if bad:
        res.outcome, res.detail, res.url = bad[0], bad[1], final
        return True
    if excluded:                                       # the co-op posting(s) were excluded: don't fall back
        res.outcome, res.detail, res.url = excluded
        return True

    # 3) a job board linked or embedded from the careers page → resolve it to the posting
    if sig["ats"] and not is_ats(final):
        if ats_board(sig["ats"]) and resolve_board(sig["ats"], res, f):
            return True
        res.outcome, res.url, res.kind = "queue", sig["ats"], "ats_board"
        res.detail = "job board linked from careers page (needs a browser to list postings)"
        return True
    if is_ats(final):
        res.outcome, res.url, res.kind = "queue", final, "ats_board"
        res.detail = "job board (needs a browser to list postings)"
        return True

    # 4) a plain careers page: only if it offers a way to apply
    if js_shell:
        res.outcome, res.url, res.kind = "queue", final, "careers_page"
        res.detail = "JavaScript page: postings not readable without a browser (apply_queue re-checks)"
        return True
    if sig["inputs"] >= 2 or sig["apply"] or sig["job_links"]:
        res.outcome, res.url, res.kind = "queue", final, "careers_page"
        res.detail = (f"careers page ({sig['inputs']} form fields, {sig['apply']} apply buttons, "
                      f"{sig['job_links']} job links)" + (" — mentions internships" if INTERN_RE.search(text) else ""))
        return True
    if sig["emails"]:
        res.outcome, res.url = "email_only", final
        res.detail = f"no form — apply by e-mail: {', '.join(sorted(set(sig['emails']))[:2])}"
        return True
    res.outcome, res.url, res.detail = "no_form", final, "careers page has no form, Apply button or job links"
    return True


def resolve(lead: Lead, use_search: bool) -> Result:
    res = Result(lead)
    f = Fetcher()
    known = lead.known_url if lead.known_url and not is_noise(lead.known_url) else ""
    if known and not_careers_reason(known) and not ats_board(known):
        known = ""
    website = lead.website if lead.website and not is_noise(lead.website) else ""
    if is_directory(website or known, lead.company):
        res.outcome, res.detail = "directory", "directory / listing site, not an employer"
        return res
    pre = offshore_reason("", website or known)                   # known offshore firms
    if pre:
        res.outcome, res.detail = "offshore", pre
        return res

    decided = False
    if known and (is_ats(known) or CAREER_LINK_RE.search(known)):   # a known careers / board link goes first
        decided = inspect_careers_page(f, known, res)
    if not decided:
        start = website or known
        if not start and use_search:
            start = search_website(f, lead, res)
            if start and (is_ats(start) or CAREER_LINK_RE.search(urllib.parse.urlsplit(start).path)):
                decided = inspect_careers_page(f, start, res)
        if not decided and not start:
            res.detail = "no website on file" + (" and none found by search" if use_search else "")
            return res
        if not decided:
            for url in candidate_careers_urls(f, start, res):
                if inspect_careers_page(f, url, res):
                    decided = True
                    break
    if decided and res.outcome == "queue":
        off = offshore_reason(" ".join(res.texts), website or res.url)
        if off:
            res.outcome, res.detail = "offshore", off
    if not decided and res.outcome == "not_found":
        if res.fetched and all(x.startswith("ERR") for x in res.fetched if not x.startswith("robots")):
            res.outcome, res.detail = "unreachable", "site did not respond"
        elif any(x.startswith("robots-disallowed") for x in res.fetched):
            res.outcome, res.detail = "robots", "robots.txt disallows the careers page"
        else:
            res.detail = "no careers page found on the website"
    return res


# ---------------------------------------------------------------------------
# Leads (DB + spreadsheet)
# ---------------------------------------------------------------------------

def _sheet() -> tuple[dict, dict]:
    sheet: dict[int, dict] = {}
    by_fp: dict[str, dict] = {}
    if XLSX.exists():
        try:
            from openpyxl import load_workbook
            ws = load_workbook(XLSX, read_only=True, data_only=True).active
            rows = ws.iter_rows(values_only=True)
            cols = [str(c or "") for c in next(rows)]
            for vals in rows:
                d = dict(zip(cols, vals))
                if not d.get("Company"):
                    continue
                m = re.search(r"#(\d+)", str(d.get("DB note") or ""))
                if m:
                    sheet[int(m.group(1))] = d
                by_fp.setdefault(app_db.company_fp(str(d["Company"])), d)
        except Exception as e:
            print(f"ℹ️ couldn't read {XLSX.name} ({type(e).__name__}); using DB scores only")
    return sheet, by_fp


def _lead(r, sheet, by_fp) -> Lead:
    d = sheet.get(r["id"]) or by_fp.get(app_db.company_fp(r["company"] or ""))
    score = int(d["Fit Score"]) if d and str(d.get("Fit Score") or "").isdigit() else int(r["score"] or 0)
    website = str((d or {}).get("Website") or "") or (r["source_url"] or "")
    known = str((d or {}).get("Apply / careers link") or "")
    if "google.com/search" in known:
        known = ""
    known = (r["apply_url"] or "") if r["status"] == "queued" else (known or r["apply_url"] or "")
    if known.startswith("mailto:"):
        known = ""
    if website and (is_ats(website) or CAREER_LINK_RE.search(urllib.parse.urlsplit(website).path)):
        website = f"https://{host(website)}" if not is_ats(website) else ""
    if not website and known and not is_noise(known) and not is_ats(known):
        p = urllib.parse.urlsplit(known)
        website = f"{p.scheme}://{p.netloc}"
    return Lead(r["id"], r["company"] or "", score, website.strip(), known.strip(), r["status"])


def load_leads(top: int) -> list[Lead]:
    sheet, by_fp = _sheet()
    with app_db.connect() as con:
        leads = [_lead(r, sheet, by_fp) for r in con.execute("SELECT * FROM opportunities WHERE status='reviewed'")]
    leads.sort(key=lambda l: (-l.score, l.id))
    return leads[:top]


def load_queued_for_recheck() -> list[Lead]:
    """Rows this script queued before, plus any queued job-board root (e.g. apply.workable.com/mozn-ai/)."""
    sheet, by_fp = _sheet()
    out = []
    with app_db.connect() as con:
        for r in con.execute("SELECT * FROM opportunities WHERE status='queued' AND apply_type='external'"):
            url = r["apply_url"] or ""
            board = ats_board(url)
            board_root = (bool(board) and board[0] in ats_resolver.RESOLVABLE
                          and not ats_resolver._is_posting_url(board[0], url))
            if "careers resolved" in (r["notes"] or "") or board_root or not_careers_reason(url):
                out.append(_lead(r, sheet, by_fp))
    out.sort(key=lambda l: (-l.score, l.id))
    return out


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------

def _set_url(con, row, url: str) -> str | None:
    new_key = app_db.canonical_key({**dict(row), "apply_url": url, "apply_type": "external"})
    other = con.execute("SELECT id, company FROM opportunities WHERE id!=? AND (canonical_key=? OR apply_url=?)",
                        (row["id"], new_key, url)).fetchone()
    if other:
        return f"duplicate: URL already used by #{other['id']} ({other['company']})"
    con.execute("UPDATE opportunities SET apply_url=?, apply_type='external', canonical_key=?, last_seen=? WHERE id=?",
                (url, new_key, app_db.now(), row["id"]))
    return None


def _demote(con, row, status: str, note: str) -> None:
    """Back to reviewed / rejected; the old apply URL is replaced by the website so no form is ever opened."""
    if row["status"] == "queued":
        site = row["source_url"] or ""
        if site and not (is_ats(site) or not_careers_reason(site)):
            p = urllib.parse.urlsplit(site)
            site = f"{p.scheme}://{p.netloc}"
        key = app_db.canonical_key({**dict(row), "apply_url": site, "apply_type": "careers_page"})
        if not con.execute("SELECT 1 FROM opportunities WHERE id!=? AND canonical_key=?", (row["id"], key)).fetchone():
            con.execute("UPDATE opportunities SET apply_url=?, apply_type='careers_page', canonical_key=? WHERE id=?",
                        (site, key, row["id"]))
    app_db.set_status(con, row["id"], status, note[:300])


def apply_outcome(con, res: Result) -> str:
    row = con.execute("SELECT * FROM opportunities WHERE id=?", (res.lead.id,)).fetchone()
    if row is None:
        return "skipped: row missing"
    if row["status"] not in ("reviewed", "queued") or row["status"] != res.lead.status:
        return f"skipped: row is now '{row['status']}'"
    recheck = row["status"] == "queued"
    o = res.outcome
    if o == "queue":
        if recheck and (row["apply_url"] or "") == res.url:
            return "still queued (verified)"
        dup = _set_url(con, row, res.url)
        if dup:
            return dup
        if recheck:
            con.execute("UPDATE opportunities SET notes=TRIM(notes || ' ' || ?) WHERE id=?",
                        (f"apply URL updated ({res.kind})", row["id"]))
            return "queued (URL updated)"
        app_db.set_status(con, row["id"], "queued", f"careers resolved ({res.kind})")
        return "queued"
    if o in ("offshore", "directory"):
        _demote(con, row, "rejected", f"{o}: {res.detail}")
        return "rejected"
    if o == "gpa":
        _demote(con, row, "rejected", res.detail)
        return "rejected"
    if recheck and o in ("closed", "no_form", "email_only", "no_match", "not_found"):
        _demote(con, row, "reviewed", f"recheck: {o} — {res.detail}")
        return "back to reviewed"
    if recheck and o == "verification":
        _demote(con, row, "manual_required", res.detail)
        return "manual_required"
    return ""                                          # unreachable / robots / new-lead skips: no change


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-search", action="store_true")
    ap.add_argument("--recheck-queued", action="store_true",
                    help="re-verify rows already queued by this script instead of resolving new ones")
    ap.add_argument("--no-cleanup", action="store_true", help="don't run cleanup_db.py first")
    args = ap.parse_args()

    if not args.no_cleanup:
        cleanup_db.cleanup(dry_run=args.dry_run, verbose=False)

    leads = load_queued_for_recheck() if args.recheck_queued else load_leads(args.top)
    if not leads:
        print("Nothing to resolve.")
        return
    what = "queued rows to re-verify" if args.recheck_queued else f"top {len(leads)} reviewed companies"
    print(f"Resolving careers pages for {len(leads)} {what} ({args.workers} at a time, polite delays)…\n")

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(resolve, l, not args.no_search): l for l in leads}
        for i, fut in enumerate(as_completed(futs), 1):
            lead = futs[fut]
            try:
                res = fut.result()
            except Exception as e:                      # one bad site never stops the run
                res = Result(lead, "unreachable", detail=f"{type(e).__name__}: {str(e)[:80]}")
            results.append(res)
            mark = {"queue": "✅", "closed": "⛔", "gpa": "⛔", "offshore": "🌍", "directory": "🗂",
                    "verification": "🔒", "no_form": "∅", "no_match": "∅", "email_only": "✉"}.get(res.outcome, "·")
            print(f"[{i:>2}/{len(leads)}] {mark} #{lead.id:<4} {lead.company[:38]:<38} {res.outcome:<12} "
                  f"{(res.url or res.detail)[:70]}")

    results.sort(key=lambda r: (-r.lead.score, r.lead.id))
    db_notes = {}
    if not args.dry_run:
        with app_db.connect() as con:
            for res in results:
                note = apply_outcome(con, res)
                if note:
                    db_notes[res.lead.id] = note
    REPORT_DIR.mkdir(exist_ok=True)
    tag = "recheck" if args.recheck_queued else "resolution"
    report = REPORT_DIR / f"careers_{tag}_{datetime.now():%Y%m%d_%H%M}.csv"
    with open(report, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "company", "fit_score", "outcome", "kind", "url", "detail", "db", "requests"])
        for res in results:
            db = db_notes.get(res.lead.id, "dry run" if args.dry_run else "")
            w.writerow([res.lead.id, res.lead.company, res.lead.score, res.outcome, res.kind, res.url,
                        res.detail, db, " | ".join(res.fetched)[:900]])

    counts: dict[str, int] = {}
    for res in results:
        counts[res.outcome] = counts.get(res.outcome, 0) + 1
    changes: dict[str, int] = {}
    for v in db_notes.values():
        k = v.split(":")[0]
        changes[k] = changes.get(k, 0) + 1
    with app_db.connect() as con:
        total = con.execute("SELECT COUNT(*) FROM opportunities WHERE status='queued'").fetchone()[0]
    print("\nOutcomes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    if changes:
        print("DB changes: " + ", ".join(f"{k} {v}" for k, v in sorted(changes.items(), key=lambda x: -x[1])))
    print(f"Report: {report}")
    if args.dry_run:
        print(f"\nDRY RUN — nothing written. Would queue {counts.get('queue', 0)}. Total queued in DB now: {total}")
    else:
        print(f"\nNewly queued: {changes.get('queued', 0)}")
        print(f"TOTAL QUEUED: {total}")


if __name__ == "__main__":
    main()
