import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://rnt.turismodeportugal.pt/RNT/Pesquisa_ET.aspx"


def log(message):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


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
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def mask_pii(value):
    text = clean(value)
    text = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "***@***.***", text, flags=re.I)
    text = re.sub(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)", "[REDACTED_PHONE]", text)
    return text


def mask_reviewer_names(value):
    def repl(match):
        parts = match.group(0).split()
        return " ".join(f"{p[0]}***" if len(p) > 1 else p for p in parts[:-1]) + f" {parts[-1][0]}."
    return re.sub(r"\b[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]+\s+[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]+\b", repl, clean(value))


def safe_title(soup):
    return (soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else ""


def canonical_product_id(url):
    parsed = urlparse(url)
    number = parse_qs(parsed.query).get("nr", [""])[0].strip()
    if number:
        return number
    return hashlib.sha256(url.split("#", 1)[0].encode("utf-8")).hexdigest()[:16]


def read_input(path):
    input_df = pd.read_excel(path, header=None).fillna("")
    if input_df.empty:
        return []
    first = [clean(x).lower() for x in input_df.iloc[0].tolist()]
    known = {"keyword", "listing_url", "detail_url"}
    if any(x in known for x in first):
        input_df = input_df.iloc[1:].copy()
        input_df.columns = first + [f"column_{i}" for i in range(len(first), input_df.shape[1])]
    else:
        input_df.columns = ["keyword"] + [f"column_{i}" for i in range(1, input_df.shape[1])]
    rows = []
    for _, row in input_df.iterrows():
        item = {str(k): clean(v) for k, v in row.to_dict().items()}
        if any(item.values()):
            rows.append(item)
    return rows


def value_from_row(row, key):
    value = row.get(key, "")
    return "" if clean(value).lower() in {"nan", "none", "null"} else clean(value)


def detect_anti_bot_signals(status, html):
    markers = (
        "cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...",
        "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold",
        "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada",
        "akamai", "shieldsquare", "security challenge", "pardon our interruption",
        "potential security risk", "detected automated scripts", "additional security check",
        "i am human", "hcaptcha", "access denied", "robot or human"
    )
    lowered = (html or "").lower()
    matches = [marker for marker in markers if marker in lowered]
    if status in {403, 429} or matches:
        return True, matches
    return False, []


async def dismiss_cookie_banner(page):
    return


async def safe_click(page, locator, selector, flags, url):
    try:
        await locator.click()
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url, "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
        try:
            await page.keyboard.press("Escape")
            await locator.click(timeout=3000)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception as fallback_exc:
                log(f"Click fallback failed on '{selector}': {fallback_exc}")
                return False


def parse_listing_row(row, base_url):
    link = row.select_one('a[href*="RNET.aspx?nr="]')
    if not link:
        return None
    cells = [clean(c.get_text("\n", strip=True)) for c in row.find_all("td")]
    href = urljoin(base_url, link.get("href", ""))
    data = {"registration_no": canonical_product_id(href), "listing_cells": " | ".join(cells)}
    names = ["registration_no", "registered_date", "name", "type", "capacity", "location", "certifications"]
    for index, name in enumerate(names):
        if index < len(cells):
            data[name] = cells[index]
    return data | {"detail_url": href}


def parse_detail(html, url, mask_reviews=True):
    soup = BeautifulSoup(html, "html.parser")
    result = {}
    for table in soup.select("table.ShowRecord, table.ShowRecord.OSInline"):
        if any("review" in " ".join(table.get("class", [])).lower() for _ in [0]):
            continue
        for row in table.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = clean(cells[0].get_text("\n", strip=True)).rstrip(":")
            value = clean(" ".join(clean(c.get_text("\n", strip=True)) for c in cells[1:]))
            if not label or not value or "review" in label.lower() or "rating" in label.lower():
                continue
            lower = label.lower()
            if any(x in lower for x in ("contact", "telefone", "telemóvel", "email", "proprietário", "responsável")):
                value = "[REDACTED]"
            elif mask_reviews:
                value = mask_pii(value)
            result[label] = value
    result["detail_typology"] = result.get("Tipologia", "")
    result["name_detail"] = result.get("Nome", "")
    result["location_detail"] = result.get("Localização", "")
    result["commercial_brand"] = result.get("Marca comercial", "")
    result["capacity_detail"] = result.get("Capacidade", "")
    result["product_id"] = canonical_product_id(url)
    result["product_url"] = url
    return result


async def goto_and_check(page, url, flags):
    response = await page.goto(url, wait_until="domcontentloaded")
    await dismiss_cookie_banner(page)
    html = await page.content()
    status = response.status if response else 0
    blocked, matches = detect_anti_bot_signals(status, html)
    if blocked:
        flags.append({"Type": "Blocked by bot protection", "URL": url, "Message": f"HTTP {status}; markers: {', '.join(matches)}"})
        log(f"Blocked by bot protection at {url}; no retries will be attempted.")
        return False, status, html
    return True, status, html


async def scrape_detail(context, url, source_listing_url, mask_reviews, records, flags):
    page = await context.new_page()
    try:
        ok, status, html = await goto_and_check(page, url, flags)
        if not ok:
            return False
        data = parse_detail(html, url, mask_reviews)
        data["source_listing_url"] = source_listing_url
        data["listing_url"] = source_listing_url
        records.append(data)
        for field in ("product_id", "product_url", "name_detail", "detail_typology"):
            if not data.get(field):
                flags.append({"Type": "Missing field", "URL": url, "Message": field})
        return True
    finally:
        await page.close()


async def scrape_listing(context, listing_url, keyword, args, records, flags, counters):
    page = await context.new_page()
    try:
        ok, status, html = await goto_and_check(page, listing_url, flags)
        if not ok:
            return False
        if keyword:
            selector = "#RichWidgets_wt104_block_wtMainContent_wtInput_FiltroTexto"
            input_box = page.locator(selector)
            await input_box.fill("")
            await input_box.fill(keyword)
            button = page.locator("#RichWidgets_wt104_block_wtMainContent_wt201")
            if await button.count() == 0:
                button = page.locator("button:has-text('Pesquisar')")
            await safe_click(page, button, selector, flags, listing_url)
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            try:
                await page.wait_for_selector('table tr:has(a[href*="RNET.aspx?nr="])', timeout=15000)
            except PlaywrightTimeoutError:
                pass
        seen_pages = set()
        while True:
            soup = BeautifulSoup(await page.content(), "html.parser")
            rows = []
            for tr in soup.select('table tr:has(a[href*="RNET.aspx?nr="])'):
                parsed = parse_listing_row(tr, listing_url)
                if parsed:
                    rows.append(parsed)
            if not rows:
                if keyword:
                    log(f"No records found for keyword: {keyword}")
                await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
                title = safe_title(soup)
                log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
                return True
            counters["links_discovered"] += len(rows)
            snapshot = "|".join(x["detail_url"] for x in rows)
            if snapshot in seen_pages:
                break
            seen_pages.add(snapshot)
            for item in rows:
                if args.max_products is not None and len(records) >= args.max_products:
                    return True
                item["source_listing_url"] = listing_url
                item["product_url"] = item["detail_url"]
                records.append(item)
                counters["links_processed"] += 1
                await scrape_detail(context, item["detail_url"], listing_url, args.mask_reviews, records, flags)
            next_link = page.locator(".ListNavigation_Next")
            if await next_link.count() == 0 or not await next_link.first.is_visible():
                break
            cls = (await next_link.first.get_attribute("class") or "").lower()
            disabled = (await next_link.first.get_attribute("aria-disabled") or "").lower() == "true"
            if disabled or "disabled" in cls:
                break
            old_snapshot = snapshot
            if not await safe_click(page, next_link.first, ".ListNavigation_Next", flags, listing_url):
                break
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass
            try:
                await page.wait_for_function("old => document.body.innerText.includes(old) === false", old_snapshot[:30], timeout=10000)
            except PlaywrightTimeoutError:
                pass
        return True
    finally:
        await page.close()


async def run(args, input_rows):
    records, flags = [], []
    counters = {"categories_discovered": 0, "links_discovered": 0, "links_processed": 0}
    anti_bot = False
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", viewport={"width": 1920, "height": 1080})
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        try:
            for row in input_rows:
                if args.max_products is not None and len(records) >= args.max_products:
                    break
                detail_url = value_from_row(row, "detail_url")
                listing_url = value_from_row(row, "listing_url") or TARGET_URL
                keyword = value_from_row(row, "keyword")
                if detail_url:
                    counters["links_discovered"] += 1
                    await scrape_detail(context, detail_url, "DIRECT_INPUT", args.mask_reviews, records, flags)
                else:
                    counters["categories_discovered"] += 1
                    await scrape_listing(context, listing_url, keyword, args, records, flags, counters)
        finally:
            await browser.close()
    counters["records_extracted"] = len(records)
    log(f"Crawl Funnel: {counters['categories_discovered']} categories, {counters['links_discovered']} links discovered, {counters['links_processed']} processed, {counters['records_extracted']} extracted.")
    return records, flags


def export_results(path, records, flags):
    safe_records = [{k: sanitize_cell_value(v) for k, v in record.items()} for record in records]
    safe_flags = [{k: sanitize_cell_value(v) for k, v in flag.items()} for flag in flags]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(safe_records).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(safe_flags or [{"Type": "Info", "URL": "", "Message": "No flags"}]).to_excel(writer, sheet_name="Flags", index=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    rows = read_input(args.input)
    records, flags = asyncio.run(run(args, rows))
    export_results(args.output, records, flags)
    if not records:
        log("ERROR: Zero product records were extracted. Aborting.")
        sys.exit(1)
    log(f"Wrote {len(records)} records to {args.output}")


if __name__ == "__main__":
    main()