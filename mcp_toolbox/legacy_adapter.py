"""
mcp_toolbox/legacy_adapter.py
=============================
Fallback Adapter for Scraper Studio.
Adapts the legacy Playwright session and external Playwright MCP server
into the unified MCPToolbox interface, ensuring 100% backward compatibility
and seamless runtime fallback if the primary custom toolbox encounters issues.
"""

import asyncio
import json
from typing import Any


class LegacyPlaywrightAdapter:
    """
    Fallback toolbox adapter wrapping the legacy NativePlaywrightBrowserSession
    or external Playwright MCP server.
    """

    def __init__(self, external_mcp_url: str | None = None):
        self.external_mcp_url = external_mcp_url
        self.session = None
        self._is_initialized = False

    async def initialize(self):
        """Initializes legacy Playwright session."""
        if self._is_initialized and self.session:
            return

        try:
            # Import legacy session from inspection_agent_service
            from inspection_agent_service import NativePlaywrightBrowserSession
            self.session = NativePlaywrightBrowserSession()
            await self.session.initialize()
            self._is_initialized = True
        except Exception as exc:
            # If native Playwright fails, fallback to pure python session
            try:
                from inspection_agent_service import NativePythonBrowserSession
                self.session = NativePythonBrowserSession()
                await self.session.initialize()
                self._is_initialized = True
            except Exception as inner_exc:
                raise RuntimeError(f"Failed to initialize legacy adapter: {exc} | {inner_exc}") from exc

    async def close(self):
        """Closes the underlying legacy session."""
        if self.session and hasattr(self.session, "close"):
            await self.session.close()
        self._is_initialized = False
        self.session = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """Translates tool names and forwards to legacy session."""
        if not self._is_initialized or not self.session:
            await self.initialize()

        # Map new custom tool names to legacy tool names
        name_map = {
            "navigate": "browser_navigate",
            "snapshot": "browser_snapshot",
            "click": "browser_click",
            "type_text": "browser_type",
            "type": "browser_type",
            "scroll": "browser_scroll",
            "run_js": "browser_evaluate",
            "save_items": "browser_snapshot",  # Legacy fallback records snapshot
            "load_all": "browser_scroll",       # Legacy fallback scrolls
        }

        target_name = name_map.get(tool_name, tool_name)
        if not target_name.startswith("browser_") and not target_name.startswith("mcp__"):
            target_name = f"browser_{target_name}"

        # Argument adaptation
        adapted_args = dict(arguments)
        if tool_name == "load_all":
            adapted_args["amount"] = 1200
        elif tool_name == "run_js" and "script" in adapted_args:
            adapted_args["expression"] = adapted_args["script"]

        try:
            res = await self.session.call_tool(target_name, adapted_args)
            return res if isinstance(res, str) else json.dumps(res, default=str)
        except Exception as exc:
            return json.dumps({"status": "failed", "tool": target_name, "error": str(exc)})

    @classmethod
    def get_tool_definitions(cls) -> list[dict]:
        """Returns tool schemas for legacy Playwright tools."""
        try:
            from inspection_agent_service import RECON_ALLOWED_TOOLS_SCHEMAS
            return RECON_ALLOWED_TOOLS_SCHEMAS
        except Exception:
            return []
