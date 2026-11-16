import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from openpyxl import Workbook, load_workbook
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://rnt.turismodeportugal.pt/RNT/Pesquisa_ET.aspx"
RESULTS_TABLE = "#RichWidgets_wt104_block_wtMainContent_wttrRegistos"
SEARCH_INPUT = "#RichWidgets_wt104_block_wtMainContent_wtInput_FiltroTexto"
SEARCH_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt201"
RESET_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt22"
NEXT_BUTTON = "#RichWidgets_wt104_block_wtMainContent_RichWidgets_wt11_block_wt28.ListNavigation_Next"
DETAIL_TABLE = "#RichWidgets_wt13_block_wtMainContent_wt15_wtShowEmprreendimento"


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
    return "" if text.lower() in {"", "nan", "none", "null"} else text


def mask_reviewer_pii(text):
    text = clean(text)
    if not text:
        return text
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***.***", text)

    def mask_name(match):
        parts = match.group(0).split()
        return " ".join(f"{p[0]}***" for p in parts[:-1]) + f" {parts[-1][0]}."

    return re.sub(r"\b[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]+\s+[A-ZÀ-ÖØ-Ý][a-zà-öø-ÿ]+\b", mask_name, text)


def extract_registration(url="", text=""):
    match = re.search(r"[?&]nr=(\d+)", url or "", re.I)
    if match:
        return match.group(1)
    match = re.search(r"RNET\s*n?[ºo]?\s*(\d+)", text or "", re.I)
    return match.group(1) if match else ""


def detect_anti_bot_signals(status, html):
    markers = (
        "cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...",
        "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold",
        "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada",
        "akamai", "shieldsquare", "security challenge", "pardon our interruption",
        "potential security risk", "detected automated scripts", "additional security check",
        "i am human", "hcaptcha", "access denied", "robot or human"
    )
    lower = (html or "").lower()
    found = [marker for marker in markers if marker in lower]
    return status in {403, 429} or bool(found), found


async def dismiss_cookie_banner(page):
    for selector in (
        "button:has-text('Accept')", "button:has-text('Aceitar')",
        "button:has-text('Agree')", "[id*='cookie' i] button"
    ):
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=500):
                await loc.click(timeout=1500)
                return
        except Exception:
            continue


class Scraper:
    def __init__(self, args):
        self.args = args
        self.flags = []
        self.records = []
        self.categories_discovered = 0
        self.links_discovered = 0
        self.links_processed = 0
        self.records_extracted = 0
        self.stop_for_antibot = False
        self.diagnostic = (0, "")
        self.seen_ids = set()

    def flag(self, kind, url, message):
        self.flags.append({"Type": kind, "URL": clean(url), "Message": clean(message)[:1000]})

    async def goto(self, page, url):
        log(f"Opening {url}")
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await dismiss_cookie_banner(page)
            html = await page.content()
            status = response.status if response else 0
            blocked, markers = detect_anti_bot_signals(status, html)
            if blocked:
                message = f"HTTP {status}; markers: {', '.join(markers)}"
                self.flag("Blocked by bot protection", url, message)
                log(f"Blocked by bot protection: {message}")
                self.stop_for_antibot = True
            return response, html
        except Exception as exc:
            self.flag("Error", url, repr(exc))
            log(f"Navigation failed for {url}: {exc}")
            return None, ""

    async def safe_click(self, page, locator, selector, url):
        try:
            await locator.click(timeout=10000)
            return True
        except Exception as exc:
            log(f"Click blocked on locator '{selector}': {exc}")
            self.flag("Click Intercepted", url, f"Element {selector} blocked: {str(exc)[:200]}")
            try:
                await page.keyboard.press("Escape")
                await locator.click(timeout=3000)
                return True
            except Exception:
                try:
                    await locator.evaluate("el => el.click()")
                    return True
                except Exception as fallback_exc:
                    log(f"Fallback click failed on '{selector}': {fallback_exc}")
                    return False

    async def wait_for_results(self, page, previous_ids=None):
        try:
            await page.wait_for_selector(RESULTS_TABLE, timeout=30000)
            await page.wait_for_function(
                """sel => Array.from(document.querySelectorAll(sel + " a[href*='RNET.aspx?nr=']")).length > 0""",
                RESULTS_TABLE,
                timeout=30000,
            )
            if previous_ids:
                try:
                    await page.wait_for_function(
                        """args => { const ids = Array.from(document.querySelectorAll(args.sel + " a[href*='RNET.aspx?nr=']")).map(a => a.href); return ids.some(x => !args.old.includes(x)); }""",
                        {"sel": RESULTS_TABLE, "old": list(previous_ids)},
                        timeout=30000,
                    )
                except PlaywrightTimeoutError:
                    self.flag("Low-confidence extraction", page.url, "Pagination click did not visibly change result links")
            return True
        except PlaywrightTimeoutError as exc:
            self.flag("Missing field", page.url, f"Search results did not load: {exc}")
            return False

    def parse_listing(self, html, source_url):
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one(RESULTS_TABLE)
        if not table:
            return []
        rows = table.select("tr")
        items = []
        for row in rows:
            link = row.select_one("a[href*='RNET.aspx?nr=']")
            if not link:
                continue
            values = [clean(cell.get_text(" ", strip=True)) for cell in row.find_all("td")]
            detail_url = urljoin(source_url, link.get("href", ""))
            registration = extract_registration(detail_url, values[0] if values else "")
            if not registration:
                self.flag("Low-confidence extraction", source_url, "Listing row has no registration number")
                continue
            if len(values) < 6:
                self.flag("Low-confidence extraction", source_url, "Listing row has fewer than six data cells")
            item = {
                "product_id": registration,
                "product_url": detail_url,
                "source_listing_url": source_url,
                "Nº": values[0] if len(values) > 0 else registration,
                "Data": values[1] if len(values) > 1 else "",
                "Nome": values[2] if len(values) > 2 else "",
                "Tipologia": values[3] if len(values) > 3 else "",
                "Capacidade": values[4] if len(values) > 4 else "",
                "Localização": values[5] if len(values) > 5 else "",
                "Certificações": values[6] if len(values) > 6 else "",
            }
            items.append(item)
        return items

    @staticmethod
    def detail_value(soup, labels):
        labels = [x.casefold() for x in labels]
        for row in soup.select("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label = clean(cells[0].get_text(" ", strip=True)).casefold().rstrip(":")
            if any(label == wanted or wanted in label for wanted in labels):
                return clean(cells[1].get_text(" ", strip=True))
        return ""

    async def extract_detail(self, context, item):
        page = await context.new_page()
        try:
            response, html = await self.goto(page, item["product_url"])
            if self.stop_for_antibot:
                return None
            soup = BeautifulSoup(html, "html.parser")
            safe_title = ((soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else ""
            full_text = soup.get_text("\n", strip=True).replace("\xa0", " ")
            registration = extract_registration(item["product_url"], full_text) or item["product_id"]
            registered = ""
            match = re.search(r"Registado\s+em\s*[:\-]?\s*([^\n]+)", full_text, re.I)
            if match:
                registered = clean(match.group(1))
            contacts = self.detail_value(soup, ["contactos"])
            website_node = soup.select_one("#RichWidgets_wt13_block_wtMainContent_wt15_wt247")
            website = ""
            if website_node:
                website = clean(website_node.get("href") or website_node.get_text(" ", strip=True))
            if website and website not in contacts:
                contacts = f"{contacts}; {website}" if contacts else website
            operator = ""
            nif = ""
            match = re.search(r"Entidade\s+exploradora\s*[:\-]?\s*([^\n]+)", full_text, re.I)
            if match:
                raw_operator = clean(match.group(1))
                tax_match = re.match(r"(\d{8,9})\s*(?:-|–|—)?\s*(.*)", raw_operator)
                if tax_match:
                    nif, operator = tax_match.group(1), clean(tax_match.group(2))
                else:
                    operator = raw_operator
            if not nif:
                match = re.search(r"(?:NIF|NIPC)\s*[:\-]?\s*(\d[\d .-]{7,})", full_text, re.I)
                if match:
                    nif = re.sub(r"\D", "", match.group(1))
            record = dict(item)
            record.update({
                "product_id": registration,
                "RNET nº": registration,
                "Data de Registo": registered,
                "Tipologia Completa": self.detail_value(soup, ["tipologia"]) or item.get("Tipologia", ""),
                "Morada e Código Postal": self.detail_value(soup, ["morada", "localização", "localizacao"]),
                "Contactos": contacts,
                "Marca Comercial": self.detail_value(soup, ["marca comercial", "marca"]),
                "Entidade Exploradora": operator,
                "NIF / NIPC": nif,
                "Title": safe_title,
                "Price": "",
                "Description": "",
                "Modules": "",
                "Categories": "",
                "Images": "",
            })
            if not soup.select_one(DETAIL_TABLE):
                self.flag("Low-confidence extraction", item["product_url"], "Expected detail table was not found")
            for field in ("RNET nº", "Data de Registo", "Tipologia Completa", "Morada e Código Postal", "Contactos", "Marca Comercial", "Entidade Exploradora", "NIF / NIPC"):
                if not record.get(field):
                    self.flag("Missing field", item["product_url"], f"Missing {field}")
            if self.args.mask_reviews:
                for key in list(record):
                    if "review" in key.casefold():
                        record[key] = mask_reviewer_pii(record[key])
            return record
        finally:
            await page.close()

    async def search_keyword(self, page, keyword):
        await page.wait_for_selector(SEARCH_INPUT, timeout=30000)
        reset = page.locator(RESET_BUTTON).first
        try:
            if await reset.is_visible(timeout=1000):
                await self.safe_click(page, reset, RESET_BUTTON, page.url)
                await page.wait_for_selector(SEARCH_INPUT, timeout=10000)
        except Exception:
            pass
        await page.locator(SEARCH_INPUT).fill("")
        await page.locator(SEARCH_INPUT).fill(keyword)
        button = page.locator(SEARCH_BUTTON).first
        if not await self.safe_click(page, button, SEARCH_BUTTON, page.url):
            return False
        return await self.wait_for_results(page)

    async def crawl_listing(self, page, context, listing_url, keyword=""):
        response, html = await self.goto(page, listing_url)
        if self.stop_for_antibot:
            return
        if keyword and not await self.search_keyword(page, keyword):
            return
        while not self.stop_for_antibot:
            source_url = page.url
            html = await page.content()
            items = self.parse_listing(html, source_url)
            self.categories_discovered += 1
            self.links_discovered += len(items)
            for item in items:
                if self.args.max_products and self.records_extracted >= self.args.max_products:
                    return
                if item["product_id"] in self.seen_ids:
                    continue
                self.seen_ids.add(item["product_id"])
                detail = await self.extract_detail(context, item)
                self.links_processed += 1
                if detail:
                    self.records.append(detail)
                    self.records_extracted += 1
            if not items:
                break
            next_link = page.locator(NEXT_BUTTON).first
            try:
                if not await next_link.count() or not await next_link.is_visible(timeout=1000):
                    break
                href = await next_link.get_attribute("href")
                classes = (await next_link.get_attribute("class") or "").casefold()
                disabled = await next_link.get_attribute("aria-disabled")
                if disabled == "true" or "disabled" in classes or href is None:
                    break
                old_ids = {x["product_id"] for x in items}
                if not await self.safe_click(page, next_link, NEXT_BUTTON, page.url):
                    break
                await self.wait_for_results(page, old_ids)
            except Exception as exc:
                self.flag("Error", page.url, f"Pagination failed: {exc}")
                break

    async def execute(self, rows):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.args.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            page = await context.new_page()
            for _, row in rows.iterrows():
                if self.stop_for_antibot or (self.args.max_products and self.records_extracted >= self.args.max_products):
                    break
                detail_url = clean(row.get("detail_url", ""))
                if detail_url:
                    url = urljoin(TARGET_URL, detail_url)
                    item = {
                        "product_id": extract_registration(url),
                        "product_url": url,
                        "source_listing_url": "DIRECT_INPUT",
                        "Nº": extract_registration(url),
                    }
                    self.links_discovered += 1
                    detail = await self.extract_detail(context, item)
                    self.links_processed += 1
                    if detail:
                        self.records.append(detail)
                        self.records_extracted += 1
                    continue
                listing_url = clean(row.get("listing_url", "")) or TARGET_URL
                keyword = clean(row.get("keyword", ""))
                await self.crawl_listing(page, context, listing_url, keyword)
            if not self.records_extracted:
                try:
                    status = 0
                    if page.url:
                        response = await page.reload(wait_until="domcontentloaded", timeout=30000)
                        status = response.status if response else 0
                    title = await page.title()
                    await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
                    self.diagnostic = (status, title)
                except Exception as exc:
                    self.flag("Error", page.url, f"Diagnostic screenshot failed: {exc}")
            await browser.close()


def read_input(path):
    sheets = pd.read_excel(path, sheet_name=None)
    sheets = {name: frame.fillna("") for name, frame in sheets.items()}
    if not sheets:
        return pd.DataFrame(columns=["keyword", "listing_url", "detail_url"])
    candidate = sheets.get("Extracted Data", next(iter(sheets.values())))
    normalized = {str(c).strip().casefold() for c in candidate.columns}
    allowed = {"keyword", "listing_url", "detail_url"}
    if not normalized.intersection(allowed):
        candidate = pd.read_excel(path, header=None).fillna("")
        candidate = candidate.rename(columns={candidate.columns[0]: "keyword"})
    candidate = candidate.copy()
    candidate.columns = [str(c).strip().casefold() for c in candidate.columns]
    for col in allowed:
        if col not in candidate.columns:
            candidate[col] = ""
    return candidate


def export_results(scraper, output, input_path):
    columns = [
        "product_id", "product_url", "source_listing_url", "Nº", "Data", "Nome", "Tipologia",
        "Capacidade", "Localização", "Certificações", "RNET nº", "Data de Registo",
        "Tipologia Completa", "Morada e Código Postal", "Contactos", "Marca Comercial",
        "Entidade Exploradora", "NIF / NIPC", "Title", "Price", "Description", "Modules",
        "Categories", "Images",
    ]
    wb = load_workbook(input_path) if Path(input_path).exists() else Workbook()
    for sheet_name in ("Extracted Data", "Flags"):
        if sheet_name in wb.sheetnames:
            del wb[sheet_name]
    data_ws = wb.create_sheet("Extracted Data", 0)
    for col, name in enumerate(columns, 1):
        data_ws.cell(1, col, sanitize_cell_value(name))
    for row_num, record in enumerate(scraper.records, 2):
        for col, name in enumerate(columns, 1):
            data_ws.cell(row_num, col, sanitize_cell_value(record.get(name, "")))
    flags_ws = wb.create_sheet("Flags")
    flag_columns = ["Type", "URL", "Message"]
    for col, name in enumerate(flag_columns, 1):
        flags_ws.cell(1, col, sanitize_cell_value(name))
    for row_num, flag in enumerate(scraper.flags, 2):
        for col, name in enumerate(flag_columns, 1):
            flags_ws.cell(row_num, col, sanitize_cell_value(flag.get(name, "")))
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is not None:
                    cell.value = sanitize_cell_value(cell.value)
    wb.save(output)


def parse_args():
    parser = argparse.ArgumentParser(description="RNET accommodation scraper")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


async def main_async(args):
    rows = read_input(args.input)
    scraper = Scraper(args)
    await scraper.execute(rows)
    log(f"Crawl Funnel: {scraper.categories_discovered} categories, {scraper.links_discovered} links discovered, {scraper.links_processed} processed, {scraper.records_extracted} extracted.")
    export_results(scraper, args.output, args.input)
    if scraper.stop_for_antibot:
        log("Stopped cleanly due to bot protection; no retries were attempted.")
        return
    if scraper.records_extracted == 0:
        status, title = scraper.diagnostic
        log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
        log("ERROR: Zero product records were extracted. Aborting.")
        sys.exit(1)


def main():
    asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    main()