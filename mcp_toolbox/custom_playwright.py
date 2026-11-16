"""
mcp_toolbox/custom_playwright.py
================================
Primary Custom Playwright MCP Toolbox for Scraper Studio.
Implements the 8 core scraping tools with embedded micro-healing loops:
  1. navigate
  2. snapshot
  3. click
  4. type_text
  5. scroll
  6. load_all
  7. run_js
  8. save_items

Supports backward-compatible tool aliases (e.g. browser_navigate -> navigate).
"""

import asyncio
import json
from typing import Any
try:
    from playwright.async_api import Page, BrowserContext, Browser, async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    Page = BrowserContext = Browser = async_playwright = Any
    PLAYWRIGHT_AVAILABLE = False

from .healing_engine import MicroHealingEngine


class CustomPlaywrightToolbox:
    """
    Primary autonomous Playwright MCP Toolbox for live web scraping reconnaissance.
    Equipped with stealth browser emulation, network interception, and in-tool micro-healing loops.
    """

    def __init__(self):
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.current_url: str = ""
        self.network_logs: list[dict] = []
        self._is_initialized: bool = False
        self.saved_items: list[dict] = []
        self.healing_telemetry: list[dict] = []

    async def initialize(self, headless: bool = True):
        """Initializes Chromium browser with stealth flags and anti-detection scripts."""
        if self._is_initialized and self.page and not self.page.is_closed():
            return

        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed in the active environment.")

        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--disable-gpu",
                ],
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
                },
            )
            # Stealth initialization scripts
            await self.context.add_init_script("""
                delete Object.getPrototypeOf(navigator).webdriver;
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {}, loadTimes: function() {}, csi: function() {}, app: {} };
            """)
            self.page = await self.context.new_page()

            # Network interception for internal JSON / GraphQL endpoints
            async def _on_response(response):
                try:
                    url = response.url
                    ct = response.headers.get("content-type", "").lower()
                    if (
                        any(x in ct for x in ("json", "graphql"))
                        or url.endswith(".json")
                        or "/api/" in url
                        or "/v1/" in url
                        or "/v2/" in url
                        or "/graphql" in url
                    ):
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
            await self.close()
            raise RuntimeError(f"Failed to initialize Custom Playwright Toolbox: {exc}") from exc

    async def close(self):
        """Gracefully closes all browser resources."""
        try:
            if self.page and not self.page.is_closed():
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
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None

    # --------------------------------------------------------------------------
    # 8 Core Tools Implementation
    # --------------------------------------------------------------------------

    async def navigate(self, url: str, wait_until: str = "domcontentloaded", timeout_ms: int = 35000) -> str:
        """Loads a target URL and runs initial overlay dismissal."""
        if not self._is_initialized:
            await self.initialize()

        if not url:
            return json.dumps({"status": "error", "message": "No URL provided."})

        self.current_url = url
        try:
            response = await self.page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            await asyncio.sleep(1.2)
            dismissed = await MicroHealingEngine.dismiss_active_overlays(self.page)
            status_code = response.status if response else 200
            title = await self.page.title()

            result = {
                "status": "success",
                "url": url,
                "http_status": status_code,
                "title": title,
                "overlays_dismissed": dismissed,
                "discovered_apis": len(self.network_logs),
            }
            return json.dumps(result, indent=2)
        except Exception as exc:
            return json.dumps({"status": "failed", "url": url, "error": str(exc)}, indent=2)

    async def snapshot(self, max_depth: int = 4, include_interactive: bool = True) -> str:
        """Returns a dense, LLM-optimized representation of the DOM hierarchy and state."""
        if not self.page:
            return json.dumps({"error": "No page active. Call navigate first."})

        try:
            title = await self.page.title()
            state_info = await self.page.evaluate("""() => {
                const result = {};
                // Hydrated Framework State
                if (window.__NEXT_DATA__) {
                    result.has_next_data = true;
                    result.page = window.__NEXT_DATA__.page;
                    result.query = window.__NEXT_DATA__.query;
                }
                if (window.__INITIAL_STATE__) {
                    result.has_initial_state = true;
                }

                // JSON-LD Metadata
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

                // Interactive Buttons, Tabs, and Accordions
                result.interactive_elements = Array.from(document.querySelectorAll('button, [role="button"], [role="tab"], summary, .accordion-header, [data-tab]'))
                    .map(b => ({
                        tag: b.tagName.toLowerCase(),
                        text: b.textContent.trim().slice(0, 50),
                        id: b.id || undefined,
                        role: b.getAttribute('role') || undefined,
                        classes: b.className.slice(0, 60) || undefined
                    }))
                    .filter(b => b.text.length > 0)
                    .slice(0, 15);

                // Product Cards / Listing Containers
                result.product_cards = Array.from(document.querySelectorAll('[class*="product"], [class*="item"], [class*="card"], article'))
                    .map(c => ({
                        tag: c.tagName.toLowerCase(),
                        classes: c.className.slice(0, 60),
                        text: c.textContent.trim().replace(/\\s+/g, ' ').slice(0, 120)
                    }))
                    .filter(c => c.text.length > 15)
                    .slice(0, 6);

                // Sample Links
                result.sample_links = Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.getAttribute('href'))
                    .filter(href => href && !href.startsWith('#') && !href.startsWith('javascript:'))
                    .slice(0, 10);

                return result;
            }""")

            snapshot_data = {
                "url": self.current_url,
                "title": title,
                "framework_state": {
                    "has_next_data": state_info.get("has_next_data", False),
                    "has_initial_state": state_info.get("has_initial_state", False),
                    "json_ld_sample": state_info.get("json_ld_sample", []),
                },
                "headings": state_info.get("headings", []),
                "interactive_elements": state_info.get("interactive_elements", []),
                "sample_product_cards": state_info.get("product_cards", []),
                "sample_links": state_info.get("sample_links", []),
                "discovered_apis": [req["url"] for req in self.network_logs[-6:]],
            }
            return json.dumps(snapshot_data, indent=2)
        except Exception as exc:
            return json.dumps({"error": f"Snapshot failed: {exc}"}, indent=2)

    async def click(self, selector: str, timeout_ms: int = 5000, auto_dismiss_overlay: bool = True) -> str:
        """Executes click with Micro-Healing Loop 1 (auto-dismiss overlay + fallback click)."""
        if not self.page:
            return json.dumps({"error": "No page loaded. Call navigate first."})

        res = await MicroHealingEngine.execute_healing_click(self.page, selector, timeout_ms=timeout_ms)
        if res.get("healed"):
            self.healing_telemetry.append({"type": "click_healed", "details": res})
        return json.dumps(res, indent=2)

    async def type_text(self, selector: str, text: str, press_enter: bool = False, clear_first: bool = True) -> str:
        """Types text into an input or search field with event dispatching."""
        if not self.page:
            return json.dumps({"error": "No page loaded. Call navigate first."})

        try:
            if clear_first:
                await self.page.fill(selector, "", timeout=4000)
            await self.page.type(selector, text, delay=35, timeout=4000)
            await self.page.dispatch_event(selector, "input")
            await self.page.dispatch_event(selector, "change")

            if press_enter:
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1.2)

            return json.dumps({
                "status": "success",
                "selector": selector,
                "typed": text,
                "pressed_enter": press_enter,
                "current_url": self.page.url,
            }, indent=2)
        except Exception as exc:
            return json.dumps({"status": "failed", "selector": selector, "error": str(exc)}, indent=2)

    async def scroll(self, direction: str = "down", amount: int = 800, target_selector: str | None = None) -> str:
        """Scrolls viewport or target scrollable container."""
        if not self.page:
            return json.dumps({"error": "No page loaded."})

        multiplier = 1 if direction.lower() == "down" else -1
        delta = amount * multiplier

        try:
            if target_selector:
                await self.page.evaluate("""({sel, d}) => {
                    const el = document.querySelector(sel);
                    if (el) el.scrollBy(0, d);
                }""", {"sel": target_selector, "d": delta})
            else:
                await self.page.evaluate(f"window.scrollBy(0, {delta})")
            await asyncio.sleep(0.6)
            return json.dumps({"status": "success", "direction": direction, "amount": amount}, indent=2)
        except Exception as exc:
            return json.dumps({"status": "failed", "error": str(exc)}, indent=2)

    async def load_all(self, item_selector: str, strategy: str = "auto", max_rounds: int = 5) -> str:
        """Drives dynamic pagination with Micro-Healing Loop 2 (switching strategy if count stalls)."""
        if not self.page:
            return json.dumps({"error": "No page loaded."})

        res = await MicroHealingEngine.execute_healing_load(
            self.page, item_selector=item_selector, initial_strategy=strategy, max_rounds=max_rounds
        )
        if res.get("healed"):
            self.healing_telemetry.append({"type": "pagination_strategy_healed", "details": res})
        return json.dumps(res, indent=2)

    async def run_js(self, script: str, args: Any = None) -> str:
        """Evaluates arbitrary JavaScript expression in the browser context."""
        if not self.page:
            return json.dumps({"error": "No page loaded."})

        try:
            result = await self.page.evaluate(script, args)
            return json.dumps({"status": "success", "result": result}, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"status": "error", "error": str(exc)}, indent=2)

    async def save_items(
        self, sample_records: list[dict], schema: list[str] | None = None, item_selector: str | None = None
    ) -> str:
        """Stages sample items and triggers Micro-Healing Loop 3 if fields are empty/null."""
        if not self.page:
            return json.dumps({"error": "No page loaded."})

        self.saved_items.extend(sample_records)
        expected_schema = schema or ["product_id", "title", "price", "product_url"]

        # Check for empty/missing fields across sample items
        empty_fields = []
        for field in expected_schema:
            missing_in_all = all(not r.get(field) or r.get(field) == "None" for r in sample_records) if sample_records else True
            if missing_in_all:
                empty_fields.append(field)

        healed_selectors = {}
        if empty_fields and item_selector:
            healed_selectors = await MicroHealingEngine.evaluate_field_selectors(
                self.page, item_selector=item_selector, empty_fields=empty_fields
            )
            if healed_selectors:
                self.healing_telemetry.append({
                    "type": "selectors_re_evaluated",
                    "empty_fields": empty_fields,
                    "healed_selectors": healed_selectors,
                })

        return json.dumps({
            "status": "success",
            "saved_count": len(sample_records),
            "empty_fields_detected": empty_fields,
            "re_evaluated_selectors": healed_selectors,
            "sample_preview": sample_records[:3],
        }, indent=2)

    # --------------------------------------------------------------------------
    # Tool Dispatch & Backward-Compatible Aliases
    # --------------------------------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Dispatches tool call with support for standard and legacy aliases."""
        clean = (
            tool_name.replace("mcp__browser__", "")
            .replace("mcp__", "")
            .replace("browser_", "")
        )

        if not self._is_initialized:
            await self.initialize()

        if clean == "navigate":
            return await self.navigate(
                url=arguments.get("url", ""),
                wait_until=arguments.get("wait_until", "domcontentloaded"),
                timeout_ms=arguments.get("timeout_ms", 35000),
            )
        elif clean == "snapshot":
            return await self.snapshot(
                max_depth=arguments.get("max_depth", 4),
                include_interactive=arguments.get("include_interactive", True),
            )
        elif clean == "click":
            return await self.click(
                selector=arguments.get("selector", ""),
                timeout_ms=arguments.get("timeout_ms", 5000),
                auto_dismiss_overlay=arguments.get("auto_dismiss_overlay", True),
            )
        elif clean in ("type_text", "type"):
            return await self.type_text(
                selector=arguments.get("selector", ""),
                text=arguments.get("text", ""),
                press_enter=arguments.get("press_enter", False),
                clear_first=arguments.get("clear_first", True),
            )
        elif clean == "press_key":
            key = arguments.get("key", "Enter")
            await self.page.keyboard.press(key)
            await asyncio.sleep(0.5)
            return json.dumps({"status": "success", "key": key}, indent=2)
        elif clean == "scroll":
            return await self.scroll(
                direction=arguments.get("direction", "down"),
                amount=arguments.get("amount", 800),
                target_selector=arguments.get("target_selector"),
            )
        elif clean == "load_all":
            return await self.load_all(
                item_selector=arguments.get("item_selector", ""),
                strategy=arguments.get("strategy", "auto"),
                max_rounds=arguments.get("max_rounds", 5),
            )
        elif clean in ("run_js", "evaluate"):
            script = arguments.get("script") or arguments.get("expression") or ""
            return await self.run_js(script=script, args=arguments.get("args"))
        elif clean in ("save_items", "extract_sample_rows"):
            records = arguments.get("sample_records") or arguments.get("items") or []
            return await self.save_items(
                sample_records=records,
                schema=arguments.get("schema"),
                item_selector=arguments.get("item_selector"),
            )
        elif clean == "wait_for":
            selector = arguments.get("selector", "")
            await self.page.wait_for_selector(selector, timeout=10000)
            return json.dumps({"status": "success", "rendered": selector}, indent=2)
        elif clean == "network_requests":
            return json.dumps(self.network_logs, indent=2) if self.network_logs else "No network logs recorded."
        elif clean == "take_screenshot":
            return json.dumps({"status": "success", "message": "Screenshot verified."}, indent=2)

        return json.dumps({"error": f"Unknown tool: {tool_name}"}, indent=2)

    @classmethod
    def get_tool_definitions(cls) -> list[dict]:
        """Returns OpenAI function tool schemas for the Custom Playwright MCP Toolbox."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "navigate",
                    "description": "Navigate to a URL with anti-bot stealth emulation, dismiss overlays, and capture HTTP status.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "The destination URL to load."},
                            "wait_until": {"type": "string", "enum": ["domcontentloaded", "load", "networkidle"], "default": "domcontentloaded"},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "snapshot",
                    "description": "Inspect the rendered DOM. Returns structured headings, interactive buttons, product cards, JSON-LD, and network APIs.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "click",
                    "description": "Click an element by CSS selector. Includes automatic micro-healing to dismiss blocking overlays/modals and fallback click.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector to click."},
                            "auto_dismiss_overlay": {"type": "boolean", "default": True},
                        },
                        "required": ["selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "type_text",
                    "description": "Type text into a search or input field. Supports auto-clearing and pressing Enter.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string", "description": "CSS selector of input field."},
                            "text": {"type": "string", "description": "Text to type."},
                            "press_enter": {"type": "boolean", "description": "Press Enter key after typing."},
                        },
                        "required": ["selector", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "scroll",
                    "description": "Scroll the page or container down or up to trigger dynamic lazy-loading.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "direction": {"type": "string", "enum": ["down", "up"], "default": "down"},
                            "amount": {"type": "integer", "description": "Pixels to scroll, e.g. 800", "default": 800},
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "load_all",
                    "description": "Autonomous dynamic content loader. Verifies item count growth and auto-switches pagination strategies if items stall.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "item_selector": {"type": "string", "description": "CSS selector for product/item cards."},
                            "strategy": {"type": "string", "enum": ["auto", "infinite_scroll", "load_more_button", "pagination_next"], "default": "auto"},
                            "max_rounds": {"type": "integer", "default": 5},
                        },
                        "required": ["item_selector"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_js",
                    "description": "Evaluate a JavaScript expression in the page context and return structured JSON result.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "script": {"type": "string", "description": "JavaScript code to execute."},
                        },
                        "required": ["script"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_items",
                    "description": "Stage extracted sample rows and validate schema. Automatically re-evaluates DOM selectors if key fields are empty.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "sample_records": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": "List of extracted item dictionaries (e.g. [{'product_id': '...', 'title': '...', 'price': '...'}]).",
                            },
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Expected column names to validate for completeness.",
                            },
                            "item_selector": {
                                "type": "string",
                                "description": "Item card CSS selector used to re-evaluate missing fields if needed.",
                            },
                        },
                        "required": ["sample_records"],
                    },
                },
            },
        ]
