# Scraper Studio — Deployment Guide

A simple, universal guide to deploy Scraper Studio on any server (Linux, Windows, macOS, or Cloud VM).

---

## 1. Prerequisites

Ensure your server has the following installed:
- **Python 3.10+** (with `pip` and `venv`)
- **Node.js 18+** (for the Playwright browser automation server)
- **Git**
- An **OpenAI API Key**

---

## 2. Installation

### Step 1: Clone the Repository
```bash
git clone https://github.com/hemanth-saravanan-2526/scraper-studio.git
cd scraper-studio
```

### Step 2: Create Virtual Environment & Install Dependencies

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browser and system libraries
playwright install chromium
playwright install-deps
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
```

---

## 3. Configuration (.env)

Create your `.env` file from the template:

**Linux / macOS:**
```bash
cp .env.example .env
```

**Windows:**
```powershell
copy .env.example .env
```

Open `.env` and add your **OpenAI API Key**:
```ini
OPENAI_API_KEY=sk-your-openai-api-key-here
OPENAI_MODEL=gpt-5.6-luna
OPENAI_RECON_MODEL=gpt-5.6-luna
OPENAI_CODEGEN_MODEL=gpt-5.6-luna
MCP_SERVER_URL=http://localhost:8931
```

---

## 4. Run the Application

Scraper Studio runs 3 lightweight services. Start each service (in separate terminal tabs, background jobs, or tmux/screen):

### 1. Playwright MCP Server (Browser Engine)
```bash
npx -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
```

### 2. FastAPI Backend (Agent Engine)
**Linux / macOS:**
```bash
source .venv/bin/activate
uvicorn api_service:app --host 0.0.0.0 --port 3683
```
**Windows:**
```powershell
.\.venv\Scripts\Activate.ps1
uvicorn api_service:app --host 0.0.0.0 --port 3683
```

### 3. Streamlit Web UI (Frontend)
**Linux / macOS:**
```bash
source .venv/bin/activate
streamlit run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true
```
**Windows:**
```powershell
.\.venv\Scripts\Activate.ps1
streamlit run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true
```

---

## 5. Access the Web Interface

Open your browser and navigate to:
```text
http://<server-ip>:3682
```
*(If running on your local machine, use `http://localhost:3682`)*

---

## 6. Quick Start Scripts (Optional)

To start or stop all three services with a single command:

### Linux / macOS (`start.sh`)
```bash
#!/bin/bash
npx -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated > /dev/null 2>&1 &
./.venv/bin/uvicorn api_service:app --host 0.0.0.0 --port 3683 > /dev/null 2>&1 &
./.venv/bin/streamlit run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true > /dev/null 2>&1 &
echo "Scraper Studio started at http://localhost:3682"
```

### Windows (`start.bat`)
```bat
@echo off
start "Playwright MCP" cmd /c npx -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
start "FastAPI Backend" cmd /c .\.venv\Scripts\uvicorn.exe api_service:app --host 0.0.0.0 --port 3683
start "Streamlit UI" cmd /c .\.venv\Scripts\streamlit.exe run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true
echo Scraper Studio started at http://localhost:3682
```

---

## 7. Ports Summary

| Port | Service | Access |
| :--- | :--- | :--- |
| `3682` | **Streamlit UI** | Public / Open in Browser |
| `3683` | **FastAPI Backend** | Local / API Access |
| `8931` | **Playwright MCP Server** | Internal Browser Automation |
