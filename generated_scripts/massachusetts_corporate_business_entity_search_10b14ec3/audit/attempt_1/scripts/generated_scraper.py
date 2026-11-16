import argparse
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://corp.sec.state.ma.us/CorpWeb/CorpSearch/CorpSearch.aspx"
BASE_URL = "https://corp.sec.state.ma.us/"


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
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def value_from_row(row, column):
    if column not in row:
        return ""
    return clean(row[column])


def mask_reviewer_pii(text):
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "***@***.***", text)

    def mask_name(match):
        words = match.group(0).split()
        return " ".join(f"{word[0]}***" if i == 0 else f"{word[0]}." for i, word in enumerate(words))

    return re.sub(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+\b", mask_name, text)


def canonical_product_id(url):
    parsed = urlparse(url)
    query_value = parse_qs(parsed.query).get("sysvalue", [""])[0].strip()
    if query_value:
        return query_value
    match = re.search(r"/([^/?#]+?)(?:\.aspx)?(?:[/?#]|$)", parsed.path, re.I)
    if match and match.group(1).lower() not in {"corpsummary", "corpsearch"}:
        return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def detect_anti_bot_signals(status, html):
    markers = (
        "cloudflare turnstile",
        "just a moment...",
        "datadome",
        "perimeterx",
        "verify you are human",
    )
    lowered = (html or "").lower()
    if status in {403, 429} or any(marker in lowered for marker in markers):
        return "Blocked by bot protection"
    return ""


def static_status(url):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"},
            timeout=30,
        )
        return response.status_code, response.text
    except requests.RequestException as exc:
        log(f"HTTP preflight failed for {url}: {exc}")
        return 0, ""


def table_field(soup, selector, labels):
    table = soup.select_one(selector)
    if not table:
        return ""
    wanted = {label.lower().rstrip(":") for label in labels}
    for row in table.select("tr"):
        cells = [" ".join(cell.stripped_strings) for cell in row.select("th, td")]
        cells = [re.sub(r"\s+", " ", cell).strip() for cell in cells if cell.strip()]
        if len(cells) >= 2:
            label = cells[0].lower().rstrip(":")
            if label in wanted:
                return " ".join(cells[1:]).strip()
    return ""


def first_table_value(soup, selector):
    table = soup.select_one(selector)
    if not table:
        return ""
    text = " ".join(table.stripped_strings)
    return re.sub(r"\s+", " ", text).strip()


def extract_detail_html(html, url, source_listing_url, mask_reviews):
    soup = BeautifulSoup(html, "html.parser")
    entity_name = table_field(soup, "#MainContent_tblEntity", ["Entity Name", "Name"])
    if not entity_name:
        entity_name = first_table_value(soup, "#MainContent_tblEntity")
    entity_type = table_field(soup, "#MainContent_tblEntityType", ["Entity Type", "Type"])
    if not entity_type:
        entity_type = first_table_value(soup, "#MainContent_tblEntityType")
    id_number = table_field(soup, "#MainContent_tblIDNumber", ["ID Number", "Identification Number"])
    if not id_number:
        id_number = first_table_value(soup, "#MainContent_tblIDNumber")
    office = table_field(soup, "#MainContent_tblPrincipleOffice", ["Principal Office", "Principal Office Address", "Address"])
    if not office:
        office = first_table_value(soup, "#MainContent_tblPrincipleOffice")
    status = table_field(soup, "#MainContent_tblStatus", ["Current Status", "Status"])
    if mask_reviews:
        entity_name = mask_reviewer_pii(entity_name)
        entity_type = mask_reviewer_pii(entity_type)
        office = mask_reviewer_pii(office)
    record = {
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
    return record


async def dismiss_cookie_banner(page):
    selectors = [
        "button:has-text('Accept')",
        "button:has-text('Allow all')",
        "button:has-text('I agree')",
        "[id*='cookie' i] button",
        "[class*='cookie' i] button",
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1500)
                return
        except Exception:
            continue


async def safe_click(page, locator, selector, flags, url):
    try:
        await locator.click(timeout=5000)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url, "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
        try:
            await page.keyboard.press("Escape")
            await locator.click(timeout=2000)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception as fallback_exc:
                log(f"Click fallback failed on locator '{selector}': {fallback_exc}")
                return False


async def get_page_detail(page, url, source_listing_url, mask_reviews, flags):
    response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await dismiss_cookie_banner(page)
    html = await page.content()
    status = response.status if response else 0
    blocked = detect_anti_bot_signals(status, html)
    if blocked:
        flags.append({"Type": blocked, "URL": url, "Message": blocked})
        return None, True
    return extract_detail_html(html, page.url, source_listing_url, mask_reviews), False


async def extract_listing_page(page, listing_url, keyword, max_remaining, mask_reviews, flags):
    response = await page.goto(listing_url, wait_until="domcontentloaded", timeout=45000)
    await dismiss_cookie_banner(page)
    initial_html = await page.content()
    status = response.status if response else 0
    blocked = detect_anti_bot_signals(status, initial_html)
    if blocked:
        flags.append({"Type": blocked, "URL": listing_url, "Message": blocked})
        return [], True
    await page.wait_for_selector("#MainContent_rdoByEntityName", timeout=15000)
    radio = page.locator("#MainContent_rdoByEntityName")
    if not await radio.is_checked():
        await radio.check()
    if keyword:
        await page.locator("#MainContent_txtEntityName").fill(keyword)
    await page.locator("#MainContent_ddBeginsWithEntityName").select_option("B")
    submit = page.locator("#MainContent_btnSearch")
    if not await safe_click(page, submit, "#MainContent_btnSearch", flags, page.url):
        return [], False
    try:
        await page.wait_for_selector("#MainContent_SearchControl_grdSearchResultsEntity", timeout=30000)
    except PlaywrightTimeoutError:
        flags.append({"Type": "Error", "URL": page.url, "Message": "Results grid did not load after search"})
        return [], False

    records = []
    seen_pages = set()
    while len(records) < max_remaining:
        grid = page.locator("#MainContent_SearchControl_grdSearchResultsEntity")
        rows = grid.locator("tr.GridRow, tr.GridAltRow")
        row_count = await rows.count()
        page_signature = await grid.inner_text()
        if page_signature in seen_pages:
            break
        seen_pages.add(page_signature)
        links = []
        for i in range(row_count):
            row = rows.nth(i)
            link = row.locator("th.RowHeader a.link, a[href*='CorpSummary.aspx']").first
            if await link.count() == 0:
                continue
            href = await link.get_attribute("href")
            name = (await link.inner_text()).strip()
            cells = row.locator("td")
            id_number = (await cells.nth(0).inner_text()).strip() if await cells.count() > 0 else ""
            office = (await cells.nth(2).inner_text()).strip() if await cells.count() > 2 else ""
            if href:
                links.append((urljoin(page.url, href), name, id_number, office))
        detail_page = await page.context.new_page()
        try:
            for detail_url, grid_name, grid_id, grid_office in links:
                if len(records) >= max_remaining:
                    break
                record, halted = await get_page_detail(detail_page, detail_url, listing_url, mask_reviews, flags)
                if halted:
                    return records, True
                if record:
                    if not record["Entity Name"]:
                        record["Entity Name"] = grid_name
                    if not record["ID Number"]:
                        record["ID Number"] = grid_id
                    if not record["Principal Office Address"]:
                        record["Principal Office Address"] = office if (office := grid_office) else ""
                    records.append(record)
        finally:
            await detail_page.close()
        if len(records) >= max_remaining:
            break
        next_link = None
        anchors = page.locator("#MainContent_SearchControl_grdSearchResultsEntity a")
        for i in range(await anchors.count()):
            anchor = anchors.nth(i)
            text = (await anchor.inner_text()).strip()
            if text.lower() == "next" or text.isdigit() and text not in {"1"}:
                next_link = anchor
                break
        if next_link is None:
            break
        if not await safe_click(page, next_link, "#MainContent_SearchControl_grdSearchResultsEntity a", flags, page.url):
            break
        try:
            await page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
    return records, False


async def run(args, input_df):
    flags = []
    records = []
    rows = input_df.to_dict("records")
    categories_discovered = len(rows)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
        page = await context.new_page()
        halted = False
        for row in rows:
            if args.max_products is not None and len(records) >= args.max_products:
                break
            detail_url = value_from_row(row, "detail_url")
            listing_url = value_from_row(row, "listing_url") or TARGET_URL
            keyword = value_from_row(row, "keyword")
            remaining = args.max_products - len(records) if args.max_products is not None else 10**9
            if detail_url:
                detail_page = await context.new_page()
                try:
                    record, halted = await get_page_detail(detail_page, urljoin(BASE_URL, detail_url), "DIRECT_INPUT", args.mask_reviews, flags)
                    if record:
                        records.append(record)
                finally:
                    await detail_page.close()
            else:
                found, halted = await extract_listing_page(page, listing_url, keyword, remaining, args.mask_reviews, flags)
                records.extend(found)
            if halted:
                break
        await browser.close()
    return records, flags, categories_discovered, halted


def write_output(records, flags, output_path):
    safe_records = [{key: sanitize_cell_value(value) for key, value in record.items()} for record in records]
    safe_flags = [{key: sanitize_cell_value(value) for key, value in flag.items()} for flag in flags]
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        pd.DataFrame(safe_records).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(safe_flags, columns=["Type", "URL", "Message"]).to_excel(writer, sheet_name="Flags", index=False)


def main():
    parser = argparse.ArgumentParser(description="Massachusetts corporations scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    try:
        input_df = pd.read_excel(args.input).fillna("")
    except FileNotFoundError:
        input_df = pd.DataFrame([{"keyword": "mobius"}])
        log(f"Input file {args.input} was not found; using the inspection keyword 'mobius'.")
    except Exception as exc:
        log(f"Unable to read input workbook: {exc}")
        sys.exit(1)
    records, flags, categories, halted = asyncio.run(run(args, input_df))
    links_discovered = len(records)
    log(f"Crawl Funnel: {categories} categories, {links_discovered} links discovered, {len(records)} processed, {len(records)} extracted.")
    if halted and any(flag.get("Type") == "Blocked by bot protection" for flag in flags):
        write_output(records, flags, args.output)
        log("Blocked by bot protection; stopped without retries.")
        return
    if not records:
        status, html = static_status(TARGET_URL)
        title = BeautifulSoup(html, "html.parser").title.get_text(strip=True) if html else ""
        flags.append({"Type": "Error", "URL": TARGET_URL, "Message": f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png"})
        log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
        log("ERROR: Zero product records were extracted. Aborting.")
        write_output(records, flags, args.output)
        sys.exit(1)
    for record in records:
        for field in ("Entity Name", "ID Number", "Entity Type", "Principal Office Address", "Current Status", "Entity Detail URL"):
            if not record.get(field):
                flags.append({"Type": "Missing Field", "URL": record.get("product_url", ""), "Message": f"Missing {field}"})
    write_output(records, flags, args.output)
    log(f"Wrote {len(records)} records to {args.output}.")


if __name__ == "__main__":
    main()