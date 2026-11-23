"""
harvest_400_riyadh_companies.py
Collects real AI / Data Science / Cybersecurity / FinTech / Cloud / Tech-consulting companies and
COOP / internship openings in Riyadh (North Riyadh first), scores each one for your profile, writes the
qualifying ones into applications.db, and exports a ranked Excel sheet.

Sources (no browser automation, no page scraping — search results and files only):
  1. Live web search through the `ddgs` package (falls back to the older `duckduckgo_search` package):
     sector × district queries, co-op/internship queries, ATS job boards (Lever, Greenhouse, Workable,
     SmartRecruiters, Workday, Oracle, SuccessFactors, Jisr …) and company directories
     (LinkedIn company pages, Crunchbase, Tracxn, MAGNiTT, F6S, Wamda, Fintech Saudi).
  2. data/saudi_ai_companies.xlsx — your list of companies that hire for AI (Riyadh rows only).
  3. A curated list of well-known Riyadh technology employers (CURATED below).

Every result must pass all filters before it is kept: located in Riyadh, in a target sector,
a clean company name, and not a news / social / generic page. Nothing is invented: rows keep the
link they came from.

Rules applied (from config.py and apply_queue.py):
  • GPA: any posting that requires more than your GPA (from config.py) or a "Very Good"/"Excellent"
    grade is stored as `rejected` with the reason (apply_queue.gpa_requirement_reason).
  • Offshore-only firms (India, Pakistan … with no Saudi office/role), directory / listing sites and
    service pages are dropped (company_filters.py); page-title noise is stripped from names.
  • Duplicates: matched against applications.db by link, company fingerprint and app_db.upsert's own
    canonical-key / fingerprint merge. Existing rows are never downgraded or overwritten.
  • status: `queued` only for a co-op/internship posting with a direct application form (ATS link);
    `reviewed` for company leads and postings without a direct form (LinkedIn, job boards).

Usage (Windows, from the coop_agent folder):
    pip install ddgs openpyxl
    python harvest_400_riyadh_companies.py               # live search + files, writes DB + Excel
    python harvest_400_riyadh_companies.py --dry-run     # everything except the database writes
    python harvest_400_riyadh_companies.py --offline     # files + curated list only (no internet)
    python harvest_400_riyadh_companies.py --max-queries 40
"""

import argparse
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

import app_db                                    # noqa: E402  (after sys.path)
from apply_queue import gpa_requirement_reason   # noqa: E402
from company_filters import (clean_company_name, is_directory, not_careers_reason,  # noqa: E402
                             offshore_reason)

OUT_XLSX = HERE / "Riyadh_400_AI_Companies_Ranked.xlsx"
SEED_XLSX = HERE / "data" / "saudi_ai_companies.xlsx"
TODAY = date.today().isoformat()

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

NORTH_AREAS = {
    "KAFD": ["kafd", "king abdullah financial district", "المركز المالي", "مركز الملك عبدالله المالي"],
    "Riyadh Front": ["riyadh front", "واجهة الرياض"],
    "Al Yasmin": ["al yasmin", "al yasmeen", "alyasmin", "الياسمين"],
    "Al Sahafah": ["al sahafah", "al sahafa", "as sahafah", "الصحافة"],
    "Al Malqa": ["al malqa", "almalqa", "al malqa", "الملقا"],
    "Al Aqiq": ["al aqiq", "al aqeeq", "alaqiq", "العقيق"],
    "Al Nakheel": ["al nakheel", "an nakheel", "النخيل", "digital city", "المدينة الرقمية"],
    "Al Wadi": ["al wadi", "حي الوادي"],
    "Al Narjis": ["al narjis", "an narjis", "النرجس"],
    "Al Nada": ["al nada", "an nada", "الندى"],
    "Hittin": ["hittin", "hitteen", "حطين"],
    "Al Raid / Olaya North": ["al raed", "al raid", "الرائد"],
    "Airport Rd": ["airport road", "طريق المطار"],
}
RIYADH_WORDS = ["riyadh", "الرياض"]
OTHER_CITIES = ["jeddah", "جدة", "dammam", "الدمام", "khobar", "الخبر", "dhahran", "الظهران", "dubai", "دبي",
                "abu dhabi", "cairo", "القاهرة", "amman", "doha", "الدوحة", "kuwait", "bahrain", "tabuk", "neom"]

SECTORS = {   # name: (points, keywords)
    "AI / Machine Learning": (30, ["artificial intelligence", " ai ", "ai-powered", "ai company", "machine learning",
                                   "deep learning", "generative ai", "genai", "llm", "computer vision", "nlp",
                                   "natural language", "ذكاء اصطناعي", "الذكاء الاصطناعي", "تعلم الآلة"]),
    "Data Science / Analytics": (26, ["data science", "data analytics", "big data", "analytics", "business intelligence",
                                      "data platform", "data engineering", "علم البيانات", "تحليل البيانات", "البيانات الضخمة"]),
    "Cybersecurity": (24, ["cybersecurity", "cyber security", "information security", "soc ", "security operations",
                           "managed security", "penetration testing", "threat", "الأمن السيبراني", "أمن المعلومات"]),
    "FinTech": (20, ["fintech", "payments", "payment", "digital bank", "neobank", "lending", "bnpl", "wealthtech",
                     "insurtech", "open banking", "تقنية مالية", "التقنية المالية", "مدفوعات"]),
    "Cloud": (16, ["cloud", "data center", "datacenter", "hyperscale", "سحابية", "الحوسبة السحابية", "مراكز البيانات"]),
    "Tech Consulting": (14, ["digital transformation", "technology consulting", "it consulting", "consultancy",
                             "management consulting", "التحول الرقمي", "استشارات تقنية"]),
    "Software / IT": (10, ["software", "saas", "it services", "information technology", "platform", "app development",
                           "systems integrator", "برمجيات", "تقنية المعلومات", "حلول تقنية"]),
}
# Map of your projects to company-description keywords that make them relevant.
# Override in profile.json under "project_signals".
_DEFAULT_PROJECT_SIGNALS = {
    "Cybersecurity project": ["intrusion", "soc", "threat detection", "anomaly", "fraud",
                              "security analytics", "siem", "network security"],
    "Multi-agent systems": ["agent", "agentic", "multi-agent", "automation", "orchestration", "autonomous",
                            "workflow", "copilot"],
    "Time series / IoT": ["forecast", "time series", "predictive", "smart city", "crowd",
                          "mobility", "iot", "routing", "optimization", "digital twin"],
    "RAG / NLP project": ["rag", "retrieval", "chatbot", "conversational", "search", "llm", "nlp",
                          "recommendation", "knowledge base"],
    "Python / API": ["python", "fastapi", "api", "backend", "microservice"],
}
PROJECT_SIGNALS: dict = config.CANDIDATE.get("project_signals", _DEFAULT_PROJECT_SIGNALS)
COOP_WORDS = ["co-op", "coop", "cooperative training", "co-operative", "تدريب تعاوني", "التدريب التعاوني",
              "intern", "internship", "trainee", "تدريب", "متدرب", "summer training"]
TERM_WORDS = ["january 2027", "jan 2027", "2027", "term 2", "second semester", "الفصل الثاني", "1448"]

ATS_HOSTS = ["jobs.lever.co", "boards.greenhouse.io", "job-boards.greenhouse.io", "apply.workable.com",
             "jobs.smartrecruiters.com", "myworkdayjobs.com", "oraclecloud.com", "successfactors", "sapsf",
             "jisr.net", "bamboohr.com", "recruitee.com", "teamtailor.com", "zohorecruit", "jobs.ashbyhq.com",
             "breezy.hr", "freshteam.com", "careers-page.com", "hr.cloud.sap"]
JOB_BOARDS = ["linkedin.com", "bayt.com", "indeed.com", "glassdoor.com", "naukrigulf.com", "gulftalent.com",
              "founditgulf.com", "wuzzuf.net", "jobxdubai.com", "edarabia.com", "tawzeefksa.com", "linkedksa.com",
              "tm.com.sa", "sauditawdhef.com", "jobzaty.com", "sabbar.com", "jobleads.com", "job-ksa.com"]
DIRECTORIES = ["crunchbase.com", "tracxn.com", "magnitt.com", "f6s.com", "wamda.com", "fintechsaudi.com",
               "dealroom.co", "cbinsights.com", "pitchbook.com"]
NOISE_HOSTS = ["wikipedia.org", "youtube.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com",
               "reddit.com", "quora.com", "medium.com", "pinterest.com", "amazon.", "noon.com", "argaam.com",
               "arabnews.com", "saudigazette.com.sa", "zawya.com", "spa.gov.sa", "alarabiya.net", "aawsat.com",
               "okaz.com.sa", "sabq.org", "aleqt.com", "gulfnews.com", "thenationalnews.com", "reuters.com",
               "bloomberg.com", "forbes", "tickerchart.com", "google.com", "bing.com", "apple.com", "microsoft.com/en-us/store",
               "github.com", "stackoverflow.com", "scribd.com", "slideshare.net", "researchgate.net", "academia.edu",
               "tripadvisor", "booking.com", "yelp.com", "yellowpages", "dnb.com", "zoominfo.com", "rocketreach.co",
               "signalhire.com", "builtwith.com", "opencorporates.com", "ksa.directory"]
GENERIC_NAMES = {"home", "careers", "career", "jobs", "job", "about us", "contact us", "contact", "login", "sign in",
                 "linkedin", "crunchbase", "tracxn", "official website", "الصفحة الرئيسية", "وظائف", "الرئيسية",
                 "riyadh", "saudi arabia", "ksa", "top", "best", "list", "companies"}

# ---------------------------------------------------------------------------
# Curated Riyadh employers (well-known, Riyadh-based or with a Riyadh office). Sectors drive the score;
# the live search adds links, areas and openings. Websites only where certain.
# ---------------------------------------------------------------------------
CURATED = [
    # AI / data / government-tech
    ("HUMAIN", "", "AI / Machine Learning", ""), ("SDAIA", "sdaia.gov.sa", "AI / Machine Learning", ""),
    ("Intelmatix", "", "AI / Machine Learning", ""), ("Lucidya", "lucidya.com", "AI / Machine Learning", ""),
    ("Mozn", "", "AI / Machine Learning;FinTech", ""), ("Elm", "elm.sa", "AI / Machine Learning;Software / IT", ""),
    ("Thiqah", "thiqah.sa", "Software / IT;Cloud", ""), ("Tahakom", "", "AI / Machine Learning;Software / IT", ""),
    ("Waqd AI", "witco.sa", "AI / Machine Learning", "Al Aqiq"), ("Quant Data & Analytics", "quant.sa",
     "Data Science / Analytics;AI / Machine Learning", "Al Sahafah"),
    ("Professional Matrix AI", "", "AI / Machine Learning;Software / IT", "Al Wadi"),
    ("AIP - Artificial Intelligence Place", "", "AI / Machine Learning", "Al Nada"),
    ("Ebhar AI & Data Analytics", "", "AI / Machine Learning;Data Science / Analytics", ""),
    ("Takamol Holding", "", "Software / IT;Data Science / Analytics", ""), ("Tamkeen Technologies", "tamkeentech.sa", "Software / IT", ""),
    ("National Information Center (NIC)", "", "Data Science / Analytics;Cloud", ""),
    ("Digital Government Authority (DGA)", "dga.gov.sa", "Software / IT;Tech Consulting", ""),
    ("KACST", "kacst.gov.sa", "AI / Machine Learning;Data Science / Analytics", ""),
    ("General Authority for Statistics (GASTAT)", "stats.gov.sa", "Data Science / Analytics", ""),
    ("Aramco Digital", "", "AI / Machine Learning;Cloud", ""),
    # Cybersecurity
    ("SITE - Saudi Information Technology Company", "", "Cybersecurity", "Al Nakheel"),
    ("DataSec", "datasec.sa", "Cybersecurity;Cloud", ""), ("Looptech for Cybersecurity", "", "Cybersecurity", "Al Yasmin"),
    ("Cipher (Aramco Digital cybersecurity)", "", "Cybersecurity", ""), ("Sirar by stc", "", "Cybersecurity", ""),
    ("Help AG Saudi Arabia", "", "Cybersecurity", ""), ("National Cybersecurity Authority (NCA)", "nca.gov.sa", "Cybersecurity", ""),
    ("House of Analytica", "", "Cybersecurity;Data Science / Analytics", "Al Malqa"),
    # FinTech & banks
    ("Lean Technologies", "", "FinTech", ""), ("Tamara", "tamara.co", "FinTech", ""), ("Tabby", "tabby.ai", "FinTech", ""),
    ("stc bank", "", "FinTech", ""), ("Barq", "", "FinTech", ""), ("Geidea", "", "FinTech", ""), ("Moyasar", "", "FinTech", ""),
    ("Lendo", "", "FinTech", ""), ("Raqamyah", "", "FinTech", ""), ("Tamam", "", "FinTech", ""), ("Erad", "", "FinTech", ""),
    ("Hala", "", "FinTech", ""), ("Rasan", "", "FinTech;Software / IT", ""), ("Derayah Financial", "", "FinTech", ""),
    ("D360 Bank", "", "FinTech", ""), ("Fintech Saudi", "fintechsaudi.com", "FinTech", "Al Aqiq"),
    ("Saudi National Bank (SNB)", "alahli.com", "FinTech", "Al Aqiq"), ("Al Rajhi Bank", "alrajhibank.com.sa", "FinTech", ""),
    ("Riyad Bank", "riyadbank.com", "FinTech", ""), ("Alinma Bank", "alinma.com", "FinTech", ""), ("SAB", "sab.com", "FinTech", ""),
    ("Banque Saudi Fransi", "", "FinTech", ""), ("Arab National Bank", "anb.com.sa", "FinTech", ""),
    ("Bank Albilad", "bankalbilad.com", "FinTech", ""), ("Bank Aljazira", "", "FinTech", ""), ("Saudi Investment Bank", "", "FinTech", ""),
    ("Saudi Exchange (Tadawul Group)", "", "FinTech;Data Science / Analytics", ""), ("Public Investment Fund (PIF)", "pif.gov.sa", "FinTech", "KAFD"),
    ("Saudi Central Bank (SAMA)", "sama.gov.sa", "FinTech", ""), ("Visa Saudi Arabia", "visa.com.sa", "FinTech", ""),
    ("Mastercard Saudi Arabia", "", "FinTech", ""), ("Tawuniya", "tawuniya.com", "FinTech", ""),
    # Telecom & cloud
    ("stc", "stc.com.sa", "Cloud;Software / IT", ""), ("Mobily", "mobily.com.sa", "Cloud;Software / IT", ""),
    ("Zain KSA", "sa.zain.com", "Cloud;Software / IT", ""), ("Salam", "", "Cloud", ""), ("center3", "", "Cloud", ""),
    ("solutions by stc", "", "Cloud;Tech Consulting", ""), ("Oracle Saudi Arabia", "", "Cloud", ""),
    ("Microsoft Saudi Arabia", "", "Cloud;AI / Machine Learning", ""), ("Google Cloud Saudi Arabia", "", "Cloud;AI / Machine Learning", ""),
    ("Amazon Web Services Saudi Arabia", "", "Cloud", ""), ("Huawei Saudi Arabia", "", "Cloud;AI / Machine Learning", ""),
    ("SAP KSA", "", "Cloud;Software / IT", "Al Nakheel"), ("IBM Saudi Arabia", "", "Cloud;AI / Machine Learning", ""),
    ("Cisco Saudi Arabia", "", "Cloud;Cybersecurity", ""), ("Dell Technologies Saudi Arabia", "", "Cloud", ""),
    ("Alibaba Cloud (SCCC)", "", "Cloud", ""), ("DataVolt", "", "Cloud", ""),
    # Consulting
    ("Accenture Saudi Arabia", "", "Tech Consulting;AI / Machine Learning", ""), ("Deloitte Middle East (KSA)", "", "Tech Consulting", ""),
    ("PwC Middle East (KSA)", "", "Tech Consulting", ""), ("EY Saudi Arabia", "", "Tech Consulting", ""),
    ("KPMG Saudi Arabia", "", "Tech Consulting", ""), ("McKinsey & Company Riyadh", "", "Tech Consulting", ""),
    ("Boston Consulting Group Riyadh", "", "Tech Consulting", ""), ("Bain & Company Riyadh", "bain.com", "Tech Consulting", ""),
    ("Kearney Riyadh", "", "Tech Consulting", ""), ("Strategy& Middle East", "", "Tech Consulting", ""),
    ("Oliver Wyman Riyadh", "", "Tech Consulting", ""), ("Roland Berger Riyadh", "", "Tech Consulting", ""),
    ("Capgemini Saudi Arabia", "", "Tech Consulting;Software / IT", ""), ("Strategic Gears", "", "Tech Consulting;Data Science / Analytics", ""),
    ("Ejada", "", "Software / IT;Tech Consulting", ""), ("Al Moammar Information Systems (MIS)", "", "Software / IT;Cybersecurity", ""),
    ("Saudi Business Machines (SBM)", "", "Software / IT;Cloud", ""), ("Perfect Presentation (2P)", "", "Software / IT", ""),
    ("Smart Dimensions for IT (SDIT)", "", "Software / IT", "Al Nakheel"), ("Comptechco", "", "Software / IT", "Al Nakheel"),
    # Tech companies & startups
    ("Foodics", "foodics.com", "Software / IT;Data Science / Analytics", "Al Aqiq"), ("Zid", "zid.sa", "Software / IT", ""),
    ("Salla", "salla.sa", "Software / IT", ""), ("Sary", "", "Software / IT", ""), ("Ninja", "", "Software / IT", ""),
    ("Nana", "", "Software / IT", ""), ("Jahez", "", "Software / IT;Data Science / Analytics", ""), ("Mrsool", "", "Software / IT", ""),
    ("Rewaa", "", "Software / IT", ""), ("Qoyod", "", "Software / IT;FinTech", ""), ("Wafeq", "", "Software / IT;FinTech", ""),
    ("Unifonic", "unifonic.com", "Software / IT;AI / Machine Learning", ""), ("Taqnyat", "", "Software / IT", ""),
    ("Noon Academy", "", "Software / IT;AI / Machine Learning", ""), ("Careem Saudi Arabia", "", "Software / IT", ""),
    ("Aqar", "", "Software / IT", ""), ("ROSHN", "", "Software / IT", ""), ("Riyadh Air", "", "Software / IT;Data Science / Analytics", ""),
    ("SABIC", "sabic.com", "Data Science / Analytics", ""), ("Saudi Electricity Company", "se.com.sa", "Data Science / Analytics", ""),
    ("SMSA Express", "", "Software / IT", ""), ("Naqel Express", "", "Software / IT", ""),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NON_WORD = re.compile(r"[^0-9a-z\u0600-\u06ff]+")


def norm(text: str) -> str:
    """Lower-case, punctuation → spaces, padded: "AI-powered, Riyadh." → " ai powered riyadh " """
    return " " + _NON_WORD.sub(" ", (text or "").lower()).strip() + " "


def has_any(text: str, words: list[str]) -> list[str]:
    """Keywords present in text. Short keywords (≤3 letters, e.g. "ai", "soc") must be whole words."""
    t = norm(text)
    hits = []
    for w in words:
        k = norm(w).strip()
        if k and ((" " + k + " ") in t if len(k) <= 3 else k in t):
            hits.append(w)
    return hits


def domain_of(url: str) -> str:
    host = urllib.parse.urlsplit(url if "://" in (url or "") else f"https://{url}").netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("com", "gov", "edu", "org", "net", "co") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def host_in(url: str, hosts: list[str]) -> bool:
    h = urllib.parse.urlsplit(url).netloc.lower()
    return any(x in h for x in hosts)


def clean_name(raw: str) -> str:
    n = re.sub(r"\s+", " ", raw or "").strip(" -|–—:·•,")
    n = re.sub(r"(?i)\b(careers?|jobs?|official (web)?site|home ?page|company profile.*|funding.*|linkedin|"
               r"crunchbase|tracxn|overview|about us)\b.*$", "", n).strip(" -|–—:·•,")
    return n


def valid_name(n: str) -> bool:
    if not n or not (2 <= len(n) <= 60) or not re.search(r"[A-Za-z؀-ۿ]", n):
        return False
    low = n.lower()
    if low in GENERIC_NAMES or re.match(r"^(top|best|list of|\d+)\b", low):
        return False
    if len(n.split()) > 7:                       # a sentence, not a name
        return False
    return True


def name_from_title(title: str, url: str) -> str:
    """Picks the title segment that names the company (prefers the one matching the domain)."""
    segs = [clean_name(s) for s in re.split(r"\s[|–—\-:]\s|\s\|\s|\|", title or "") if s.strip()]
    segs = [s for s in segs if valid_name(s)]
    if not segs:
        return ""
    stem = domain_of(url).split(".")[0]
    for s in segs:
        if stem and stem[:5] in re.sub(r"[^a-z0-9]", "", s.lower()):
            return s
    return min(segs, key=len) if len(segs) > 1 else segs[0]


def ats_company(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    host, parts = p.netloc.lower(), [x for x in p.path.split("/") if x]
    if any(h in host for h in ("lever.co", "greenhouse.io", "workable.com", "smartrecruiters.com", "ashbyhq.com")) and parts:
        return parts[0].replace("-", " ").replace("_", " ").title()
    if "myworkdayjobs.com" in host:
        return host.split(".")[0].replace("-", " ").title()
    return ""


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class Entity:
    company: str
    key: str
    website: str = ""
    careers_url: str = ""
    linkedin_url: str = ""
    opp_title: str = ""
    opp_url: str = ""
    direct_form: bool = False
    is_coop: bool = False
    sectors: set = field(default_factory=set)
    areas: set = field(default_factory=set)
    riyadh: bool = False
    text: str = ""
    sources: set = field(default_factory=set)
    gpa_reason: str = ""
    score: int = 0
    reasons: str = ""
    status: str = ""
    db_note: str = ""


def key_for(name: str, website: str) -> str:
    return f"d:{domain_of(website)}" if website else f"n:{app_db.company_fp(name)}"


class Store:
    def __init__(self):
        self.by_key: dict[str, Entity] = {}
        self.fp_to_key: dict[str, str] = {}

    def add(self, company: str, website: str = "", **kw) -> Entity:
        company = clean_company_name(company, website) or company
        fp = app_db.company_fp(company)
        key = key_for(company, website)
        if key not in self.by_key and fp in self.fp_to_key:       # same company found by name
            key = self.fp_to_key[fp]
        e = self.by_key.get(key)
        if e is None:
            e = self.by_key[key] = Entity(company=company, key=key, website=website)
            self.fp_to_key[fp] = key
        e.website = e.website or website
        for k, v in kw.items():
            if isinstance(getattr(e, k), set):
                getattr(e, k).update(v if isinstance(v, (set, list, tuple)) else [v])
            elif k == "text":
                e.text = (e.text + " \n " + v)[:6000]
            elif v and not getattr(e, k):
                setattr(e, k, v)
            elif k in ("is_coop", "direct_form", "riyadh") and v:
                setattr(e, k, True)
        return e


def classify(text: str) -> tuple[set, set, bool]:
    sectors = {s for s, (_, words) in SECTORS.items() if has_any(text, words)}
    areas = {a for a, words in NORTH_AREAS.items() if has_any(text, words)}
    riyadh = bool(areas) or bool(has_any(text, RIYADH_WORDS))
    return sectors, areas, riyadh


def ingest_result(store: Store, r: dict, query: str) -> bool:
    """Turns one search result into a company / opportunity, or rejects it. Returns True if kept."""
    url, title, body = r.get("href") or r.get("url") or "", r.get("title") or "", r.get("body") or ""
    if not url.startswith("http") or host_in(url, NOISE_HOSTS):
        return False
    text = f"{title} {body}"
    sectors, areas, riyadh = classify(text)
    only_elsewhere = has_any(text, OTHER_CITIES) and not riyadh
    if not sectors or only_elsewhere:
        return False
    if offshore_reason(text, url):                                     # India / Pakistan … with no Saudi mention
        return False
    if not riyadh and not domain_of(url).endswith(".sa"):
        return False
    coop = bool(has_any(text, COOP_WORDS))
    src = f"search: {query[:60]}"

    if host_in(url, ATS_HOSTS):                                        # job board of the company itself
        name = ats_company(url) or name_from_title(title.split(" at ")[-1] if " at " in title else title, url)
        if not valid_name(name):
            return False
        board = re.match(r"https?://[^/]+/[^/]+", url)
        store.add(name, careers_url=board.group(0) if board else url, opp_title=title if coop else "",
                  opp_url=url if coop else "", direct_form=coop, is_coop=coop, sectors=sectors, areas=areas,
                  riyadh=True, text=text, sources=src)
        return True

    if "linkedin.com" in url:
        m = re.search(r"linkedin\.com/company/([^/?#]+)", url)
        if m:
            name = clean_name(title.split("|")[0])
            if not valid_name(name):
                return False
            store.add(name, linkedin_url=f"https://www.linkedin.com/company/{m.group(1)}", sectors=sectors,
                      areas=areas, riyadh=riyadh, text=text, sources=src)
            return True
        m = re.match(r"(.+?) hiring (.+?) in (.+?)(?: \||$)", title)
        if m and coop:
            name = clean_name(m.group(1))
            if valid_name(name):
                store.add(name, opp_title=m.group(2), opp_url=url, is_coop=True, sectors=sectors, areas=areas,
                          riyadh=True, text=text, sources=src)
                return True
        return False

    if host_in(url, JOB_BOARDS):                                       # postings on third-party boards
        m = re.match(r"(.+?)\s+(?:at|-|–|\|)\s+(.+?)(?:\s+[-|–].*)?$", title)
        if not (m and coop):
            return False
        a, b = clean_name(m.group(1)), clean_name(m.group(2))
        name = b if has_any(a, COOP_WORDS) else a
        if not valid_name(name) or has_any(name, COOP_WORDS):
            return False
        store.add(name, opp_title=title, opp_url=url, is_coop=True, sectors=sectors, areas=areas, riyadh=True,
                  text=text, sources=src)
        return True

    if host_in(url, DIRECTORIES):                                      # Crunchbase, Tracxn, MAGNiTT …
        name = clean_name(re.split(r"\s[-|–—:]\s", title)[0])
        if not valid_name(name) or not riyadh:
            return False
        store.add(name, sectors=sectors, areas=areas, riyadh=True, text=text, sources=src)
        return True

    name = name_from_title(title, url)                                 # the company's own site
    if not valid_name(name) or is_directory(url, name) or is_directory("", title):
        return False
    path = urllib.parse.urlsplit(url).path.lower()
    careers = url if re.search(r"career|jobs|join|vacanc|وظائف|التوظيف", path) and not not_careers_reason(url) else ""
    store.add(name, website=f"https://{domain_of(url)}", careers_url=careers,
              opp_title=title if coop and careers else "", opp_url=url if coop and careers else "",
              is_coop=coop and bool(careers), sectors=sectors, areas=areas, riyadh=riyadh, text=text, sources=src)
    return True


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def build_queries() -> list[str]:
    sectors = ["artificial intelligence company", "machine learning startup", "data analytics company",
               "data science company", "cybersecurity company", "fintech company", "cloud services provider",
               "digital transformation consultancy", "software company", "generative AI startup"]
    areas = ["KAFD", "Riyadh Front", "Al Yasmin", "Al Sahafah", "Al Malqa", "Al Aqiq", "Al Nakheel", "North Riyadh"]
    q = [f"{s} {a} Riyadh" for s in sectors[:6] for a in areas]                 # 48 sector × area
    q += [f"{s} Riyadh Saudi Arabia" for s in sectors]                           # 10
    q += [f'"COOP" OR "co-op" {t} Riyadh 2027' for t in
          ["artificial intelligence", "data science", "cybersecurity", "software engineering", "fintech", "data analytics"]]
    q += [f'site:{h} Riyadh intern OR co-op OR trainee' for h in
          ["jobs.lever.co", "boards.greenhouse.io", "apply.workable.com", "jobs.smartrecruiters.com", "myworkdayjobs.com"]]
    q += ['site:linkedin.com/jobs "COOP" Riyadh data OR AI OR cyber', 'site:linkedin.com/jobs internship Riyadh "artificial intelligence"']
    q += [f'site:{d} Riyadh {s}' for d in ["linkedin.com/company", "crunchbase.com/organization", "tracxn.com", "magnitt.com"]
          for s in ["artificial intelligence", "cybersecurity", "fintech", "data analytics"]]
    q += ['"تدريب تعاوني" ذكاء اصطناعي الرياض 2027', '"تدريب تعاوني" الأمن السيبراني الرياض',
          '"تدريب تعاوني" علم البيانات الرياض', 'شركات الذكاء الاصطناعي في الرياض', 'شركات التقنية المالية الرياض',
          'شركات الأمن السيبراني الرياض']
    seen, out = set(), []
    for x in q:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def live_search(store: Store, max_queries: int, per_query: int, target: int) -> dict:
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            print("⚠️ Neither `ddgs` nor `duckduckgo_search` is installed: pip install ddgs   (skipping live search)")
            return {"queries": 0, "results": 0, "kept": 0}
    stats = {"queries": 0, "results": 0, "kept": 0, "errors": 0}
    queries = build_queries()[:max_queries]
    with DDGS() as ddgs:
        for i, q in enumerate(queries, 1):
            for attempt in range(2):
                try:
                    results = list(ddgs.text(q, region="xa-en" if re.search(r"[A-Za-z]", q) else "xa-ar",
                                             max_results=per_query) or [])
                    break
                except Exception as e:
                    stats["errors"] += 1
                    wait = 20 if attempt == 0 else 0
                    print(f"   ⚠️ search error ({type(e).__name__}){' — waiting 20 s' if wait else ''}")
                    time.sleep(wait)
                    results = []
            kept = sum(ingest_result(store, r, q) for r in results)
            stats["queries"] += 1
            stats["results"] += len(results)
            stats["kept"] += kept
            print(f"[{i:>3}/{len(queries)}] {kept:>2}/{len(results):<2} kept · {len(store.by_key)} companies · {q[:70]}")
            if len(store.by_key) >= target * 1.3 and i >= 30:
                print("   ✓ enough candidates collected")
                break
            time.sleep(random.uniform(1.5, 3.0))                         # polite pacing
    return stats


def load_seed_xlsx(store: Store) -> int:
    if not SEED_XLSX.exists():
        return 0
    import openpyxl
    n = 0
    for row in openpyxl.load_workbook(SEED_XLSX, read_only=True, data_only=True).active.iter_rows(values_only=True):
        if not row or not row[0] or row[0] in ("الاسم",) or len(row) < 4:
            continue
        name, site, _contact, city = (str(x or "").strip() for x in row[:4])
        if "الرياض" not in city and "riyadh" not in city.lower():
            continue                                                       # Riyadh only
        name_en = re.split(r"\s*\(", name)[0].strip() or name
        site = site if site.startswith("http") else ""
        store.add(name_en, website=f"https://{domain_of(site)}" if site else "", careers_url=site if "career" in site else "",
                  sectors={"AI / Machine Learning"}, riyadh=True, text=name, sources="data/saudi_ai_companies.xlsx")
        n += 1
    return n


def load_curated(store: Store) -> int:
    for name, site, sectors, area in CURATED:
        store.add(name, website=f"https://{site}" if site else "", sectors=set(sectors.split(";")),
                  areas={area} if area else set(), riyadh=True, text=f"{name} {sectors}", sources="curated list")
    return len(CURATED)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def fit_score(e: Entity) -> tuple[int, str]:
    why, s = [], 0
    pts = sorted((SECTORS[x][0] for x in e.sectors if x in SECTORS), reverse=True)
    if pts:
        sector_pts = min(36, pts[0] + 3 * (len(pts) - 1))
        s += sector_pts
        why.append(f"+{sector_pts} sector ({', '.join(sorted(e.sectors))})")
    proj = [p for p, words in PROJECT_SIGNALS.items() if has_any(e.text, words)]
    if proj:
        p = min(24, 6 * len(proj))
        s += p
        why.append(f"+{p} matches {', '.join(x.split(' (')[0] for x in proj)}")
    north = [a for a in e.areas]
    if north:
        s += 15
        why.append(f"+15 North Riyadh ({', '.join(sorted(north))})")
    elif e.riyadh:
        s += 6
        why.append("+6 Riyadh")
    if e.is_coop:
        s += 20
        why.append("+20 co-op/internship posting")
        if e.direct_form:
            s += 6
            why.append("+6 direct application form")
        if has_any(e.text, TERM_WORDS):
            s += 4
            why.append("+4 mentions 2027 / Term 2")
    elif e.careers_url or e.linkedin_url:
        s += 3
        why.append("+3 careers page known")
    return max(0, min(100, s)), "; ".join(why)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def write_db(entities: list[Entity], dry_run: bool) -> dict:
    stats = {"queued": 0, "reviewed": 0, "rejected": 0, "already_in_db": 0}
    with app_db.connect() as con:
        existing = {}
        for r in con.execute("SELECT id, company, apply_url, status FROM opportunities"):
            existing.setdefault(app_db.company_fp(r["company"] or ""), r)
            if r["apply_url"]:
                existing.setdefault("url:" + r["apply_url"].rstrip("/").lower(), r)
        for e in entities:
            fp = app_db.company_fp(e.company)
            url_hit = existing.get("url:" + (e.opp_url or "").rstrip("/").lower()) if e.opp_url else None
            hit = url_hit or (existing.get(fp) if not e.is_coop else None)
            if hit:
                e.status, e.db_note = hit["status"], f"already in DB (#{hit['id']})"
                stats["already_in_db"] += 1
                continue
            if e.gpa_reason:
                status = "rejected"
            elif e.is_coop and e.direct_form and e.opp_url:
                status = "queued"
            else:
                status = "reviewed"
            e.status = status
            stats[status] += 1
            if dry_run:
                e.db_note = "dry run (not written)"
                continue
            if e.is_coop and e.opp_url:
                apply_type = "external" if e.direct_form else (
                    "easy_apply_or_unknown" if "linkedin.com" in e.opp_url else "careers_page")
                title, url = e.opp_title[:160] or "Co-op / internship", e.opp_url
            else:
                apply_type, title = "careers_page", "Co-op inquiry (company lead)"
                url = e.careers_url or e.website or e.linkedin_url
            row = {"job_id": f"harvest:{e.key}:{title[:60]}", "source": "harvest_riyadh", "company": e.company,
                   "title": title, "apply_url": url, "apply_type": apply_type, "location": "Riyadh, Saudi Arabia"
                   + (f" ({', '.join(sorted(e.areas))})" if e.areas else ""), "posted_date": TODAY,
                   "linkedin_url": e.linkedin_url if "jobs/view" in e.linkedin_url else "",
                   "source_url": e.opp_url or e.careers_url or e.website}
            opp_id, created = app_db.upsert(con, row)
            if not created:                            # merged into an existing row: leave its status and score alone
                e.status, e.db_note = "(existing)", f"merged into #{opp_id}"
                stats[status] -= 1
                stats["already_in_db"] += 1
                continue
            app_db.set_status(con, opp_id, status, f"GPA filter: {e.gpa_reason}" if e.gpa_reason else "harvested")
            con.execute("UPDATE opportunities SET score=?, score_reasons=?, notes=TRIM(notes || ' ' || ?) WHERE id=?",
                        (e.score, e.reasons[:500], f"[{', '.join(sorted(e.sectors))}]", opp_id))
            e.db_note = f"#{opp_id} created"
    return stats


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def export_xlsx(entities: list[Entity], path: Path, summary: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "Ranked"
    cols = ["Rank", "Company", "Fit Score", "Type", "DB status", "Sectors", "North Riyadh area", "Apply / careers link",
            "Website", "LinkedIn", "Co-op / internship title", "GPA filter", "Why this score", "Source", "DB note"]
    ws.append(cols)
    for i, e in enumerate(entities, 1):
        link = e.opp_url or e.careers_url or e.website or e.linkedin_url
        lookup = "" if link else ("https://www.google.com/search?q=" + urllib.parse.quote(f"{e.company} Riyadh careers"))
        ws.append([i, e.company, e.score, "Opportunity" if e.is_coop else "Company lead", e.status,
                   ", ".join(sorted(e.sectors)), ", ".join(sorted(e.areas)), link or lookup, e.website, e.linkedin_url,
                   e.opp_title[:120], e.gpa_reason, e.reasons, "; ".join(sorted(e.sources))[:200], e.db_note])
    head = PatternFill("solid", fgColor="1F3A5F")
    for c in ws[1]:
        c.font, c.fill, c.alignment = Font(bold=True, color="FFFFFF"), head, Alignment(vertical="center")
    widths = [6, 34, 9, 13, 14, 34, 20, 48, 30, 40, 40, 30, 70, 40, 22]
    for j, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(j)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in (row[7], row[8], row[9]):
            if isinstance(c.value, str) and c.value.startswith("http"):
                c.hyperlink, c.font = c.value, Font(color="1F5FBF", underline="single")
        sc = row[2]
        sc.fill = PatternFill("solid", fgColor="D8F0DD" if sc.value >= 70 else "FFF4CC" if sc.value >= 50 else "F3F3F3")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = ws.dimensions
    s2 = wb.create_sheet("Summary")
    for k, v in summary.items():
        s2.append([k, str(v)])
    s2.column_dimensions["A"].width, s2.column_dimensions["B"].width = 34, 90
    wb.save(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no live search: files + curated list only")
    ap.add_argument("--dry-run", action="store_true", help="don't write to applications.db")
    ap.add_argument("--max-queries", type=int, default=200)
    ap.add_argument("--per-query", type=int, default=25)
    ap.add_argument("--target", type=int, default=400)
    a = ap.parse_args()

    store = Store()
    n_cur = load_curated(store)
    n_xl = load_seed_xlsx(store)
    print(f"Seeds: {n_cur} curated + {n_xl} from {SEED_XLSX.name}")
    stats = {"queries": 0, "results": 0, "kept": 0}
    if not a.offline:
        stats = live_search(store, a.max_queries, a.per_query, a.target)

    entities, dropped = [], 0
    for e in store.by_key.values():
        if not (e.riyadh and e.sectors):
            continue
        site = e.website or e.careers_url
        if is_directory(site, e.company) or offshore_reason(e.text if site else "", site):
            dropped += 1                                               # listing sites / offshore-only firms
            continue
        entities.append(e)
    if dropped:
        print(f"Dropped {dropped} directory / offshore-only rows")
    for e in entities:
        e.gpa_reason = gpa_requirement_reason(e.text) or ""
        e.score, e.reasons = fit_score(e)
    entities.sort(key=lambda e: (-e.score, e.company.lower()))

    db_stats = write_db(entities, a.dry_run)
    summary = {"Run date": TODAY, "Mode": "offline" if a.offline else "live search + files",
               "Search queries / results / kept": f"{stats.get('queries')} / {stats.get('results')} / {stats.get('kept')}",
               "Companies + opportunities (after filters & dedupe)": len(entities),
               "Co-op / internship postings": sum(e.is_coop for e in entities),
               "In North Riyadh areas": sum(bool(e.areas) for e in entities),
               "GPA-rejected": sum(bool(e.gpa_reason) for e in entities),
               "DB: queued / reviewed / rejected / already in DB": f"{db_stats['queued']} / {db_stats['reviewed']} / "
                                                                    f"{db_stats['rejected']} / {db_stats['already_in_db']}"
                                                                    + (" (dry run)" if a.dry_run else "")}
    export_xlsx(entities, OUT_XLSX, summary)

    print("\n" + "\n".join(f"{k}: {v}" for k, v in summary.items()))
    if len(entities) < a.target:
        print(f"ℹ️ {len(entities)} rows — below the {a.target} target. Run again with live search (no --offline) "
              f"or raise --max-queries; every row is a real, filtered result, so the count isn't padded.")
    print(f"\n📊 {OUT_XLSX.name}\n\nTop 20:")
    for i, e in enumerate(entities[:20], 1):
        link = e.opp_url or e.careers_url or e.website or e.linkedin_url or "(no link yet)"
        flag = " ⛔ GPA" if e.gpa_reason else ""
        print(f"{i:>2}. [{e.score:>3}] {e.company[:40]:<40} {e.status:<10}{flag}  {link}")


if __name__ == "__main__":
    main()
