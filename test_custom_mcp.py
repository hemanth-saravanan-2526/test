"""
test_custom_mcp.py
==================
Unit and Integration Test Suite for Scraper Studio's Enhanced Autonomous AI Flow:
  1. Custom Playwright MCP Toolbox tool calling and schemas
  2. In-Tool Micro-Healing Loop 1: Element / Click Blocked -> Auto-Dismiss Overlay & Fallback
  3. In-Tool Micro-Healing Loop 2: Items Not Growing -> Strategy Switching
  4. In-Tool Micro-Healing Loop 3: Empty Fields -> DOM Selector Re-evaluation
  5. Extensible MCP Registry (Primary Custom Playwright + Fallback Legacy Adapter)
  6. Dynamic third-party MCP tool onboarding
  7. Backward-compatible tool aliases
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp_toolbox import (
    CustomPlaywrightToolbox,
    LegacyPlaywrightAdapter,
    MCPToolboxManager,
    MicroHealingEngine,
)


class TestCustomMCPToolbox(unittest.TestCase):

    def setUp(self):
        self.toolbox = CustomPlaywrightToolbox()

    def test_tool_definitions_schema(self):
        """Verify the 8 core tools are defined in the Custom Playwright MCP Toolbox."""
        tool_defs = self.toolbox.get_tool_definitions()
        self.assertIsInstance(tool_defs, list)
        tool_names = [t["function"]["name"] for t in tool_defs]
        expected_tools = [
            "navigate",
            "snapshot",
            "click",
            "type_text",
            "scroll",
            "load_all",
            "run_js",
            "save_items",
        ]
        for exp in expected_tools:
            self.assertIn(exp, tool_names, f"Tool '{exp}' must be present in definitions")

    def test_micro_healing_loop_1_click_fallback(self):
        """Verify Micro-Healing Loop 1: click fallback to JS when native click fails."""
        mock_page = MagicMock()
        mock_page.url = "https://example.com/products"

        # Native click raises an exception (e.g. element blocked by overlay)
        mock_page.click = AsyncMock(side_effect=Exception("Element is obscured by backdrop"))
        mock_page.keyboard.press = AsyncMock()

        # JS fallback succeeds
        mock_page.evaluate = AsyncMock(return_value={"found": True, "clicked": True, "tag": "BUTTON"})

        async def run_test():
            res = await MicroHealingEngine.execute_healing_click(mock_page, "button.add-to-cart")
            self.assertEqual(res["status"], "success")
            self.assertTrue(res["healed"])
            self.assertEqual(res["strategy"], "js_direct_click_fallback")

        asyncio.run(run_test())

    def test_micro_healing_loop_2_pagination_strategy_switch(self):
        """Verify Micro-Healing Loop 2: switches strategy when item count stalls."""
        mock_page = MagicMock()

        # Simulate count staying constant (e.g., 10 items) to trigger strategy switch
        mock_page.evaluate = AsyncMock(return_value=10)

        async def run_test():
            res = await MicroHealingEngine.execute_healing_load(
                mock_page,
                item_selector=".product-card",
                initial_strategy="infinite_scroll",
                max_rounds=3,
            )
            self.assertEqual(res["status"], "success")
            self.assertTrue(res["healed"])
            # Verified strategies attempted include switching
            self.assertIn("infinite_scroll", res["strategies_attempted"])
            self.assertIn("nested_scroll", res["strategies_attempted"])

        asyncio.run(run_test())

    def test_micro_healing_loop_3_empty_fields_selector_reevaluation(self):
        """Verify Micro-Healing Loop 3: discovers alternative selectors when fields are empty."""
        mock_page = MagicMock()

        async def mock_eval(script, *args):
            if "candidates" in script and "price" in script.lower():
                return {"selector": ".product-price-current", "sample": "$49.99", "source": "card_dom"}
            if "sku" in script.lower() or "productid" in script.lower():
                return {"selector": "[data-sku]", "sample": "SKU-9921", "source": "card_dom"}
            return None

        mock_page.evaluate = AsyncMock(side_effect=mock_eval)

        async def run_test():
            res = await MicroHealingEngine.evaluate_field_selectors(
                mock_page,
                item_selector=".product-card",
                empty_fields=["price", "product_id"],
            )
            self.assertIn("price", res)
            self.assertEqual(res["price"]["selector"], ".product-price-current")
            self.assertIn("product_id", res)
            self.assertEqual(res["product_id"]["selector"], "[data-sku]")

        asyncio.run(run_test())

    def test_save_items_with_healing_telemetry(self):
        """Verify save_items stages sample records and detects empty fields."""
        self.toolbox.page = MagicMock()
        self.toolbox.page.evaluate = AsyncMock(return_value={
            "selector": ".item-price", "sample": "$19.99", "source": "card_dom"
        })

        async def run_test():
            sample_data = [
                {"product_id": "P1", "title": "Wireless Mouse", "price": ""},  # empty price
                {"product_id": "P2", "title": "Mechanical Keyboard", "price": ""},
            ]
            res_str = await self.toolbox.save_items(
                sample_records=sample_data,
                schema=["product_id", "title", "price"],
                item_selector=".product-card",
            )
            res = json.loads(res_str)
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["saved_count"], 2)
            self.assertIn("price", res["empty_fields_detected"])
            self.assertIn("price", res["re_evaluated_selectors"])

        asyncio.run(run_test())

    def test_backward_compatible_tool_aliases(self):
        """Verify legacy tool names (e.g. browser_navigate) route cleanly to custom methods."""
        self.toolbox.navigate = AsyncMock(return_value=json.dumps({"status": "success", "url": "https://test.com"}))
        self.toolbox.snapshot = AsyncMock(return_value=json.dumps({"title": "Test Page"}))
        self.toolbox._is_initialized = True

        async def run_test():
            res1 = await self.toolbox.call_tool("browser_navigate", {"url": "https://test.com"})
            self.assertIn("https://test.com", res1)

            res2 = await self.toolbox.call_tool("mcp__browser__browser_snapshot", {})
            self.assertIn("Test Page", res2)

        asyncio.run(run_test())


class TestMCPToolboxManager(unittest.TestCase):

    def test_manager_primary_and_fallback_routing(self):
        """Verify MCPToolboxManager initializes primary and manages fallback."""
        manager = MCPToolboxManager()

        # Mock primary
        mock_primary = MagicMock()
        mock_primary.initialize = AsyncMock()
        mock_primary.call_tool = AsyncMock(return_value="primary_result")
        manager.primary = mock_primary
        manager.active_toolbox = mock_primary

        async def run_test():
            res = await manager.call_tool("navigate", {"url": "https://example.com"})
            self.assertEqual(res, "primary_result")
            self.assertFalse(manager.failover_occurred)

        asyncio.run(run_test())

    def test_manager_runtime_failover_to_fallback(self):
        """Verify dynamic failover to fallback when primary encounters catastrophic error."""
        manager = MCPToolboxManager()

        # Mock failing primary
        mock_primary = MagicMock()
        mock_primary.call_tool = AsyncMock(side_effect=RuntimeError("Browser disconnected"))
        manager.primary = mock_primary
        manager.active_toolbox = mock_primary

        # Mock fallback
        mock_fallback = MagicMock()
        mock_fallback.initialize = AsyncMock()
        mock_fallback.call_tool = AsyncMock(return_value="fallback_result")
        manager.fallback = mock_fallback

        async def run_test():
            res = await manager.call_tool("navigate", {"url": "https://example.com"})
            self.assertEqual(res, "fallback_result")
            self.assertTrue(manager.failover_occurred)
            self.assertEqual(manager.active_toolbox, mock_fallback)

        asyncio.run(run_test())

    def test_dynamic_third_party_mcp_tool_onboarding(self):
        """Verify onboarding a third-party MCP tool (e.g. captcha_solver) into the registry."""
        manager = MCPToolboxManager()

        mock_captcha_tool = MagicMock()
        mock_captcha_tool.call_tool = AsyncMock(return_value=json.dumps({"token": "bypass_token_xyz"}))
        mock_captcha_tool.get_tool_definitions.return_value = [
            {
                "type": "function",
                "function": {
                    "name": "captcha_solver",
                    "description": "Solves challenge token on target page.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        manager.register_mcp_tool("captcha_solver", mock_captcha_tool)

        async def run_test():
            defs = manager.get_tool_definitions()
            names = [d["function"]["name"] for d in defs]
            self.assertIn("captcha_solver", names)

            # Test tool execution routed directly to the third-party tool
            res = await manager.call_tool("captcha_solver", {})
            self.assertIn("bypass_token_xyz", res)

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
