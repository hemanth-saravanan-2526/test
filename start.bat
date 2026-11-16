@echo off
echo Starting Scraper Studio services...
start "Playwright MCP" cmd /c npx -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated
start "FastAPI Backend" cmd /c .\.venv\Scripts\uvicorn.exe api_service:app --host 0.0.0.0 --port 3683
start "Streamlit UI" cmd /c .\.venv\Scripts\streamlit.exe run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true
echo.
echo Scraper Studio is running!
echo Open your browser at: http://localhost:3682
echo.

