"""
seed_opportunities.py — the opportunities verified during research (Nov 2026), loaded into applications.db
so the bot has something to apply to on the very first run. Safe to run repeatedly: existing rows are
merged, never duplicated, and statuses you've already changed are left alone.

  queued           → direct application links the bot fills (Nafath/SMS pages are skipped automatically)
  manual_required  → careers pages, e-mail applications or links that need you
"""

from datetime import date

from app_db import connect, set_status, upsert

TODAY = date.today().isoformat()

QUEUED = [
    dict(company="Mobily", title="COOP Training Program (Term 2, 1448H) - Digital & AI",
         apply_url="https://careers.mobily.com.sa/en/sites/opportunities/job/2113",
         deadline="2026-11-29", location="Riyadh, Saudi Arabia",
         notes="Final-semester students; GPA >= 3.25/5; fluent English; university co-op letter"),
    dict(company="Visa", title="COOP Trainee (second-semester COOP, starting January 2027)",
         apply_url="https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/job/SA---Riyadh-Saudi-Arabia/"
                   "COOP-Trainee--For-current-students---second-semester-COOP--starting-in-January-2027-_REF086773W/apply",
         source_url="https://visa.wd5.myworkdayjobs.com/Visa_Early_Careers/job/SA---Riyadh-Saudi-Arabia/"
                    "COOP-Trainee--For-current-students---second-semester-COOP--starting-in-January-2027-_REF086773W",
         location="Riyadh, Saudi Arabia", notes="Workday: create an account with your e-mail"),
    dict(company="National Center for Government Resource Systems", title="Cooperative Training - Technology",
         apply_url="https://career-sa20.hr.cloud.sap/sfcareer/jobreqcareer?jobId=1462&company=national15",
         location="Riyadh, Saudi Arabia", notes="CS / IT / cybersecurity / information systems"),
    dict(company="Schneider Electric", title="COOP Training - Riyadh",
         apply_url="https://spr.ly/6047BHYYOF", location="Riyadh, Saudi Arabia"),
    dict(company="Al Rajhi Bank", title="COOP Training",
         apply_url="https://careers.alrajhibank.com.sa/en/saudi-arabia/jobs/"
                   "coop-training-%D8%A7%D9%84%D8%AA%D8%AF%D8%B1%D9%8A%D8%A8-%D8%A7%D9%84%D8%AA%D8%B9%D8%A7%D9%88%D9%86%D9%8A-1100037814/",
         location="Riyadh, Saudi Arabia", notes="Posting date not confirmed; the page shows whether it is still open"),
    dict(company="Lumi", title="Cooperative Training",
         apply_url="https://docs.google.com/forms/d/e/1FAIpQLSe5DFrcrwuTeTIpZmqUYIHNo_z5fYuBxFgksbzd0jp9aUj3QA/viewform",
         location="Riyadh, Saudi Arabia", notes="Saudi nationality; university letter; any major"),
]

QUEUED += [
    dict(company="Zain KSA", title="CO-OP Program", apply_url="https://sa.zain.com/about/join-programs/",
         source_url="https://sa.zain.com/about/co-op-program/", location="Riyadh, Saudi Arabia",
         notes="Web form, no Nafath. Choose Program Type = CO-OP; GPA field is required (filled from config); CV + university letter"),
    dict(company="Bain & Company", title="Mustaqbalak Consulting COOP (January 2027 start)",
         apply_url="https://careers.bain.com/recruits/signin?folderId=106107",
         source_url="https://www.bain.com/careers/work-with-us/internships-programs/coop/", location="Riyadh, Saudi Arabia",
         deadline="2027-02-07", notes="All majors, no GPA minimum, paid; needs an account, CV, transcript and a cover letter"),
    dict(company="Lucidya", title="Entry-Level Software Engineer (Internship for graduating co-op students) - AI",
         apply_url="https://apply.workable.com/lucidya/j/972E263571/apply",
         source_url="https://apply.workable.com/lucidya/jobs/view/972E263571", location="Riyadh, Saudi Arabia",
         notes="Posted 6 May 2026; open to students who need the internship to graduate"),
    dict(company="SAP", title="Student Internship Riyadh", location="Riyadh, Saudi Arabia",
         apply_url="https://jobs.sap.com/job/Riyadh-Student-Internship-Riyadh-11435/1290191201",
         notes="Posted 5 Mar 2026; Digital City, ~7 km from home; may need an SAP careers account"),
    dict(company="Strategic Gears", title="University Internship (Co-op)", location="Riyadh, Saudi Arabia",
         apply_url="https://jobs.lever.co/strategicgears/2e4ddb22-05bc-43d8-b81c-61d42ebfc819/apply",
         source_url="https://jobs.lever.co/strategicgears/2e4ddb22-05bc-43d8-b81c-61d42ebfc819",
         notes="6-7 months per university requirement; on-site"),
]

MANUAL = [
    dict(company="Waqd AI", title="Co-op request (cold email)", apply_type="email", apply_url="mailto:info@waqd.sa",
         notes="Nearby (Al Aqiq). Draft: outreach_nearby/Waqd_AI.eml"),
    dict(company="DataSec", title="Co-op request (cold email + talent network)", apply_type="email",
         apply_url="mailto:info@datasec.sa", notes="~2 km (At Taawun). Draft: outreach_nearby/DataSec.eml"),
    dict(company="Quant Data & Analytics", title="Co-op request (careers portal + LinkedIn)", apply_type="careers_page",
         apply_url="https://careers.quant.sa/jobs/Careers", notes="Al Sahafa ~6 km"),
    dict(company="Professional Matrix AI", title="Co-op request (WhatsApp / walk-in)", apply_type="careers_page",
         apply_url="", notes="Al Wadi ~1 km; WhatsApp link in outreach_plan.md"),
    dict(company="AIP - Artificial Intelligence Place", title="Co-op request (WhatsApp / walk-in)",
         apply_type="careers_page", apply_url="", notes="Al Nada ~2 km; WhatsApp link in outreach_plan.md"),
    dict(company="SITE", title="Co-op request (LinkedIn)", apply_type="careers_page", apply_url="",
         notes="Digital City ~7 km; LinkedIn note in outreach_plan.md"),
    dict(company="Foodics", title="Co-op request (LinkedIn)", apply_type="careers_page", apply_url="",
         notes="Al Aqiq ~8 km; LinkedIn note in outreach_plan.md"),
    dict(company="HUMAIN", title="Technical Co-op (AI focus)", apply_type="careers_page",
         apply_url="", notes="Find 'Technical Co-op' on HUMAIN's careers page, then queue its link"),
    dict(company="SEEF", title="Cooperative Training - Information Systems & Technology", apply_type="careers_page",
         apply_url="https://fa-esiu-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs",
         notes="Job list: open the co-op posting, then queue its link"),
    dict(company="Tamkeen Technologies", title="Career & COOP", apply_type="careers_page",
         apply_url="https://tamkeentech.sa/en/careers", notes="From LEAP 2025 list"),
    dict(company="IBM", title="Career & COOP", apply_type="careers_page",
         apply_url="https://www.ibm.com/careers", notes="Search 'Riyadh intern / co-op'"),
    dict(company="NITC", title="Career & COOP", apply_type="careers_page", apply_url="https://nitc.sa/joinUs"),
    dict(company="PwC Middle East", title="Career & COOP", apply_type="careers_page",
         apply_url="https://www.pwc.com/m1/en/careers.html"),
    dict(company="KPMG Saudi Arabia", title="COOP & Internship Programs", apply_type="careers_page",
         apply_url="https://kpmg.com/sa/en/home/careers/coop-and-internship-programs.html"),
    dict(company="KAPSARC", title="Internship Program", apply_type="careers_page",
         apply_url="https://www.kapsarc.org/careers/internship-program/"),
    dict(company="Osool", title="COOP (by e-mail)", apply_type="email", apply_url="mailto:coop@osoolre.com",
         notes="Send CV + approval-request letter by e-mail"),
    dict(company="SDAIA", title="Cooperative Training (Term 2)", apply_type="careers_page",
         apply_url="https://athkax.sdaia.gov.sa/coop", notes="Window opened 18 Oct for one week; likely closed"),
]


def seed() -> dict:
    stats = {"queued": 0, "manual_required": 0, "already_tracked": 0}
    with connect() as con:
        for item, target in [(q, "queued") for q in QUEUED] + [(m, "manual_required") for m in MANUAL]:
            row = {"source": "seed", "job_id": f"seed:{item['company']}:{item['title']}",
                   "apply_type": item.get("apply_type", "external"), "posted_date": TODAY,
                   "location": item.get("location", "Saudi Arabia"), "deadline": item.get("deadline", ""),
                   "source_url": item.get("source_url", ""),
                   **{k: item[k] for k in ("company", "title", "apply_url")}}
            opp_id, created = upsert(con, row)
            status = con.execute("SELECT status FROM opportunities WHERE id=?", (opp_id,)).fetchone()["status"]
            if created or status == "new":
                set_status(con, opp_id, target, item.get("notes", "seeded"))
                stats[target] += 1
            else:
                stats["already_tracked"] += 1
    return stats


if __name__ == "__main__":
    print(seed())
