import argparse
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

TARGET_URL = "https://news.ycombinator.com/jobs"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}")


def clean(value: Any) -> str:
    if value is None:
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


def get_proxy() -> None:
    return None


def solve_captcha(*args: Any, **kwargs: Any) -> None:
    raise NotImplementedError("CAPTCHA solving is intentionally not implemented")


def dismiss_cookie_banner(page: Any) -> None:
    async def _dismiss() -> None:
        patterns = ["Accept all", "Accept", "I agree", "Allow all", "Agree", "Got it"]
        for text in patterns:
            try:
                locator = page.get_by_role("button", name=re.compile(rf"^{re.escape(text)}$", re.I))
                if await locator.count():
                    await locator.first.click(timeout=1000)
                    return
            except Exception:
                pass

    return _dismiss()


def detect_anti_bot_signals(
    response: Optional[requests.Response] = None,
    page_title: str = "",
    body: str = "",
) -> List[str]:
    signals: List[str] = []
    status = response.status_code if response is not None else None
    if status in {403, 429}:
        signals.append(f"HTTP anti-bot status {status}")
    haystack = f"{page_title}\n{body}".lower()
    markers = {
        "captcha": "CAPTCHA marker",
        "cloudflare": "Cloudflare marker",
        "perimeterx": "PerimeterX marker",
        "datadome": "DataDome marker",
    }
    for marker, description in markers.items():
        if marker in haystack:
            signals.append(description)
    if body and len(body.strip()) < 200:
        signals.append("Unusually short response body")
    return signals


def model_code_from_url(url: str) -> str:
    query = parse_qs(urlparse(url).query)
    for key in ("modelCode", "sku"):
        values = query.get(key, [])
        if values and clean(values[0]):
            return clean(values[0])
    return ""


def normalize_currency(value: str) -> str:
    text = clean(value)
    match = re.search(r"(?:INR|USD|EUR|GBP)\s*[\d,]+(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\b", text, re.I)
    return match.group(0) if match else ""


def is_external(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() not in {
        "news.ycombinator.com",
        "www.news.ycombinator.com",
    }


def company_from_title(title: str) -> str:
    value = clean(title)
    value = re.split(r"\s+\((?:YC|Y Combinator)\b", value, maxsplit=1, flags=re.I)[0]
    value = re.split(r"\s+is hiring\b|\s+are hiring\b|\s+hiring\b", value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"\s+[-|:]\s*$", "", value).strip()
    return value


def location_from_text(title: str, body: str) -> str:
    text = f"{clean(title)} {clean(body)}"
    patterns = [
        r"\b(remote(?:[- ]friendly)?(?:,?\s+[^.;|]+)?|hybrid(?:,?\s+[^.;|]+)?|on[- ]site(?:,?\s+[^.;|]+)?)",
        r"\b(SF(?: and remote)?|San Francisco(?: and remote)?|New York(?: City)?|London|Berlin|Toronto)\b",
        r"\b(remote,?\s+[^.;|]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return clean(match.group(1))
    return ""


def keyword_matches(keyword: str, title: str, body: str) -> bool:
    keyword = clean(keyword)
    if not keyword:
        return True
    return keyword.casefold() in f"{title}\n{body}".casefold()


def age_for_row(row: Any) -> str:
    age = row.select_one(".subtext .age")
    if age:
        return clean(age.get_text(" ", strip=True))
    sibling = row.find_next_sibling("tr")
    if sibling:
        age = sibling.select_one(".subtext .age, .age")
        if age:
            return clean(age.get_text(" ", strip=True))
    return ""


def first_external_link(soup: BeautifulSoup, base_url: str) -> str:
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, anchor.get("href", ""))
        if is_external(href):
            return href
    return ""


def parse_detail(html: str, source_url: str) -> Dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    title_node = soup.select_one("table.fatitem .titleline, .fatitem .titleline, .athing .titleline")
    title = clean(title_node.get_text(" ", strip=True)) if title_node else ""
    if not title:
        title_node = soup.select_one("title, h1")
        title = clean(title_node.get_text(" ", strip=True)) if title_node else ""
    age_node = soup.select_one(".subtext .age")
    posted = clean(age_node.get_text(" ", strip=True)) if age_node else ""
    body_node = soup.select_one(".toptext")
    body = body_node.get_text("\n", strip=True) if body_node else soup.get_text("\n", strip=True)
    application_url = first_external_link(soup, source_url)
    return {
        "Job Title": title,
        "Company Name": company_from_title(title),
        "Location / Remote Status": location_from_text(title, body),
        "Posted Time": posted,
        "Application URL": application_url,
        "Source URL": source_url,
        "_body": body,
    }


class Scraper:
    def __init__(self, headless: bool, max_products: Optional[int]) -> None:
        self.headless = headless
        self.max_products = max_products
        self.flags: List[Dict[str, str]] = []
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

    def flag(self, kind: str, message: str, source_url: str = "") -> None:
        self.flags.append({"Type": kind, "Message": message, "Source URL": source_url})

    def fetch(self, url: str) -> Tuple[str, bool]:
        log(f"Fetching {url}")
        try:
            response = self.session.get(url, timeout=30)
            body = response.text
            signals = detect_anti_bot_signals(response=response, body=body)
            for signal in signals:
                self.flag("Anti-bot", signal, url)
            if response.status_code >= 400:
                self.flag("HTTP error", f"HTTP status {response.status_code}", url)
                return body, False
            return body, True
        except requests.RequestException as exc:
            self.flag("Error", str(exc), url)
            return "", False

    async def browser_fetch(self, url: str) -> str:
        log(f"Using Playwright browser fallback for {url}")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page(user_agent=USER_AGENT)
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                await dismiss_cookie_banner(page)
                html = await page.content()
                title = await page.title()
                status = response.status if response else None
                signals = detect_anti_bot_signals(page_title=title, body=html)
                if status in {403, 429}:
                    signals.append(f"Browser HTTP anti-bot status {status}")
                for signal in signals:
                    self.flag("Anti-bot", signal, url)
                return html
            except Exception as exc:
                self.flag("Browser error", str(exc), url)
                return ""
            finally:
                await browser.close()

    def fetch_with_fallback(self, url: str) -> str:
        html, ok = self.fetch(url)
        if ok and html.strip():
            return html
        return asyncio.run(self.browser_fetch(url))

    def scrape_detail(self, url: str, keyword: str = "") -> Optional[Dict[str, str]]:
        html = self.fetch_with_fallback(url)
        if not html:
            return None
        item = parse_detail(html, url)
        body = item.pop("_body", "")
        if not keyword_matches(keyword, item["Job Title"], body):
            return None
        if not item["Job Title"]:
            self.flag("Low confidence", "Job title was not found", url)
        if not item["Application URL"]:
            self.flag("Low confidence", "No external application URL was found on detail page", url)
        return item

    def scrape_listing(self, listing_url: str, keyword: str = "") -> List[Dict[str, str]]:
        html = self.fetch_with_fallback(listing_url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        results: List[Dict[str, str]] = []
        for row in soup.select("tr.athing"):
            link = row.select_one("a:first-of-type")
            if not link:
                continue
            title = clean(link.get_text(" ", strip=True))
            href = urljoin(listing_url, link.get("href", ""))
            posted = age_for_row(row)
            body = ""
            detail = None
            if href.startswith("https://news.ycombinator.com/item?") or href.startswith("http://news.ycombinator.com/item?"):
                detail = self.scrape_detail(href, keyword)
                if detail:
                    body = detail.get("Location / Remote Status", "")
            elif not keyword_matches(keyword, title, body):
                continue
            if keyword and detail is None and not keyword_matches(keyword, title, body):
                continue
            if detail:
                item = detail
                if not item["Posted Time"]:
                    item["Posted Time"] = posted
            else:
                item = {
                    "Job Title": title,
                    "Company Name": company_from_title(title),
                    "Location / Remote Status": location_from_text(title, ""),
                    "Posted Time": posted,
                    "Application URL": href if is_external(href) else "",
                    "Source URL": listing_url,
                }
                if not item["Application URL"]:
                    self.flag("Low confidence", "Listing link is not an external application URL", href)
            results.append(item)
            if self.max_products and len(results) >= self.max_products:
                break
        return results


def value_from_row(row: pd.Series, column: str) -> str:
    return clean(row[column]) if column in row.index else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape Hacker News job postings")
    parser.add_argument("--input", default="input.xlsx")
    parser.add_argument("--output", default="output.xlsx")
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    log("Reading input workbook")
    try:
        input_df = pd.read_excel(args.input).fillna("")
    except FileNotFoundError:
        input_df = pd.DataFrame([{"listing_url": TARGET_URL}])
    except Exception as exc:
        log(f"Input workbook error: {exc}")
        input_df = pd.DataFrame([{"listing_url": TARGET_URL}])

    scraper = Scraper(args.headless, args.max_products)
    rows: List[Dict[str, str]] = []
    if input_df.empty:
        input_df = pd.DataFrame([{"listing_url": TARGET_URL}])

    for _, input_row in input_df.iterrows():
        keyword = value_from_row(input_row, "keyword")
        detail_url = value_from_row(input_row, "detail_url")
        listing_url = value_from_row(input_row, "listing_url") or TARGET_URL
        if detail_url:
            log(f"Scraping independent detail URL {detail_url}")
            item = scraper.scrape_detail(detail_url, keyword)
            if item:
                rows.append(item)
        else:
            rows.extend(scraper.scrape_listing(listing_url, keyword))
        if args.max_products and len(rows) >= args.max_products:
            rows = rows[: args.max_products]
            break

    columns = [
        "Job Title",
        "Company Name",
        "Location / Remote Status",
        "Posted Time",
        "Application URL",
        "Source URL",
    ]
    output_df = pd.DataFrame(rows, columns=columns).fillna("")
    flags_df = pd.DataFrame(scraper.flags, columns=["Type", "Message", "Source URL"]).fillna("")
    log(f"Writing {len(output_df)} extracted rows and {len(flags_df)} flags")
    with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
        output_df.to_excel(writer, sheet_name="Extracted Data", index=False)
        flags_df.to_excel(writer, sheet_name="Flags", index=False)
    log("Run complete")


if __name__ == "__main__":
    main()