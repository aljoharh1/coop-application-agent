"""
outreach.py — prepares (never sends) outreach emails to companies that have no published internship.

Targets rows of startups.csv where:
  internship_signal != "yes"  AND  has_open_opportunity != "yes"  AND  a contact email or form exists
  AND the company has no outreach draft yet.

Output per company in outreach_drafts/:
  <company>.eml  — opens as an editable, unsent draft in Outlook / Apple Mail (X-Unsent: 1),
                   with resume.pdf attached when it exists
  <company>.md   — same text, for contact forms or copy-paste
and a row in the application DB's `outreach` table (status: draft). Mark it sent yourself with:
    python tracker.py outreach-status <id> sent_manually

Usage:
    python outreach.py --limit 5
    python outreach.py --company "Mozn"
    python outreach.py --lang ar        # en (default) | ar
"""

import argparse
import csv
import os
import re
from email.message import EmailMessage

from app_db import company_fp, connect, now
from config import (CANDIDATE, DEFAULT_PROJECT, MODEL_WRITER, OUTREACH_DIR, PROJECT_BY_CATEGORY, RESUME_PATH,
                    STARTUPS_CSV)

PLACEHOLDER_DATES = "[COOP START DATE – END DATE]"

WRITER_SYSTEM = """You write short, specific cold emails from a Saudi university student asking a company to host
her mandatory cooperative training (co-op) placement.

Hard rules:
- Use ONLY the candidate facts provided. Never invent experience, metrics, grades, awards or claims about
  the company. If you mention the company, keep it to its name and the category given.
- Keep the literal placeholder text exactly as given if dates are missing.
- 120–170 words for the body. Plain text. No emojis. Professional, warm, direct; no clichés.
- Structure: who she is (1 line) → what she is asking for (co-op, duration, in person, Riyadh) →
  the ONE most relevant project in 1–2 sentences → offer: CV (as stated in the facts), links → polite close with her name.
- Subject: under 9 words, mentions co-op.
- Write in {language}."""

DRAFT_TOOL = {
    "name": "save_draft",
    "description": "Save the email draft.",
    "input_schema": {"type": "object", "required": ["subject", "body"],
                     "properties": {"subject": {"type": "string"}, "body": {"type": "string"}}},
}


def coop_dates() -> str:
    c = CANDIDATE["coop"]
    return f"{c['start']} – {c['end']}" if c["start"] and c["end"] else PLACEHOLDER_DATES


def facts_for(category: str) -> str:
    project = PROJECT_BY_CATEGORY.get((category or "").lower(), DEFAULT_PROJECT)
    c = CANDIDATE
    return "\n".join([
        f"Name: {c['name']}", f"Degree: {c['degree']}", f"University: {c['university']}",
        f"Co-op requirement: {c['coop']['duration']}; focus areas: {c['coop']['focus']}",
        f"Co-op dates: {coop_dates()}",
        f"Most relevant project — {project}: {c['projects'][project]}",
        f"Skills: {', '.join(c['skills'])}", f"Certifications: {'; '.join(c['certifications'])}",
        f"Email: {c['email']}", f"LinkedIn: {c['linkedin']}", f"GitHub: {c['github']}",
        "CV: attached to this email" if os.path.isfile(RESUME_PATH)
        else "CV: NOT attached — offer to send it on request; do not say it is attached",
    ])


def template_draft(company: str, category: str) -> tuple[str, str]:
    """Offline fallback (no API key): deterministic, facts-only."""
    project = PROJECT_BY_CATEGORY.get((category or "").lower(), DEFAULT_PROJECT)
    c = CANDIDATE
    body = (f"Dear {company} Hiring Team,\n\n"
            f"I am {c['name']}, a senior Artificial Intelligence student at {c['university']}. "
            f"My degree requires a cooperative training placement of {c['coop']['duration']}, "
            f"scheduled for {coop_dates()}, and I would value the opportunity to complete it with your team.\n\n"
            f"A project that reflects how I could contribute: {project} — {c['projects'][project]}\n\n"
            f"I work mainly in {', '.join(c['skills'][:6])}. "
            f"{'My CV is attached, and my' if os.path.isfile(RESUME_PATH) else 'I would be happy to send my CV; my'}"
            f" work is at {c['github']}.\n\n"
            f"Would your team be open to hosting a co-op trainee? I would be glad to share more or meet at "
            f"your convenience.\n\nBest regards,\n{c['name']}\n{c['email']}\n{c['linkedin']}")
    uni_short = c.get("university", "").split("(")[0].strip().split(",")[0] or "university"
    return f"Co-op training request — AI student, {uni_short}", body


def claude_draft(client, company: str, category: str, lang: str) -> tuple[str, str]:
    language = "Arabic (Modern Standard, formal)" if lang == "ar" else "English"
    resp = client.messages.create(
        model=MODEL_WRITER, max_tokens=900,
        system=WRITER_SYSTEM.format(language=language),
        tools=[DRAFT_TOOL], tool_choice={"type": "tool", "name": "save_draft"},
        messages=[{"role": "user", "content":
                   f"Company: {company}\nCategory: {category or 'technology'}\n\nCandidate facts:\n{facts_for(category)}"}])
    for b in resp.content:
        if b.type == "tool_use":
            return b.input["subject"].strip(), b.input["body"].strip()
    raise RuntimeError("no draft returned")


def safe_name(s: str) -> str:
    return re.sub(r"[^\w\-]+", "_", s, flags=re.UNICODE).strip("_")[:60] or "company"


def write_files(row: dict, subject: str, body: str) -> str:
    os.makedirs(OUTREACH_DIR, exist_ok=True)
    stem = os.path.join(OUTREACH_DIR, safe_name(row["company"]))
    to = row.get("contact_email", "")
    with open(stem + ".md", "w", encoding="utf-8") as f:
        f.write(f"To: {to or '(contact form) ' + row.get('contact_form_url', '')}\nSubject: {subject}\n\n{body}\n")
    if to:
        msg = EmailMessage()
        msg["To"], msg["From"], msg["Subject"] = to, CANDIDATE["email"], subject
        msg["X-Unsent"] = "1"                      # Outlook opens it as a draft you can edit and send
        msg.set_content(body)
        if os.path.isfile(RESUME_PATH):
            with open(RESUME_PATH, "rb") as f:
                msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                                   filename=f"{safe_name(CANDIDATE['name'])}_CV.pdf")
        with open(stem + ".eml", "wb") as f:
            f.write(bytes(msg))
        return stem + ".eml"
    return stem + ".md"


def eligible(rows: list[dict], drafted: set[str], only: str | None) -> list[dict]:
    out = []
    for r in rows:
        if only and only.lower() not in r["company"].lower():
            continue
        if company_fp(r["company"]) in drafted:
            continue
        if r.get("internship_signal") == "yes" or r.get("has_open_opportunity") == "yes":
            continue                               # they publish openings → apply, don't cold-email
        if not (r.get("contact_email") or r.get("contact_form_url")):
            continue
        out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--company")
    ap.add_argument("--lang", choices=["en", "ar"], default="en")
    a = ap.parse_args()

    if not os.path.exists(STARTUPS_CSV):
        raise SystemExit(f"{STARTUPS_CSV} not found — run startup_discovery.py first.")
    with open(STARTUPS_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    with connect() as con:
        drafted = {r[0] for r in con.execute("SELECT company_fp FROM outreach")}
    todo = eligible(rows, drafted, a.company)[:a.limit]
    if not todo:
        print("No eligible companies (need a contact email/form, no published internship, not drafted yet).")
        return

    client = None
    if os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
    else:
        print("ℹ️ ANTHROPIC_API_KEY not set — using the built-in template.")
    if coop_dates() == PLACEHOLDER_DATES:
        print(f"⚠️ CANDIDATE['coop'] dates are empty in config.py — drafts contain {PLACEHOLDER_DATES}; "
              "fill it in before sending.")
    if not os.path.isfile(RESUME_PATH):
        print(f"⚠️ {RESUME_PATH} missing — drafts are saved without the CV attached.")

    for r in todo:
        try:
            subject, body = claude_draft(client, r["company"], r.get("category", ""), a.lang) if client \
                else template_draft(r["company"], r.get("category", ""))
        except Exception as e:
            print(f"⚠️ {r['company']}: {e} — using template")
            subject, body = template_draft(r["company"], r.get("category", ""))
        path = write_files(r, subject, body)
        with connect() as con:
            con.execute("INSERT OR IGNORE INTO outreach(company_fp, company, email, contact_form_url, draft_path,"
                        " status, created_at, updated_at) VALUES (?,?,?,?,?, 'draft', ?, ?)",
                        (company_fp(r["company"]), r["company"], r.get("contact_email", ""),
                         r.get("contact_form_url", ""), path, now(), now()))
        print(f"✉️  {r['company']}: {path}")
    print("\nNothing was sent. Review each draft, edit, and send it yourself.")


if __name__ == "__main__":
    main()
