import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://www.ikea.com/in/en/cat/home-office-desks-and-tables-20651/"
API_URL = "https://sik.search.blue.cdtapps.com/in/en/search?c=listaf&v=20250507"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"


def log(message):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def value_from_row(row, key):
    return clean(row.get(key, ""))


def get_proxy():
    return None


def solve_captcha(*args, **kwargs):
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def detect_anti_bot_signals(response_or_page, source_url=""):
    flags = []
    if hasattr(response_or_page, "status_code"):
        status = response_or_page.status_code
        body = response_or_page.text or ""
        title = ""
        if status in (403, 429):
            flags.append({"type": "anti_bot", "message": f"HTTP status {status}", "source_url": source_url})
    else:
        status = None
        body = ""
        title = ""
    haystack = f"{title} {body}".lower()
    markers = ("captcha", "cloudflare", "perimeterx", "datadome")
    for marker in markers:
        if marker in haystack:
            flags.append({"type": "anti_bot", "message": f"Detected marker: {marker}", "source_url": source_url})
    if body and len(body.strip()) < 500:
        flags.append({"type": "low_confidence", "message": "Unusually short HTTP response body", "source_url": source_url})
    return flags


def http_session():
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    proxy = get_proxy()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def extract_json_ld(soup):
    values = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
            values.append(data)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return values


def find_model_code(url, text=""):
    match = re.search(r"(?:modelCode|sku)=([^&#]+)", url, re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b\d{3}\.\d{3}\.\d{2}\b", text)
    return match.group(0) if match else ""


def clean_price(text):
    text = clean(text).replace("\xa0", " ")
    match = re.search(r"(?:Rs\.?|INR)\s*([\d,]+)", text, re.I)
    if not match:
        return ""
    digits = re.sub(r"[^\d]", "", match.group(1))
    return f"INR {digits}" if digits else ""


def dimensions_from_text(text):
    match = re.search(r"\b\d{2,4}\s*[x×]\s*\d{2,4}(?:\s*[x×]\s*\d{1,4})?\s*cm(?:\s*\([^)]*\))?", text, re.I)
    return clean(match.group(0)) if match else ""


def static_parse(url, session, flags):
    try:
        response = session.get(url, timeout=30)
        flags.extend(detect_anti_bot_signals(response, url))
        soup = BeautifulSoup(response.text, "html.parser")
        body = soup.get_text("\n", strip=True)
        heading = soup.find("h1")
        title = clean(heading.get_text(" ", strip=True) if heading else "")
        price = clean_price(body)
        return {"body": body, "title": title, "price": price, "json_ld": extract_json_ld(soup)}
    except requests.RequestException as exc:
        flags.append({"type": "error", "message": f"HTTP request failed: {exc}", "source_url": url})
        return {}


def recursive_product_nodes(value):
    found = []
    if isinstance(value, dict):
        if any(k in value for k in ("productName", "name", "price", "url")):
            found.append(value)
        for child in value.values():
            found.extend(recursive_product_nodes(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(recursive_product_nodes(child))
    return found


def try_search_api(keyword, flags):
    if not keyword:
        return []
    session = http_session()
    payload = {"query": keyword, "page": 1}
    try:
        response = session.post(API_URL, json=payload, timeout=30)
        flags.extend(detect_anti_bot_signals(response, API_URL))
        if response.ok:
            data = response.json()
            return recursive_product_nodes(data)
        flags.append({"type": "low_confidence", "message": "Observed search API did not accept the assumed payload", "source_url": API_URL})
    except (requests.RequestException, ValueError) as exc:
        flags.append({"type": "low_confidence", "message": f"Search API unavailable or payload uncertain: {exc}", "source_url": API_URL})
    return []


async def dismiss_cookie_banner(page):
    try:
        for text in ("Accept all", "Accept", "I agree", "Allow all", "Agree", "Got it"):
            locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I)).first
            if await locator.count() and await locator.is_visible():
                await locator.click(timeout=1000)
                break
    except Exception:
        pass


async def page_antibot_flags(page, url):
    flags = []
    try:
        title = await page.title()
        body = await page.locator("body").inner_text(timeout=3000)
        haystack = f"{title} {body}".lower()
        if len(body.strip()) < 500:
            flags.append({"type": "low_confidence", "message": "Unusually short rendered page body", "source_url": url})
        for marker in ("captcha", "cloudflare", "perimeterx", "datadome"):
            if marker in haystack:
                flags.append({"type": "anti_bot", "message": f"Detected marker: {marker}", "source_url": url})
    except Exception as exc:
        flags.append({"type": "error", "message": f"Could not inspect page for anti-bot signals: {exc}", "source_url": url})
    return flags


async def goto(page, url, flags):
    log(f"Opening {url}")
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if response and response.status in (403, 429):
            flags.append({"type": "anti_bot", "message": f"HTTP status {response.status}", "source_url": url})
        await dismiss_cookie_banner(page)
        await page.wait_for_timeout(1200)
        flags.extend(await page_antibot_flags(page, url))
        return True
    except Exception as exc:
        flags.append({"type": "error", "message": f"Navigation failed: {exc}", "source_url": url})
        return False


async def detail_record(page, url, flags):
    if not await goto(page, url, flags):
        return None
    try:
        await page.wait_for_selector("h1", timeout=15000)
    except PlaywrightTimeoutError:
        flags.append({"type": "low_confidence", "message": "Detail h1 was not rendered", "source_url": url})
    body = await page.locator("body").inner_text()
    h1 = await page.locator("h1").first.inner_text() if await page.locator("h1").count() else ""
    price = clean_price(body)
    dimensions = dimensions_from_text(body)
    name = clean(h1)
    subtitle = ""
    if name:
        parts = [clean(x) for x in re.split(r"\s+[–-]\s+", name) if clean(x)]
        if len(parts) > 1:
            name, subtitle = parts[0], parts[-1]
    colors = []
    try:
        color_section = page.get_by_text(re.compile(r"Choose colour", re.I)).first
        if await color_section.count():
            section_text = await color_section.locator("xpath=..\n").inner_text(timeout=2000)
            colors = sorted(set(re.findall(r"\b(?:white|black-brown|black|brown|beige|grey|blue|green|yellow|red)\b", section_text, re.I)))
    except Exception:
        pass
    stock = "Availability requires postal code/store check; no concrete stock result observed"
    if re.search(r"out of stock|not available", body, re.I):
        stock = "Out of stock / not available"
    record = {"Product Name": name, "Subtitle / Category": subtitle, "Price (INR)": price, "Dimensions": dimensions, "Color Options": ", ".join(colors), "In Stock Status": stock, "Product URL": url, "Source URL": url}
    if not name or not price:
        flags.append({"type": "low_confidence", "message": "Missing product name or price", "source_url": url})
    return record


async def listing_records(page, url, keyword, max_products, flags):
    if not await goto(page, url, flags):
        return []
    try:
        await page.wait_for_selector('a[href*="/in/en/p/"]', timeout=15000)
    except PlaywrightTimeoutError:
        flags.append({"type": "low_confidence", "message": "Product cards were not rendered", "source_url": url})
    anchors = page.locator('a[href*="/in/en/p/"]')
    count = await anchors.count()
    records = []
    seen = set()
    for index in range(count):
        anchor = anchors.nth(index)
        href = await anchor.get_attribute("href")
        if not href:
            continue
        product_url = urljoin(url, href)
        if product_url in seen:
            continue
        text = clean(await anchor.inner_text())
        if keyword and keyword.lower() not in text.lower():
            continue
        seen.add(product_url)
        record = await detail_record(page, product_url, flags)
        if record:
            records.append(record)
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await dismiss_cookie_banner(page)
        if max_products and len(records) >= max_products:
            break
    return records


async def run_async(input_df, args, flags):
    records = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(user_agent=USER_AGENT, locale="en-IN")
        page = await context.new_page()
        for _, row in input_df.iterrows():
            keyword = value_from_row(row, "keyword")
            detail_url = value_from_row(row, "detail_url")
            listing_url = value_from_row(row, "listing_url") or TARGET_URL
            if detail_url:
                record = await detail_record(page, detail_url, flags)
                if record:
                    records.append(record)
            else:
                api_nodes = try_search_api(keyword, flags)
                if api_nodes:
                    flags.append({"type": "low_confidence", "message": "API response found, but exact schema/payload was not captured; DOM extraction used for authoritative fields", "source_url": API_URL})
                records.extend(await listing_records(page, listing_url, keyword, args.max_products, flags))
            if args.max_products and len(records) >= args.max_products:
                records = records[:args.max_products]
                break
        await browser.close()
    return records


def main():
    parser = argparse.ArgumentParser(description="IKEA India desk scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    log("Reading input workbook")
    input_df = pd.read_excel(args.input).fillna("")
    flags = []
    records = asyncio.run(run_async(input_df, args, flags))
    columns = ["Product Name", "Subtitle / Category", "Price (INR)", "Dimensions", "Color Options", "In Stock Status", "Product URL", "Source URL"]
    extracted = pd.DataFrame(records, columns=columns)
    flag_columns = ["type", "message", "source_url"]
    flag_df = pd.DataFrame(flags, columns=flag_columns)
    log(f"Writing {len(extracted)} records and {len(flag_df)} flags")
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        extracted.to_excel(writer, sheet_name="Extracted Data", index=False)
        flag_df.to_excel(writer, sheet_name="Flags", index=False)
    log("Finished")


if __name__ == "__main__":
    main()