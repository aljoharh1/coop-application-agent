"""
cover_letter.py — a short, truthful cover letter per company, built only from cv_facts.json and the tailored CV.

    make_cover_letter("Acme Corp", "Data Science COOP", cv_report) →
        {"pdf": "tailored_cvs/Jane_Doe_Cover_Letter_Acme_Corp.pdf",
         "txt": ".../...txt", "text": "Dear Hiring Team …", "ok": True, "problems": []}

The letter uses the same projects the tailored CV chose (cv_report["projects"]), their first verified bullet,
the co-op dates from config.py and your contact details. It never mentions the GPA and adds no new claims.
The .txt version is pasted into "Cover letter" text boxes; the PDF is uploaded to "Cover letter" file inputs.
"""

import html
import re
from datetime import datetime
from pathlib import Path

from config import COOP_END, COOP_START, CV_OUTPUT_DIR

COVER_CSS = """
@page { size: A4; margin: 22mm 22mm 20mm 22mm; }
body { font-family: Calibri, Carlito, Arial, Helvetica, sans-serif; font-size: 11pt; line-height: 1.45; color: #111;
       font-variant-ligatures: none; }
.head { margin-bottom: 14pt; } .name { font-size: 16pt; font-weight: bold; }
.contact { font-size: 9.5pt; color: #333; } p { margin: 0 0 9pt 0; } ul { margin: 0 0 9pt 16pt; padding: 0; }
li { margin-bottom: 4pt; }
"""


def _date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d")
    return f"{d.day} {d.strftime('%B %Y')}"


def _sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _role_phrase(role: str) -> str:
    r = re.sub(r"\s+", " ", role or "").strip()
    if not r or r.lower().startswith("co-op inquiry"):
        return "a Cooperative Training (COOP) placement"
    return f"the {r} position" if not re.search(r"\b(co-?op|intern|training|program)", r, re.I) else f"the {r}"


def letter_text(company: str, role: str, facts: dict, project_ids: list[str]) -> str:
    projects = {p["id"]: p for p in facts.get("projects", [])}
    chosen = [projects[i] for i in project_ids if i in projects][:2] or list(projects.values())[:2]
    edu = facts.get("education", {})
    school = (edu.get("school") or "Example University").split(",")[0]
    lines = [
        f"Dear Hiring Team at {company},",
        "",
        f"I am applying for {_role_phrase(role)} at {company}. I am a final-year Artificial Intelligence student at "
        f"{school} in Riyadh, and my degree requires a full-time, in-person Cooperative Training placement of "
        f"110 working days, from {_date(COOP_START)} to {_date(COOP_END)}.",
        "",
        "Recent work most relevant to your team:",
    ]
    for p in chosen:
        title = p["title"].split(":")[0].strip()
        bullet = (p.get("bullets") or [""])[0]
        lines.append(f"- {title} ({p.get('stack', '').split(',')[0].strip() or 'Python'}): {_sentence(bullet)}")
    lines += [
        "",
        "I enjoy turning messy data and loosely defined problems into working, measurable systems, and I would bring "
        "that hands-on approach to your team for the full placement period. My CV and academic transcript are "
        "attached, and I would welcome the chance to discuss how I can contribute.",
        "",
        "Thank you for your time and consideration.",
        "",
        "Sincerely,",
        facts.get("name", "Jane Doe"),
    ]
    return "\n".join(lines)


def _problems(text: str, facts: dict) -> list[str]:
    probs = []
    if re.search(r"\bc?gpa\b|grade point|المعدل", text, re.I):
        probs.append("mentions GPA")
    if len(text.split()) > 380:
        probs.append("longer than one page")
    # every number in the letter must come from the facts or the co-op dates
    allowed = re.sub(r"\s+", " ", str(facts)) + f" {COOP_START} {COOP_END} 110 {_date(COOP_START)} {_date(COOP_END)}"
    for n in set(re.findall(r"\d+(?:\.\d+)?", text)):
        if n not in allowed:
            probs.append(f"number {n} not in cv_facts.json")
    return probs


def make_cover_letter(company: str, role: str = "", cv_report: dict | None = None, out_dir: str = CV_OUTPUT_DIR,
                      render_pdf: bool = True) -> dict:
    from cv_tailor import load_facts, slug, to_pdf                     # same facts and renderer as the CV
    facts = load_facts()
    text = letter_text(company, role, facts, (cv_report or {}).get("projects", []))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cname = facts.get("name", "Candidate").replace(" ", "_")
    base = out / f"{cname}_Cover_Letter_{slug(company)}"
    txt = base.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    c = facts.get("contact", {})
    contact = " | ".join(x for x in (c.get("location"), c.get("phone"), c.get("email"), c.get("linkedin")) if x)
    body = []
    in_list = False
    for line in text.split("\n")[2:]:
        if line.startswith("- "):
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{html.escape(line[2:])}</li>")
            continue
        if in_list:
            body.append("</ul>")
            in_list = False
        if line.strip():
            body.append(f"<p>{html.escape(line)}</p>")
    page = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Cover Letter</title>"
            f"<style>{COVER_CSS}</style></head><body><div class='head'><div class='name'>{html.escape(facts['name'])}</div>"
            f"<div class='contact'>{html.escape(contact)}</div></div><p>{datetime.now():%d %B %Y}</p>"
            f"<p>{html.escape(text.split(chr(10))[0])}</p>{''.join(body)}</body></html>")
    pdf = base.with_suffix(".pdf")
    problems = _problems(text, facts)
    if render_pdf:
        try:
            to_pdf(page, pdf)
        except Exception as e:
            problems.append(f"PDF not rendered ({type(e).__name__})")
    return {"pdf": str(pdf) if pdf.exists() else "", "txt": str(txt), "text": text, "ok": not problems,
            "problems": problems}


if __name__ == "__main__":
    import sys
    r = make_cover_letter(sys.argv[1] if len(sys.argv) > 1 else "Example Company",
                          sys.argv[2] if len(sys.argv) > 2 else "")
    print(r["text"], "\n\n", {k: v for k, v in r.items() if k != "text"})
