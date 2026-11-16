# Live Inspection & Codegen Agent Service (OpenAI-Powered)

Backend module for Scraper Studio. Runs the "OpenAI drives a real browser and
writes the scraper" workflow as an unattended service, using OpenAI models
(defaulting to **GPT-5.6 Luna** for both phases) + Playwright MCP.

## Why this exists

Earlier prototyping proved the concept but required human-in-the-loop chat sessions.
This service executes the complete inspect-then-generate workflow autonomously
as a backend component — the Builder page hits it directly, and it can also be
triggered automatically by change detection modules for continuous scraping adaptation.

## Components

| File | Role |
|---|---|
| `inspection_agent_service.py` | Core agent logic, built on a **two-phase pipeline** per job: a `recon` session (browser tools via Playwright MCP over HTTP/SSE or Stdio) that produces a compact JSON of *findings* rather than code, followed by a separate `codegen` session (fresh context, zero browser tools) that writes the standalone scraper from those findings. Powered by OpenAI (`gpt-5.6-luna` default, `temperature=1.0`), with audit logging, step capping, redundant-call dedup, and anti-bot detection. |
| `api_service.py` | FastAPI layer: `POST /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/stream` (SSE live agent log), `GET /jobs/{id}/cost`, `GET /health`, `GET /jobs`, `POST /jobs/{id}/test-run` + `GET /jobs/{id}/test-run` (executes the generated scraper against a sample input in an isolated per-job venv), `GET /jobs/{id}/sample-output` (download result). |
| `auth_config.py` | OpenAI credential provisioning — validates `OPENAI_API_KEY` (or Azure OpenAI credentials) at service startup, failing fast if missing or malformed. |
| `cost_metering.py` | Token accounting and USD calculation for the OpenAI model family (`gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-4o`, etc.) with cached token discounts, appending per-phase cost records to `cost_ledger.jsonl`. |
| `requirements.txt` | `openai`, `mcp`, `fastapi`, `uvicorn`, `pydantic`, `openpyxl`, `httpx`, `python-dotenv`. |

---

## Two-Phase Architecture & Cost Optimization

1. **Phase 1: Reconnaissance (`recon`)**
   - Drives browser tools (`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, etc.) via Playwright MCP.
   - Evaluates page structure (listing vs. detail), handles cookie overlays, tests search flows, and checks for API endpoints.
   - Guarded by **PreToolUse hooks**: audit logging, redundant-call deduplication, and anti-bot early detection (`captcha`, `cloudflare`, `perimeterx`, etc.).
   - Emits a compact JSON findings object.

2. **Phase 2: Code Generation (`codegen`)**
   - Operates in a **fresh context** with zero browser tools.
   - Input is solely the compact findings JSON and the user's extraction rules.
   - Generates production-ready Python code adhering strictly to `SCRIPT_CONTRACT`.

### Model Configuration

Both phases are independently configurable via environment variables, runtime parameters, or API requests:

| Parameter | Environment Variable | Default | Description |
|---|---|---|---|
| Recon Model | `OPENAI_RECON_MODEL` | `gpt-5.6-luna` | Fast, cost-efficient model for browser navigation and fact extraction. |
| Codegen Model | `OPENAI_CODEGEN_MODEL` | `gpt-5.6-luna` | Precision coding model for standalone scraper synthesis. |
| Fallback Model | `OPENAI_MODEL` | `gpt-5.6-luna` | Global fallback if specific phase models are unset. |
| Temperature | `OPENAI_TEMPERATURE` | `1.0` | Default temperature (optimal for GPT-5.6 Luna). |

---

## Credentials & Authentication

`auth_config.py` centralizes provisioning and validates credentials at startup:

**Direct OpenAI API Key (Default):**
```bash
export OPENAI_API_KEY=sk-...
# Optional custom base URL or Organization / Project:
# export OPENAI_BASE_URL=https://api.openai.com/v1
# export OPENAI_ORG_ID=org-...
# export OPENAI_PROJECT_ID=proj-...
```

**Azure OpenAI Service:**
```bash
export AZURE_OPENAI_API_KEY=your-azure-key
export AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
```

**Secrets Manager:** Wire custom secrets retrieval into `get_secret_from_manager()` in `auth_config.py` for automated fallback.

---

## Running the Service

### 1. Start the Playwright MCP Server
Start the standalone Playwright MCP server with HTTP transport and session isolation:
```bash
npx @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
```

## Running Scraper Studio with Streamlit (Recommended)

To run the entire application natively in Python (no Node.js required):

```powershell
# 1. Activate virtual environment
.\.venv\Scripts\activate

# 2. Run the Streamlit web application
streamlit run streamlit_app.py
```

The Streamlit UI will open at `http://localhost:8501` featuring:
- **🏠 Overview:** Live metrics, active model fleet, and quick builder CTA.
- **⚡ Builder:** Natural language rules, Excel file inputs, model tier selection, and 1-click generation.
- **🔍 Job Detail:** Live step-by-step agent trace, code syntax highlighting, test run executor against live sites, and artifact downloads.
- **📊 Dashboard:** Complete search, filter, and history table for all scrapers.
- **💰 Cost Ops:** Token economy, model tier comparison (GPT-5.6 Sol/Terra/Luna), and persistent cost ledger.
- **⚙️ Settings:** Live configuration of OpenAI API keys, base URLs, and default models saved to `.env`.

---

## Alternative: Running via FastAPI Backend & HTML UI

If you prefer running the headless API service:
```powershell
.\.venv\Scripts\uvicorn api_service:app --host 0.0.0.0 --port 8000
```
Open [`scraper_studio.html`](file:///c:/Users/MOB260005997/Scraper%20Studio/scraper_studio.html) in your browser.

