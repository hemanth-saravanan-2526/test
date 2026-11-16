"""
mcp_toolbox/healing_engine.py
=============================
In-Tool Micro-Healing Engine for Scraper Studio's Custom Playwright MCP.
Provides runtime self-healing during Phase 1 Live Interactive Reconnaissance:
  - Micro-Healing Loop 1: Element / Click Blocked -> Auto-Dismiss Overlay & Fallback Click
  - Micro-Healing Loop 2: Items Not Growing -> Switch Pagination / Load-More Strategy
  - Micro-Healing Loop 3: Empty Fields -> Re-evaluate DOM Selectors & JSON-LD
"""

import asyncio
import json
import re
from typing import Any


class MicroHealingEngine:
    """Specialized micro-healing routines operating directly inside Playwright tool actions."""

    OVERLAY_SELECTORS = [
        "button:has-text('Accept all')",
        "button:has-text('Accept All')",
        "button:has-text('Accept')",
        "button:has-text('I accept')",
        "button:has-text('I agree')",
        "button:has-text('Allow all')",
        "button:has-text('Allow All')",
        "button:has-text('Agree')",
        "button:has-text('Got it')",
        "button:has-text('Continue')",
        "button:has-text('Close')",
        "button:has-text('Dismiss')",
        "button[aria-label*='Close' i]",
        "button[aria-label*='close' i]",
        "button[aria-label*='dismiss' i]",
        "[id*='cookie' i] button",
        "[id*='consent' i] button",
        "[class*='cookie' i] button",
        "[class*='consent' i] button",
        "[id*='onetrust-accept']",
        "[class*='close-modal' i]",
        "[class*='modal-close' i]",
        ".modal-header .close",
        "[aria-modal='true'] button",
    ]

    LOAD_MORE_SELECTORS = [
        "button:has-text('Load more')",
        "button:has-text('Load More')",
        "button:has-text('Show more')",
        "button:has-text('Show More')",
        "button:has-text('View more')",
        "button:has-text('View More')",
        "a:has-text('Load more')",
        "a:has-text('Show more')",
        "[data-action*='more' i]",
        "[class*='load-more' i]",
        "[class*='show-more' i]",
        "[id*='loadMore' i]",
        "[id*='load-more' i]",
    ]

    PAGINATION_NEXT_SELECTORS = [
        "a[rel='next']",
        "link[rel='next']",
        "button[aria-label*='Next' i]",
        "a[aria-label*='Next' i]",
        ".pagination .next a",
        ".pagination li.next a",
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a:has-text('Next >')",
        "a:has-text('Next »')",
        "[class*='pagination'] [class*='next']",
    ]

    # --------------------------------------------------------------------------
    # Micro-Healing Loop 1: Click Interception & Overlay Dismissal
    # --------------------------------------------------------------------------

    @classmethod
    async def dismiss_active_overlays(cls, page) -> list[str]:
        """Scans for and dismisses modal overlays, cookie banners, and backdrops."""
        if not page:
            return []
        dismissed = []

        # 1. Check common button selectors
        for sel in cls.OVERLAY_SELECTORS:
            try:
                locator = page.locator(sel).first
                if await locator.is_visible(timeout=250):
                    await locator.click(timeout=800)
                    dismissed.append(sel)
                    await asyncio.sleep(0.3)
                    break
            except Exception:
                pass

        # 2. Press Escape key as a universal dialog dismisser
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
        except Exception:
            pass

        # 3. Remove lingering high z-index backdrop overlays via JS if present
        try:
            removed_backdrops = await page.evaluate("""() => {
                let removed = 0;
                const backdrops = document.querySelectorAll('.modal-backdrop, .overlay, [class*="backdrop"], [class*="overlay-shade"]');
                backdrops.forEach(el => {
                    el.remove();
                    removed++;
                });
                return removed;
            }""")
            if removed_backdrops > 0:
                dismissed.append(f"removed_{removed_backdrops}_backdrops")
        except Exception:
            pass

        return dismissed

    @classmethod
    async def execute_healing_click(cls, page, selector: str, timeout_ms: int = 5000) -> dict:
        """
        Executes an interactive click with automated in-tool healing.
        If blocked or intercepted:
          1. Auto-dismisses active overlays / modals.
          2. Retries click with auto-wait.
          3. Falls back to JavaScript direct dispatch.
        """
        if not page:
            return {"status": "error", "message": "No active page session."}

        # Attempt 1: Direct native click
        try:
            await page.click(selector, timeout=timeout_ms)
            await asyncio.sleep(0.6)
            return {
                "status": "success",
                "selector": selector,
                "strategy": "native_click",
                "healed": False,
                "current_url": page.url,
            }
        except Exception as direct_err:
            pass

        # Micro-Healing Loop: Auto-Dismiss Overlay & Fallback
        dismissed = await cls.dismiss_active_overlays(page)

        # Attempt 2: Retry native click after overlay dismissal
        try:
            await page.click(selector, timeout=2500)
            await asyncio.sleep(0.6)
            return {
                "status": "success",
                "selector": selector,
                "strategy": "retry_after_overlay_dismiss",
                "healed": True,
                "dismissed_overlays": dismissed,
                "current_url": page.url,
            }
        except Exception:
            pass

        # Attempt 3: JavaScript direct element click
        try:
            js_res = await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (el) {
                    el.scrollIntoView({ behavior: 'instant', block: 'center' });
                    el.click();
                    return { found: true, clicked: true, tag: el.tagName };
                }
                return { found: false, clicked: false };
            }""", selector)

            if js_res.get("clicked"):
                await asyncio.sleep(0.8)
                return {
                    "status": "success",
                    "selector": selector,
                    "strategy": "js_direct_click_fallback",
                    "healed": True,
                    "dismissed_overlays": dismissed,
                    "details": js_res,
                    "current_url": page.url,
                }
        except Exception as js_err:
            pass

        # Attempt 4: Dispatch click event fallback
        try:
            event_res = await page.evaluate("""(sel) => {
                const el = document.querySelector(sel);
                if (el) {
                    const evt = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    el.dispatchEvent(evt);
                    return true;
                }
                return false;
            }""", selector)
            if event_res:
                await asyncio.sleep(0.8)
                return {
                    "status": "success",
                    "selector": selector,
                    "strategy": "js_event_dispatch_fallback",
                    "healed": True,
                    "dismissed_overlays": dismissed,
                    "current_url": page.url,
                }
        except Exception:
            pass

        return {
            "status": "failed",
            "selector": selector,
            "healed": False,
            "error": "Element could not be clicked after overlay dismissal and JS fallbacks.",
        }

    # --------------------------------------------------------------------------
    # Micro-Healing Loop 2: Dynamic Growth & Pagination Strategy Switching
    # --------------------------------------------------------------------------

    @classmethod
    async def execute_healing_load(
        cls, page, item_selector: str, initial_strategy: str = "auto", max_rounds: int = 5
    ) -> dict:
        """
        Drives autonomous dynamic content loading.
        Monitors item count before and after each action:
          If item count stops growing, automatically switches pagination strategies:
          'infinite_scroll' -> 'nested_scroll' -> 'load_more_button' -> 'pagination_next'.
        """
        if not page:
            return {"status": "error", "message": "No active page."}

        rounds_completed = 0
        strategy_history = []
        current_strategy = initial_strategy if initial_strategy != "auto" else "infinite_scroll"

        async def get_count():
            try:
                return await page.evaluate(f"document.querySelectorAll('{item_selector}').length")
            except Exception:
                return 0

        initial_count = await get_count()
        last_count = initial_count
        healed_switches = 0

        while rounds_completed < max_rounds:
            strategy_history.append(current_strategy)

            if current_strategy == "infinite_scroll":
                # Scroll window down and wait for lazy network requests
                await page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
                await asyncio.sleep(1.2)
                await page.evaluate("window.scrollBy(0, 800)")
                await asyncio.sleep(1.0)

            elif current_strategy == "nested_scroll":
                # Detect inner scrollable container
                scrolled_inner = await page.evaluate("""() => {
                    const containers = Array.from(document.querySelectorAll('div, main, section'))
                        .filter(el => {
                            const style = window.getComputedStyle(el);
                            return (style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
                        });
                    if (containers.length > 0) {
                        containers[0].scrollBy(0, 1200);
                        return true;
                    }
                    return false;
                }""")
                await asyncio.sleep(1.2)

            elif current_strategy == "load_more_button":
                # Locate and click 'Load More' button
                clicked_more = False
                for sel in cls.LOAD_MORE_SELECTORS:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=300):
                            await btn.click(timeout=1000)
                            clicked_more = True
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass
                if not clicked_more:
                    # Try JS search for button with 'more' text
                    clicked_more = await page.evaluate("""() => {
                        const buttons = Array.from(document.querySelectorAll('button, a'));
                        const target = buttons.find(b => /load more|show more|view more/i.test(b.textContent || ''));
                        if (target) {
                            target.click();
                            return true;
                        }
                        return false;
                    }""")
                    if clicked_more:
                        await asyncio.sleep(1.5)

            elif current_strategy == "pagination_next":
                # Locate and click 'Next' page button/link
                clicked_next = False
                for sel in cls.PAGINATION_NEXT_SELECTORS:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=300):
                            await btn.click(timeout=1200)
                            clicked_next = True
                            await asyncio.sleep(2.0)
                            break
                    except Exception:
                        pass
                if not clicked_next:
                    # Switch to URL parameter manipulation if present or stop
                    break

            new_count = await get_count()
            rounds_completed += 1

            # Check growth
            if new_count > last_count:
                last_count = new_count
            else:
                # Items not growing -> Micro-Healing: Switch strategy
                healed_switches += 1
                if current_strategy == "infinite_scroll":
                    current_strategy = "nested_scroll"
                elif current_strategy == "nested_scroll":
                    current_strategy = "load_more_button"
                elif current_strategy == "load_more_button":
                    current_strategy = "pagination_next"
                else:
                    # Exhausted all strategies
                    break

        final_count = await get_count()
        return {
            "status": "success",
            "item_selector": item_selector,
            "initial_count": initial_count,
            "final_count": final_count,
            "growth": final_count - initial_count,
            "rounds_completed": rounds_completed,
            "strategies_attempted": list(dict.fromkeys(strategy_history)),
            "effective_strategy": strategy_history[-1] if strategy_history else initial_strategy,
            "healed": healed_switches > 0,
        }

    # --------------------------------------------------------------------------
    # Micro-Healing Loop 3: Empty Fields & DOM Selector Re-evaluation
    # --------------------------------------------------------------------------

    @classmethod
    async def evaluate_field_selectors(
        cls, page, item_selector: str, empty_fields: list[str]
    ) -> dict:
        """
        Re-evaluates DOM selectors when extracted sample fields are empty or null.
        Inspects child DOM nodes, microdata, data-* attributes, and JSON-LD schema
        to discover working selectors.
        """
        if not page or not empty_fields:
            return {}

        results = {}

        # 1. Check for JSON-LD Product Schema
        json_ld_schema = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
                try {
                    const parsed = JSON.parse(s.textContent);
                    const items = Array.isArray(parsed) ? parsed : [parsed];
                    for (const it of items) {
                        if (it['@type'] === 'Product' || it['@type'] === 'ItemList' || (it['@graph'] && it['@graph'].some(g => g['@type'] === 'Product'))) {
                            return it;
                        }
                    }
                } catch(e) {}
            }
            return null;
        }""")

        for field in empty_fields:
            f_lower = field.lower()

            # Price re-evaluation
            if "price" in f_lower:
                price_findings = await page.evaluate("""(itemSel) => {
                    const card = document.querySelector(itemSel) || document.body;
                    const candidates = [
                        '[itemprop="price"]',
                        '.price',
                        '.product-price',
                        '.current-price',
                        '.actual-price',
                        '.sale-price',
                        '[data-price]',
                        '[data-product-price]',
                        '.amount',
                        '.money',
                        '[class*="price" i]'
                    ];
                    for (const sel of candidates) {
                        const el = card.querySelector(sel);
                        if (el) {
                            const txt = (el.getAttribute('content') || el.getAttribute('data-price') || el.textContent || '').trim();
                            if (/(?:[\\$£€₹]|USD|CAD|EUR|GBP)?\\s*\\d+(?:[.,]\\d{2})?/.test(txt)) {
                                return { selector: sel, sample: txt.slice(0, 40), source: 'card_dom' };
                            }
                        }
                    }
                    return null;
                }""", item_selector)

                if price_findings:
                    results[field] = price_findings
                elif json_ld_schema:
                    results[field] = {
                        "selector": "script[type='application/ld+json'] -> offers.price",
                        "sample": str(json_ld_schema.get("offers", {})),
                        "source": "json_ld",
                    }

            # Product ID / SKU re-evaluation
            elif any(k in f_lower for k in ("id", "sku", "code", "mpn")):
                id_findings = await page.evaluate("""(itemSel) => {
                    const card = document.querySelector(itemSel) || document.body;
                    const candidates = [
                        '[itemprop="sku"]',
                        '[itemprop="productID"]',
                        '[data-product-id]',
                        '[data-item-id]',
                        '[data-sku]',
                        '[data-id]',
                        '.sku',
                        '.product-sku',
                        '.item-number'
                    ];
                    for (const sel of candidates) {
                        const el = card.querySelector(sel);
                        if (el) {
                            const val = (el.getAttribute('data-product-id') || el.getAttribute('data-sku') || el.getAttribute('content') || el.textContent || '').trim();
                            if (val.length > 0 && val.length < 50) {
                                return { selector: sel, sample: val, source: 'card_dom' };
                            }
                        }
                    }
                    // Try link href pattern
                    const link = card.querySelector('a[href]');
                    if (link) {
                        const href = link.getAttribute('href');
                        const match = href.match(/\\/([a-zA-Z0-9_-]{5,})\\/?(?:\\?|$)/);
                        if (match) {
                            return { selector: 'a[href]', sample: match[1], source: 'url_slug_regex', regex: match[0] };
                        }
                    }
                    return null;
                }""", item_selector)

                if id_findings:
                    results[field] = id_findings
                elif json_ld_schema and ("sku" in json_ld_schema or "productID" in json_ld_schema):
                    results[field] = {
                        "selector": "script[type='application/ld+json'] -> sku/productID",
                        "sample": str(json_ld_schema.get("sku") or json_ld_schema.get("productID")),
                        "source": "json_ld",
                    }

            # Title / Name re-evaluation
            elif any(k in f_lower for k in ("title", "name")):
                title_findings = await page.evaluate("""(itemSel) => {
                    const card = document.querySelector(itemSel) || document.body;
                    const candidates = [
                        '[itemprop="name"]',
                        'h1, h2, h3, h4',
                        '.product-title',
                        '.title',
                        '.product-name',
                        '[class*="title" i]',
                        'a[class*="title" i]'
                    ];
                    for (const sel of candidates) {
                        const el = card.querySelector(sel);
                        if (el) {
                            const val = el.textContent.trim();
                            if (val.length > 3 && val.length < 200) {
                                return { selector: sel, sample: val.slice(0, 80), source: 'card_dom' };
                            }
                        }
                    }
                    return null;
                }""", item_selector)

                if title_findings:
                    results[field] = title_findings

        return results
