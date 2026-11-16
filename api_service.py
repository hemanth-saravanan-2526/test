"""
Scraper Studio - Inspection API (OpenAI-powered)
=================================================
Thin HTTP layer in front of inspection_agent_service.py. This is what the
Builder page's "Generate Scraper" button calls.

Endpoints:
  POST /jobs                  - submit a new inspection job, returns job_id
  GET  /jobs/{id}              - job status + result once complete
  GET  /jobs/{id}/stream       - Server-Sent Events stream of the live action log
  GET  /jobs                   - list all jobs (backs the Dashboard table)
  POST /jobs/{id}/test-run     - run the generated script against a small sample input in an isolated venv
  GET  /jobs/{id}/test-run     - poll test-run status
  GET  /jobs/{id}/sample-output - download test-run's output.xlsx
  GET  /health                 - check service & auth health
"""

import asyncio
import difflib
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from inspection_agent_service import (
    InspectionJob,
    InspectionResult,
    run_inspection_job,
    run_codegen_repair,
    run_refinement_job,
    sanitize_generated_code,
)
from auth_config import require_credentials_at_startup, CredentialError
from cost_metering import JSONLCostSink

app = FastAPI(title="Scraper Studio Inspection API")
cost_sink = JSONLCostSink(path="cost_ledger.jsonl")

# CORS middleware for local frontend development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _validate_credentials_on_startup():
    """
    Fail fast: if OPENAI_API_KEY (or Azure equivalent) isn't configured,
    refuse to come up rather than accept jobs that will fail.
    """
    try:
        require_credentials_at_startup()
    except CredentialError as exc:
        raise RuntimeError(f"Inspection service cannot start: {exc}") from exc

    ledger_path = Path(cost_sink.path).resolve()
    print(f"[inspection-service] Cost ledger will be written to: {ledger_path}")

    # Restore job history from disk
    store.load_from_disk()


@app.get("/health")
async def health():
    """Confirms credentials are resolvable without exposing the secret."""
    try:
        creds = require_credentials_at_startup()
        return {"status": "ok", "auth_backend": creds.backend.value, "detail": creds.detail}
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/cost-ledger-location")
async def cost_ledger_location():
    path = Path(cost_sink.path).resolve()
    return {
        "path": str(path),
        "exists": path.exists(),
        "note": "Ledger is appended whenever an inspection job reaches a terminal state.",
    }


# --------------------------------------------------------------------------
# Request / Response Contracts
# --------------------------------------------------------------------------

class CreateJobRequest(BaseModel):
    name: str | None = None
    target_url: str
    sample_keyword: str | None = None
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    max_steps: int = 40
    max_seconds: int = 600
    max_budget_usd: float | None = None
    recon_model: str | None = None     # e.g. "gpt-5.6-luna"
    codegen_model: str | None = None   # e.g. "gpt-5.6-luna"
    temperature: float | None = None   # default 1.0 for luna


class JobRecord(BaseModel):
    job_id: str
    name: str
    status: str  # "queued" | "running" | "completed" | "failed" | "step_limit_hit" | "time_limit_hit" | "budget_limit_hit" | "anti_bot_detected" | "refining"
    created_at: str
    target_url: str
    sample_keyword: str | None = None
    search_rules: str = ""
    filter_rules: str = ""
    navigation_rules: str = ""
    extraction_rules: str = ""
    recon_model: str | None = None
    codegen_model: str | None = None
    result: dict | None = None
    cost: dict | None = None
    saved_to: str | None = None
    anti_bot_marker: str | None = None
    test_run: dict | None = None
    revisions: list[dict] = []
    audit_history: list[dict] = []


class AuditVaultManager:
    """
    Permanently logs all prompt context, generated scripts, unified diffs,
    agent decisions, runtime errors, fixes, and smoke test outcomes to disk.
    
    Vault directory structure:
      generated_scripts/<job_folder>/audit/
        audit_trail.jsonl           (Append-only chronological log of all attempts)
        attempt_1/
          prompts/
            recon_system_prompt.txt
            recon_task_prompt.txt
            codegen_system_prompt.txt
            codegen_task_prompt.txt
          scripts/
            generated_scraper.py
            requirements.txt
          diff_from_previous.patch
          agent_decisions.json
          errors.json
          fixes.json
          outcome.json
    """

    @staticmethod
    def compute_diff(old_code: str, new_code: str, old_label: str = "previous", new_label: str = "current") -> str:
        if not old_code:
            lines = new_code.splitlines()
            return f"--- (New file created)\n+++ {new_label}\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + l for l in lines)
        old_lines = old_code.splitlines(keepends=True)
        new_lines = new_code.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=old_label, tofile=new_label)
        return "".join(diff)

    @staticmethod
    def record_attempt(
        folder: Path,
        attempt_number: int,
        phase: str,
        prompts: dict | None = None,
        code: str = "",
        reqs: str = "",
        previous_code: str = "",
        agent_decisions: dict | None = None,
        errors: dict | None = None,
        fixes: dict | None = None,
    ) -> dict:
        folder = Path(folder)
        audit_dir = folder / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = audit_dir / f"attempt_{attempt_number}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        # 1. Permanently log prompts
        prompts_dir = attempt_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompts = prompts or {}
        for p_name, p_val in prompts.items():
            if p_val:
                safe_name = re.sub(r"[^A-Za-z0-9_]+", "_", p_name)
                (prompts_dir / f"{safe_name}.txt").write_text(str(p_val), encoding="utf-8")

        # 2. Permanently log scripts
        scripts_dir = attempt_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        if code:
            (scripts_dir / "generated_scraper.py").write_text(code, encoding="utf-8")
        if reqs:
            (scripts_dir / "requirements.txt").write_text(reqs, encoding="utf-8")

        # 3. Permanently log unified diff
        diff_text = AuditVaultManager.compute_diff(
            old_code=previous_code,
            new_code=code,
            old_label=f"attempt_{attempt_number-1}.py" if attempt_number > 1 else "empty.py",
            new_label=f"attempt_{attempt_number}.py",
        )
        (attempt_dir / "diff_from_previous.patch").write_text(diff_text, encoding="utf-8")

        # 4. Permanently log agent decisions
        agent_decisions = agent_decisions or {}
        (attempt_dir / "agent_decisions.json").write_text(
            json.dumps(agent_decisions, indent=2, default=str), encoding="utf-8"
        )

        # 5. Permanently log errors
        errors = errors or {}
        (attempt_dir / "errors.json").write_text(
            json.dumps(errors, indent=2, default=str), encoding="utf-8"
        )

        # 6. Permanently log fixes
        fixes = fixes or {}
        (attempt_dir / "fixes.json").write_text(
            json.dumps(fixes, indent=2, default=str), encoding="utf-8"
        )

        ts = datetime.now().isoformat(timespec="seconds")
        summary_record = {
            "attempt": attempt_number,
            "phase": phase,
            "timestamp": ts,
            "prompts": {k: f"{len(str(v))} chars" for k, v in prompts.items() if v},
            "code_length": len(code),
            "diff_summary": f"{len(diff_text.splitlines())} diff lines",
            "agent_decisions": agent_decisions,
            "errors": errors,
            "fixes": fixes,
            "outcome": None,
            "audit_path": str(attempt_dir.resolve()),
        }

        # 7. Append to audit_trail.jsonl
        trail_file = audit_dir / "audit_trail.jsonl"
        with trail_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary_record, default=str) + "\n")

        return summary_record

    @staticmethod
    def update_attempt_outcome(
        folder: Path,
        attempt_number: int,
        outcome: dict,
    ):
        folder = Path(folder)
        audit_dir = folder / "audit"
        attempt_dir = audit_dir / f"attempt_{attempt_number}"
        attempt_dir.mkdir(parents=True, exist_ok=True)

        outcome_file = attempt_dir / "outcome.json"
        outcome_file.write_text(json.dumps(outcome, indent=2, default=str), encoding="utf-8")

        trail_file = audit_dir / "audit_trail.jsonl"
        event = {
            "event": "outcome_update",
            "attempt": attempt_number,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "outcome": outcome,
        }
        with trail_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")


class TestRunRequest(BaseModel):
    max_products: int = 10


class RefineJobRequest(BaseModel):
    refinement_instructions: str
    sample_keyword: str | None = None
    recon_model: str | None = None
    codegen_model: str | None = None
    temperature: float | None = None


# --------------------------------------------------------------------------
# Writing the generated script to disk
# --------------------------------------------------------------------------

GENERATED_SCRIPTS_DIR = Path("generated_scripts")


def _safe_folder_name(name: str, job_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    short_id = job_id.split("-")[0]
    return f"{slug}_{short_id}" if slug else short_id


def write_generated_files_to_disk(job_id: str, name: str, generated_code: str, requirements_txt: str) -> str:
    folder = GENERATED_SCRIPTS_DIR / _safe_folder_name(name, job_id)
    folder.mkdir(parents=True, exist_ok=True)

    clean_code, _ = sanitize_generated_code(generated_code)
    (folder / "generated_scraper.py").write_text(clean_code, encoding="utf-8")
    (folder / "requirements.txt").write_text(
        requirements_txt or "# agent did not return requirements.txt\n",
        encoding="utf-8",
    )
    resolved = str(folder.resolve())
    print(f"[inspection-service] Wrote generated script to: {resolved}")
    return resolved


# --------------------------------------------------------------------------
# Test Run Execution (No Timeout, Real-Time Streaming Logs)
# --------------------------------------------------------------------------

DEFAULT_TEST_RUN_MAX_PRODUCTS = 10
TEST_RUN_MAX_PRODUCTS = DEFAULT_TEST_RUN_MAX_PRODUCTS


def _count_meaningful_rows(df) -> int:
    """Rejects empty dicts, all-blank rows, and rows containing only URLs."""
    if df is None or getattr(df, "empty", True):
        return 0
    meaningful = 0
    skip_cols = {"source url", "source_listing_url", "product_url", "detail_url", "listing_url", "url", "source", "flags"}
    core_cols = [c for c in df.columns if str(c).strip().lower() not in skip_cols]
    if not core_cols:
        return 0
    for _, row in df.iterrows():
        has_val = any(
            str(row[c]).strip() != "" and str(row[c]).strip().lower() not in {"nan", "none", "null", "{}", "[]"}
            for c in core_cols
        )
        if has_val:
            meaningful += 1
    return meaningful


def _build_sample_input_xlsx(folder: Path, record: "JobRecord") -> Path:
    from openpyxl import Workbook
    import shutil

    path = folder / "sample_input.xlsx"
    input_xlsx = folder / "input.xlsx"

    # If sample_input.xlsx already exists and has content, keep it
    if path.exists() and path.stat().st_size > 500:
        return path

    # If input.xlsx exists (e.g. uploaded by user), copy to sample_input.xlsx
    if input_xlsx.exists() and input_xlsx.stat().st_size > 500:
        try:
            shutil.copyfile(input_xlsx, path)
            return path
        except Exception:
            pass

    kw = (record.sample_keyword or "").strip() or None

    wb = Workbook()
    ws = wb.active
    ws.title = "Sample Input"
    ws.append(["keyword", "listing_url", "detail_url"])
    ws.append([kw, record.target_url, None])
    wb.save(path)
    return path


async def _run_test_run(job_id: str, on_log=None, max_products: int = DEFAULT_TEST_RUN_MAX_PRODUCTS):
    record = store.jobs.get(job_id)
    if record is None or not record.saved_to:
        return

    folder = Path(record.saved_to)
    all_logs = []

    async def emit_log(phase: str, message: str, level: str = "info"):
        entry = {
            "ts": datetime.now().strftime("%H:%M:%S"),
            "phase": phase,
            "message": message,
            "level": level,
        }
        all_logs.append(entry)
        if on_log:
            try:
                if asyncio.iscoroutinefunction(on_log):
                    await on_log(entry)
                else:
                    on_log(entry)
            except Exception:
                pass

    record.test_run = {
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "logs": all_logs,
    }
    await store._persist()

    venv_dir = folder / ".venv"
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    async def _run_streaming(*args, cwd: str | None = None, phase: str = "EXEC"):
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout_chunks = []
        stderr_chunks = []

        async def read_stream(stream, is_stderr: bool):
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    break
                text = line_bytes.decode(errors="replace")
                if is_stderr:
                    stderr_chunks.append(text)
                else:
                    stdout_chunks.append(text)
                clean_text = text.rstrip("\r\n").strip()
                if clean_text:
                    await emit_log(
                        phase=phase if not is_stderr else f"{phase}-ERR",
                        message=clean_text,
                        level="error" if is_stderr else "stdout",
                    )

        await asyncio.gather(
            read_stream(proc.stdout, False),
            read_stream(proc.stderr, True),
        )
        await proc.wait()
        full_stdout = "".join(stdout_chunks).encode("utf-8")
        full_stderr = "".join(stderr_chunks).encode("utf-8")
        return proc.returncode, full_stdout, full_stderr

    try:
        await emit_log("INIT", f"Initializing isolated test environment for '{record.name}'...", "info")
        await emit_log("INIT", f"Target: {record.target_url} | Keyword: {record.sample_keyword or 'None'}", "info")

        # 1. Environment Setup
        if not venv_python.exists():
            await emit_log("ENV", f"Creating isolated Python virtual environment at {venv_dir}...", "info")
            rc, _, stderr = await _run_streaming(sys.executable, "-m", "venv", str(venv_dir), phase="ENV")
            if rc != 0:
                err_str = stderr.decode(errors='replace')[-2000:]
                await emit_log("ENV", f"❌ Failed to create virtual environment: {err_str}", "error")
                raise RuntimeError(f"venv creation failed: {err_str}")
            await emit_log("ENV", "✅ Virtual environment created successfully.", "success")
        else:
            await emit_log("ENV", f"✅ Active isolated virtual environment detected at {venv_dir}.", "info")

        await emit_log("ENV", f"Active Python interpreter: {venv_python}", "info")

        # 2. Dependency Installation
        req_file = folder / "requirements.txt"
        if req_file.exists() and req_file.read_text(encoding="utf-8").strip():
            req_content = req_file.read_text(encoding="utf-8").strip()
            summary_reqs = ", ".join([l.strip() for l in req_content.splitlines() if l.strip() and not l.startswith("#")])
            await emit_log("DEPS", f"Installing dependencies: {summary_reqs}", "info")
            rc, _, stderr = await _run_streaming(
                str(venv_python), "-m", "pip", "install", "--no-warn-script-location", "-r", str(req_file),
                phase="PIP"
            )
            if rc != 0:
                err_str = stderr.decode(errors='replace')[-2000:]
                await emit_log("DEPS", f"❌ pip install failed: {err_str}", "error")
                raise RuntimeError(f"pip install -r requirements.txt failed: {err_str}")
            await emit_log("DEPS", "✅ Python dependencies installed successfully.", "success")
            
            # If script requires Playwright, ensure Chromium binary is available
            if "playwright" in req_content.lower():
                await emit_log("PLAYWRIGHT", "Playwright detected in requirements. Ensuring Chromium browser binary is installed...", "info")
                rc_pw, _, _ = await _run_streaming(
                    str(venv_python), "-m", "playwright", "install", "chromium",
                    phase="PLAYWRIGHT"
                )
                if rc_pw == 0:
                    await emit_log("PLAYWRIGHT", "✅ Playwright Chromium browser binaries ready.", "success")
                else:
                    await emit_log("PLAYWRIGHT", "⚠️ Playwright install chromium returned non-zero, continuing...", "warning")
        else:
            await emit_log("DEPS", "No custom requirements specified. Using standard virtual environment.", "info")

        # 3. Test Input Workbook Preparation
        await emit_log("SETUP", "Building sample_input.xlsx with target URL and keyword...", "info")
        sample_input = _build_sample_input_xlsx(folder, record)
        sample_output = folder / "sample_output.xlsx"
        if sample_output.exists():
            sample_output.unlink()
        await emit_log("SETUP", f"✅ Prepared sample_input.xlsx (Max products: {max_products}). Cleaned output path.", "success")

        # 4. Executing Scraper (No Timeout)
        await emit_log("EXEC", f"🚀 Launching generated_scraper.py in isolated sandbox (max {max_products} products, no timeout limit)...", "info")
        rc, stdout, stderr = await _run_streaming(
            str(venv_python), "generated_scraper.py",
            "--input", str(sample_input),
            "--output", str(sample_output),
            "--max-products", str(max_products),
            cwd=str(folder),
            phase="SCRAPER",
        )
        await emit_log("EXEC", f"Scraper execution process completed with exit code {rc}.", "success" if rc == 0 else "warning")

        num_extracted_rows = 0
        if sample_output.exists():
            try:
                import pandas as pd
                df_out = pd.read_excel(sample_output, sheet_name="Extracted Data")
                num_extracted_rows = _count_meaningful_rows(df_out)
            except Exception as ex:
                await emit_log("VALIDATE", f"Note: could not read output Excel: {ex}", "warning")

        await emit_log("VALIDATE", f"Output inspection: found {num_extracted_rows} meaningful product rows in 'Extracted Data'.", "success" if num_extracted_rows > 0 else "warning")

        # Update outcome for attempt 1 (or current latest attempt) in AuditVault
        initial_passed = (rc == 0 and sample_output.exists() and num_extracted_rows > 0)
        current_attempt_idx = len(record.audit_history) if getattr(record, "audit_history", None) else 1
        AuditVaultManager.update_attempt_outcome(
            folder=folder,
            attempt_number=current_attempt_idx,
            outcome={
                "status": "passed" if initial_passed else "failed",
                "returncode": rc,
                "extracted_rows": num_extracted_rows,
                "error_summary": stderr.decode(errors="replace")[-500:] if rc != 0 else "",
            },
        )
        if getattr(record, "audit_history", None) and current_attempt_idx <= len(record.audit_history):
            record.audit_history[current_attempt_idx - 1]["outcome"] = {
                "status": "passed" if initial_passed else "failed",
                "returncode": rc,
                "extracted_rows": num_extracted_rows,
            }

        # ----------------------------------------------------------------------
        # Self-Healing Loop: Auto-repair if execution failed or extracted 0 rows
        # ----------------------------------------------------------------------
        healed = False
        heal_attempted = False
        diagnostic_recon_findings = {}
        if rc != 0 or not sample_output.exists() or num_extracted_rows == 0:
            heal_attempted = True
            err_msg = stderr.decode(errors="replace")[-3000:] or stdout.decode(errors="replace")[-3000:]
            if num_extracted_rows == 0 and rc == 0:
                err_msg = (
                    "Scraper execution finished with exit code 0, but the 'Extracted Data' sheet has 0 meaningful rows. "
                    "Either all products were filtered out, selectors failed to extract attributes, or only empty/URL rows were written."
                )

            await emit_log("SELF-HEAL", "⚠️ Test run encountered issues (exit code != 0 or 0 meaningful rows extracted).", "warning")
            await emit_log("SELF-HEAL", "Triggering autonomous self-healing repair pass with live MCP diagnostic inspection...", "info")
            await emit_log("SELF-HEAL", f"Diagnostic context: {err_msg[:260]}...", "info")

            try:
                code_file = folder / "generated_scraper.py"
                broken_code = code_file.read_text(encoding="utf-8") if code_file.exists() else ""
                job_spec = InspectionJob(
                    job_id=record.job_id,
                    target_url=record.target_url,
                    sample_keyword=record.sample_keyword,
                    search_rules=record.search_rules or "",
                    filter_rules=record.filter_rules or "",
                    navigation_rules=record.navigation_rules or "",
                    extraction_rules=record.extraction_rules or "",
                    recon_model=record.recon_model,
                    codegen_model=record.codegen_model,
                )

                run_details = {
                    "exit_code": rc,
                    "num_extracted_rows": num_extracted_rows,
                    "sample_output_exists": sample_output.exists(),
                    "sample_input_path": str(sample_input),
                    "sample_output_path": str(sample_output),
                    "max_products": max_products,
                    "stderr": stderr.decode(errors="replace")[-4000:],
                    "stdout_tail": stdout.decode(errors="replace")[-4000:],
                    "error_summary": err_msg,
                }

                # Context Retention Across Retries: build attempt_history from audit_history
                attempt_history = []
                for entry in getattr(record, "audit_history", []):
                    err_entry = entry.get("errors", {})
                    diag_entry = entry.get("agent_decisions", {})
                    attempt_history.append({
                        "attempt": entry.get("attempt"),
                        "error_summary": err_entry.get("error_summary", "") or err_entry.get("stderr_tail", ""),
                        "diagnostic_summary": diag_entry.get("root_cause", "") or diag_entry.get("diagnostic_summary", ""),
                        "fixes_attempted": entry.get("fixes", {}).get("strategy", ""),
                        "outcome": entry.get("outcome", {}),
                    })

                async def on_repair_log(entry: dict):
                    tool = entry.get("tool", "")
                    tool_input = entry.get("input", {})
                    msg = f"🔧 Live MCP Recon [{tool}]: {json.dumps(tool_input, default=str)[:140]}"
                    await emit_log("SELF-HEAL", msg, "info")

                await emit_log("SELF-HEAL", "🌐 Launching live MCP browser session to inspect DOM, test selectors, and diagnose root cause...", "info")
                mcp_url = os.getenv("MCP_SERVER_URL") or os.getenv("PLAYWRIGHT_MCP_URL") or "http://localhost:8931"
                repair_res = await run_codegen_repair(
                    job=job_spec,
                    broken_code=broken_code,
                    error_context=err_msg,
                    recon_findings=record.result,
                    run_details=run_details,
                    attempt_history=attempt_history,
                    mcp_base_url=mcp_url,
                    cost_sink=cost_sink,
                    on_log_entry=on_repair_log,
                )
                repaired_code = getattr(repair_res, "repaired_code", repair_res[0] if len(repair_res) > 0 else "")
                new_reqs = getattr(repair_res, "requirements_txt", repair_res[1] if len(repair_res) > 1 else "")
                notes = getattr(repair_res, "confidence_notes", repair_res[2] if len(repair_res) > 2 else [])
                diagnostic_recon_findings = getattr(repair_res, "diagnostic_findings", {})
                prompts_used = getattr(repair_res, "prompts", {})

                if diagnostic_recon_findings:
                    root_cause = diagnostic_recon_findings.get("root_cause", "")
                    if root_cause:
                        await emit_log("SELF-HEAL", f"🔍 Live Diagnosis Root Cause: {root_cause[:200]}", "info")

                if repaired_code:
                    await emit_log("SELF-HEAL", "✅ Codegen Repair Agent synthesized code patch using live verified selectors. Writing to generated_scraper.py...", "success")
                    new_attempt_num = (len(record.audit_history) + 1) if getattr(record, "audit_history", None) else 2

                    # Permanently log retry attempt to AuditVault
                    audit_entry = AuditVaultManager.record_attempt(
                        folder=folder,
                        attempt_number=new_attempt_num,
                        phase="self_healing_retry",
                        prompts=prompts_used,
                        code=repaired_code,
                        reqs=new_reqs or (req_file.read_text(encoding="utf-8") if req_file.exists() else ""),
                        previous_code=broken_code,
                        agent_decisions={
                            "root_cause": diagnostic_recon_findings.get("root_cause", ""),
                            "diagnostic_findings": diagnostic_recon_findings,
                            "confidence_notes": notes,
                        },
                        errors={
                            "exit_code": rc,
                            "error_summary": err_msg,
                            "stderr_tail": stderr.decode(errors="replace")[-1000:],
                            "stdout_tail": stdout.decode(errors="replace")[-1000:],
                        },
                        fixes={
                            "strategy": "live_mcp_codegen_repair",
                            "working_listing_selectors": diagnostic_recon_findings.get("working_listing_selectors", []),
                            "working_detail_selectors": diagnostic_recon_findings.get("working_detail_selectors", {}),
                            "working_price_selector": diagnostic_recon_findings.get("working_price_selector", ""),
                        },
                    )
                    if not hasattr(record, "audit_history") or record.audit_history is None:
                        record.audit_history = []
                    record.audit_history.append(audit_entry)

                    # Persist official revision in record.revisions
                    if not hasattr(record, "revisions") or record.revisions is None:
                        record.revisions = []
                    record.revisions.append({
                        "version": new_attempt_num,
                        "label": f"v{new_attempt_num} (Self-Healed Retry)",
                        "created_at": datetime.now().isoformat(timespec="seconds"),
                        "prompt": f"Self-Healing: {err_msg[:60]}",
                        "generated_code": repaired_code,
                        "requirements_txt": new_reqs,
                        "cost": record.cost,
                        "status": "self_healed",
                        "sample_keyword": record.sample_keyword,
                    })

                    write_generated_files_to_disk(
                        job_id=job_id,
                        name=record.name,
                        generated_code=repaired_code,
                        requirements_txt=new_reqs or (req_file.read_text(encoding="utf-8") if req_file.exists() else ""),
                    )
                    # Re-install dependencies if updated
                    if new_reqs and req_file.exists():
                        await emit_log("SELF-HEAL", "Re-installing updated dependencies...", "info")
                        await _run_streaming(str(venv_python), "-m", "pip", "install", "--no-warn-script-location", "-r", str(req_file), phase="PIP-REPAIR")
                        if "playwright" in new_reqs.lower():
                            await _run_streaming(str(venv_python), "-m", "playwright", "install", "chromium", phase="PW-REPAIR")

                    # Re-run smoke test
                    if sample_output.exists():
                        sample_output.unlink()

                    await emit_log("SELF-HEAL", "🔄 Re-running smoke test with repaired scraper...", "info")
                    rc, stdout, stderr = await _run_streaming(
                        str(venv_python), "generated_scraper.py",
                        "--input", str(sample_input),
                        "--output", str(sample_output),
                        "--max-products", str(max_products),
                        cwd=str(folder),
                        phase="SCRAPER-REPAIR",
                    )
                    if sample_output.exists():
                        try:
                            df_out = pd.read_excel(sample_output, sheet_name="Extracted Data")
                            num_extracted_rows = _count_meaningful_rows(df_out)
                        except Exception:
                            pass
                    healed = (rc == 0 and sample_output.exists() and num_extracted_rows > 0)

                    # Update retry outcome in AuditVault
                    AuditVaultManager.update_attempt_outcome(
                        folder=folder,
                        attempt_number=new_attempt_num,
                        outcome={
                            "status": "passed" if healed else "failed",
                            "returncode": rc,
                            "extracted_rows": num_extracted_rows,
                            "healed": healed,
                        },
                    )
                    if record.audit_history:
                        record.audit_history[-1]["outcome"] = {
                            "status": "passed" if healed else "failed",
                            "returncode": rc,
                            "extracted_rows": num_extracted_rows,
                            "healed": healed,
                        }

                    await emit_log("SELF-HEAL", f"Self-healing repair pass completed. Re-run exit code: {rc}, meaningful rows: {num_extracted_rows}", "success" if healed else "warning")
            except Exception as repair_exc:
                await emit_log("SELF-HEAL", f"❌ Self-healing repair pass error: {repair_exc}", "error")

        passed = (rc == 0 and sample_output.exists() and num_extracted_rows > 0)

        # Parse Crawl Funnel Telemetry from logs
        funnel_telemetry = {}
        for entry in all_logs:
            msg = entry.get("message", "")
            if "Crawl Funnel:" in msg:
                m = re.search(r"Crawl Funnel:\s*(\d+)\s*categories,\s*(\d+)\s*links discovered,\s*(\d+)\s*processed,\s*(\d+)\s*extracted", msg)
                if m:
                    funnel_telemetry = {
                        "categories_discovered": int(m.group(1)),
                        "links_discovered": int(m.group(2)),
                        "links_processed": int(m.group(3)),
                        "records_extracted": int(m.group(4)),
                    }

        # Check for Diagnostic Screenshot or empty state notice
        diagnostic_data = {}
        screenshot_file = folder / "diagnostic_screenshot.png"
        if screenshot_file.exists():
            diagnostic_data["screenshot_path"] = str(screenshot_file)
        for entry in all_logs:
            msg = entry.get("message", "")
            if "No product records found:" in msg or "Blocked by bot protection" in msg or "HTTP anti-bot" in msg:
                diagnostic_data["diagnostic_message"] = msg

        if passed:
            await emit_log("DONE", f"🎉 Test run PASSED! Successfully extracted {num_extracted_rows} meaningful product rows.", "success")
        else:
            await emit_log("DONE", f"❌ Test run finished with status: FAILED. Meaningful rows extracted: {num_extracted_rows}.", "error")

        record.test_run = {
            "status": "passed" if passed else "failed",
            "returncode": rc,
            "extracted_rows": num_extracted_rows,
            "self_healed": healed,
            "self_heal_attempted": heal_attempted,
            "diagnostic_recon_findings": diagnostic_recon_findings,
            "funnel": funnel_telemetry,
            "diagnostic": diagnostic_data,
            "stdout_tail": stdout.decode(errors="replace")[-4000:],
            "stderr_tail": stderr.decode(errors="replace")[-4000:],
            "sample_output_ready": sample_output.exists() and num_extracted_rows > 0,
            "max_products": max_products,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "logs": all_logs,
        }

    except Exception as exc:
        await emit_log("ERROR", f"❌ Test run execution failed: {exc}", "error")
        record.test_run = {
            "status": "failed",
            "error": str(exc),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "logs": all_logs,
        }

    await store._persist()


# --------------------------------------------------------------------------
# Job Store
# --------------------------------------------------------------------------

JOBS_STORE_PATH = Path("jobs_store.json")


class JobStore:
    def __init__(self):
        self.jobs: dict[str, JobRecord] = {}
        self.live_logs: dict[str, asyncio.Queue] = {}
        self._persist_lock = asyncio.Lock()

    def load_from_disk(self):
        if not JOBS_STORE_PATH.exists():
            print(f"[inspection-service] No existing job store at {JOBS_STORE_PATH.resolve()} - starting fresh.")
            return
        try:
            raw = json.loads(JOBS_STORE_PATH.read_text(encoding="utf-8"))
            self.jobs.clear()
            self.live_logs.clear()
            for job_id, record_dict in raw.items():
                self.jobs[job_id] = JobRecord(**record_dict)
                self.live_logs[job_id] = asyncio.Queue()
            print(f"[inspection-service] Restored {len(self.jobs)} job(s) from {JOBS_STORE_PATH.resolve()}")
        except Exception as exc:
            print(f"[inspection-service] Could not load job store ({exc}) - starting fresh.")

    async def _persist(self):
        async with self._persist_lock:
            data = {job_id: record.model_dump() for job_id, record in self.jobs.items()}
            JOBS_STORE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def create(self, req: CreateJobRequest) -> JobRecord:
        job_id = str(uuid.uuid4())
        record = JobRecord(
            job_id=job_id,
            name=req.name or f"Untitled scraper - {datetime.now().strftime('%b %d, %H:%M')}",
            status="queued",
            created_at=datetime.now().isoformat(timespec="seconds"),
            target_url=req.target_url,
            sample_keyword=req.sample_keyword,
            search_rules=req.search_rules or "",
            filter_rules=req.filter_rules or "",
            navigation_rules=req.navigation_rules or "",
            extraction_rules=req.extraction_rules or "",
            recon_model=req.recon_model,
            codegen_model=req.codegen_model,
        )
        self.jobs[job_id] = record
        self.live_logs[job_id] = asyncio.Queue()
        await self._persist()
        return record

    def get(self, job_id: str) -> JobRecord:
        if job_id not in self.jobs:
            raise HTTPException(status_code=404, detail="Job not found")
        return self.jobs[job_id]

    async def update_status(self, job_id: str, status: str, result: InspectionResult | None = None):
        record = self.jobs[job_id]
        record.status = status
        if result:
            record.result = {
                "generated_code": result.generated_code,
                "requirements_txt": result.requirements_txt,
                "confidence_notes": result.confidence_notes,
                "total_tool_calls": result.total_tool_calls,
                "action_log": result.action_log,
            }
            record.cost = result.cost_record
            record.anti_bot_marker = result.anti_bot_marker

            if result.generated_code:
                record.saved_to = write_generated_files_to_disk(
                    job_id=job_id,
                    name=record.name,
                    generated_code=result.generated_code,
                    requirements_txt=result.requirements_txt,
                )
                folder = Path(record.saved_to)

                # Ensure initial attempt 1 is recorded in AuditVault
                if not hasattr(record, "audit_history") or record.audit_history is None:
                    record.audit_history = []
                if len(record.audit_history) == 0:
                    audit_entry = AuditVaultManager.record_attempt(
                        folder=folder,
                        attempt_number=1,
                        phase="initial_generation",
                        prompts=getattr(result, "prompts", {}),
                        code=result.generated_code,
                        reqs=result.requirements_txt,
                        previous_code="",
                        agent_decisions={
                            "confidence_notes": result.confidence_notes,
                            "anti_bot_marker": result.anti_bot_marker,
                            "total_tool_calls": result.total_tool_calls,
                        },
                        errors={},
                        fixes={"note": "Initial scraper generation from live inspection and codegen"},
                    )
                    record.audit_history.append(audit_entry)

                # Ensure v1 exists in revisions
                if not hasattr(record, "revisions") or record.revisions is None:
                    record.revisions = []
                if len(record.revisions) == 0:
                    record.revisions.append({
                        "version": 1,
                        "label": "v1 (Initial Generation)",
                        "created_at": record.created_at,
                        "prompt": "Initial Scraper Generation",
                        "generated_code": result.generated_code,
                        "requirements_txt": result.requirements_txt,
                        "cost": record.cost,
                        "status": record.status,
                        "sample_keyword": record.sample_keyword,
                    })
        await self._persist()


store = JobStore()


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.post("/jobs", response_model=JobRecord)
async def create_job(req: CreateJobRequest):
    record = await store.create(req)

    job = InspectionJob(
        job_id=record.job_id,
        target_url=req.target_url,
        sample_keyword=req.sample_keyword,
        search_rules=req.search_rules,
        filter_rules=req.filter_rules,
        navigation_rules=req.navigation_rules,
        extraction_rules=req.extraction_rules,
        max_steps=req.max_steps,
        max_seconds=req.max_seconds,
        max_budget_usd=req.max_budget_usd,
        recon_model=req.recon_model,
        codegen_model=req.codegen_model,
        temperature=req.temperature,
    )

    asyncio.create_task(_execute(job))
    return record


async def _execute(job: InspectionJob):
    await store.update_status(job.job_id, "running")
    queue = store.live_logs[job.job_id]

    async def push_live(entry: dict):
        await queue.put(json.dumps(entry))

    result = await run_inspection_job(job, cost_sink=cost_sink, on_log_entry=push_live)
    await store.update_status(job.job_id, result.status, result)

    phases = (result.cost_record or {}).get("phases", [])
    if phases:
        breakdown = ", ".join(
            f"{p.get('phase') or 'unknown'}=${p.get('total_cost_usd') or 0:.4f} "
            f"({p.get('num_turns')}t, cached={p.get('cache_read_input_tokens') or 0})"
            for p in phases
        )
        print(f"[inspection-service] job={job.job_id} status={result.status} {breakdown}")

    await queue.put(None)


@app.post("/jobs/{job_id}/test-run")
async def start_test_run(job_id: str, req: TestRunRequest | None = None):
    record = store.get(job_id)
    if not record.saved_to:
        raise HTTPException(status_code=400, detail="This job has no generated script yet.")
    if record.test_run and record.test_run.get("status") == "running":
        return {"status": "already_running"}

    max_products = req.max_products if (req and req.max_products > 0) else DEFAULT_TEST_RUN_MAX_PRODUCTS
    asyncio.create_task(_run_test_run(job_id, max_products=max_products))
    return {"status": "started", "max_products": max_products}


@app.get("/jobs/{job_id}/test-run")
async def get_test_run_status(job_id: str):
    record = store.get(job_id)
    return record.test_run or {"status": "not_run"}


@app.get("/jobs/{job_id}/sample-output")
async def download_sample_output(job_id: str):
    record = store.get(job_id)
    if not record.saved_to:
        raise HTTPException(status_code=404, detail="No generated script for this job.")
    sample_output = Path(record.saved_to) / "sample_output.xlsx"
    if not sample_output.exists():
        raise HTTPException(status_code=404, detail="No sample output yet - start a test run first.")
    return FileResponse(
        path=str(sample_output),
        filename=f"{_safe_folder_name(record.name, job_id)}_sample_output.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/jobs/{job_id}/cost")
async def get_job_cost(job_id: str):
    record = store.get(job_id)
    return {"job_id": job_id, "status": record.status, "cost": record.cost}


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(job_id: str):
    return store.get(job_id)


@app.get("/jobs")
async def list_jobs():
    return list(store.jobs.values())


@app.get("/jobs/{job_id}/stream")
async def stream_job_log(job_id: str):
    store.get(job_id)

    async def event_generator():
        queue = store.live_logs[job_id]
        while True:
            item = await queue.get()
            if item is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield f"data: {item}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --------------------------------------------------------------------------
# Scraper Refinement & Re-Generation
# --------------------------------------------------------------------------

async def refine_job_backend(
    job_id: str,
    refinement_instructions: str,
    sample_keyword: str | None = None,
    recon_model: str | None = None,
    codegen_model: str | None = None,
    temperature: float | None = None,
    on_stream_log=None,
) -> JobRecord:
    record = store.get(job_id)

    # 1. Retrieve current code and requirements
    prior_code = ""
    if record.saved_to:
        folder = Path(record.saved_to)
        script_path = folder / "generated_scraper.py"
        if script_path.exists():
            try:
                prior_code = script_path.read_text(encoding="utf-8")
            except Exception:
                pass
    if not prior_code and record.result:
        prior_code = record.result.get("generated_code", "")

    if not prior_code:
        raise HTTPException(status_code=400, detail="Cannot refine a job with no existing scraper code.")

    prior_reqs = ""
    if record.saved_to:
        folder = Path(record.saved_to)
        reqs_path = folder / "requirements.txt"
        if reqs_path.exists():
            try:
                prior_reqs = reqs_path.read_text(encoding="utf-8")
            except Exception:
                pass
    if not prior_reqs and record.result:
        prior_reqs = record.result.get("requirements_txt", "")

    # 2. Archive current version to revisions history
    if not hasattr(record, "revisions") or record.revisions is None:
        record.revisions = []

    # If first refinement, save v1 initial state
    if len(record.revisions) == 0:
        record.revisions.append({
            "version": 1,
            "label": "v1 (Initial Generation)",
            "created_at": record.created_at,
            "prompt": "Initial Scraper Generation",
            "generated_code": prior_code,
            "requirements_txt": prior_reqs,
            "cost": record.cost,
            "status": record.status,
            "sample_keyword": record.sample_keyword,
        })

    # Set status to refining
    record.status = "refining"
    await store._persist()

    queue = store.live_logs.get(job_id)
    if queue is None:
        queue = asyncio.Queue()
        store.live_logs[job_id] = queue

    async def push_live(entry: dict):
        if on_stream_log:
            try:
                res = on_stream_log(entry)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        await queue.put(json.dumps(entry))

    # 3. Build InspectionJob for refinement
    active_keyword = sample_keyword.strip() if sample_keyword and sample_keyword.strip() else record.sample_keyword
    job = InspectionJob(
        job_id=job_id,
        target_url=record.target_url,
        sample_keyword=active_keyword,
        recon_model=recon_model or record.recon_model,
        codegen_model=codegen_model or record.codegen_model,
        temperature=temperature,
    )

    prior_findings = record.result if record.result else {}

    # 4. Execute Refinement Pipeline (Targeted Recon + Codegen)
    result, merged_findings = await run_refinement_job(
        job=job,
        prior_code=prior_code,
        refinement_instructions=refinement_instructions,
        prior_findings=prior_findings,
        cost_sink=cost_sink,
        on_log_entry=push_live,
    )

    # 5. Update record with new result
    new_version_num = len(record.revisions) + 1
    record.status = result.status
    record.sample_keyword = active_keyword
    record.result = {
        "generated_code": result.generated_code,
        "requirements_txt": result.requirements_txt,
        "confidence_notes": result.confidence_notes,
        "total_tool_calls": result.total_tool_calls,
        "action_log": result.action_log,
        "refinement_instructions": refinement_instructions,
        "version": new_version_num,
    }

    if result.cost_record:
        if record.cost and "phases" in record.cost:
            merged_phases = list(record.cost.get("phases", [])) + list(result.cost_record.get("phases", []))
            total_cost = sum(p.get("total_cost_usd", 0) or 0 for p in merged_phases)
            record.cost["phases"] = merged_phases
            record.cost["total_cost_usd"] = round(total_cost, 6)
        else:
            record.cost = result.cost_record

    record.anti_bot_marker = result.anti_bot_marker

    # 6. Write updated files to disk
    if result.generated_code:
        record.saved_to = write_generated_files_to_disk(
            job_id=job_id,
            name=record.name,
            generated_code=result.generated_code,
            requirements_txt=result.requirements_txt,
        )
        if record.saved_to:
            try:
                _build_sample_input_xlsx(Path(record.saved_to) / "sample_input.xlsx", record.target_url, active_keyword)
                _build_sample_input_xlsx(Path(record.saved_to) / "input.xlsx", record.target_url, active_keyword)
            except Exception:
                pass

            # Permanently log refinement iteration in AuditVault
            audit_entry = AuditVaultManager.record_attempt(
                folder=Path(record.saved_to),
                attempt_number=new_version_num,
                phase="refinement",
                prompts=getattr(result, "prompts", {}),
                code=result.generated_code,
                reqs=result.requirements_txt,
                previous_code=prior_code,
                agent_decisions={
                    "confidence_notes": result.confidence_notes,
                    "anti_bot_marker": result.anti_bot_marker,
                    "refinement_instructions": refinement_instructions,
                },
                errors={},
                fixes={
                    "strategy": "user_refinement_cycle",
                    "instructions": refinement_instructions,
                },
            )
            if not hasattr(record, "audit_history") or record.audit_history is None:
                record.audit_history = []
            record.audit_history.append(audit_entry)

    # Save new version in revisions
    record.revisions.append({
        "version": new_version_num,
        "label": f"v{new_version_num} (Refined)",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "prompt": refinement_instructions,
        "generated_code": result.generated_code,
        "requirements_txt": result.requirements_txt,
        "cost": result.cost_record,
        "status": result.status,
        "sample_keyword": active_keyword,
    })

    await store._persist()
    await queue.put(None)

    # 7. Automated Smoke Test Verification
    if result.generated_code and record.saved_to:
        await _run_test_run(job_id)

    return record


@app.post("/jobs/{job_id}/refine")
async def refine_job(job_id: str, req: RefineJobRequest):
    record = store.get(job_id)
    if not record.saved_to and not (record.result and record.result.get("generated_code")):
        raise HTTPException(status_code=400, detail="Cannot refine a job that has no existing code.")
    if record.status in ("running", "refining"):
        return {"status": "already_running"}

    asyncio.create_task(refine_job_backend(
        job_id=job_id,
        refinement_instructions=req.refinement_instructions,
        sample_keyword=req.sample_keyword,
        recon_model=req.recon_model,
        codegen_model=req.codegen_model,
        temperature=req.temperature,
    ))
    return {"status": "refinement_started", "job_id": job_id}

