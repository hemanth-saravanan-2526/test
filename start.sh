#!/bin/bash
echo "Starting Scraper Studio services..."
npx -y @playwright/mcp@latest --port 8931 --host 0.0.0.0 --isolated > logs_mcp.log 2>&1 &
./.venv/bin/uvicorn api_service:app --host 0.0.0.0 --port 3683 > logs_api.log 2>&1 &
./.venv/bin/streamlit run streamlit_app.py --server.port 3682 --server.address 0.0.0.0 --server.headless true > logs_ui.log 2>&1 &
echo ""
echo "Scraper Studio is running!"
echo "Open your browser at: http://localhost:3682"
echo ""

