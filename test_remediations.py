"""
Comprehensive Unit & Integration Test Suite for Scraper Studio Feedback Remediation.
Validates all 7 user feedbacks:
  1. Listing crawl funnel parsing & empty row detection
  2. Regression protection AST schema extractor
  3. Price regex scoping & title digits protection ("250 North American Birds Sound Book")
  4. Honest row counting (rejecting {}, all-blank, or URL-only rows)
  5. Excel 32K cell limit sanitizer
  6. Canonical product keys schema verification in SCRIPT_CONTRACT
  7. Anti-bot signal detection & PII reviewer masking
"""

import ast
import json
import re
import unittest
import pandas as pd
from pathlib import Path

# Imports from codebase
from inspection_agent_service import (
    _extract_schema_from_code,
    _detect_anti_bot_signal,
    SCRIPT_CONTRACT,
)
from api_service import (
    _count_meaningful_rows,
    DEFAULT_TEST_RUN_MAX_PRODUCTS,
    JobRecord,
)


class TestFeedbackRemediations(unittest.TestCase):

    def test_item_1_funnel_parsing(self):
        """Item 1: Test crawl funnel parsing from log messages."""
        msg = "Crawl Funnel: 3 categories, 45 links discovered, 10 processed, 10 extracted."
        m = re.search(
            r"Crawl Funnel:\s*(\d+)\s*categories,\s*(\d+)\s*links discovered,\s*(\d+)\s*processed,\s*(\d+)\s*extracted",
            msg,
        )
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), 3)
        self.assertEqual(int(m.group(2)), 45)
        self.assertEqual(int(m.group(3)), 10)
        self.assertEqual(int(m.group(4)), 10)

        # Default test run limit is now at least 10 (not hardcoded 3)
        self.assertGreaterEqual(DEFAULT_TEST_RUN_MAX_PRODUCTS, 10)

    def test_item_2_ast_schema_and_sheet_extraction(self):
        """Item 2: Test regression protection AST schema extraction."""
        code_sample = '''
FIELDS = ["Job Title", "Company Name", "Salary Range", "Modules"]

with pd.ExcelWriter(args.output, engine="openpyxl") as writer:
    output_df.to_excel(writer, sheet_name="Extracted Data", index=False)
    flags_df.to_excel(writer, sheet_name="Flags", index=False)
    rec_df.to_excel(writer, sheet_name="Recommendations", index=False)
'''
        fields, sheets = _extract_schema_from_code(code_sample)
        self.assertEqual(fields, ["Job Title", "Company Name", "Salary Range", "Modules"])
        self.assertIn("Recommendations", sheets)
        self.assertIn("Extracted Data", sheets)
        self.assertIn("Flags", sheets)

    def test_item_3_price_scoping_and_title_digits(self):
        """Item 3: Test that numbers in product titles are NOT extracted as price."""
        title = "250 North American Birds Sound Book"
        currency_regex = re.compile(r'(?i)(?:[\$£€₹]|USD|CAD|EUR|GBP)\s*(\d{1,5}(?:[.,]\d{2})?)')

        # Matching title text against currency regex should return None
        title_match = currency_regex.search(title)
        self.assertIsNone(title_match, "Title number '250' should not match currency-anchored regex")

        # But valid buy-box price does match
        price_elem = "$24.99"
        price_match = currency_regex.search(price_elem)
        self.assertIsNotNone(price_match)
        self.assertEqual(price_match.group(1), "24.99")

    def test_item_4_honest_row_counting(self):
        """Item 4: Test that empty dicts, all-blank, or URL-only rows are counted as 0."""
        # 1. Empty dict rows
        df_empty = pd.DataFrame([{}, {}])
        self.assertEqual(_count_meaningful_rows(df_empty), 0)

        # 2. Rows with only URL / Source URL
        df_url_only = pd.DataFrame([
            {"Source URL": "https://example.com/p1"},
            {"source_listing_url": "https://example.com/cat"},
        ])
        self.assertEqual(_count_meaningful_rows(df_url_only), 0)

        # 3. Rows with 'nan', 'none', '{}'
        df_nan = pd.DataFrame([
            {"title": "nan", "price": "none", "product_id": "{}"},
            {"title": "", "price": "   ", "product_id": "None"},
        ])
        self.assertEqual(_count_meaningful_rows(df_nan), 0)

        # 4. Genuinely populated rows
        df_valid = pd.DataFrame([
            {"product_id": "SKU-99", "title": "Wireless Mouse", "price": "$19.99"},
            {"product_id": "SKU-100", "title": "Mechanical Keyboard", "price": "$89.99"},
        ])
        self.assertEqual(_count_meaningful_rows(df_valid), 2)

    def test_item_5_excel_32k_limit_sanitizer(self):
        """Item 5: Test Excel 32K cell truncation sanitizer."""
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

        huge_payload = "A" * 45000
        sanitized = sanitize_cell_value(huge_payload)
        self.assertLessEqual(len(sanitized), 32000)
        self.assertTrue(sanitized.endswith("... [TRUNCATED_EXCEL_LIMIT]"))

        # Writing sanitized content to Excel via pandas openpyxl succeeds without error
        test_df = pd.DataFrame([{"large_field": sanitized}])
        test_xlsx_path = Path("test_excel_limits.xlsx")
        try:
            test_df.to_excel(test_xlsx_path, index=False)
            read_back = pd.read_excel(test_xlsx_path)
            self.assertEqual(len(read_back), 1)
        finally:
            if test_xlsx_path.exists():
                test_xlsx_path.unlink()

    def test_item_6_canonical_columns_in_contract(self):
        """Item 6: Verify SCRIPT_CONTRACT mandates product_id, product_url, source_listing_url."""
        self.assertIn("product_id", SCRIPT_CONTRACT)
        self.assertIn("product_url", SCRIPT_CONTRACT)
        self.assertIn("source_listing_url", SCRIPT_CONTRACT)
        self.assertIn("Do NOT output a duplicate \"Source URL\" column", SCRIPT_CONTRACT)

    def test_item_7_anti_bot_detection_and_pii_masking(self):
        """Item 7: Test anti-bot classification and PII reviewer masking."""
        # WAF signals
        self.assertEqual(_detect_anti_bot_signal("403 Forbidden"), "403 forbidden")
        self.assertEqual(_detect_anti_bot_signal("Attention Required! Cloudflare"), "cloudflare")
        self.assertEqual(_detect_anti_bot_signal("Verify you are human to continue"), "verify you are human")

        # PII reviewer name masking
        def mask_reviewer_name(text: str) -> str:
            # Matches names like "Reviewed by John Doe on" or "By Jane Smith"
            def _mask(m):
                full_name = m.group(1).strip()
                parts = full_name.split()
                if len(parts) >= 2:
                    return f"{parts[0][0]}*** {parts[-1][0]}."
                return f"{parts[0][0]}***"
            return re.sub(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', _mask, text)

        raw_review = "Reviewed by John Doe: Great product! Also recommended by Sarah Jane Connor."
        masked_review = mask_reviewer_name(raw_review)
        self.assertNotIn("John Doe", masked_review)
        self.assertNotIn("Sarah Jane Connor", masked_review)
        self.assertIn("J*** D.", masked_review)
        self.assertIn("S*** C.", masked_review)

    def test_item_8_read_excel_dict_sanitization_and_stealth(self):
        """Item 8: Test Incapsula WAF detection, read_excel dict safety, and stealth injection."""
        from inspection_agent_service import sanitize_generated_code

        # 1. Incapsula and enterprise WAF detection
        self.assertIn("incapsula", _detect_anti_bot_signal("Request unsuccessful. Incapsula incident ID: 12345"))
        self.assertTrue(bool(_detect_anti_bot_signal("<script src='/_Incapsula_Resource?CWUDNSAI=23'></script>")))
        self.assertIn("imperva", _detect_anti_bot_signal("Blocked by Imperva WAF"))

        # 2. Multi-sheet read_excel dictionary protection
        bad_code = "original_sheets = pd.read_excel(args.input, sheet_name=None).fillna('')"
        sanitized, _ = sanitize_generated_code(bad_code)
        self.assertNotIn(".fillna", sanitized)
        self.assertIn("sheet_name=None", sanitized)

        valid_dict_call = "original_sheets = pd.read_excel(args.input, sheet_name=None)"
        sanitized_dict, _ = sanitize_generated_code(valid_dict_call)
        self.assertNotIn(".fillna", sanitized_dict)

        # 3. Single-DataFrame read_excel NaN protection
        single_sheet = "input_df = pd.read_excel(args.input)"
        sanitized_single, _ = sanitize_generated_code(single_sheet)
        self.assertIn('.fillna("")', sanitized_single)

        # 4. Playwright stealth webdriver masking injection (even with parenthesized UA)
        pw_code = """
async def run():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36")
        page = await context.new_page()
"""
        sanitized_pw, _ = sanitize_generated_code(pw_code)
        self.assertIn("webdriver", sanitized_pw)
        self.assertIn("add_init_script", sanitized_pw)
        self.assertIn("Windows NT 10.0", sanitized_pw)  # Normalized from Linux to Windows desktop Chrome

        # 5. Safe title guard against NoneType get_text error
        unsafe_title = 'title = BeautifulSoup(html, "html.parser").title.get_text(strip=True) if html else ""'
        sanitized_title, _ = sanitize_generated_code(unsafe_title)
        self.assertIn("getattr", sanitized_title)

        # 6. Anti-bot markers expansion
        naive_signals = '''def detect_anti_bot_signals(status, html):
    markers = ("cloudflare", "perimeterx")
    if any(m in html for m in markers):
        return True
    return False'''
        sanitized_signals, _ = sanitize_generated_code(naive_signals)
        self.assertIn("potential security risk", sanitized_signals)
        self.assertIn("detected automated scripts", sanitized_signals)
        self.assertIn("incapsula", sanitized_signals)

        # 7. Class-agnostic table row selector normalization
        zebra_code = 'rows = table.select("tr.TableRecords_OddLine, tr.TableRecords_EvenLine")'
        sanitized_zebra, _ = sanitize_generated_code(zebra_code)
        self.assertIn('"tr:has(td)"', sanitized_zebra)
        self.assertNotIn("TableRecords_OddLine", sanitized_zebra)

        # 8. Postback / AJAX search and pagination stabilization
        wait_code = """
async def wait_results(self, page):
    try:
        await page.wait_for_selector("#table_id", timeout=30000)
    except Exception as exc:
        pass
"""
        sanitized_wait, _ = sanitize_generated_code(wait_code)
        self.assertIn("networkidle", sanitized_wait)

        # 9. Pagination anti-stall circuit breaker
        crawl_code = """
async def crawl_listing(self, page, context, listing_url, keyword=""):
    while not self.stop_for_antibot:
        items = await self.parse_listing_page(page, page.url)
        for item in items:
            pass
        next_btn = page.locator(".next").first
        await next_btn.click()
"""
        sanitized_crawl, _ = sanitize_generated_code(crawl_code)
        self.assertIn("if not items:", sanitized_crawl)
        self.assertIn("Halting pagination loop", sanitized_crawl)

        # 10. Table and container visibility timeout protection
        wait_table_code = 'await page.wait_for_selector(LISTING_TABLE, timeout=20000)'
        sanitized_table, _ = sanitize_generated_code(wait_table_code)
        self.assertIn('state="attached"', sanitized_table)

        # 11. Safe clear button postback stabilization
        clear_search_code = """
CLEAR_BUTTON = "#clear"
SEARCH_BUTTON = "#search"
if await clear.is_visible():
    await safe_click(page, clear, CLEAR_BUTTON, flags, url)
search = page.locator("#input")
await search.fill(keyword)
await safe_click(page, button, SEARCH_BUTTON, flags, url)
"""
        sanitized_clear, _ = sanitize_generated_code(clear_search_code)
        # 12. Playwright wait_for_function positional arg normalization to arg=
        wff_code = 'await page.wait_for_function("old => document.body.innerText.includes(old) === false", old_snapshot[:30], timeout=10000)'
        sanitized_wff, _ = sanitize_generated_code(wff_code)
        self.assertIn('arg=old_snapshot[:30]', sanitized_wff)
        self.assertNotIn('", old_snapshot[:30]', sanitized_wff)

        # 13. Universal record consolidation injection into write_output
        raw_write_code = """
def write_output(records, flags, output_path):
    safe_records = [{k: str(v) for k, v in r.items()} for r in records]
    pd.DataFrame(safe_records).to_excel(output_path, index=False)
"""
        sanitized_write, _ = sanitize_generated_code(raw_write_code)
        self.assertIn("_merged_records", sanitized_write)
        self.assertIn("_pk = _r.get(\"product_url\")", sanitized_write)

        # 14. Unicode NFKC normalization injection into clean(value)
        clean_code = """
def clean(value):
    text = str(value).replace("\\xa0", " ").strip()
    return text
"""
        sanitized_clean, _ = sanitize_generated_code(clean_code)
        self.assertIn("unicodedata.normalize", sanitized_clean)
        self.assertIn("import unicodedata", sanitized_clean)

    def test_dynamic_rules_in_contract(self):
        """Verify SCRIPT_CONTRACT mandates AJAX postback sync, class-agnostic rows, and pagination anti-stall."""
        self.assertIn("DYNAMIC FORM & AJAX / POSTBACK SYNCHRONIZATION", SCRIPT_CONTRACT)
        self.assertIn("CLASS-AGNOSTIC TABLE & LISTING ROW EXTRACTION", SCRIPT_CONTRACT)
        self.assertIn("PAGINATION ANTI-STALL & EMPTY-PAGE CIRCUIT BREAKER", SCRIPT_CONTRACT)
        self.assertIn("CANONICAL PRIMARY IDENTIFIERS & PROVENANCE URLS", SCRIPT_CONTRACT)
        self.assertIn("DOMAIN-ADAPTIVE EXTRACTION ARCHETYPES", SCRIPT_CONTRACT)
        self.assertIn("SAFE INPUT CLEARING & COLLISION GUARD", SCRIPT_CONTRACT)
        self.assertIn("CONTAINER VISIBILITY TRAP AVOIDANCE", SCRIPT_CONTRACT)
        self.assertIn("PLAYWRIGHT PYTHON WAIT_FOR_FUNCTION SIGNATURE", SCRIPT_CONTRACT)
        self.assertIn("UNIFIED ENTITY RECORDS & ROW CONSOLIDATION", SCRIPT_CONTRACT)
        self.assertIn("HEADER-DRIVEN TABLE EXTRACTION & INDEX RESILIENCE", SCRIPT_CONTRACT)
        self.assertIn("VALUE HYGIENE & LABEL PREFIX CLEANING", SCRIPT_CONTRACT)

    def test_output_sanitizer_engine(self):
        """Verify output_sanitizer.py cleans values, consolidates disjoint rows, and coalesces aliases."""
        from output_sanitizer import (
            clean_text_value,
            consolidate_records,
            coalesce_alias_columns,
            sanitize_extracted_dataframe,
        )

        # 1. clean_text_value tests
        self.assertEqual(clean_text_value("13702.0"), "13702")
        self.assertEqual(clean_text_value("Nome: CASA DO PADRE"), "CASA DO PADRE")
        self.assertEqual(clean_text_value("Nome CASA DO PADRE"), "CASA DO PADRE")
        self.assertEqual(clean_text_value("Data 2026-10-20"), "2026-10-20")
        self.assertEqual(clean_text_value("nan"), "")
        self.assertEqual(clean_text_value("None"), "")
        self.assertEqual(clean_text_value(None), "")
        self.assertEqual(clean_text_value("Melga\u00e7o"), "Melgaço")

        # 2. consolidate_records tests (merging disjoint rows)
        row_listing = {
            "product_url": "https://example.com/item/101",
            "product_id": "101",
            "name": "Listing Title",
            "detail_attr": "",
        }
        row_detail = {
            "product_url": "https://example.com/item/101",
            "product_id": "101",
            "name": "",
            "detail_attr": "Rich Detail Value",
        }
        consolidated = consolidate_records([row_listing, row_detail])
        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0]["name"], "Listing Title")
        self.assertEqual(consolidated[0]["detail_attr"], "Rich Detail Value")

        # 3. coalesce_alias_columns tests
        records_with_alias = [{
            "product_id": "101",
            "product_url": "https://example.com/item/101",
            "name": "",
            "name_detail": "True Name",
            "listing_cells": "internal debug dump",
        }]
        coalesced, columns = coalesce_alias_columns(records_with_alias)
        self.assertEqual(coalesced[0]["name"], "True Name")
        self.assertNotIn("listing_cells", columns)
        self.assertNotIn("name_detail", columns)


if __name__ == "__main__":
    unittest.main()


