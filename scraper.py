#!/usr/bin/env python3
"""
India Internship Tracker — Scraper
===================================
Reads companies.json and fetches active internship listings from each company's
career page or ATS API.  Outputs internships.json.

Dependencies: requests, beautifulsoup4, lxml
"""

import json
import os
import re
import sys
import time
import html
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COMPANIES_FILE = os.path.join(SCRIPT_DIR, "companies.json")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "internships.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
HTML_HEADERS = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}

RATE_LIMIT_DELAY = 0.5          # seconds between company fetches
RETRY_DELAY = 3                 # seconds before retry on 429/503
MAX_RETRIES = 2                 # max retries per request
REQUEST_TIMEOUT = 15            # seconds
STALE_DAYS = 7                  # remove entries older than this

INTERN_KEYWORDS = [
    "intern", "internship", "trainee", "apprentice",
    "co-op", "coop", "graduate program", "fresher",
]

INDIA_LOCATION_KEYWORDS = [
    "india", "bangalore", "bengaluru", "mumbai", "hyderabad",
    "delhi", "pune", "chennai", "gurgaon", "gurugram", "noida",
    "kolkata", "remote", "anywhere", "in",
]

TECH_ROLE_KEYWORDS = [
    "software", "engineer", "engineering", "developer", "development", "sde", "swe",
    "data", "analytics", "analyst", "science", "scientist", "machine learning", "ml", "ai",
    "artificial intelligence", "deep learning", "nlp", "computer vision",
    "backend", "frontend", "front-end", "back-end", "full stack", "fullstack",
    "devops", "devsecops", "platform", "infrastructure", "infra", "cloud", "sre",
    "security", "cybersecurity", "appsec", "penetration", "blockchain",
    "mobile", "android", "ios", "flutter", "react native",
    "product", "ui", "ux", "design", "figma",
    "quant", "quantitative", "algo", "algorithmic", "hft", "trading systems",
    "research", "robotics", "embedded", "firmware", "hardware", "vlsi", "chip",
    "network", "systems", "kernel", "compiler", "database", "distributed",
    "test", "qa", "quality assurance", "automation", "sdet",
    "technical", "tech", "it ", "information technology",
    "program manager", "tpm", "technical program",
]

# Pre-compile patterns for performance
_INTERN_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in INTERN_KEYWORDS), re.IGNORECASE
)
_INDIA_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(kw) for kw in INDIA_LOCATION_KEYWORDS) + r")\b",
    re.IGNORECASE,
)
_TECH_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in TECH_ROLE_KEYWORDS), re.IGNORECASE
)

# Tags to strip when extracting plain text from HTML descriptions
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Current UTC time in ISO-8601."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_html(raw: Optional[str]) -> str:
    """Strip HTML tags and collapse whitespace."""
    if not raw:
        return ""
    text = _HTML_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _snippet(text: Optional[str], length: int = 300) -> str:
    """Return first `length` characters of plain text."""
    clean = _strip_html(text or "")
    return clean[:length]


def _is_intern(title: str) -> bool:
    """Check if a job title matches intern keywords."""
    return bool(_INTERN_PATTERN.search(title))


def _is_tech_role(title: str) -> bool:
    """Check if a job title matches tech-role keywords."""
    return bool(_TECH_PATTERN.search(title))


def _is_india_location(location: str, company: Dict[str, Any]) -> bool:
    """
    Check if a location string indicates India.
    For "remote"/"anywhere" matches, only include if company is tier 1 or 2.
    """
    if not location:
        return False
    loc_lower = location.lower()

    # Direct India keywords (non-ambiguous)
    non_remote_keywords = [
        "india", "bangalore", "bengaluru", "mumbai", "hyderabad",
        "delhi", "pune", "chennai", "gurgaon", "gurugram", "noida", "kolkata",
    ]
    for kw in non_remote_keywords:
        if kw in loc_lower:
            return True

    # "IN" as a standalone word (country code)
    if re.search(r"\bIN\b", location):
        return True

    # "remote"/"anywhere" — only for India-based companies (tier 1 or 2)
    tier = company.get("tier", 3)
    if tier in (1, 2):
        if "remote" in loc_lower or "anywhere" in loc_lower:
            return True

    return False


def _safe_get(url: str, *, json_response: bool = True,
              headers: Optional[Dict] = None) -> Optional[Any]:
    """
    GET with retries on 429/503.  Returns parsed JSON or Response object.
    Returns None on unrecoverable failure.
    """
    hdrs = headers or (HEADERS if json_response else HTML_HEADERS)
    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.get(url, headers=hdrs, timeout=REQUEST_TIMEOUT)
            if resp.status_code in (429, 503):
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
                return None
            resp.raise_for_status()
            if json_response:
                return resp.json()
            return resp
        except requests.RequestException:
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
            return None
    return None


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Best-effort parse of a date string to ISO-8601 UTC."""
    if not raw:
        return None
    # Already ISO-ish
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw[:30], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            continue
    # Epoch milliseconds (Lever uses this)
    try:
        ts = int(raw)
        if ts > 1e12:
            ts = ts / 1000
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        pass
    return None


def _build_entry(
    company: Dict[str, Any],
    ats_job_id: Any,
    role: str,
    location: str,
    apply_url: str,
    posted_at: Optional[str],
    description: Optional[str],
    scraped_at: str,
) -> Dict[str, Any]:
    """Build a normalised internship entry."""
    return {
        "id": f"{company['id']}_{ats_job_id}",
        "company": company["name"],
        "company_id": company["id"],
        "tier": company.get("tier", 0),
        "category": company.get("category", ""),
        "role": role.strip(),
        "location": location.strip() if location else company.get("location_hint", "India"),
        "apply_url": apply_url,
        "scraped_at": scraped_at,
        "posted_at": posted_at or scraped_at,
        "ats_source": company["ats"],
        "description_snippet": _snippet(description),
    }


# ──────────────────────────────────────────────────────────────────────────────
# ATS HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_greenhouse(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """Greenhouse JSON API handler."""
    url = company.get("api_endpoint")
    if not url:
        return []

    data = _safe_get(url)
    if data is None:
        return []

    jobs = data.get("jobs", [])
    results = []
    for job in jobs:
        title = job.get("title", "")
        if not _is_intern(title):
            continue
        loc_name = ""
        location_obj = job.get("location")
        if isinstance(location_obj, dict):
            loc_name = location_obj.get("name", "")
        elif isinstance(location_obj, str):
            loc_name = location_obj
        if not _is_india_location(loc_name, company):
            continue
        results.append(_build_entry(
            company=company,
            ats_job_id=job.get("id", ""),
            role=title,
            location=loc_name,
            apply_url=job.get("absolute_url", ""),
            posted_at=_parse_date(job.get("updated_at")),
            description=job.get("content", ""),
            scraped_at=scraped_at,
        ))
    return results


def _fetch_lever(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """Lever JSON API handler."""
    url = company.get("api_endpoint")
    if not url:
        return []

    data = _safe_get(url)
    if data is None or not isinstance(data, list):
        return []

    results = []
    for job in data:
        title = job.get("text", "")
        if not _is_intern(title):
            continue
        categories = job.get("categories", {}) or {}
        loc = categories.get("location", "")
        if not _is_india_location(loc, company):
            continue
        posted_raw = job.get("createdAt")
        posted_at = None
        if posted_raw is not None:
            # Lever createdAt is epoch ms
            posted_at = _parse_date(str(posted_raw))
        results.append(_build_entry(
            company=company,
            ats_job_id=job.get("id", ""),
            role=title,
            location=loc,
            apply_url=job.get("hostedUrl", ""),
            posted_at=posted_at,
            description=job.get("descriptionPlain", ""),
            scraped_at=scraped_at,
        ))
    return results


def _fetch_smartrecruiters(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """SmartRecruiters API handler."""
    slug = company.get("board_slug")
    url = company.get("api_endpoint")
    if not url and slug:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
    if not url:
        return []

    data = _safe_get(url)
    if data is None:
        return []

    postings = data.get("content", [])
    results = []
    for job in postings:
        name = job.get("name", "")
        if not _is_intern(name):
            continue
        loc_obj = job.get("location", {}) or {}
        country = loc_obj.get("country", "")
        city = loc_obj.get("city", "")
        loc_str = f"{city}, {country}".strip(", ")
        if country.upper() == "IN" or _is_india_location(loc_str, company):
            pass  # ok
        else:
            continue
        # Direct apply URL: prefer ref, then construct from slug + id
        apply_url = job.get("ref") or ""
        if not apply_url:
            posting_id = job.get("id", "")
            if slug and posting_id:
                apply_url = f"https://jobs.smartrecruiters.com/{slug}/{posting_id}"
        results.append(_build_entry(
            company=company,
            ats_job_id=job.get("id", ""),
            role=name,
            location=loc_str if loc_str else company.get("location_hint", "India"),
            apply_url=apply_url,
            posted_at=_parse_date(job.get("releasedDate")),
            description=job.get("name", ""),  # SR listings rarely have inline description
            scraped_at=scraped_at,
        ))
    return results


def _fetch_ashby(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """Ashby job board API handler."""
    slug = company.get("board_slug")
    url = company.get("api_endpoint")
    if not url and slug:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    if not url:
        return []

    data = _safe_get(url)
    if data is None:
        return []

    postings = data.get("jobPostings", [])
    if not postings and isinstance(data, dict):
        # Some Ashby boards nest under 'jobs'
        postings = data.get("jobs", [])

    results = []
    for job in postings:
        title = job.get("title", "")
        if not _is_intern(title):
            continue
        loc = job.get("location", "")
        if isinstance(loc, dict):
            loc = loc.get("name", "")
        if not _is_india_location(loc, company):
            continue
        # Direct apply URL: prefer applyUrl, then construct from slug + id
        apply_url = job.get("applyUrl") or job.get("hostedUrl") or ""
        if not apply_url:
            posting_id = job.get("id", "")
            if slug and posting_id:
                apply_url = f"https://jobs.ashbyhq.com/{slug}/{posting_id}"
        results.append(_build_entry(
            company=company,
            ats_job_id=job.get("id", ""),
            role=title,
            location=loc,
            apply_url=apply_url,
            posted_at=_parse_date(job.get("publishedDate")),
            description=job.get("descriptionHtml", job.get("descriptionPlain", "")),
            scraped_at=scraped_at,
        ))
    return results


def _fetch_workday(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """
    Workday career pages (best-effort HTML scraping).
    Most Workday sites are fully JS-rendered, so this is a graceful fallback.
    """
    url = company.get("career_url")
    if not url:
        return []

    resp = _safe_get(url, json_response=False, headers=HTML_HEADERS)
    if resp is None:
        return []

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    # Common Workday selectors — many variants exist
    job_items = soup.select(
        '[data-automation-id="jobTitle"], '
        '.css-19uc56f, '               # modern Workday
        'a[data-automation-id="jobTitle"], '
        'li.css-1q2dra3, '
        'div.job-listing, '
        'div.jobTitle, '
        'a.job-link, '
        'div[role="listitem"]'
    )

    if not job_items:
        # Fallback: look for ANY links with intern keywords
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            if _is_intern(text):
                href = link["href"]
                if not href.startswith("http"):
                    href = requests.compat.urljoin(url, href)
                results.append(_build_entry(
                    company=company,
                    ats_job_id=abs(hash(href)) % (10**10),
                    role=text,
                    location=company.get("location_hint", "India"),
                    apply_url=href,
                    posted_at=None,
                    description=text,
                    scraped_at=scraped_at,
                ))
        if not results:
            print(f"  ⚠  Workday page likely JS-rendered for {company['name']}, skipping")
        return results

    for item in job_items:
        title_text = item.get_text(strip=True)
        if not _is_intern(title_text):
            continue
        href = ""
        if item.name == "a" and item.get("href"):
            href = item["href"]
        else:
            a_tag = item.find("a", href=True)
            if a_tag:
                href = a_tag["href"]
        if href and not href.startswith("http"):
            href = requests.compat.urljoin(url, href)
        if not href:
            continue  # skip listings without a direct apply link
        results.append(_build_entry(
            company=company,
            ats_job_id=abs(hash(href)) % (10**10),
            role=title_text,
            location=company.get("location_hint", "India"),
            apply_url=href,
            posted_at=None,
            description=title_text,
            scraped_at=scraped_at,
        ))
    return results


def _fetch_custom(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """
    Custom/generic career page scraping (best-effort).
    Uses BeautifulSoup to find links containing intern keywords.
    """
    url = company.get("career_url")
    if not url:
        return []

    resp = _safe_get(url, json_response=False, headers=HTML_HEADERS)
    if resp is None:
        return []

    body = resp.text.strip()
    if len(body) < 200:
        print(f"  ⚠  Custom page for {company['name']} returned near-empty body (JS-rendered?), skipping")
        return []

    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        soup = BeautifulSoup(body, "html.parser")

    results = []
    seen_hrefs = set()

    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True)
        if not text:
            continue
        if not _is_intern(text):
            continue
        href = link["href"]
        if not href.startswith("http"):
            href = requests.compat.urljoin(url, href)
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)
        results.append(_build_entry(
            company=company,
            ats_job_id=abs(hash(href)) % (10**10),
            role=text,
            location=company.get("location_hint", "India"),
            apply_url=href,
            posted_at=None,
            description=text,
            scraped_at=scraped_at,
        ))

    # Also scan elements (not just <a>) that might contain intern keywords
    if not results:
        for elem in soup.find_all(True):
            if elem.name in ("script", "style", "meta", "link", "head"):
                continue
            text = elem.get_text(strip=True)
            if len(text) > 300:
                continue
            if _is_intern(text):
                # Try to find a parent or child link
                parent_a = elem.find_parent("a")
                child_a = elem.find("a", href=True)
                href = ""
                if parent_a and parent_a.get("href"):
                    href = parent_a["href"]
                elif child_a and child_a.get("href"):
                    href = child_a["href"]
                if href and not href.startswith("http"):
                    href = requests.compat.urljoin(url, href)
                if not href:
                    continue  # skip listings without a direct apply link
                if href in seen_hrefs:
                    continue
                seen_hrefs.add(href)
                results.append(_build_entry(
                    company=company,
                    ats_job_id=abs(hash(href + text)) % (10**10),
                    role=text[:200],
                    location=company.get("location_hint", "India"),
                    apply_url=href,
                    posted_at=None,
                    description=text[:300],
                    scraped_at=scraped_at,
                ))
    return results


def _fetch_naukri_jobs(company: Dict[str, Any], scraped_at: str) -> List[Dict]:
    """Naukri requires authentication; skip gracefully."""
    print(f"  ⚠  Naukri requires authentication, skipping {company['name']}")
    return []


# ATS dispatch table
ATS_HANDLERS = {
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "smartrecruiters": _fetch_smartrecruiters,
    "ashby": _fetch_ashby,
    "workday": _fetch_workday,
    "custom": _fetch_custom,
    "naukri_jobs": _fetch_naukri_jobs,
}


# ──────────────────────────────────────────────────────────────────────────────
# MERGE / DEDUP / CLEANUP
# ──────────────────────────────────────────────────────────────────────────────

def _load_existing(path: str) -> List[Dict]:
    """Load existing internships.json, returning empty list if missing/corrupt."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []


def _remove_stale(entries: List[Dict], cutoff: datetime) -> List[Dict]:
    """Remove entries where scraped_at is older than `cutoff`."""
    kept = []
    for e in entries:
        scraped = e.get("scraped_at", "")
        try:
            dt = datetime.strptime(scraped, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                kept.append(e)
        except (ValueError, TypeError):
            kept.append(e)   # keep if unparseable (be conservative)
    return kept


def _deduplicate(entries: List[Dict]) -> List[Dict]:
    """Deduplicate by apply_url, keeping the most recently scraped."""
    by_url: Dict[str, Dict] = {}
    for e in entries:
        url = e.get("apply_url", "")
        if url in by_url:
            existing_ts = by_url[url].get("scraped_at", "")
            new_ts = e.get("scraped_at", "")
            if new_ts >= existing_ts:
                by_url[url] = e
        else:
            by_url[url] = e
    return list(by_url.values())


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    # ── Load companies ──
    if not os.path.exists(COMPANIES_FILE):
        print(f"ERROR: {COMPANIES_FILE} not found. Exiting.")
        sys.exit(1)

    with open(COMPANIES_FILE, "r", encoding="utf-8") as f:
        companies: List[Dict] = json.load(f)

    print(f"Loaded {len(companies)} companies from companies.json\n")

    scraped_at = _now_iso()
    new_entries: List[Dict] = []
    companies_with_results = set()

    for idx, company in enumerate(companies):
        name = company.get("name", "Unknown")
        ats = company.get("ats", "custom")
        handler = ATS_HANDLERS.get(ats, _fetch_custom)

        print(f"[{idx + 1}/{len(companies)}] Fetching {name} ({ats})...", end=" ", flush=True)

        try:
            results = handler(company, scraped_at)
            # Apply tech-role filter
            filtered = []
            for entry in results:
                if _is_tech_role(entry["role"]):
                    filtered.append(entry)
                else:
                    print(f"  SKIPPED (non-tech): {entry['role']} at {name}")
            n = len(filtered)
            print(f"found {n} internship{'s' if n != 1 else ''}")
            if n > 0:
                new_entries.extend(filtered)
                companies_with_results.add(company["id"])
        except Exception as exc:
            print(f"\nERROR {name}: {exc} — skipping")

        # Rate limit between companies
        if idx < len(companies) - 1:
            time.sleep(RATE_LIMIT_DELAY)

    # ── Merge with existing data ──
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    existing = _load_existing(OUTPUT_FILE)
    existing_fresh = _remove_stale(existing, cutoff)

    # Combine: existing (fresh) + new
    merged = existing_fresh + new_entries
    final = _deduplicate(merged)

    # Sort by tier asc, then company name
    final.sort(key=lambda e: (e.get("tier", 99), e.get("company", "")))

    # ── Write output ──
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(final, f, indent=2, ensure_ascii=False)

    # ── Summary ──
    unique_companies = {e.get("company_id") for e in final}
    print()
    print("=" * 60)
    print(f"=== SCRAPE COMPLETE === Total: {len(final)} active internships "
          f"across {len(unique_companies)} companies. Written to internships.json")
    print("=" * 60)


if __name__ == "__main__":
    main()
