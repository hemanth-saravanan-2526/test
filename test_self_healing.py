"""
Unit and Integration Test Suite for Self-Healing Repair Loop with Playwright MCP.
Validates:
  1. Retention of all input context (target URL, sample keyword, user rules, prior findings, broken code)
  2. Retention of full test run details (exit code, stderr traceback, stdout funnel logs, extracted row count)
  3. Regression protection schema and sheet preservation in diagnostic prompts
  4. Integration of live MCP diagnostic recon findings into codegen synthesis
  5. RepairResult backward compatibility (3-tuple unpack + attribute access)
  6. End-to-end flow with mocked OpenAI client and MCP tool interceptor
  7. Graceful fallback resilience when browser recon encounters exceptions
"""

import ast
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from inspection_agent_service import (
    InspectionJob,
    RepairResult,
    build_diagnostic_recon_prompt,
    build_diagnostic_codegen_prompt,
    run_codegen_repair,
    DIAGNOSTIC_RECON_SYSTEM_PROMPT,
    SCRIPT_CONTRACT,
)


class TestSelfHealingLoop(unittest.TestCase):

    def setUp(self):
        self.sample_broken_code = '''
import pandas as pd

TARGET_URL = "https://example-store.com/electronics"
FIELDS = ["product_id", "product_url", "source_listing_url", "title", "price", "specs"]

def extract_products():
    return []

if __name__ == "__main__":
    with pd.ExcelWriter("output.xlsx", engine="openpyxl") as writer:
        pd.DataFrame([]).to_excel(writer, sheet_name="Extracted Data", index=False)
        pd.DataFrame([]).to_excel(writer, sheet_name="Flags", index=False)
        pd.DataFrame([]).to_excel(writer, sheet_name="Recommendations", index=False)
'''
        self.job = InspectionJob(
            job_id="test-job-456",
            target_url="https://example-store.com/electronics",
            sample_keyword="OLED Monitor",
            search_rules="Search for keyword in top search bar, press Enter.",
            filter_rules="Filter by In Stock only.",
            navigation_rules="Click each product card to open details.",
            extraction_rules="Extract title, price ($xx.xx), specs table, SKU.",
            recon_model="gpt-5.6-luna",
            codegen_model="gpt-5.6-luna",
        )
        self.prior_findings = {
            "page_type": "listing",
            "listing_selectors": ".old-product-card",
        }
        self.run_details = {
            "exit_code": 1,
            "num_extracted_rows": 0,
            "sample_output_exists": True,
            "sample_input_path": "c:/path/to/input.xlsx",
            "sample_output_path": "c:/path/to/output.xlsx",
            "max_products": 10,
            "stderr": "Traceback (most recent call last):\n  File 'generated_scraper.py', line 45, in <module>\n    raise TimeoutError('Selector .old-product-card timed out after 30s')",
            "stdout_tail": "Crawl Funnel: 1 categories, 0 links discovered, 0 processed, 0 extracted.\nERROR: Zero product records were extracted. Aborting.",
            "error_summary": "Scraper failed with exit code 1: TimeoutError on selector .old-product-card",
        }

    def test_context_retention_in_recon_prompt(self):
        """Test that build_diagnostic_recon_prompt retains all input context and run details."""
        prompt = build_diagnostic_recon_prompt(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context=self.run_details["error_summary"],
            recon_findings=self.prior_findings,
            run_details=self.run_details,
        )

        # 1. Job input context
        self.assertIn("https://example-store.com/electronics", prompt)
        self.assertIn("OLED Monitor", prompt)
        self.assertIn("Search for keyword in top search bar", prompt)
        self.assertIn("Filter by In Stock only", prompt)
        self.assertIn("Click each product card to open details", prompt)
        self.assertIn("Extract title, price ($xx.xx), specs table, SKU", prompt)

        # 2. Existing schema and multi-sheet preservation
        self.assertIn("EXISTING COLUMNS REQUIRED", prompt)
        self.assertIn("product_id", prompt)
        self.assertIn("specs", prompt)
        self.assertIn("Recommendations", prompt)

        # 3. Test run details & traceback
        self.assertIn("Exit code: 1", prompt)
        self.assertIn("Meaningful extracted rows: 0", prompt)
        self.assertIn("Selector .old-product-card timed out after 30s", prompt)
        self.assertIn("Crawl Funnel: 1 categories, 0 links discovered", prompt)

        # 4. Prior findings & broken code
        self.assertIn(".old-product-card", prompt)
        self.assertIn("TARGET_URL = \"https://example-store.com/electronics\"", prompt)

    def test_diagnostic_codegen_prompt_incorporates_live_recon(self):
        """Test that build_diagnostic_codegen_prompt includes live MCP findings and regression invariants."""
        live_mcp_findings = {
            "root_cause": "Website underwent frontend redesign; listing cards now use .product-card-v2 and prices require clicking #pricing-tab",
            "working_listing_selectors": ".product-card-v2",
            "working_detail_selectors": "h1.prod-title, .buy-box .final-price",
            "working_price_selector": ".buy-box .final-price",
            "cookie_banner": "Cookie banner #onetrust-accept-btn-handler dismissed via browser_click",
            "sample_extracted_fields": {
                "title": "UltraSharp OLED 32",
                "price": "$899.99",
                "product_id": "SKU-OLED-32"
            }
        }

        prompt = build_diagnostic_codegen_prompt(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context=self.run_details["error_summary"],
            recon_findings=self.prior_findings,
            diagnostic_findings=live_mcp_findings,
            run_details=self.run_details,
        )

        # 1. Regression invariants
        self.assertIn("DO NOT DROP PREVIOUSLY WORKING FIELDS", prompt)
        self.assertIn("MULTI-SHEET PRESERVATION", prompt)
        self.assertIn("Recommendations", prompt)
        self.assertIn("STRICT PRICE SCOPING", prompt)
        self.assertIn("EXCEL 32K LIMIT", prompt)
        self.assertIn("CRAWL FUNNEL & FAIL LOUDLY", prompt)

        # 2. Live MCP verified findings
        self.assertIn(".product-card-v2", prompt)
        self.assertIn("#onetrust-accept-btn-handler", prompt)
        self.assertIn("$899.99", prompt)
        self.assertIn("SKU-OLED-32", prompt)

        # 3. Telemetry details
        self.assertIn("Exit Code: 1", prompt)
        self.assertIn("TimeoutError", prompt)

    def test_repair_result_backward_compatibility(self):
        """Test RepairResult can be unpacked as 3-tuple and also accessed via attributes."""
        diag = {"root_cause": "Fixed selector", "working_price": "$12.00"}
        logs = [{"tool": "browser_navigate", "url": "https://example.com"}]
        res = RepairResult(
            repaired_code="# repaired",
            requirements_txt="playwright>=1.40",
            confidence_notes=["Selector updated"],
            diagnostic_findings=diag,
            action_log=logs,
        )

        # 1. 3-element unpack (legacy backward compatibility)
        code, reqs, notes = res
        self.assertEqual(code, "# repaired")
        self.assertEqual(reqs, "playwright>=1.40")
        self.assertEqual(notes, ["Selector updated"])

        # 2. Attribute access
        self.assertEqual(res.repaired_code, "# repaired")
        self.assertEqual(res.requirements_txt, "playwright>=1.40")
        self.assertEqual(res.confidence_notes, ["Selector updated"])
        self.assertEqual(res.diagnostic_findings["root_cause"], "Fixed selector")
        self.assertEqual(len(res.action_log), 1)

    @patch("inspection_agent_service._run_openai_recon_session")
    @patch("inspection_agent_service._run_openai_codegen_session")
    @patch("inspection_agent_service.resolve_credentials")
    async def _async_test_run_codegen_repair_flow(self, mock_creds, mock_codegen_session, mock_recon_session):
        mock_creds.return_value = MagicMock(api_key="sk-test", base_url=None, org_id=None, project_id=None)

        # Phase 1: Mock live MCP recon output
        recon_payload = {
            "root_cause": "Dynamic page requires waiting for .product-grid",
            "working_listing_selectors": ".product-grid .item-link",
            "working_price_selector": ".pricing-box .amount",
            "sample_extracted_fields": {"title": "Monitor 4K", "price": "$399.00"},
            "confidence_notes": ["Verified with Playwright MCP"],
        }
        mock_recon_session.return_value = MagicMock(
            status="completed",
            final_text=json.dumps(recon_payload),
            tool_calls=3,
            usage_info={
                "input_tokens": 500,
                "output_tokens": 200,
                "cached_tokens": 0,
                "model": "gpt-5.6-luna",
                "duration_ms": 100,
                "num_turns": 1,
            },
            anti_bot_marker=None,
        )

        # Phase 2: Mock codegen output
        codegen_payload = {
            "generated_code": "import playwright\n# Full repaired scraper code",
            "requirements_txt": "playwright>=1.40.0\npandas>=2.0.0",
            "confidence_notes": ["Repaired successfully using live selectors"],
        }
        mock_codegen_session.return_value = MagicMock(
            status="completed",
            final_text=json.dumps(codegen_payload),
            usage_info={
                "input_tokens": 1500,
                "output_tokens": 600,
                "cached_tokens": 0,
                "model": "gpt-5.6-luna",
                "duration_ms": 150,
                "num_turns": 1,
            },
        )

        # Collect streamed logs
        streamed_entries = []
        async def on_log(entry):
            streamed_entries.append(entry)

        res = await run_codegen_repair(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context=self.run_details["error_summary"],
            recon_findings=self.prior_findings,
            run_details=self.run_details,
            mcp_base_url="http://localhost:8931",
            on_log_entry=on_log,
        )

        # Verify Phase 1 (Recon with MCP) was invoked with DIAGNOSTIC_RECON_SYSTEM_PROMPT
        mock_recon_session.assert_called_once()
        recon_call_kwargs = mock_recon_session.call_args.kwargs
        self.assertEqual(recon_call_kwargs["system_prompt"], DIAGNOSTIC_RECON_SYSTEM_PROMPT)
        self.assertEqual(recon_call_kwargs["mcp_base_url"], "http://localhost:8931")
        self.assertIn("https://example-store.com/electronics", recon_call_kwargs["task_prompt"])
        self.assertIn("TimeoutError", recon_call_kwargs["task_prompt"])

        # Verify Phase 2 (Codegen) received diagnostic findings
        mock_codegen_session.assert_called_once()
        codegen_call_kwargs = mock_codegen_session.call_args.kwargs
        self.assertIn(".product-grid .item-link", codegen_call_kwargs["task_prompt"])
        self.assertIn("$399.00", codegen_call_kwargs["task_prompt"])

        # Verify result contains diagnostic findings
        self.assertEqual(res.diagnostic_findings["root_cause"], "Dynamic page requires waiting for .product-grid")
        self.assertIn("import playwright", res.repaired_code)
        self.assertIn("playwright>=1.40.0", res.requirements_txt)

    def test_run_codegen_repair_flow(self):
        """Execute the async test for run_codegen_repair flow."""
        import asyncio
        asyncio.run(self._async_test_run_codegen_repair_flow())

    @patch("inspection_agent_service._run_openai_recon_session")
    @patch("inspection_agent_service._run_openai_codegen_session")
    @patch("inspection_agent_service.resolve_credentials")
    async def _async_test_recon_exception_fallback(self, mock_creds, mock_codegen_session, mock_recon_session):
        mock_creds.return_value = MagicMock(api_key="sk-test", base_url=None, org_id=None, project_id=None)

        # Phase 1 raises an error (e.g. MCP connection refused)
        mock_recon_session.side_effect = ConnectionRefusedError("MCP Server at port 8931 is offline")

        # Phase 2 still succeeds
        mock_codegen_session.return_value = MagicMock(
            status="completed",
            final_text=json.dumps({
                "generated_code": "# fallback repaired code",
                "requirements_txt": "requests",
                "confidence_notes": ["Repaired from traceback"],
            }),
            usage_info={
                "input_tokens": 800,
                "output_tokens": 300,
                "cached_tokens": 0,
                "model": "gpt-5.6-luna",
                "duration_ms": 120,
                "num_turns": 1,
            },
        )

        res = await run_codegen_repair(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context="Test traceback error",
            run_details=self.run_details,
        )

        # Should not throw - should fall back to error context
        self.assertTrue(res.diagnostic_findings.get("fallback"))
        self.assertIn("MCP Server at port 8931 is offline", res.diagnostic_findings["root_cause"])
        self.assertEqual(res.repaired_code, "# fallback repaired code")

    def test_recon_exception_fallback(self):
        """Execute the fallback resilience test."""
        import asyncio
        asyncio.run(self._async_test_recon_exception_fallback())

    def test_context_retention_across_retries_in_prompts(self):
        """Verify that attempt_history across retries is injected into recon and codegen prompts."""
        attempt_history = [
            {
                "attempt": 1,
                "error_summary": "Exit 1: TimeoutError on .product-card",
                "diagnostic_summary": "Overlay modal intercepted click",
                "fixes_attempted": "JS evaluate click",
            },
            {
                "attempt": 2,
                "error_summary": "Exit 0: Extracted 0 rows",
                "diagnostic_summary": "Prices inside iframe buy-box",
                "fixes_attempted": "Scoped selector to frame",
            },
        ]

        recon_prompt = build_diagnostic_recon_prompt(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context="Exit 0: Extracted 0 rows",
            recon_findings=self.prior_findings,
            run_details=self.run_details,
            attempt_history=attempt_history,
        )

        codegen_prompt = build_diagnostic_codegen_prompt(
            job=self.job,
            broken_code=self.sample_broken_code,
            error_context="Exit 0: Extracted 0 rows",
            recon_findings=self.prior_findings,
            diagnostic_findings={"root_cause": "Live check passed"},
            run_details=self.run_details,
            attempt_history=attempt_history,
        )

        # Recon prompt verification
        self.assertIn("PRIOR REPAIR ATTEMPTS & CUMULATIVE OUTCOMES", recon_prompt)
        self.assertIn("Attempt 1: Error: Exit 1: TimeoutError on .product-card", recon_prompt)
        self.assertIn("Prior Diagnosis: Overlay modal intercepted click", recon_prompt)
        self.assertIn("Attempt 2: Error: Exit 0: Extracted 0 rows", recon_prompt)
        self.assertIn("Do not repeat failed inspection assumptions", recon_prompt)

        # Codegen prompt verification
        self.assertIn("PRIOR REPAIR ATTEMPTS & CUMULATIVE OUTCOMES", codegen_prompt)
        self.assertIn("Attempt 1: Error: Exit 1: TimeoutError on .product-card", codegen_prompt)
        self.assertIn("Prior Diagnosis: Overlay modal intercepted click", codegen_prompt)
        self.assertIn("Attempt 2: Error: Exit 0: Extracted 0 rows", codegen_prompt)
        self.assertIn("Do NOT repeat strategies or selectors that previously failed", codegen_prompt)

    def test_repair_result_prompts_attribute_and_unpacking(self):
        """Verify RepairResult preserves 3-element unpacking while exposing .prompts."""
        res = RepairResult(
            repaired_code="# new code",
            requirements_txt="playwright",
            confidence_notes=["Notes"],
            diagnostic_findings={"root_cause": "Resolved"},
            action_log=[{"step": 1}],
            prompts={
                "recon_task_prompt": "RECON PROMPT CONTENT",
                "codegen_task_prompt": "CODEGEN PROMPT CONTENT",
            },
        )
        # Unpack as 3-tuple
        code, reqs, notes = res
        self.assertEqual(code, "# new code")
        self.assertEqual(reqs, "playwright")
        self.assertEqual(notes, ["Notes"])

        # Attribute access
        self.assertEqual(res.prompts["recon_task_prompt"], "RECON PROMPT CONTENT")
        self.assertEqual(res.prompts["codegen_task_prompt"], "CODEGEN PROMPT CONTENT")

    def test_audit_vault_manager_recording_and_persistence(self):
        """Verify AuditVaultManager permanently writes prompts, scripts, diff, decisions, errors, fixes, and outcomes."""
        import tempfile
        from pathlib import Path
        from api_service import AuditVaultManager

        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir)
            prompts = {
                "recon_system_prompt": "Recon system prompt content",
                "recon_task_prompt": "Recon task prompt content",
                "codegen_system_prompt": "Codegen system prompt content",
                "codegen_task_prompt": "Codegen task prompt content",
            }
            prev_code = "print('old code version 1')\n"
            new_code = "print('new repaired code version 2')\n"

            # 1. Record Attempt 2
            audit_entry = AuditVaultManager.record_attempt(
                folder=folder,
                attempt_number=2,
                phase="self_healing_retry",
                prompts=prompts,
                code=new_code,
                reqs="playwright>=1.40",
                previous_code=prev_code,
                agent_decisions={"root_cause": "Replaced bad selector with .product-grid"},
                errors={"exit_code": 1, "error_summary": "TimeoutError"},
                fixes={"strategy": "live_mcp_codegen_repair"},
            )

            attempt_dir = folder / "audit" / "attempt_2"
            self.assertTrue(attempt_dir.exists())

            # Verify prompts on disk
            prompts_dir = attempt_dir / "prompts"
            self.assertTrue((prompts_dir / "recon_system_prompt.txt").exists())
            self.assertEqual((prompts_dir / "recon_task_prompt.txt").read_text(encoding="utf-8"), "Recon task prompt content")

            # Verify scripts on disk
            scripts_dir = attempt_dir / "scripts"
            self.assertTrue((scripts_dir / "generated_scraper.py").exists())
            self.assertEqual((scripts_dir / "requirements.txt").read_text(encoding="utf-8"), "playwright>=1.40")

            # Verify diff
            diff_file = attempt_dir / "diff_from_previous.patch"
            self.assertTrue(diff_file.exists())
            diff_text = diff_file.read_text(encoding="utf-8")
            self.assertIn("-print('old code version 1')", diff_text)
            self.assertIn("+print('new repaired code version 2')", diff_text)

            # Verify JSON artifacts
            self.assertTrue((attempt_dir / "agent_decisions.json").exists())
            self.assertTrue((attempt_dir / "errors.json").exists())
            self.assertTrue((attempt_dir / "fixes.json").exists())

            # Verify audit trail file
            trail_file = folder / "audit" / "audit_trail.jsonl"
            self.assertTrue(trail_file.exists())
            lines = trail_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

            # 2. Update outcome
            AuditVaultManager.update_attempt_outcome(
                folder=folder,
                attempt_number=2,
                outcome={"status": "passed", "returncode": 0, "extracted_rows": 12},
            )
            self.assertTrue((attempt_dir / "outcome.json").exists())
            outcome_data = json.loads((attempt_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome_data["status"], "passed")
            self.assertEqual(outcome_data["extracted_rows"], 12)

            # Audit trail appended with outcome update
            lines2 = trail_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines2), 2)


if __name__ == "__main__":
    unittest.main()
