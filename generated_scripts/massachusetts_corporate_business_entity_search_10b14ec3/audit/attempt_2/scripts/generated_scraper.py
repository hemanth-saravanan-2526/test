import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://corp.sec.state.ma.us/CorpWeb/CorpSearch/CorpSearch.aspx"
BASE_URL = "https://corp.sec.state.ma.us/"
GRID_SELECTOR = "#MainContent_SearchControl_grdSearchResultsEntity"
ROW_SELECTOR = GRID_SELECTOR + " tr.GridRow, " + GRID_SELECTOR + " tr.GridAltRow"
DETAIL_LINK_SELECTOR = GRID_SELECTOR + " a[href*='CorpSummary.aspx']"


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
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in {"", "nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", text)


def value_from_row(row, key):
    return clean(row.get(key, ""))


def canonical_product_id(url):
    parsed = urlparse(url)
    value = parse_qs(parsed.query).get("sysvalue", [""])[0].strip()
    if value:
        return value
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def detect_anti_bot_signals(status, html):
    markers = (
        "cloudflare turnstile", "cf-browser-verification", "ray id:",
        "just a moment...", "datadome", "perimeterx", "px-captcha",
        "distil networks", "press & hold", "incapsula", "_incapsula_resource",
        "imperva", "incident id", "kasada", "akamai", "shieldsquare",
        "security challenge", "pardon our interruption", "potential security risk",
        "detected automated scripts", "additional security check", "i am human",
        "hcaptcha", "access denied", "robot or human"
    )
    text = (html or "").lower()
    return "Blocked by bot protection" if status in {403, 429} or any(x in text for x in markers) else ""


def mask_reviewer_pii(text):
    text = clean(text)
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "***@***.***", text)

    def mask_name(match):
        parts = match.group(0).split()
        return " ".join(f"{part[0]}***" if i == 0 else f"{part[0]}." for i, part in enumerate(parts))

    return re.sub(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", mask_name, text)


def table_text(soup, selector):
    table = soup.select_one(selector)
    return clean(" ".join(table.stripped_strings)) if table else ""


def labeled_value(text, patterns):
    text = clean(text)
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return clean(match.group(1))
    return ""


def principal_office(soup):
    table = soup.select_one("#MainContent_tblPrincipleOffice")
    if not table:
        return ""
    values = []
    labels = {"address", "city", "state", "zip", "country", "principal office", "principal office address"}
    for row in table.select("tr"):
        cells = [clean(" ".join(c.stripped_strings)) for c in row.select("th, td")]
        cells = [x for x in cells if x]
        if len(cells) >= 2 and cells[0].lower().rstrip(":") in labels:
            values.extend(cells[1:])
        elif len(cells) == 1 and not values:
            values.append(cells[0])
    return clean(" ".join(values)) or table_text(soup, "#MainContent_tblPrincipleOffice")


def extract_detail(html, url, source_listing_url, mask_reviews):
    soup = BeautifulSoup(html or "", "html.parser")
    name_text = table_text(soup, "#MainContent_tblEntity")
    type_text = table_text(soup, "#MainContent_tblEntityType")
    id_text = table_text(soup, "#MainContent_tblIDNumber")
    entity_name = labeled_value(name_text, [r"summary\s+for\s*:\s*(.+)$", r"entity\s+name\s*:\s*(.+)$"])
    entity_name = entity_name or name_text
    entity_type = labeled_value(type_text, [r"entity\s+type\s*:\s*(.+)$", r"type\s*:\s*(.+)$"]) or type_text
    id_number = labeled_value(id_text, [r"identification\s+number\s*:\s*(.+)$", r"id\s+number\s*:\s*(.+)$"]) or id_text
    office = principal_office(soup)
    status = table_text(soup, "#MainContent_tblStatus")
    status = labeled_value(status, [r"current\s+status\s*:\s*(.+)$", r"status\s*:\s*(.+)$"]) or status
    if mask_reviews:
        entity_name = mask_reviewer_pii(entity_name)
        entity_type = mask_reviewer_pii(entity_type)
        office = mask_reviewer_pii(office)
    return {
        "product_id": canonical_product_id(url),
        "product_url": url,
        "source_listing_url": source_listing_url or "DIRECT_INPUT",
        "Entity Name": entity_name,
        "ID Number": id_number,
        "Entity Type": entity_type,
        "Principal Office Address": office,
        "Current Status": status,
        "Entity Detail URL": url,
    }


async def dismiss_cookie_banner(page):
    for selector in ("button:has-text('Accept')", "button:has-text('Allow all')", "button:has-text('I agree')", "[id*='cookie' i] button", "[class*='cookie' i] button"):
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1500)
                return
        except Exception:
            continue


async def safe_click(page, locator, selector, flags, url):
    try:
        await locator.click(timeout=7000)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url, "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
        try:
            await page.keyboard.press("Escape")
            await dismiss_cookie_banner(page)
            await locator.click(timeout=2500)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception as fallback_exc:
                log(f"Click fallback failed on locator '{selector}': {fallback_exc}")
                flags.append({"Type": "Error", "URL": url, "Message": str(fallback_exc)[:300]})
                return False


async def goto_checked(page, url, flags):
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)[:300]})
        return None, ""
    await dismiss_cookie_banner(page)
    html = await page.content()
    blocked = detect_anti_bot_signals(response.status if response else 0, html)
    if blocked:
        flags.append({"Type": blocked, "URL": page.url, "Message": blocked})
        log(f"{blocked}: {page.url}")
        return response, blocked
    return response, html


async def get_detail(page, url, source_listing_url, mask_reviews, flags):
    response, html = await goto_checked(page, url, flags)
    if html == "Blocked by bot protection":
        return None, True
    if not html:
        return None, False
    return extract_detail(html, page.url, source_listing_url, mask_reviews), False


async def wait_for_grid_change(page, old_signature):
    try:
        await page.wait_for_load_state("networkidle", timeout=30000)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_selector(GRID_SELECTOR, timeout=30000)
    for _ in range(40):
        current = await page.locator(GRID_SELECTOR).inner_text()
        if current != old_signature:
            return True
        await page.wait_for_timeout(250)
    return False


async def extract_listing(page, listing_url, keyword, limit, mask_reviews, flags):
    response, html = await goto_checked(page, listing_url, flags)
    if html == "Blocked by bot protection":
        return [], True, 0, 0
    if not html:
        return [], False, 0, 0
    try:
        await page.wait_for_selector("#MainContent_rdoByEntityName", timeout=20000)
        radio = page.locator("#MainContent_rdoByEntityName")
        if not await radio.is_checked():
            await radio.check()
        if keyword:
            await page.locator("#MainContent_txtEntityName").fill(keyword)
        await page.locator("#MainContent_ddBeginsWithEntityName").select_option("B")
        submit = page.locator("#MainContent_btnSearch")
        if not await safe_click(page, submit, "#MainContent_btnSearch", flags, page.url):
            return [], False, 0, 0
        await page.wait_for_selector(GRID_SELECTOR, timeout=30000)
    except Exception as exc:
        flags.append({"Type": "Error", "URL": page.url, "Message": f"Search failed: {str(exc)[:300]}"})
        return [], False, 0, 0

    records = []
    links_discovered = 0
    links_processed = 0
    seen = set()
    detail_page = await page.context.new_page()
    try:
        while len(records) < limit:
            grid = page.locator(GRID_SELECTOR)
            signature = await grid.inner_text()
            if signature in seen:
                break
            seen.add(signature)
            rows = page.locator(ROW_SELECTOR)
            page_links = []
            for i in range(await rows.count()):
                row = rows.nth(i)
                link = row.locator("a[href*='CorpSummary.aspx']").first
                if await link.count() == 0:
                    continue
                href = await link.get_attribute("href")
                if not href:
                    continue
                cells = row.locator("td")
                values = [clean(await cells.nth(j).inner_text()) for j in range(await cells.count())]
                page_links.append((urljoin(page.url, href), clean(await link.inner_text()), values))
            links_discovered += len(page_links)
            for detail_url, listing_name, values in page_links:
                if len(records) >= limit:
                    break
                record, halted = await get_detail(detail_page, detail_url, page.url, mask_reviews, flags)
                links_processed += 1
                if halted:
                    return records, True, links_discovered, links_processed
                if record:
                    record["Entity Name"] = record["Entity Name"] or listing_name
                    record["ID Number"] = record["ID Number"] or (values[0] if values else "")
                    record["Principal Office Address"] = record["Principal Office Address"] or (values[-1] if values else "")
                    records.append(record)
            if len(records) >= limit:
                break
            next_link = None
            anchors = page.locator(GRID_SELECTOR + " a")
            for i in range(await anchors.count()):
                anchor = anchors.nth(i)
                href = (await anchor.get_attribute("href") or "").lower()
                text = clean(await anchor.inner_text()).lower()
                if "page$" in href and (text == "next" or text.isdigit()):
                    next_link = anchor
                    break
            if next_link is None or not await safe_click(page, next_link, GRID_SELECTOR + " a", flags, page.url):
                break
            if not await wait_for_grid_change(page, signature):
                break
    finally:
        await detail_page.close()
    return records, False, links_discovered, links_processed


async def run(args, input_df):
    records, flags = [], []
    categories = len(input_df.to_dict("records"))
    links_discovered = links_processed = 0
    status, title, halted = 0, "", False
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", viewport={"width": 1920, "height": 1080})
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = {runtime: {}};")
        page = await context.new_page()
        try:
            for row in input_df.to_dict("records"):
                if args.max_products is not None and len(records) >= args.max_products:
                    break
                detail_url = value_from_row(row, "detail_url")
                listing_url = value_from_row(row, "listing_url") or TARGET_URL
                keyword = value_from_row(row, "keyword")
                remaining = args.max_products - len(records) if args.max_products is not None else 10**9
                if detail_url:
                    detail_page = await context.new_page()
                    try:
                        record, halted = await get_detail(detail_page, urljoin(BASE_URL, detail_url), "DIRECT_INPUT", args.mask_reviews, flags)
                        links_discovered += 1
                        links_processed += 1
                        if record:
                            records.append(record)
                    finally:
                        await detail_page.close()
                else:
                    found, halted, discovered, processed = await extract_listing(page, listing_url, keyword, remaining, args.mask_reviews, flags)
                    records.extend(found)
                    links_discovered += discovered
                    links_processed += processed
                if halted:
                    break
            status = 200
            title = await page.title()
            if not records:
                try:
                    await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
                except Exception as exc:
                    flags.append({"Type": "Error", "URL": page.url, "Message": f"Screenshot failed: {str(exc)[:200]}"})
        finally:
            await browser.close()
    return records, flags, categories, links_discovered, links_processed, halted, status, title


def write_output(records, flags, output_path, source_workbook=None):
    output = Path(output_path)
    source = Path(source_workbook) if source_workbook else None
    if output.exists():
        workbook = load_workbook(output)
    elif source and source.exists():
        workbook = load_workbook(source)
    else:
        workbook = Workbook()
    old_headers = []
    if "Extracted Data" in workbook.sheetnames:
        old_headers = [cell.value for cell in workbook["Extracted Data"][1] if cell.value]
        del workbook["Extracted Data"]
    if "Flags" in workbook.sheetnames:
        del workbook["Flags"]
    data_ws = workbook.create_sheet("Extracted Data", 0)
    flags_ws = workbook.create_sheet("Flags", 1)
    standard = ["product_id", "product_url", "source_listing_url", "Entity Name", "ID Number", "Entity Type", "Principal Office Address", "Current Status", "Entity Detail URL"]
    columns = standard + [x for x in old_headers if x not in standard]
    safe_records = [{key: sanitize_cell_value(value) for key, value in record.items()} for record in records]
    data_ws.append(columns)
    for record in safe_records:
        data_ws.append([sanitize_cell_value(record.get(column, "")) for column in columns])
    flags_ws.append(["Type", "URL", "Message"])
    for flag in flags:
        flags_ws.append([sanitize_cell_value(flag.get(k, "")) for k in ("Type", "URL", "Message")])
    workbook.save(output_path)


def main():
    parser = argparse.ArgumentParser(description="Massachusetts corporations scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    try:
        sheets = pd.read_excel(args.input, sheet_name=None)
        sheets = {k: v.fillna("") for k, v in sheets.items()}
        input_df = sheets.get("Extracted Data") or next(iter(sheets.values()), pd.DataFrame())
    except FileNotFoundError:
        input_df = pd.DataFrame([{"keyword": "mobius"}])
        log(f"Input file {args.input} was not found; using keyword 'mobius'.")
    except Exception as exc:
        log(f"Unable to read input workbook: {exc}")
        sys.exit(1)
    try:
        records, flags, categories, discovered, processed, halted, status, title = asyncio.run(run(args, input_df))
    except Exception as exc:
        log(f"Fatal crawl error: {exc}")
        records, flags, categories, discovered, processed, halted, status, title = [], [{"Type": "Error", "URL": TARGET_URL, "Message": str(exc)[:500]}], len(input_df), 0, 0, False, 0, ""
    log(f"Crawl Funnel: {categories} categories, {discovered} links discovered, {processed} processed, {len(records)} extracted.")
    if halted and any(x.get("Type") == "Blocked by bot protection" for x in flags):
        write_output(records, flags, args.output, args.input)
        sys.exit(1)
    if not records:
        safe_title = clean(title)
        flags.append({"Type": "Error", "URL": TARGET_URL, "Message": f"No product records found: HTTP {status}, Title: '{safe_title}', Screenshot: diagnostic_screenshot.png"})
        log(f"No product records found: HTTP {status}, Title: '{safe_title}', Screenshot: diagnostic_screenshot.png")
        log("ERROR: Zero product records were extracted. Aborting.")
        write_output(records, flags, args.output, args.input)
        sys.exit(1)
    required = ["product_id", "product_url", "source_listing_url", "Entity Name", "ID Number", "Entity Type", "Principal Office Address", "Current Status", "Entity Detail URL"]
    for record in records:
        for field in required:
            if not clean(record.get(field)):
                flags.append({"Type": "Missing Field", "URL": record.get("product_url", ""), "Message": f"Missing {field}"})
    write_output(records, flags, args.output, args.input)
    log(f"Wrote {len(records)} records to {args.output}.")


if __name__ == "__main__":
    main()