# COOP Application Agent

An end-to-end automation pipeline for cooperative training (COOP) and internship applications targeting AI, Data Science, and tech roles in the GCC region.

The agent discovers companies, resolves their careers pages to direct application URLs, tailors a one-page CV per company, generates a cover letter, and fills multi-step web forms with Playwright — pausing only at the final Submit button for human review.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        COOP Application Agent                        │
├──────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────────────────────┐  │
│  │  Discovery   │──▶│   Scoring &  │──▶│   Careers Resolution     │  │
│  │  Agents      │   │   Filtering  │   │   & ATS Board Resolver   │  │
│  │              │   │              │   │                          │  │
│  │ • Google     │   │ • Sector fit │   │ • Workable, Greenhouse,  │  │
│  │ • LinkedIn   │   │ • Offshore   │   │   Lever, Zoho, Manatal,  │  │
│  │ • Telegram   │   │   filtering  │   │   SmartRecruiters, Ashby │  │
│  │ • Startup    │   │ • GPA gate   │   │ • Direct posting lookup  │  │
│  │   scraping   │   │ • Dedup      │   │ • robots.txt respect     │  │
│  └─────────────┘   └──────────────┘   └───────────┬──────────────┘  │
│                                                    │                 │
│                                                    ▼                 │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │                    Application Engine                        │    │
│  │                                                              │    │
│  │  cv_tailor.py ──▶ cover_letter.py ──▶ job_form_filler.py    │    │
│  │                                                              │    │
│  │  • Per-company CV   • Facts-only      • Multi-step wizard   │    │
│  │    from cv_facts     letter from        navigation           │    │
│  │  • Quality gate       same facts      • Semantic paraphrase │    │
│  │    (1 page, ATS)   • PDF + text         memory matching     │    │
│  │  • No GPA on CV      variants         • Human-in-the-loop   │    │
│  │                                         at Submit            │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  ┌──────────────┐   ┌────────────────────────────────────────────┐   │
│  │  SQLite DB   │◀──│  memory_manager.py                         │   │
│  │  (app state) │   │  • Profile defaults from profile.json      │   │
│  │              │   │  • Semantic embedding similarity (MiniLM)   │   │
│  │  • queued    │   │  • Passive learning: remembers every new   │   │
│  │  • applied   │   │    answer for future auto-fill             │   │
│  │  • rejected  │   │  • 3-tier lookup: profile → memory → ask   │   │
│  └──────────────┘   └────────────────────────────────────────────┘   │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Key Features

### Multi-Step Wizard Support
Handles Workday, Oracle, Taleo, Bain-style multi-page forms. After filling each step, the agent detects and clicks **Next / Continue / Save and Continue**, waits for the new page, and fills it — up to 8 steps. It stops at the final **Submit / Review** page and at any mandatory field it cannot fill.

### Semantic Paraphrase Memory
`memory_manager.py` uses `sentence-transformers/all-MiniLM-L6-v2` to compute embeddings for every question it encounters. When a new form asks "What is your area of study?" after you already answered "What is your major?", the system finds the semantic match (cosine similarity ≥ 0.82) and fills it automatically — no duplicate prompts across companies.

### Human-in-the-Loop Safety
- The browser **never clicks Submit** without your confirmation.
- Nafath / SMS OTP / manual login pages pause and wait for you.
- `--no-ask` mode fills everything it knows and leaves unknown fields for you to complete in the browser — no terminal interrupts.
- Verification steps are remembered per-page so they don't ask twice.

### ATS Board Resolution
For job boards (Workable, Greenhouse, Lever, Zoho Recruit, Manatal, SmartRecruiters, Ashby, BambooHR, Breezy, Recruitee), the resolver fetches the board's API, finds the best matching internship/entry-level posting in Saudi Arabia, and queues the direct apply URL — not the board root.

### Truthful CV Generation
Every sentence in a tailored CV comes from `cv_facts.json`. The LLM writes only the 2–3 sentence summary, which is rejected if it contains any number or technology not in the facts. GPA is never printed on CVs or cover letters.

### Offshore & Junk Filtering
Companies with no Saudi presence, directory listing sites (Crunchbase, Clutch, GoodFirms…), and service/solution pages are automatically rejected. The filter runs at harvest, resolution, and application time.

---

## Setup & Configuration

### 1. Clone and install

```bash
git clone https://github.com/<your-username>/coop-application-agent.git
cd coop-application-agent
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure your profile

```bash
cp profile.example.json profile.json
cp unknown_questions.example.json unknown_questions.json
cp .env.example .env
```

Edit `profile.json` with your real details: name, email, phone, university, GPA, projects, skills, and document paths. This is the **single source of truth** for all auto-fill data.

### 3. Add your documents

Place your files in `my_docs/`:
- `transcript.pdf` — academic transcript
- `national_id.pdf` — national ID (uploaded only to official portals)
- `iban_letter.pdf` — IBAN letter

Place your base resume at `resume.pdf` in the project root.

### 4. Create `cv_facts.json`

This JSON file holds every verified fact that tailored CVs and cover letters are built from: projects (with bullets and tags), skills, education, contact info, and certifications. See the CV tailor's docstring for the expected schema.

### 5. Set your API key

```bash
# In .env:
ANTHROPIC_API_KEY=sk-ant-...
```

| Variable | Needed for | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | Keyword extraction, CV summaries, outreach drafts | CV tailoring still works (lexicon + template summary) |
| `TG_API_ID` / `TG_API_HASH` | Telegram discovery agent | Telegram agent doesn't run |

---

## CLI Usage

### Discover companies

```bash
# Full pipeline: web search + files + curated list → SQLite DB + Excel
python harvest_400_riyadh_companies.py

# Dry run (no DB writes)
python harvest_400_riyadh_companies.py --dry-run

# Offline mode (files + curated list only)
python harvest_400_riyadh_companies.py --offline
```

### Resolve careers pages and queue applications

```bash
# Resolve top 80 reviewed companies and queue those with application forms
python resolve_careers_and_queue.py

# Re-check already-queued entries for stale URLs
python resolve_careers_and_queue.py --recheck-queued

# Limit concurrency
python resolve_careers_and_queue.py --top 40 --workers 3
```

### Apply to queued companies

```bash
# Fully automatic: fills all known fields, auto-advances wizards, pauses at Submit
python apply_queue.py --no-ask

# Interactive: prompts before each Next step
python apply_queue.py --auto-advance

# Apply to a single company by DB row ID
python apply_queue.py --id 42

# Limit batch size
python apply_queue.py --no-ask --limit 5
```

### Tailor a single CV

```bash
python cv_tailor.py "Company Name" --jd-url https://careers.example.com/job/123
python cv_tailor.py "Company Name" --jd-file job_description.txt
python cv_tailor.py "Company Name" --title "Data Science Co-op"
```

### Fill a single form

```bash
python job_form_filler.py https://apply.workable.com/example/j/ABC123/apply/ resume.pdf --no-ask
```

### Daily pipeline

```bash
# Run the full daily cycle once: discover → score → resolve → apply
python daily_pipeline.py

# Loop mode: runs at the configured time every day
python daily_pipeline.py --loop
```

---

## Project Structure

```
coop-application-agent/
├── config.py                     # All settings, loaded from .env and profile.json
├── profile.example.json          # Template for personal profile (copy to profile.json)
├── unknown_questions.example.json# Template for learned form answers
├── .env.example                  # Template for API keys and flags
├── requirements.txt              # Python dependencies
│
├── harvest_400_riyadh_companies.py  # Company discovery (Google, curated lists, files)
├── google_discovery_agent.py        # Google search discovery
├── linkedin_discovery_agent.py      # LinkedIn discovery
├── startup_discovery.py             # Startup ecosystem scraping
├── telegram_discovery_agent.py      # Telegram channel extraction
├── live_search_companies.py         # Quick live search helper
│
├── resolve_careers_and_queue.py  # Careers page resolution and DB queuing
├── ats_resolver.py               # ATS board API reader (Workable, Greenhouse, Lever…)
├── company_filters.py            # Offshore, directory, and junk filtering
├── cleanup_db.py                 # Database hygiene (dedup, re-fingerprint, offshore purge)
│
├── apply_queue.py                # Batch application runner
├── job_form_filler.py            # Playwright form filler with multi-step wizard support
├── memory_manager.py             # 3-tier answer memory with semantic paraphrase matching
│
├── cv_tailor.py                  # Per-company CV builder (facts-only, quality-gated)
├── cover_letter.py               # Per-company cover letter generator
├── scoring.py                    # Opportunity ranking algorithm
├── outreach.py                   # Cold outreach email draft generator
│
├── app_db.py                     # SQLite database layer
├── seed_opportunities.py         # Manually curated opportunity seeds
├── opportunities_store.py        # CSV/Excel data layer
├── tracker.py                    # Application status tracker
├── llm.py                        # LLM abstraction (Anthropic Claude)
├── daily_pipeline.py             # Orchestrator for the full daily cycle
├── run.py                        # Quick-start entry point
├── check_setup.py                # Environment verification
│
├── docs/                         # Place your resume.pdf and other docs here
│   └── .gitkeep
├── data/                         # Static data files (sector lists, company seeds)
└── my_docs/                      # Personal documents (gitignored)
```

---

## Configuration Reference

All personal data lives in `profile.json` (never committed). The agent loads it at startup and uses it for:
- Form auto-fill (name, email, phone, university, GPA, etc.)
- CV and cover letter generation
- Outreach email drafts

Environment variables in `.env` override specific settings. See `.env.example` for the full list.

### Key config flags

| Flag | Effect |
|---|---|
| `COOP_NO_ASK=1` | Skip all terminal prompts; leave unknown fields blank |
| `COOP_AUTO_ADVANCE=1` | Auto-click Next in multi-step wizards |
| `GPA_ON_CV=False` | Never print GPA on CVs (default) |
| `GPA_FILL_ONLY_IF_REQUIRED=True` | Only fill GPA in forms when the field is mandatory |

---

## License

MIT
