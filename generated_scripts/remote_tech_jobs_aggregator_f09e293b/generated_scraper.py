import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
import requests
from bs4 import BeautifulSoup

TARGET_URL = "https://remoteok.com/"
API_URL = "https://remoteok.com/api"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
FIELDS = [
    "Job Title",
    "Company Name",
    "Location/Timezone",
    "Salary Range",
    "Tech Tags",
    "Posted Date",
    "Apply URL",
    "Source URL",
]


def log(message):
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{timestamp}] {message}", flush=True)


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def value_from_row(row, column):
    return clean(row.get(column, ""))


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def detect_anti_bot_signals(response=None, page_title="", body=""):
    signals = []
    status = getattr(response, "status_code", None)
    if status in {403, 429}:
        signals.append(f"HTTP anti-bot status: {status}")
    combined = f"{page_title}\n{body}".lower()
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
        "verify you are human": "human-verification marker",
        "checking your browser": "browser-check marker",
    }
    for marker, description in markers.items():
        if marker in combined:
            signals.append(description)
    if body and len(body.strip()) < 500:
        signals.append("Unusually short response body")
    return list(dict.fromkeys(signals))


def parse_json_ld(soup):
    values = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
            if isinstance(value, list):
                values.extend(value)
            else:
                values.append(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return values


def normalize_salary(value):
    text = clean(value)
    if not text:
        return ""
    match = re.search(
        r"(?i)([$€£₹]|USD|EUR|GBP|INR)\s*[\d,]+(?:\.\d+)?(?:\s*[-–]\s*(?:[$€£₹]|USD|EUR|GBP|INR)?\s*[\d,]+(?:\.\d+)?)?",
        text,
    )
    if match:
        return clean(match.group(0))
    match = re.search(r"\b\d[\d,]*(?:\.\d+)?\s*[-–]\s*\d[\d,]*(?:\.\d+)?\b", text)
    return clean(match.group(0)) if match else ""


def format_salary(job):
    currency = clean(job.get("salary_currency") or job.get("currency"))
    low = job.get("salary_min")
    high = job.get("salary_max")
    if low not in (None, "") and high not in (None, ""):
        return clean(f"{currency} {low}-{high}")
    if low not in (None, ""):
        return clean(f"{currency} {low}")
    if high not in (None, ""):
        return clean(f"{currency} {high}")
    return normalize_salary(job.get("salary") or job.get("compensation"))


def api_job_to_item(job, base_url=TARGET_URL):
    if not isinstance(job, dict):
        return None
    slug = clean(job.get("slug"))
    source = clean(job.get("url"))
    if source and not source.startswith("http"):
        source = urljoin(base_url, source)
    if not source and slug:
        source = urljoin(TARGET_URL, f"remote-jobs/{slug}")
    apply_url = clean(job.get("apply_url") or job.get("application_url"))
    if apply_url and not apply_url.startswith("http"):
        apply_url = urljoin(TARGET_URL, apply_url)
    tags = job.get("tags", [])
    if isinstance(tags, str):
        tags = re.split(r"[,|]", tags)
    tags = ", ".join(dict.fromkeys(clean(tag) for tag in tags if clean(tag)))
    posted = clean(job.get("date") or job.get("epoch") or job.get("posted"))
    return {
        "Job Title": clean(job.get("position") or job.get("title")),
        "Company Name": clean(job.get("company") or job.get("company_name")),
        "Location/Timezone": clean(job.get("location") or job.get("locations")),
        "Salary Range": format_salary(job),
        "Tech Tags": tags,
        "Posted Date": posted,
        "Apply URL": apply_url,
        "Source URL": source,
    }


def matches_keyword(item, keyword):
    keyword = clean(keyword).lower()
    if not keyword:
        return True
    haystack = " ".join(clean(item.get(field)) for field in FIELDS).lower()
    return keyword in haystack


def request_page(session, url, flags):
    try:
        response = session.get(url, timeout=35)
        body = response.text or ""
        for signal in detect_anti_bot_signals(response=response, body=body):
            flags.append({"Type": "Anti-bot", "URL": url, "Message": signal})
        response.raise_for_status()
        return body
    except Exception as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)})
        return ""


def fetch_api_jobs(session, keyword, flags):
    try:
        response = session.get(API_URL, timeout=35, headers={"Accept": "application/json"})
        body = response.text or ""
        for signal in detect_anti_bot_signals(response=response, body=body):
            flags.append({"Type": "Anti-bot", "URL": API_URL, "Message": signal})
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            data = data.get("jobs", data.get("data", []))
        if not isinstance(data, list):
            return []
        results = []
        for job in data:
            item = api_job_to_item(job)
            if item and item.get("Job Title") and matches_keyword(item, keyword):
                results.append(item)
        return results
    except Exception as exc:
        flags.append({"Type": "Low confidence", "URL": API_URL, "Message": f"API unavailable: {exc}"})
        return []


def extract_detail_html(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for selector in ["h1", "h1[itemprop='title']", ".job h1", "meta[property='og:title']"]:
        node = soup.select_one(selector)
        if node:
            title = clean(node.get("content") if node.name == "meta" else node.get_text(" ", strip=True))
            if title:
                break
    company = ""
    node = soup.select_one('a[href*="/company/"], [itemprop="hiringOrganization"]')
    if node:
        company = clean(node.get("content") or node.get_text(" ", strip=True))
    location_nodes = soup.select('[class*="location"], [id*="location"], [itemprop="jobLocation"], a[href*="/remote-jobs-in-"]')
    location = "; ".join(dict.fromkeys(clean(n.get_text(" ", strip=True)) for n in location_nodes if clean(n.get_text(" ", strip=True))))
    apply_node = soup.select_one('a[href^="/l/"], a[href*="/apply"], a[rel~="nofollow"]')
    apply_url = urljoin(source_url, apply_node.get("href")) if apply_node and apply_node.get("href") else ""
    tag_nodes = soup.select('a[href*="/remote-"], a[href*="/tags/"], .tags a, [class*="tag"]')
    tags = ", ".join(dict.fromkeys(clean(n.get_text(" ", strip=True)) for n in tag_nodes if clean(n.get_text(" ", strip=True))))
    salary = ""
    for obj in parse_json_ld(soup):
        if isinstance(obj, dict):
            offers = obj.get("offers")
            if isinstance(offers, dict):
                salary = normalize_salary(f"{offers.get('priceCurrency', '')} {offers.get('lowPrice', '')}-{offers.get('highPrice', '')}")
                if not salary:
                    salary = normalize_salary(f"{offers.get('priceCurrency', '')} {offers.get('price', '')}")
            if not company and isinstance(obj.get("hiringOrganization"), dict):
                company = clean(obj["hiringOrganization"].get("name"))
    if not salary:
        for node in soup.select('[class*="salary"], [id*="salary"], [class*="compensation"]'):
            salary = normalize_salary(node.get_text(" ", strip=True))
            if salary:
                break
    body = soup.get_text("\n", strip=True)
    posted_match = re.search(r"\b(?:\d+\s*(?:m|h|d|w|mo|y)|today|yesterday)\b", body, re.I)
    return {
        "Job Title": title,
        "Company Name": company,
        "Location/Timezone": location,
        "Salary Range": salary,
        "Tech Tags": tags,
        "Posted Date": clean(posted_match.group(0)) if posted_match else "",
        "Apply URL": apply_url,
        "Source URL": source_url,
    }


def extract_listing_html(html, listing_url, keyword=""):
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.job, article.job, [data-id^='job-'], .job")
    results = []
    for row in rows:
        title_node = row.select_one('a[href*="/remote-jobs/"], a[itemprop="url"]')
        if not title_node:
            continue
        title = clean(title_node.get_text(" ", strip=True))
        href = title_node.get("href")
        if not title or not href:
            continue
        company_node = row.select_one('a[href*="/company/"], [itemprop="hiringOrganization"]')
        location_nodes = row.select('a[href*="/remote-jobs-in-"], [class*="location"]')
        tag_nodes = row.select('.tags a, [class*="tag"] a, a[href*="/remote-"]')
        row_text = clean(row.get_text(" ", strip=True))
        item = {
            "Job Title": title,
            "Company Name": clean(company_node.get_text(" ", strip=True)) if company_node else "",
            "Location/Timezone": "; ".join(dict.fromkeys(clean(n.get_text(" ", strip=True)) for n in location_nodes if clean(n.get_text(" ", strip=True)))),
            "Salary Range": normalize_salary(row_text),
            "Tech Tags": ", ".join(dict.fromkeys(clean(n.get_text(" ", strip=True)) for n in tag_nodes if clean(n.get_text(" ", strip=True)))),
            "Posted Date": clean((re.search(r"\b(?:\d+\s*(?:m|h|d|w|mo|y)|today|yesterday)\b", row_text, re.I) or [""])[0]),
            "Apply URL": urljoin(listing_url, (row.select_one('a[href^="/l/"], a[href*="/apply"]') or {}).get("href", "")),
            "Source URL": urljoin(listing_url, href),
        }
        if matches_keyword(item, keyword) and not re.search(r"\b(expired|removed)\b", row_text, re.I):
            results.append(item)
    return results


def is_expired(html):
    return bool(re.search(r"\b(expired|no longer accepting applications|job has been removed)\b", BeautifulSoup(html, "html.parser").get_text(" ", strip=True), re.I))


async def dismiss_cookie_banner(page):
    try:
        for text in ["Accept all", "Accept", "I agree", "Allow all", "Agree", "Got it"]:
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
            if await locator.count():
                await locator.click(timeout=1500)
                break
    except Exception:
        pass


async def browser_content(page, url, flags, keyword=""):
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=50000)
        await dismiss_cookie_banner(page)
        try:
            await page.wait_for_selector("tr.job, article.job, h1, body", timeout=12000)
        except Exception:
            pass
        if keyword:
            search = page.locator('input[type="search"], input[name="search"], input[placeholder*="earch" i]').first
            if await search.count():
                await search.fill(keyword)
                await search.press("Enter")
                await page.wait_for_timeout(1800)
        content = await page.content()
        title = await page.title()
        for signal in detect_anti_bot_signals(response=response, page_title=title, body=content):
            flags.append({"Type": "Anti-bot", "URL": url, "Message": signal})
        return content
    except Exception as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)})
        return ""


async def run_browser(urls, args, flags, results):
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        flags.append({"Type": "Error", "URL": TARGET_URL, "Message": "Playwright is not installed"})
        return
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        for url, keyword, direct in urls:
            html = await browser_content(page, url, flags, keyword if not direct else "")
            if not html:
                continue
            item = extract_detail_html(html, url) if direct else None
            if direct:
                if item.get("Job Title") and not is_expired(html):
                    results.append(item)
                continue
            for found in extract_listing_html(html, url, keyword):
                detail_html = await browser_content(page, found["Source URL"], flags)
                detailed = extract_detail_html(detail_html, found["Source URL"]) if detail_html else {}
                found.update({k: v for k, v in detailed.items() if v})
                results.append(found)
                if args.max_products and len(results) >= args.max_products:
                    break
        await browser.close()


async def run(args, input_df, flags):
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    results = []
    browser_urls = []
    for _, row in input_df.iterrows():
        detail_url = value_from_row(row, "detail_url")
        listing_url = value_from_row(row, "listing_url") or TARGET_URL
        keyword = value_from_row(row, "keyword")
        if detail_url:
            log(f"Fetching independent detail page: {detail_url}")
            html = request_page(session, detail_url, flags)
            item = extract_detail_html(html, detail_url) if html else {}
            if item.get("Job Title") and not is_expired(html) and matches_keyword(item, keyword):
                results.append(item)
            elif not item.get("Job Title"):
                browser_urls.append((detail_url, keyword, True))
        else:
            log(f"Fetching Remote OK API for {listing_url}")
            found = fetch_api_jobs(session, keyword, flags)
            if found:
                results.extend(found)
            else:
                html = request_page(session, listing_url, flags)
                found = extract_listing_html(html, listing_url, keyword) if html else []
                if found:
                    results.extend(found)
                else:
                    browser_urls.append((listing_url, keyword, False))
    if browser_urls:
        log("Using Playwright fallback for rendered or unavailable content")
        await run_browser(browser_urls, args, flags, results)
    deduped = []
    seen = set()
    for item in results:
        key = item.get("Source URL") or (item.get("Job Title"), item.get("Company Name"))
        if key and key not in seen:
            seen.add(key)
            deduped.append({field: clean(item.get(field, "")) for field in FIELDS})
    return deduped[:args.max_products] if args.max_products else deduped


def main():
    parser = argparse.ArgumentParser(description="Scrape Remote OK jobs")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    log("Reading input workbook")
    try:
        input_df = pd.read_excel(args.input).fillna("")
    except FileNotFoundError:
        input_df = pd.DataFrame([{"listing_url": TARGET_URL}]).fillna("")
        log("Input workbook not found; using the default target URL")
    flags = []
    data = asyncio.run(run(args, input_df, flags))
    log(f"Writing {len(data)} extracted rows")
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        pd.DataFrame(data, columns=FIELDS).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flags, columns=["Type", "URL", "Message"]).to_excel(writer, sheet_name="Flags", index=False)
    log("Run complete")


if __name__ == "__main__":
    main()