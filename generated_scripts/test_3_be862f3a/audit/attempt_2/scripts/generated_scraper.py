import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import urljoin

import pandas as pd
from bs4 import BeautifulSoup
from openpyxl import load_workbook
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://rnt.turismodeportugal.pt/RNT/Pesquisa_ET.aspx"
LISTING_TABLE = "#RichWidgets_wt104_block_wtMainContent_wttrRegistos"
SEARCH_INPUT = "#RichWidgets_wt104_block_wtMainContent_wtInput_FiltroTexto"
SEARCH_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt201"
CLEAR_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt22"
DETAIL_TABLE = "#RichWidgets_wt13_block_wtMainContent_wt15_wtShowEmprreendimento"
OPERATING_TABLE = "#RichWidgets_wt13_block_wtMainContent_wt15_wtShowFuncionamento"
BASE_COLUMNS = ["product_id", "product_url", "source_listing_url", "Nº", "Data", "Nome", "Tipologia", "Capacidade", "Localização", "Certificações", "RNET nº", "Data de Registo", "Tipologia Completa", "Morada e Código Postal", "Contactos", "Marca Comercial", "Entidade Exploradora", "NIF / NIPC", "Title", "Price", "Description", "Modules", "Categories", "Images"]


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
    value = str(value).replace("\xa0", " ")
    if value.strip().lower() in {"", "nan", "none", "null"}:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def safe_title(soup):
    return (soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else ""


def product_id(url):
    match = re.search(r"[?&]nr=([^&#]+)", url, re.I)
    return match.group(1) if match else hashlib.sha256(url.encode()).hexdigest()[:24]


def mask_reviews(text):
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***.***", clean(text))
    def mask_name(match):
        parts = match.group(0).split()
        return " ".join(p[0] + "***" for p in parts[:-1]) + " " + parts[-1][0] + "."
    return re.sub(r"\b[A-ZÁÀÃÉÊÍÓÔÕÚÜÇ][a-záàãéêíóôõúüç]+\s+[A-ZÁÀÃÉÊÍÓÔÕÚÜÇ][a-záàãéêíóôõúüç]+\b", mask_name, text)


def detect_anti_bot_signals(status, html):
    markers = ("cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...", "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold", "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada", "akamai", "shieldsquare", "security challenge", "pardon our interruption", "potential security risk", "detected automated scripts", "additional security check", "i am human", "hcaptcha", "access denied", "robot or human")
    text = (html or "").lower()
    return status in (403, 429) or any(x in text for x in markers)


async def dismiss_cookie_banner(page):
    for selector in ("button:has-text('Accept')", "button:has-text('Aceitar')", "[aria-label*='accept' i]"):
        try:
            loc = page.locator(selector).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1500)
                break
        except Exception:
            pass


async def click_with_fallback(page, locator, selector, flags, url):
    try:
        await locator.click(timeout=15000)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url, "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
        try:
            await page.keyboard.press("Escape")
            await locator.evaluate("el => el.click()")
            return True
        except Exception as fallback_exc:
            log(f"Click fallback failed on locator '{selector}': {fallback_exc}")
            flags.append({"Type": "Error", "URL": url, "Message": str(fallback_exc)[:300]})
            return False


async def wait_results(page, old_signature=None):
    await page.wait_for_selector(f"{LISTING_TABLE} tr:has(td)", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    if old_signature is not None:
        async def changed():
            try:
                return (await page.locator(LISTING_TABLE).inner_text()) != old_signature
            except Exception:
                return False
        try:
            await page.wait_for_function("() => true", timeout=100)
            for _ in range(120):
                if await changed():
                    return
                await asyncio.sleep(0.25)
        except Exception:
            pass


def rows_from_table(soup):
    result = []
    for tr in soup.select("tr"):
        if tr.select_one("th") or not tr.select_one("a[href*='RNET.aspx?nr=']"):
            continue
        cells = [clean(td.get_text("\n", strip=True)) for td in tr.select("td")]
        if cells and any(cells):
            result.append((tr, cells))
    return result


def listing_record(tr, cells, source_url):
    link = tr.select_one("a[href*='RNET.aspx?nr=']")
    detail_url = urljoin(source_url, link.get("href", ""))
    fields = {"Nº": "", "Data": "", "Nome": "", "Tipologia": "", "Capacidade": "", "Localização": "", "Certificações": ""}
    table = tr.find_parent("table")
    headers = []
    if table:
        first = table.select_one("tr")
        headers = [clean(x.get_text(" ", strip=True)).lower() for x in first.select("th,td")] if first else []
    aliases = {"nº": "Nº", "n.º": "Nº", "no": "Nº", "data": "Data", "nome": "Nome", "tipologia": "Tipologia", "capacidade": "Capacidade", "localização": "Localização", "localizacao": "Localização", "certificações": "Certificações", "certificacoes": "Certificações"}
    for i, header in enumerate(headers):
        for alias, field in aliases.items():
            if header == alias or header.startswith(alias + " "):
                if i < len(cells):
                    fields[field] = cells[i]
                break
    if not any(fields.values()):
        usable = cells[1:] if len(cells) >= 8 and not cells[0] else cells
        for i, field in enumerate(fields):
            if i < len(usable):
                fields[field] = usable[i]
    if not fields["Nº"]:
        match = re.search(r"[?&]nr=([^&#]+)", detail_url, re.I)
        fields["Nº"] = match.group(1) if match else ""
    return fields, detail_url


def labeled_fields(soup):
    result = {}
    for tr in soup.select("tr"):
        cells = [clean(x.get_text(" ", strip=True)) for x in tr.select("th,td")]
        if len(cells) >= 2:
            result[cells[0].rstrip(":").lower()] = clean("; ".join(x for x in cells[1:] if x))
    return result


def first_labeled(fields, labels):
    for wanted in labels:
        for key, value in fields.items():
            if wanted in key and value:
                return value
    return ""


def parse_detail(html, mask):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one(DETAIL_TABLE) or soup
    operating = soup.select_one(OPERATING_TABLE)
    fields = labeled_fields(main)
    if operating:
        fields.update(labeled_fields(operating))
    body = clean(soup.get_text(" ", strip=True))
    rnet = re.search(r"RNET\s*n[ºo]?\s*[:.]?\s*([\w-]+)", body, re.I)
    registered = re.search(r"Registado\s+em\s*[:.]?\s*([^|;]+)", body, re.I)
    entity = first_labeled(fields, ["entidade exploradora"])
    nif = first_labeled(fields, ["nif", "nipc"])
    result = {"RNET nº": rnet.group(1) if rnet else "", "Data de Registo": clean(registered.group(1)) if registered else "", "Tipologia Completa": first_labeled(fields, ["tipologia completa", "tipologia"]), "Morada e Código Postal": first_labeled(fields, ["morada", "localização", "codigo postal", "código postal"]), "Contactos": first_labeled(fields, ["contactos", "contacto", "telefone", "email"]), "Marca Comercial": first_labeled(fields, ["marca comercial", "marca"]), "Entidade Exploradora": entity, "NIF / NIPC": nif}
    return {k: (mask_reviews(v) if mask else clean(v)) for k, v in result.items()}


async def extract_detail(context, detail_url, source_url, args, flags, metrics):
    tab = await context.new_page()
    try:
        response = await tab.goto(detail_url, wait_until="domcontentloaded", timeout=60000)
        await dismiss_cookie_banner(tab)
        html = await tab.content()
        status = response.status if response else 0
        if detect_anti_bot_signals(status, html):
            flags.append({"Type": "Blocked by bot protection", "URL": detail_url, "Message": "HTTP status or WAF marker detected"})
            return None, True
        metrics["links_processed"] += 1
        return {"product_id": product_id(detail_url), "product_url": detail_url, "source_listing_url": source_url, **parse_detail(html, args.mask_reviews)}, False
    finally:
        await tab.close()


async def crawl_listing(page, context, listing_url, keyword, args, flags, records, metrics):
    response = await page.goto(listing_url, wait_until="domcontentloaded", timeout=60000)
    await dismiss_cookie_banner(page)
    status = response.status if response else 0
    html = await page.content()
    if detect_anti_bot_signals(status, html):
        flags.append({"Type": "Blocked by bot protection", "URL": listing_url, "Message": "HTTP status or WAF marker detected"})
        return True
    box = page.locator(SEARCH_INPUT)
    if keyword:
        try:
            clear = page.locator(CLEAR_BUTTON).first
            if await clear.count() and await clear.is_visible():
                await click_with_fallback(page, clear, CLEAR_BUTTON, flags, page.url)
        except Exception:
            pass
        await box.fill("")
        await box.fill(keyword)
        if not await click_with_fallback(page, page.locator(SEARCH_BUTTON).first, SEARCH_BUTTON, flags, page.url):
            return False
        try:
            await wait_results(page)
        except PlaywrightTimeoutError:
            flags.append({"Type": "Missing field", "URL": page.url, "Message": "Results table did not load"})
            return False
    while True:
        table_html = await page.locator(LISTING_TABLE).inner_html()
        rows = rows_from_table(BeautifulSoup(table_html, "html.parser"))
        if not rows:
            log(f"No results found for keyword: {keyword}")
            return False
        for tr, cells in rows:
            listing, detail_url = listing_record(tr, cells, page.url)
            metrics["links_discovered"] += 1
            item, blocked = await extract_detail(context, detail_url, page.url, args, flags, metrics)
            if blocked:
                return True
            if item:
                records.append({**listing, **item})
                if args.max_products and len(records) >= args.max_products:
                    return False
        next_link = page.locator(".ListNavigation_Next").first
        if not await next_link.count() or not await next_link.is_visible():
            return False
        disabled = (await next_link.get_attribute("aria-disabled") or "").lower()
        classes = (await next_link.get_attribute("class") or "").lower()
        if disabled == "true" or "disabled" in classes:
            return False
        old = await page.locator(LISTING_TABLE).inner_text()
        if not await click_with_fallback(page, next_link, ".ListNavigation_Next", flags, page.url):
            return False
        try:
            await wait_results(page, old)
        except PlaywrightTimeoutError:
            return False


async def diagnostic(page, status):
    try:
        await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
        soup = BeautifulSoup(await page.content(), "html.parser")
        log(f"No product records found: HTTP {status}, Title: '{safe_title(soup)}', Screenshot: diagnostic_screenshot.png")
    except Exception as exc:
        log(f"Diagnostic screenshot failed: {exc}")


def input_rows(path):
    df = pd.read_excel(path, header=None).fillna("")
    result = []
    for _, row in df.iterrows():
        vals = [clean(x) for x in row.tolist()]
        vals += [""] * (3 - len(vals))
        if any(vals[:3]):
            result.append({"keyword": vals[0], "listing_url": vals[1], "detail_url": vals[2]})
    return result


def write_output(path, records, flags):
    old_sheets = {}
    if os.path.exists(path):
        try:
            wb = load_workbook(path, read_only=True, data_only=False)
            for ws in wb.worksheets:
                if ws.title not in {"Extracted Data", "Flags"}:
                    old_sheets[ws.title] = [[cell.value for cell in row] for row in ws.iter_rows()]
            wb.close()
        except Exception:
            old_sheets = {}
    mode = "a" if os.path.exists(path) else "w"
    with pd.ExcelWriter(path, engine="openpyxl", mode=mode, if_sheet_exists="replace" if mode == "a" else None) as writer:
        columns = list(BASE_COLUMNS)
        output = pd.DataFrame([{k: sanitize_cell_value(item.get(k, "")) for k in columns} for item in records], columns=columns)
        output.to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame([{k: sanitize_cell_value(v) for k, v in x.items()} for x in flags]).to_excel(writer, sheet_name="Flags", index=False)
        for name, data in old_sheets.items():
            pd.DataFrame(data).to_excel(writer, sheet_name=name, header=False, index=False)


async def main(args):
    records, flags = [], []
    rows = input_rows(args.input)
    metrics = {"categories_discovered": len(rows), "links_discovered": 0, "links_processed": 0}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", viewport={"width": 1920, "height": 1080})
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); window.chrome = {runtime: {}};")
        page = await context.new_page()
        blocked = False
        for row in rows:
            if args.max_products and len(records) >= args.max_products:
                break
            if row["detail_url"]:
                metrics["links_discovered"] += 1
                item, blocked = await extract_detail(context, row["detail_url"], "DIRECT_INPUT", args, flags, metrics)
                if item:
                    records.append(item)
            else:
                blocked = await crawl_listing(page, context, row["listing_url"] or TARGET_URL, row["keyword"], args, flags, records, metrics)
            if blocked:
                log("Blocked by bot protection; stopping without retries.")
                break
        if not records:
            await diagnostic(page, 0)
        await browser.close()
    metrics["records_extracted"] = len(records)
    log(f"Crawl Funnel: {metrics['categories_discovered']} categories, {metrics['links_discovered']} links discovered, {metrics['links_processed']} processed, {metrics['records_extracted']} extracted.")
    write_output(args.output, records, flags)
    if not records:
        log("ERROR: Zero product records were extracted. Aborting.")
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--mask-reviews", action=argparse.BooleanOptionalAction, default=True)
    asyncio.run(main(parser.parse_args()))