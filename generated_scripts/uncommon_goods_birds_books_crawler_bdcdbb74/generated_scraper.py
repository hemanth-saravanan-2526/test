import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

TARGET_URL = "https://www.uncommongoods.com/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
WAF_MARKERS = (
    "cloudflare turnstile",
    "just a moment...",
    "datadome",
    "perimeterx",
    "verify you are human",
)
PRICE_RE = re.compile(r"(?i)(?:[\$£€₹]|USD|CAD|EUR|GBP)\s*[\d]{1,5}(?:[,.][\d]{2})?")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PERSON_RE = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+)\b")


def log(message):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}", flush=True)


def sanitize_cell_value(val, max_len=32000):
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val)
    if len(s) > max_len:
        return s[:max_len - 35] + "... [TRUNCATED_EXCEL_LIMIT]"
    return s


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in {"", "nan", "none", "null"} else text


def text_of(node):
    return node.get_text("\n", strip=True) if node else ""


def first_nonempty(*values):
    for value in values:
        value = clean(value)
        if value:
            return value
    return ""


def detect_anti_bot_signals(status, html):
    content = (html or "").lower()
    if status in {403, 429} or any(marker in content for marker in WAF_MARKERS):
        return "Blocked by bot protection"
    return ""


def mask_review_text(text):
    text = EMAIL_RE.sub("***@***.***", text or "")
    return PERSON_RE.sub(lambda m: f"{m.group(1)[0]}*** {m.group(2)[0]}.", text)


def json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from json_objects(item)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from json_objects(child)


def product_jsonld(soup):
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for obj in json_objects(data):
            kind = obj.get("@type", "")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(str(item).lower() == "product" for item in kinds):
                return obj
    return {}


def canonical_url(soup, fallback):
    node = soup.select_one("link[rel='canonical'][href]")
    return urljoin(fallback, node.get("href")) if node else fallback.split("#")[0]


def official_id(data):
    for key in ("sku", "productID", "mpn"):
        value = data.get(key, "") if isinstance(data, dict) else ""
        if not isinstance(value, (dict, list)) and clean(value):
            return clean(value)
    return ""


def slug_id(url):
    match = re.search(r"/product/([^/?#]+)", urlparse(url).path, re.I)
    if match:
        return match.group(1)
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def price_from_json(data):
    offers = data.get("offers", {}) if isinstance(data, dict) else {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return ""
    amount = clean(offers.get("price"))
    currency = clean(offers.get("priceCurrency"))
    if not amount or not currency:
        return ""
    try:
        amount = f"{float(amount):.2f}"
    except (TypeError, ValueError):
        pass
    symbols = {"USD": "$", "CAD": "CAD ", "EUR": "€", "GBP": "£", "INR": "₹"}
    return symbols.get(currency.upper(), currency.upper() + " ") + amount


def price_from_buy_box(soup):
    for node in soup.select("p.item-price.price, [itemprop='price']"):
        classes = " ".join(node.get("class", []))
        if re.search(r"(?i)recommend|related|carousel|reward", classes):
            continue
        match = PRICE_RE.search(text_of(node).replace("\xa0", " "))
        if match:
            return re.sub(r"\s+", " ", match.group(0)).strip()
    return ""


def extract_record(html, page_url, source_url, mask_reviews=True):
    soup = BeautifulSoup(html, "html.parser")
    data = product_jsonld(soup)
    title = first_nonempty(data.get("name"), text_of(soup.select_one("h1")))
    if not title and not data:
        return None
    product_url = canonical_url(soup, page_url)
    brand = data.get("brand", "")
    if isinstance(brand, dict):
        brand = brand.get("name", "")
    category = data.get("category", "")
    if not category:
        category = text_of(soup.select_one("[itemprop='category']"))
    description = first_nonempty(
        data.get("description"), text_of(soup.select_one("[itemprop='description']"))
    )
    details = []
    properties = data.get("additionalProperty", [])
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties:
        if isinstance(prop, dict):
            name, value = clean(prop.get("name")), clean(prop.get("value"))
            if name and value:
                details.append(f"{name}: {value}")
    for row in soup.select("table tr, .details tr, .specifications tr, [class*='dimension' i]"):
        value = text_of(row)
        if value and not re.search(r"(?i)review|rating", value):
            details.append(value)
    review_nodes = soup.select("[class*='review' i], [id*='review' i]")
    review_text = " ".join(text_of(node) for node in review_nodes)
    if mask_reviews and review_text:
        review_text = mask_review_text(review_text)
    if review_text and not details:
        details.append(review_text)
    return {
        "product_id": "",
        "_official_id": official_id(data),
        "_slug_id": slug_id(product_url),
        "product_url": product_url,
        "source_listing_url": source_url,
        "title": title,
        "price": price_from_json(data) or price_from_buy_box(soup),
        "brand": clean(brand),
        "category": clean(category),
        "description": description,
        "details_and_dimensions": "\n".join(dict.fromkeys(details)),
    }


async def dismiss_cookie_banner(page):
    selectors = [
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
        "button:has-text('Got it')",
        "[aria-label*='accept' i]",
        "[id*='cookie' i] button",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=400):
                await locator.click(timeout=1500)
                return
        except Exception:
            continue


async def safe_click(page, locator, selector, url, flags):
    try:
        await locator.click(timeout=2500)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({
            "Type": "Click Intercepted",
            "URL": url,
            "Message": f"Element {selector} blocked: {str(exc)[:200]}",
        })
        try:
            await page.keyboard.press("Escape")
            await locator.click(timeout=1500, force=True)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception as fallback_exc:
                log(f"Click fallback failed on locator '{selector}': {fallback_exc}")
                return False


async def goto(page, url):
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await dismiss_cookie_banner(page)
    await page.wait_for_timeout(1200)
    return response.status if response else 0


async def wait_for_listing(page):
    try:
        await page.wait_for_selector(
            "ul.product-grid, app-item-widget.grid-card, article.item-widget, article.item-widget[id^='pid-']",
            timeout=15000,
        )
    except Exception:
        await page.wait_for_timeout(2500)


async def listing_links(page, keyword):
    await wait_for_listing(page)
    cards = page.locator("article.item-widget, app-item-widget.grid-card")
    count = await cards.count()
    result = []
    needle = clean(keyword).casefold()
    for index in range(count):
        card = cards.nth(index)
        try:
            name = text_of(BeautifulSoup(await card.inner_html(), "html.parser").select_one(".product__name"))
            if needle and needle not in name.casefold():
                continue
            link = card.locator("a.a-secondary[href^='/product/'], .product__name a[href^='/product/']").first
            href = await link.get_attribute("href")
            if href:
                result.append(urljoin(page.url, href).split("#")[0])
        except Exception:
            continue
    return list(dict.fromkeys(result))


async def browser_run(jobs, mask_reviews, headless, flags, metrics):
    records = []
    status = 0
    diagnostic_page = None
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        diagnostic_page = page
        blocked = False
        try:
            for url, source, keyword, direct in jobs:
                search_url = url
                if keyword and not direct:
                    search_url = f"{TARGET_URL.rstrip('/')}/search?q={quote(keyword)}"
                try:
                    status = await goto(page, search_url)
                    html = await page.content()
                    signal = detect_anti_bot_signals(status, html)
                    if signal:
                        flags.append({"Type": signal, "URL": search_url, "Message": signal})
                        log(signal)
                        blocked = True
                        break
                    if direct:
                        links = [search_url]
                    else:
                        links = await listing_links(page, keyword)
                        metrics["links_discovered"].update(links)
                    for detail_url in links:
                        if len(records) >= metrics["max_products"]:
                            break
                        status = await goto(page, detail_url)
                        metrics["links_processed"].add(detail_url)
                        html = await page.content()
                        signal = detect_anti_bot_signals(status, html)
                        if signal:
                            flags.append({"Type": signal, "URL": detail_url, "Message": signal})
                            log(signal)
                            blocked = True
                            break
                        record = extract_record(html, page.url, source, mask_reviews)
                        if record:
                            records.append(record)
                    if blocked:
                        break
                except Exception as exc:
                    flags.append({"Type": "Error", "URL": search_url, "Message": str(exc)[:200]})
                    log(f"Browser error for {search_url}: {exc}")
        finally:
            if not records and diagnostic_page:
                try:
                    title = await diagnostic_page.title()
                    await diagnostic_page.screenshot(path="diagnostic_screenshot.png", full_page=True)
                    log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
                except Exception as exc:
                    log(f"Diagnostic screenshot failed: {exc}")
            await browser.close()
    return records, blocked


def add_missing_flags(records, flags):
    fields = ("product_id", "product_url", "title", "price", "brand", "category", "description", "details_and_dimensions")
    for record in records:
        for field in fields:
            if not clean(record.get(field)):
                flags.append({"Type": "Missing Field", "URL": record.get("product_url", ""), "Message": field})


def read_input_workbook(path):
    workbook = pd.ExcelFile(path)
    sheets = {name: pd.read_excel(path, sheet_name=name).fillna("") for name in workbook.sheet_names}
    preferred = sheets.get("Extracted Data")
    if preferred is None:
        preferred = next(iter(sheets.values()), pd.DataFrame())
    return sheets, preferred


def write_workbook(path, sheets, records, flags):
    data_columns = [
        "product_id", "product_url", "source_listing_url", "title", "price",
        "brand", "category", "description", "details_and_dimensions",
    ]
    data_rows = [
        {column: sanitize_cell_value(record.get(column, "")) for column in data_columns}
        for record in records
    ]
    flag_columns = ["Type", "URL", "Message"]
    flag_rows = [
        {column: sanitize_cell_value(item.get(column, "")) for column in flag_columns}
        for item in flags
    ]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(data_rows, columns=data_columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flag_rows, columns=flag_columns).to_excel(writer, sheet_name="Flags", index=False)
        for name, frame in sheets.items():
            if name not in {"Extracted Data", "Flags"}:
                frame.to_excel(writer, sheet_name=name[:31], index=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    log("Reading input workbook")
    sheets, input_df = read_input_workbook(args.input)
    if input_df.empty:
        input_df = pd.DataFrame([{"listing_url": TARGET_URL}])
    limit = args.max_products if args.max_products is not None else 10**9
    jobs = []
    flags = []
    categories = set()
    for _, row in input_df.iterrows():
        keyword = clean(row.get("keyword", ""))
        listing_url = clean(row.get("listing_url", "")) or TARGET_URL
        detail_url = clean(row.get("detail_url", ""))
        if detail_url:
            jobs.append((detail_url, "DIRECT_INPUT", "", True))
            categories.add("DIRECT_INPUT")
        else:
            source = f"{TARGET_URL.rstrip('/')}/search?q={quote(keyword)}" if keyword else listing_url
            jobs.append((listing_url, source, keyword, False))
            categories.add(listing_url)

    metrics = {
        "links_discovered": set(),
        "links_processed": set(),
        "max_products": limit,
    }
    log(f"Crawl Funnel: {len(categories)} categories, 0 links discovered, 0 processed, 0 extracted.")
    records, blocked = asyncio.run(browser_run(jobs, args.mask_reviews, args.headless, flags, metrics))
    deduped = {}
    for record in records:
        deduped[record["product_url"]] = record
    records = list(deduped.values())[:limit]
    metrics["records_extracted"] = len(records)
    log(
        f"Crawl Funnel: {len(categories)} categories, {len(metrics['links_discovered'])} links discovered, "
        f"{len(metrics['links_processed'])} processed, {len(records)} extracted."
    )

    if records:
        use_official = all(clean(record.get("_official_id")) for record in records)
        for record in records:
            record["product_id"] = clean(record.get("_official_id")) if use_official else clean(record.get("_slug_id"))
            if not record["product_id"]:
                flags.append({"Type": "Low Confidence", "URL": record.get("product_url", ""), "Message": "No product identifier found"})
            for key in list(record):
                if key.startswith("_"):
                    del record[key]
        add_missing_flags(records, flags)
        write_workbook(args.output, sheets, records, flags)
        log(f"Wrote {len(records)} records to {args.output}")
        return

    if blocked:
        log("Blocked by bot protection; halting without retry.")
        sys.exit(0)
    log("ERROR: Zero product records were extracted. Aborting.")
    sys.exit(1)


if __name__ == "__main__":
    main()