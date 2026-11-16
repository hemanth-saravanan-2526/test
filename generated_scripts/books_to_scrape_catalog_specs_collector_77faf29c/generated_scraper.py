import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup

TARGET_URL = 'https://books.toscrape.com/catalogue/category/books/travel_2/index.html'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36'

FLAGS = []


def log(message):
    print(f'[{datetime.now(timezone.utc).isoformat()}] {message}', flush=True)


def add_flag(flag_type, url, message):
    FLAGS.append({'Type': flag_type, 'URL': url or '', 'Message': message})
    log(f'{flag_type}: {message}')


def sanitize_cell_value(val, max_len=32000):
    if val is None:
        return ''
    if isinstance(val, (dict, list)):
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val)
    if len(s) > max_len:
        return s[:max_len - 35] + '... [TRUNCATED_EXCEL_LIMIT]'
    return s


def clean(value):
    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):
        return ""
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    value = str(value).strip()
    return '' if value.lower() in {'nan', 'none', 'null'} else value


def value_from_row(row, name):
    return clean(row.get(name, ''))


def safe_title(soup):
    return ((soup.title.get_text(strip=True) if getattr(soup, "title", None) else "") if getattr(soup, "title", None) else "") if getattr(soup, 'title', None) else ''


def detect_anti_bot_signals(response):
    markers = (
        'cloudflare turnstile', 'cf-browser-verification', 'ray id:', 'just a moment...',
        'datadome', 'perimeterx', 'px-captcha', 'distil networks', 'press & hold',
        'incapsula', '_incapsula_resource', 'imperva', 'incident id', 'kasada',
        'akamai', 'shieldsquare', 'security challenge', 'pardon our interruption',
        'potential security risk', 'detected automated scripts', 'additional security check',
        'i am human', 'hcaptcha', 'access denied', 'robot or human'
    )
    text = (response.text or '').lower()
    found = [marker for marker in markers if marker in text]
    if response.status_code in {403, 429} or found:
        message = f'HTTP {response.status_code}; markers: {", ".join(found) or "none"}'
        add_flag('Blocked by bot protection', response.url, message)
        log('Bot protection detected; stopping without retries.')
        return True
    return False


def dismiss_cookie_banner(page):
    return None


def mask_reviewer_pii(text):
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '***@***.***', text)

    def mask_name(match):
        parts = match.group(0).split()
        return ' '.join(f'{part[0]}***' if i == 0 else f'{part[0]}.' for i, part in enumerate(parts))

    return re.sub(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', mask_name, text)


def canonical_product_id(url, upc=''):
    # One dataset-wide strategy: the official UPC is the primary identifier.
    # A URL-derived fallback is used only when the source page has no UPC.
    if upc:
        return upc
    path = urlparse(url).path.rstrip('/').split('/')[-1]
    return re.sub(r'\.html?$', '', path, flags=re.I)


def extract_price(container):
    if not container:
        return ''
    text = container.get_text(' ', strip=True).replace('\xa0', ' ')
    match = re.search(r'(?i)(?:[£$€₹]|USD|CAD|EUR|GBP)\s*(\d{1,5}(?:[.,]\d{2})?)', text)
    if not match:
        return ''
    prefix = text[match.start():match.end()]
    return re.sub(r'\s+', ' ', prefix).strip()


def get_specifications(soup):
    specs = {}
    table = soup.select_one('table.table-striped')
    if not table:
        return specs
    for row in table.select('tr'):
        cells = row.select('th, td')
        if len(cells) >= 2:
            key = cells[0].get_text(' ', strip=True)
            value = cells[1].get_text(' ', strip=True)
            if key:
                specs[key] = value
    return specs


def extract_detail(soup, url, source_listing_url, mask_reviews=True):
    container = soup.select_one('article.product_page') or soup
    title_node = container.select_one('h1')
    title = title_node.get_text(' ', strip=True) if title_node else ''
    price_node = container.select_one('.product_main .price_color') or container.select_one('.price_color')
    price = extract_price(price_node)
    availability_node = container.select_one('.availability')
    availability = availability_node.get_text(' ', strip=True) if availability_node else ''
    rating_node = container.select_one('.star-rating')
    rating = ''
    if rating_node:
        classes = rating_node.get('class', [])
        rating = next((item for item in classes if item.lower() != 'star-rating'), '')
        if not rating:
            rating = rating_node.get_text(' ', strip=True)
    heading = soup.find(id='product_description')
    description = ''
    if heading:
        paragraph = heading.find_next('p')
        description = paragraph.get_text(' ', strip=True) if paragraph else ''
    description = description[:300]
    specs = get_specifications(soup)
    upc = specs.get('UPC', '')
    if not title:
        add_flag('Missing field', url, 'title')
    if not price:
        add_flag('Missing field', url, 'price')
    if not availability:
        add_flag('Missing field', url, 'availability')
    if not upc:
        add_flag('Low-confidence extraction', url, 'UPC missing; URL slug fallback used for product_id')
    if 'in stock' not in availability.lower():
        return None
    canonical = urljoin(url, soup.select_one('link[rel="canonical"]')['href']) if soup.select_one('link[rel="canonical"]') and soup.select_one('link[rel="canonical"]').get('href') else url
    record = {
        'product_id': canonical_product_id(canonical, upc),
        'title': title,
        'price': price,
        'availability': availability,
        'rating': rating,
        'product_url': canonical,
        'source_listing_url': source_listing_url,
        'description': mask_reviewer_pii(description) if mask_reviews else description,
    }
    return record


def fetch(session, url):
    log(f'Fetching {url}')
    response = session.get(url, timeout=30)
    if detect_anti_bot_signals(response):
        raise RuntimeError('Blocked by bot protection')
    response.raise_for_status()
    return response


def discover_links(session, listing_url, keyword=''):
    links = []
    visited_pages = set()
    current = listing_url
    while current and current not in visited_pages:
        visited_pages.add(current)
        response = fetch(session, current)
        soup = BeautifulSoup(response.text, 'html.parser')
        for card in soup.select('article.product_pod'):
            anchor = card.select_one('h3 a')
            if not anchor or not anchor.get('href'):
                continue
            detail = urljoin(response.url, anchor['href'])
            card_title = anchor.get('title', '') or anchor.get_text(' ', strip=True)
            haystack = f'{card_title} {urlparse(detail).path}'.lower()
            if keyword and keyword.lower() not in haystack:
                continue
            stock = card.select_one('.instock')
            if stock and 'in stock' not in stock.get_text(' ', strip=True).lower():
                continue
            if detail not in links:
                links.append(detail)
        next_anchor = soup.select_one('.next a')
        current = urljoin(response.url, next_anchor['href']) if next_anchor and next_anchor.get('href') else None
    return links


async def diagnostic_screenshot(url, status, html, headless=True):
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless, args=['--disable-blink-features=AutomationControlled', '--no-sandbox'])
            context = await browser.new_context(user_agent=USER_AGENT, viewport={'width': 1920, 'height': 1080})
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
            page = await context.new_page()
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await dismiss_cookie_banner(page)
            await page.screenshot(path='diagnostic_screenshot.png', full_page=True)
            title = await page.title()
            await browser.close()
            log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")
    except Exception as exc:
        safe = BeautifulSoup(html or '', 'html.parser')
        title = safe_title(safe)
        log(f"No product records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png; screenshot error: {exc}")


def click_blocked(selector, url, exc):
    log(f"Click blocked on locator '{selector}': {exc}")
    add_flag('Click Intercepted', url, f'Element {selector} blocked: {str(exc)[:200]}')


def export_results(records, output):
    columns = ['product_id', 'title', 'price', 'availability', 'rating', 'product_url', 'source_listing_url', 'description']
    data = [{key: sanitize_cell_value(row.get(key, '')) for key in columns} for row in records]
    flags = [{key: sanitize_cell_value(row.get(key, '')) for key in ('Type', 'URL', 'Message')} for row in FLAGS]
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(data, columns=columns).to_excel(writer, sheet_name='Extracted Data', index=False)
        pd.DataFrame(flags, columns=['Type', 'URL', 'Message']).to_excel(writer, sheet_name='Flags', index=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', default='input.xlsx')
    parser.add_argument('--output', default='output.xlsx')
    parser.add_argument('--max-products', type=int, default=None)
    parser.add_argument('--headless', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--mask-reviews', action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()
    try:
        input_df = pd.read_excel(args.input).fillna('')
    except FileNotFoundError:
        input_df = pd.DataFrame([{}])
        log(f'Input file {args.input} not found; using default target.')
    except Exception as exc:
        add_flag('Error', args.input, f'Could not read input workbook: {exc}')
        input_df = pd.DataFrame([{}])
    if input_df.empty:
        input_df = pd.DataFrame([{}])
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'})
    records = []
    links_discovered = 0
    links_processed = 0
    categories = 0
    seen = set()
    for _, row in input_df.iterrows():
        keyword = value_from_row(row, 'keyword')
        listing_url = value_from_row(row, 'listing_url') or TARGET_URL
        detail_url = value_from_row(row, 'detail_url')
        try:
            if detail_url:
                targets = [(detail_url, 'DIRECT_INPUT')]
            else:
                categories += 1
                targets = [(url, listing_url) for url in discover_links(session, listing_url, keyword)]
            links_discovered += len(targets)
            for url, source in targets:
                if url in seen:
                    continue
                seen.add(url)
                if args.max_products is not None and len(records) >= args.max_products:
                    break
                response = fetch(session, url)
                soup = BeautifulSoup(response.text, 'html.parser')
                record = extract_detail(soup, response.url, source, args.mask_reviews)
                links_processed += 1
                if record:
                    records.append(record)
        except RuntimeError as exc:
            add_flag('Error', detail_url or listing_url, str(exc))
            break
        except Exception as exc:
            add_flag('Error', detail_url or listing_url, str(exc))
    log(f'Crawl Funnel: {categories} categories, {links_discovered} links discovered, {links_processed} processed, {len(records)} extracted.')
    if not records:
        diagnostic_url = value_from_row(input_df.iloc[0], 'detail_url') or value_from_row(input_df.iloc[0], 'listing_url') or TARGET_URL
        try:
            response = session.get(diagnostic_url, timeout=30)
            asyncio.run(diagnostic_screenshot(diagnostic_url, response.status_code, response.text, args.headless))
        except Exception as exc:
            add_flag('Error', diagnostic_url, f'Diagnostic capture failed: {exc}')
        log('ERROR: Zero product records were extracted. Aborting.')
        export_results([], args.output)
        sys.exit(1)
    export_results(records, args.output)
    log(f'Wrote {len(records)} records to {args.output}')


if __name__ == '__main__':
    main()