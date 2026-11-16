"""
Scraper Studio - Streamlit Application
======================================
Full-featured, modern web UI for Scraper Studio powered by OpenAI and Python.
Recreates all Builder, Live Agent Detail, Dashboard, Cost Ops, and Settings features.
"""

import asyncio
import html
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# Load .env variables immediately
load_dotenv(override=False)

# Ensure local imports work cleanly
sys.path.insert(0, os.getcwd())

import auth_config
from auth_config import require_credentials_at_startup, resolve_credentials, CredentialError
import cost_metering
from cost_metering import JSONLCostSink, DEFAULT_MAX_BUDGET_USD, OPENAI_MODEL_PRICING
import inspection_agent_service
from inspection_agent_service import (
    InspectionJob,
    InspectionResult,
    run_inspection_job,
    get_default_model,
    get_default_recon_model,
    get_default_codegen_model,
    get_default_temperature,
    sanitize_generated_code,
)
import re
import zipfile
from openpyxl import Workbook
import importlib
import api_service
importlib.reload(api_service)
from api_service import store, write_generated_files_to_disk, _build_sample_input_xlsx, TEST_RUN_MAX_PRODUCTS, CreateJobRequest, _run_test_run, refine_job_backend


def _build_scraper_zip(job_record) -> bytes:
    """
    Bundles the complete scraper deliverables into a ZIP archive:
      1. generated_scraper.py (standalone executable Python scraper)
      2. input.xlsx (standard 3-column input workbook named input.xlsx)
      3. requirements.txt (Python dependencies)
    """
    zip_buffer = io.BytesIO()
    folder = Path(job_record.saved_to) if getattr(job_record, "saved_to", None) else None

    # 1. Script content
    code_content = ""
    if folder and (folder / "generated_scraper.py").exists():
        try:
            code_content = (folder / "generated_scraper.py").read_text(encoding="utf-8")
        except Exception:
            pass
    if not code_content and job_record.result:
        code_content = job_record.result.get("generated_code", "")

    if code_content:
        try:
            code_content, _ = sanitize_generated_code(code_content)
        except Exception:
            pass

    # 2. Requirements content
    reqs_content = ""
    if folder and (folder / "requirements.txt").exists():
        try:
            reqs_content = (folder / "requirements.txt").read_text(encoding="utf-8")
        except Exception:
            pass
    if not reqs_content and job_record.result:
        reqs_content = job_record.result.get("requirements_txt", "")
    if not reqs_content:
        reqs_content = "pandas\nopenpyxl\nrequests\nbeautifulsoup4\nplaywright\n"

    # 3. input.xlsx content (named input.xlsx, matching sample_input.xlsx)
    input_xlsx_bytes = None
    if folder and (folder / "sample_input.xlsx").exists():
        try:
            input_xlsx_bytes = (folder / "sample_input.xlsx").read_bytes()
        except Exception:
            pass
    elif folder and (folder / "input.xlsx").exists():
        try:
            input_xlsx_bytes = (folder / "input.xlsx").read_bytes()
        except Exception:
            pass

    if not input_xlsx_bytes:
        wb = Workbook()
        ws = wb.active
        ws.title = "Input"
        ws.append(["keyword", "listing_url", "detail_url"])
        ws.append([job_record.sample_keyword or None, job_record.target_url, None])
        bio = io.BytesIO()
        wb.save(bio)
        input_xlsx_bytes = bio.getvalue()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        if code_content:
            zf.writestr("generated_scraper.py", code_content)
        if reqs_content:
            zf.writestr("requirements.txt", reqs_content)
        if input_xlsx_bytes:
            zf.writestr("input.xlsx", input_xlsx_bytes)
        sanitizer_file = Path(__file__).parent / "output_sanitizer.py"
        if sanitizer_file.exists():
            try:
                zf.writestr("output_sanitizer.py", sanitizer_file.read_text(encoding="utf-8"))
            except Exception:
                pass

    return zip_buffer.getvalue()

# -----------------------------------------------------------------------------
# Streamlit Page Config & Custom Styling
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Scraper Studio — Autonomous AI Web Scraping",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for modern clean white & orange theme
st.markdown("""
<style>
    /* Global Styles */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap');
    
    .stApp {
        background-color: #f8f9fc;
        font-family: 'Inter', sans-serif;
        color: #1e293b;
    }
    
    /* Sidebar Styling */
    section[data-testid="stSidebar"] {
        background-color: #f4f6f9;
        border-right: 1px solid #e2e8f0;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 10px 16px;
        margin-bottom: 8px;
        transition: all 0.2s ease;
        color: #334155;
        font-weight: 500;
        cursor: pointer;
        display: flex;
        align-items: center;
        box-shadow: 0 1px 3px rgba(0,0,0,0.02);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
        display: none !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label div[data-testid="stMarkdownContainer"] p {
        margin: 0;
        font-size: 14px;
        font-weight: 500;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        border-color: #ff8a65;
        background: #fffaf5;
        color: #ff5722;
        transform: translateY(-1px);
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label[data-checked="true"],
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
        background: linear-gradient(135deg, #ff5722 0%, #ea580c 100%) !important;
        border-color: #ea580c !important;
        color: #ffffff !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 14px rgba(255, 87, 34, 0.35) !important;
    }
    section[data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) * {
        color: #ffffff !important;
        font-weight: 600 !important;
    }
    
    /* Header Styling */
    .app-header {
        display: flex;
        align-items: flex-start;
        gap: 16px;
        padding: 22px 26px;
        background: #ffffff;
        border-radius: 14px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 2px 12px rgba(0, 0, 0, 0.03);
        margin-bottom: 24px;
    }
    .app-title {
        font-size: 26px;
        font-weight: 700;
        color: #0f172a;
        margin: 0;
        letter-spacing: -0.02em;
    }
    .app-subtitle {
        font-size: 13.5px;
        color: #64748b;
        margin-top: 5px;
        line-height: 1.5;
    }
    
    /* Stat Cards */
    .stat-card {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 18px 20px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
        transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .stat-card:hover {
        border-color: #ff5722;
        transform: translateY(-2px);
        box-shadow: 0 6px 16px rgba(255, 87, 34, 0.12);
    }
    .stat-val {
        font-size: 26px;
        font-weight: 700;
        color: #ff5722;
        font-family: 'JetBrains Mono', monospace;
    }
    .stat-lbl {
        font-size: 11.5px;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: #64748b;
        font-weight: 600;
        margin-top: 4px;
    }
    
    /* Status Badges & Pills */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-completed { background: #dcfce7; color: #16a34a; border: 1px solid #bbf7d0; }
    .badge-running { background: #ffedd5; color: #ea580c; border: 1px solid #fed7aa; }
    .badge-failed { background: #fee2e2; color: #dc2626; border: 1px solid #fecaca; }
    .badge-anti_bot { background: #fef3c7; color: #d97706; border: 1px solid #fde68a; }
    
    /* Tag Pills */
    .pill-blue { background: #e0f2fe; color: #0284c7; border: 1px solid #bae6fd; }
    .pill-purple { background: #f3e8ff; color: #9333ea; border: 1px solid #e9d5ff; }
    .pill-green { background: #dcfce7; color: #16a34a; border: 1px solid #bbf7d0; }
    .pill-orange { background: #ffedd5; color: #ea580c; border: 1px solid #fed7aa; }
    
    /* Banner Panels */
    .banner-panel {
        background: linear-gradient(135deg, #fffaf5 0%, #ffffff 100%);
        border: 1px solid #ffedd5;
        border-left: 4px solid #ff5722;
        border-radius: 12px;
        padding: 22px 24px;
        margin-bottom: 24px;
        box-shadow: 0 2px 8px rgba(0, 0, 0, 0.02);
    }
    
    /* Log Box */
    .log-container {
        background: #0f172a;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 14px 16px;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
        color: #e2e8f0;
        max-height: 380px;
        overflow-y: auto;
    }
    .log-entry {
        margin-bottom: 6px;
        padding: 4px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    }
    .log-ts { color: #94a3b8; margin-right: 8px; }
    .log-phase { color: #fb923c; font-weight: 600; margin-right: 8px; }
    .log-tool { color: #38bdf8; font-weight: 600; margin-right: 8px; }
    
    /* Custom Buttons */
    div.stButton > button:first-child, div.stDownloadButton > button:first-child {
        background: linear-gradient(135deg, #ff5722 0%, #ea580c 100%) !important;
        color: white !important;
        font-weight: 600 !important;
        border: none !important;
        border-radius: 8px !important;
        padding: 10px 20px !important;
        transition: all 0.2s ease !important;
        box-shadow: 0 2px 8px rgba(255, 87, 34, 0.25) !important;
    }
    div.stButton > button:first-child:hover, div.stDownloadButton > button:first-child:hover {
        background: linear-gradient(135deg, #f4511e 0%, #c2410c 100%) !important;
        box-shadow: 0 4px 14px rgba(255, 87, 34, 0.4) !important;
        transform: translateY(-1px);
    }
    
    /* Metrics */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.02);
    }
    div[data-testid="stMetricValue"] > div {
        color: #ff5722 !important;
        font-weight: 700 !important;
        font-size: 24px !important;
        line-height: 1.2 !important;
    }
    div[data-testid="stMetricLabel"] {
        color: #64748b !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        font-size: 11px !important;
        letter-spacing: 0.04em !important;
    }
    
    /* Form Inputs */
    div[data-testid="stForm"] {
        background: #ffffff;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 24px;
        box-shadow: 0 2px 10px rgba(0,0,0,0.02);
    }
    
    /* Tabs */
    button[data-baseweb="tab"][aria-selected="true"] {
        color: #ff5722 !important;
        border-bottom-color: #ff5722 !important;
    }
    div[data-baseweb="tab-highlight"] {
        background-color: #ff5722 !important;
    }

    /* Test Run Sandbox Console */
    .sandbox-terminal {
        background: #090d16;
        border: 1px solid #1e293b;
        border-radius: 12px;
        box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.25), 0 8px 10px -6px rgba(0, 0, 0, 0.2);
        overflow: hidden;
        margin: 16px 0 20px 0;
        font-family: 'JetBrains Mono', 'Consolas', monospace;
    }
    .sandbox-header {
        background: #111827;
        padding: 10px 16px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        border-bottom: 1px solid #1f2937;
    }
    .sandbox-dots {
        display: flex;
        gap: 6px;
        align-items: center;
    }
    .sandbox-dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        display: inline-block;
    }
    .sandbox-dot-red { background: #ef4444; }
    .sandbox-dot-yellow { background: #f59e0b; }
    .sandbox-dot-green { background: #10b981; }
    .sandbox-title {
        font-size: 11.5px;
        font-weight: 700;
        color: #94a3b8;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        margin-left: 6px;
    }
    .sandbox-status-pill {
        font-size: 11px;
        font-weight: 600;
        padding: 2px 10px;
        border-radius: 9999px;
        letter-spacing: 0.04em;
    }
    .sandbox-pill-running {
        background: rgba(59, 130, 246, 0.15);
        color: #60a5fa;
        border: 1px solid rgba(59, 130, 246, 0.4);
    }
    .sandbox-pill-passed {
        background: rgba(34, 197, 94, 0.15);
        color: #4ade80;
        border: 1px solid rgba(34, 197, 94, 0.4);
    }
    .sandbox-pill-failed {
        background: rgba(239, 68, 68, 0.15);
        color: #f87171;
        border: 1px solid rgba(239, 68, 68, 0.4);
    }
    .sandbox-body {
        padding: 14px 16px;
        color: #cbd5e1;
        font-size: 12px;
        line-height: 1.6;
        max-height: 380px;
        overflow-y: auto;
    }
    .sandbox-log-line {
        display: flex;
        align-items: flex-start;
        gap: 10px;
        padding: 2.5px 0;
        border-bottom: 1px solid rgba(255, 255, 255, 0.03);
    }
    .sandbox-ts {
        color: #64748b;
        font-size: 11px;
        white-space: nowrap;
        font-family: inherit;
    }
    .sandbox-badge {
        font-size: 10px;
        font-weight: 700;
        padding: 1px 6px;
        border-radius: 4px;
        white-space: nowrap;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-family: inherit;
    }
    .badge-env { background: rgba(168, 85, 247, 0.2); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.4); }
    .badge-deps { background: rgba(6, 182, 212, 0.2); color: #22d3ee; border: 1px solid rgba(6, 182, 212, 0.4); }
    .badge-playwright { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
    .badge-setup { background: rgba(99, 102, 241, 0.2); color: #818cf8; border: 1px solid rgba(99, 102, 241, 0.4); }
    .badge-scraper { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-validate { background: rgba(139, 92, 246, 0.2); color: #a78bfa; border: 1px solid rgba(139, 92, 246, 0.4); }
    .badge-self-heal { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-done { background: rgba(34, 197, 94, 0.2); color: #4ade80; border: 1px solid rgba(34, 197, 94, 0.4); }
    .badge-error { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.4); }
    .sandbox-msg {
        color: #e2e8f0;
        word-break: break-word;
        font-family: inherit;
    }
</style>
""", unsafe_allow_html=True)

def render_sandbox_terminal(logs: list, is_running: bool = False, status_override: str | None = None) -> str:
    if not logs:
        return ""
    
    if is_running:
        status_html = '<span class="sandbox-status-pill sandbox-pill-running">● RUNNING (NO TIMEOUT)</span>'
    elif status_override == "passed" or (logs and logs[-1].get("phase") == "DONE" and "PASSED" in str(logs[-1].get("message", ""))):
        status_html = '<span class="sandbox-status-pill sandbox-pill-passed">✔ PASSED</span>'
    elif status_override == "failed" or (logs and logs[-1].get("phase") in ["ERROR", "DONE"] and "FAILED" in str(logs[-1].get("message", ""))):
        status_html = '<span class="sandbox-status-pill sandbox-pill-failed">✖ FAILED</span>'
    else:
        status_html = '<span class="sandbox-status-pill sandbox-pill-running">EXECUTION LOGS</span>'

    lines_html = []
    for entry in logs:
        ts = html.escape(str(entry.get("ts", "")))
        phase = str(entry.get("phase", "LOG")).upper()
        msg = html.escape(str(entry.get("message", "")))
        level = str(entry.get("level", "info")).lower()
        
        badge_cls = "badge-setup"
        if "ENV" in phase:
            badge_cls = "badge-env"
        elif "PIP" in phase or "DEP" in phase:
            badge_cls = "badge-deps"
        elif "PW" in phase or "PLAYWRIGHT" in phase:
            badge_cls = "badge-playwright"
        elif "SCRAPER" in phase or "EXEC" in phase:
            badge_cls = "badge-scraper"
        elif "VALIDATE" in phase:
            badge_cls = "badge-validate"
        elif "SELF-HEAL" in phase or "REPAIR" in phase:
            badge_cls = "badge-self-heal"
        elif "DONE" in phase or "SUCCESS" in phase or level == "success":
            badge_cls = "badge-done"
        elif "ERROR" in phase or "FAIL" in phase or "ERR" in phase or level == "error":
            badge_cls = "badge-error"

        lines_html.append(
            f'<div class="sandbox-log-line">'
            f'<span class="sandbox-ts">[{ts}]</span>'
            f'<span class="sandbox-badge {badge_cls}">[{html.escape(phase)}]</span>'
            f'<span class="sandbox-msg">{msg}</span>'
            f'</div>'
        )

    content_html = "".join(lines_html)
    return (
        f'<div class="sandbox-terminal">'
        f'  <div class="sandbox-header">'
        f'    <div class="sandbox-dots">'
        f'      <span class="sandbox-dot sandbox-dot-red"></span>'
        f'      <span class="sandbox-dot sandbox-dot-yellow"></span>'
        f'      <span class="sandbox-dot sandbox-dot-green"></span>'
        f'      <span class="sandbox-title">⚡ ISOLATED SANDBOX EXECUTION CONSOLE</span>'
        f'    </div>'
        f'    {status_html}'
        f'  </div>'
        f'  <div class="sandbox-body" id="sandbox-body-terminal">'
        f'    {content_html}'
        f'  </div>'
        f'</div>'
    )

# Ensure jobs store is loaded
store.load_from_disk()

# -----------------------------------------------------------------------------
# Sidebar Navigation
# -----------------------------------------------------------------------------
with st.sidebar:
    st.markdown("""
    <div style="display:flex; align-items:center; gap:12px; padding:4px 0 16px 0; margin-bottom:12px; border-bottom:1px solid #e2e8f0;">
        <div style="background:linear-gradient(135deg, #ff5722 0%, #ea580c 100%); width:38px; height:38px; border-radius:10px; display:flex; align-items:center; justify-content:center; box-shadow:0 4px 12px rgba(255,87,34,0.3); font-size:20px; color:#ffffff;">⚡</div>
        <div>
            <div style="font-size:17px; font-weight:700; color:#0f172a; letter-spacing:-0.01em;">Scraper Studio</div>
            <div style="font-size:11.5px; color:#64748b; font-weight:500;">Autonomous AI Scraper</div>
        </div>
    </div>
    <div style="font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:0.08em; color:#94a3b8; margin:12px 4px 8px 4px;">Navigation</div>
    """, unsafe_allow_html=True)
    
    nav_selection = st.radio(
        "Navigation",
        ["⚡ Builder", "📊 Dashboard"],
        label_visibility="collapsed",
    )
    
    st.markdown("<div style='height: 24px;'></div>", unsafe_allow_html=True)
    
    st.markdown("""
    <div style="padding:14px; background:#ffffff; border:1px solid #e2e8f0; border-radius:12px; box-shadow:0 1px 3px rgba(0,0,0,0.02); margin-top:16px;">
        <div style="display:flex; align-items:center; gap:8px; font-size:12px; font-weight:600; color:#0f172a;">
            <span style="display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; box-shadow:0 0 6px #22c55e;"></span>
            <span>Agent Engine Ready</span>
        </div>
        <div style="font-size:11px; color:#64748b; margin-top:5px; line-height:1.4;">OpenAI Playwright agent ready for live inspection & synthesis.</div>
    </div>
    """, unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# 1. BUILDER (HOME) VIEW
# -----------------------------------------------------------------------------
if nav_selection == "⚡ Builder":
    st.markdown("""
    <div class="app-header">
        <span style="font-size:36px; line-height:1;">⚡</span>
        <div>
            <h1 class="app-title">Scraper Studio</h1>
            <div class="app-subtitle">Describe what to collect in plain English. OpenAI (GPT-5.6 Luna) inspects the live site with Playwright and generates production-ready Python scrapers.</div>
            <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    with st.form("builder_form"):
        col1, col2 = st.columns([2, 1])
        with col1:
            job_name = st.text_input("Scraper Name", placeholder="e.g. Samsung Smartphones Data Collector")
            target_url = st.text_input("Target URL (Listing or Detail Page)", placeholder="https://www.example.com/products")
        with col2:
            sample_keyword = st.text_input("Sample Keyword (Optional)", placeholder="e.g. Galaxy S24")
            uploaded_excel = st.file_uploader("Sample Input Excel (.xlsx)", type=["xlsx", "xls"], help="Upload an Excel file with keyword/listing_url columns to test with.")
        
        st.markdown("### Natural Language Rules")
        tab_search, tab_filter, tab_nav, tab_extract = st.tabs(["🔎 Search Rules", "🎯 Filter Rules", "🧭 Navigation Rules", "📦 Extraction Rules"])
        
        with tab_search:
            search_rules = st.text_area("Search Rules", placeholder="Describe search rules (e.g. Match products whose title or slug contains the search keyword)...", height=90)
        with tab_filter:
            filter_rules = st.text_area("Filter Rules", placeholder="Describe filter rules (e.g. Ignore accessories, cases, and refurbished products)...", height=90)
        with tab_nav:
            navigation_rules = st.text_area("Navigation Rules", placeholder="Describe navigation rules (e.g. Listing page -> click through to each product detail page to inspect specs)...", height=90)
        with tab_extract:
            extraction_rules = st.text_area("Extraction Rules (Fields to capture)", placeholder="Describe extraction rules (e.g. Product Title, Model Code, Price, Color, Specs, Availability)...", height=90)
        
        with st.expander("⚙️ Advanced Settings (Models, Limits, & Budget)"):
            c1, c2, c3 = st.columns(3)
            with c1:
                recon_model = st.selectbox("Phase 1 (Recon) Model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"], index=0)
                max_steps = st.number_input("Max Inspection Steps", min_value=5, max_value=100, value=40)
            with c2:
                codegen_model = st.selectbox("Phase 2 (Codegen) Model", ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"], index=0)
                max_seconds = st.number_input("Max Wall-Clock Time (Seconds)", min_value=30, max_value=1200, value=600)
            with c3:
                temperature = st.slider("Temperature", min_value=0.0, max_value=2.0, value=float(get_default_temperature()), step=0.1)
                max_budget = st.number_input("Max Budget ($USD)", min_value=0.10, max_value=10.0, value=1.50, step=0.25)
            
            mask_reviews = st.checkbox("Mask Reviewer PII & Skip Review Accordions", value=True, help="Excludes customer review accordions from spec extraction and masks reviewer personal names/emails.")
        
        submit_btn = st.form_submit_button("🚀 Generate Scraper")

    if submit_btn:
        if not target_url or not target_url.strip():
            st.error("Please provide a valid Target URL.")
        else:
            effective_job_name = job_name.strip() if job_name and job_name.strip() else "Custom Scraper"
            effective_extraction = extraction_rules.strip() if extraction_rules else ""
            if mask_reviews:
                effective_extraction = (effective_extraction + "\nMask reviewer PII and skip review accordions.").strip()

            # Process uploaded Excel if present
            uploaded_excel_bytes = None
            extracted_keyword = None
            if uploaded_excel is not None:
                try:
                    uploaded_excel_bytes = uploaded_excel.getvalue()
                    import io
                    import pandas as pd
                    df_up = pd.read_excel(io.BytesIO(uploaded_excel_bytes), header=None)
                    for val in df_up.iloc[:, 0].dropna():
                        s = str(val).strip()
                        if s and s.lower() not in ("keyword", "keywords", "search", "nan", "none"):
                            extracted_keyword = s
                            break
                    if not extracted_keyword and not df_up.empty:
                        extracted_keyword = str(df_up.iloc[0, 0]).strip()
                except Exception as up_err:
                    print(f"Warning: could not parse uploaded excel: {up_err}")

            active_sample_keyword = sample_keyword.strip() if sample_keyword else (extracted_keyword or None)

            job_req = InspectionJob(
                job_id=str(time.time()).replace(".", "-"),
                target_url=target_url.strip(),
                sample_keyword=active_sample_keyword,
                search_rules=search_rules.strip() if search_rules else "",
                filter_rules=filter_rules.strip() if filter_rules else "",
                navigation_rules=navigation_rules.strip() if navigation_rules else "",
                extraction_rules=effective_extraction,
                max_steps=max_steps,
                max_seconds=max_seconds,
                max_budget_usd=max_budget,
                recon_model=recon_model,
                codegen_model=codegen_model,
                temperature=temperature,
            )
            
            # Create Job Record
            async def _run_job_flow():
                record = await store.create(api_service.CreateJobRequest(
                    name=effective_job_name,
                    target_url=target_url.strip(),
                    sample_keyword=active_sample_keyword,
                    search_rules=search_rules.strip() if search_rules else "",
                    filter_rules=filter_rules.strip() if filter_rules else "",
                    navigation_rules=navigation_rules.strip() if navigation_rules else "",
                    extraction_rules=effective_extraction,
                    max_steps=max_steps,
                    max_seconds=max_seconds,
                    max_budget_usd=max_budget,
                    recon_model=recon_model,
                    codegen_model=codegen_model,
                    temperature=temperature,
                ))
                
                status_placeholder = st.empty()
                log_placeholder = st.empty()
                
                live_logs = []
                async def _on_log(entry):
                    live_logs.append(entry)
                    log_html = "".join(
                        f"<div class='log-entry'><span class='log-ts'>[{e.get('ts','')}]</span> <span class='log-phase'>[{e.get('phase','').upper()}]</span> <span class='log-tool'>{e.get('tool','')}</span> #{e.get('call_number','')}</div>"
                        for e in live_logs[-12:]
                    )
                    log_placeholder.markdown(f"<div class='log-container'>{log_html}</div>", unsafe_allow_html=True)
                
                status_placeholder.info(f"⏳ Running autonomous agent inspection for **{effective_job_name}**...")
                
                job_req.job_id = record.job_id
                res = await run_inspection_job(job_req, on_log_entry=_on_log)
                await store.update_status(record.job_id, res.status, res)

                # Persist uploaded Excel file to generated script folder if available
                rec_fresh = store.jobs.get(record.job_id)
                if rec_fresh and rec_fresh.saved_to and uploaded_excel_bytes:
                    try:
                        dest_f = Path(rec_fresh.saved_to)
                        dest_f.mkdir(parents=True, exist_ok=True)
                        (dest_f / "sample_input.xlsx").write_bytes(uploaded_excel_bytes)
                        (dest_f / "input.xlsx").write_bytes(uploaded_excel_bytes)
                    except Exception as ex_save:
                        print(f"Warning: could not write uploaded excel to disk: {ex_save}")
                
                if res.status == "completed":
                    status_placeholder.success(f"✅ Scraper successfully generated! Total spend: **${res.total_cost_usd:.4f}** • Tool calls: **{res.total_tool_calls}**")
                    if res.generated_code:
                        st.markdown("### 📄 Generated Script Preview")
                        st.code(res.generated_code[:1200] + ("\n\n# ... [Open Dashboard tab to view full script] ..." if len(res.generated_code) > 1200 else ""), language="python")
                        st.info("💡 Switch to the **📊 Dashboard** tab in the sidebar to download files, run isolated test runs, or view the complete audit log.")
                else:
                    status_placeholder.warning(f"⚠️ Inspection ended with status: **{res.status}**")
                    if res.confidence_notes:
                        for note in res.confidence_notes:
                            st.warning(note)
                
                st.session_state["selected_job_id"] = record.job_id
            
            # Execute async in streamlit
            with st.spinner("Executing autonomous agent inspection and code synthesis..."):
                async def __patched():
                    import sys; sys.set_asyncgen_hooks(finalizer=None)
                    await _run_job_flow()
                try:
                    asyncio.run(__patched())
                except BaseException as ex:
                    import traceback
                    status_placeholder.error(f"❌ Inspection process halted: {ex}")
                    st.error(f"**Error Details:** `{ex}`")
                    with st.expander("Full Traceback"):
                        st.code(traceback.format_exc(), language="python")


# -----------------------------------------------------------------------------
# 2. DASHBOARD (JOB DETAIL & TRACE) VIEW
# -----------------------------------------------------------------------------
elif nav_selection == "📊 Dashboard":
    st.markdown("""
    <div class="app-header">
        <span style="font-size:36px; line-height:1;">📊</span>
        <div>
            <h1 class="app-title">Scraper Dashboard</h1>
            <div class="app-subtitle">Inspect generated Python code, audit browser actions, analyze token spend breakdown, download assets, or run smoke tests.</div>
            <div style="display:flex; gap:8px; margin-top:12px; flex-wrap:wrap;">
                <span class="badge pill-blue">LIVE TRACE</span>
                <span class="badge pill-orange">COST BREAKDOWN</span>
                <span class="badge pill-green">CODE DELIVERABLES</span>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)
    
    all_jobs = list(store.jobs.values())
    if not all_jobs:
        st.warning("No jobs found. Create a scraper in the Builder tab first.")
    else:
        job_options = {j.job_id: f"{j.name} ({j.job_id[:8]}) — {j.status.upper()}" for j in reversed(all_jobs)}
        default_index = 0
        if "selected_job_id" in st.session_state and st.session_state["selected_job_id"] in job_options:
            default_index = list(job_options.keys()).index(st.session_state["selected_job_id"])
        
        selected_id = st.selectbox("Select Scraper Job", options=list(job_options.keys()), format_func=lambda x: job_options[x], index=default_index)
        job = store.jobs[selected_id]
        
        # Header Stats & Badges
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        with c1:
            st.markdown(f"### {job.name}")
            st.caption(f"🎯 Target: `{job.target_url}`")
        with c2:
            status_class = f"badge-{job.status}"
            st.markdown(f"<span class='badge {status_class}'>{job.status}</span>", unsafe_allow_html=True)
            st.caption(f"Created: {job.created_at}")
        with c3:
            total_cost = (job.cost or {}).get("total_cost_usd")
            st.metric("Total Spend", f"${total_cost:.4f}" if total_cost is not None else "—")
        with c4:
            tool_calls = (job.result or {}).get("total_tool_calls", 0)
            st.metric("Tool Calls", tool_calls)
        
        # Anti-Bot or Confidence Flags
        notes = (job.result or {}).get("confidence_notes", [])
        if job.anti_bot_marker or notes:
            st.markdown("<div style='background:#fffbeb; border:1px solid #fef3c7; border-left:4px solid #f59e0b; padding:14px 18px; border-radius:8px; margin:16px 0;'>", unsafe_allow_html=True)
            st.markdown("<div style='font-size:12px; font-weight:700; color:#b45309; letter-spacing:0.04em;'>⚠️ OBSERVATIONS & CONFIDENCE FLAGS</div>", unsafe_allow_html=True)
            for n in notes:
                st.markdown(f"- {n}")
            st.markdown("</div>", unsafe_allow_html=True)
        
        # Phase Breakdown Spend
        phases = (job.cost or {}).get("phases", [])
        if phases:
            st.markdown("#### Phase Spend Breakdown")
            p_cols = st.columns(len(phases))
            for i, p in enumerate(phases):
                with p_cols[i]:
                    st.metric(
                        label=f"Phase: {p.get('phase','').upper()}",
                        value=f"${p.get('total_cost_usd',0) or 0:.4f}",
                        delta=f"{p.get('num_turns',0)} turns • {p.get('cache_read_input_tokens',0)} cached tokens",
                    )
        
        res = job.result or {}
        code_str = res.get("generated_code", "")
        if code_str:
            try:
                code_str, _ = sanitize_generated_code(code_str)
            except Exception:
                pass
        reqs_str = res.get("requirements_txt", "")
        action_log = res.get("action_log", [])

        # Check for Revisions / Iteration History
        revisions = getattr(job, "revisions", []) or []
        view_code = code_str
        view_reqs = reqs_str
        active_ver_label = f"v{len(revisions)}" if revisions else "v1"

        if revisions and len(revisions) > 1:
            def _format_rev(r):
                ver = r.get("version", 1)
                lbl = r.get("label") or f"v{ver}"
                prompt = (r.get("prompt") or "Generation")[:50]
                return f"📌 {lbl} — {prompt}"

            rev_opts = {r["version"]: _format_rev(r) for r in revisions}
            selected_v = st.selectbox(
                "📜 Scraper Iteration History & Version Switcher",
                options=list(rev_opts.keys()),
                format_func=lambda v: rev_opts[v],
                index=len(revisions) - 1,
                help="Inspect previous versions of this scraper generated across refinement cycles."
            )
            selected_rev_obj = next((r for r in revisions if r["version"] == selected_v), None)
            if selected_rev_obj:
                view_code = selected_rev_obj.get("generated_code", code_str)
                view_reqs = selected_rev_obj.get("requirements_txt", reqs_str)
                active_ver_label = selected_rev_obj.get("label", f"v{selected_v}")

        # Code & Deliverables Tabs
        safe_slug = re.sub(r"[^A-Za-z0-9]+", "_", job.name).strip("_").lower() or "scraper"
        zip_filename = f"{safe_slug}_scraper.zip"
        zip_bytes = _build_scraper_zip(job) if view_code else None

        col_deliv_title, col_deliv_btn = st.columns([3, 2])
        with col_deliv_title:
            st.markdown(f"#### Generated Deliverables ({active_ver_label})")
        with col_deliv_btn:
            if view_code and zip_bytes:
                st.download_button(
                    label="📦 Download Complete Scraper (.zip)",
                    data=zip_bytes,
                    file_name=zip_filename,
                    mime="application/zip",
                    help="Downloads complete standalone scraper package: generated_scraper.py, input.xlsx, and requirements.txt.",
                    key=f"dl_zip_top_{job.job_id}",
                )

        code_tabs = st.tabs(["📄 generated_scraper.py", "📦 requirements.txt", "🔍 Action Log", "📊 Findings JSON", "🛡️ Audit Vault & Retries"])
        
        with code_tabs[0]:
            if view_code:
                st.code(view_code, language="python", line_numbers=True)
                btn_c1, btn_c2 = st.columns([1, 1])
                with btn_c1:
                    st.download_button(
                        label="📥 Download generated_scraper.py",
                        data=view_code,
                        file_name=f"{job.job_id}_generated_scraper.py",
                        mime="text/x-python",
                    )
                with btn_c2:
                    if zip_bytes:
                        st.download_button(
                            label="📦 Download Package (.zip)",
                            data=zip_bytes,
                            file_name=zip_filename,
                            mime="application/zip",
                            help="Includes generated_scraper.py, input.xlsx, and requirements.txt",
                            key=f"dl_zip_tab_{job.job_id}",
                        )
            else:
                st.info("No code was generated for this job.")
        
        with code_tabs[1]:
            if view_reqs:
                st.code(view_reqs, language="text")
                st.download_button(
                    label="📥 Download requirements.txt",
                    data=view_reqs,
                    file_name="requirements.txt",
                    mime="text/plain",
                )
            else:
                st.info("No custom requirements specified.")
        
        with code_tabs[2]:
            if action_log:
                log_html = "".join(
                    f"<div class='log-entry'><span class='log-ts'>[{e.get('ts','')}]</span> <span class='log-phase'>[{e.get('phase','').upper()}]</span> <span class='log-tool'>{e.get('tool','')}</span> #{e.get('call_number','')} <span style='color:#94a3b8;'>({json.dumps(e.get('input',{}))})</span></div>"
                    for e in action_log
                )
                st.markdown(f"<div class='log-container'>{log_html}</div>", unsafe_allow_html=True)
            else:
                st.info("No action log recorded.")
        
        with code_tabs[3]:
            st.json(res)

        with code_tabs[4]:
            st.markdown("#### 🛡️ Audit Vault & Retry Verification")
            st.markdown(
                "<div style='font-size:12.5px; color:#64748b; margin-bottom:12px;'>"
                "Immutable, disk-backed audit log capturing prompt contexts, scripts, diffs, agent decisions, "
                "runtime errors, fixes, and smoke test outcomes across all generation and self-healing retries."
                "</div>",
                unsafe_allow_html=True,
            )

            audit_history = list(getattr(job, "audit_history", []) or [])
            saved_folder = Path(job.saved_to) if getattr(job, "saved_to", None) else None
            trail_file = (saved_folder / "audit" / "audit_trail.jsonl") if saved_folder else None

            # Auto-discover any additional attempt folders on disk if present
            if saved_folder and (saved_folder / "audit").exists():
                known_attempts = {e.get("attempt") for e in audit_history if e.get("attempt")}
                attempt_dirs = sorted(
                    (saved_folder / "audit").glob("attempt_*"),
                    key=lambda p: int(p.name.split("_")[-1]) if p.name.split("_")[-1].isdigit() else 0,
                )
                for a_dir in attempt_dirs:
                    try:
                        num = int(a_dir.name.split("_")[-1])
                    except ValueError:
                        continue
                    if num not in known_attempts:
                        entry = {
                            "attempt": num,
                            "phase": "initial_generation" if num == 1 else "self_healing_retry",
                            "timestamp": datetime.fromtimestamp(a_dir.stat().st_mtime).isoformat(timespec="seconds"),
                            "prompts": {},
                            "code_length": 0,
                            "diff_summary": "",
                            "agent_decisions": {},
                            "errors": {},
                            "fixes": {},
                            "outcome": None,
                            "audit_path": str(a_dir.resolve()),
                        }
                        if (a_dir / "outcome.json").exists():
                            try:
                                entry["outcome"] = json.loads((a_dir / "outcome.json").read_text(encoding="utf-8"))
                            except Exception:
                                pass
                        if (a_dir / "agent_decisions.json").exists():
                            try:
                                entry["agent_decisions"] = json.loads((a_dir / "agent_decisions.json").read_text(encoding="utf-8"))
                            except Exception:
                                pass
                        if (a_dir / "errors.json").exists():
                            try:
                                entry["errors"] = json.loads((a_dir / "errors.json").read_text(encoding="utf-8"))
                            except Exception:
                                pass
                        if (a_dir / "fixes.json").exists():
                            try:
                                entry["fixes"] = json.loads((a_dir / "fixes.json").read_text(encoding="utf-8"))
                            except Exception:
                                pass
                        audit_history.append(entry)
                audit_history.sort(key=lambda x: x.get("attempt", 0))

            # Download button for audit_trail.jsonl
            if trail_file and trail_file.exists():
                try:
                    trail_bytes = trail_file.read_bytes()
                    st.download_button(
                        label="📥 Download audit_trail.jsonl",
                        data=trail_bytes,
                        file_name=f"{job.job_id}_audit_trail.jsonl",
                        mime="application/x-ndjson",
                        key=f"dl_audit_{job.job_id}",
                    )
                except Exception:
                    pass

            if not audit_history:
                st.info("No retry audit records logged yet. Run a test run or refinement to generate audit records.")
            else:
                for entry in reversed(audit_history):
                    att_num = entry.get("attempt", 1)
                    phase = entry.get("phase", "generation").replace("_", " ").title()
                    ts = entry.get("timestamp", "")
                    attempt_dir = (saved_folder / "audit" / f"attempt_{att_num}") if saved_folder else None
                    outcome = entry.get("outcome") or {}
                    if not outcome and attempt_dir and (attempt_dir / "outcome.json").exists():
                        try:
                            outcome = json.loads((attempt_dir / "outcome.json").read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    out_status = outcome.get("status", "unknown").upper()
                    status_badge = "🟢 PASSED" if out_status == "PASSED" else ("🔴 FAILED" if out_status == "FAILED" else "⚪ RUNNING/PENDING")

                    with st.expander(f"Attempt #{att_num} — {phase} ({ts}) — {status_badge}", expanded=(att_num == len(audit_history))):
                        m1, m2, m3, m4 = st.columns(4)
                        with m1:
                            st.metric("Attempt", f"#{att_num}")
                        with m2:
                            st.metric("Phase", phase)
                        with m3:
                            st.metric("Outcome", out_status)
                        with m4:
                            rows = outcome.get("extracted_rows", "—")
                            st.metric("Extracted Rows", rows)

                        t_dec, t_err, t_fix, t_diff, t_prm = st.tabs([
                            "🧠 Decisions & Diagnosis",
                            "⚠️ Errors",
                            "🔧 Fixes Applied",
                            "📜 Code Diff",
                            "📋 Prompts Context",
                        ])

                        with t_dec:
                            dec = entry.get("agent_decisions", {})
                            if attempt_dir and (attempt_dir / "agent_decisions.json").exists():
                                try:
                                    dec = json.loads((attempt_dir / "agent_decisions.json").read_text(encoding="utf-8"))
                                except Exception:
                                    pass
                            if dec:
                                root_c = dec.get("root_cause")
                                if root_c:
                                    st.markdown(f"**Root Cause Diagnosed:** `{root_c}`")
                                st.json(dec)
                            else:
                                st.info("No decision metadata recorded for this attempt.")

                        with t_err:
                            errs = entry.get("errors", {})
                            if attempt_dir and (attempt_dir / "errors.json").exists():
                                try:
                                    errs = json.loads((attempt_dir / "errors.json").read_text(encoding="utf-8"))
                                except Exception:
                                    pass
                            if errs:
                                st.json(errs)
                            else:
                                st.success("No errors recorded for this attempt.")

                        with t_fix:
                            fixes = entry.get("fixes", {})
                            if attempt_dir and (attempt_dir / "fixes.json").exists():
                                try:
                                    fixes = json.loads((attempt_dir / "fixes.json").read_text(encoding="utf-8"))
                                except Exception:
                                    pass
                            if fixes:
                                st.json(fixes)
                            else:
                                st.info("No explicit fixes object recorded.")

                        with t_diff:
                            diff_content = ""
                            if attempt_dir and (attempt_dir / "diff_from_previous.patch").exists():
                                try:
                                    diff_content = (attempt_dir / "diff_from_previous.patch").read_text(encoding="utf-8")
                                except Exception:
                                    pass
                            if diff_content:
                                st.code(diff_content, language="diff")
                            else:
                                st.caption("No diff recorded or initial script creation.")

                        with t_prm:
                            prompt_files = list((attempt_dir / "prompts").glob("*.txt")) if (attempt_dir and (attempt_dir / "prompts").exists()) else []
                            if prompt_files:
                                for p_file in sorted(prompt_files):
                                    with st.expander(f"📄 {p_file.name}", expanded=False):
                                        st.text_area(
                                            p_file.name,
                                            value=p_file.read_text(encoding="utf-8"),
                                            height=200,
                                            disabled=True,
                                            key=f"p_{job.job_id}_{att_num}_{p_file.stem}",
                                        )
                            elif entry.get("prompts"):
                                st.json(entry.get("prompts"))
                            else:
                                st.info("No prompt text files captured.")
        
        st.divider()

        # =====================================================================
        # 💡 INTERACTIVE SCRAPER REFINEMENT & FEEDBACK STUDIO
        # =====================================================================
        st.markdown("""
        <div class="banner-panel" style="border-left: 4px solid #ff5722; margin: 12px 0 20px 0;">
            <div style="display:flex; align-items:center; gap:10px; margin-bottom:8px;">
                <span style="font-size:24px;">💡</span>
                <div>
                    <h3 style="margin:0; font-size:18px; font-weight:700; color:#0f172a;">Interactive Scraper Refinement Studio</h3>
                    <div style="font-size:11.5px; color:#ea580c; font-weight:600; text-transform:uppercase; letter-spacing:0.04em;">Autonomous Recon + Codegen + Sandbox Validation</div>
                </div>
            </div>
            <div style="font-size:13px; color:#475569; line-height:1.5;">
                Need updates or new data points? Describe your desired changes in plain English below.
                The agent will autonomously perform a <strong>targeted live inspection (Recon)</strong> using Playwright to discover the new selectors/APIs,
                <strong>synthesize the updated Python scraper (Codegen)</strong>, and <strong>smoke-test it against the live site</strong> in the isolated sandbox.
            </div>
        </div>
        """, unsafe_allow_html=True)

        prompt_key = f"refine_prompt_val_{job.job_id}"
        if prompt_key not in st.session_state:
            st.session_state[prompt_key] = ""

        # Quick Suggestion Chips
        st.markdown("<div style='font-size:11.5px; font-weight:700; text-transform:uppercase; letter-spacing:0.06em; color:#64748b; margin-bottom:6px;'>Quick Prompt Inspirations:</div>", unsafe_allow_html=True)
        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        
        with col_c1:
            if st.button("➕ Add Ratings & SKU", key=f"btn_p1_{job.job_id}", use_container_width=True):
                st.session_state[prompt_key] = "Add customer ratings, total review count, and SKU / Model Code fields. Ensure they are extracted into the 'Extracted Data' Excel sheet."
                st.rerun()
        with col_c2:
            if st.button("📄 Multi-Page Crawl", key=f"btn_p2_{job.job_id}", use_container_width=True):
                st.session_state[prompt_key] = "Implement multi-page pagination crawl up to --max-products limit. Click the 'Next' page button or navigate page numbers cleanly."
                st.rerun()
        with col_c3:
            if st.button("🔍 Relax Search Filter", key=f"btn_p3_{job.job_id}", use_container_width=True):
                st.session_state[prompt_key] = "Adapt search and filter logic to match titles case-insensitively, and relax strict keyword filters so more products match."
                st.rerun()
        with col_c4:
            if st.button("⏱️ Extra Stealth & Tabs", key=f"btn_p4_{job.job_id}", use_container_width=True):
                st.session_state[prompt_key] = "Click open any accordion tabs like 'Specifications' or 'Details' before scraping, and auto-dismiss any regional cookie overlays."
                st.rerun()

        # Refinement Input Form
        refine_triggered = False
        with st.form(key=f"refine_form_{job.job_id}"):
            refine_user_instructions = st.text_area(
                "What changes or additions do you need in the scraper?",
                value=st.session_state.get(prompt_key, ""),
                placeholder="e.g. Add product ratings, customer review count, and SKU fields. Also click through pagination up to 3 pages, and extract the tech specifications table.",
                height=110,
                help="Describe any new data points, different keywords, pagination needs, or DOM navigation changes.",
            )
            
            c_rf1, c_rf2 = st.columns([1, 1])
            with c_rf1:
                refine_keyword = st.text_input(
                    "Sample Keyword (Optional Override)",
                    value=job.sample_keyword or "",
                    placeholder="e.g. Galaxy S25 Ultra",
                    help="Leave unchanged or provide a new keyword to test search/filtering flow.",
                )
            with c_rf2:
                with st.expander("⚙️ Refinement Models & Temperature"):
                    ref_recon_model = st.selectbox(
                        "Recon Model",
                        ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"],
                        index=0,
                        key=f"ref_recon_m_{job.job_id}",
                    )
                    ref_codegen_model = st.selectbox(
                        "Codegen Model",
                        ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-4o", "gpt-4o-mini"],
                        index=0,
                        key=f"ref_codegen_m_{job.job_id}",
                    )

            btn_submit_refine = st.form_submit_button("🚀 Refine & Re-Generate Scraper", use_container_width=True)
            if btn_submit_refine:
                if not refine_user_instructions.strip():
                    st.error("Please enter instructions describing what changes or data points you need.")
                else:
                    refine_triggered = True

        refine_terminal_placeholder = st.empty()
        refine_status_banner = st.empty()

        if refine_triggered:
            live_refine_logs = []
            refine_status_banner.info("⏳ **Refinement In Progress...** Running targeted Recon (Playwright live DOM inspection) ➔ Synthesizing updated Python scraper (Codegen) ➔ Executing automated smoke test in sandbox.")

            async def _on_refine_stream_log(entry):
                live_refine_logs.append(entry)
                refine_terminal_placeholder.markdown(
                    render_sandbox_terminal(live_refine_logs, is_running=True),
                    unsafe_allow_html=True,
                )

            with st.spinner("Refining scraper: Recon ➔ Codegen ➔ Automated Sandbox Verification..."):
                try:
                    asyncio.run(api_service.refine_job_backend(
                        job_id=job.job_id,
                        refinement_instructions=refine_user_instructions,
                        sample_keyword=refine_keyword,
                        recon_model=ref_recon_model,
                        codegen_model=ref_codegen_model,
                        on_stream_log=_on_refine_stream_log,
                    ))
                except Exception as ref_exc:
                    importlib.reload(api_service)
                    asyncio.run(api_service.refine_job_backend(
                        job_id=job.job_id,
                        refinement_instructions=refine_user_instructions,
                        sample_keyword=refine_keyword,
                        recon_model=ref_recon_model,
                        codegen_model=ref_codegen_model,
                        on_stream_log=_on_refine_stream_log,
                    ))

            # Reload updated job store from disk
            store.load_from_disk()
            job = store.jobs.get(job.job_id, job)
            revisions_count = len(getattr(job, "revisions", []))
            test_run_info = job.test_run or {}
            final_status = test_run_info.get("status", "completed")
            final_rows = test_run_info.get("extracted_rows", 0)

            refine_terminal_placeholder.markdown(
                render_sandbox_terminal(live_refine_logs, is_running=False, status_override=final_status),
                unsafe_allow_html=True,
            )
            refine_status_banner.success(f"🎉 **Scraper Successfully Refined (v{revisions_count})!** Updated code synthesized and verified in sandbox with **{final_rows}** extracted records.")
            st.session_state[prompt_key] = ""
            time.sleep(1.2)
            st.rerun()

        st.divider()
        
        # Test Run Section
        st.markdown("#### 🧪 Test Run Against Sample Site")
        col_test1, col_test2 = st.columns([1, 2])
        test_disabled = not bool(code_str)

        run_triggered = False
        with col_test1:
            test_max_products = st.number_input(
                "Max Products for Test Run",
                min_value=1,
                max_value=50,
                value=10,
                step=1,
                help="Controls how many products the sandbox crawls to test pagination and second-category crawling.",
                key=f"max_prod_{job.job_id}",
            )
            if st.button("🚀 Run Test Run Script", disabled=test_disabled, help="Runs the scraper in an isolated sandbox with zero timeouts, streaming execution logs, and autonomous self-healing.", key=f"run_btn_{job.job_id}"):
                run_triggered = True
        
        test_run_info = job.test_run
        with col_test2:
            if test_run_info and not run_triggered:
                status_str = test_run_info.get("status", "").upper()
                st.markdown(f"**Status:** `{status_str}`")

                # Crawl Funnel Metrics display
                funnel = test_run_info.get("funnel")
                if funnel and isinstance(funnel, dict):
                    f1, f2, f3, f4 = st.columns(4)
                    with f1:
                        st.metric("Categories", funnel.get("categories_discovered", 1))
                    with f2:
                        st.metric("Discovered", funnel.get("links_discovered", 0))
                    with f3:
                        st.metric("Processed", funnel.get("links_processed", 0))
                    with f4:
                        st.metric("Extracted", funnel.get("records_extracted", 0))
                
                if test_run_info.get("status") == "passed":
                    if test_run_info.get("self_healed"):
                        st.info("✨ **Self-Healing Auto-Repair Triggered**: Initial blockers were automatically resolved by the self-healing repair pass!")
                    extracted_count = test_run_info.get("extracted_rows", "sample")
                    st.success(f"✅ Test run passed! Collected **{extracted_count}** meaningful product records.")
                    if job.saved_to:
                        sample_output_path = Path(job.saved_to) / "sample_output.xlsx"
                        if sample_output_path.exists():
                            with open(sample_output_path, "rb") as f:
                                st.download_button(
                                    label="📊 Download sample_output.xlsx",
                                    data=f.read(),
                                    file_name=f"{job.name}_sample_output.xlsx",
                                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                    key=f"dl_sample_out_{job.job_id}",
                                )
                elif test_run_info.get("status") == "failed":
                    if test_run_info.get("self_heal_attempted"):
                        st.warning("⚠️ **Self-Healing Attempted**: A repair pass was synthesized, but the smoke test still failed. Check diagnostics below.")
                    else:
                        st.error(f"❌ Test run failed: {test_run_info.get('error', 'Zero meaningful product records extracted or execution error.')}")
                    if test_run_info.get("stderr_tail"):
                        st.code(test_run_info["stderr_tail"], language="text")

                if test_run_info.get("diagnostic_recon_findings"):
                    with st.expander("🛠️ Live Diagnostic Recon Findings (Playwright MCP)", expanded=False):
                        st.json(test_run_info["diagnostic_recon_findings"])

                # Diagnostic screenshot / message viewer (Huckberry & empty runs)
                diag = test_run_info.get("diagnostic") or {}
                diag_msg = diag.get("diagnostic_message")
                diag_img = diag.get("screenshot_path")
                if not diag_img and job.saved_to:
                    candidate_img = Path(job.saved_to) / "diagnostic_screenshot.png"
                    if candidate_img.exists():
                        diag_img = str(candidate_img)

                if diag_msg:
                    st.warning(f"🔍 **Diagnostic Note**: {diag_msg}")
                if diag_img and Path(diag_img).exists():
                    st.image(str(diag_img), caption="Diagnostic screenshot captured during run", use_container_width=True)

        terminal_placeholder = st.empty()
        status_banner_placeholder = st.empty()

        if run_triggered:
            live_logs = []
            status_banner_placeholder.info(f"⏳ Test run executing (max {int(test_max_products)} products) with zero timeouts... Streaming real-time environment creation, package installations, scraper outputs, and self-healing actions below.")

            async def _on_test_stream_log(entry):
                live_logs.append(entry)
                terminal_placeholder.markdown(render_sandbox_terminal(live_logs, is_running=True), unsafe_allow_html=True)

            with st.spinner("Executing smoke test in isolated sandbox (no timeout)..."):
                try:
                    asyncio.run(api_service._run_test_run(job.job_id, on_log=_on_test_stream_log, max_products=int(test_max_products)))
                except TypeError:
                    importlib.reload(api_service)
                    asyncio.run(api_service._run_test_run(job.job_id, on_log=_on_test_stream_log, max_products=int(test_max_products)))

            # Reload updated job from store
            store.load_from_disk()
            job = store.jobs.get(job.job_id, job)
            test_run_info = job.test_run or {}
            final_status = test_run_info.get("status", "completed")
            final_rows = test_run_info.get("extracted_rows", 0)

            terminal_placeholder.markdown(render_sandbox_terminal(live_logs, is_running=False, status_override=final_status), unsafe_allow_html=True)
            if final_status == "passed":
                status_banner_placeholder.success(f"🎉 Smoke test PASSED without timeouts! Successfully extracted **{final_rows}** meaningful product records.")
            else:
                status_banner_placeholder.error(f"Smoke test ended with status: **{final_status}**. Check log trace above.")
            st.rerun()

        elif test_run_info and test_run_info.get("logs"):
            terminal_placeholder.markdown(render_sandbox_terminal(test_run_info.get("logs", []), is_running=False, status_override=test_run_info.get("status")), unsafe_allow_html=True)

        # Live Excel Output Table Preview
        if job.saved_to:
            sample_output_path = Path(job.saved_to) / "sample_output.xlsx"
            if sample_output_path.exists():
                st.markdown("##### 📋 Extracted Data Preview (`sample_output.xlsx`)")
                try:
                    df_preview = pd.read_excel(sample_output_path, sheet_name="Extracted Data")
                    st.dataframe(df_preview, use_container_width=True)

                    # Check for unflagged blank attributes only when records were successfully extracted
                    blank_cols = []
                    if len(df_preview) > 0:
                        for col in ["brand", "category", "price", "image_url", "images", "title", "product_id"]:
                            matching = [c for c in df_preview.columns if col in c.lower()]
                            for m in matching:
                                if df_preview[m].dropna().astype(str).str.strip().eq("").all():
                                    blank_cols.append(m)
                    if blank_cols:
                        st.warning(f"⚠️ **Attribute Warning**: The following column(s) are completely blank across all rows: `{', '.join(blank_cols)}`. Consider adding targeted extraction rules.")
                    
                    # Also check Flags sheet
                    df_flags = pd.read_excel(sample_output_path, sheet_name="Flags")
                    if not df_flags.empty:
                        with st.expander(f"🚩 Audit & Observation Flags ({len(df_flags)} entries)"):
                            st.dataframe(df_flags, use_container_width=True)
                except Exception as exc:
                    st.caption(f"Could not load preview: {exc}")

        # Bottom Export Card
        if code_str and zip_bytes:
            st.markdown("<div style='height: 16px;'></div>", unsafe_allow_html=True)
            st.divider()
            b_col1, b_col2 = st.columns([3, 1])
            with b_col1:
                st.markdown("##### 📦 Standalone Scraper Package")
                st.caption("Complete drop-in scraper ready to run locally: includes `generated_scraper.py`, `input.xlsx`, and `requirements.txt`.")
            with b_col2:
                st.download_button(
                    label="📦 Download Scraper (.zip)",
                    data=zip_bytes,
                    file_name=zip_filename,
                    mime="application/zip",
                    help="Downloads generated_scraper.py, input.xlsx, and requirements.txt as a ZIP archive.",
                    key=f"dl_zip_bottom_{job.job_id}",
                )
