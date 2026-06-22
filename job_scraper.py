#!/usr/bin/env python3
"""
Daily Job Search Assistant
Scrapes multiple job boards, filters UK/EU-remote jobs,
scores by relevance, deduplicates, and writes an HTML email digest.
"""

import json
import os
import hashlib
import html as html_lib
import re
import logging
import time
from datetime import datetime, timezone

import requests

try:
    from langdetect import detect
    LANGDETECT_OK = True
except ImportError:
    LANGDETECT_OK = False

try:
    from deep_translator import GoogleTranslator
    TRANSLATOR_OK = True
except ImportError:
    TRANSLATOR_OK = False

try:
    from jobspy import scrape_jobs
    JOBSPY_OK = True
except ImportError:
    JOBSPY_OK = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

SEEN_FILE = "seen_jobs.json"
EMAIL_FILE = "email_body.html"
GH_OUTPUT = os.environ.get("GITHUB_OUTPUT", "")

# Ordered by priority: highest score first
ROLE_SCORES = {
    "copywriter": 5,
    "digital content creator": 5,
    "english tutor": 5,
    "english teacher": 5,
    "english teaching assistant": 5,
    "teaching assistant": 5,
    "efl teacher": 5,
    "esl teacher": 5,
    "tefl teacher": 5,
    "tesol teacher": 5,
    "content creator": 4,
    "content writer": 4,
    "language tutor": 4,
    "literacy tutor": 4,
    "academic tutor": 4,
    "copy editor": 3,
    "copyeditor": 3,
    "proofreader": 3,
    "content strategist": 3,
    "content specialist": 3,
    "social media writer": 3,
    "blog writer": 3,
    "tutor": 2,
    "teacher": 2,
    "writer": 2,
}

# Terms used for jobspy + API searches
SEARCH_TERMS = [
    "copywriter",
    "digital content creator",
    "teaching assistant",
    "english tutor",
    "english teacher",
    "english teaching assistant",
]

UK_KEYWORDS = [
    "united kingdom", " uk ", "(uk)", "u.k.", "england", "scotland",
    "wales", "northern ireland", "great britain", "britain",
    "london", "manchester", "birmingham", "leeds", "liverpool",
    "edinburgh", "bristol", "sheffield", "glasgow", "leicester",
    "coventry", "cardiff", "nottingham", "newcastle", "cambridge",
    "oxford", "brighton", "reading", "southampton", "portsmouth",
    "derby", "wolverhampton", "norwich", "bath", "chester",
    "hull", "york", "exeter", "swansea", "aberdeen", "dundee",
]

US_ONLY = [
    "us only", "usa only", "united states only", "us-based only",
    "must be based in us", "must be based in the us",
    "must reside in us", "authorized to work in the us",
    "north america only", "us residents only", "us citizens only",
    "must be located in the us", "located in the united states",
    "must be in the us", "us and canada only", "canada and us only",
]

SOURCE_COLORS = {
    "linkedin":  "#0A66C2",
    "indeed":    "#003A9B",
    "glassdoor": "#0CAA41",
    "google":    "#4285F4",
    "remotive":  "#6366F1",
    "the muse":  "#FF5F5F",
    "arbeitnow": "#FF6B35",
    "jobicy":    "#10B981",
}

JOB_TYPE_MAP = {
    "fulltime":   "Full-time",
    "full-time":  "Full-time",
    "parttime":   "Part-time",
    "part-time":  "Part-time",
    "contract":   "Contract",
    "internship": "Internship",
    "temporary":  "Temporary",
    "freelance":  "Freelance",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def fmt_job_type(raw: str) -> str:
    if not raw:
        return "—"
    return JOB_TYPE_MAP.get(raw.lower().replace(" ", ""), raw.title())


def truncate(text: str, n: int = 200) -> str:
    text = (text or "").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def esc(text) -> str:
    return html_lib.escape(str(text) if text else "")


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_job(title: str, description: str) -> int:
    t = (title or "").lower()
    d = (description or "").lower()[:600]
    best = 1
    for role, sc in ROLE_SCORES.items():
        if role in t:
            best = max(best, sc)
    if best < 3:
        for role in ["copywriter", "content creator", "teaching assistant",
                     "english tutor", "english teacher", "tutor", "teacher", "writer"]:
            if role in d:
                best = max(best, 2)
                break
    return min(best, 5)


# ── Location filter ───────────────────────────────────────────────────────────

def is_eligible(location: str, description: str, is_remote: bool) -> bool:
    loc = (location or "").lower()
    body = (description or "").lower()[:1200]

    # Direct UK location match
    for kw in UK_KEYWORDS:
        if kw in f" {loc} ":
            return True
    # Also catch "uk" at start/end of string
    if loc == "uk" or loc.startswith("uk,") or loc.endswith(", uk"):
        return True

    # Remote job — accept unless restricted to US
    if is_remote or "remote" in loc or "worldwide" in loc or "anywhere" in loc:
        combined = loc + " " + body
        for pat in US_ONLY:
            if pat in combined:
                return False
        return True

    return False


# ── Translation ───────────────────────────────────────────────────────────────

def maybe_translate(text: str) -> str:
    if not text or len(text) < 30:
        return text
    if not LANGDETECT_OK or not TRANSLATOR_OK:
        return text
    try:
        lang = detect(text[:300])
        if lang == "en":
            return text
        translated = GoogleTranslator(source="auto", target="en").translate(text[:2000])
        return f"[Translated from {lang.upper()}] {translated}"
    except Exception:
        return text


# ── Deduplication ─────────────────────────────────────────────────────────────

def job_id(title: str, company: str, source: str) -> str:
    raw = f"{title.lower().strip()}|{company.lower().strip()}|{source.lower()}"
    return hashlib.md5(raw.encode()).hexdigest()


def load_seen() -> set:
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE) as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()
    return set()


def save_seen(seen: set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=2)


# ── Scrapers ──────────────────────────────────────────────────────────────────

def scrape_jobspy(term: str) -> list:
    if not JOBSPY_OK:
        return []
    jobs = []
    try:
        df = scrape_jobs(
            site_name=["linkedin", "indeed", "glassdoor", "google"],
            search_term=term,
            location="United Kingdom",
            results_wanted=15,
            hours_old=48,
            country_indeed="UK",
            verbose=0,
        )
        if df is None or df.empty:
            return []
        for _, row in df.iterrows():
            title    = str(row.get("title", "") or "")
            company  = str(row.get("company", "") or "")
            location = str(row.get("location", "") or "")
            desc     = strip_html(str(row.get("description", "") or ""))
            source   = str(row.get("site", "unknown")).lower()
            is_rem   = str(row.get("is_remote", "")).lower() in ("true", "1", "yes")
            jtype    = fmt_job_type(str(row.get("job_type", "") or ""))
            url      = str(row.get("job_url", "") or "")
            posted   = str(row.get("date_posted", "") or "")[:10]

            if not is_eligible(location, desc, is_rem):
                continue
            jobs.append(dict(title=title, company=company, location=location,
                             is_remote=is_rem, job_type=jtype, description=desc[:2000],
                             url=url, source=source, date_posted=posted))
    except Exception as e:
        log.warning(f"jobspy '{term}': {e}")
    return jobs


def scrape_remotive(term: str) -> list:
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs",
                         params={"search": term, "limit": 20}, timeout=15)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            title   = j.get("title", "")
            company = j.get("company_name", "")
            loc     = j.get("candidate_required_location", "Worldwide")
            desc    = strip_html(j.get("description", "") or "")
            url     = j.get("url", "")
            posted  = (j.get("publication_date", "") or "")[:10]
            jtype   = fmt_job_type(j.get("job_type", "") or "")
            if not is_eligible(loc, desc, True):
                continue
            jobs.append(dict(title=title, company=company, location=loc or "Remote",
                             is_remote=True, job_type=jtype, description=desc[:2000],
                             url=url, source="remotive", date_posted=posted))
    except Exception as e:
        log.warning(f"Remotive '{term}': {e}")
    return jobs


def scrape_the_muse() -> list:
    """The Muse doesn't take search terms — fetch Content + Education and post-filter."""
    jobs = []
    for category in ["Content", "Education"]:
        try:
            r = requests.get("https://www.themuse.com/api/public/jobs",
                             params={"category": category, "page": 0}, timeout=15)
            r.raise_for_status()
            for j in r.json().get("results", []):
                title   = j.get("name", "")
                company = (j.get("company") or {}).get("name", "")
                locs    = j.get("locations") or [{}]
                loc     = locs[0].get("name", "Flexible") if locs else "Flexible"
                levels  = [lv.get("name", "") for lv in (j.get("levels") or [])]
                jtype   = ", ".join(filter(None, levels)) or "—"
                desc    = strip_html(j.get("contents", "") or "")
                is_rem  = any(w in loc.lower() for w in ("flexible", "remote"))
                url     = (j.get("refs") or {}).get("landing_page", "")
                posted  = (j.get("publication_date", "") or "")[:10]
                if not is_eligible(loc, desc, is_rem):
                    continue
                if score_job(title, desc) < 2:
                    continue
                jobs.append(dict(title=title, company=company, location=loc,
                                 is_remote=is_rem, job_type=jtype, description=desc[:2000],
                                 url=url, source="the muse", date_posted=posted))
        except Exception as e:
            log.warning(f"The Muse '{category}': {e}")
        time.sleep(1)
    return jobs


def scrape_arbeitnow() -> list:
    jobs = []
    try:
        r = requests.get("https://www.arbeitnow.com/api/job-board-api", timeout=15)
        r.raise_for_status()
        for j in r.json().get("data", []):
            title   = j.get("title", "")
            if score_job(title, "") < 2:
                continue
            company = j.get("company_name", "")
            loc     = j.get("location", "")
            is_rem  = bool(j.get("remote", False))
            desc    = strip_html(j.get("description", "") or "")
            tags    = j.get("job_types") or []
            jtype   = fmt_job_type(tags[0] if tags else "")
            url     = j.get("url", "")
            posted  = (j.get("created_at", "") or "")[:10]
            if not is_eligible(loc, desc, is_rem):
                continue
            jobs.append(dict(title=title, company=company,
                             location=loc or ("Remote" if is_rem else ""),
                             is_remote=is_rem, job_type=jtype, description=desc[:2000],
                             url=url, source="arbeitnow", date_posted=posted))
    except Exception as e:
        log.warning(f"Arbeitnow: {e}")
    return jobs


def scrape_jobicy(term: str) -> list:
    jobs = []
    try:
        r = requests.get("https://jobicy.com/api/v2/remote-jobs",
                         params={"tag": term, "count": 20}, timeout=15)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            title   = j.get("jobTitle", "")
            company = j.get("companyName", "")
            loc     = j.get("jobGeo", "Worldwide")
            desc    = strip_html(j.get("jobDescription", "") or "")
            jtype   = fmt_job_type(j.get("jobType", "") or "")
            url     = j.get("url", "")
            posted  = (j.get("pubDate", "") or "")[:10]
            if not is_eligible(loc, desc, True):
                continue
            jobs.append(dict(title=title, company=company, location=loc or "Remote",
                             is_remote=True, job_type=jtype, description=desc[:2000],
                             url=url, source="jobicy", date_posted=posted))
    except Exception as e:
        log.warning(f"Jobicy '{term}': {e}")
    return jobs


# ── Email HTML ────────────────────────────────────────────────────────────────

def stars_html(n: int) -> str:
    return (
        f'<span style="color:#F59E0B;font-size:15px">{"★"*n}</span>'
        f'<span style="color:#D1D5DB;font-size:15px">{"☆"*(5-n)}</span>'
    )


def badge(source: str) -> str:
    color = SOURCE_COLORS.get(source.lower(), "#6B7280")
    label = source.replace("-", " ").title()
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap">'
        f'{label}</span>'
    )


def loc_display(job: dict) -> str:
    loc = esc(job.get("location", ""))
    if job["is_remote"]:
        sub = loc if loc.lower() not in ("remote", "anywhere", "worldwide", "") else ""
        return f"🌍 Remote{(' · ' + sub) if sub else ''}"
    return f"📍 {loc}" if loc else "—"


def build_html(jobs: list, date_str: str) -> str:
    rows = ""
    for job in jobs:
        sc   = job["score"]
        url  = esc(job.get("url", "#"))
        desc = esc(truncate(job.get("description", ""), 200))
        rows += f"""
        <tr style="border-bottom:1px solid #E5E7EB;vertical-align:top">
          <td style="padding:12px 10px;min-width:180px">
            <a href="{url}" style="color:#1D4ED8;font-weight:600;text-decoration:none;
               font-size:14px;line-height:1.4">{esc(job['title'])}</a><br>
            <span style="color:#374151;font-size:13px">{esc(job['company'])}</span><br>
            <div style="margin-top:5px">{badge(job['source'])}</div>
          </td>
          <td style="padding:12px 10px;color:#4B5563;font-size:13px;
              white-space:nowrap">{loc_display(job)}</td>
          <td style="padding:12px 10px;color:#4B5563;font-size:13px;
              white-space:nowrap">{esc(job.get('job_type', '—'))}</td>
          <td style="padding:12px 10px;text-align:center">{stars_html(sc)}</td>
          <td style="padding:12px 10px;color:#6B7280;font-size:13px;
              max-width:320px;line-height:1.5">{desc}</td>
        </tr>"""

    header_row = "".join(
        f'<th style="padding:10px;text-align:{"center" if h=="Match" else "left"};'
        f'color:#374151;font-size:11px;text-transform:uppercase;letter-spacing:.05em">{h}</th>'
        for h in ["Role / Company / Source", "Location", "Type", "Match", "Description"]
    )

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#F3F4F6;margin:0;padding:20px">
  <div style="max-width:960px;margin:0 auto;background:#fff;border-radius:12px;
              box-shadow:0 1px 4px rgba(0,0,0,.12);overflow:hidden">
    <div style="background:linear-gradient(135deg,#1D4ED8 0%,#7C3AED 100%);
                padding:24px 30px">
      <h1 style="color:#fff;margin:0;font-size:22px;font-weight:700">
        Daily Job Digest
      </h1>
      <p style="color:#BFDBFE;margin:6px 0 0;font-size:14px">
        {date_str} &mdash; {len(jobs)} new listing{'s' if len(jobs)!=1 else ''}
      </p>
    </div>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#F9FAFB;border-bottom:2px solid #E5E7EB">
            {header_row}
          </tr>
        </thead>
        <tbody>{rows}
        </tbody>
      </table>
    </div>
    <div style="padding:14px 30px;background:#F9FAFB;border-top:1px solid #E5E7EB;
                font-size:12px;color:#9CA3AF;text-align:center">
      Automated digest &middot; Sources: LinkedIn, Indeed, Glassdoor, Google Jobs,
      Remotive, The Muse, Arbeitnow, Jobicy &middot;
      Disable via GitHub Actions to unsubscribe
    </div>
  </div>
</body>
</html>"""


# ── GitHub Actions output ─────────────────────────────────────────────────────

def set_output(key: str, value: str):
    if GH_OUTPUT:
        with open(GH_OUTPUT, "a") as f:
            f.write(f"{key}={value}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    seen = load_seen()
    log.info(f"Loaded {len(seen)} previously seen job IDs")

    raw: list = []

    # ── jobspy (LinkedIn, Indeed, Glassdoor, Google) ──
    for term in SEARCH_TERMS:
        log.info(f"jobspy: '{term}'")
        raw.extend(scrape_jobspy(term))
        time.sleep(3)

    # ── Remotive ──
    log.info("Remotive...")
    for term in ["copywriter", "content creator", "english teacher", "tutor"]:
        raw.extend(scrape_remotive(term))
        time.sleep(1)

    # ── The Muse ──
    log.info("The Muse...")
    raw.extend(scrape_the_muse())

    # ── Arbeitnow ──
    log.info("Arbeitnow...")
    raw.extend(scrape_arbeitnow())

    # ── Jobicy ──
    log.info("Jobicy...")
    for term in ["copywriter", "content", "teacher", "tutor", "teaching"]:
        raw.extend(scrape_jobicy(term))
        time.sleep(1)

    log.info(f"Total raw fetched: {len(raw)}")

    # ── Deduplicate, score, filter ──
    new_jobs = []
    new_ids: set = set()

    for job in raw:
        jid = job_id(job["title"], job["company"], job["source"])
        if jid in seen or jid in new_ids:
            continue
        sc = score_job(job["title"], job["description"])
        if sc < 2:
            continue
        job["score"] = sc
        job["description"] = maybe_translate(job["description"])
        new_jobs.append(job)
        new_ids.add(jid)

    log.info(f"New relevant jobs after dedup+filter: {len(new_jobs)}")

    # ── Persist seen IDs ──
    seen.update(new_ids)
    save_seen(seen)

    if not new_jobs:
        log.info("Nothing new today — skipping email.")
        set_output("has_jobs", "false")
        return

    # ── Sort: score desc, then date desc ──
    new_jobs.sort(key=lambda j: (j["score"], j.get("date_posted", "")), reverse=True)

    # ── Write HTML ──
    date_str = datetime.now(timezone.utc).strftime("%A, %d %B %Y")
    with open(EMAIL_FILE, "w", encoding="utf-8") as f:
        f.write(build_html(new_jobs, date_str))

    log.info(f"Wrote {EMAIL_FILE} ({len(new_jobs)} jobs)")
    set_output("has_jobs", "true")
    set_output("job_count", str(len(new_jobs)))


if __name__ == "__main__":
    main()
