"""
ats_resolver.py — turns a company's job-board URL into the direct posting / apply URL.

Supported boards (public JSON / HTML, plain `requests`, no browser):
    Workable, Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Breezy, BambooHR, Zoho Recruit, Manatal

    find_posting("https://apply.workable.com/mozn-ai/")
      → {"status": "found", "url": ".../j/ABC123/", "apply_url": ".../j/ABC123/apply", "title": "AI Intern", ...}

Status values:
    found          a co-op / internship (preferred) or entry-level posting in Saudi Arabia that passed the checks
    is_posting     the URL already points at one posting — keep it
    none_suitable  the board has openings, but none are internship / entry-level roles in Saudi Arabia
                   (or every one is closed / above your GPA)
    empty          the board has no openings at all
    unsupported    not a board this module can read (Workday, SuccessFactors, Taleo …) — keep the URL
    error          the board could not be read right now — keep the URL
"""

import html
import json
import random
import re
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

from company_filters import OFFSHORE_RE, SAUDI_RE, ats_board

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0 Safari/537.36")
TIMEOUT = 12

INTERN_TITLE_RE = re.compile(
    r"\b(intern(ship)?s?|co-?op|coop|cooperative|trainee(ship)?|summer\s+training|students?)\b|"
    r"تدريب\s*تعاوني|التدريب\s*التعاوني|متدرب|تدريب", re.I)
ENTRY_TITLE_RE = re.compile(r"\b(graduate|new\s+grad|fresh(\s+grad(uate)?)?|entry[\s-]*level|junior|jr\.?|"
                            r"early\s+careers?|associate\s+(data|ml|ai|software|machine))\b|حديثي\s*التخرج", re.I)
SENIOR_TITLE_RE = re.compile(r"\b(senior|sr\.?|lead|principal|head|director|manager|staff|architect|vp|chief)\b", re.I)
CLOSED_TEXT_RE = re.compile(
    r"no\s+longer\s+(accepting\s+applications|available|open)|position\s+has\s+been\s+filled|"
    r"(applications?|this\s+(job|position|posting))\s+(is|are|has\s+been)\s+(now\s+)?closed|job\s+(has\s+)?expired|"
    r"انتهى\s*(التقديم|التسجيل)|تم\s*إغلاق\s*(التقديم|الوظيفة)", re.I)


def _session(s=None):
    if s is not None:
        return s
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ar;q=0.8"})
    return s


def _get_json(s, url, **kw):
    time.sleep(random.uniform(0.5, 1.2))
    r = s.get(url, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r.json()


def _get_html(s, url):
    time.sleep(random.uniform(0.5, 1.2))
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.encoding or r.apparent_encoding
    return r.text, r.url


def _plain(markup: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html.unescape(markup or ""), "html.parser").get_text(" ", strip=True))


def _is_posting_url(vendor: str, url: str) -> bool:
    parts = [x for x in urllib.parse.urlsplit(url).path.split("/") if x]
    return {
        "workable": len(parts) >= 3 and parts[1] == "j",
        "greenhouse": "jobs" in parts[1:],
        "lever": len(parts) >= 2,
        "ashby": len(parts) >= 2,
        "smartrecruiters": len(parts) >= 2,
        "manatal": "job" in parts[1:],
        "recruitee": "o" in parts,
        "breezy": "p" in parts,
        "bamboohr": len(parts) >= 2 and parts[0] == "careers" and parts[1].isdigit(),
        "zoho": bool(re.search(r"/\d{6,}", url)),
    }.get(vendor, False)


# ---------------------------------------------------------------------------
# Board readers → list of {"title", "location", "url", "apply_url", "text"}
# ---------------------------------------------------------------------------

def _workable(s, slug):
    jobs = []
    try:
        data = _get_json(s, f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
        for j in data.get("jobs", []):
            code = j.get("shortcode") or ""
            loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x)
            for l in j.get("locations") or []:
                loc += " " + " ".join(str(v) for v in l.values() if isinstance(v, str))
            url = f"https://apply.workable.com/{slug}/j/{code}/" if code else j.get("url", "")
            jobs.append({"title": j.get("title", ""), "location": loc, "url": url,
                         "apply_url": (url + "apply") if code else j.get("application_url") or url,
                         "text": _plain(j.get("description", "")) + " " + _plain(j.get("requirements", ""))})
        return jobs
    except Exception:
        pass
    data = s.post(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", timeout=TIMEOUT,
                  json={"query": "", "location": [], "department": [], "worktype": [], "remote": []}).json()
    for j in data.get("results", []):
        code = j.get("shortcode", "")
        loc = j.get("location") or {}
        locs = " ".join(str(v) for v in loc.values() if isinstance(v, str)) if isinstance(loc, dict) else str(loc)
        url = f"https://apply.workable.com/{slug}/j/{code}/"
        jobs.append({"title": j.get("title", ""), "location": locs, "url": url, "apply_url": url + "apply", "text": ""})
    return jobs


def _greenhouse(s, token):
    data = _get_json(s, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
    return [{"title": j.get("title", ""), "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("absolute_url", ""), "apply_url": (j.get("absolute_url", "") + "#app") if j.get("absolute_url") else "",
             "text": _plain(j.get("content", ""))} for j in data.get("jobs", [])]


def _lever(s, slug):
    try:
        data = _get_json(s, f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    except Exception:
        data = _get_json(s, f"https://api.eu.lever.co/v0/postings/{slug}", params={"mode": "json"})
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        loc = " ".join([cat.get("location") or ""] + list(cat.get("allLocations") or []))
        out.append({"title": j.get("text", ""), "location": loc, "url": j.get("hostedUrl", ""),
                    "apply_url": j.get("applyUrl") or j.get("hostedUrl", ""),
                    "text": (j.get("descriptionPlain") or "") + " " + (j.get("additionalPlain") or "") + " " +
                            " ".join(_plain(x.get("content", "")) for x in j.get("lists") or [])})
    return out


def _ashby(s, org):
    data = _get_json(s, f"https://api.ashbyhq.com/posting-api/job-board/{org}")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = " ".join([j.get("location") or ""] + [x.get("location", "") for x in j.get("secondaryLocations") or []])
        out.append({"title": j.get("title", ""), "location": loc, "url": j.get("jobUrl", ""),
                    "apply_url": j.get("applyUrl") or j.get("jobUrl", ""), "text": j.get("descriptionPlain") or ""})
    return out


def _smartrecruiters(s, company):
    data = _get_json(s, f"https://api.smartrecruiters.com/v1/companies/{company}/postings", params={"limit": 100})
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        url = f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}"
        level = (j.get("experienceLevel") or {}).get("label", "")
        out.append({"title": j.get("name", "") + (f" ({level})" if level else ""),
                    "location": " ".join(str(loc.get(k) or "") for k in ("city", "region", "country")),
                    "url": url, "apply_url": url, "text": ""})
    return out


def _recruitee(s, sub):
    data = _get_json(s, f"https://{sub}.recruitee.com/api/offers/")
    return [{"title": j.get("title", ""), "location": " ".join(str(j.get(k) or "") for k in ("location", "city", "country")),
             "url": j.get("careers_url", ""), "apply_url": j.get("careers_apply_url") or j.get("careers_url", ""),
             "text": _plain(j.get("description", "")) + " " + _plain(j.get("requirements", ""))}
            for j in data.get("offers", [])]


def _breezy(s, sub):
    data = _get_json(s, f"https://{sub}.breezy.hr/json")
    return [{"title": j.get("name", ""), "location": (j.get("location") or {}).get("name", ""),
             "url": j.get("url", ""), "apply_url": j.get("url", ""), "text": ""} for j in data or []]


def _bamboohr(s, sub):
    data = _get_json(s, f"https://{sub}.bamboohr.com/careers/list")
    out = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        url = f"https://{sub}.bamboohr.com/careers/{j.get('id')}"
        out.append({"title": j.get("jobOpeningName", ""), "location": " ".join(str(v or "") for v in loc.values()),
                    "url": url, "apply_url": url, "text": ""})
    return out


def _html_board(s, board_url, link_re):
    """Zoho Recruit / Manatal: posting links in the board's HTML."""
    raw, final = _get_html(s, board_url)
    soup = BeautifulSoup(raw, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(final, a["href"])
        if not link_re.search(url) or url in seen:
            continue
        seen.add(url)
        box = a.find_parent(["li", "tr", "article", "div"]) or a
        out.append({"title": a.get_text(" ", strip=True)[:150], "location": box.get_text(" ", strip=True)[:300],
                    "url": url, "apply_url": url, "text": ""})
    # Zoho also embeds the openings as JSON
    for m in re.finditer(r'"Posting_Title"\s*:\s*"([^"]+)".{0,800}?"id"\s*:\s*"(\d{6,})"', raw, re.S):
        url = urllib.parse.urljoin(final, f"/jobs/Careers/{m.group(2)}")
        if url not in seen:
            seen.add(url)
            out.append({"title": m.group(1), "location": "", "url": url, "apply_url": url, "text": ""})
    return out


READERS = {
    "workable": _workable, "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
    "smartrecruiters": _smartrecruiters, "recruitee": _recruitee, "breezy": _breezy, "bamboohr": _bamboohr,
}


RESOLVABLE = set(READERS) | {"zoho", "manatal"}

# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _text_problem(text: str) -> str | None:
    if not text:
        return None
    from apply_queue import gpa_requirement_reason               # same rule everywhere
    m = CLOSED_TEXT_RE.search(text)
    if m:
        return f"closed: “{m.group(0)[:50]}”"
    g = gpa_requirement_reason(text)
    return f"GPA filter: {g}" if g else None


def _rank(job: dict) -> int:
    t = job["title"]
    if SENIOR_TITLE_RE.search(t):                                  # "Internship Program Manager" is not for you
        return 0
    if INTERN_TITLE_RE.search(t):
        return 3
    if ENTRY_TITLE_RE.search(t):
        return 2
    return 0


def _in_saudi(job: dict) -> int:
    """2 = Saudi location, 1 = unknown / remote, 0 = offshore only."""
    loc = job.get("location") or ""
    if SAUDI_RE.search(loc):
        return 2
    if OFFSHORE_RE.search(loc):
        return 0
    if not loc.strip() and job.get("text") and SAUDI_RE.search(job["text"]):
        return 2
    return 1


def find_posting(board_url: str, session=None, max_checks: int = 4) -> dict:
    board = ats_board(board_url)
    if not board:
        return {"status": "unsupported", "reason": "not a recognised job board"}
    vendor, slug = board
    if _is_posting_url(vendor, board_url):
        return {"status": "is_posting", "url": board_url, "apply_url": board_url}
    s = _session(session)
    try:
        if vendor in READERS:
            jobs = READERS[vendor](s, slug)
        elif vendor == "zoho":
            host = urllib.parse.urlsplit(board_url).netloc
            jobs = _html_board(s, f"https://{host}/jobs/Careers", re.compile(r"/jobs/Careers/\d{6,}", re.I))
        elif vendor == "manatal":
            jobs = _html_board(s, f"https://www.careers-page.com/{slug}", re.compile(rf"/{re.escape(slug)}/job/", re.I))
        else:
            return {"status": "unsupported", "reason": f"{vendor} boards are not readable without a browser"}
    except Exception as e:
        return {"status": "error", "reason": f"{vendor} board unreadable ({type(e).__name__})"}

    if not jobs:
        return {"status": "empty", "reason": f"{vendor} board has no openings", "total": 0}
    candidates = [j for j in jobs if _rank(j) and _in_saudi(j)]
    candidates.sort(key=lambda j: (-_rank(j), -_in_saudi(j)))
    skipped = []
    for j in candidates[:max_checks]:
        text = j.get("text") or ""
        if not text and j["url"].startswith("http"):              # read the posting page itself
            try:
                raw, _ = _get_html(s, j["url"])
                soup = BeautifulSoup(raw, "html.parser")
                text = soup.get_text(" ", strip=True)
                if vendor == "manatal":                            # Manatal: the posting's own Apply link
                    a = next((a for a in soup.find_all("a", href=True) if "/apply" in a["href"]), None)
                    if a:
                        j["apply_url"] = urllib.parse.urljoin(j["url"], a["href"])
            except Exception:
                text = ""
        if _in_saudi({**j, "text": text}) == 0 or (not SAUDI_RE.search(j["location"] + " " + text)
                                                    and OFFSHORE_RE.search(text)):
            skipped.append(f"{j['title'][:40]}: offshore")
            continue
        problem = _text_problem(text)
        if problem:
            skipped.append(f"{j['title'][:40]}: {problem}")
            continue
        return {"status": "found", "url": j["url"], "apply_url": j["apply_url"] or j["url"], "title": j["title"],
                "location": j["location"][:120], "kind": "internship" if _rank(j) == 3 else "entry_level",
                "total": len(jobs), "text": text[:8000]}
    reason = (f"{len(jobs)} opening(s), none internship / entry-level in Saudi Arabia" if not candidates
              else "matching postings excluded: " + "; ".join(skipped)[:250])
    return {"status": "none_suitable", "reason": reason, "total": len(jobs)}
