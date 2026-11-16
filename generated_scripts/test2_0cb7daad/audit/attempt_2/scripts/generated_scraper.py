import argparse
import asyncio
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs

import pandas as pd
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://rnt.turismodeportugal.pt/RNT/Pesquisa_ET.aspx"
LISTING_TABLE = "#RichWidgets_wt104_block_wtMainContent_wttrRegistos"
SEARCH_INPUT = "#RichWidgets_wt104_block_wtMainContent_wtInput_FiltroTexto"
SEARCH_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt201"
CLEAR_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt22"
NEXT_BUTTON = ".ListNavigation_Next"
DETAIL_TABLE = "#RichWidgets_wt13_block_wtMainContent_wt15_wtShowEmprreendimento"

WAF_MARKERS = (
    "cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...",
    "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold",
    "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada",
    "akamai", "shieldsquare", "security challenge", "pardon our interruption",
    "potential security risk", "detected automated scripts", "additional security check",
    "i am human", "hcaptcha", "access denied", "robot or human"
)

OUTPUT_COLUMNS = [
    "product_id", "product_url", "source_listing_url", "Nº", "Data", "Nome",
    "Tipologia", "Capacidade", "Localização", "Certificações", "RNET nº",
    "Data de Registo", "Tipologia Completa", "Morada e Código Postal", "Contactos",
    "Marca Comercial", "Entidade Exploradora", "NIF / NIPC"
]


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


def clean(val):
    if val is None:
        return ""
    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    value = str(val).replace("\xa0", " ").strip()
    return "" if value.lower() in {"nan", "none", "null"} else value


def norm(value):
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def safe_title(soup):
    return (soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else ""


def parse_input(path):
    sheets = pd.read_excel(path, sheet_name=None, header=None)
    sheets = {name: frame.fillna("") for name, frame in sheets.items()}
    frame = next(iter(sheets.values()), pd.DataFrame())
    if frame.empty:
        return [], sheets

    first = [clean(v).lower() for v in frame.iloc[0].tolist()]
    header_names = {"keyword", "listing_url", "detail_url"}
    is_header = bool(set(first) & header_names)
    rows = []
    start = 1 if is_header else 0
    positions = {name: first.index(name) for name in header_names if name in first} if is_header else {}

    for _, row in frame.iloc[start:].iterrows():
        values = [clean(v) for v in row.tolist()]
        if is_header:
            item = {name: clean(row.iloc[pos]) for name, pos in positions.items()}
        else:
            item = {"keyword": values[0] if values else ""}
            if len(values) > 1:
                item["listing_url"] = values[1]
            if len(values) > 2:
                item["detail_url"] = values[2]
        if any(clean(v) for v in item.values()):
            rows.append(item)
    return rows, sheets


async def dismiss_cookie_banner(page):
    for selector in (
        "button:has-text('Aceitar')", "button:has-text('Accept')",
        "button:has-text('Concordo')", "[id*='cookie' i] button"
    ):
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1500)
                return
        except Exception:
            pass


async def detect_anti_bot_signals(page, response, flags, url):
    status = response.status if response else 0
    html = (await page.content()).lower()
    matched = [marker for marker in WAF_MARKERS if marker in html]
    if status in {403, 429} or matched:
        message = f"HTTP {status}; markers: {', '.join(matched) or 'none'}"
        flags.append({"Type": "Blocked by bot protection", "URL": url, "Message": message})
        log(f"Blocked by bot protection: {message}")
        return True
    return False


async def goto_page(page, url, flags):
    log(f"Opening {url}")
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)[:500]})
        log(f"Navigation error: {exc}")
        return None, False
    await dismiss_cookie_banner(page)
    blocked = await detect_anti_bot_signals(page, response, flags, url)
    if not blocked:
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass
    return response, blocked


async def safe_click(page, locator, selector, flags, url):
    try:
        await locator.click(timeout=10000)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url,
                      "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
        try:
            await page.keyboard.press("Escape")
            await locator.click(timeout=3000)
            return True
        except Exception:
            try:
                await locator.evaluate("el => el.click()")
                return True
            except Exception as fallback_exc:
                log(f"Click fallback failed: {fallback_exc}")
                flags.append({"Type": "Click Error", "URL": url, "Message": str(fallback_exc)[:500]})
                return False


async def wait_for_results(page, previous_signature=""):
    await page.wait_for_selector(LISTING_TABLE, timeout=20000)
    try:
        await page.wait_for_function(
            """([table, previous]) => {
                const node = document.querySelector(table);
                if (!node) return false;
                const rows = [...node.querySelectorAll('tr')].filter(r => r.querySelector("a[href*='RNET.aspx?nr=']"));
                const text = document.body.innerText || '';
                const counter = /\\d+\\s+a\\s+\\d+\\s+de\\s+\\d+\\s+registos/i.test(text);
                const signature = rows.map(r => r.innerText).join('|');
                return rows.length > 0 && (counter || !previous || signature !== previous);
            }""",
            [LISTING_TABLE, previous_signature], timeout=20000
        )
    except PlaywrightTimeoutError:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except PlaywrightTimeoutError:
        pass


def extract_listing_rows(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(LISTING_TABLE)
    if not table:
        return []

    header_map = {}
    header = table.select_one("tr:has(th)")
    if header:
        for index, cell in enumerate(header.select("th, td")):
            key = norm(cell.get_text(" ", strip=True))
            if key:
                header_map[key] = index

    results = []
    for tr in table.select("tr"):
        link = tr.select_one("a[href*='RNET.aspx?nr=']")
        cells = [cell.get_text(" ", strip=True) for cell in tr.select("td")]
        if not link or not cells:
            continue
        href = urljoin(source_url, link.get("href", ""))
        match = re.search(r"[?&]nr=(\d+)", href, re.I)
        if not match:
            continue
        product_id = match.group(1)

        def value(label, fallback):
            index = header_map.get(norm(label))
            if index is not None and index < len(cells):
                return clean(cells[index])
            return clean(cells[fallback]) if fallback < len(cells) else ""

        results.append({
            "product_id": product_id,
            "product_url": href,
            "source_listing_url": TARGET_URL,
            "Nº": value("Nº", 0),
            "Data": value("Data", 1),
            "Nome": value("Nome", 2),
            "Tipologia": value("Tipologia", 3),
            "Capacidade": value("Capacidade", 4),
            "Localização": value("Localização", 5),
            "Certificações": value("Certificações", 6),
        })
    return results


def detail_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(DETAIL_TABLE) or soup
    pairs = {}
    for tr in table.select("tr"):
        cells = [clean(c.get_text(" ", strip=True)) for c in tr.select("th, td")]
        if len(cells) >= 2 and cells[0]:
            pairs[norm(cells[0])] = clean(" ".join(cells[1:]))

    full = soup.get_text("\n", strip=True)

    def find(labels):
        labels = [norm(label) for label in labels]
        for key, value in pairs.items():
            if any(label == key or label in key for label in labels):
                return value
        for label in labels:
            match = re.search(re.escape(label).replace("\\ ", r"\s+") + r"\s*:?\s*([^\n]+)", full, re.I)
            if match:
                return clean(match.group(1))
        return ""

    heading_match = re.search(r"RNET\s*n[ºo]?\s*[:\-]?\s*(\d+)", full, re.I)
    rnet = heading_match.group(1) if heading_match else find(["RNET nº", "RNET n", "RNET"])
    return {
        "RNET nº": rnet,
        "Data de Registo": find(["Data de Registo", "Registado em"]),
        "Tipologia Completa": find(["Tipologia Completa", "Tipologia"]),
        "Morada e Código Postal": find(["Morada e Código Postal", "Morada", "Código Postal"]),
        "Contactos": find(["Contactos", "Contacto"]),
        "Marca Comercial": find(["Marca Comercial"]),
        "Entidade Exploradora": find(["Entidade Exploradora"]),
        "NIF / NIPC": find(["NIF / NIPC", "NIF", "NIPC"]),
    }


async def diagnostic_screenshot(page, response):
    try:
        await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
    except Exception:
        pass
    soup = BeautifulSoup(await page.content(), "html.parser")
    status = response.status if response else 0
    title = safe_title(soup)
    log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")


async def scrape(args):
    inputs, input_sheets = parse_input(args.input)
    records, flags = [], []
    categories_discovered = len(inputs)
    links_discovered = 0
    links_processed = 0
    blocked = False
    page = None
    last_response = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = {runtime: {}};"
        )
        page = await context.new_page()

        for item in inputs:
            if args.max_products is not None and len(records) >= args.max_products:
                break
            direct_url = clean(item.get("detail_url"))
            keyword = clean(item.get("keyword"))
            listing_url = clean(item.get("listing_url")) or TARGET_URL

            if direct_url:
                detail_page = await context.new_page()
                try:
                    response, is_blocked = await goto_page(detail_page, direct_url, flags)
                    if is_blocked:
                        blocked = True
                        break
                    match = re.search(r"[?&]nr=(\d+)", direct_url, re.I)
                    fields = detail_fields(await detail_page.content())
                    product_id = match.group(1) if match else clean(fields.get("RNET nº"))
                    if product_id:
                        record = {"product_id": product_id, "product_url": direct_url,
                                  "source_listing_url": "DIRECT_INPUT"}
                        record.update(fields)
                        records.append(record)
                        links_processed += 1
                finally:
                    await detail_page.close()
                continue

            last_response, is_blocked = await goto_page(page, listing_url, flags)
            if is_blocked:
                blocked = True
                break

            if keyword:
                try:
                    clear = page.locator(CLEAR_BUTTON).first
                    if await clear.count() and await clear.is_visible(timeout=1000):
                        await safe_click(page, clear, CLEAR_BUTTON, flags, listing_url)
                    search = page.locator(SEARCH_INPUT)
                    await search.fill("")
                    await search.fill(keyword)
                    button = page.locator(SEARCH_BUTTON).first
                    if not await safe_click(page, button, SEARCH_BUTTON, flags, listing_url):
                        continue
                    await wait_for_results(page)
                    if await detect_anti_bot_signals(page, None, flags, page.url):
                        blocked = True
                        break
                except Exception as exc:
                    flags.append({"Type": "Search Error", "URL": listing_url, "Message": str(exc)[:500]})
                    continue

            keyword_had_rows = False
            seen_pages = set()
            while True:
                html = await page.content()
                found = extract_listing_rows(html, listing_url)
                signature = "|".join(row["product_id"] for row in found)
                if not found or signature in seen_pages:
                    if not keyword_had_rows and not records:
                        await diagnostic_screenshot(page, last_response)
                    if keyword and not keyword_had_rows:
                        log(f"No results found for keyword: {keyword}")
                    break
                seen_pages.add(signature)
                keyword_had_rows = True
                links_discovered += len(found)

                for listing in found:
                    if args.max_products is not None and len(records) >= args.max_products:
                        break
                    detail_page = await context.new_page()
                    try:
                        response, is_blocked = await goto_page(detail_page, listing["product_url"], flags)
                        if is_blocked:
                            blocked = True
                            break
                        listing.update(detail_fields(await detail_page.content()))
                        records.append(listing)
                        links_processed += 1
                    finally:
                        await detail_page.close()
                if blocked or (args.max_products is not None and len(records) >= args.max_products):
                    break

                next_link = page.locator(NEXT_BUTTON).first
                try:
                    if not await next_link.count() or not await next_link.is_visible():
                        break
                    aria_disabled = (await next_link.get_attribute("aria-disabled") or "").lower()
                    classes = (await next_link.get_attribute("class") or "").lower()
                    if aria_disabled == "true" or "disabled" in classes:
                        break
                    before = signature
                    if not await safe_click(page, next_link, NEXT_BUTTON, flags, page.url):
                        break
                    await wait_for_results(page, before)
                except Exception as exc:
                    flags.append({"Type": "Pagination Error", "URL": page.url, "Message": str(exc)[:500]})
                    break

        await browser.close()

    for record in records:
        for field in ("product_id", "product_url", "source_listing_url"):
            if not clean(record.get(field)):
                flags.append({"Type": "Missing Field", "URL": record.get("product_url", ""), "Message": field})
        for field in ("Nome", "Tipologia", "Marca Comercial"):
            if not clean(record.get(field)):
                flags.append({"Type": "Low-confidence extraction", "URL": record.get("product_url", ""), "Message": f"Missing {field}"})

    log(f"Crawl Funnel: {categories_discovered} categories, {links_discovered} links discovered, {links_processed} processed, {len(records)} extracted.")
    write_output(args.output, input_sheets, records, flags)
    if blocked:
        sys.exit(2)
    if not records:
        log("ERROR: Zero product records were extracted. Aborting.")
        sys.exit(1)


def write_output(path, source_sheets, records, flags):
    sheets = dict(source_sheets)
    sheets["Extracted Data"] = pd.DataFrame(
        [{column: sanitize_cell_value(record.get(column, "")) for column in OUTPUT_COLUMNS} for record in records],
        columns=OUTPUT_COLUMNS
    )
    sheets["Flags"] = pd.DataFrame(
        [{"Type": sanitize_cell_value(flag.get("Type", "")),
          "URL": sanitize_cell_value(flag.get("URL", "")),
          "Message": sanitize_cell_value(flag.get("Message", ""))} for flag in flags],
        columns=["Type", "URL", "Message"]
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.fillna("").to_excel(writer, sheet_name=str(name)[:31], index=False, header=True)


def main():
    parser = argparse.ArgumentParser(description="Scrape Portugal RNET accommodation listings.")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    asyncio.run(scrape(args))


if __name__ == "__main__":
    main()