"""
mcp_toolbox/registry.py
=======================
Extensible MCP Tool Registry & Orchestrator for Scraper Studio.
Provides:
  - Primary Custom Playwright MCP Toolbox routing
  - Seamless automatic fallback to Legacy Playwright MCP
  - Plugin registration interface for onboarding future third-party MCP tools
    (e.g., CAPTCHA solvers, residential proxy rotators, LLM vision extractors)
"""

import json
from typing import Any, Protocol, runtime_checkable

from .custom_playwright import CustomPlaywrightToolbox
from .legacy_adapter import LegacyPlaywrightAdapter


@runtime_checkable
class MCPToolbox(Protocol):
    """Protocol that all Scraper Studio MCP toolboxes must satisfy."""

    async def initialize(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        ...

    @classmethod
    def get_tool_definitions(cls) -> list[dict]:
        ...


class MCPToolboxManager:
    """
    Orchestrates the lifecycle, execution, and fallback of MCP toolboxes.
    Uses CustomPlaywrightToolbox as primary, with LegacyPlaywrightAdapter as fallback.
    """

    def __init__(
        self,
        primary_provider: str = "custom_playwright",
        fallback_provider: str = "legacy_playwright",
    ):
        self.primary_provider_name = primary_provider
        self.fallback_provider_name = fallback_provider

        self.primary: MCPToolbox | None = None
        self.fallback: MCPToolbox | None = None
        self.active_toolbox: MCPToolbox | None = None
        self.active_provider_name: str = primary_provider

        # Extensible registry for future MCP tools (e.g. 'captcha_solver', 'stealth_proxy')
        self._third_party_tools: dict[str, MCPToolbox] = {}
        self.failover_occurred: bool = False
        self.failover_reason: str | None = None

    async def initialize(self):
        """Initializes primary toolbox with automatic fallback upon failure."""
        if self.active_toolbox:
            return

        try:
            self.primary = CustomPlaywrightToolbox()
            await self.primary.initialize()
            self.active_toolbox = self.primary
            self.active_provider_name = self.primary_provider_name
        except Exception as exc:
            # Primary failed at startup; fail over to fallback
            self.failover_occurred = True
            self.failover_reason = f"Primary initialization failed: {exc}"
            print(f"[MCPToolboxManager] WARNING: {self.failover_reason}. Activating fallback ({self.fallback_provider_name}).")
            self.fallback = LegacyPlaywrightAdapter()
            await self.fallback.initialize()
            self.active_toolbox = self.fallback
            self.active_provider_name = self.fallback_provider_name

    async def close(self):
        """Closes all active and initialized toolboxes."""
        if self.primary:
            try:
                await self.primary.close()
            except Exception:
                pass
        if self.fallback:
            try:
                await self.fallback.close()
            except Exception:
                pass
        for name, tb in self._third_party_tools.items():
            try:
                await tb.close()
            except Exception:
                pass

        self.active_toolbox = None
        self.primary = None
        self.fallback = None

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        """
        Dispatches tool call to the active toolbox.
        If a third-party tool is registered under that name, it routes there.
        If primary fails during execution, attempts dynamic failover to fallback.
        """
        if not self.active_toolbox:
            await self.initialize()

        # Check third-party registered tools first
        if tool_name in self._third_party_tools:
            return await self._third_party_tools[tool_name].call_tool(tool_name, arguments)

        try:
            return await self.active_toolbox.call_tool(tool_name, arguments)
        except Exception as exc:
            # Runtime failover from primary to fallback
            if self.active_toolbox == self.primary and not self.failover_occurred:
                self.failover_occurred = True
                self.failover_reason = f"Primary tool execution failed on '{tool_name}': {exc}"
                print(f"[MCPToolboxManager] WARNING: {self.failover_reason}. Failing over to {self.fallback_provider_name}.")
                if not self.fallback:
                    self.fallback = LegacyPlaywrightAdapter()
                    await self.fallback.initialize()
                self.active_toolbox = self.fallback
                self.active_provider_name = self.fallback_provider_name
                return await self.fallback.call_tool(tool_name, arguments)
            raise exc

    def register_mcp_tool(self, tool_name: str, toolbox: MCPToolbox):
        """
        Onboards a new or specialized MCP tool into the registry.
        Allows future expansions (e.g. CAPTCHA solvers, proxy rotators) to be plugged in.
        """
        self._third_party_tools[tool_name] = toolbox

    def get_tool_definitions(self) -> list[dict]:
        """Returns consolidated tool definitions for LLM tool calling."""
        definitions = CustomPlaywrightToolbox.get_tool_definitions()

        # Append any registered third-party tool definitions
        for tb in self._third_party_tools.values():
            if hasattr(tb, "get_tool_definitions"):
                definitions.extend(tb.get_tool_definitions())

        return definitions

    def get_telemetry(self) -> dict:
        """Returns runtime telemetry for audit logging and diagnostics."""
        telemetry = {
            "active_provider": self.active_provider_name,
            "failover_occurred": self.failover_occurred,
            "failover_reason": self.failover_reason,
            "registered_third_party_tools": list(self._third_party_tools.keys()),
        }
        if isinstance(self.primary, CustomPlaywrightToolbox):
            telemetry["in_tool_healing_events"] = getattr(self.primary, "healing_telemetry", [])
            telemetry["saved_items_count"] = len(getattr(self.primary, "saved_items", []))
        return telemetry
