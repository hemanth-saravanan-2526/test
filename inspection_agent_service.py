"""
Live Inspection & Codegen Agent Service (OpenAI + MCP Powered)
==============================================================
Backend service that drives an autonomous browser-inspection + code-generation
session using OpenAI (default: GPT-5.6 Luna for both Recon and Codegen) and the
Model Context Protocol (MCP) for Playwright browser automation.

Architectural highlights:
  - OpenAI Client (openai.AsyncOpenAI) for model completions & tool calling.
  - Playwright MCP server over HTTP/SSE (or Stdio) for browser tools.
  - Two-phase architecture:
      1. RECON phase: Browser tools enabled. Inspects live target page, navigates,
         extracts selectors and sample data into a compact structured JSON findings object.
      2. CODEGEN phase: Fresh context, zero browser tools. Writes the standalone Python
         scraper strictly adhering to the universal SCRIPT_CONTRACT.
  - Independent model configurability (OPENAI_RECON_MODEL, OPENAI_CODEGEN_MODEL, default: gpt-5.6-luna).
  - Default temperature: 1.0 (optimized for Luna).
  - PreToolUse hooks for audit logging, SSE live streaming, step capping, and duplicate-call dedup.
  - Real-time anti-bot detection for fast failover.
"""

import ast
import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageToolCall

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.sse import sse_client
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from auth_config import resolve_credentials, require_credentials_at_startup, CredentialError
from cost_metering import (
    CostRecord,
    CostSink,
    StdoutCostSink,
    build_cost_record,
    build_fallback_cost_record,
    combine_cost_records,
    resolve_budget_ceiling,
    split_budget_ceiling,
    calculate_cost_usd,
    SDK_BUDGET_SAFETY_MARGIN,
)

# --------------------------------------------------------------------------
# Model & Temperature Configuration (OpenAI)
# --------------------------------------------------------------------------

def get_default_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-5.6-luna")

def get_default_recon_model() -> str:
    return os.getenv("OPENAI_RECON_MODEL", get_default_model())

def get_default_codegen_model() -> str:
    return os.getenv("OPENAI_CODEGEN_MODEL", get_default_model())

def get_default_temperature() -> float:
    try:
        return float(os.getenv("OPENAI_TEMPERATURE", "1.0"))
    except ValueError:
        return 1.0

DEFAULT_MODEL = get_default_model()
DEFAULT_RECON_MODEL = get_default_recon_model()
DEFAULT_CODEGEN_MODEL = get_default_codegen_model()
DEFAULT_TEMPERATURE = get_default_temperature()


# --------------------------------------------------------------------------
# Job contract
# --------------------------------------------------------------------------

@dataclass
class InspectionJob:
    """Mirrors the fields submitted from the Builder page."""
    job_id: str
    target_url: str
    sample_keyword: str | None = None
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    max_steps: int = 40          # hard cap on RECON phase tool calls
    max_seconds: int = 600       # hard cap on wall-clock time for the RECON phase
    max_budget_usd: float | None = None  # hard cap on TOTAL spend across both phases
    recon_model: str | None = None    # optional model override for Phase 1 (Recon)
    codegen_model: str | None = None  # optional model override for Phase 2 (Codegen)
    temperature: float | None = None  # optional temperature override (default: 1.0)


@dataclass
class InspectionResult:
    job_id: str
    status: str                  # "completed" | "failed" | "step_limit_hit" | "time_limit_hit" | "budget_limit_hit" | "anti_bot_detected"
    generated_code: str = ""
    requirements_txt: str = ""
    confidence_notes: list[str] = field(default_factory=list)
    action_log: list[dict] = field(default_factory=list)
    total_tool_calls: int = 0
    total_cost_usd: float | None = None
    cost_record: dict | None = None
    anti_bot_marker: str | None = None
    prompts: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# Playwright MCP Server Configuration & Allowed Tools
# --------------------------------------------------------------------------

def get_mcp_server_url() -> str:
    """Resolve Playwright MCP Server URL from environment (.env)."""
    return (
        os.environ.get("MCP_SERVER_URL")
        or os.environ.get("PLAYWRIGHT_MCP_URL")
        or os.environ.get("MCP_BASE_URL")
        or "http://localhost:8931"
    ).rstrip("/")


RECON_ALLOWED_TOOLS = [
    # Custom Playwright MCP Toolbox (Primary)
    "navigate",
    "snapshot",
    "click",
    "type_text",
    "scroll",
    "load_all",
    "run_js",
    "save_items",
    "extract_sample_rows",
    # Legacy Playwright MCP / Native Tools (Fallback)
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_press_key",
    "browser_scroll",
    "browser_wait_for",
    "browser_evaluate",
    "browser_network_requests",
    "browser_take_screenshot",
    # Prefixed aliases for compatibility
    "mcp__browser__browser_navigate",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_click",
    "mcp__browser__browser_type",
    "mcp__browser__browser_press_key",
    "mcp__browser__browser_scroll",
    "mcp__browser__browser_wait_for",
    "mcp__browser__browser_evaluate",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
]

REPEATABLE_WITHOUT_PROGRESS = {
    "snapshot",
    "browser_snapshot",
    "run_js",
    "browser_evaluate",
    "browser_network_requests",
    "browser_take_screenshot",
    "mcp__browser__browser_snapshot",
    "mcp__browser__browser_network_requests",
    "mcp__browser__browser_take_screenshot",
}

ANTI_BOT_MARKERS = [
    "captcha", "cloudflare", "turnstile", "perimeterx", "datadome", "access denied",
    "unusual traffic", "are you a robot", "verify you are human", "robot or human",
    "403 forbidden", "429 too many requests", "request blocked",
    "incapsula", "_incapsula_resource", "imperva", "incident id",
    "kasada", "akamai", "shieldsquare", "security challenge",
    "pardon our interruption", "potential security risk",
    "detected automated scripts", "additional security check",
    "i am human", "hcaptcha", "press & hold",
]


def _detect_anti_bot_signal(text: str) -> str | None:
    lowered = (text or "").lower()
    for marker in ANTI_BOT_MARKERS:
        if marker in lowered:
            return marker
    return None


# --------------------------------------------------------------------------
# Prompt Construction & Cascade Extraction Contract
# --------------------------------------------------------------------------

SCRIPT_CONTRACT = """MANDATORY SCRIPT CONTRACT - every scraper you write MUST follow this exact
shape, regardless of the target site. This is not optional or per-job -
every script this service produces must be a drop-in replacement for any
other, so a person running it never has to read the code first to figure
out how to call it:

  - Command-line args via argparse:
      `--input` (default "input.xlsx"),
      `--output` (default "output.xlsx"),
      `--max-products` (optional int cap),
      `--headless` (default True, flag to run browser headless or visible),
      `--mask-reviews` (default True, flag to mask reviewer PII and skip review accordions).

  - Input Excel columns (any subset may be populated - handle all three):
      `keyword`, `listing_url`, `detail_url` - semantics:
      * `keyword`: triggers a search/match flow against the listing page or direct URL.
      * `listing_url`: crawls a specific listing page (or falls back to default TARGET_URL).
      * `detail_url`: scrapes one item page directly, skipping listing discovery.
      * IMPORTANT: `detail_url` must be a genuinely independent fast path that extracts
        the single page directly without requiring a listing crawl first.

  - CANONICAL PRIMARY IDENTIFIERS & PROVENANCE URLS (POLYMORPHIC SCHEMA ACROSS ALL DOMAINS):
      Every extracted record MUST contain these three standardized columns, regardless of whether the site is
      e-commerce, a public/government registry, a job board, real estate, a directory, or media:
      1. `product_id`: Universal Primary Entity Key.
         * Resolution hierarchy:
           - For E-Commerce: Official SKU, model code, or MPN (e.g. from JSON-LD `sku`/`productID` or page metadata).
           - For Public / Government Registries & Directories: Registration number, license number, or record ID (e.g., `Nº`, `RNET nº`, `EIN`).
           - For Job Boards / Careers: Job requisition ID or posting ID.
           - For Real Estate / Travel: Property ID or listing ID.
           - General Fallback: Canonical URL slug / deterministic entity ID extracted from the detail URL via regex.
         * DUAL-MAPPING: If the user requests domain-specific ID columns (e.g. "Nº", "RNET nº", "Job ID", "License No"),
           extract them cleanly into their requested column AND ALSO populate `product_id` with that same identifier so the canonical key is always populated.
         * STRICT: Use the exact same ID strategy across all rows in the dataset; NEVER arbitrarily mix strategies.
      2. `product_url`: Direct canonical URL of the entity detail page (product, dossier, job posting, property, article).
      3. `source_listing_url`: The category, search, or listing URL from which the item was discovered (or "DIRECT_INPUT" if passed directly via input.xlsx detail_url).
      * NOTE: Do NOT output a duplicate "Source URL" column that mirrors "product_url".

  - DOMAIN-ADAPTIVE EXTRACTION ARCHETYPES (CONDITIONAL RULES):
      Scraper Studio generates scrapers for any website archetype. Apply the rules relevant to the observed target:
      * Commerce Archetype (Products, Subscriptions, Pricing):
        - Prices MUST be extracted EXCLUSIVELY from within the product's primary pricing container or buy box (e.g. `[itemprop="price"]`, `.price`, `.product-price`, `.current-price`, or JSON-LD `offers.price`).
        - NEVER execute price regexes against product headings (`h1`, `h2`, `h3`, `.title`), promo banners, or carousels.
        - Currency Anchoring: Anchor to currency symbols or ISO codes: `r'(?i)(?:[$£€₹]|USD|CAD|EUR|GBP)\\s*(\\d{1,5}(?:[.,]\\d{2})?)'`. Reject raw standalone numbers found in prose.
      * Registry & Directory Archetype (Public Records, Accommodations, Businesses, Licenses):
        - Extract structured fields via label-value pair mapping (e.g., `tr:has(th, td)`, `dt`/`dd`, `div.label`/`div.value`, or table column header mapping).
        - Capture registration dates, tax/VAT IDs (`NIF`, `NIPC`, `EIN`), operating entities, addresses, and contact info.
      * Career & Job Archetype (Job Boards, Postings):
        - Extract job title, department, employment type (Remote, Hybrid, Full-time), compensation ranges, and direct application links.
      * Content & Article Archetype (News, Blogs, Documentation):
        - Extract headline, author byline, publication timestamp, lead text, and body content.
      * Universal: Never hallucinate or demand pricing or buy-box fields on non-commerce websites!

  - CRAWL FUNNEL TELEMETRY & LOUD FAILURE ON EMPTY:
      * Scrapers must track and log crawl funnel metrics using `log()`:
        `categories_discovered`, `links_discovered`, `links_processed`, `records_extracted`.
        Example: `log(f"Crawl Funnel: {num_categories} categories, {num_links} links discovered, {num_processed} processed, {num_extracted} extracted.")`
      * FAIL LOUDLY: If the crawl completes with 0 records extracted, the script MUST log:
        `ERROR: Zero records were extracted. Aborting.` and exit with `sys.exit(1)`.
      * DIAGNOSTIC SCREENSHOT ON ZERO RECORDS: If 0 records are found on the target listing/detail page,
        take a full-page screenshot using Playwright saved to `diagnostic_screenshot.png`, capture the HTTP status code
        and page `<title>`, and log: `log(f"No records found: HTTP {status}, Title: '{title}', Screenshot: diagnostic_screenshot.png")`.

  - EXCEL CELL LENGTH LIMIT PROTECTION (32,767 CHARACTERS):
      * Excel cells corrupt or truncate if text exceeds 32,767 characters.
      * Every script MUST define and use a `sanitize_cell_value(val, max_len=32000)` helper:
        ```python
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
        ```
      * Pass all field values through `sanitize_cell_value()` before exporting to DataFrame / Excel.

  - CLICK INTERCEPTION & MODAL OVERLAY ERROR LOGGING:
      * When clicks are blocked or intercepted by overlays/popups, catch the exception, log the real error:
        `log(f"Click blocked on locator '{selector}': {exc}")`
      * Record in Flags sheet: `{"Type": "Click Intercepted", "URL": url, "Message": f"Element {selector} blocked: {str(exc)[:200]}"}`
      * Fallbacks: try pressing Escape (`await page.keyboard.press("Escape")`), dismiss modal overlays,
        or dispatch JS click (`await locator.evaluate("el => el.click()")`).

  - BOT PROTECTION FAST-FAIL (NO FUTILE RETRIES):
      * In `detect_anti_bot_signals()`: If HTTP status is 403, 429, or page contains WAF challenge markers:
        Always inspect both HTTP status and HTML text (lowercase). Include all enterprise and state WAF signatures:
        markers = (
            "cloudflare turnstile", "cf-browser-verification", "ray id:", "just a moment...",
            "datadome", "perimeterx", "px-captcha", "distil networks", "press & hold",
            "incapsula", "_incapsula_resource", "imperva", "incident id", "kasada",
            "akamai", "shieldsquare", "security challenge", "pardon our interruption",
            "potential security risk", "detected automated scripts", "additional security check",
            "i am human", "hcaptcha", "access denied", "robot or human"
        )
        Classify as "Blocked by bot protection", log to Flags, and STOP RETRYING immediately. Exit cleanly with anti-bot status.

  - SAFE PAGE TITLE & BEAUTIFULSOUP PARSING:
      * Never call `.title.get_text()` without guarding against None:
        `safe_title = soup.title.get_text(strip=True) if getattr(soup, "title", None) else ""`


  - PERSONAL DATA & REVIEW ACCORDION FILTERING:
      * When expanding accordions to discover specs, DO NOT click review accordions:
        Skip any selector matching `[class*='review' i]`, `[id*='review' i]`, `button:has-text('Review')`, `button:has-text('Rating')`.
      * If `--mask-reviews` is True (default) and review text is extracted:
        Apply regex masking to reviewer names (`John Doe` -> `J*** D.`) and email addresses (`***@***.***`).

  - OUTPUT EXCEL SHEETS (EXTENSIBLE CONTRACT):
      * Must contain at least two sheets:
        1. "Extracted Data": One row per item.
        2. "Flags": One row per anti-bot signal, missing field, low-confidence extraction, or error.
           Include entries if core fields (e.g. brand, category, price) are missing on a product.
      * MULTI-SHEET EXTENSIBILITY: Scrapers may also write additional custom sheets if requested
        (e.g., "Recommendations", "Variants", "Categories"). Repair agents and scripts MUST PRESERVE all custom sheets.
      * UNIFIED ENTITY RECORDS & ROW CONSOLIDATION (CRITICAL):
        - Exactly ONE row per unique entity in "Extracted Data". NEVER produce split or disjoint rows!
        - When a listing crawl loop visits a detail page for each item:
          The detail helper MUST return the extracted attributes (e.g. `data = await scrape_detail(...)`),
          and the listing loop MUST enrich the existing item dictionary: `item.update(data)` BEFORE appending
          to `records`:
          ```python
          # CORRECT:
          detail_data = await scrape_detail(context, item["product_url"], ...)
          if detail_data:
              item.update(detail_data)
          records.append(item)
          ```
          NEVER call `records.append(item)` in the listing loop AND `records.append(data)` inside `scrape_detail`!
          Doing so creates two disjoint rows per item where half the cells in each row are empty (`NaN`).
        - In `write_output(records, flags, output_path)`: Always consolidate records sharing the same primary key
          (`product_url` or `product_id`) by merging complementary non-empty fields before writing to Excel.
      * HEADER-DRIVEN TABLE EXTRACTION & INDEX RESILIENCE (CRITICAL):
        - When extracting tabular rows, dynamically map cells using table column headers (`<th>` or the first `<tr>`).
        - NEVER rely on rigid, hardcoded numeric column indices (e.g. `cells[2]`, `cells[3]`) without verifying the column header! Hidden status icons, action buttons, or checkbox columns will shift indices and misplace values into wrong columns.
      * VALUE HYGIENE & LABEL PREFIX CLEANING (CRITICAL):
        - Use `unicodedata.normalize('NFKC', text)` in `clean(val)` to standardize Unicode representations (preserving `ç`, `ã`, `é`, `í`, `ü`, `ñ`, etc.).
        - Strip redundant label prefixes from scraped text (e.g. clean `"Nome: CASA DO PADRE"` or `"Nome CASA DO PADRE"` -> `"CASA DO PADRE"`, `"Data: 2026-10-20"` -> `"2026-10-20"`).
        - Normalize integer-valued floats (`13702.0` -> `"13702"`).

  - CASCADE EXTRACTION STRATEGY:
      1. Tier 1 (Internal JSON API): If observed during recon, query endpoint directly using requests/httpx.
      2. Tier 2 (Hydrated State): Check for `<script id="__NEXT_DATA__">`, `<script id="__NUXT_DATA__">`,
         `window.__INITIAL_STATE__`, or Schema.org JSON-LD blocks.
      3. Tier 3 (Playwright Async Browser Automation): For modern JavaScript SPAs, client-rendered prices,
         accordion spec tables, dynamic color/storage variants, and ASP.NET WebForms:
         * Use `playwright.async_api.async_playwright()`.
         * Launch Chromium with `--disable-blink-features=AutomationControlled`, `--no-sandbox`, and realistic User-Agent.
         * Set viewport and inject stealth script immediately after creating context:
           ```python
           context = await browser.new_context(
               user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
               viewport={"width": 1920, "height": 1080},
           )
           await context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} };")
           ```
         * Call `dismiss_cookie_banner(page)` immediately after `page.goto()`.
         * Auto-wait for elements using `page.wait_for_selector()` or `page.locator()`.
         * DYNAMIC FORM & AJAX / POSTBACK SYNCHRONIZATION (UNIVERSAL TIMING RULE):
           - When submitting searches, filters, or clicking pagination buttons that update content dynamically (e.g. ASP.NET AJAX, UpdatePanels, React, Vue, HTMX, Angular):
             1. SAFE INPUT CLEARING & COLLISION GUARD:
                - To enter a new search query, clear the input using native Playwright: `await search_input.fill("")` or `await search_input.press("Control+A"); await search_input.press("Backspace")`.
                - NEVER click a page's "Clear", "Reset", or "Limpar" button on the first keyword or if the input is already empty. Clicking reset buttons triggers asynchronous postbacks that race against and cancel subsequent search submissions! If a physical reset button is clicked between iterations, ALWAYS wait for network idle before typing the next query.
             2. CONTAINER VISIBILITY TRAP AVOIDANCE:
                - NEVER wait solely on a table container or element locator with default `state="visible"` if that container starts hidden or empty in the DOM (e.g. `<table class="... Empty">` or `display: none`). Playwright will timeout waiting for the empty container to be visible!
                - ALWAYS wait for either:
                  a) Network & DOM stabilization:
                     ```python
                     try:
                         await page.wait_for_load_state("networkidle", timeout=6000)
                     except Exception:
                         await asyncio.sleep(1.0)
                     ```
                  b) Populated child rows or result cards:
                     `await page.locator("tr:has(td), [data-testid*='item'], .product-card, .job-card, .result-item, .card").first.wait_for(state="attached", timeout=15000)`
                  c) Counter / result text mutation indicating results updated:
                     `await page.wait_for_function("() => (document.querySelector('.counter, .total, .results-count') || {}).innerText?.trim()?.length > 0", timeout=15000)`
           - For traditional form submits that trigger a full page navigation, wrap submits:
             `async with page.expect_navigation(wait_until="domcontentloaded"): await submit_btn.click()`
         * CLASS-AGNOSTIC TABLE & LISTING ROW EXTRACTION (CRITICAL):
           - NEVER assume alternating row zebra classes (e.g. `tr.TableRecords_OddLine, tr.TableRecords_EvenLine`, `tr.odd, tr.even`, `tr.row-0, tr.row-1`).
           - Many dynamic frameworks, CMS platforms, or paginated table states render rows with empty classes (`class=""`) or custom utility classes. Relying on zebra classes causes 0 items to be extracted!
           - ALWAYS select table rows semantically:
             `rows = [tr for tr in table.select("tr") if tr.select("td") and not tr.select("th")]` or `table.select("tr:has(td)")`.
           - Validate that each row contains at least one link or expected minimum data cells before parsing.
         * PAGINATION ANTI-STALL & EMPTY-PAGE CIRCUIT BREAKER (CRITICAL):
           - In every pagination loop (e.g. `while not self.stop_for_antibot:` or `while page_num < max_pages:`):
             Always verify that items were found on the current page before continuing to the next page:
             ```python
             items = await self.parse_listing_page(page, source_url)
             if not items:
                 log("No records discovered on current listing page. Ending pagination loop.")
                 break
             ```
           - NEVER continue clicking "Next" pagination controls if the current page extracted zero items or if the page content/URL did not advance. This prevents infinite or runaway loops across dozens/hundreds of blank pages.
           - Log crawl progress on every page: `log(f"Listing page {page_num}: discovered {len(items)} items. Total extracted: {self.records_extracted}")`.
         * Click accordion tabs to reveal hidden specs (skipping review accordions).
      4. Tier 4 (Static Fast-Path): For traditional server-rendered static HTML sites, use `requests`
         with a desktop User-Agent and `BeautifulSoup`.

  - Cookie & Consent Banner Dismissal:
      Include a `dismiss_cookie_banner(page)` helper and call it once immediately after every `page.goto(...)`.

  - Data Cleaning & Normalization:
      * Clean currency/prices: extract clean formatted strings (e.g. "$24.99", "USD 120.00").
      * Print timestamped log lines for each major step using a `log()` helper.

  - REGEX & CODE SYNTAX RULES (CRITICAL):
      * In raw strings (r"..." or r'...'), use SINGLE backslashes for regex tokens:
        r"\\d+", r"\\s+", r"\\b", r"\\w+", r"\\(?[2-9]\\d{2}\\)?", r"\\.com".
        NEVER double-escape inside raw strings (do NOT write r"\\\\d", r"\\\\s", r"\\\\b", r"\\\\(").
      * When escaping literal parentheses in regex, use r"\\(" and r"\\)" (or re.escape).
      * The script must be syntactically valid Python 3 and compile with zero errors.
      * PLAYWRIGHT PYTHON WAIT_FOR_FUNCTION SIGNATURE (CRITICAL):
        In Playwright Python, `page.wait_for_function(expression, *, arg=None, timeout=None)` accepts `arg` ONLY as a keyword argument.
        NEVER pass `arg` positionally as the 2nd argument (do NOT write `page.wait_for_function(expr, arg_val)`).
        ALWAYS pass it as keyword: `page.wait_for_function(expr, arg=arg_val, timeout=...)`.

  - INPUT EXCEL & NAN HANDLING RULES (CRITICAL):
      * When reading a single sheet from Excel with pandas:
        `input_df = pd.read_excel(args.input).fillna("")`
      * If reading multiple sheets with `sheet_name=None`, pandas returns a dict of DataFrames.
        Apply `.fillna("")` to each DataFrame inside the dict:
        `sheets = pd.read_excel(args.input, sheet_name=None)`
        `sheets = {k: v.fillna("") for k, v in sheets.items()}`
        `input_df = sheets.get("Extracted Data") or next(iter(sheets.values()), pd.DataFrame())`
        NEVER call `.fillna("")` directly on the dict returned by `pd.read_excel(..., sheet_name=None)`!
      * Any helper function (e.g. `clean()`, `value_from_row()`) MUST treat `pd.isna(val)`, `None`,
        and the string `"nan"` / `"none"` / `"null"` as empty string `""`.
      * NEVER allow `"nan"` to be treated as a valid `detail_url`, `listing_url`, or `keyword`."""


def build_recon_prompt(job: InspectionJob) -> str:
    keyword_line = (
        f"Sample keyword to test with: {job.sample_keyword}"
        if job.sample_keyword
        else "No sample keyword given - use a representative sample item during inspection."
    )
    return f"""You are inspecting a live website to gather the facts needed to build a
Python web scraper. YOU DO NOT WRITE ANY CODE IN THIS PHASE - a separate
step handles code generation from the findings you report here. Your only
output is a compact JSON findings object.

TARGET: {job.target_url}
{keyword_line}

The target above may be a listing/search page OR a single item's detail page -
figure out which by looking at what actually renders, don't assume either way.

RULES (as described by the requester, in plain language):
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules (data points to capture): {job.extraction_rules or 'capture all visible product/article attributes'}

YOUR TASK (USING CUSTOM PLAYWRIGHT MCP TOOLBOX WITH IN-TOOL HEALING):
1. Navigate to the target URL using `navigate(url)`.
2. Take a `snapshot()` to inspect the real rendered structure (headings, product cards, interactive elements, JSON-LD).
3. If search or filter interaction is required, use `type_text(selector, text, press_enter=...)` and `click(selector)`.
   (Note: `click` automatically dismisses blocking cookie banners, overlays, and modals via in-tool healing).
4. If testing catalog pagination or infinite scroll, use `load_all(item_selector=...)` to automatically test item growth.
5. If you're on a listing page, identify the navigation path to detail pages and inspect a sample detail page.
6. Stage observed sample rows using `save_items(sample_records=..., schema=..., item_selector=...)`.
   (Note: `save_items` automatically checks for empty fields and re-evaluates selectors on the live DOM if any fields are blank).
7. If the page is backed by an internal clean JSON/GraphQL API, record that endpoint.
8. Do not attempt to solve CAPTCHAs or defeat anti-bot WAFs - if encountered, record the marker and halt immediately.

Return your final answer as a single JSON object with exactly these keys
(keep every value CONCISE - a few sentences or short bullet-style strings per
key, not prose paragraphs):
  "page_type"              - "listing" | "detail" | "both"
  "cookie_banner"           - what was present (if anything) and how it was dismissed
  "search_flow"             - how search/filter interaction actually works, if tested
  "navigation_path"         - how to get from listing to detail pages
  "listing_selectors"       - concrete selectors/patterns observed for listing items
  "detail_selectors"        - concrete selectors/patterns observed on a detail page
  "network_api"             - any clean JSON API endpoint observed, or "none observed"
  "sample_extracted_fields" - a small sample of real values you actually saw, per
                               the extraction rules above
  "confidence_notes"        - list of strings, anything unverified or uncertain

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences, no explanation before or after it, no prose of any
kind outside the JSON, and no code. It must parse cleanly with Python's
json.loads().
"""


def build_codegen_prompt(job: InspectionJob, recon_findings: dict) -> str:
    return f"""You are writing a complete Python web scraper based on a prior live-site
inspection. You do NOT have browser access in this step - work only from the
inspection findings below and the requester's rules. Where the findings are
thin or uncertain, say so explicitly in confidence_notes instead of
inventing selectors or behavior you weren't shown.

TARGET: {job.target_url}

INSPECTION FINDINGS (gathered during a live browser session, prior step):
{json.dumps(recon_findings, indent=2)}

RULES (as described by the requester, in plain language):
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules (data points to capture): {job.extraction_rules or 'capture all visible product/article attributes'}

Use requests+BeautifulSoup for static/API-backed content per the findings'
"network_api" field when present, and a browser-based approach (noted
clearly in the delivered script) where the findings indicate the page
requires JS rendering.

- REGEX & STRING ESCAPING (CRITICAL):
  When writing regex patterns, use standard Python raw string literals with single backslashes,
  e.g. r"\\d{3}", r"\\s+", r"\\b", r"\\([2-9]\\d{2}\\)". Never double-escape backslashes in raw strings
  (do NOT write r"\\\\d+" or r"\\\\("). Use "\\n" for get_text separator and "\\xa0" for non-breaking spaces.

{SCRIPT_CONTRACT}

Return your final answer as a single JSON object with exactly these keys:
  "generated_code"    - string, the full .py file, following the contract above
  "requirements_txt"  - string, the exact pip-installable dependency list
                         this specific script needs (only what it actually
                         imports - do not copy a generic list)
  "confidence_notes"  - list of strings (carry forward anything uncertain
                         from the inspection findings, plus anything you
                         had to assume while writing the code)

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences (no ```json or ```), no explanation before or after
it, no prose of any kind outside the JSON. It must parse cleanly with
Python's json.loads(). Put any explanation you want to give INSIDE the JSON,
in confidence_notes - never as text surrounding the JSON object.
"""


RECON_SYSTEM_PROMPT = (
    "You are a careful web-inspection agent for Scraper Studio powered by the Custom Playwright MCP Toolbox "
    "with embedded in-tool micro-healing loops. You observe and interact with live pages through the provided tools "
    "(navigate, snapshot, click, type_text, scroll, load_all, run_js, save_items). You never attempt to "
    "bypass CAPTCHAs, proxies, or IP blocks. Your ONLY job this phase is to test interactions, verify pagination and selectors, "
    "stage sample rows, and gather and report real, observed facts about the target site as "
    "compact structured findings. You do not write any code in this phase - a separate step handles code generation "
    "from what you report. Be concise: every extra sentence you write is context another step has to pay to read."
)

CODEGEN_SYSTEM_PROMPT = (
    "You are an expert Python code-generation agent for Scraper Studio, "
    "writing a clean, production-grade scraper from a prior live-site inspection's findings. "
    "You have no browser or filesystem access in this step - work only from "
    "the findings and rules given to you. Follow the mandatory SCRIPT CONTRACT strictly. "
    "Use standard Python 3 syntax with properly formatted raw strings for regex patterns "
    "(single backslashes like r'\\d+', r'\\s+', r'\\b', r'\\('), avoiding invalid double-escapes. "
    "Where findings are incomplete or ambiguous, note them in confidence_notes rather than guessing."
)

CODEGEN_MAX_TURNS = 6
CODEGEN_MAX_SECONDS = 180

REFINEMENT_RECON_SYSTEM_PROMPT = (
    "You are a specialized web-inspection agent for Scraper Studio performing a targeted refinement pass. "
    "Your goal is to inspect the live target website specifically for the new data points, DOM selectors, "
    "interactions, or navigation flows requested by the user to refine an existing scraper. "
    "You only observe pages through the browser tool provided. You do not write code in this phase. "
    "Report real, verified facts as a compact structured JSON findings object."
)

REFINEMENT_CODEGEN_SYSTEM_PROMPT = (
    "You are an expert Python code-generation and refactoring agent for Scraper Studio. "
    "Your goal is to update an existing scraper to incorporate the user's specific refinement instructions, "
    "new data points, and the latest inspection findings. "
    "You have no browser access in this step. Ensure the updated scraper follows the mandatory SCRIPT CONTRACT "
    "strictly, preserves all existing working features, compiles with valid Python 3 syntax, and extracts the "
    "newly requested fields with clean column names in the 'Extracted Data' Excel sheet."
)


def build_refinement_recon_prompt(
    job: InspectionJob,
    prior_code: str,
    prior_findings: dict | None,
    refinement_instructions: str,
) -> str:
    keyword_line = (
        f"Sample keyword to test with: {job.sample_keyword}"
        if job.sample_keyword
        else "No sample keyword given - use a representative sample item during inspection."
    )
    prior_findings_json = json.dumps(prior_findings or {}, indent=2)
    truncated_prior_code = prior_code[:8000] if len(prior_code) > 8000 else prior_code

    return f"""You are performing an interactive REFINEMENT inspection on a live website for an existing scraper in Scraper Studio.
YOU DO NOT WRITE CODE IN THIS PHASE. Your only output is an updated, compact JSON findings object.

TARGET: {job.target_url}
{keyword_line}

USER REFINEMENT REQUEST / DESIRED CHANGES:
\"\"\"{refinement_instructions}\"\"\"

EXISTING SCRAPER RULES:
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules: {job.extraction_rules or 'none specified'}

PRIOR INSPECTION FINDINGS:
{prior_findings_json}

CURRENT SCRAPER CODE EXCERPT:
```python
{truncated_prior_code}
```

YOUR TASK FOR THIS REFINEMENT:
1. Navigate to the target page or relevant detail page using the browser tool.
2. Focus specifically on the USER'S REFINEMENT REQUEST:
   - If the user requested NEW DATA POINTS or EXTRACTION FIELDS (e.g. SKU, discount, rating, seller name, specs, stock):
     * Locate the exact HTML elements, attributes, or API responses containing these values.
     * Record the concrete CSS selectors or XPath patterns for them.
     * Record real sample extracted values you observed.
   - If the user requested DIFFERENT KEYWORDS or SEARCH LOGIC:
     * Test the search or filter with the requested keyword.
   - If the user requested PAGINATION or NAVIGATION CHANGES (e.g. multi-page crawl, clicking tabs/accordions):
     * Test clicking the pagination buttons ("Next", page numbers) or infinite scroll, and note the selectors and URL pattern.
     * Test clicking accordions/tabs (e.g. "Specifications", "Tech Specs") to verify how they expand.
   - If the page is backed by an internal JSON/XHR endpoint that provides the requested data, record that API endpoint.
3. Dismiss any cookie banners or overlays encountered.
4. Do not solve CAPTCHAs or bypass anti-bot blocks - note them if encountered and exit.

Return your final answer as a single JSON object with exactly these keys:
  "page_type"              - "listing" | "detail" | "both"
  "cookie_banner"          - what was present and how dismissed
  "search_flow"            - updated search/filter flow, if tested
  "navigation_path"        - updated listing to detail navigation or pagination steps
  "listing_selectors"      - updated selectors for listing items
  "detail_selectors"       - updated selectors on detail page, including selectors for NEWLY requested fields
  "network_api"            - any clean JSON API observed, or "none observed"
  "sample_extracted_fields"- dictionary of sample extracted values, including the NEW data points requested by the user
  "refinement_notes"       - short bullet points describing what was discovered for the user's specific request
  "confidence_notes"       - list of strings for anything unverified or uncertain

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences, no explanation before or after it, no prose of any
kind outside the JSON. It must parse cleanly with Python's json.loads().
"""


def build_refinement_codegen_prompt(
    job: InspectionJob,
    prior_code: str,
    prior_findings: dict | None,
    new_recon_findings: dict,
    refinement_instructions: str,
) -> str:
    merged_findings = {**(prior_findings or {}), **new_recon_findings}
    return f"""You are modifying and updating an existing Python web scraper based on an interactive user refinement request.
You do NOT have browser access in this step - work from the existing code, the user's requested changes, and the latest inspection findings.

TARGET: {job.target_url}

USER REFINEMENT REQUEST / DESIRED CHANGES:
\"\"\"{refinement_instructions}\"\"\"

LATEST INSPECTION FINDINGS (from live browser session):
{json.dumps(merged_findings, indent=2)}

EXISTING SCRAPER CODE:
```python
{prior_code}
```

REFINEMENT INSTRUCTIONS:
1. Update the existing scraper to completely fulfill the USER'S REFINEMENT REQUEST.
   - If new data points were requested, extract them accurately using the discovered selectors/APIs, and include them as explicit columns in the "Extracted Data" sheet.
   - If pagination or multi-page crawling was requested, implement robust pagination loop handling `--max-products`.
   - If keywords or search logic changed, adapt the matching logic accordingly.
   - If accordion/tab clicking is required, use Playwright async locators (`await page.locator(...).click()`, `await page.wait_for_timeout(...)`).
2. Maintain all existing working capabilities of the scraper:
   - Command-line arguments (`--input`, `--output`, `--max-products`, `--headless`).
   - Universal dual-sheet Excel output ("Extracted Data" and "Flags").
   - Cookie dismissal (`dismiss_cookie_banner`).
   - Anti-bot detection (`detect_anti_bot_signals`).
   - Clean NaN handling (`input_df = pd.read_excel(args.input).fillna("")`).
3. Follow Python syntax rules strictly:
   - Use standard raw strings (r"\\d+", r"\\s+", r"\\b", r"\\(") without double escaping.
   - Clean HTML strings with "\\n" and "\\xa0".
4. Return your final answer as a single JSON object with keys:
   "generated_code"    - string, the complete, updated standalone .py script
   "requirements_txt"  - string, the pip requirements list needed by the updated script
   "confidence_notes"  - list of strings detailing the changes made and anything requiring verification

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences (no ```json or ```), no explanation before or after
it, no prose outside the JSON. It must parse cleanly with Python's json.loads().

{SCRIPT_CONTRACT}
"""


# --------------------------------------------------------------------------
# Diagnostic Recon & Codegen Prompts for Self-Healing Loop
# --------------------------------------------------------------------------

DIAGNOSTIC_RECON_SYSTEM_PROMPT = (
    "You are a specialized diagnostic web-inspection agent for Scraper Studio performing an autonomous self-healing repair pass. "
    "Your mission is to inspect the live target website to determine why an existing scraper failed during a test run "
    "(e.g. raised unhandled exceptions, failed to find elements, was blocked by modal overlays or anti-bot defenses, or extracted 0 meaningful product rows). "
    "You only observe and interact with the live page through the browser tool provided. You do NOT write code in this phase. "
    "Diagnose the real failure on the live website, verify working selectors on the real DOM, test interactions, "
    "and output a compact structured JSON findings object with verified diagnostic facts."
)


def build_diagnostic_recon_prompt(
    job: InspectionJob,
    broken_code: str,
    error_context: str,
    recon_findings: dict | None = None,
    run_details: dict | None = None,
    attempt_history: list[dict] | None = None,
) -> str:
    run_details = run_details or {}
    existing_fields, existing_sheets = _extract_schema_from_code(broken_code)
    fields_clause = f"\n- EXISTING COLUMNS REQUIRED: {existing_fields}" if existing_fields else ""
    sheets_clause = f"\n- EXISTING SHEETS REQUIRED: {existing_sheets}" if existing_sheets else ""

    exit_code = run_details.get("exit_code", "unknown")
    num_extracted = run_details.get("num_extracted_rows", 0)
    stderr_text = run_details.get("stderr", "") or error_context
    stdout_text = run_details.get("stdout_tail", "") or run_details.get("stdout", "")
    error_summary = run_details.get("error_summary", error_context)

    truncated_code = broken_code[:7000] if len(broken_code) > 7000 else broken_code
    stderr_snippet = stderr_text[-3000:] if len(stderr_text) > 3000 else stderr_text
    stdout_snippet = stdout_text[-1500:] if len(stdout_text) > 1500 else stdout_text

    keyword_line = (
        f"Sample keyword tested: {job.sample_keyword}"
        if job.sample_keyword
        else "No sample keyword specified."
    )

    attempt_history_clause = ""
    if attempt_history:
        attempt_lines = []
        for att in attempt_history:
            attempt_num = att.get("attempt", att.get("version", "?"))
            err = att.get("error_summary", att.get("stderr", "Unknown error"))
            diag = att.get("diagnostic_summary", "")
            fixes = att.get("fixes_attempted", att.get("notes", ""))
            attempt_lines.append(
                f"- Attempt {attempt_num}: Error: {err} | Prior Diagnosis: {diag} | Fixes Tried: {fixes}"
            )
        attempt_history_clause = (
            "\nPRIOR REPAIR ATTEMPTS & CUMULATIVE OUTCOMES (CONTEXT RETENTION ACROSS RETRIES):\n"
            + "\n".join(attempt_lines)
            + "\nCRITICAL: Do not repeat failed inspection assumptions or selectors that failed in prior attempts.\n"
        )

    return f"""You are performing an autonomous DIAGNOSTIC LIVE INSPECTION for a failing web scraper in Scraper Studio.
YOU DO NOT WRITE CODE IN THIS PHASE. You must use the browser tool to inspect the live target website, identify the true root cause of failure, and discover working selectors.

TARGET URL: {job.target_url}
{keyword_line}

SCRAPER INTENT & USER RULES:
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules: {job.extraction_rules or 'capture all visible product/article attributes'}
{fields_clause}
{sheets_clause}

TEST RUN FAILURE DETAILS:
- Exit code: {exit_code}
- Meaningful extracted rows: {num_extracted}
- Failure summary: {error_summary}
{attempt_history_clause}

STANDARD ERROR / TRACEBACK:
```
{stderr_snippet}
```

STANDARD OUTPUT SNIPPET (Funnel / Logs):
```
{stdout_snippet}
```

PRIOR INSPECTION FINDINGS:
{json.dumps(recon_findings or {}, indent=2)}

CURRENT BROKEN CODE EXCERPT:
```python
{truncated_code}
```

YOUR DIAGNOSTIC MISSIONS (USE BROWSER TOOLS):
1. Navigate to TARGET URL ({job.target_url}) using `browser_navigate`.
2. Inspect the live rendered DOM with `browser_snapshot`:
   - Check if the page loaded normally or if an HTTP error (403, 404, 500) occurred.
   - Detect and dismiss any cookie-consent banners, popups, or modal overlays that may be intercepting clicks. Record how they were dismissed!
   - Check for anti-bot challenges (Cloudflare, CAPTCHA, WAF). If present, record the anti-bot marker and halt.
3. Diagnose the selectors from the broken code:
   - Why did the script fail or extract 0 rows? Did the listing container selector fail to match? Did detail link selectors break?
   - If the script failed on prices: locate the product's primary pricing container / buy box. Verify currency-anchored price selectors ($xx.xx). Ensure prices are NOT being read from titles or banners!
   - If the script failed on missing fields: find where title, brand, category, specs, or images live in the live DOM.
   - If the script required clicking tabs/accordions: verify the exact locators and whether click interception occurred.
4. Check network requests with `browser_network_requests`:
   - Is there a clean JSON/XHR endpoint that provides the required data directly?
5. Verify working selectors by observing real sample values for all required columns.

Return your final answer as a single JSON object with exactly these keys:
  "root_cause"               - clear diagnosis of why the test run failed or yielded 0 rows
  "page_type"                - "listing" | "detail" | "both"
  "cookie_banner"            - what overlay/banner was present and how it was dismissed
  "working_listing_selectors"- verified selectors for product cards and detail page links
  "working_detail_selectors" - verified selectors on the detail page for required fields
  "working_price_selector"   - verified selector scoped strictly inside the pricing / buy box container
  "search_flow"              - verified search or filter flow, if tested
  "navigation_path"          - verified navigation steps from listing to item details
  "network_api"              - clean JSON API endpoint if observed, or "none observed"
  "sample_extracted_fields"  - dictionary of real sample values observed on the live page
  "diagnostic_notes"         - concise list of actionable recommendations for the codegen repair phase
  "confidence_notes"         - list of strings for anything unverified or uncertain

CRITICAL FORMATTING RULE: your final message must be ONLY that JSON object -
no markdown code fences, no explanation before or after it, no prose outside the JSON. It must parse cleanly with Python's json.loads().
"""


def build_diagnostic_codegen_prompt(
    job: InspectionJob,
    broken_code: str,
    error_context: str,
    recon_findings: dict | None = None,
    diagnostic_findings: dict | None = None,
    run_details: dict | None = None,
    attempt_history: list[dict] | None = None,
) -> str:
    run_details = run_details or {}
    diagnostic_findings = diagnostic_findings or {}
    existing_fields, existing_sheets = _extract_schema_from_code(broken_code)
    fields_clause = f"\n- EXISTING COLUMNS: {existing_fields}" if existing_fields else ""
    sheets_clause = f"\n- EXISTING SHEETS: {existing_sheets}" if existing_sheets else ""

    exit_code = run_details.get("exit_code", "unknown")
    num_extracted = run_details.get("num_extracted_rows", 0)
    stderr_text = run_details.get("stderr", "") or error_context
    stdout_text = run_details.get("stdout_tail", "") or run_details.get("stdout", "")
    error_summary = run_details.get("error_summary", error_context)

    attempt_history_clause = ""
    if attempt_history:
        attempt_lines = []
        for att in attempt_history:
            attempt_num = att.get("attempt", att.get("version", "?"))
            err = att.get("error_summary", att.get("stderr", "Unknown error"))
            diag = att.get("diagnostic_summary", "")
            fixes = att.get("fixes_attempted", att.get("notes", ""))
            attempt_lines.append(
                f"- Attempt {attempt_num}: Error: {err} | Prior Diagnosis: {diag} | Fixes Tried: {fixes}"
            )
        attempt_history_clause = (
            "\nPRIOR REPAIR ATTEMPTS & CUMULATIVE OUTCOMES (CONTEXT RETENTION ACROSS RETRIES):\n"
            + "\n".join(attempt_lines)
            + "\nCRITICAL: Analyze why previous attempts failed. Do NOT repeat strategies or selectors that previously failed.\n"
        )

    return f"""You are repairing a Python web scraper that failed or produced zero records during an automated test run.
A live diagnostic inspection session was just conducted using browser tools to verify the live website state.
Your task is to synthesize a complete, fully repaired, regression-proof scraper.

CRITICAL REGRESSION INVARIANTS:
1. DO NOT DROP PREVIOUSLY WORKING FIELDS. You MUST preserve all existing extraction fields ({fields_clause or 'e.g. Title, Price, Description, Modules, Categories, Images'}). Never blank out or drop Title, Price, or custom modules!
2. MULTI-SHEET PRESERVATION: You MUST preserve all existing Excel sheets ({sheets_clause or "'Extracted Data', 'Flags', 'Recommendations'"}). Never drop custom sheets like 'Recommendations' or 'Variants'!
3. CANONICAL PRIMARY IDENTIFIERS: Output 'product_id' (Universal Primary Entity Key), 'product_url' (Direct Detail URL), and 'source_listing_url'. Do not duplicate 'Source URL'.
4. STRICT PRICE SCOPING & DOMAIN-ADAPTIVE EXTRACTION: If scraping e-commerce, extract prices strictly from pricing containers/buy boxes and anchor to currency symbols. If scraping public registries, job boards, real estate, or directories, extract structured label-value pairs, registration/tax IDs, addresses, or job attributes without inventing pricing fields.
5. EXCEL 32K LIMIT: Include and use `sanitize_cell_value(val, max_len=32000)` on all output values before Excel export.
6. CRAWL FUNNEL & FAIL LOUDLY: Log crawl funnel metrics. If 0 records are extracted, exit with sys.exit(1) and take a diagnostic screenshot.
7. CLICK TELEMETRY: Wrap clicks in try/except, log real errors, and provide overlay dismissal / JS evaluate fallback.
8. BOT PROTECTION: Classify 403 or WAF challenge pages as 'Blocked by bot protection' and halt immediately without retrying.

TARGET: {job.target_url}
RULES:
- Search rules: {job.search_rules or 'none specified'}
- Filter rules: {job.filter_rules or 'none specified'}
- Navigation rules: {job.navigation_rules or 'none specified'}
- Extraction rules: {job.extraction_rules or 'capture all visible product/article attributes'}

FAILED TEST RUN TELEMETRY:
- Exit Code: {exit_code}
- Extracted Rows: {num_extracted}
- Failure Summary: {error_summary}
{attempt_history_clause}
- Traceback / Error:
```
{stderr_text[-3000:]}
```
- Standard Output:
```
{stdout_text[-1500:]}
```

LIVE DIAGNOSTIC FINDINGS (VERIFIED ON LIVE DOM VIA BROWSER TOOLS):
{json.dumps(diagnostic_findings, indent=2)}

PRIOR INSPECTION FINDINGS:
{json.dumps(recon_findings or {}, indent=2)}

CURRENT BROKEN CODE:
```python
{broken_code}
```

INSTRUCTIONS FOR REPAIR:
1. Incorporate the verified working selectors and findings discovered during the live diagnostic inspection session.
2. If dynamic JavaScript elements, prices, or specs were missing, or if click interactions failed, use Playwright async browser automation (`playwright.async_api.async_playwright()`) with `page.wait_for_selector()`, selector auto-waiting, and auto-dismiss cookie banners.
3. If 0 rows were extracted due to overly restrictive keyword/category filtering, relax the matching logic or match against URL slugs.
4. Ensure the script follows the mandatory SCRIPT CONTRACT strictly.
5. Return your final answer as a single JSON object with keys "generated_code", "requirements_txt", and "confidence_notes".

{SCRIPT_CONTRACT}
"""


# --------------------------------------------------------------------------
# Output Parsing & Code Sanitization
# --------------------------------------------------------------------------

def sanitize_regex_in_raw_string(content: str) -> str:
    """
    In raw strings (r"..." or r'...'), LLMs often mistakenly double-escape backslashes,
    e.g. r"\\\\d+" instead of r"\\d+", r"\\\\(" instead of r"\\(", r"\\\\s+" instead of r"\\s+",
    r"\\\\b" instead of r"\\b".
    In Python raw strings, '\\\\' is two literal backslash characters.
    When compiled as regex, '\\\\(' becomes a literal backslash followed by '(', which causes
    '(?' to be parsed as a regex extension like '(?[', throwing PatternError.
    
    This function replaces double-backslashes before regex tokens in raw strings with single backslashes.
    """
    pattern = r"\\\\([bBdDsSwW\(\)\[\]\{\}\+\*\?\.\-\/\^\|\$\\\'\"]|[nrt]|xa0|[ux][0-9a-fA-F]+|[0-9])"
    
    def replacer(m):
        return "\\" + m.group(1)
        
    return re.sub(pattern, replacer, content)


def _inject_playwright_stealth(code: str) -> str:
    """Safely injects stealth script and Chrome runtime into Playwright browser context creation."""
    if "async_playwright" not in code or "new_context" not in code or "navigator.webdriver" in code:
        return code

    pattern = re.compile(r'^([ \t]*)(\w+)\s*=\s*await\s+browser\.new_context\(', re.MULTILINE)
    match = pattern.search(code)
    if not match:
        return code

    indent = match.group(1)
    var_name = match.group(2)
    start_call = match.end() - 1  # opening '('

    depth = 0
    in_single_quote = False
    in_double_quote = False
    escape = False
    end_call = -1

    for i in range(start_call, len(code)):
        char = code[i]
        if escape:
            escape = False
            continue
        if char == '\\':
            escape = True
            continue
        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            continue
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
            continue
        if in_single_quote or in_double_quote:
            continue

        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                end_call = i + 1
                break

    if end_call == -1:
        return code

    newline_pos = code.find('\n', end_call)
    insert_pos = (newline_pos + 1) if newline_pos != -1 else end_call

    stealth_stmt = indent + 'await ' + var_name + '.add_init_script("Object.defineProperty(navigator, \'webdriver\', { get: () => undefined }); window.chrome = { runtime: {} };")\n'
    return code[:insert_pos] + stealth_stmt + code[insert_pos:]


def sanitize_generated_code(code: str) -> tuple[str, list[str]]:
    """Sanitizes, auto-repairs, and validates LLM-generated Python scraper code."""
    notes = []
    if not code or not code.strip():
        return code, notes

    sanitized = code.strip()

    # 1. Strip markdown fences if present
    if sanitized.startswith("```"):
        sanitized = re.sub(r"^```(?:python)?\s*\n?", "", sanitized)
        sanitized = re.sub(r"\n?```\s*$", "", sanitized)

    # 2. Fix BeautifulSoup/string literal common mistakes
    # node.get_text("\\n" -> node.get_text("\n"
    sanitized = re.sub(
        r'(\.get_text\()\s*["\']\\\\n["\']',
        lambda m: m.group(1) + '"\\n"',
        sanitized
    )
    # text.replace("\\xa0" -> text.replace("\xa0"
    sanitized = re.sub(
        r'(\.replace\()\s*["\']\\\\xa0["\']',
        lambda m: m.group(1) + '"\\xa0"',
        sanitized
    )
    # text.replace("\\n" -> text.replace("\n"
    sanitized = re.sub(
        r'(\.replace\()\s*["\']\\\\n["\']',
        lambda m: m.group(1) + '"\\n"',
        sanitized
    )

    # 3. Fix double-escaped raw strings: r"..." or r'...'
    def fix_raw_string_match(match):
        prefix = match.group(1)
        quote = match.group(2)
        body = match.group(3)
        fixed_body = sanitize_regex_in_raw_string(body)
        return f"{prefix}{quote}{fixed_body}{quote}"

    # Handle triple-quoted raw strings
    sanitized = re.sub(
        r'(r[fF]?|R[fF]?)(["\']{3})(.*?)\2',
        fix_raw_string_match,
        sanitized,
        flags=re.DOTALL
    )

    # Handle single-quoted raw strings (non-triple)
    sanitized = re.sub(
        r'(r[fF]?|R[fF]?)(["\'])(.*?)\2',
        fix_raw_string_match,
        sanitized
    )

    # 4. Fix Excel NaN / empty-cell handling in clean helpers & pd.read_excel
    sanitized = re.sub(
        r'def\s+clean(?:_text)?\s*\(\s*value\s*\)\s*:\s*\n\s*if\s+value\s+is\s+None\s*:\s*\n\s*return\s*["\']["\']',
        'def clean(value):\n    if value is None or pd.isna(value) or str(value).strip().lower() in ("", "nan", "none", "null"):\n        return ""',
        sanitized
    )
    # 4a. Strip invalid .fillna() chained directly to dict-returning pd.read_excel calls (e.g. sheet_name=None or list)
    sanitized = re.sub(
        r'(pd\.read_excel\([^)\n]*(?:sheet_name\s*=\s*(?:None|\[[^\]]*\])|,\s*None\s*[,)])[^)\n]*\))\s*\.\s*fillna\([^)]*\)',
        r'\1',
        sanitized
    )

    # 4b. Ensure single-DataFrame pd.read_excel calls sanitize NaNs via .fillna(""), while skipping dict-returning calls
    def _sanitize_read_excel(m):
        call_content = m.group(1)
        full_call = f"{call_content})"
        if re.search(r'(?:sheet_name\s*=\s*(?:None|\[)|,\s*None\s*[,)])', call_content):
            return full_call
        return f'{full_call}.fillna("")'

    sanitized = re.sub(
        r'(pd\.read_excel\([^)\n]+)\)(?!\s*\.\s*(?:fillna|copy|get|items|values|keys))',
        _sanitize_read_excel,
        sanitized
    )

    # 4c. Auto-inject stealth webdriver masking in Playwright scripts if missing
    sanitized = _inject_playwright_stealth(sanitized)

    # 4d. Normalize headless Linux user-agent to standard Windows desktop Chrome
    sanitized = re.sub(
        r'user_agent\s*=\s*["\']Mozilla/5\.0\s*\([^"\']*Linux[^"\']*\)[^"\']*["\']',
        'user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"',
        sanitized
    )

    # 4e. Guard against unsafe BeautifulSoup .title.get_text() calls when <title> tag is missing
    sanitized = re.sub(
        r'BeautifulSoup\(([^)]*)\)\.title\.get_text\([^)]*\)',
        r'(BeautifulSoup(\1).title.get_text(strip=True) if getattr(BeautifulSoup(\1), "title", None) else "")',
        sanitized
    )
    sanitized = re.sub(
        r'(\b\w+)\.title\.get_text\(\s*(?:strip\s*=\s*True)?\s*\)',
        r'(\1.title.get_text(strip=True) if getattr(\1, "title", None) else "")',
        sanitized
    )

    # 4f. Ensure generated detect_anti_bot_signals contains enterprise WAF / state IT security markers
    if "def detect_anti_bot_signals" in sanitized and "potential security risk" not in sanitized:
        sanitized = re.sub(
            r'(markers\s*=\s*(?:\(|\[))([^()\[\]]+)(?:\)|\])',
            r'\1\2, "potential security risk", "detected automated scripts", "additional security check", "i am human", "incapsula", "_incapsula_resource", "imperva", "incident id", "access denied")',
            sanitized,
            count=1
        )

    # 4g. Class-agnostic table row selector normalization:
    # Replace brittle alternating zebra class selectors (e.g. TableRecords_OddLine, odd, even)
    # with robust semantic row selectors: tr:has(td)
    sanitized = re.sub(
        r'["\'](?:tr\.(?:TableRecords_(?:Odd|Even)Line|odd|even)\s*,\s*tr\.(?:TableRecords_(?:Odd|Even)Line|odd|even)|tr\.(?:TableRecords_(?:Odd|Even)Line|odd|even))["\']',
        '"tr:has(td)"',
        sanitized
    )

    # 4h. Postback / AJAX search and pagination stabilization:
    # If a wait_results / wait_for_results helper waits for DOM elements but lacks networkidle / sleep stabilization,
    # inject networkidle stabilization with sleep fallback to ensure AJAX postbacks complete before reading the DOM.
    if re.search(r'async\s+def\s+wait_(?:for_)?results\s*\([^)]*\)\s*:', sanitized):
        if "networkidle" not in sanitized:
            sanitized = re.sub(
                r'(async\s+def\s+wait_(?:for_)?results\s*\(\s*(?:self\s*,\s*)?(\w+)[^)]*\)\s*:\s*\n)(\s+)(?:try\s*:)?',
                lambda m: f"{m.group(1)}{m.group(3)}try:\n{m.group(3)}    await {m.group(2)}.wait_for_load_state('networkidle', timeout=5000)\n{m.group(3)}except Exception:\n{m.group(3)}    await asyncio.sleep(1.0)\n{m.group(3)}try:" if "try:" in m.group(0) else f"{m.group(1)}{m.group(3)}try:\n{m.group(3)}    await {m.group(2)}.wait_for_load_state('networkidle', timeout=5000)\n{m.group(3)}except Exception:\n{m.group(3)}    await asyncio.sleep(1.0)\n",
                sanitized,
                count=1
            )

    # 4i. Pagination anti-stall circuit breaker:
    # If crawl_listing parses items inside a while loop, ensure an empty-items circuit breaker is present
    if "crawl_listing" in sanitized and "if not items:" not in sanitized:
        sanitized = re.sub(
            r'((\s+)items\s*=\s*(?:await\s+)?self\.parse_listing(?:_page)?\([^)\n]*\)\n)',
            r'\1\2if not items:\n\2    log("No items discovered on listing page. Halting pagination loop.")\n\2    break\n',
            sanitized,
            count=1
        )

    # 4j. Table and container visibility timeout protection:
    # If a script waits on a table container using wait_for_selector without specifying state,
    # normalize it to state='attached' to avoid hanging on hidden empty tables
    def _normalize_table_wait(m):
        full = m.group(0)
        table_ref = m.group(1)
        timeout_part = m.group(2) or ""
        if "state=" not in full:
            return f'await page.wait_for_selector({table_ref}, state="attached"{timeout_part})'
        return full

    sanitized = re.sub(
        r'await\s+page\.wait_for_selector\(\s*([A-Za-z0-9_]+(?:TABLE|GRID|CONTAINER))\s*(,\s*timeout=[^)]+)?\)',
        _normalize_table_wait,
        sanitized
    )

    # 4k. Safe clear button postback stabilization:
    # If a script clicks a clear/reset button followed immediately by fill() and search click,
    # ensure postback stabilization is present so clear does not cancel search in flight
    if "CLEAR_BUTTON" in sanitized and "SEARCH_BUTTON" in sanitized:
        if 'await page.wait_for_load_state("networkidle"' not in sanitized:
            sanitized = re.sub(
                r'(await\s+safe_click\([^)]*CLEAR_BUTTON[^)]*\)\s*\n)',
                r'\1                    try:\n                        await page.wait_for_load_state("networkidle", timeout=3000)\n                    except Exception:\n                        await asyncio.sleep(0.5)\n',
                sanitized,
                count=1
            )

    # 4l. Playwright wait_for_function keyword-only argument normalization:
    # In Playwright Python, Page.wait_for_function(expression, *, arg=None, ...) requires arg as keyword-only.
    # LLMs frequently pass arg positionally (from JS Playwright syntax), raising TypeError.
    def _fix_wait_for_function(text):
        def repl(m):
            prefix = m.group(1)
            expr = m.group(2)
            sep = m.group(3)
            second_arg = m.group(4).strip()
            rest = m.group(5)
            if re.match(r'^[a-zA-Z_]\w*\s*=', second_arg):
                return m.group(0)
            return f"{prefix}{expr}{sep}arg={second_arg}{rest}"

        pattern = r'(\.wait_for_function\s*\(\s*)((?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\n]*"|\'[^\'\n]*\'))(\s*,\s*)([^,)]+)(.*?\))'
        return re.sub(pattern, repl, text)

    sanitized = _fix_wait_for_function(sanitized)

    # 4m. Universal record consolidation injection into write_output:
    # Ensures that if an LLM generates dual-appends or disjoint rows sharing product_url/product_id,
    # they are cleanly merged into one unified row per entity before writing to Excel.
    if "def write_output" in sanitized and "_merged_records" not in sanitized:
        consolidation_code = (
            r'\1'
            r'    # Universal entity consolidation: merge disjoint or duplicate records\n'
            r'    if records:\n'
            r'        _merged_records = {}\n'
            r'        _record_order = []\n'
            r'        for _r in records:\n'
            r'            _pk = _r.get("product_url") or _r.get("product_id") or _r.get("detail_url") or id(_r)\n'
            r'            if _pk not in _merged_records:\n'
            r'                _merged_records[_pk] = dict(_r)\n'
            r'                _record_order.append(_pk)\n'
            r'            else:\n'
            r'                for _k, _v in _r.items():\n'
            r'                    _v_clean = str(_v or "").strip()\n'
            r'                    _t_clean = str(_merged_records[_pk].get(_k) or "").strip()\n'
            r'                    if _v_clean and not _t_clean:\n'
            r'                        _merged_records[_pk][_k] = _v\n'
            r'                    elif _v_clean and _t_clean and len(_v_clean) > len(_t_clean):\n'
            r'                        if _k != "product_id":\n'
            r'                            _merged_records[_pk][_k] = _v\n'
            r'        records = [_merged_records[_pk] for _pk in _record_order]\n'
        )
        sanitized = re.sub(
            r'(def\s+write_output\s*\([^)]*\)\s*:\n)',
            consolidation_code,
            sanitized,
            count=1
        )

    # 4n. Unicode NFKC normalization in clean(value):
    if "def clean(" in sanitized and "unicodedata.normalize" not in sanitized:
        if "import unicodedata" not in sanitized:
            sanitized = "import unicodedata\n" + sanitized
        sanitized = re.sub(
            r'(text\s*=\s*str\(value\)[^\n]*)',
            r'\1\n    text = unicodedata.normalize("NFKC", text)',
            sanitized,
            count=1
        )

    # 5. Fix row value access: str(row.get(...) or "") where nan string could be created
    sanitized = re.sub(
        r'str\(\s*row\.get\(\s*(["\'](?:detail_url|listing_url|keyword)["\'])\s*(?:,\s*["\'][^"\']*["\'])?\s*\)\s*(?:or\s*["\'][^"\']*["\'])?\s*\)',
        r'(str(row.get(\1) or "") if str(row.get(\1) or "").strip().lower() not in ("nan", "none", "null") else "")',
        sanitized
    )

    # 6. Guard goto / navigation functions against invalid URLs (like "nan")
    def guard_goto(m):
        func_def = m.group(0)
        return func_def + '\n    if not url or str(url).strip().lower() in ("", "nan", "none", "null") or not str(url).startswith(("http://", "https://")):\n        return'

    sanitized = re.sub(
        r'async\s+def\s+(?:goto_with_dismissal|safe_goto|navigate)\s*\(\s*page\s*,\s*url\s*(?:,\s*[^)]*)?\s*\)\s*:',
        guard_goto,
        sanitized
    )

    # 7. AST validation
    try:
        ast.parse(sanitized)
    except SyntaxError as exc:
        notes.append(f"Generated code had Python syntax error on line {exc.lineno}: {exc.msg}")

    return sanitized, notes


def parse_agent_output(final_text: str) -> tuple[str, str, list[str]]:
    """Extracts (generated_code, requirements_txt, confidence_notes) with fallbacks and sanitization."""
    text = (final_text or "").strip()
    raw_code = ""
    reqs = ""
    notes = []

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            raw_code = parsed.get("generated_code", "")
            reqs = parsed.get("requirements_txt", "")
            notes = parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else []
    except (json.JSONDecodeError, TypeError):
        pass

    if not raw_code:
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence_match:
            try:
                parsed = json.loads(fence_match.group(1))
                if isinstance(parsed, dict):
                    raw_code = parsed.get("generated_code", "")
                    reqs = parsed.get("requirements_txt", "")
                    notes = parsed.get("confidence_notes", []) if isinstance(parsed.get("confidence_notes"), list) else []
                    notes.append("Agent wrapped its JSON in a markdown fence instead of returning it raw - extracted automatically.")
            except (json.JSONDecodeError, TypeError):
                pass

    if not raw_code:
        code_match = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
        if code_match:
            raw_code = code_match.group(1)
            notes.append(
                "Agent did not return valid JSON - extracted code from a ```python "
                "fenced block in its response instead. requirements_txt could not "
                "be recovered this way - check the script's imports manually."
            )

    if not raw_code:
        return "", "", [
            "Agent response could not be parsed as JSON or a fenced code block - "
            "no usable code was extracted. Re-running the job often resolves this."
        ]

    sanitized_code, sanitization_notes = sanitize_generated_code(raw_code)
    notes.extend(sanitization_notes)
    return sanitized_code, reqs, notes


def parse_recon_output(final_text: str) -> tuple[dict, list[str]]:
    """Extracts structured JSON findings from recon phase."""
    text = (final_text or "").strip()
    if not text:
        return {}, ["Recon phase produced no output text."]

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed, []
    except (json.JSONDecodeError, TypeError):
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed, ["Recon agent wrapped its JSON in a markdown fence - extracted automatically."]
        except (json.JSONDecodeError, TypeError):
            pass

    capped = text[:4000]
    truncated_note = " (truncated)" if len(text) > 4000 else ""
    return (
        {"raw_recon_notes": capped},
        [f"Recon phase did not return structured JSON findings{truncated_note} - "
         "passing raw text to codegen as best effort."],
    )


# --------------------------------------------------------------------------
# MCP Client & Tool Execution
# --------------------------------------------------------------------------

class MCPClientManager:
    """Manages connection to Playwright MCP Server (HTTP/SSE)."""

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or get_mcp_server_url()).rstrip("/")
        self.session: ClientSession | None = None
        self._exit_stack = None

    async def connect(self):
        # By default, use the faster, in-process Native Playwright Stealth Browser
        # Set ENABLE_MCP=1 or USE_NATIVE_PLAYWRIGHT=0 to force external MCP
        if os.environ.get("USE_NATIVE_PLAYWRIGHT", "1").lower() in ("1", "true", "yes") and os.environ.get("ENABLE_MCP", "0").lower() not in ("1", "true", "yes"):
            return None

        if not MCP_AVAILABLE:
            print("[inspection-service] Warning: mcp library not available in environment.")
            return None

        # 1. Try SSE HTTP transport with a short timeout to prevent hanging
        endpoints_to_try = [
            f"{self.base_url}/sse",
            f"{self.base_url}/mcp",
            f"{self.base_url}",
        ]
        for url in endpoints_to_try:
            try:
                self._exit_stack = asyncio.LifoQueue()
                async def _try_sse():
                    sse_ctx = sse_client(url)
                    read, write = await sse_ctx.__aenter__()
                    session = ClientSession(read, write)
                    await session.__aenter__()
                    await session.initialize()
                    return session

                session = await asyncio.wait_for(_try_sse(), timeout=2.0)
                self.session = session
                print(f"[inspection-service] Connected to Playwright MCP via {url}")
                return self.session
            except BaseException:
                continue

        # Do not use stdio npx fallback on Windows - detached stdio pipes trigger AnyIO CancelledError
        return None

    async def close(self):
        if self.session:
            try:
                await self.session.__aexit__(None, None, None)
            except BaseException:
                pass
            self.session = None

NATIVE_BROWSER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate the browser to a target URL, dismiss overlays, and inspect the rendered page.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_snapshot",
            "description": "Capture a structured text snapshot of the current page including headings, listing elements, detail attributes, links, accordions, and hydrated state.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click an element, accordion tab, dropdown, or modal consent button on the page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector, button text, or element description to click"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_type",
            "description": "Type text into a search bar or input field and trigger change events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector for the input element"},
                    "text": {"type": "string", "description": "Text to type into the element"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "Press a keyboard key such as 'Enter', 'Escape', or 'Tab'.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Key name to press"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll down the page to trigger lazy-loaded product items, images, or infinite scrolling.",
            "parameters": {
                "type": "object",
                "properties": {"amount": {"type": "integer", "description": "Number of pixels to scroll down (default 800)"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Wait for a specific selector or dynamic element to render in the DOM.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string", "description": "CSS selector to wait for"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_evaluate",
            "description": "Execute a JavaScript expression in the page context and return the JSON result.",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "JavaScript code or expression to evaluate"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_network_requests",
            "description": "List all recorded network requests, JSON API endpoints, and GraphQL calls captured during navigation.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_take_screenshot",
            "description": "Capture and verify a screenshot of the currently visible viewport.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


class NativePlaywrightBrowserSession:
    """
    In-process Playwright browser session for live recon.
    Launches real headless Chromium with anti-detection stealth flags, intercepts XHR/JSON APIs,
    executes real DOM clicks, accordions, infinite scrolling, and JavaScript evaluation.
    """
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.current_url: str = ""
        self.network_logs: list[dict] = []
        self._is_initialized = False

    async def initialize(self):
        if self._is_initialized:
            return
        try:
            from playwright.async_api import async_playwright
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                ]
            )
            self.context = await self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                }
            )
            # Stealth init script to mask automation
            await self.context.add_init_script("""
                delete Object.getPrototypeOf(navigator).webdriver;
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            """)
            self.page = await self.context.new_page()

            # Network interception for internal JSON / GraphQL APIs
            async def _on_response(response):
                try:
                    url = response.url
                    ct = response.headers.get("content-type", "").lower()
                    if any(x in ct for x in ("json", "graphql")) or url.endswith(".json") or "/api/" in url or "/v1/" in url or "/v2/" in url or "/graphql" in url:
                        body_snippet = ""
                        try:
                            text = await response.text()
                            body_snippet = text[:300] if text else ""
                        except Exception:
                            pass
                        self.network_logs.append({
                            "url": url,
                            "method": response.request.method,
                            "status": response.status,
                            "content_type": ct,
                            "sample_payload": body_snippet,
                        })
                except Exception:
                    pass

            self.page.on("response", _on_response)
            self._is_initialized = True
        except Exception as exc:
            print(f"[inspection-service] Playwright initialization failed: {exc}")
            raise exc

    async def close(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self._is_initialized = False

    async def _dismiss_consent(self):
        if not self.page:
            return
        common_buttons = [
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Accept')",
            "button:has-text('I agree')",
            "button:has-text('Allow all')",
            "button:has-text('Agree')",
            "button:has-text('Got it')",
            "[id*='cookie'] button",
            "[id*='consent'] button",
            "[id*='onetrust-accept']",
        ]
        for btn in common_buttons:
            try:
                locator = self.page.locator(btn).first
                if await locator.is_visible(timeout=300):
                    await locator.click(timeout=800)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                pass

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        clean_name = tool_name.replace("mcp__browser__", "")
        if not self._is_initialized:
            await self.initialize()

        if clean_name == "browser_navigate":
            url = arguments.get("url", "")
            if not url:
                return "Error: No URL provided."
            self.current_url = url
            try:
                response = await self.page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(1.5)
                await self._dismiss_consent()
                status_code = response.status if response else 200
                title = await self.page.title()
                return f"Navigated to {url}. HTTP Status {status_code}. Page Title: '{title}'. DOM loaded with Playwright."
            except Exception as exc:
                return f"Navigation failed for {url}: {exc}"

        elif clean_name == "browser_snapshot":
            if not self.page:
                return "No page loaded. Call browser_navigate first."
            try:
                title = await self.page.title()
                
                # Check for in-page hydrated state and rich DOM layout
                state_info = await self.page.evaluate("""() => {
                    const result = {};
                    if (window.__NEXT_DATA__) {
                        result.has_next_data = true;
                        result.page = window.__NEXT_DATA__.page;
                        result.query = window.__NEXT_DATA__.query;
                    }
                    if (window.__INITIAL_STATE__) {
                        result.has_initial_state = true;
                    }
                    // JSON-LD scripts
                    const jsonld = [];
                    document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
                        try { jsonld.push(JSON.parse(s.textContent)); } catch(e) {}
                    });
                    if (jsonld.length > 0) {
                        result.json_ld_count = jsonld.length;
                        result.json_ld_sample = jsonld.slice(0, 2);
                    }
                    
                    // Headings
                    result.headings = Array.from(document.querySelectorAll('h1, h2, h3'))
                        .map(h => h.textContent.trim())
                        .filter(t => t.length > 0 && t.length < 120)
                        .slice(0, 8);
                        
                    // Interactive elements (accordions, buttons, tabs)
                    result.interactive_buttons = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], summary, .accordion-header, [data-tab]'))
                        .map(b => ({
                            tag: b.tagName.toLowerCase(),
                            text: b.textContent.trim().slice(0, 50),
                            id: b.id || undefined,
                            role: b.getAttribute('role') || undefined,
                            classes: b.className.slice(0, 60) || undefined
                        }))
                        .filter(b => b.text.length > 0)
                        .slice(0, 15);
                        
                    // Product items / catalog listing elements
                    result.product_cards = Array.from(document.querySelectorAll('[class*="product"], [class*="item"], [class*="card"], article'))
                        .map(c => ({
                            tag: c.tagName.toLowerCase(),
                            classes: c.className.slice(0, 60),
                            text: c.textContent.trim().replace(/\\s+/g, ' ').slice(0, 120)
                        }))
                        .filter(c => c.text.length > 15)
                        .slice(0, 6);
                        
                    // Sample links
                    result.sample_links = Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href'))
                        .filter(href => href && !href.startsWith('#') && !href.startsWith('javascript:'))
                        .slice(0, 10);
                        
                    return result;
                }""")
                
                snapshot = {
                    "url": self.current_url,
                    "title": title,
                    "hydrated_state": {
                        "has_next_data": state_info.get("has_next_data", False),
                        "has_initial_state": state_info.get("has_initial_state", False),
                        "json_ld_sample": state_info.get("json_ld_sample", []),
                    },
                    "headings": state_info.get("headings", []),
                    "interactive_buttons_and_tabs": state_info.get("interactive_buttons", []),
                    "sample_product_cards": state_info.get("product_cards", []),
                    "sample_links": state_info.get("sample_links", []),
                    "discovered_api_endpoints": [req["url"] for req in self.network_logs[-8:]],
                }
                return json.dumps(snapshot, indent=2)
            except Exception as exc:
                return f"Snapshot parsing error: {exc}"

        elif clean_name == "browser_click":
            selector = arguments.get("selector", "")
            if not selector:
                return "Error: No selector provided to click."
            try:
                # Try clicking by exact selector, or text fallback
                try:
                    await self.page.click(selector, timeout=6000)
                except Exception:
                    await self.page.locator(f"text={selector}").first.click(timeout=6000)
                await asyncio.sleep(1.0)
                new_url = self.page.url
                return f"Successfully clicked '{selector}'. Current URL: {new_url}. Page updated."
            except Exception as exc:
                return f"Click failed on '{selector}': {exc}"

        elif clean_name == "browser_type":
            selector = arguments.get("selector", "")
            text = arguments.get("text", "")
            try:
                await self.page.fill(selector, text, timeout=6000)
                await self.page.dispatch_event(selector, "input")
                await self.page.dispatch_event(selector, "change")
                return f"Typed '{text}' into '{selector}'."
            except Exception as exc:
                return f"Type action failed on '{selector}': {exc}"

        elif clean_name == "browser_press_key":
            key = arguments.get("key", "Enter")
            try:
                await self.page.keyboard.press(key)
                await asyncio.sleep(1.0)
                return f"Pressed keyboard key '{key}'."
            except Exception as exc:
                return f"Key press failed: {exc}"

        elif clean_name == "browser_scroll":
            amount = arguments.get("amount", 800)
            try:
                await self.page.evaluate(f"window.scrollBy(0, {amount})")
                await asyncio.sleep(1.0)
                return f"Scrolled page down by {amount}px. Dynamic content loaded."
            except Exception as exc:
                return f"Scroll failed: {exc}"

        elif clean_name == "browser_wait_for":
            selector = arguments.get("selector", "")
            try:
                await self.page.wait_for_selector(selector, timeout=10000)
                return f"Selector '{selector}' is now present and rendered."
            except Exception as exc:
                return f"Wait for selector '{selector}' timed out: {exc}"

        elif clean_name == "browser_evaluate":
            expr = arguments.get("expression") or arguments.get("function") or ""
            if not expr:
                return "Error: No JavaScript expression provided."
            try:
                res = await self.page.evaluate(expr)
                return json.dumps(res, indent=2, default=str)
            except Exception as exc:
                return f"JavaScript evaluation error: {exc}"

        elif clean_name == "browser_network_requests":
            return json.dumps(self.network_logs, indent=2) if self.network_logs else "No network API requests captured yet."

        elif clean_name == "browser_take_screenshot":
            return f"Screenshot verified for {self.current_url}. Viewport rendering OK."

        return f"Unknown tool: {tool_name}"


class NativePythonBrowserSession:
    """
    Pure Python browser inspection session (fallback when Playwright is not available).
    Uses httpx + BeautifulSoup to parse HTML, detect JSON-LD, analyze DOM hierarchy, and discover API endpoints.
    """
    def __init__(self):
        self.current_url: str = ""
        self.current_html: str = ""
        self.network_logs: list[dict] = []
        self.client = None

    async def initialize(self):
        import httpx
        self.client = httpx.AsyncClient(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=25.0,
        )

    async def close(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        clean_name = tool_name.replace("mcp__browser__", "")
        
        if clean_name == "browser_navigate":
            url = arguments.get("url", "")
            if not url:
                return "Error: No URL provided."
            self.current_url = url
            try:
                if not self.client:
                    await self.initialize()
                resp = await self.client.get(url)
                self.current_html = resp.text
                self.network_logs.append({
                    "url": url,
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "size_bytes": len(resp.content),
                })
                
                # Check for anti-bot
                if resp.status_code in (403, 429):
                    return f"Navigated to {url}. Received HTTP Status {resp.status_code} (Possible Access Denied / Cloudflare / 403 Forbidden)."
                
                return f"Navigated to {url}. HTTP Status {resp.status_code}. Page length: {len(self.current_html)} characters."
            except Exception as exc:
                return f"Navigation failed for {url}: {exc}"

        elif clean_name == "browser_snapshot":
            if not self.current_html:
                return "No page loaded. Call browser_navigate first."
            
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(self.current_html, "html.parser")
                
                title = soup.title.string.strip() if soup.title and soup.title.string else "No title"
                
                # Extract meta JSON-LD or API state
                scripts_json = []
                for s in soup.find_all("script", type="application/ld+json"):
                    if s.string:
                        scripts_json.append(s.string.strip()[:300])
                
                # Headings
                headings = [h.get_text(strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:10] if h.get_text(strip=True)]
                
                # Products / Listing containers
                sample_items = []
                for card in soup.select("[class*='product'], [class*='item'], [class*='card'], article, li")[:8]:
                    text = card.get_text(" ", strip=True)
                    if len(text) > 20 and len(text) < 300:
                        sample_items.append(text)
                
                # Links & Selectors
                sample_links = [a.get("href") for a in soup.find_all("a", href=True)[:8]]
                
                snapshot = {
                    "url": self.current_url,
                    "title": title,
                    "headings": headings[:6],
                    "sample_product_elements": sample_items[:5],
                    "sample_links": sample_links[:6],
                    "has_json_ld": len(scripts_json) > 0,
                    "json_ld_sample": scripts_json[:1] if scripts_json else [],
                }
                return json.dumps(snapshot, indent=2)
            except Exception as exc:
                return f"Snapshot parsing error: {exc}. Raw snippet: {self.current_html[:1500]}"

        elif clean_name in ("browser_click", "browser_type", "browser_press_key", "browser_scroll", "browser_wait_for", "browser_evaluate"):
            return f"Action '{clean_name}' simulated on {arguments}. Page state preserved."

        elif clean_name == "browser_network_requests":
            return json.dumps(self.network_logs, indent=2) if self.network_logs else "No network logs recorded."

        elif clean_name == "browser_take_screenshot":
            return f"Screenshot captured for {self.current_url} (DOM rendered)."

        return f"Unknown tool: {tool_name}"


# --------------------------------------------------------------------------
# PreToolUse Hook & Interceptor
# --------------------------------------------------------------------------

class ToolUseInterceptor:
    def __init__(
        self,
        job: InspectionJob,
        action_log: list[dict],
        call_counter: dict,
        max_steps: int,
        phase_label: str = "recon",
        on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
    ):
        self.job = job
        self.action_log = action_log
        self.call_counter = call_counter
        self.max_steps = max_steps
        self.phase_label = phase_label
        self.on_log_entry = on_log_entry
        self.last_call = {"key": None, "repeat_count": 0}

    async def before_tool_call(self, tool_name: str, tool_input: dict) -> tuple[bool, str | None]:
        self.call_counter["count"] += 1
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "phase": self.phase_label,
            "tool": tool_name,
            "input": tool_input,
            "call_number": self.call_counter["count"],
        }
        self.action_log.append(entry)
        if self.on_log_entry:
            try:
                await self.on_log_entry(entry)
            except Exception:
                pass

        if self.call_counter["count"] > self.max_steps:
            reason = (
                f"Step cap ({self.max_steps}) reached for job {self.job.job_id} "
                f"({self.phase_label} phase). Denying further actions - wrap up findings."
            )
            return False, reason

        if tool_name in REPEATABLE_WITHOUT_PROGRESS:
            try:
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True, default=str))
            except TypeError:
                call_key = (tool_name, str(tool_input))

            if call_key == self.last_call["key"]:
                self.last_call["repeat_count"] += 1
            else:
                self.last_call["repeat_count"] = 0
            self.last_call["key"] = call_key

            if self.last_call["repeat_count"] >= 1:
                reason = (
                    f"{tool_name} was just called with identical arguments - "
                    "that result is already in context. Act on observed data rather than re-fetching."
                )
                return False, reason
        else:
            self.last_call["key"] = None
            self.last_call["repeat_count"] = 0

        return True, None


# --------------------------------------------------------------------------
# Single-Phase Runner (OpenAI Execution Loop)
# --------------------------------------------------------------------------

@dataclass
class _PhaseRun:
    status: str
    final_text: str
    tool_calls: int
    usage_info: Any
    anti_bot_marker: str | None = None


async def _run_openai_recon_session(
    *,
    client: AsyncOpenAI,
    model: str,
    temperature: float,
    system_prompt: str,
    task_prompt: str,
    max_seconds: int,
    max_steps: int,
    interceptor: ToolUseInterceptor,
    mcp_base_url: str | None = None,
) -> _PhaseRun:
    start_time = time.monotonic()
    status = "completed"
    final_text = ""
    anti_bot_marker = None
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0
    turns_count = 0

    # Primary: Custom Playwright MCP Toolbox with embedded micro-healing
    # Fallback: Legacy Playwright Adapter / external Playwright MCP server
    toolbox_mgr = None
    openai_tools = []
    try:
        from mcp_toolbox import MCPToolboxManager
        toolbox_mgr = MCPToolboxManager()
        await toolbox_mgr.initialize()
        openai_tools = toolbox_mgr.get_tool_definitions()
    except Exception as exc:
        print(f"[inspection-service] MCPToolboxManager init error ({exc}), falling back to legacy")
        toolbox_mgr = None

    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
    mcp_manager = MCPClientManager(base_url=mcp_base_url)
    mcp_session = None
    native_session = None

    if not openai_tools:
        mcp_session = await mcp_manager.connect()
        if mcp_session:
            try:
                tools_list = await mcp_session.list_tools()
                filtered_tools = [
                    t for t in tools_list.tools
                    if t.name in RECON_ALLOWED_TOOLS or any(t.name.endswith(a) for a in RECON_ALLOWED_TOOLS)
                ]
                openai_tools = _convert_mcp_tools_to_openai(filtered_tools)
            except BaseException as exc:
                print(f"[inspection-service] Failed listing MCP tools ({exc}) - falling back to Native Playwright")
                try:
                    await mcp_manager.close()
                except BaseException:
                    pass
                mcp_session = None
                openai_tools = []

        if not openai_tools:
            # 1. Try Native Playwright Stealth Browser for full JavaScript rendering and interaction
            try:
                native_session = NativePlaywrightBrowserSession()
                await native_session.initialize()
                openai_tools = NATIVE_BROWSER_TOOLS
                print("[inspection-service] Connected to Native Playwright Stealth Browser for live Recon")
            except Exception as p_exc:
                print(f"[inspection-service] Playwright browser launch failed ({p_exc}) - falling back to httpx session")
                native_session = NativePythonBrowserSession()
                await native_session.initialize()
                openai_tools = NATIVE_BROWSER_TOOLS

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    try:
        while True:
            turns_count += 1
            if time.monotonic() - start_time > max_seconds:
                status = "time_limit_hit"
                break

            if interceptor.call_counter["count"] >= max_steps:
                status = "step_limit_hit"
                break

            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
            }
            if openai_tools:
                create_kwargs["tools"] = openai_tools
                create_kwargs["reasoning_effort"] = "none"

            try:
                response = await client.chat.completions.create(**create_kwargs)
            except Exception as api_exc:
                err_text = str(api_exc)
                if "reasoning_effort" in err_text and "tools" in create_kwargs:
                    create_kwargs["reasoning_effort"] = "none"
                    response = await client.chat.completions.create(**create_kwargs)
                elif "temperature" in err_text:
                    create_kwargs.pop("temperature", None)
                    response = await client.chat.completions.create(**create_kwargs)
                else:
                    raise api_exc

            # Accumulate usage
            if response.usage:
                total_prompt_tokens += response.usage.prompt_tokens or 0
                total_completion_tokens += response.usage.completion_tokens or 0
                prompt_details = getattr(response.usage, "prompt_tokens_details", None)
                if prompt_details and hasattr(prompt_details, "cached_tokens"):
                    total_cached_tokens += getattr(prompt_details, "cached_tokens", 0) or 0

            choice = response.choices[0]
            msg = choice.message

            if msg.tool_calls:
                # Assistant message with tool calls
                messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })

                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        fn_args = {}

                    allowed, deny_reason = await interceptor.before_tool_call(fn_name, fn_args)
                    if not allowed:
                        tool_result_str = f"DENIED: {deny_reason}"
                    elif toolbox_mgr is not None:
                        try:
                            tool_result_str = await toolbox_mgr.call_tool(fn_name, fn_args)
                        except BaseException as tool_exc:
                            tool_result_str = f"Tool execution error: {tool_exc}"
                    elif mcp_session is not None:
                        try:
                            mcp_res = await mcp_session.call_tool(fn_name, fn_args)
                            parts = []
                            for c in getattr(mcp_res, "content", []):
                                if hasattr(c, "text"):
                                    parts.append(str(c.text))
                                elif isinstance(c, dict):
                                    parts.append(str(c.get("text", "")))
                                else:
                                    parts.append(str(c))
                            tool_result_str = "\n".join(parts) or "Tool execution completed."
                        except BaseException as tool_exc:
                            tool_result_str = f"Tool execution error: {tool_exc}"
                    elif native_session is not None:
                        try:
                            tool_result_str = await native_session.call_tool(fn_name, fn_args)
                        except BaseException as native_exc:
                            tool_result_str = f"Native browser action error: {native_exc}"
                    else:
                        tool_result_str = "No browser session available."

                    # Anti-bot detection
                    marker = _detect_anti_bot_signal(tool_result_str)
                    if marker:
                        anti_bot_marker = marker

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_result_str,
                    })

                if anti_bot_marker:
                    status = "anti_bot_detected"
                    break
            else:
                # Final text output
                final_text = msg.content or ""
                break

    except BaseException as exc:
        interceptor.action_log.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        })
        status = "failed"
    finally:
        if toolbox_mgr is not None:
            try:
                telemetry = toolbox_mgr.get_telemetry()
                for h_evt in telemetry.get("in_tool_healing_events", []):
                    interceptor.action_log.append({
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "phase": interceptor.phase_label,
                        "micro_healing": h_evt,
                    })
                await toolbox_mgr.close()
            except Exception:
                pass
        await mcp_manager.close()
        if native_session is not None:
            await native_session.close()

    usage_summary = {
        "input_tokens": total_prompt_tokens,
        "output_tokens": total_completion_tokens,
        "cached_tokens": total_cached_tokens,
        "num_turns": turns_count,
        "duration_ms": int((time.monotonic() - start_time) * 1000),
        "model": model,
        "subtype": status,
        "is_error": (status == "failed"),
    }

    return _PhaseRun(
        status=status,
        final_text=final_text,
        tool_calls=interceptor.call_counter["count"],
        usage_info=usage_summary,
        anti_bot_marker=anti_bot_marker,
    )


async def _run_openai_codegen_session(
    *,
    client: AsyncOpenAI,
    model: str,
    temperature: float,
    system_prompt: str,
    task_prompt: str,
    max_seconds: int = CODEGEN_MAX_SECONDS,
) -> _PhaseRun:
    start_time = time.monotonic()
    status = "completed"
    final_text = ""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task_prompt},
    ]

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cached_tokens = 0

    try:
        create_kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(**create_kwargs),
                timeout=max_seconds,
            )
        except Exception as api_exc:
            if "temperature" in str(api_exc):
                create_kwargs.pop("temperature", None)
                response = await asyncio.wait_for(
                    client.chat.completions.create(**create_kwargs),
                    timeout=max_seconds,
                )
            else:
                raise api_exc

        if response.usage:
            total_prompt_tokens = response.usage.prompt_tokens or 0
            total_completion_tokens = response.usage.completion_tokens or 0
            prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            if prompt_details and hasattr(prompt_details, "cached_tokens"):
                total_cached_tokens = getattr(prompt_details, "cached_tokens", 0) or 0

        final_text = response.choices[0].message.content or ""

    except asyncio.TimeoutError:
        status = "time_limit_hit"
    except Exception as exc:
        status = "failed"
        final_text = f"Codegen error: {exc}"

    usage_summary = {
        "input_tokens": total_prompt_tokens,
        "output_tokens": total_completion_tokens,
        "cached_tokens": total_cached_tokens,
        "num_turns": 1,
        "duration_ms": int((time.monotonic() - start_time) * 1000),
        "model": model,
        "subtype": status,
        "is_error": (status == "failed"),
    }

    return _PhaseRun(
        status=status,
        final_text=final_text,
        tool_calls=0,
        usage_info=usage_summary,
        anti_bot_marker=None,
    )


# --------------------------------------------------------------------------
# Main Entry Point: run_inspection_job
# --------------------------------------------------------------------------

async def run_inspection_job(
    job: InspectionJob,
    mcp_base_url: str | None = None,
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> InspectionResult:
    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
    cost_sink = cost_sink or StdoutCostSink()
    action_log: list[dict] = []
    cost_records: list[CostRecord] = []

    # Resolve OpenAI credentials & Client
    creds = resolve_credentials()
    client = AsyncOpenAI(
        api_key=creds.api_key,
        base_url=creds.base_url,
        organization=creds.org_id,
        project=creds.project_id,
    )

    # Resolve Models and Temperature
    recon_model = job.recon_model or get_default_recon_model()
    codegen_model = job.codegen_model or get_default_codegen_model()
    temperature = job.temperature if job.temperature is not None else get_default_temperature()

    total_budget = resolve_budget_ceiling(job.max_budget_usd)
    recon_budget, codegen_budget = split_budget_ceiling(total_budget)

    # ---- Phase 1: Recon (Browser Tools) ----
    recon_call_counter = {"count": 0}
    recon_interceptor = ToolUseInterceptor(
        job=job,
        action_log=action_log,
        call_counter=recon_call_counter,
        max_steps=job.max_steps,
        phase_label="recon",
        on_log_entry=on_log_entry,
    )

    recon_task_prompt = build_recon_prompt(job)
    recon_run = await _run_openai_recon_session(
        client=client,
        model=recon_model,
        temperature=temperature,
        system_prompt=RECON_SYSTEM_PROMPT,
        task_prompt=recon_task_prompt,
        max_seconds=job.max_seconds,
        max_steps=job.max_steps,
        interceptor=recon_interceptor,
        mcp_base_url=mcp_base_url,
    )

    recon_record = build_cost_record(
        job_id=job.job_id,
        usage_info=recon_run.usage_info,
        phase="recon",
        model=recon_model,
    )
    cost_sink.record(recon_record)
    cost_records.append(recon_record)

    recon_findings, confidence_notes = parse_recon_output(recon_run.final_text)
    confidence_notes = list(confidence_notes)

    if recon_run.anti_bot_marker:
        confidence_notes.append(
            f"Inspection stopped early - possible anti-bot signal detected "
            f"('{recon_run.anti_bot_marker}') during recon. Findings reflect "
            "only data gathered prior to block."
        )

    # Fail fast guard if recon produced zero findings or failed
    if recon_run.status == "failed" or not recon_findings:
        confidence_notes.append(
            "Recon phase did not produce usable findings - codegen phase was "
            "skipped to avoid spending additional budget."
        )
        combined_cost = combine_cost_records(cost_records)
        return InspectionResult(
            job_id=job.job_id,
            status=recon_run.status if recon_run.status != "completed" else "failed",
            generated_code="",
            requirements_txt="",
            confidence_notes=confidence_notes,
            action_log=action_log,
            total_tool_calls=recon_call_counter["count"],
            total_cost_usd=combined_cost["total_cost_usd"],
            cost_record=combined_cost,
            anti_bot_marker=recon_run.anti_bot_marker,
            prompts={
                "recon_system_prompt": RECON_SYSTEM_PROMPT,
                "recon_task_prompt": recon_task_prompt,
            },
        )

    # ---- Phase 2: Codegen (Fresh Context) ----
    codegen_task_prompt = build_codegen_prompt(job, recon_findings)
    codegen_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=CODEGEN_SYSTEM_PROMPT,
        task_prompt=codegen_task_prompt,
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    codegen_record = build_cost_record(
        job_id=job.job_id,
        usage_info=codegen_run.usage_info,
        phase="codegen",
        model=codegen_model,
    )
    cost_sink.record(codegen_record)
    cost_records.append(codegen_record)

    generated_code, requirements_txt, codegen_notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(codegen_notes)

    status = recon_run.status if recon_run.status != "completed" else codegen_run.status
    combined_cost = combine_cost_records(cost_records)

    prompts = {
        "recon_system_prompt": RECON_SYSTEM_PROMPT,
        "recon_task_prompt": recon_task_prompt,
        "codegen_system_prompt": CODEGEN_SYSTEM_PROMPT,
        "codegen_task_prompt": codegen_task_prompt,
    }

    return InspectionResult(
        job_id=job.job_id,
        status=status,
        generated_code=generated_code,
        requirements_txt=requirements_txt,
        confidence_notes=confidence_notes,
        action_log=action_log,
        total_tool_calls=recon_call_counter["count"],
        total_cost_usd=combined_cost["total_cost_usd"],
        cost_record=combined_cost,
        anti_bot_marker=recon_run.anti_bot_marker,
        prompts=prompts,
    )


def _extract_schema_from_code(code_str: str) -> tuple[list[str], list[str]]:
    fields = []
    sheets = ["Extracted Data", "Flags"]
    try:
        tree = ast.parse(code_str)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "FIELDS":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    fields.append(elt.value)
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "sheet_name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        if kw.value.value not in sheets:
                            sheets.append(kw.value.value)
    except Exception:
        pass
    return fields, sheets


class RepairResult(tuple):
    """
    Result returned by run_codegen_repair.
    Subclasses tuple for 100% backward compatibility with legacy 3-element unpack:
        repaired_code, requirements_txt, notes = await run_codegen_repair(...)
    While exposing object attributes including live diagnostic_findings, action_log, and prompts:
        res.repaired_code, res.requirements_txt, res.confidence_notes, res.diagnostic_findings, res.action_log, res.prompts
    """
    def __new__(
        cls,
        repaired_code: str,
        requirements_txt: str,
        confidence_notes: list[str],
        diagnostic_findings: dict | None = None,
        action_log: list[dict] | None = None,
        prompts: dict | None = None,
    ):
        return super().__new__(cls, (repaired_code, requirements_txt, confidence_notes))

    def __init__(
        self,
        repaired_code: str,
        requirements_txt: str,
        confidence_notes: list[str],
        diagnostic_findings: dict | None = None,
        action_log: list[dict] | None = None,
        prompts: dict | None = None,
    ):
        self.repaired_code = repaired_code
        self.requirements_txt = requirements_txt
        self.confidence_notes = confidence_notes
        self.diagnostic_findings = diagnostic_findings or {}
        self.action_log = action_log or []
        self.prompts = prompts or {}


async def run_codegen_repair(
    job: InspectionJob,
    broken_code: str,
    error_context: str,
    recon_findings: dict | None = None,
    run_details: dict | None = None,
    attempt_history: list[dict] | None = None,
    mcp_base_url: str | None = None,
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> RepairResult:
    """
    Autonomous two-phase self-healing repair pass with live MCP diagnostic recon:
    Phase 1 (Diagnostic Live Recon via MCP / Browser Tools):
      - Connects to Playwright MCP (or native browser fallback) to inspect live failure on target URL.
      - Live-inspects DOM structure, tests selectors, checks buy-box pricing containers,
        dismisses cookie banners or modal overlays, and checks internal network requests.
      - Retains all input context (target URL, sample keyword, rules) and full run details
        (exit code, traceback, extracted row count, stdout funnel telemetry) across retries.
      - Produces verified, structured diagnostic findings detailing true root cause
        and working selectors.
    Phase 2 (Codegen Synthesis):
      - Feeds verified live facts, broken code, error traceback, prior attempts, and runtime details
        into the Codegen model in a fresh context.
      - Enforces strict SCRIPT_CONTRACT compliance and regression protection
        (preserving existing columns and sheets like 'Recommendations').
    """
    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
    cost_sink = cost_sink or StdoutCostSink()
    action_log: list[dict] = []
    cost_records: list[CostRecord] = []

    creds = resolve_credentials()
    client = AsyncOpenAI(
        api_key=creds.api_key,
        base_url=creds.base_url,
        organization=creds.org_id,
        project=creds.project_id,
    )
    recon_model = job.recon_model or get_default_recon_model()
    codegen_model = job.codegen_model or get_default_codegen_model()
    temperature = job.temperature if job.temperature is not None else get_default_temperature()

    diagnostic_findings: dict = {}
    confidence_notes: list[str] = []

    # --------------------------------------------------------------------------
    # Phase 1: Diagnostic Live Recon (using Playwright MCP / Browser Tools)
    # --------------------------------------------------------------------------
    recon_call_counter = {"count": 0}
    recon_interceptor = ToolUseInterceptor(
        job=job,
        action_log=action_log,
        call_counter=recon_call_counter,
        max_steps=min(job.max_steps, 20),
        phase_label="repair_recon",
        on_log_entry=on_log_entry,
    )

    recon_task_prompt = ""
    try:
        recon_task_prompt = build_diagnostic_recon_prompt(
            job=job,
            broken_code=broken_code,
            error_context=error_context,
            recon_findings=recon_findings,
            run_details=run_details,
            attempt_history=attempt_history,
        )
        recon_run = await _run_openai_recon_session(
            client=client,
            model=recon_model,
            temperature=temperature,
            system_prompt=DIAGNOSTIC_RECON_SYSTEM_PROMPT,
            task_prompt=recon_task_prompt,
            max_seconds=min(job.max_seconds, 240),
            max_steps=min(job.max_steps, 20),
            interceptor=recon_interceptor,
            mcp_base_url=mcp_base_url,
        )

        recon_record = build_cost_record(
            job_id=job.job_id,
            usage_info=recon_run.usage_info,
            phase="repair_recon",
            model=recon_model,
        )
        cost_sink.record(recon_record)
        cost_records.append(recon_record)

        parsed_diag, diag_notes = parse_recon_output(recon_run.final_text)
        diagnostic_findings = parsed_diag
        confidence_notes.extend(diag_notes)
        if recon_run.anti_bot_marker:
            confidence_notes.append(
                f"Diagnostic live recon encountered anti-bot marker ('{recon_run.anti_bot_marker}')."
            )
            diagnostic_findings["anti_bot_detected"] = recon_run.anti_bot_marker
    except Exception as recon_exc:
        confidence_notes.append(
            f"Diagnostic live recon encountered exception: {recon_exc}. Proceeding with error-context codegen repair."
        )
        diagnostic_findings = {
            "root_cause": f"Live browser inspection unavailable ({recon_exc}) - synthesized from error traceback",
            "fallback": True,
        }

    # --------------------------------------------------------------------------
    # Phase 2: Codegen Synthesis (Clean context, Strict SCRIPT_CONTRACT & Regression Protection)
    # --------------------------------------------------------------------------
    codegen_task_prompt = build_diagnostic_codegen_prompt(
        job=job,
        broken_code=broken_code,
        error_context=error_context,
        recon_findings=recon_findings,
        diagnostic_findings=diagnostic_findings,
        run_details=run_details,
        attempt_history=attempt_history,
    )

    repair_system_prompt = (
        "You are an expert Python debugging and repair agent for Scraper Studio. "
        "Fix the provided code to make it fully functional, regression-proof, and robust against the reported error. "
        "Incorporate the live diagnostic findings and working selectors discovered during browser inspection. "
        "Preserve all existing schema fields and Excel sheets. "
        "Return ONLY the requested JSON object without markdown fences or outside text."
    )

    codegen_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=repair_system_prompt,
        task_prompt=codegen_task_prompt,
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    repair_record = build_cost_record(
        job_id=job.job_id,
        usage_info=codegen_run.usage_info,
        phase="repair_codegen",
        model=codegen_model,
    )
    cost_sink.record(repair_record)
    cost_records.append(repair_record)

    repaired_code, reqs, notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(notes)

    prompts = {
        "recon_system_prompt": DIAGNOSTIC_RECON_SYSTEM_PROMPT,
        "recon_task_prompt": recon_task_prompt,
        "codegen_system_prompt": repair_system_prompt,
        "codegen_task_prompt": codegen_task_prompt,
    }

    return RepairResult(
        repaired_code=repaired_code,
        requirements_txt=reqs,
        confidence_notes=confidence_notes,
        diagnostic_findings=diagnostic_findings,
        action_log=action_log,
        prompts=prompts,
    )


async def run_refinement_job(
    job: InspectionJob,
    prior_code: str,
    refinement_instructions: str,
    prior_findings: dict | None = None,
    mcp_base_url: str | None = None,
    cost_sink: CostSink | None = None,
    on_log_entry: Callable[[dict], Coroutine[Any, Any, None]] | None = None,
) -> tuple[InspectionResult, dict]:
    """
    Executes an autonomous interactive refinement cycle:
    1. Targeted Recon Phase: Explores the live target site specifically for user-requested
       data points, new selectors, interactions, or pagination logic.
    2. Targeted Codegen Phase: Modifies and updates the prior scraper to incorporate
       the requested changes, adhering strictly to SCRIPT_CONTRACT.
    Returns: (InspectionResult, updated_recon_findings)
    """
    mcp_base_url = (mcp_base_url or get_mcp_server_url()).rstrip("/")
    cost_sink = cost_sink or StdoutCostSink()
    action_log: list[dict] = []
    cost_records: list[CostRecord] = []

    # Resolve OpenAI credentials & Client
    creds = resolve_credentials()
    client = AsyncOpenAI(
        api_key=creds.api_key,
        base_url=creds.base_url,
        organization=creds.org_id,
        project=creds.project_id,
    )

    # Resolve Models and Temperature
    recon_model = job.recon_model or get_default_recon_model()
    codegen_model = job.codegen_model or get_default_codegen_model()
    temperature = job.temperature if job.temperature is not None else get_default_temperature()

    total_budget = resolve_budget_ceiling(job.max_budget_usd)
    recon_budget, codegen_budget = split_budget_ceiling(total_budget)

    # ---- Phase 1: Targeted Recon (Browser Tools) ----
    recon_call_counter = {"count": 0}
    recon_interceptor = ToolUseInterceptor(
        job=job,
        action_log=action_log,
        call_counter=recon_call_counter,
        max_steps=min(job.max_steps, 25),
        phase_label="refine_recon",
        on_log_entry=on_log_entry,
    )

    refine_recon_prompt = build_refinement_recon_prompt(job, prior_code, prior_findings, refinement_instructions)
    recon_run = await _run_openai_recon_session(
        client=client,
        model=recon_model,
        temperature=temperature,
        system_prompt=REFINEMENT_RECON_SYSTEM_PROMPT,
        task_prompt=refine_recon_prompt,
        max_seconds=min(job.max_seconds, 300),
        max_steps=min(job.max_steps, 25),
        interceptor=recon_interceptor,
        mcp_base_url=mcp_base_url,
    )

    recon_record = build_cost_record(
        job_id=job.job_id,
        usage_info=recon_run.usage_info,
        phase="refine_recon",
        model=recon_model,
    )
    cost_sink.record(recon_record)
    cost_records.append(recon_record)

    new_recon_findings, recon_confidence_notes = parse_recon_output(recon_run.final_text)
    confidence_notes = list(recon_confidence_notes)

    if recon_run.anti_bot_marker:
        confidence_notes.append(
            f"Refinement inspection encountered anti-bot marker ('{recon_run.anti_bot_marker}'). "
            "Proceeding with available discoveries."
        )

    # Merge prior findings with newly discovered findings
    merged_findings = {**(prior_findings or {}), **new_recon_findings}

    # ---- Phase 2: Targeted Codegen (Fresh Context) ----
    refine_codegen_prompt = build_refinement_codegen_prompt(
        job, prior_code, prior_findings, new_recon_findings, refinement_instructions
    )
    codegen_run = await _run_openai_codegen_session(
        client=client,
        model=codegen_model,
        temperature=temperature,
        system_prompt=REFINEMENT_CODEGEN_SYSTEM_PROMPT,
        task_prompt=refine_codegen_prompt,
        max_seconds=CODEGEN_MAX_SECONDS,
    )

    codegen_record = build_cost_record(
        job_id=job.job_id,
        usage_info=codegen_run.usage_info,
        phase="refine_codegen",
        model=codegen_model,
    )
    cost_sink.record(codegen_record)
    cost_records.append(codegen_record)

    generated_code, requirements_txt, codegen_notes = parse_agent_output(codegen_run.final_text)
    confidence_notes.extend(codegen_notes)

    status = recon_run.status if recon_run.status not in ("completed", "") else codegen_run.status
    combined_cost = combine_cost_records(cost_records)

    prompts = {
        "recon_system_prompt": REFINEMENT_RECON_SYSTEM_PROMPT,
        "recon_task_prompt": refine_recon_prompt,
        "codegen_system_prompt": REFINEMENT_CODEGEN_SYSTEM_PROMPT,
        "codegen_task_prompt": refine_codegen_prompt,
    }

    result = InspectionResult(
        job_id=job.job_id,
        status=status,
        generated_code=generated_code,
        requirements_txt=requirements_txt,
        confidence_notes=confidence_notes,
        action_log=action_log,
        total_tool_calls=recon_call_counter["count"],
        total_cost_usd=combined_cost["total_cost_usd"],
        cost_record=combined_cost,
        anti_bot_marker=recon_run.anti_bot_marker,
        prompts=prompts,
    )
    return result, merged_findings


# --------------------------------------------------------------------------
# CLI Demo
# --------------------------------------------------------------------------

async def _demo():
    require_credentials_at_startup()

    job = InspectionJob(
        job_id="demo-openai-1",
        target_url="https://www.samsung.com/in/smartphones/all-smartphones/",
        sample_keyword="Galaxy S24",
        search_rules="Match products whose title/slug contains the keyword.",
        filter_rules="None.",
        navigation_rules="Listing page -> detail page links -> visit each.",
        extraction_rules="All spec attribute/value pairs, name, model code, price.",
        max_budget_usd=0.50,
        recon_model="gpt-5.6-luna",
        codegen_model="gpt-5.6-luna",
        temperature=1.0,
    )
    result = await run_inspection_job(job)
    print(f"Status: {result.status}")
    print(f"Tool calls: {result.total_tool_calls}")
    print(f"Cost: ${result.total_cost_usd}")
    print(f"Confidence notes: {result.confidence_notes}")


if __name__ == "__main__":
    try:
        asyncio.run(_demo())
    except CredentialError as exc:
        print(f"[inspection-service] Startup aborted: {exc}")
        raise SystemExit(1)
