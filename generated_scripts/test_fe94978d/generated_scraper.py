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
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

TARGET_URL = "https://rnt.turismodeportugal.pt/RNT/Pesquisa_ET.aspx?FiltroVisivel=True"
RESULTS_TABLE = "#RichWidgets_wt104_block_wtMainContent_wttrRegistos"
RESULT_ROWS = RESULTS_TABLE + " > tbody > tr:not(:has(td[colspan='8']))"
SEARCH_INPUT = "#RichWidgets_wt104_block_wtMainContent_wtInput_FiltroTexto"
SEARCH_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt201"
CLEAR_BUTTON = "#RichWidgets_wt104_block_wtMainContent_wt22"

LIST_COLUMNS = [
    "Nº", "Data", "Nome", "Tipologia", "Capacidade", "Localização", "Certificações"
]
DETAIL_COLUMNS = [
    "RNET nº", "Data de Registo", "Tipologia Completa", "Morada e Código Postal",
    "Contactos", "Marca Comercial", "Entidade Exploradora", "NIF / NIPC",
    "Detail Heading"
]
LEGACY_COLUMNS = [
    "Title", "Price", "Description", "Modules", "Categories", "Images"
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


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    value = str(value).replace("\xa0", " ").strip()
    return "" if value.lower() in {"", "nan", "none", "null", "nat"} else value


def value_from_row(row, name):
    return clean(row.get(name, "")) if hasattr(row, "get") else ""


def canonical_id(url):
    return "url-" + hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:20]


def safe_title(soup):
    return ((soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else ""


def soup_text(soup):
    return soup.get_text("\n", strip=True).replace("\xa0", " ")


def mask_review_text(text):
    text = clean(text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "***@***.***", text)

    def mask_name(match):
        parts = match.group(0).split()
        return " ".join(p[0] + "***" for p in parts[:-1]) + " " + parts[-1][0] + "."

    return re.sub(
        r"\b[A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][a-záàãâéêíóôõúç]+\s+[A-ZÁÀÃÂÉÊÍÓÔÕÚÇ][a-záàãâêíóôõúç]+\b",
        mask_name,
        text,
    )


def read_input_workbook(path):
    sheets = pd.read_excel(path, sheet_name=None)
    sheets = {name: frame.fillna("") for name, frame in sheets.items()}
    if not sheets:
        return {"Sheet1": pd.DataFrame({"keyword": []})}, pd.DataFrame({"keyword": []})
    first = next(iter(sheets.values()))
    known = {"keyword", "listing_url", "detail_url"}
    normalized = {str(c).strip().lower() for c in first.columns}
    if normalized.intersection(known):
        frame = first.rename(columns={c: str(c).strip().lower() for c in first.columns}).copy()
    else:
        raw = pd.read_excel(path, header=None).fillna("")
        frame = pd.DataFrame({"keyword": raw.iloc[:, 0] if not raw.empty else []})
    for column in known:
        if column not in frame.columns:
            frame[column] = ""
    return sheets, frame.fillna("")


def detect_anti_bot_signals(status, html):
    markers = (
        "cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...",
        "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold",
        "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada",
        "akamai", "shieldsquare", "security challenge", "pardon our interruption",
        "potential security risk", "detected automated scripts", "additional security check",
        "i am human", "hcaptcha", "access denied", "robot or human"
    )
    text = (html or "").lower()
    found = [marker for marker in markers if marker in text]
    return status in {403, 429} or bool(found), found


class BotProtectionError(RuntimeError):
    pass


async def dismiss_cookie_banner(page):
    selectors = [
        "button:has-text('Accept')", "button:has-text('Aceitar')",
        "button:has-text('Concordo')", "[id*='cookie' i] button",
        "[class*='cookie' i] button"
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if await loc.is_visible(timeout=600):
                await loc.click(timeout=1500)
                return
        except Exception:
            continue


async def safe_click(page, locator, selector, flags, url):
    try:
        await locator.click(timeout=5000)
        return True
    except Exception as exc:
        log(f"Click blocked on locator '{selector}': {exc}")
        flags.append({"Type": "Click Intercepted", "URL": url,
                      "Message": f"Element {selector} blocked: {str(exc)[:200]}"})
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


async def verify_response(response, page, flags, url):
    html = await page.content()
    status = response.status if response else 0
    blocked, markers = detect_anti_bot_signals(status, html)
    if blocked:
        message = ", ".join(markers) or str(status)
        flags.append({"Type": "Blocked by bot protection", "URL": url, "Message": message})
        raise BotProtectionError(message)
    return html


def extract_table_rows(soup):
    table = soup.select_one(RESULTS_TABLE)
    if table is None:
        return []
    header_cells = table.select("thead th")
    headers = [clean(cell.get_text(" ", strip=True)) for cell in header_cells]
    if not headers:
        first = table.find("tr")
        headers = [clean(cell.get_text(" ", strip=True)) for cell in first.find_all(["th", "td"])] if first else []
    rows = []
    for tr in table.select("tbody > tr"):
        if tr.select_one("td[colspan='8']") or "Nenhum registo" in tr.get_text(" ", strip=True):
            continue
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
        row = {}
        for index, value in enumerate(values):
            row[headers[index] if index < len(headers) else f"column_{index + 1}"] = value
        anchors = tr.find_all("a", href=True)
        if anchors:
            detail = next((a for a in anchors if "RNET.aspx" in a.get("href", "")), anchors[0])
            row["__detail_href"] = detail.get("href", "")
        rows.append(row)
    return rows


def mapped_row(row):
    result = {}
    for key, value in row.items():
        if key.startswith("__"):
            continue
        low = clean(key).lower()
        if low in {"nº", "nº.", "n", "no", "n.º"}:
            result["Nº"] = value
        elif low == "data":
            result["Data"] = value
        elif "nome" in low:
            result["Nome"] = value
        elif "tipologia" in low:
            result["Tipologia"] = value
        elif "capacidade" in low or "capacity" in low:
            result["Capacidade"] = value
        elif "local" in low:
            result["Localização"] = value
        elif "certifica" in low:
            result["Certificações"] = value
    for column in LIST_COLUMNS:
        result.setdefault(column, "")
    return result


def label_value(text, labels):
    escaped = "|".join(re.escape(label) for label in labels)
    match = re.search(r"(?:" + escaped + r")\s*[:\-]?\s*([^\n]+)", text, re.I)
    return clean(match.group(1)) if match else ""


def scoped_price(soup):
    selectors = [
        "[itemprop='price']", ".price", ".product-price", ".current-price",
        ".buy-box", ".buybox", "[class*='price' i]"
    ]
    currency = re.compile(r"(?i)(?:[\$£€₹]|USD|CAD|EUR|GBP)\s*\d{1,5}(?:[.,]\d{2})?")
    for selector in selectors:
        for element in soup.select(selector):
            match = currency.search(element.get_text(" ", strip=True))
            if match:
                return clean(match.group(0))
    return ""


def extract_detail(soup):
    text = soup_text(soup)
    heading = soup.find(["h1", "h2"])
    heading_text = clean(heading.get_text(" ", strip=True)) if heading else ""
    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if meta:
        description = clean(meta.get("content", ""))
    reviews = []
    for element in soup.select("[class*='review' i], [id*='review' i]"):
        reviews.append(element.get_text(" ", strip=True))
    return {
        "RNET nº": label_value(text, ["RNET nº", "RNET n.º", "RNET"]),
        "Data de Registo": label_value(text, ["Registado em", "Data de Registo"]),
        "Tipologia Completa": label_value(text, ["Tipologia Completa", "Tipologia"]),
        "Morada e Código Postal": label_value(text, ["Morada e Código Postal", "Morada", "Endereço"]),
        "Contactos": label_value(text, ["Contactos"]),
        "Marca Comercial": label_value(text, ["Marca Comercial", "Marca"]),
        "Entidade Exploradora": label_value(text, ["Entidade Exploradora", "Entidade exploradora"]),
        "NIF / NIPC": label_value(text, ["NIF", "NIPC"]),
        "Detail Heading": heading_text,
        "Title": heading_text,
        "Price": scoped_price(soup),
        "Description": description,
        "Modules": "",
        "Categories": "",
        "Images": [urljoin("", image.get("src", "")) for image in soup.select("img[src]")],
        "__reviews": reviews,
    }


def safe_filename(value):
    value = clean(value) or "export"
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).strip(" .")
    return (value[:150] or "export") + ".xlsx"


async def export_for_keyword(page, keyword, flags):
    selectors = [
        "a:has-text('Exportar detalhe registos')",
        "a:has(span:has-text('Exportar detalhe registos'))",
        "text=Exportar detalhe registos"
    ]
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            if await loc.is_visible(timeout=1200):
                try:
                    async with page.expect_download(timeout=6000) as info:
                        await loc.evaluate("el => el.click()")
                    download = await info.value
                    await download.save_as(safe_filename(keyword))
                    log(f"Saved export for keyword '{keyword}'")
                    return
                except Exception as dl_err:
                    log(f"Export download note: {dl_err}")
        except Exception:
            continue
    flags.append({"Type": "Missing field", "URL": page.url,
                  "Message": "Exportar detalhe registos was not observed or did not download."})


async def inspect_detail(context, url, source_url, mask_reviews, flags):
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await dismiss_cookie_banner(page)
        html = await verify_response(response, page, flags, url)
        data = extract_detail(BeautifulSoup(html, "html.parser"))
        if mask_reviews:
            data["Reviews"] = mask_review_text("\n".join(data.pop("__reviews", [])))
        else:
            data.pop("__reviews", None)
        data.update({"product_id": canonical_id(url), "product_url": url,
                     "source_listing_url": source_url})
        for field in DETAIL_COLUMNS:
            if not data.get(field):
                flags.append({"Type": "Missing field", "URL": url, "Message": field})
        return data
    except BotProtectionError:
        raise
    except Exception as exc:
        flags.append({"Type": "Error", "URL": url, "Message": str(exc)[:500]})
        return None
    finally:
        await page.close()


async def wait_for_results(page):
    await page.wait_for_selector("table.TableRecords:not(.Empty)", timeout=20000)
    try:
        await page.wait_for_function(
            """() => /\\d+\\s*(?:a|de)\\s*\\d+|\\d+\\s*registos/i.test(document.body.innerText)""",
            timeout=15000,
        )
    except PlaywrightTimeoutError:
        log("Record-count text was not observed; continuing with the verified results table.")
    await page.wait_for_timeout(500)


async def reset_search(page, flags):
    clear = page.locator(CLEAR_BUTTON)
    if await clear.count() and await clear.is_visible(timeout=1000):
        await safe_click(page, clear, CLEAR_BUTTON, flags, page.url)
        await page.wait_for_timeout(500)
    else:
        await page.locator(SEARCH_INPUT).fill("")


async def crawl_listing(context, url, keyword, max_products, mask_reviews, records, flags, metrics):
    page = await context.new_page()
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await dismiss_cookie_banner(page)
        await verify_response(response, page, flags, url)
        if keyword:
            await page.locator(SEARCH_INPUT).fill(keyword)
        if not await safe_click(page, page.locator(SEARCH_BUTTON), SEARCH_BUTTON, flags, page.url):
            return
        await wait_for_results(page)
        await export_for_keyword(page, keyword or "export", flags)
        seen = set()
        while True:
            rows = extract_table_rows(BeautifulSoup(await page.content(), "html.parser"))
            log(f"Listing page yielded {len(rows)} rows for keyword '{keyword}'.")
            for raw in rows:
                if max_products is not None and len(records) >= max_products:
                    return
                item = mapped_row(raw)
                href = clean(raw.get("__detail_href", ""))
                detail_url = urljoin(page.url, href) if href else ""
                if not detail_url or detail_url in seen:
                    continue
                seen.add(detail_url)
                metrics["links_discovered"] += 1
                detail = await inspect_detail(context, detail_url, url, mask_reviews, flags)
                metrics["links_processed"] += 1
                if detail:
                    record = {**item, **detail}
                    record.update({"product_id": canonical_id(detail_url), "product_url": detail_url,
                                   "source_listing_url": url})
                    records.append(record)
                    metrics["records_extracted"] += 1
            next_loc = page.locator("a", has_text=re.compile(r"próximo|proximo|next", re.I)).first
            if not await next_loc.is_visible(timeout=800):
                break
            classes = (await next_loc.get_attribute("class") or "").lower()
            if "disabled" in classes or await next_loc.get_attribute("aria-disabled") == "true":
                break
            before = await page.locator(RESULTS_TABLE).inner_text()
            if not await safe_click(page, next_loc, "a:has-text('próximo')", flags, page.url):
                break
            try:
                await page.wait_for_function(
                    "before => document.querySelector(arguments[0])?.innerText !== before",
                    arg=before,
                    timeout=15000,
                )
            except Exception:
                await page.wait_for_timeout(1500)
    finally:
        await page.close()


async def run(args):
    input_sheets, input_df = read_input_workbook(args.input)
    records, flags = [], []
    metrics = {"categories_discovered": 0, "links_discovered": 0,
               "links_processed": 0, "records_extracted": 0}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            accept_downloads=True,
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };"
        )
        try:
            for _, row in input_df.iterrows():
                if args.max_products is not None and len(records) >= args.max_products:
                    break
                keyword = value_from_row(row, "keyword")
                listing_url = value_from_row(row, "listing_url") or TARGET_URL
                detail_url = value_from_row(row, "detail_url")
                metrics["categories_discovered"] += 1
                if detail_url:
                    metrics["links_discovered"] += 1
                    detail = await inspect_detail(context, detail_url, "DIRECT_INPUT", args.mask_reviews, flags)
                    metrics["links_processed"] += 1
                    if detail:
                        records.append(detail)
                        metrics["records_extracted"] += 1
                else:
                    log(f"Crawling listing URL {listing_url} with keyword '{keyword}'.")
                    await crawl_listing(context, listing_url, keyword, args.max_products,
                                        args.mask_reviews, records, flags, metrics)
                if keyword and len(records) < (args.max_products or float("inf")):
                    page = await context.new_page()
                    try:
                        response = await page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
                        await dismiss_cookie_banner(page)
                        await verify_response(response, page, flags, listing_url)
                        await reset_search(page, flags)
                    finally:
                        await page.close()
        finally:
            await browser.close()
    log(
        f"Crawl Funnel: {metrics['categories_discovered']} categories, "
        f"{metrics['links_discovered']} links discovered, {metrics['links_processed']} processed, "
        f"{metrics['records_extracted']} extracted."
    )
    return input_sheets, records, flags, metrics


async def diagnostic(args):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, args=["--no-sandbox"])
        page = await browser.new_page()
        try:
            response = await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
            await dismiss_cookie_banner(page)
            status = response.status if response else 0
            title = await page.title()
            await page.screenshot(path="diagnostic_screenshot.png", full_page=True)
            log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
        finally:
            await browser.close()


def write_output(path, input_sheets, records, flags):
    existing = input_sheets.get("Extracted Data", pd.DataFrame())
    preserved_columns = [str(c) for c in existing.columns] if not existing.empty else []
    columns = []
    for column in ["product_id", "product_url", "source_listing_url"] + LIST_COLUMNS + DETAIL_COLUMNS + LEGACY_COLUMNS + ["Reviews"] + preserved_columns:
        if column not in columns:
            columns.append(column)
    data = [{column: sanitize_cell_value(record.get(column, "")) for column in columns} for record in records]
    flag_rows = [{"Type": sanitize_cell_value(item.get("Type")),
                  "URL": sanitize_cell_value(item.get("URL")),
                  "Message": sanitize_cell_value(item.get("Message"))} for item in flags]
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame(data, columns=columns).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame(flag_rows, columns=["Type", "URL", "Message"]).to_excel(writer, sheet_name="Flags", index=False)
        for name, frame in input_sheets.items():
            if name in {"Extracted Data", "Flags"}:
                continue
            mapped_frame = frame.map(sanitize_cell_value) if hasattr(frame, "map") else frame.applymap(sanitize_cell_value)
            mapped_frame.to_excel(writer, sheet_name=str(name)[:31], index=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--visible", dest="headless", action="store_false")
    parser.add_argument("--mask-reviews", action="store_true", default=True)
    parser.add_argument("--no-mask-reviews", dest="mask_reviews", action="store_false")
    args = parser.parse_args()
    try:
        input_sheets, records, flags, metrics = asyncio.run(run(args))
        if not records:
            asyncio.run(diagnostic(args))
            log("ERROR: Zero product records were extracted. Aborting.")
            sys.exit(1)
        write_output(args.output, input_sheets, records, flags)
        log(f"Wrote {len(records)} records to {args.output}")
    except BotProtectionError:
        log("Blocked by bot protection. Stopped without retrying.")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()