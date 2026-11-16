"""
output_sanitizer.py - Universal Scraper Studio Output Sanitization & Consolidation Engine
========================================================================================
General-purpose, domain-agnostic post-processing utility for web scraping datasets.
Functions across all site archetypes (e-commerce, registries, job boards, real estate, directories):
  1. Record Consolidation: Merges disjoint/split listing + detail rows by primary entity key.
  2. Column Alias Coalescing: Harmonizes duplicate/variant column names (e.g. name / name_detail).
  3. Unicode & Diacritic Normalization: Preserves international characters (NFKC) without mojibake.
  4. Label Prefix Stripping: Cleans DOM label artifacts (e.g. 'Nome CASA' -> 'CASA').
  5. Excel 32K Protection & Null Hygiene: Strips 'nan'/'none' strings and enforces Excel limits.
"""

import argparse
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from openpyxl import Workbook, load_workbook


EXCEL_CELL_LIMIT = 32000
INTERNAL_DEBUG_PATTERNS = (
    r"^listing_cells$",
    r"^raw_html$",
    r"^row_html$",
    r"^_temp",
    r"^_internal",
    r"^debug_",
)

# Common label prefix patterns across multiple languages (Portuguese, Spanish, French, English, German)
LABEL_PREFIX_REGEX = re.compile(
    r"^(?:Nome|Name|Data|Date|Localização|Localizacao|Location|Address|Tipo|Type|Tipologia|Typology|"
    r"Status|Estado|Marca\s+comercial|Brand|Preço|Price|Capacidade|Capacity|ID|Nº|No\.|Categoria|Category)"
    r"(?:\s*[:\-]\s*|\s+(?=[A-Z0-9À-ÖØ-öø-ÿ]))"
    r"(.+)$",
    re.IGNORECASE | re.UNICODE,
)

# Canonical identity synonyms that should coalesce into standard contract keys
CANONICAL_SYNONYMS = {
    "detail_url": "product_url",
    "item_url": "product_url",
    "listing_url": "source_listing_url",
    "category_url": "source_listing_url",
    "search_url": "source_listing_url",
}


def clean_text_value(val: Any, max_len: int = EXCEL_CELL_LIMIT) -> str:
    """
    Universally cleans, normalizes, and sanitizes a single cell value:
      - Converts None, pd.isna, 'nan', 'none', 'null', '{}', '[]' to empty string.
      - Normalizes Unicode to standard NFKC format (preserving diacritics ç, ã, é, ü, etc.).
      - Strips non-breaking spaces (\xa0) and zero-width artifacts (\u200b, \ufeff).
      - Converts integer-valued floats ('13702.0') to clean integer strings ('13702').
      - Strips explicit label prefixes ('Nome: CASA DO PADRE' -> 'CASA DO PADRE').
      - Enforces the Excel cell character limit with safe truncation.
    """
    if val is None:
        return ""

    try:
        if pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass

    if isinstance(val, (dict, list)):
        if not val:
            return ""
        s = json.dumps(val, ensure_ascii=False)
    else:
        s = str(val)

    # Unicode normalization (NFKC ensures composed glyphs like ç, ã, é are standardized)
    s = unicodedata.normalize("NFKC", s)

    # Remove non-breaking spaces, zero-width spaces, and normalize whitespace
    s = s.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"[\r\n\t]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    # Reject null strings
    if s.lower() in ("", "nan", "none", "null", "undefined", "{}", "[]"):
        return ""

    # Normalize integer-valued float strings (e.g. '13702.0' -> '13702')
    float_match = re.match(r"^(\d+)\.0+$", s)
    if float_match:
        s = float_match.group(1)

    # Strip explicit label prefix if present (e.g. 'Nome: CASA' -> 'CASA', 'Date: 2026-10-20' -> '2026-10-20')
    prefix_match = LABEL_PREFIX_REGEX.match(s)
    if prefix_match:
        cleaned_inner = prefix_match.group(1).strip()
        if cleaned_inner:
            s = cleaned_inner

    # Excel 32K limit enforcement
    if len(s) > max_len:
        s = s[:max_len - 35] + "... [TRUNCATED_EXCEL_LIMIT]"

    return s


def consolidate_records(
    records: List[Dict[str, Any]],
    id_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Merges disjoint / split records belonging to the same entity.
    If a scraper appended listing records and detail records separately,
    they are identified by canonical primary keys (product_url, product_id, detail_url)
    and consolidated into a single complete record per unique entity.
    """
    if not records:
        return []

    if id_keys is None:
        id_keys = ["product_url", "product_id", "detail_url", "url", "id", "sku"]

    merged_map: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    unkeyed_counter = 0

    for r in records:
        # Determine the best primary key for grouping
        primary_key = ""
        for k in id_keys:
            val = clean_text_value(r.get(k, ""))
            if val and val.lower() not in ("nan", "none", "null"):
                primary_key = f"{k}::{val}"
                break

        if not primary_key:
            unkeyed_counter += 1
            primary_key = f"__unkeyed_{unkeyed_counter}__"

        if primary_key not in merged_map:
            merged_map[primary_key] = {}
            order.append(primary_key)

        # Merge fields into existing entity record:
        target = merged_map[primary_key]
        for key, val in r.items():
            cleaned_val = clean_text_value(val)
            existing_cleaned = clean_text_value(target.get(key, ""))
            # If target lacks this value, or current val is richer/longer, update it
            if cleaned_val and not existing_cleaned:
                target[key] = cleaned_val
            elif cleaned_val and existing_cleaned and len(cleaned_val) > len(existing_cleaned):
                # Never clobber clean integer or structured product_id with a longer unformatted string
                if key == "product_id" and re.match(r"^\d+$", existing_cleaned):
                    continue
                target[key] = cleaned_val
            elif key not in target:
                target[key] = cleaned_val

    return [merged_map[k] for k in order]


def coalesce_alias_columns(
    records: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Harmonizes duplicate alias columns and prunes internal debug columns:
      - Coalesces variants like 'name' and 'name_detail' -> 'name'.
      - Coalesces 'typology' and 'detail_typology' -> 'typology'.
      - Prunes crawler scratch columns (listing_cells, _temp_*, raw_html).
      - Orders canonical identifier columns first (product_id, product_url, source_listing_url).
    """
    if not records:
        return [], []

    # 1. Gather all existing column keys across records
    all_keys: Set[str] = set()
    for r in records:
        all_keys.update(r.keys())

    # 2. Identify and prune internal debug keys
    debug_keys = set()
    for k in all_keys:
        for pat in INTERNAL_DEBUG_PATTERNS:
            if re.match(pat, k, re.IGNORECASE):
                debug_keys.add(k)
                break

    clean_keys = all_keys - debug_keys

    # 3. Detect alias mappings (canonical synonyms, case-insensitive folding, and suffix/prefix pairs)
    alias_map: Dict[str, str] = {}

    # 3a. Canonical identity synonyms (e.g. detail_url -> product_url, listing_url -> source_listing_url)
    for k in clean_keys:
        k_norm = k.lower().replace(" ", "_")
        if k_norm in CANONICAL_SYNONYMS:
            target = CANONICAL_SYNONYMS[k_norm]
            if target in clean_keys and target != k:
                alias_map[k] = target

    # 3b. Case-insensitive key folding
    lower_map: Dict[str, str] = {}
    for k in sorted(clean_keys):
        lk = k.lower()
        if lk not in lower_map:
            lower_map[lk] = k
        else:
            existing = lower_map[lk]
            if k[0].isupper() and not existing[0].isupper():
                lower_map[lk] = k

    for k in clean_keys:
        canonical_case = lower_map[k.lower()]
        if k != canonical_case and k not in alias_map:
            alias_map[k] = canonical_case

    # 3c. Prefix/suffix alias pairs (e.g. 'name' & 'name_detail')
    for k in sorted(clean_keys, key=len, reverse=True):
        m_detail_suffix = re.match(r"^(.+)_(?:detail|page|raw|scraped)$", k, re.I)
        m_detail_prefix = re.match(r"^(?:detail|page|raw|scraped)_(.+)$", k, re.I)
        candidate = None
        if m_detail_suffix:
            candidate = m_detail_suffix.group(1)
        elif m_detail_prefix:
            candidate = m_detail_prefix.group(1)

        if candidate:
            target = None
            if candidate in clean_keys and candidate != k:
                target = candidate
            elif candidate.lower() in lower_map and lower_map[candidate.lower()] != k:
                target = lower_map[candidate.lower()]
            if target:
                alias_map[k] = target

    # 4. Build coalesced records
    coalesced_records: List[Dict[str, Any]] = []
    for r in records:
        new_row: Dict[str, Any] = {}
        # Apply alias coalescence
        for k, v in r.items():
            if k in debug_keys:
                continue
            target_key = alias_map.get(k, k)
            cleaned_val = clean_text_value(v)
            if cleaned_val:
                existing = clean_text_value(new_row.get(target_key, ""))
                if not existing:
                    new_row[target_key] = cleaned_val
                elif target_key in ("product_id", "product_url", "source_listing_url") and existing:
                    # Do not overwrite canonical identity key with alias
                    pass
                elif len(cleaned_val) > len(existing):
                    new_row[target_key] = cleaned_val
            elif target_key not in new_row:
                new_row[target_key] = ""
        coalesced_records.append(new_row)

    # 5. Determine clean final column ordering
    final_keys_set: Set[str] = set()
    for r in coalesced_records:
        final_keys_set.update(r.keys())

    priority_keys = ["product_id", "product_url", "source_listing_url"]
    ordered_columns = [k for k in priority_keys if k in final_keys_set]
    remaining_columns = sorted([k for k in final_keys_set if k not in priority_keys])
    ordered_columns.extend(remaining_columns)

    return coalesced_records, ordered_columns


def sanitize_extracted_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Applies complete universal sanitization, record consolidation,
    and column coalescence to a pandas DataFrame.
    """
    if df.empty:
        return df

    raw_records = df.to_dict(orient="records")
    # Clean all individual cell values first
    for r in raw_records:
        for k in list(r.keys()):
            r[k] = clean_text_value(r[k])

    # Consolidate disjoint rows
    consolidated = consolidate_records(raw_records)

    # Coalesce aliases and prune debug columns
    coalesced, ordered_columns = coalesce_alias_columns(consolidated)

    # Create cleaned DataFrame
    clean_df = pd.DataFrame(coalesced)
    if ordered_columns:
        existing_cols = [c for c in ordered_columns if c in clean_df.columns]
        clean_df = clean_df[existing_cols]

    return clean_df.fillna("")


def sanitize_excel_workbook(
    input_path: str | Path,
    output_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Sanitizes an entire Excel workbook in-place or writes to output_path:
      - Reads and consolidates 'Extracted Data'.
      - Cleans all cell values and removes duplicate sparse rows.
      - Preserves all other sheets ('Flags', 'Recommendations', etc.) intact.
    """
    src = Path(input_path)
    dest = Path(output_path) if output_path else src

    if not src.exists():
        raise FileNotFoundError(f"Input workbook not found: {src}")

    wb = load_workbook(src)
    stats: Dict[str, Any] = {
        "original_rows": 0,
        "clean_rows": 0,
        "original_columns": 0,
        "clean_columns": 0,
    }

    if "Extracted Data" in wb.sheetnames:
        df_raw = pd.read_excel(src, sheet_name="Extracted Data")
        stats["original_rows"] = len(df_raw)
        stats["original_columns"] = len(df_raw.columns)

        df_clean = sanitize_extracted_dataframe(df_raw)
        stats["clean_rows"] = len(df_clean)
        stats["clean_columns"] = len(df_clean.columns)

        # Replace 'Extracted Data' sheet
        idx = wb.sheetnames.index("Extracted Data")
        del wb["Extracted Data"]
        ws = wb.create_sheet("Extracted Data", idx)

        # Write header
        ws.append(list(df_clean.columns))
        # Write rows
        for row in df_clean.itertuples(index=False):
            ws.append([clean_text_value(v) for v in row])

    wb.save(dest)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Universal Scraper Studio Output Sanitizer")
    parser.add_argument("--input", required=True, help="Path to input Excel file (.xlsx)")
    parser.add_argument("--output", required=False, default=None, help="Path to clean output Excel file (defaults to overwriting input)")
    args = parser.parse_args()

    input_file = Path(args.input)
    output_file = Path(args.output) if args.output else input_file

    print(f"[output_sanitizer] Processing workbook: {input_file}")
    stats = sanitize_excel_workbook(input_file, output_file)
    print(f"[output_sanitizer] Completed successfully:")
    print(f"  Rows: {stats['original_rows']} -> {stats['clean_rows']} (consolidated duplicate/disjoint entries)")
    print(f"  Columns: {stats['original_columns']} -> {stats['clean_columns']} (coalesced aliases & pruned debug columns)")
    print(f"  Saved to: {output_file}")


if __name__ == "__main__":
    main()
