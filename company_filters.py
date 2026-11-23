"""
company_filters.py — shared rules that keep the pipeline to real, Saudi-based employers.

Used by harvest_400_riyadh_companies.py, resolve_careers_and_queue.py, apply_queue.py and cleanup_db.py.

  • offshore_reason(text, url)   → why a company / page is offshore-only (India, Pakistan, UAE …) with no
                                   Saudi office or role mentioned anywhere, else None
  • is_directory(url, name)      → directory / listing / map sites that are not employers
  • not_careers_reason(url)      → service, product or recruitment-agency pages that look like careers links
  • ats_board(url)               → (vendor, slug) for a company-specific job-board URL, None for vendor
                                   marketing pages such as manatal.com/?utm_source=careers-page.com
  • clean_company_name(name, url)→ "About GoAgile" → "GoAgile", "Big Data and Analytics at CompTech" → "CompTech"
"""

import re
import urllib.parse

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

SAUDI_RE = re.compile(
    r"\b(riyadh|saudi|ksa|kingdom of saudi arabia|jeddah|jiddah|dammam|khobar|al[\s-]?khobar|dhahran|mecca|makkah|"
    r"medina|madinah|tabuk|abha|neom|jubail|yanbu|qassim|buraidah|taif|hail|kafd|diriyah)\b|"
    r"الرياض|السعودية|المملكة العربية|جدة|الدمام|الخبر|الظهران|مكة|المدينة المنورة|نيوم", re.I)

# Places that, when they are the ONLY locations a company or posting mentions, mean "not in Saudi Arabia"
OFFSHORE_RE = re.compile(
    r"\b(india|indian|pune|ahmedabad|bangalore|bengaluru|hyderabad|chennai|mumbai|navi mumbai|noida|gurgaon|"
    r"gurugram|kolkata|new delhi|delhi ncr|delhi|kochi|cochin|trivandrum|thiruvananthapuram|coimbatore|jaipur|"
    r"indore|chandigarh|nagpur|vadodara|surat|mohali|lucknow|bhubaneswar|"
    r"pakistan|karachi|lahore|islamabad|rawalpindi|bangladesh|dhaka|sri lanka|colombo|nepal|kathmandu|"
    r"philippines|manila|cebu|vietnam|ho chi minh|hanoi|"
    r"egypt|cairo|alexandria|jordan|amman|lebanon|beirut|"
    r"united arab emirates|uae|dubai|abu dhabi|sharjah|qatar|doha|bahrain|manama|kuwait|oman|muscat)\b|"
    r"الهند|باكستان|مصر|القاهرة|الأردن|عمّان|دبي|أبوظبي|الإمارات|قطر|الدوحة|البحرين|الكويت|عُمان", re.I)
OFFSHORE_TLDS = (".in", ".co.in", ".pk", ".com.pk", ".bd", ".com.bd", ".lk", ".np", ".ph", ".vn", ".eg", ".com.eg",
                 ".jo", ".ae", ".qa", ".bh", ".kw", ".om")
# Well-known offshore firms that slipped through earlier harvests (no Saudi office or role found)
KNOWN_OFFSHORE = {"clariontech.com": "Clarion Technologies is India-based (Pune / Ahmedabad); no Saudi office or role"}


def host_of(url: str) -> str:
    h = urllib.parse.urlsplit(url if "://" in (url or "") else f"https://{url}").netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def root_domain(url_or_host: str) -> str:
    h = host_of(url_or_host) if "/" in url_or_host or "." in url_or_host else url_or_host
    parts = h.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "net", "org", "gov", "edu", "co") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def offshore_reason(text: str, url: str = "") -> str | None:
    """Reason when the text/URL points only to non-Saudi locations; None when Saudi is mentioned or nothing is known."""
    dom = root_domain(url) if url else ""
    if dom in KNOWN_OFFSHORE:
        return KNOWN_OFFSHORE[dom]
    t = text or ""
    if SAUDI_RE.search(t) or dom.endswith(".sa"):
        return None
    places = sorted({m.group(0).strip().title() for m in OFFSHORE_RE.finditer(t)})
    if places:
        return "offshore only (" + ", ".join(places[:4]) + "); no Saudi office or role mentioned"
    if dom and dom.endswith(OFFSHORE_TLDS) and t.strip():
        return f"offshore domain ({dom}); no Saudi office or role mentioned"
    return None


# ---------------------------------------------------------------------------
# Directories, listings and non-careers pages
# ---------------------------------------------------------------------------

DIRECTORY_HOSTS = (
    "startupblink.com", "datacentermap.com", "clutch.co", "goodfirms.co", "crunchbase.com", "tracxn.com",
    "magnitt.com", "f6s.com", "dealroom.co", "cbinsights.com", "pitchbook.com", "ksa.directory", "yellowpages",
    "zoominfo.com", "rocketreach.co", "signalhire.com", "dnb.com", "opencorporates.com", "builtwith.com",
    "glassdoor.", "indeed.", "bayt.com", "wuzzuf.net", "gulftalent.com", "naukrigulf.com", "themanifest.com",
    "designrush.com", "sortlist.com", "topdevelopers.co", "techbehemoths.com", "ensun.io", "seedtable.com",
    "failory.com", "wellfound.com", "angel.co", "producthunt.com", "g2.com", "capterra.com", "datacenters.com",
    "cloudscene.com", "inven.ai", "owler.com", "craft.co", "apollo.io", "lusha.com",
)
DIRECTORY_NAME_RE = re.compile(
    r"\b(startupblink|data ?center ?map|directory|top \d+|best \d+|list of|companies in|companies list|"
    r"startup ecosystem|ecosystem map|company list|rankings?)\b", re.I)

CAREER_WORD_RE = re.compile(r"career|jobs?\b|join|vacanc|hiring|opening|position|work-with-us|work_with_us|"
                            r"وظائف|التوظيف|وظيفة|انضم", re.I)
NON_CAREERS_PATH_RE = re.compile(
    r"(?:/|-)(solutions?|services?|erp-services|products?|platform|blog|news|insights|events?|case-stud(y|ies)|"
    r"developer-network|partners?|resources|webinars?|pricing|features?|industries|"
    r"permanent-recruitment|recruitment-services|staffing|executive-search|hr-services|outsourcing|"
    r"zoho-recruit|recruit-software|ats-software)(/|$|-)", re.I)

# Job-board vendors: their marketing sites are never an employer's board
ATS_VENDOR_HOMES = ("manatal.com", "workable.com", "greenhouse.io", "lever.co", "smartrecruiters.com",
                    "ashbyhq.com", "bamboohr.com", "teamtailor.com", "recruitee.com", "personio.com", "personio.de",
                    "zoho.com", "breezy.hr", "jobvite.com", "icims.com", "freshteam.com", "jisr.net", "pinpointhq.com")


def is_directory(url: str = "", name: str = "") -> bool:
    u = (url or "").lower()
    return any(h in u for h in DIRECTORY_HOSTS) or bool(DIRECTORY_NAME_RE.search(name or ""))


def ats_board(url: str) -> tuple[str, str] | None:
    """(vendor, company slug) for a company-specific board/posting URL, else None."""
    if not url:
        return None
    p = urllib.parse.urlsplit(url)
    host = p.netloc.lower().split(":")[0]
    parts = [x for x in p.path.split("/") if x]
    sub = host.split(".")[0]
    if host == "apply.workable.com" and parts and parts[0] not in ("api", "j"):
        return "workable", parts[0]
    if host.endswith(".workable.com") and sub not in ("www", "apply", "help", "resources", "jobs"):
        return "workable", sub
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io") and parts:
        return "greenhouse", parts[0]
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts:
        return "lever", parts[0]
    if host == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        return "smartrecruiters", parts[0]
    if host.endswith(".zohorecruit.com") or host.endswith(".zohorecruit.eu") or host.endswith(".zohorecruit.in"):
        return "zoho", sub
    if host.endswith("careers-page.com") and parts:
        return "manatal", parts[0]
    if host.endswith(".bamboohr.com") and sub not in ("www",):
        return "bamboohr", sub
    if host.endswith(".teamtailor.com") and sub not in ("www", "app"):
        return "teamtailor", sub
    if host.endswith(".recruitee.com") and sub not in ("www", "app"):
        return "recruitee", sub
    if host.endswith(".breezy.hr") and sub not in ("www", "app"):
        return "breezy", sub
    if "myworkdayjobs.com" in host:
        return "workday", sub
    if host.endswith("jisr.net") and sub not in ("www", "app") and parts:
        return "jisr", sub
    return None


def is_ats_vendor_page(url: str) -> bool:
    """True for a job-board vendor's own marketing site (e.g. manatal.com/?utm_source=careers-page.com)."""
    if ats_board(url):
        return False
    return root_domain(url) in ATS_VENDOR_HOMES


def not_careers_reason(url: str) -> str | None:
    """Reason when a URL is a service/product/agency page rather than an employer's careers page."""
    if not url or url.startswith("mailto:"):
        return None
    if is_ats_vendor_page(url):
        return "job-board vendor's marketing site, not the company's board"
    if is_directory(url):
        return "directory / listing site"
    path = urllib.parse.urlsplit(url).path
    if NON_CAREERS_PATH_RE.search(path) and not CAREER_WORD_RE.search(path):
        return "service / product page, not a careers page"
    return None


# ---------------------------------------------------------------------------
# Company names
# ---------------------------------------------------------------------------

_GENERIC_WORDS = {"ai", "solutions", "solution", "automation", "cyber", "security", "cybersecurity", "experts",
                  "expert", "government", "energy", "big", "data", "analytics", "and", "software", "development",
                  "company", "services", "technology", "technologies", "it", "digital", "consulting", "the", "of",
                  "in", "for", "riyadh", "saudi", "arabia", "ksa", "home", "about", "contact", "us", "offices",
                  "office", "tech", "hub", "group", "systems", "network", "networks", "cloud", "machine", "learning"}


def _from_domain(url: str) -> str:
    stem = root_domain(url).split(".")[0]
    if not stem:
        return ""
    words = re.split(r"[-_]+", stem)
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in words if w)


def clean_company_name(name: str, url: str = "") -> str:
    """Strips page-title noise from a harvested company name. Falls back to the domain for generic labels."""
    n = re.sub(r"\s+", " ", name or "").strip()
    n = re.sub(r"\s*(\.\.\.|…)\s*$", "", n)
    n = re.sub(r"^(about|contact|company|welcome to|meet)\s*(us)?\s*[·|:\-–—]?\s+", "", n, flags=re.I)
    n = re.sub(r"\s*[:|·–—\-]\s*(home|homepage|home page|official site|official website)$", "", n, flags=re.I)
    n = re.sub(r"\s*[:|·–—]\s*.*\b(company|solutions|services|agency|firm|provider|development)\b.*\bin\b.*$", "",
               n, flags=re.I)
    n = re.sub(r"\s*[:|,·–—]\s*(riyadh|saudi arabia|ksa)(\s*,\s*(riyadh|saudi arabia|ksa))*$", "", n, flags=re.I)
    n = re.sub(r"\s+(offices?|riyadh tech hub|tech hub)$", "", n, flags=re.I)
    if " · " in n or " | " in n:                                  # "Cignify · Saudi Cybersecurity"
        segs = [x.strip() for x in re.split(r"\s[·|]\s", n) if x.strip()]
        stem = root_domain(url).split(".")[0].replace("-", "") if url else ""
        n = next((x for x in segs if stem and stem[:5] in re.sub(r"[^a-z0-9]", "", x.lower())), segs[0])
    m = re.match(r"^.+?\s+at\s+([A-Z][\w&.\- ]{1,40})$", n)     # "Big Data and Analytics at CompTech"
    if m:
        n = m.group(1)
    n = n.strip(" -|–—:·•,")
    tokens = re.findall(r"[a-z0-9]+", n.lower())
    if url and (not tokens or all(t in _GENERIC_WORDS for t in tokens)):
        return _from_domain(url) or n
    return n or (_from_domain(url) if url else "")
