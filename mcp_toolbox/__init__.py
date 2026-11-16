"""
mcp_toolbox
===========
Enhanced Autonomous AI Scraping Toolbox for Scraper Studio.
Provides custom high-leverage Playwright tools, in-tool micro-healing loops,
and an extensible registry with seamless fallback to legacy Playwright MCP.
"""

from .custom_playwright import CustomPlaywrightToolbox
from .legacy_adapter import LegacyPlaywrightAdapter
from .registry import MCPToolboxManager, MCPToolbox
from .healing_engine import MicroHealingEngine

__all__ = [
    "CustomPlaywrightToolbox",
    "LegacyPlaywrightAdapter",
    "MCPToolboxManager",
    "MCPToolbox",
    "MicroHealingEngine",
]
